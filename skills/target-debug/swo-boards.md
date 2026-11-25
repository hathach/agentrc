# SWO — per-board reference

Validated boards for `swo_capture.py` (`swo-dwt.md`). Read the row and its notes before capturing on a board; add both
when a board is validated. A family's register requirements are its `--setup` entry in `scripts/swo_boards.json`,
whose `ref` fields cite the documents; this file holds what was measured. Board names are TinyUSB BSP aliases. A row
measured on the fixture below has its timers, nested interrupt, ITM marker and watched variable at known rates; a row
that names other firmware (stm32f723disco: `cdc_msc`) has no such reference counts.

| Board           | Probe, route                        | Setup   | Trace clock         | Exact SWO rates used | Verified                                                                |
|-----------------|-------------------------------------|---------|---------------------|----------------------|-------------------------------------------------------------------------|
| stm32f407disco  | on-board J-Link (ST-Link reflashed) | stm32f4 | HCLK 168 MHz        | 1.5 MHz (4.5 MHz/3)  | exc, LTS, ITM, data trace, PC sample, observe, comparator, PB3, restore |
| stm32f723disco  | on-board J-Link (roster `jlink`)    | stm32f7 | HCLK 216 MHz        | 1.5 MHz (4.5 MHz/3)  | caps, exc, LTS, restore (`cdc_msc`, no fixture port)                    |
| stm32h743nucleo | STLINK-V3, OpenOCD `stlink-dap.cfg` | stm32h7 | 400 MHz (see notes) | 8 MHz, one source    | exc, LTS, ITM, data trace, PC sample, PB3, restore                      |
| ra6m5_ek        | on-board J-Link OB (S124)           | ra6m5   | TRCLK 100 MHz       | 500 kHz (3 MHz/6)    | exc, LTS, ITM, data trace, CYCMATCH, comparator, gate, restore          |

## Fixture

`tests/fixtures/swo` in this repository: one C file on TinyUSB's board support (clocks, LED), for stm32f407disco,
stm32h743nucleo and ra6m5_ek. Its README gives the workload, the source comments its timing layout; `swo_fixture_last`
holds the counts of the last complete phase, the values a capture is checked against. Build it against a local TinyUSB
checkout, named by `TINYUSB_PATH` as below or in the environment. Its rates rest on the BSP's clocks, measured with
TinyUSB 13f9a36a: re-check them on hardware after a change to a board's clock setup. A `-DLOGGER=rtt` build also prints
each phase's marker on RTT, for `--rtt`.

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
- A J-Link connect rewrites DBGMCU_CR to 0x7 and DBGMCU_APB1_FZ to 0x1800 (read back through OpenOCD, whose handler
  only sets bits): firmware's TRACE_IOEN and timer freezes are gone; `SWO EnableTarget` sets TRACE_IOEN again.
- J-Link halts and restarts the core at every session close, and a halt stops SysTick: the fixture freezes TIM6 and
  TIM7 in debug and sets the freeze again each phase, so consecutive captures keep the layout (8 in a row with 0 to 2
  overflows in 4 s, all sources, with and without `--rtt`; without the freeze 1 to 4040). J-Link Commander halts as
  it connects, after rewriting the freeze: the timers then lose their phase to SysTick and bursts collide. Reset and
  run the board after any such session before a timing capture.
- Two comparators matching one access send one PC or address-offset packet between them: beside an `offset` watch, a
  `pcval` watch on the same variable got its values but no PC, and the other way round the offset watch lost the
  accesses the `pcval` one traced. Neither the ARMv7-M ARM nor the Cortex-M4 TRM in the library describes it.
- `-device stm32f407vg` from the roster; GDB server memory access while running works over this probe.
- DWT_MASKn reads back at most 15: `mask=31` fails the capture at its read-back (and is restored).

## stm32f723disco

- TinyUSB's `cdc_msc` (HSE 25 MHz / 25 x 432 / 2 = 216 MHz HCLK) at 1.5 MHz: 4081 SysTick entries in a 4.08 s span
  of 216 MHz local timestamps, no overflow, one segment; restore complete.
