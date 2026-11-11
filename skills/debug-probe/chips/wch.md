# WCH chips

Each section separates what the documents say, what was seen on a board, and
what nobody checked. `probe.py config` writes only the chips with a "Supported
by `config`" line; another chip's config is read-only here (`wchisp -u info`,
if wchisp knows the chip). Writing one first needs its field layout checked in
its documents, then an entry in `probe.py`'s `CHIPS` and in the wchisp patch's
`set-word` allowlist, then a validated run recorded below.

## CH582 / CH583 (CH58x)

Supported by `config`: CH582 (chip ID 0x82), word `USER_CFG`, fields
`CFG_RESET_EN`, `CFG_DEBUG_EN`, `CFG_ROM_READ` (to 1 only).

**Documented** (CH583DS1, book 6485, PDF V1.9 though catalogued Rev 2.2):

- USER_CFG bits (p11, Table 2-3): [2:0] reserved 101b, 3 `CFG_RESET_EN` (RST#
  enable, default 0), 4 `CFG_DEBUG_EN` (2-wire debug, default 1), 5 reserved 0,
  6 `CFG_BOOT_EN` (default 1), 7 `CFG_ROM_READ` (0 = read-protected, default 1),
  [27:8] reserved FFF0Fh, [31:28] `VALID_SIG` 0100b. Default word `0x4FFF0FD5`.
- RST# is PB23 (p6). `R8_GLOB_CFG_INFO` (0x40001045, p30) reports the live
  state: bit 0 ROM_READ, bit 2 RESET_EN, bit 3 BOOT_EN.

