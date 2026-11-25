---
name: usb-kernel-recover
description: Use when a USB device or fixture on a HIL host (a rig, a bench PC) is wedged, not enumerating, or when processes touching USB (testusb, JLinkExe, uhubctl, openocd, libusb tools) hang in D state. Linux-host side only; a bus owned by a TinyUSB host is out of reach. Who may run which rung, the board's recovery flasher and the host's rosters come from the project's HIL contract.
---

# USB recovery on a HIL host (Linux kernel side)

**The rule:** a wedged usbfs ioctl holds that device's `device_lock`
(`usbdev_do_ioctl` takes `usb_lock_device`, the uninterruptible variant —
v6.12.96 devio.c:2609) and the driver under it waits in a plain
`wait_for_completion()` with no timeout (usbtest.c:1404; `usb_sg_wait`,
message.c:765). Nothing that also takes that lock can help. Only two levers
don't: **failing the URB at the device** (rung 1) and **the port-side data-line
drop** (rung 2).

The project's HIL contract (`hil` skill) owns the rest: which rung a dispatch may
reach, the board's recovery flasher, the rosters a host-wide reservation covers, and
the host's own layout and limits. `<skill dir>` below is this skill's directory, the
installed copy or the project's pinned one, as its contract says; the script runs
under `sudo` by path.

## 1. Triage: find the holder

```bash
ps -eo pid,stat,etimes,wchan:22,args | awk '$2 ~ /D/'
sudo cat /proc/<pid>/stack          # never opens the node, so it cannot block
```

- **`S` = victim.** Lock-taking sysfs *reads* use `usb_lock_device_interruptible`
  (sysfs.c:124-139, 11 sites), so readers are killable and `timeout` bounds them.
  Ignore them; they unwind by themselves.
- **`D` = the holder, or a writer that took the uninterruptible path.**

| Stack shows | Meaning | Go to |
|---|---|---|
| `test_queue [usbtest]` → `usbtest_ioctl` → `usbdev_ioctl` | **owner, holds the lock**, waiting on queued transfers | rung 1 (DUT), then recheck; park-flash if no disconnect follows |
| `usbdev_ioctl` + another driver-module wait | **owner, holds the lock** | inspect the wait; rung 3 — terminal if the permitted reset/disconnect cannot release it |
| `usbdev_ioctl`, no driver frames | owner waiting on a URB | rung 1 (DUT) / rung 2 (probe) |
| `usbdev_open`, sysfs reads | victim | ignore |
| `tee .../usbtest/bind`, `unbind` | **victim that SPREADS it** | stop issuing them |
| `hub_event` in a kworker | teardown stuck behind an owner | find and recover the owner first; rung 3 if its wait cannot be released |

Driver-bind writes are not passive: `__device_driver_lock` (drivers/base/dd.c)
takes `device_lock()` uninterruptibly **and `device_lock(parent)`**, because
`usb_bus_type` sets `.need_parent_lock = true` (driver.c:2048) — each one holds
the HUB's lock, which is how one wedged port takes a whole bus down.

A `test_queue` owner waits with no timeout for its queued URBs (usbtest.c:2126,
v6.12.113); `complicated_callback` stops resubmitting after a failed URB and
completes the wait once none is pending (usbtest.c:1926-1968). One occurrence
(ci.lan, 2026-11-24, tinyusb's `ea4088_quickstart`, `testusb -t 28`) cleared at
rung 1 with no reboot. In this occurrence the reset was followed by disconnect,
the queue's error report (`during the test, 32 errors out of 0`, with `-g 32`
queued URBs on test 28, a bulk read) and owner exit, consistent with outstanding
transfers completing or failing until the callback released the wait (rung 1's
mechanism).

For this `test_queue` case:

- **Cleared:** the owner exits, the DUT re-enumerates and a fresh D-state check
  finds no holder. A flasher exit or a disconnect line alone is not enough.
- **Disconnected, owner still in D state:** rung 3 only once rung 1 has issued
  the reset — the flasher reported it, or, where the reset cannot reach the
  peripheral, park-flash's flasher reported a successful flash — and a fresh
  D-state check, taken after the disconnect and the pending URB completions had
  time to settle, still finds the owner.
