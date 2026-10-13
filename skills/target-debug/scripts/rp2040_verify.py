#!/usr/bin/env python3
"""Verify the image programmed on an RP2040 against an ELF, over OpenOCD, and
leave core 0 running (target-debug's families.md, "RP2040 flash verification").

  rp2040_verify.py --probe <serial> --interface-cfg interface/cmsis-dap.cfg --speed 5000 \
                   --elf <flashed.elf> --symbol tud_task_ext

Flash reads return zeroes while the core sits in RAM-resident code with flash
access off (get_bootsel_button), so the batch first stops core 0 at a hardware
breakpoint on --symbol, a flash-resident function the firmware runs repeatedly,
checks the PC is there, then verifies through the uncached XIP alias. Whatever
fails after the first halt, it removes its own breakpoint and resumes, then
reads DHCSR to show the core running. A reset OpenOCD detects during the session
fails the run: the board was not left as found.

RP2040_VERIFY_OPENOCD and RP2040_VERIFY_NM name the tools when they are not
`openocd` and `arm-none-eabi-nm` on PATH.

Exit: 0 verified; 1 the image did not verify or the breakpoint was not reached,
core running again; 2 bad usage or a symbol outside flash; 3 the probe or tool
failed, a reset was detected, or the core was not shown running afterwards.
"""
import argparse
import json
import os
import re
import subprocess
import sys

XIP_BASE, XIP_END = 0x10000000, 0x11000000
XIP_NOCACHE_OFFSET = 0x03000000  # 0x10000000 -> 0x13000000, the uncached alias
S_HALT = 1 << 17


class ToolError(RuntimeError):
    pass


def symbol_address(nm, elf, symbol):
    try:
        out = subprocess.run([nm, elf], capture_output=True, text=True, timeout=60)
    except (OSError, subprocess.TimeoutExpired) as e:
        raise ToolError(f'{nm}: {e}')
    if out.returncode != 0:
        raise ToolError(f'{nm} exited {out.returncode}: {out.stderr.strip()}')
    found = {int(f[0], 16) for f in (l.split() for l in out.stdout.splitlines())
             if len(f) == 3 and f[2] == symbol and f[1] in 'tT'}
    if len(found) != 1:
        raise ValueError(f'{symbol}: {len(found)} text symbols in {elf}, need exactly one')
    addr = found.pop() & ~1
    if not XIP_BASE <= addr < XIP_END:
        raise ValueError(f'{symbol} at 0x{addr:08x} is not flash-resident')
    return addr


# Arguments reach OpenOCD's Tcl unquoted; anything outside this set is refused
# rather than escaped.
SAFE_ARG = re.compile(r'[A-Za-z0-9_./+-]+')


def tcl_batch(addr, elf, wait_ms):
    """RESULT carries the outcome: `verified`, `not-at-breakpoint`, `mismatch`
    (verify_image reported differing bytes) or `tool-error`; CLEANUP and RESUME
    lines report a failed restoring step."""
    a = f'0x{addr:08x}'
    return f'''init
halt
set bp_set 0
set rc [catch {{
  bp {a} 2 hw
  set bp_set 1
  resume
  set timedout [catch {{wait_halt {wait_ms}}} werr]
  if {{$timedout}} {{
    # wait_halt's timeout error carries no message; a core still running is the timeout
    set state [[target current] curstate]
    if {{$state ne "running"}} {{error "tool-error wait_halt: state=$state $werr"}}
    halt
  }}
  rbp {a}
  set bp_set 0
  set pc [dict get [get_reg pc] pc]
  if {{$timedout || $pc != {a}}} {{error "not-at-breakpoint pc=$pc"}}
  if {{[catch {{capture [list verify_image {elf} 0x{XIP_NOCACHE_OFFSET:08x}]}} out]}} {{
    set out [string map {{"\\n" " | "}} [string trim $out]]
    if {{[string match "*diff *" $out]}} {{error "mismatch $out"}}
    error "tool-error verify_image: $out"
  }}
  echo "RESULT [string trim $out]"
}} msg]
if {{$rc}} {{
  if {{![string match "not-at-breakpoint *" $msg] && ![string match "mismatch *" $msg] && ![string match "tool-error *" $msg]}} {{
    set msg "tool-error $msg"
  }}
  echo "RESULT $msg"
  if {{$bp_set}} {{
    if {{[catch {{halt}} e]}} {{echo "CLEANUP failed halt: $e"}}
    if {{[catch {{rbp {a}}} e]}} {{echo "CLEANUP failed rbp: $e"}}
  }}
}}
if {{[catch {{resume}} e]}} {{echo "RESUME failed $e"}}
echo [format "DHCSR 0x%08x" [read_memory 0xE000EDF0 32 1]]
shutdown'''


