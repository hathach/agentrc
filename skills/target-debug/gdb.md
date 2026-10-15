# GDB — state autopsy, breakpoints and watchpoints

Part of the `target-debug` skill.

Start one server using the board's device/config and probe serial. These
examples cover ARM targets: J-Link, and upstream OpenOCD for STM32H7 with
ST-Link and RP2040 with CMSIS-DAP; select the installed OpenOCD `interface/`
and `target/` configs for other boards. Espressif chips use openocd-esp32
and the chip's own USB-Serial-JTAG or pad JTAG: `espressif.md` "Attach".

```bash
JLinkGDBServer -device <JLINK_DEVICE> -select usb=<uid> -if SWD -speed 4000 -port 2331 -nogui -singlerun
openocd -f interface/stlink.cfg -f target/stm32h7x.cfg -c 'adapter serial <uid>'
openocd -f interface/cmsis-dap.cfg -f target/rp2040.cfg -c 'adapter serial <uid>' -c 'adapter speed 5000'
```

## OpenOCD batch sessions

OpenOCD 0.12 does not emit `mdw` output from a `-c` script, nor `reg` or
`verify_image` output from inside a compound `-c` string (seen 2026-09-18).
Print it explicitly with `echo [capture "<command>"]`, or read memory with
`read_memory`; for example, the Cortex-M validity anchor is:

```bash
openocd ... -c 'init; echo [format 0x%08x [read_memory 0xE000EDF0 32 1]]; shutdown'
```

A failed command stops the whole batch, including every later `-c`
argument: a following `reset run`, `resume` or `shutdown` will not execute,
and the target can be left halted. Put a flash or `verify_image` that may fail
in its own session, or wrap it in `catch` and run the restoring commands
unconditionally before `shutdown error`.

On RP2040, reach a flash-resident halt before any flash read or
`verify_image`: `families.md`, "RP2040 flash verification", has the
precondition and the failure it prevents.

RP2040 needs the roster's explicit `adapter speed` (5000 on the measured
CMSIS-DAP rigs); the 100 kHz default failed to connect the multidrop DAP.
Before `program`, run `init; reset halt`: without that state preparation the
flash algorithm failed to allocate its bounce buffer and left the core halted.

Connect with the matching toolchain's GDB (`arm-none-eabi-gdb <flashed.elf>`
for ARM), then `target remote :2331` for J-Link or `target remote :3333`
for OpenOCD. For an autopsy, use `monitor halt`; for a fresh start only,
use `monitor reset halt`, `load`, then `continue`.

Script JLinkGDBServer sessions with `-singlerun` — the server exits with the connection, and
back-to-back relaunches race the probe handle and hang. Symbols need the
firmware's DWARF: check the project's release configuration keeps it.

**Autopsy of a wedged board: attach and halt ONLY** — skip
`monitor reset halt` + `load` (those are for fresh starts; a reset destroys
the evidence). On Espressif RISC-V builds with the end-of-stack watchpoint
the attach itself can panic and reset the chip (`espressif.md`, Attach):
take the console baseline first, and a snapshot taken after a new banner is
post-reset. Symbolize with the ELF that is actually flashed — the one from the build
that wedged; do not rebuild while the wedge is still on the board. The
names to put in these slots are the project's (`projects/<name>.md` when the
skill keeps notes for it, else read the stack and the port driver first):

```gdb
p/x <device stack's endpoint state table>   # busy/stalled/claimed per endpoint
p <host stack's per-device state>           # addr, enum/config
p/x <port's private state>                  # per-port names — read the port driver first
x/8wx <USB peripheral base>                 # raw regs: only ranges the manual documents as side-effect-free
                                            #   (DWC2: a read of GRXSTSP 0x20 pops the RxFIFO; families.md)
watch  <port's transfer bookkeeping field>  # HW watchpoint (Cortex-M: ~4; Espressif: espressif.md)
break <the USB ISR>                         # works, but see warning below
```

**Hardware budget — read it off the chip** (Cortex-M FPB/DWT, verified:
F407/M4 = 6 bp + 4 wp, rp2040/M0+ = 4 + 2; M7 typically 8/4; Espressif
counts come from the tool per chip, `espressif.md`):

```gdb
p ((*(unsigned*)0xE0002000)>>4) & 0xF   # FPB NUM_CODE = hw breakpoints (M7 adds bits[14:12])
p (*(unsigned*)0xE0001000)>>28          # DWT_CTRL NUMCOMP = watchpoint comparators
```