- **No DUT `USB disconnect` line after a reported reset, owner still in D state
  at a fresh check:** park-flash next. A task holding or waiting on the DUT's
  parent hub, a device-level bind/unbind writer (`__device_driver_lock`, above)
  or a `hub_event` kworker for that hub, holds that line back: `hub_event` takes
  the hub's lock (`usb_lock_device(hdev)`) before `usb_disconnect` logs it
  (hub.c). With such a task the missing line shows neither that the reset
  missed the peripheral nor that it reached it, so it never counts as the
  disconnect rung 3 needs; park-flash's outcome, below, decides.
- **That park-flash's flasher reports a successful flash, still no
  disconnect:** if a fresh check still finds the owner, escalate to rung 3, or
  to the contract's terminal recovery where the dispatch may not reach it.
- **Park-flash reports no successful flash:** it proves nothing. A probe it
  cannot reach goes to rung 2; any other failure goes to the contract's
  terminal recovery, never rung 3.
- **Owner exited, another holder remains at a fresh check:** triage that one
  by its stack.
- **No holder left, no DUT:** the lock is free, so this is no longer a kernel
  wedge. If the DUT still has a sysfs node, use section 4; with none,
  `authorized` has nothing to act on, so go straight to the board's own
  recovery (park-flash). Never rung 3.

Map the holder to a busport with **lock-free attrs only** (`devnum`, `idVendor`,
`idProduct` are `usb_descriptor_attr*`, plain `sysfs_emit`, sysfs.c:688-705):

```bash
for d in /sys/bus/usb/devices/<bus>-*/; do
  [ "$(cat $d/devnum)" = "<devnum>" ] && echo "$d $(cat $d/idVendor):$(cat $d/idProduct)"
done
grep -l <SERIAL> /sys/bus/usb/devices/*/serial    # only on a HEALTHY device
```

## 2. Shield first (prerequisite for anything using libusb)

A wedged device blocks every enumerator that reads its locking attributes —
JLinkExe, uhubctl, openocd's HID fallback. `chmod 000` makes the VFS reject the
read before `->show()` runs, so they skip it and keep enumerating. `chmod` never
blocks (inode setattr, no `show()`), so it works on a fully wedged device.

```bash
sudo <skill dir>/scripts/usb_recover.sh shield <busport> $$    # leaf + parent hub + root hub, modes recorded first
sudo <skill dir>/scripts/usb_recover.sh shield-status          # who holds what, owner alive/dead, what re-enumerated
sudo <skill dir>/scripts/usb_recover.sh unshield <busport> $$  # restore the recorded modes; the owner pid, or none once it is dead
```

- The owner pid is the recovery session (your shell, the orchestrating process),
  not the short-lived `sudo`: a stale shield is one whose owner is gone, and
  `shield-status` says so. Never unshield blindly; a live foreign owner is refused.
- The root hub is shared, so a second shield touching it is refused: finish or
  unshield the first one.
- **Run the recovery tool as NON-root**: root has `CAP_DAC_OVERRIDE`, ignores the
  `000`, and blocks anyway.
- Leaf shields vanish on re-enumeration (new kobject, new inodes); `unshield`
  restores only the surviving originals and reports the rest as gone. It never
  copies modes from a sibling: two of the nine are 644 where the rest are 444.
- **JLinkExe always needs it**, even with `-USB`/`-SelectEmuBySN`: to find its probe
  it reads `product`, `serial` and `bNumInterfaces` of every USB device on the host
  (strace, 2026-09-21), and those are served under each device's lock.
- **Not needed for a convoy-safe flasher**: openocd pinned with `vid_pid` or over
  `interface/jlink.cfg`, and `STM32_Programmer_CLI`, skip a foreign device before
  `libusb_open` and read no other device's locking attributes. The contract's
  Recovery says which of the board's flashers is convoy-safe.
- The state dir is root-owned: `shield-status` without `sudo` cannot read it and says
  so, never "no shields".

## 3. The rungs — go straight to the one triage names

**Rung 1 — wedged DUT: reset it through its own probe.** The contract's Recovery
names the board's recovery flasher (its `flasher_recover`, else its primary flasher, both
from agentrc's board registry, `hil` skill) and the probe serial. A convoy-safe one (section 2) needs no shield; for openocd:

```bash
openocd -c "adapter serial <probe-sn>" <flasher args> -c "init; reset run; shutdown"
```

When the flasher args name `target/rp2350.cfg`, halt both cores first, as the
harness's `reset_openocd` does: openocd's SMP reset leaves core1 running while core0
resets, and an RCP fault there can leave core0 stalled in its NMI handler (`hil_flash.py`):

```bash
openocd -c "adapter serial <probe-sn>" <flasher args> -c init -c "catch halt" \
        -c "init; reset run; shutdown"
```

For stlink: `STM32_Programmer_CLI --connect port=swd sn=<probe-sn> --rst --go`.

Any other runs only behind the shield (section 2), then unshield; for JLink:

```bash
sudo <skill dir>/scripts/usb_recover.sh shield <busport> $$
printf "r\ng\nq\n" > /tmp/rec.jlink
JLinkExe -device <DEV> -if SWD -speed 4000 -SelectEmuBySN <probe-sn> \
         -autoconnect 1 -nogui 1 -CommandFile /tmp/rec.jlink
sudo <skill dir>/scripts/usb_recover.sh unshield <busport> $$
```

Reset **before** park-flash: non-destructive (the firmware under test survives
for autopsy), no flash wear, and no bad park image — a `wfe`/`wfi` park has
bricked SWD on mimxrt1064_evk and max32666fthr through a power cycle.
`ResetTarget` measures 128-129 ms; cleared 57 → 0, 26 → 0 and 5 → 0 D-state
processes, single shot each. Mechanism: chip reset drops the pull-up → the
device detaches → the outstanding URBs complete or fail → the completion fires →
the ioctl returns → the lock releases. A disconnect does not flush them under the
lock: `usb_disconnect` reaches `usb_hcd_flush_endpoint` (`-ESHUTDOWN`, hcd.c:1783)
only after `usb_lock_device`, so that flush runs once the owner has let go (hub.c
`usb_disconnect`; usbtest.c's warning above `usbtest_ioctl`).

Works on i.MX RT (`USBCMD.RS` = 0 detaches, RT1050 RM Rev 3 p.2453) **and on
DWC2** — measured 2026-08-16 on stm32f407disco: `r; g` gave
`usb 13-2.2: USB disconnect, device number 107`, re-enumerating 325 ms later.
(A bare **halt** does not: the core keeps running with the pull-up asserted.)

**Park-flash** is the fallback where the reset cannot reach the peripheral; the
project's harness automates it (the contract's Recovery names the flags). Delivery
must be convoy-safe: **openocd pinned with `vid_pid`** or over `interface/jlink.cfg`,
`STM32_Programmer_CLI`, or esptool (`-p <ttyACM>`). JLinkExe needs the shield (section 2).
A harness may also send Reset_TT to a full- or low-speed device's hub before a flash;
that is the harness's own pre-flash lever, not a rung here.

**Rung 2 — wedged PROBE: `root-cycle`.** A probe has no probe to reset it, so the
port-side drop is the only lever left. `scripts/root_port_cycle.py` sends
ClearPortFeature(PORT_POWER), then SetPortFeature after 5 s, to the one root port
feeding the target, over that root hub's own usbfs node: it opens no child, no other
hub and no USB 3 companion bus (uhubctl opened every hub and the root port's children
first, agentrc#149). Its sysfs checks are lock-free, and a check run validates the
root hub and its per-port power switching before the power-off; off and on only
re-verify the root hub's identity. Each run is supervised (15 s, then SIGKILL).

It still takes the root hub's own `device_lock` (`usbdev_open`, every usbfs ioctl:
uninterruptible), so a root hub lock already held, such as `hub_wq` disconnecting the
wedged child under it, blocks it in D state. The script then names the surviving pid,
sends no further request and fails (exit 1). The reservation below then prints a BLOCKER
line with its token, logs the blocker in each board's cleanup log and keeps the host
until `hil_lock.py release --token <token>` once the survivor ended, so one in D state
holds it until the reboot; its logged failure stays open until an operator acknowledges it
(`hil_lock.py resolve`). The software rung failed, so follow the contract's terminal
recovery. It is not a guaranteed escape, only the rung that opens nothing but the root hub.