def parse(text):
    result = re.findall(r'^RESULT (.*)$', text, re.M)
    dhcsr = re.findall(r'^DHCSR (0x[0-9a-fA-F]{8})$', text, re.M)
    restore = re.findall(r'^(?:RESUME|CLEANUP) failed (.*)$', text, re.M)
    return (result[-1] if result else None,
            int(dhcsr[-1], 16) if dhcsr else None,
            restore)


def main(argv=None):
    ap = argparse.ArgumentParser(description=__doc__, formatter_class=argparse.RawDescriptionHelpFormatter)
    ap.add_argument('--probe', required=True, help='probe serial (adapter serial)')
    ap.add_argument('--interface-cfg', required=True, help='OpenOCD interface config, e.g. interface/cmsis-dap.cfg')
    ap.add_argument('--speed', required=True, type=int, help='adapter speed in kHz (the roster value)')
    ap.add_argument('--elf', required=True, help='the ELF the board should be running')
    ap.add_argument('--symbol', required=True, help='a flash-resident function the firmware runs repeatedly')
    ap.add_argument('--wait-ms', type=int, default=3000, help='how long to wait for the breakpoint (3000)')
    ap.add_argument('--timeout', type=int, default=120, help='OpenOCD wall limit in seconds (120)')
    args = ap.parse_args(argv)
    for name, value in (('--probe', args.probe), ('--elf', os.path.abspath(args.elf))):
        if not SAFE_ARG.fullmatch(value):
            print(f'error: {name} {value!r} has characters outside [A-Za-z0-9_./+-]', file=sys.stderr)
            return 2
    if not os.path.isfile(args.elf):
        print(f'error: {args.elf}: no such file', file=sys.stderr)
        return 2
    nm = os.environ.get('RP2040_VERIFY_NM', 'arm-none-eabi-nm')
    openocd = os.environ.get('RP2040_VERIFY_OPENOCD', 'openocd')
    try:
        addr = symbol_address(nm, args.elf, args.symbol)
    except ValueError as e:
        print(f'error: {e}', file=sys.stderr)
        return 2
    except ToolError as e:
        print(f'error: {e}', file=sys.stderr)
        return 3
    cmd = [openocd, '-c', 'gdb port disabled; tcl port disabled; telnet port disabled',
           '-f', args.interface_cfg, '-c', f'adapter serial {args.probe}',
           '-c', 'set USE_CORE 0',  # the default SMP pair fails `resume` when core 1 will not halt
           '-f', 'target/rp2040.cfg', '-c', f'adapter speed {args.speed}',
           '-c', tcl_batch(addr, os.path.abspath(args.elf), args.wait_ms)]
    try:
        run = subprocess.run(cmd, capture_output=True, text=True, timeout=args.timeout)
    except (OSError, subprocess.TimeoutExpired) as e:
        print(f'error: {openocd}: {e}', file=sys.stderr)
        return 3
    text = run.stdout + run.stderr
    sys.stderr.write(text)
    result, dhcsr, restore_errors = parse(text)
    reset_detected = 'external reset detected' in text
    running = dhcsr is not None and not dhcsr & S_HALT and not restore_errors
    print(json.dumps({'symbol': args.symbol, 'address': f'0x{addr:08x}', 'result': result,
                      'dhcsr': None if dhcsr is None else f'0x{dhcsr:08x}', 'running': running,
                      'restoreErrors': restore_errors, 'resetDetected': reset_detected,
                      'openocdExit': run.returncode}))
    if result is None or not running or reset_detected or run.returncode != 0:
        return 3
    if result.startswith('verified '):
        return 0
    return 1 if result.startswith(('not-at-breakpoint ', 'mismatch ')) else 3


if __name__ == '__main__':
    sys.exit(main())
