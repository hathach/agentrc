---
name: debug-probe
description: "Use to update or recover a debug probe's own firmware, switch its mode or power output, or change a target chip's user config / option bytes (debug enable, RST pin, read protection) - WCH-LinkE probes and WCH chips (CH582, CH32X035, CH569, CH32V4x7, CH32H417) today. Also to read which firmware a probe runs without touching its target, and when a WCH-Link attach is refused (0x55) or a chip needs a power-off erase to get its debug pins back."
---

# debug-probe — maintain probes and chip config words

Two jobs that share one discipline: the **probe's own firmware and mode**
(`probes/<vendor>.md`) and the **target chip's config word** written through its
ISP bootloader (`chips/<family>.md`). Flashing and debugging firmware is not
this skill's: that is the vendor tool's recipe in `probes/<vendor>.md` and the
`target-debug` skill.

`scripts/probe.py` owns the mechanics; its docstring and `--help` give every
refusal. wlink-iap and wchisp run as the patched builds from `patches/` (build
below), wlink-rs as its upstream release (0.1.3 or later knows CH32V4x7);
`tools.toml` lists the probe firmware images `fw-update` accepts.

```bash
S=<skill dir>/scripts/probe.py
$S probes --version                    # every WCH probe: port, serial, mode, wlink index, firmware vNN
$S fw-update --serial S --image F      # image must be listed in tools.toml for that model
$S fw-recover --port 5-2.5 --model WCH-LinkE-CH32V305 --image F   # probe stuck in IAP mode
$S config --chip CH582 --show --wait 60
$S config --chip CH582 --set CFG_RESET_EN=1 --wait 60
```

## Rules for every probe and chip

- **Identity, never index.** Name a probe by serial or USB port. Index order
  (`wlink -d N`) changes with plugging; a bare command once erased the wrong
  board. `probe.py` and the patched backends refuse anything ambiguous.
- **Read a probe's version without its target.** `probes --version` asks the
  probe only. `wlink status` attaches and halts the target: never use it to
  learn a firmware version.
- **Firmware images are pinned.** A newer image goes into `tools.toml` with its
  source and revision before any probe gets it. There is no "latest": a probe
  on a version no listed image provides stays there until its image is found.
- **Farm probes** (ci.lan) are touched only under the `hil` skill's board lock
  (an unrostered board by an agreed name). `hil-farm-setup` pins the stock
  tools, so the patched builds run from a copy staged in a temporary directory
  there, this skill and `hil/scripts/usb_observe.py` beside them, never
  installed over the pinned ones. Bench probes on this PC are temporary; there
  is no lock.
- **Config writes**: read, change only the named fields `probe.py` allows for
  that chip, never clear boot-enable, never set read protection, read back,
  then power-cycle out of the bootloader and check the feature itself (press
  the RST pin, attach the debugger). A write that read back is not yet a
  working pin. `wchisp config reset|enable-debug|disable-debug` rewrite every
  field to the YAML's defaults: never use them as a "set one bit" (on CH58x the
  YAML's reset word is byte-swapped, see `chips/wch.md`).
- **One ISP device on the bus for `config`.** wchisp picks a device by index
  and a probe in IAP mode shares `4348:55e0` with a chip's bootloader, so
  `config` refuses while more than one is present. Unplug the other.
- **Something else may hold the bootloader.** A running virt-manager with USB
  redirection claims `4348:55e0` the moment it enumerates (`Can't claim`,
  timeouts); `usb-kernel-recover`'s triage finds the holder.

## Build the patched backends

Once per host, and again when a file in `patches/` changes. Each
lands in `~/bin` under a name carrying base and patch hash, then the plain name
is pointed at it, so the previous build stays for comparison:

```bash
(set -e; T=<skill dir>; B=$(mktemp -d); cd "$B"   # fresh tree; any failure stops before install
 W=$(sha256sum $T/patches/wlink-iap.patch | cut -c1-8); C=$(sha256sum $T/patches/wchisp.patch | cut -c1-8)
 git clone -q https://github.com/cjacker/wlink-iap; git -C wlink-iap checkout -q 8c36f5e06730938414e06beb09e0ec196fb127f0
 git -C wlink-iap apply $T/patches/wlink-iap.patch; make -C wlink-iap   # needs libusb-1.0 dev headers
 git clone -q https://github.com/ch32-rs/wchisp; git -C wchisp checkout -q 139315fc2bed971c2311c9d022aab9c13752eece
 git -C wchisp apply $T/patches/wchisp.patch; (cd wchisp && cargo build --release)
 install -m755 wlink-iap/src/wlink-iap ~/bin/wlink-iap-8c36f5e-$W; ln -sfn wlink-iap-8c36f5e-$W ~/bin/wlink-iap
 install -m755 wchisp/target/release/wchisp ~/bin/wchisp-0.3.0-139315f-$C; ln -sfn wchisp-0.3.0-139315f-$C ~/bin/wchisp)
```

`probe.py` refuses an unpatched build.

## Where things are

| File                  | What                                                                                 |
| --------------------- | ------------------------------------------------------------------------------------ |
| `probes/wch-linke.md` | WCH-LinkE: firmware update and recovery, RV/DAP mode, power, wlink-rs, 0x55 errors   |
| `chips/wch.md`        | WCH chips: debug pins, bootloader entry, config fields, what was validated           |
| `tools.toml`          | the probe firmware images `fw-update` and `fw-recover` accept                        |
| `patches/`            | wlink-iap (port/serial selection, `-V`, errors) and wchisp (`config set-word`, V4x7) |

Another vendor (ST-Link, LPC-Link2/MCU-Link, J-Link) joins as one
`probes/<vendor>.md` plus a backend in `probe.py` when a task first needs it.