That is a different lock from the host's **board flocks**, and this rung still needs
those: it bounces every fixture under the root port, including boards another job is
mid-flash on. `hil_reserve.py` (`hil` skill) takes them all, around the cycle:

```bash
python3 <hil skill dir>/scripts/hil_reserve.py run root-cycle <busport> \
    --roster <roster> ... --reason "root-cycle <busport>"
```

One `--roster` per project with boards on this host (the contract's Hosts/rosters
names them). `hil_reserve.py --help` has its
coverage, hold and release rules; a failed cycle releases every lock unless a descendant
survives (above) or its host fence cannot be cleared. Read its BLOCKER line: `holding`
means the host is kept and its token is the one `hil_lock.py release --token` takes;
`released` means the locks are gone and the failure is only logged. A refusal
naming a runner's holder (`hil_test.py`) means CI is mid-test — wait, do not force.
Give it the wedged probe's own busport (e.g. `13-1.6`), not the `13-1` hub path: the
script derives the root port itself, and the serial guard and the success check both
read the path you pass.

What a root-port cycle reaches is the hub's business: a hub declares ganged or per-port
power switching in `wHubCharacteristics` (USB 2.0 §11.11, Table 11-13), and a host
controller may advertise `ppps` yet never drop VBUS, so unless the contract's Limits
say this host cuts power, treat it as a forced re-enumeration, never a power cycle — a
device whose firmware is wedged can ride it out. Success is the sysfs inode changing,
not the requests' exit status.

**Not a rung — a healthy leaf's port power-off/on: `hub-cycle`.** For a device that
still answers, `hil_reserve.py run hub-cycle <busport>`, under the same reservation.
Success proves re-enumeration, not a power cycle. A wedged device is never hub-cycled:
the request runs under the hub's lock, so the script refuses a target whose serial
does not answer, and the rungs above own it. Its guards, timing and the hub-dependent
VBUS caveats are `usb_recover.sh`'s usage and its hub-cycle comment.

