# Vector catch and fault autopsy — catch the crash, not the wedge (Cortex-M)

Part of the `target-debug` skill. Cortex-M registers throughout; the RISC-V analog is the last line, and on Espressif
chips the panic handler is the breakpoint target (`espressif.md`).

A wedge that is really a fault (HardFault loop, lockup) autopsies best AT the faulting instruction. Two hardware-proven
gotchas: **FPB/DWT comparators survive reflash and dead sessions** — a stale one fires as a phantom SIGTRAP at an
unrelated line of NEW firmware — and J-Link's reset strategy manages vector-catch bits: scrub first, arm AFTER reset:

```gdb
# scrub: FP_COMP0..5 = 0xE0002008..201C, DWT_FUNCTIONn = 0xE0001028 + n*0x10
set *(unsigned*)0xE0002008 = 0
# ... (repeat per comparator; count = the budget reads in `gdb.md`)
# arm (after monitor reset; tool-agnostic — works via JLinkExe w4 too):
set *(unsigned*)0xE000EDFC |= (1<<10)|(1<<9)|(1<<8)|(1<<7)|(1<<6)|(1<<5)|(1<<4)
# = VC_HARDERR|INTERR|BUSERR|STATERR|CHKERR|NOCPERR|MMERR; bit0 VC_CORERESET halts at reset
```

OpenOCD native: `cortex_m vector_catch hard_err bus_err state_err chk_err mm_err`. It halts at exception ENTRY (pc =
handler, LR = EXC_RETURN 0xFFFFFFFx); decode:

```gdb
p/x *(unsigned*)0xE000ED28   # CFSR — low byte MemManage, byte1 BusFault, top half UsageFault
p/x *(unsigned*)0xE000ED2C   # HFSR — bit30 FORCED = an escalated lower-priority fault
p/x *(unsigned*)0xE000ED38   # BFAR — faulting address (valid if CFSR bit15 BFARVALID)
x/8wx $msp                   # stacked frame: r0 r1 r2 r3 r12 lr pc xpsr — pc = culprit
# frame is on PSP when EXC_RETURN bit2 is set (LR = 0xFFFFFFFD — FreeRTOS
# tasks run on PSP): then x/8wx $psp instead. LR 0xFFFFFFF1/E9 = MSP.
```

`addr2line -e <elf> <stacked pc>` names the line (verified: CFSR 0x8200, BFAR = the bad address, stacked pc = the
faulting ldr). Loads fault precisely; stores usually IMPRECISERR (BFAR invalid, pc late). ARMv6-M has no CFSR/BFAR, only
VC_HARDERR|VC_CORERESET (ARMv6-M ARM book 1243 Fig C1-7 p339) — stacked frame alone. Still a halt (host URB timeouts
apply); clear DEMCR (`&= ~0x7F0`) before handing back; RISC-V: breakpoint the trap handler; mcause/mepc/mtval are the
CFSR/BFAR analogs.