**Bootloader**: hold Download (S3 on the CH583EVT / ch582m boards) while
powering the board, USB to the PC; it enumerates as `4348:55e0` and leaves after
a short idle time, so start `probe.py config ... --wait 60` first. The download
pin is PB22 by default (wchisp's YAML, `DOWNLOAD_CFG`).

**Observed**:

- 2026-11-11, htpc, bench CH582M UID D4-43-62-7B-54-50-8A-0F: USER_CFG
  `0x450F3FDD` (RESET_EN, DEBUG_EN, ROM_READ all 1; bits [27:8] `0x50F3F`, as
  WCH's tools write them, not the datasheet's FFF0Fh). `config --set
  CFG_RESET_EN=0` then `=1` wrote and read back `0x450F3FD5` and `0x450F3FDD`,
  other config bytes unchanged.
- 2-wire debug needs DEBUG_EN *and* ROM_READ: with ROM_READ=0 the attach still
  fails `0x55` (2026-07, an earlier CH582 board; written as user byte `0xDD`
  with isp55e0).
- wchisp's CH58x YAML: `reset: 0xd50fff4f` is the datasheet default
  byte-swapped (its VALID_SIG nibble is D), so `wchisp config reset` must not
  run on CH58x; `enable_debug: 0x450f3fd7` turns RESET_EN off.
- Reads of `R8_GLOB_CFG_INFO` through wlink and OpenOCD returned garbage
  (CHIP_ID 0x0f, not 0x82) on 2026-11-11, without `--chip CH582`.

**Unverified**: RST# resetting the chip after the write, and the value after a
power cycle; CH581/CH583/CH584/CH585 (`config` refuses them).

## CH32X035

Supported by `config`: CH32X035C8T6 (chip ID 81), word `RDPR_USER`, field
`RST_MODE` (0 or 3); `probe.py` keeps RDPR/nRDPR at A5/5A and rewrites nUSER.

**Documented**:

- Option word at 0x1FFFF800 is RDPR, nRDPR, USER, nUSER (CH32X035RM V1.9, book
  6495, §20.5, p248). USER[4:3] `RST_MODE`: 00 = RST pin enabled, 11 = off, the
  pin is GPIO (reset value 11b). RDPR 0xA5 = not read-protected.
- On the 48-pin C8T6 the reset pin is PA21 (CH32X035 datasheet, book 6608,
  p17). PC17 high at power-up enters the BOOT area, the USB ISP bootloader (p21,
  note 8).
- Writing option bytes over SDI instead takes the fast-page sequence, an erase
  of the whole 256-byte area first (RM §20.5.2, p249): an interrupted erase
  leaves RDPR erased, i.e. protected. That path is not implemented.

**Observed** (2026-11-11, ci.lan, probe A8D88F062D95, unrostered): ChipID
`0x03510611`, read-protected (`Flash protected: true`, then `DMI Status Failed`;
the ESIG UID read all FF). `wlink unprotect` (0.1.2) exited 0, and the chip still
reported protected before a power cycle, as RM §20.5.4 (p250: erase, write RDPR
0xA5, which mass-erases, then a power-on reset) says.

**Unverified**: all of it on hardware: the ISP write, whether the bootloader
keeps nUSER as written, PA21 resetting the chip. wchisp notes that CH32X03x
refuses masked config reads; `set-word` reads the whole block for that reason.

## CH569 (HydraUSB3)

Supported by `config`: CH569 (chip ID 0x69), word `NV_INFO`, fields
`RESET_EN`, `DEBUG_EN`, `CODE_READ_EN` (to 1 only); `config` refuses DEBUG_EN
together with RESET_EN.

**Documented** (CH569DS1 Rev 1.2, book 6613, §2.2.1 Table 2-2, p18; a table,
read from the page): [31:30] `USER_MEM` (RAMX/ROM split), 29 `LOCKUP_RST_EN`, 7
`CODE_READ_EN`, 6 `BOOT_EN`, 5 `DEBUG_EN`, 4 `RESET_EN` (external reset on
PB15), [3:0] 0101b. "When debug interface is enabled, external reset must be
disabled. Otherwise, the debug function cannot be used normally."

**Bootloader**: Flash Mode jumper (J3), USB A-to-A cable to the USB2 port,
`3V3 DISABLE` button; it leaves after about 10 s idle, so start `probe.py config
... --wait 180` first.

**Observed**:

- 2026-07: attach failed `0x55` until DEBUG_EN was set; `wchisp config reset`
  restored `0x8FFFF2E5` (DEBUG_EN=1, CODE_READ_EN=1, BOOT_EN kept).
- 2026-11-11, htpc, HydraUSB3 UID BA-FE-BA-E4-C2-84-36-68: NV_INFO read
  `0x8FFFF245` (DEBUG_EN and CODE_READ_EN 0); `config --set
  DEBUG_EN=1,CODE_READ_EN=1` wrote and read back `0x8FFFF2E5`, the other config
  words unchanged. A wlink-rs 0.1.3 attach failed `0x55` while the Flash Mode
  jumper (J3) stayed fitted; with it removed, `wlink status --chip CH56X`
  attached (ChipID `0x69000000`) and `wlink reset` printed `Reset Quit`.

The chip must be powered for SDI. HydraUSB3 notes warn that 2-wire debug can
brick a CH569. What an active debug module did to it afterwards is in
`target-debug`'s `families.md`.

**Unverified**: when a written NV_INFO takes effect (power-on or any reset), and
its value after a confirmed power cycle; whether the J3 jumper alone caused the
`0x55`; the firmware running after `wlink reset`; `wchisp config enable-debug`
here (it applies every reset value first; the CH56x YAML has no `enable_debug`
word).

## CH32V4x7 (CH32V407 / CH32V467)

**Observed** (2026-11-10/11, ch32v407_evt_r0): wlink-rs needs 0.1.3 (0.1.2:
"Unknown RISC-V Chip: 0xa6"); wchisp needs the patched build (V4x7 device
file). Option word read `0x40BF5AA5`. An attach's effect on its clocks is in
`target-debug`'s `families.md`.

## CH32H417 (MuseLab nanoCH32H417)

**Observed** (2026-07-13..16): USB-HS firmware takes the SWD pins (why, and
the `DBGMCU_Config()` hang: `target-debug`'s `families.md`). Getting them back:
unplug the USB 3.0 Type-A cable, then `wlink -d N erase --method power-off
--chip CH32H41X` (works while SB7 powers the board from the probe). wchisp has
no CH32H support.

## CH32V10x / CH32V20x / CH32V30x

Attach behaviour (reset, RAM kept, clocks left changed) is in the `target-debug`
skill's `families.md`, WCH QingKe. Nothing config-related recorded yet.