**Rung 3 — terminal case: a driver ioctl that OWNS the lock** in a wait the permitted
reset/disconnect cannot release (section 1's table). No software cure:
the task is uninterruptible and SIGKILL is queued, not delivered. Reboot with
**sysrq**, never `reboot(2)` — a graceful reboot runs `device_shutdown()`, which
takes every device lock and stalls on the wedged one.

```bash
echo b | sudo tee /proc/sysrq-trigger     # after: sync; sudo umount -a
```

**Rung 4 — hypervisor**, where the host is a VM and the contract names its
hypervisor: `qm stop <vmid> && qm start <vmid>` from the PVE host, never needed in
eight recorded wedges. A VM *reboot* is not reliable — hubs can latch across the
PCIe reset.

## 3b. If the CONTROLLER is dead, not a device

Signature: `xhci-pci-renesas <addr>: Timeout while waiting for setup device
command`, devices on that controller failing to enumerate, or its buses gone —
as opposed to ONE device wedged. The rungs above cannot help; the controller
itself needs re-initialising.

```bash
python3 <hil skill dir>/scripts/hil_reserve.py run pci-rebind <pciaddr> \
    --roster <roster> ... --reason "pci-rebind <pciaddr>"   # unbind + bind the whole xHCI
python3 <hil skill dir>/scripts/hil_reserve.py run pci-bind <pciaddr> \
    --roster <roster> ... --reason "pci-bind <pciaddr>"     # only if it ends up driverless
```

On a successful rebind every fixture re-enumerates, and **it renumbers every bus that controller owns** (observed: buses
17 and 18 came back as 1 and 2), so re-derive busports afterwards from the bus map the reservation prints. Every fixture
on the controller goes down with it, so it runs only under that reservation, given every roster on the host, and only
where the contract's Reservation allows it; `pci-bind` under the same reservation recovers a controller left driverless.
Prerequisites: every project on the host pinned at or after the agentrc whose `hil_lock` checks the host fence, and the
rig's root-owned `/usr/local/sbin/usb_recover`, which the reservation prefers, refreshed by `hil-farm-setup`.
`hil_reserve.py --help` has what it checks, holds and judges.

Do NOT reach for it while a device-lock convoy is live — see Common mistakes.

## 4. If nothing is in D state

The device is dead or silent, not wedged. `sudo <skill dir>/scripts/usb_recover.sh authorized
<busport>` unconfigures and reconfigures it (`usb_set_configuration(dev, -1)`
then re-choose, hub.c) — it fixes stale driver/interface state, does **not**
replug: the `usb_device` survives, so most probes keep their sysfs node. If that
does not take, the device is wedged rather than silent — go to rung 1 or 2.
`resolve <dev-node>` maps `/dev/ttyACM3` → busport.

**It takes `usb_lock_device` uninterruptibly** (hub.c `usb_deauthorize_device`),
so it is safe only while nothing is in D state.

## 5. Before declaring the host healthy

```bash
ps -eo stat,args | awk '$1 ~ /D/' | wc -l    # must be 0
timeout 15 lsusb                             # rc 0 and a sane device count
sudo uhubctl -l <bus> -p <port>              # "0000 off" = never came back
sudo uhubctl -l <bus> -p <port> -a on
```

Observed: 5 boards missing with a completely clean D-state list, because
`usb17-port2` sat at `disable=1`.

## Common mistakes

- **`uhubctl -a cycle` on a root port without `-S`.** It writes sysfs
  `disable`, and `disable_store` takes `usb_lock_device(hdev)` uninterruptibly
  then calls `usb_disconnect(child)` inside it (port.c) — against a wedged child
  that blocks while holding the root hub's lock, poisoning the bus.
  `usb_recover.sh` sends the hub request over usbfs instead.
- **`echo 1 > .../remove`** to make a wedged device "go away": `remove_store` is
  the one attribute in sysfs.c taking the uninterruptible `usb_lock_device`
  (sysfs.c:765). It joins the convoy instead of clearing it.
- **`authorized` on anything wedged** — same uninterruptible lock. Driver
  unbind/bind (`/sys/bus/usb/drivers/usb/{unbind,bind}`) does the same
  unconfigure/reconfigure via `usb_generic_driver_disconnect` (generic.c) but ALSO
  takes the parent hub's lock (`need_parent_lock`), so it is strictly worse; it was
  removed from `usb_recover.sh` for that reason.
- **`pci-rebind` for a wedged DEVICE.** It is the cure for a dead CONTROLLER (see
  above), not for a device-lock convoy: with a live D-state URB the re-bind can
  hang and leave the controller with **no driver** and every fixture offline
  (observed once). Recover that with `pci-bind <addr>`.
- **Writing `/sys/bus/pci/devices/<addr>/reset`** — a controller without FLR turns
  it into a bus reset behind a live driver: card halted, host power cycle.
- **Resetting a victim's board.** Two boards were reset innocently before anyone
  found the holder. Map by `devnum`, not by which board "should" be running.
- **Assuming one controller.** Observed: 26 D-state processes across three xHCI
  controllers, all cleared by one probe reset on one device.

## Limits

- The script's `udevadm` call runs without a timeout; the serial and product reads are
  bounded (2 s), and each port request (a `disable` write, a `root_port_cycle.py` run)
  is supervised (15 s, then SIGKILL; a survivor ends the cycle).
- `root-cycle` with a serial whose read times out warns and proceeds (the wedge
  signature is an unreadable serial): this is how `hil_reserve.py run` hands over a
  wedged target. It admits one by udev's record of its serial, written when
  the device was added and read without touching it, refuses one with neither, and
  counts the subtree restored only from sysfs reads afterwards, never from that record.
- `hil_reserve.py run root-cycle` has not been run against a real wedge; its
  hardware trial is agentrc#149.
- `hil_reserve.py run pci-rebind` and `pci-bind` have not been run on hardware, and
  when a rebind strands its controller is still unmeasured; the experiment that settles
  the Common-mistakes entry above is agentrc#236.
- `sudo` runs a user-owned script from a user-writable checkout: the existing trust
  boundary of the install, not widened by this skill.
- Which hubs and controllers a host has, whether a port cycle cuts power there, and
  which buses renumber are the contract's Hosts/rosters and Limits, not this page.
