# SWO fixture

Firmware on TinyUSB's board support (stm32f407disco, stm32h743nucleo) with a known trace workload, for checking SWO
capture end to end on real hardware: bringing up a new board's SWO route, trace clock and rate, and re-checking
`swo_capture.py` and its decoders after a change. Once the board's row in `skills/target-debug/swo-boards.md` cites
the fixture, debug by capturing the real firmware.

## Workload

| Source                         | Rate                                  | Shows in a capture                                    |
|--------------------------------|---------------------------------------|-------------------------------------------------------|
| SysTick (from the BSP)         | 1 kHz                                 | exception trace                                       |
| TIM6, writes `swo_fixture_var` | 1 kHz, 700 us into each SysTick tick  | exception trace, data trace of a watch on it          |
| TIM7, reads `swo_fixture_var`  | 100 Hz, 250 us into the tick          | an episode of about 300 us                            |
| nested interrupt               | one per TIM7 tick, pended mid-handler | entered inside TIM7's episode                         |
| ITM port 1                     | once per phase                        | `swo-fixture <phase>\n`                               |
| ITM port 2                     | once per phase                        | the phase (32-bit), `0xA55A` (16-bit), `0x7E` (8-bit) |
| `swo_fixture_busy` / `_idle`   | 100 ms each, alternating              | PC samples split between the two                      |

Bursts are spaced to fit a 1.5 MHz SWO (layout in `src/swo_fixture.c`); compare a capture with `swo_fixture_last`,
the counts of the last 200 ms phase.

Build recipe, board notes, and when to re-check the timing on hardware: `skills/target-debug/swo-boards.md` (Fixture).