- PB3 reaches the on-board probe's T_SWO through SB13 (MB1260 schematic, book 2058, sheet 3); UM2140 has no
  solder-bridge table, so SB13's default is read from the drawing. A J-Link connect left DBGMCU_CR at 0x7, trace clock
  off, until the setup write.

## stm32h743nucleo

- The SWO unit is not the core TPIU: OpenOCD's `stm32h7x.swo` object at 0xE00E3000 on AP2 (the system-bus alias at
  0x5C003000 ignores debugger writes); `stm32h7x.tpiu` is the parallel port and rejects `uart`. The J-Link route, which
  programs the core TPIU, is refused for this entry.
- `stm32h7x.cfg`'s examine sets DBGMCU_CR (D1/D3 debug clocks, TRACECLKEN) and SWTF_CTRL (ENS0, ENS1); the entry
  checks them and does not write them. A system reset keeps SWTF_CTRL, and with the debug clocks off it reads 0 and
  ignores writes. TinyUSB's `TRACE_SWO=1` build sets DBGMCU_CR to 0x00700000, ENS0 and PB3 AF0 itself (read back
  with the examine handler disabled).
- The SWO unit's input clock is not named in RM0433. 400 MHz worked, with rcc_c_ck and pll1_r_ck both 400 MHz in
  this BSP's clock tree; a slower guess gives ratio-garbled bytes, a faster one silence. Re-measure when a project
  changes either clock.
- The probe stays on the latest STLINK-V3 firmware, V3J17M11 (J13 stalls after 2 KiB; no downgrade). Through OpenOCD
  0.12.0+dev-02620-g0ce743125 it loses bytes two ways (fixture, 10 s captures); the decode flags each loss as an
  unknown header or unexpected source and resynchronizes. Use 8 MHz with one source: 5 exception-trace (200 KB each)
  and 4 PC-sampling (1.08 MB each) captures there were one clean segment each. The row's ITM, data trace, PB3 and
  restore checks date from 16 MHz on J12M3.
  - Breaking 0 to 5 bytes past a 2 KiB-multiple stream offset, at 16 MHz only: 15 to 20 per capture on J17 with either
    source alone, against 0 to 6 on J12M3. OpenOCD reads what the probe holds, up to 4096 bytes, every 1 ms
    (`stlink_usb_trace_read`, unchanged upstream), with no 2 KiB unit on its side.
  - With exception trace and PC sampling together, inside a packet next to a sleep PC sample and an exception packet,
    anywhere in the stream, at 4, 8 and 16 MHz on J12M3 and J17 alike (J17 at 8 MHz: 20 per 1.28 MB). The same
    sources on stm32f407disco through J-Link lose nothing (1.14 MB); target and probe are not told apart here (#306).
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
  3 MHz base, MinDiv 6, so 500 kHz at most. The fixture's layout is spaced for 1.5 MHz: exception trace with local
  timestamps overflows (423 in 4 s) and ITM alone is clean, so capture its sources apart. Exception trace without
  timestamps gives the fixture's counts with no overflow, but about one packet a second decodes as a reserved exception
  function or an unknown header, at 250 kHz as at 500 kHz: stream corruption on this route, its cause not located.
  PC sampling at its slowest (`1:15`, about 12 k samples/s at 200 MHz) would not fit.
- The Cortex-M33's four comparators read DWT_FUNCTIONn.ID 0b01011, 0b11010, 0b01010, 0b11110 (`--caps`): comparator 0
  alone matches the cycle counter, 1 and 3 link, 3 alone matches a data value. With the fixture, `w,val`, `r,pc` (all
  Long PC packets, no Short or Medium seen), `rw,pcval`, `--cycmatch` (one PC per CYCCNT wrap, 21.5 s), a comparator
  in use skipped or taken by `--take-comparator`, kill -9 then `--restore`, and SIGINT all left the comparators as
  found.
- J-Link Commander halts the core: run it with `r` and `g` before a capture (DHCSR 0x00130003 gave a 1-byte stream).