- `hbreak`/`thbreak` force a hardware breakpoint (software breaks in flash
  need flash-breakpoint support — J-Link has it; OpenOCD: `bp <addr> 2 hw`;
  openocd-esp32 writes a breakpoint beyond the budget into flash unless
  flash support is off, `espressif.md`); `tbreak` = one-shot. gdb inserts
  breakpoints at the next `continue`, so a budget probe sees the refusal
  there unless `set breakpoint always-inserted on` is set first, and
  `info breakpoints` keeps listing a refused one as enabled, so the
  refusal shows only in the insert error and the server log. A `-batch`
  script stops at that error and exits, skipping its trailing cleanup, and
  openocd-esp32 then leaves the target halted (2026-10-16, P4, ~36 s under
  traffic): run budget probes apart from traffic, put the breakpoint
  cleanup and resume in an error handler or an outer controller that
  survives the gdb failure, and confirm the cores are running afterwards.
- `watch -l <expr>` watches the address expr evaluates to once — almost
  always what you want; `rwatch`/`awatch` trap reads/any access (hardware-
  only — they error, never fall back). OpenOCD adds a data-VALUE match GDB
  can't express: `wp <addr> 4 w <value> [mask]` — catch who writes 0 into a
  busy flag, ignoring writes of 1.
- **Demand the word "Hardware" in the confirmation.** `watch` silently falls
  back to a software watchpoint when no hardware comparator or trigger fits — GDB then
  single-steps the whole program, hundreds of times slower: certain USB
  death. Plain `Watchpoint 2:` = delete it and narrow the expression
  (`watch -l`, cast to a 4-byte int).
- Conditional breaks (`break ... if ep_addr==0x81`) and `dprintf
  <loc>,"fmt",args` (printf without recompiling; keep `dprintf-style gdb`)
  are host-evaluated — no Cortex-M agent expressions in these stubs: every hit
  halts+resumes (~ms) even when the condition is false. Post-wedge/cold
  paths only; ISR-rate events belong in the RAM ring.
- `commands <bpnum> ... end` (start `silent`, end `continue`) auto-collects
  evidence per hit — same halt-per-hit cost.
- Stepping with the USB ISR firing between steps is chaos: OpenOCD
  `cortex_m maskisr steponly` (Cortex-M; openocd-esp32 sets `riscv
  set_maskisr steponly` itself at gdb attach). The bus runs either way — the
  host may still reset a halted-looking device.
- Poking state while halted (`set var <endpoint state>.busy = 0`)
  tests a hypothesis but invalidates the post-mortem — dump first, poke after.
- FreeRTOS examples: J-Link `-rtos GDBServer/RTOSPlugin_FreeRTOS`, or
  upstream OpenOCD `-rtos FreeRTOS` on the target in its config → `info
  threads` lists every task with state/prio/frame (verified: 6 tasks, on
  those routes). It populates only after a run→stop cycle — plain attach
  shows one 0xDEAD placeholder. openocd-esp32 enables FreeRTOS by default
  (`set ESP_RTOS` before the board cfg overrides it); what it showed per
  target is in `espressif.md`. Semihosting as a log channel is never the
  answer (traps + halts per call — RTT instead; IDF's own semihosting calls
  under a debugger: `espressif.md`). **Monitor-mode debugging** (J-Link,
  M3+) keeps chosen IRQs serviced at a breakpoint — needs SEGGER's
  JLINK_MONITOR files + `SetMonModeDebug=1`; not set up here:
  <https://kb.segger.com/Monitor_Mode_Debugging> (untested).

While halted the device answers **nothing**: host control transfers time out
in ~5 s and the OS may reset/re-enumerate — after `continue`, the bus traffic
shows recovery, not the original bug. Prefer one halt for a post-mortem dump
over stepping through live USB traffic. When the probe shares that host, the
stalled enumeration can block it too: halting pico2_etm_trace stalled the
host's enumeration for ~30 s (`device not accepting address`, error -62) and
JLinkExe blocked at "Connecting to J-Link" meanwhile. verified 2026-09-18.

## J-Link Commander sessions

- In a hand-written JLinkExe command file, `-ExitOnError 0` still exits at a
  failed `verifybin` and leaves the core halted: resume it from a new session.
  verified 2026-10-12 mimxrt1060_evk.
- `JLinkExe` can crash in `libjlinkarm` at the probe connect, before the
  target connect (SIGSEGV; once a glibc double-free abort); the cause is
  unknown. verified 2026-10-12 htpc, mimxrt1060_evk: 2 of 94 sessions of
  TinyUF2's HIL runner.
- Keep the probe off the hub of a device that hangs mid-write: the xHCI keeps
  retrying its enumeration, and a J-Link there stalled for ~80 s. verified
  2026-10-12 lpcxpresso55s69 (that the hub is the cause is a hypothesis).
