---
name: usb-sniffer
description: "Use when you need wire-level USB evidence host capture cannot give: a device that never enumerates, usbmon showing only Submits, NAK storms, STALL, babble, bus-reset or enumeration timing, or a link with no Linux host (MCU host, gadget peer). LS/FS/HS packets to pcapng via the ataradov usb-sniffer."
---

# usb-sniffer — wire-level capture with the ataradov hardware analyzer

The layer below URBs: usbmon (`usb-kernel-debug` in projects that have it)
shows what a Linux host exchanged, a target-side debugger shows what the
firmware did, the sniffer shows **what actually crossed D+/D-** (PIDs,
handshakes, resets, timing). Reach for it when usbmon can't see (device never
binds, pre-enumeration failures), can't be trusted (URB completed, but did the
wire really ACK?), or doesn't exist (an MCU host runs no kernel; a Linux
gadget's UDC bypasses usbmon). Where a Linux PC is the host, usbmon is
cheaper — no hardware, no locks.

`scripts/sniff.py` owns the mechanics: `capture` (bounded, at the tapped
segment's speed) and `addr` (the DUT's wire address out of a capture).

## Find the sniffer and what it taps

```bash
lsusb -d 6666:6620          # capture port present? (github.com/ataradov/usb-sniffer)
```

The sniffer is a passive tap: host-side and device-side connectors pass
through, the capture port is a separate USB device. What it taps is a cabling
fact to confirm every session, not assume: capture, provoke known control
traffic to a candidate (`lsusb -v -s <bus>:<dev> >/dev/null`), and see whether
those requests appear on the wire. If the tapped board is shared rig hardware,
hold whatever lock the project uses for anything that resets or reflashes it;
capturing alone perturbs nothing and the sniffer itself needs no lock.

## Capture

```bash
S=<skill dir>/scripts/sniff.py
$S capture raw.pcapng --port 3-2.7 --seconds 30 --limit 3000000 --fold   # speed from the tapped port's sysfs entry
$S capture raw.pcapng --speed hs --seconds 15                            # explicit speed
```

- The speed must be the tapped **segment's**: an FS device behind an HS hub
  is HS on the hub's upstream cable (you will see SPLIT transactions, not
  native FS packets — tap the device's own cable at `fs` for those). Wrong
  speed = no USB packets, only the sniffer's own records ("Line state: SE0",
  "VBUS ON", "Detected speed: Full-Speed"): the script fails such a capture,
  keeps the file and prints the records; fix the speed before doubting the
  hardware.
- Every capture takes `--seconds`, a wall-clock bound: `--limit` alone never
  ends on an idle bus or an unfired trigger, and HS runs 15–20 MB/s even with
  `--fold` whenever any device on the bus is busy (`--fold` collapses only
  truly empty frames). `--limit` on top ends it earlier, at N packets.
  It also refuses an existing output file, so a tool killed at the time
  bound can never pass off an older capture as this one. `--seconds` kills
  the tool, which closes its file on no signal, so the script runs the cut
  file through `editcap` in place; `--limit` ends cleanly by itself.
- For one-pass enumeration use `--limit 3000000` (~6–7 s, ~120 MB at HS):
  shorter windows have provably missed the ladder when the trigger's latency
  varied (a debug-probe connect takes 0.5–4 s run to run); longer ones only
  make every later tshark pass slower. `--trigger low|high|falling|rising`
  arms on the external trigger pin instead of starting at once; a trigger that
  never fired within `--seconds` fails the capture ("never fired").
  The pin is the pad pair J4 "IN" / J5 "GND" (FPGA pin 41, 3.3 V LVCMOS, pulled
  up), unpopulated as shipped: with nothing soldered it reads high, so `high`
  fires at once and `low`/`falling`/`rising` end "never fired" (all four seen
  2026-09-18). The only trigger ever seen to FIRE is `high` on the bare pad; the
  edge and `low` modes firing needs a wire on J4.
