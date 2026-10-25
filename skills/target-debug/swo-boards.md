# SWO — per-board reference

Validated boards for `swo_capture.py` (`swo-dwt.md`). Read the row and its notes before capturing on a board; add both
when a board is validated. A family's register requirements are its `--setup` entry in `scripts/swo_boards.json`,
whose `ref` fields cite the documents; this file holds what was measured. Board names are TinyUSB BSP aliases. The
firmware behind the stm32 rows is the fixture below, whose timers, nested interrupt, ITM marker and watched variable
have known rates; ra6m5_ek ran unmodified `cdc_msc`, so its row has no fixture golden checks and no injected-failure
cleanup.

| Board           | Probe, route                        | Setup   | Trace clock         | Exact SWO rates used | Verified                                                                |
|-----------------|-------------------------------------|---------|---------------------|----------------------|-------------------------------------------------------------------------|
| stm32f407disco  | on-board J-Link (ST-Link reflashed) | stm32f4 | HCLK 168 MHz        | 1.5 MHz (4.5 MHz/3)  | exc, LTS, ITM, data trace, PC sample, observe, comparator, PB3, restore |
| stm32h743nucleo | STLINK-V3, OpenOCD `stlink-dap.cfg` | stm32h7 | 400 MHz (see notes) | 16 MHz               | exc, LTS, ITM, data trace, PC sample, PB3, restore                      |
| ra6m5_ek        | on-board J-Link OB (S124)           | ra6m5   | TRCLK 100 MHz       | 500 kHz (3 MHz/6)    | exc, LTS, trace clock gate, restore                                     |

## Fixture

`tests/fixtures/swo` in this repository: one C file on TinyUSB's board support (clocks, LED), for stm32f407disco and
stm32h743nucleo. Its comments give the workload and its timing layout; `swo_fixture_last` holds the counts of
the last complete phase, the values a capture is checked against. Build it against a local TinyUSB checkout, named by
`TINYUSB_PATH` as below or in the environment. Its rates rest on the BSP's clocks, measured with TinyUSB 13f9a36a:
re-check them on hardware after a change to a board's clock setup.

```bash
python3 <tinyusb>/tools/get_deps.py -b <board>
cmake -S tests/fixtures/swo -B tests/fixtures/swo/_build/<board> -DBOARD=<board> -DTINYUSB_PATH=<tinyusb>
cmake --build tests/fixtures/swo/_build/<board>
```

Another board needs a port of its timers and spare interrupt, which are vendor peripherals, and its own timing check
on hardware before its row can cite the fixture.

## stm32f407disco

- The J-Link's SWO rates are 4.5 MHz / n; the fastest that divides 168 MHz is 1.5 MHz (n = 3). All sources together
  (exceptions with timestamps, two ITM ports, one data watch) fit 1.5 MHz only when the firmware's bursts are spaced;
  the fixture's layout does that and shows one overflow in 4 s.
- J-Link's `SWO EnableTarget` sets DBGMCU_CR.TRACE_IOEN itself; the setup entry writes it too, so the requirement
  holds whichever tool connects.
- PB3 is TRACESWO from reset (MODER AF, AF0); the entry refuses when the firmware remapped it (checked with PB3 set
  to output, and to AF3).
- J-Link Commander halts the core when it connects: the fixture's timers then lose their phase to SysTick and bursts
  collide (22 overflows in 4 s against 0). Reset and run the board after any such session before a timing capture.
- `-device stm32f407vg` from the roster; GDB server memory access while running works over this probe.
- DWT_MASKn reads back at most 15: `mask=31` fails the capture at its read-back (and is restored).

## stm32h743nucleo

- The SWO unit is not the core TPIU: OpenOCD's `stm32h7x.swo` object at 0xE00E3000 on AP2 (the system-bus alias at
  0x5C003000 ignores debugger writes); `stm32h7x.tpiu` is the parallel port and rejects `uart`. The J-Link route, which
  programs the core TPIU, is refused for this entry.
- `stm32h7x.cfg`'s examine sets DBGMCU_CR (D1/D3 debug clocks, TRACECLKEN) and SWTF_CTRL (ENS0, ENS1); the entry
  checks them and does not write them.
- The SWO unit's input clock is not named in RM0433. 400 MHz worked, with rcc_c_ck and pll1_r_ck both 400 MHz in
  this BSP's clock tree; a slower guess gives ratio-garbled bytes, a faster one silence. Re-measure when a project
  changes either clock.
- STLINK-V3 (J12M3) through OpenOCD drops or corrupts a byte now and then at a 2 KiB-multiple offset (3 in 178 KB,
  1 in 263 KB); the decode flags each.
- OpenOCD's `-pin-freq 0` picks 24 MHz, not a divisor of 400 MHz: always name the rate.

## ra6m5_ek

- Its TPIU must not be touched while TRCKCR.TRCKEN is 0, the reset state. One TPIU read (TPIU_CSPSR) through J-Link
  Commander then left the AHB-AP with a transfer in progress: CSW.TrInProg set, TAR at that address, CPUID and the
  ROM table unreadable, the J-Link and OpenOCD connects failing, under reset too. DP ABORT, a debug power-domain
  cycle, SRST and the OCDREG DBIRQ wake (AP1) did not clear it; MCUSTAT showed the CPU running. Power-off did: hub
  21-1 ports 1 and 2 together on ci.lan (port 2, the J-Link OB, alone left the target powered). The stopped trace
  clock is the suspected cause: the manual warns that TPIU access can stop bus access (p86 note 1, stated for access
  without an OCD connection) and that TRCKEN stops the trace clock (TRCKCR, p191); neither states this case.
- The `ra6m5` entry starts the trace clock (PRCR then TRCKCR, sized, PRCR with its key) before the first TPIU access
  and stops it last; on a J-Link whose `-device` starts with `R7FA6M5`, `swo_capture.py` refuses a TPIU access while
  the clock is stopped, `--caps` skips the TPIU, and a capture needs `--setup ra6m5`. Never read 0xE0040000 to
  0xE0040FFF on this part by hand.
- Trace clock: TRCLK is the system clock source divided by TRCKCR.TRCK (Table 8.2, p152; /2 after reset, kept). With
  TinyUSB's BSP the source is the PLL at 24 MHz / 3 x 25 = 200 MHz (SCKSCR 5, PLLCCR 0x3102 read from the target), so
  TRCLK is 100 MHz, the rate the capture decoded traffic at. A TinyUSB `TRACE_ETM` build sets TRCK /4 itself (50 MHz;
  `etm-boards.md`), and firmware that writes TRCKCR without a debugger attached wedges a standalone boot until
  power-cycle (same notes): a different path to the same register.
- On-board S124 J-Link OB: P109/TDO reaches it through J29 3-4 (schematic, book 1965, p7); `SWO GetSpeedInfo` gives
  3 MHz base, MinDiv 6, so 500 kHz at most. Exception trace with local timestamps on unmodified `cdc_msc`: 2 overflows
  and one packet decoded as a reserved exception function in 2 s, its cause unknown; PC sampling at its slowest
  (`1:15`, about 12 k samples/s at 200 MHz) would not fit.
- J-Link Commander halts the core: run it with `r` and `g` before a capture (DHCSR 0x00130003 gave a 1-byte stream).
- The board's own USB never enumerated on ci.lan during this work, whatever image ran.
