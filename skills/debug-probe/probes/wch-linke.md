# WCH-LinkE

A CH32V305 probe. USB IDs: `1a86:8010` in RV mode (WCH SDI targets), `1a86:8012`
in DAP mode (Arm targets over CMSIS-DAP), `4348:55e0` in its own IAP mode, the
same ID as a WCH chip's ISP bootloader. Holding IAP while powering the probe
enters IAP mode (blue LED blinks); holding ModeS while powering it switches
RV/DAP, on WCH-LinkE R0-1v2 and later (WCH-LinkUserManual V2.7, book 2156, §1.2
p2 and p17).

## Firmware

- **Version.** `probe.py probes --version`, from `wlink-iap -V`: the probe's
  answer to `81 0d 01 01`, no target attach. WCH counts `vNN = major*10 +
  minor` (2.22 is v42). wlink-rs prints the same probe as `v2.22(v42)` in 0.1.2
  and `v3.6(v42)` in 0.1.3: trust the `(vNN)`. The USB descriptor's
  `bcdDevice`, readable from sysfs without opening the probe, matched `-V` in
  RV mode on every WCH-LinkE (v39, v43) and WCH-Link-CH549 (v32) read on
  2026-11-11 (`0223` is 2.23, v43); other versions are unverified, and the model
  is not in it.
- **Tool.** Patched wlink-iap only. wchisp cannot: the probe's IAP mode speaks
  the WCH-Link `0x81` protocol, not WCHISP's, so wchisp 0.2.3 and 0.3.0 fail
  with "response command type mismatch" or a timeout (2026-07-08). wlink-rs has
  no firmware command.
- **Image.** `FIRMWARE_CH32V305.bin`, the app-only image in WCH-LinkUtility's
  `Firmware_Link/`; its `wchlink.wcfg` names the version (`CH32V307Ver=43`).
  Fetch it through WCH's headless API as `download-doc`'s `vendor_wch.py`
  describes, with the page name `WCH-LinkUtility_ZIP.html` (it gives the
  package's version, date and file id). The GitHub mirror
  `21km43/WCH-Link_Firmware` lags (v40 when WCH shipped v43). Never
  `WCH-LinkE-APP-IAP.bin`: that is the 8 KB IAP loader plus the app, for a probe
  reflashed over SWD or the factory bootloader. `tools.toml` lists what
  `fw-update` accepts.
- **No button for an update.** `fw-update` switches a running probe into IAP
  over USB (`81 0f 01 01`); it re-enumerates as `4348:55e0` on the same port,
  is flashed and verified, and returns in RV mode with its serial.
- **Recovery.** The IAP button held at power-up is only for a probe whose
  firmware no longer answers USB. A probe left in IAP mode (button pressed,
  update interrupted) enumerates as `4348:55e0`: `probe.py fw-recover --port
  <port> --model WCH-LinkE-CH32V305 --image <image>`. Its IAP mode showed
  `bcdDevice` 1.00 and no strings, where the chip bootloaders seen here showed
  their own versions (24.00, 27.00); do not send it wchisp commands to find
  out, it does not answer them. If the erase gets no reply (`Fail to erase`,
  `LIBUSB_ERROR_TIMEOUT`), replug the probe and run it again: two attempts
  failed that way after a `wchisp info` to the IAP device, the first after a
  replug succeeded (2026-11-11). The stock tool's bare `-q` (quit IAP) did not
  reboot an idle stuck probe (2026-07-08).
- **Right after a flash** the probe's usbfs node can still refuse an open
  (`LIBUSB_ERROR_ACCESS`; a read a minute later worked, consistent with udev
  setting its permissions after sysfs showed the device; ci.lan, 2026-11-11).
  `probe.py` waits for an openable node after a flash (`--timeout`) and before
  `config`'s wchisp (only while `--wait` lasts; its default 0 checks once), and
  `fw-recover` refuses a node it cannot open. wlink-iap's own open of the IAP
  device after `81 0f` relies on a fixed 3.5 s settle with no permission retry.
- **WCH-Link (CH549)**, the farm's `BC…` serials, takes another image and
  update path; `fw-update` does not cover it.

Validated on hardware 2026-11-11: `fw-update` and `fw-recover` on WCH-LinkE,
to v43.

## wlink-rs (`wlink`)

Flash, erase, reset, halt/resume, register and memory access over SDI, read
protection, probe mode and power. No option bytes beyond read protection, no
probe firmware.

- `-d <index>` on every command, from `wlink list`; upstream nightly adds
  `-d VID:PID:SERIAL`, not in 0.1.3.
- `status` attaches and halts the target; detach does not always resume it.
- `erase --method power-off` cuts the probe's target power and catches the
  boot window before firmware takes the debug pins; `--method pin-rst` holds
  RST, if wired. The recovery for a chip whose firmware repurposes SWD
  (`chips/wch.md`, CH32H417).
- CH58x: pass `--chip CH582` (wlink's own docs: auto-detection does not work
  there).
- `mode-switch --rv|--dap`, `set-power enable3v3|disable3v3|enable5v|disable5v`:
  unverified here.

## `0x55` — attach refused

`WCH-Link underlying protocol error: 0x55 [0x81,0x55,0x01,0x01]` from wlink-rs
came back every time the probe could not attach; the WCH-LinkUserManual V2.7
(book 2156) does not define it. The causes seen here, most frequent first:

1. Debug disabled in the chip's config (CH58x needs DEBUG_EN *and* ROM_READ;
   CH569 DEBUG_EN): `chips/wch.md`.
2. The firmware took the debug pins (CH32H417's SWD is its USB-HS pair):
   power-off erase.
3. The target is unpowered (CH569 needs its own supply).
4. Read protection.

## After any SDI contact

What an attach leaves behind (an active debug module that halts on `ebreak`,
resets, clocks, pins) and how to quiesce it is in the `target-debug` skill's
`families.md`, WCH QingKe: read it before trusting a device a probe touched.