- Start the capture FIRST, then the event. Triggers: a debug-probe reset of
  the DUT gives the full ladder including SET_ADDRESS; `USBDEVFS_RESET`
  (`usbreset`, or the ioctl from Python) makes the host re-address the device,
  also a full ladder; de/re-authorizing the sysfs port may reuse the address
  and skip SET_ADDRESS — fine for descriptor reads, useless for addressing or
  reset timing.
- Tool diagnostics: `USB_SNIFFER_LOG=/tmp/sniffer.log`.

## Reading the capture

```bash
$S addr raw.pcapng                                          # wire address(es) from SET_ADDRESS, each with its anchored usbll.src/dst filter
tshark -r raw.pcapng -Y 'usb.bmRequestType'                 # the control ladder
tshark -r raw.pcapng -Y 'usb.bDescriptorType == 1' -T fields -e usb.idVendor -e usb.idProduct   # VID:PID off the wire
tshark -r raw.pcapng -Y 'usbll.pid'                         # raw token/handshake level
editcap -r raw.pcapng slice.pcapng <first>-<last>           # trim huge captures
```

On a capture >100 MB make exactly ONE filtered pass (the ladder filter) to
find the frame window of your event, `editcap -r` to it, and analyse the
slice — repeated broad passes over a 300 MB raw turn a 5-minute job into 15.

The DUT's wire address comes from the capture, never from lsusb: **on xHCI
hosts the lsusb device number is not the wire address**. `addr` refuses a
capture with no SET_ADDRESS (started after enumeration, or the DUT is not on
the tap) and lists every device when several enumerated — pick by frame order.

## What the wire really shows (read before concluding anything)

- **Downstream is broadcast.** Tokens, SETUP and OUT data addressed to EVERY
  device on the tapped segment appear; upstream (DATA answering IN) only from
  devices on the tapped branch. Lone IN→ACK pairs without DATA to some other
  address are normal, not corruption.
- **The sniffer can capture its own upload.** If its capture port shares the
  host controller bus with the tap, its bulk-IN polling floods the capture
  (easily >90% of packets): filter by the DUT's address; for surgically clean
  captures move the capture cable to another host controller.
- **Port-reset visibility depends on the tap point.** Tapping the DUT's own
  cable, a reset reaches the sniffer PHY: explicit `--- Bus Reset ---` /
  `Detected speed:` Syslog records. Tapping a hub's upstream, the hub isolates
  downstream resets and no marker appears; anchor timing on the hub
  choreography instead — SetPortFeature(PORT_RESET) to the hub's address is
  the start, ClearPortFeature(C_PORT_RESET) the end (start capturing before
  triggering or the SetPortFeature is missing). Idle captures contain benign
  multi-ms gaps: do not read every gap as a reset.
- Answers come from packet payloads (SETUP/DATA hex), not from host-side
  logs — that is the point of being on the wire; if an answer is not in the
  capture, say so rather than approximating from sysfs or dmesg.

## Setup (one-time)

```bash
sudo cp <skill dir>/scripts/88-usb-sniffer.rules /etc/udev/rules.d/   # capture port + blank FX2LP
sudo udevadm control --reload-rules && sudo udevadm trigger -s usb
# binary from the ataradov repo (bin/usb_sniffer_linux) + Wireshark extcap symlink (Wireshark >= 4.x)
cp usb_sniffer_linux ~/.local/bin/usb_sniffer && chmod +x ~/.local/bin/usb_sniffer
mkdir -p ~/.local/lib/wireshark/extcap && ln -sf ~/.local/bin/usb_sniffer ~/.local/lib/wireshark/extcap/usb_sniffer
```

Never run `--mcu-eeprom` / `--fpga-flash` / `--fpga-erase` against a working sniffer: those program NEW hardware.

## At session end

Delete or `editcap`-trim multi-hundred-MB raws before handing off, release
any board lock, and leave no capture process behind (`pgrep -a usb_sniffer`;
`capture` refuses to start while one runs).
