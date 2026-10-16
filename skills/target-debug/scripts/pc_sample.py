#!/usr/bin/env python3
"""Where does the core spin? Sample DWT_PCSR without halting and histogram the PCs by
function (the target-debug skill's PC-sampling technique).

  pc_sample.py --probe <serial> --device <JLINK_DEVICE> --interface swd --speed 4000 \
               --elf <flashed.elf> [--samples N]
  pc_sample.py --hil-config <file> --board <name> [--interface swd --speed 4000] \
               --elf <flashed.elf> [--samples N] [--halt-samples N --halt-timeout-ms M]

A J-Link is driven here through J-Link Commander (PC_SAMPLE_JLINK_EXE names it when it is
not `JLinkExe`, `JLink.exe` on Windows, on PATH). A HIL board flashed over openocd is
sampled through probe_state.py's OpenOCD session (the file next to this one; its
PROBE_STATE_OPENOCD names openocd), which also reports the other cores' DHCSR.

--halt-samples N (openocd boards only) is the fallback for a core without a usable PCSR:
when the samples gave none from a core found running, a second session re-reads DHCSR and,
on a single-core target that is running, halts it N times (each halt waits up to
--halt-timeout-ms), reads PC and resumes, stopping at the first halt or resume that fails
and leaving the core resumed when it can (a session that fails or times out mid-loop is
followed by one that resumes a core it left halted). Those PCs are counted apart from the observed
ones. A multi-core target is refused: an SMP halt stops every core and OpenOCD cannot
resume a core it has not polled. J-Link Commander cannot stop on a failed resume, so the
J-Link route has no fallback.

Reads DEMCR first and refuses the histogram while DEMCR.TRCENA (DWTENA on
ARMv6-M) is 0 (techniques.md, PC-sampling). Reads DHCSR before and after
the samples so a 0xFFFFFFFF sample (core halted or in WFI) can be read against
the core's state. A capture is complete only when every requested sample, the
DEMCR read and both DHCSR reads came back: JLinkExe exits 0 after a failed
command, so the output is the evidence, not the exit code.
"""
import argparse
import collections
import json
import math
import os
import re
import shlex
import shutil
import subprocess
import sys

DWT_PCSR = 0xE000101C
DHCSR = 0xE000EDF0
DEMCR = 0xE000EDFC
DEMCR_TRCENA = 1 << 24  # DWTENA on ARMv6-M; DWT reads are invalid while it is 0
SENTINEL = 0xFFFFFFFF
# "E000101C = 20000ABC" — JLinkExe may prefix the line with its "J-Link>" prompt
_MEM32_RE = re.compile(r'([0-9A-Fa-f]{8}) = ([0-9A-Fa-f]{8})')


class ToolError(RuntimeError):
    pass


def jlink_script(samples, interval_ms):
    lines = [f'mem32 {DEMCR:X}, 1', f'mem32 {DHCSR:X}, 1']
    for _ in range(samples):
        lines.append(f'mem32 {DWT_PCSR:X}, 1')
        if interval_ms:
            lines.append(f'Sleep {interval_ms}')
    lines += [f'mem32 {DHCSR:X}, 1', 'qc']
    return '\n'.join(lines) + '\n'


def parse_reads(text):
    """(pcs, dhcsr, demcr) from JLinkExe output: every DWT_PCSR, DHCSR and DEMCR
    value, each in order."""
    reads = {DWT_PCSR: [], DHCSR: [], DEMCR: []}
    for m in _MEM32_RE.finditer(text):
        addr = int(m.group(1), 16)
        if addr in reads:
            reads[addr].append(int(m.group(2), 16))
    return reads[DWT_PCSR], reads[DHCSR], reads[DEMCR]


def sample(probe, device, interface, speed, samples, interval_ms, timeout):
    name = os.environ.get('PC_SAMPLE_JLINK_EXE') or ('JLink.exe' if os.name == 'nt' else 'JLinkExe')
    exe = shutil.which(name)
    if not exe:
        raise ToolError(f'{name} not found - install J-Link Commander or set PC_SAMPLE_JLINK_EXE')
    cmd = [exe, '-device', device, '-SelectEmuBySN', probe, '-if', interface, '-speed', speed,
           '-autoconnect', '1', '-nogui', '1']
    try:
        r = subprocess.run(cmd, input=jlink_script(samples, interval_ms), capture_output=True,
                           text=True, timeout=timeout)
    except subprocess.TimeoutExpired as e:
        raise ToolError(f'{name} did not finish within {timeout:.0f} s') from e
    if r.returncode != 0:
        # reads printed before the failure can look like a complete capture
        tail = '\n'.join((r.stdout + r.stderr).strip().splitlines()[-6:])
        raise ToolError(f'{name} exited {r.returncode}; last output:\n{tail}')
    pcs, dhcsr, demcr = parse_reads(r.stdout)
    if len(pcs) != samples or len(dhcsr) != 2 or len(demcr) != 1:
        tail = '\n'.join((r.stdout + r.stderr).strip().splitlines()[-6:])
        raise ToolError(f'incomplete capture: {len(pcs)}/{samples} samples, {len(dhcsr)}/2 DHCSR reads, '
                        f'{len(demcr)}/1 DEMCR reads (exit {r.returncode}); last output:\n{tail}')
    if not demcr[0] & DEMCR_TRCENA:
        raise ToolError(f'DEMCR.TRCENA (DWTENA on ARMv6-M) is 0 (DEMCR=0x{demcr[0]:08x}): DWT_PCSR samples are '
                        f'not valid while trace is disabled; enable trace from a debugger session first')
    return pcs, dhcsr


def probe_state_module():
    sys.path.insert(0, os.path.dirname(os.path.abspath(__file__)))
    try:
        import probe_state
    except ImportError as e:
        raise ToolError(f'an openocd board needs probe_state.py next to this script: {e}')
    return probe_state


def sample_openocd(backend, samples, interval_ms, timeout):
    """(pcs, [dhcsr before, after], {other core: (before, after)}) through probe_state's session."""
    ps = probe_state_module()
    cap = ps.Capture(samples, interval_ms, False, [])
    try:
        vals = cap.unpack(backend.observe(cap, timeout))
    except ps.ToolError as e:
        raise ToolError(str(e)) from e
    if vals['cpuid'] >> 16 & 0xF not in ps.M_PROFILE_ARCH:
        raise ToolError(f"the target does not identify as an M-profile core (CPUID 0x{vals['cpuid']:08x})")
    if not vals['demcr'] & DEMCR_TRCENA:
        raise ToolError(f"DEMCR.TRCENA (DWTENA on ARMv6-M) is 0 (DEMCR=0x{vals['demcr']:08x}): DWT_PCSR samples "
                        f'are not valid while trace is disabled; enable trace from a debugger session first')
    return vals['pcs'], [vals['dhcsr0'], vals['dhcsr1']], backend.others


def halt_tcl(n, halt_timeout_ms, interval_ms):
    """Halt, read PC, resume, n times on a single running core; judged by DHCSR, since
    OpenOCD's halt and resume errors are not reliable either way on these targets."""
    pause = f'  sleep {interval_ms}\n' if interval_ms else ''
    return (f'set d [read_memory 0x{DHCSR:08x} 32 1]\necho [format "R DHCSR 0x%08x" $d]\n'
            'if {[llength [target names]] > 1} {\n'
            '  error "refused: [llength [target names]] cores ([target names]); an SMP halt stops them all '
            'and OpenOCD cannot resume a core it has not polled"\n'
            '}\n'
            'if {$d & 0x20000} {error [format "refused: the core is halted now (DHCSR 0x%08x)" $d]}\n'
            'echo GUARDED\n'
            f'for {{set i 0}} {{$i < {n}}} {{incr i}} {{\n'
            f'  if {{[catch {{halt {halt_timeout_ms}; echo [format "H 0x%08x" [dict get [get_reg pc] pc]]}} e]}} {{\n'
            '    catch {resume}\n'
            f'    set a [read_memory 0x{DHCSR:08x} 32 1]\n'
            '    error [format "halt %d: %s; DHCSR after a resume 0x%08x" $i '
            '[expr {$e eq {} ? {failed without a message} : $e}] $a]\n'
            '  }\n'
            '  catch {resume}\n'
            f'  set a [read_memory 0x{DHCSR:08x} 32 1]\n'
            '  if {$a & 0x20000} {error [format "resume %d failed: the core is left halted (DHCSR 0x%08x)" $i $a]}\n'
            f'{pause}'
            '}\n'
            f'echo [format "R DHCSR 0x%08x" [read_memory 0x{DHCSR:08x} 32 1]]')


# After a halting session that failed or was killed mid-loop: resume a single core left halted
# (it was running when that session began), and report what the core is doing. A new session
# must poll before `resume`, which refuses a target whose state is not yet known to be halted.
RECOVER_TCL = (f'if {{[llength [target names]] > 1}} {{error "not touched: [llength [target names]] cores"}}\n'
               f'set d [read_memory 0x{DHCSR:08x} 32 1]\necho [format "R DHCSR 0x%08x" $d]\n'
               'if {$d & 0x20000} {[target current] arp_poll; catch {resume}}\n'
               f'echo [format "R DHCSR 0x%08x" [read_memory 0x{DHCSR:08x} 32 1]]')


def recover(backend, timeout):
    ps = probe_state_module()
    try:
        _, reads = backend.batch(RECOVER_TCL, timeout)
    except ps.ToolError as e:
        return f'; the recovery session failed too ({e}): check the core with probe_state.py'
    dhcsr = [v for name, v in reads if name == 'DHCSR']
    if len(dhcsr) != 2:
        return '; the recovery session read no DHCSR: check the core with probe_state.py'
    first, last = (v & (1 << 17) for v in dhcsr)
    now = f"{'halted' if last else 'running'} (DHCSR 0x{dhcsr[1]:08x})"
    return f'; the core was left halted, resumed: now {now}' if first else f'; the core is {now}'


def halt_samples(backend, n, halt_timeout_ms, interval_ms, timeout):
    """(pcs, [dhcsr before, after]) from halt reads; raises ToolError on a refusal or failure,
    after a recovery session when the loop may have started."""
    ps = probe_state_module()
    try:
        text, reads = backend.batch(halt_tcl(n, halt_timeout_ms, interval_ms), timeout)
    except ps.ToolError as e:
        # recover only a core this session showed running on a single-core target before halting it;
        # openocd flushes each echo at once (log_puts in log.c), so GUARDED is out before the first halt
        if re.search(r'^GUARDED$', getattr(e, 'text', ''), re.M):
            after = recover(backend, 30)
        elif str(e).startswith('openocd: refused:'):
            after = ''
        else:
            after = '; it stopped before its guards passed, so it halted nothing: check the core with probe_state.py'
        raise ToolError(f'halting fallback: {e}{after}') from e
    pcs = [int(v, 16) for v in re.findall(r'^H 0x([0-9a-f]{8})$', text, re.M)]
    dhcsr = [v for name, v in reads if name == 'DHCSR']
    if len(pcs) != n or len(dhcsr) != 2:
        raise ToolError(f'halting fallback: {len(pcs)}/{n} halt reads; last output:\n{ps.tail(text)}')
    return pcs, dhcsr


def symbolize(addr2line, elf, pcs, timeout):
    """{pc: (func, 'file:line')} for the given PCs; a PC addr2line cannot place is ('??', '')."""
    if not pcs:
        return {}
    exe = shutil.which(addr2line)
    if not exe:
        raise ToolError(f'{addr2line} not on PATH')
    cmd = [exe, '-e', elf, '-f', '-C'] + [f'0x{pc:08x}' for pc in pcs]
    try:
        r = subprocess.run(cmd, capture_output=True, text=True, timeout=timeout)
    except subprocess.TimeoutExpired as e:
        raise ToolError(f'{addr2line} did not finish within {timeout:.0f} s') from e
    if r.returncode != 0:
        raise ToolError(f'{addr2line} failed: {r.stderr.strip() or r.stdout.strip()}')
    lines = r.stdout.splitlines()
    if len(lines) != 2 * len(pcs):
        raise ToolError(f'{addr2line} returned {len(lines)} lines for {len(pcs)} addresses')
    out = {}
    for i, pc in enumerate(pcs):
        func, loc = lines[2 * i].strip(), lines[2 * i + 1].strip()
        out[pc] = ('??', '') if func.startswith('??') else (func, loc)
    return out


def report(pcs, dhcsr, symbols, top, out=sys.stdout, what='samples'):
    """Histogram by function (a spin loop is several PCs), each with its PC sites."""
    usable = [pc for pc in pcs if pc not in (0, SENTINEL)]
    sentinel = sum(1 for pc in pcs if pc == SENTINEL)
    no_pcsr = sum(1 for pc in pcs if pc == 0)
    by_pc = collections.Counter(usable)
    by_func = collections.Counter()
    sites = collections.defaultdict(list)
    for pc, n in by_pc.items():
        func, loc = symbols.get(pc, ('??', ''))
        by_func[func] += n
        sites[func].append((n, pc, loc))
    unresolved = by_func['??']
    print(f'{len(usable)} usable of {len(pcs)} {what}', file=out)
    for func, n in by_func.most_common(top):
        print(f'{n:6d} {100 * n / len(usable):5.1f}%  {func}', file=out)
        for m, pc, loc in sorted(sites[func], reverse=True)[:4]:
            print(f'{"":6} {m:6d}    0x{pc:08x}  {loc}', file=out)
    print(f'sentinel 0xFFFFFFFF (halted or WFI): {sentinel}', file=out)
    print(f'no PCSR (0): {no_pcsr}', file=out)
    print(f'unresolved symbols: {unresolved}', file=out)
    for when, v in zip(('before', 'after'), dhcsr):
        halt = 'halted' if v & (1 << 17) else 'running'
        print(f'DHCSR {when}: 0x{v:08x} ({halt}{", reset since last read" if v & (1 << 25) else ""})',
              file=out)
    return len(usable)


HIL_PROBE_ROUTES = {'jlink': 'jlink', 'openocd': 'openocd', 'stlink': 'openocd'}


def hil_flasher(path, board):
    """The `flasher` of one board in a project's HIL config json ({"boards": [{"name",
    "flasher": {"name", "uid", "args", "vid_pid"}}]}), checked but not completed: only
    flasher.name must be there, what else is missing is the caller's to supply. Raises
    ValueError. Kept identical in rtt.py and pc_sample.py, which are copied around alone."""
    try:
        with open(path) as f:
            config = json.load(f)
    except (OSError, ValueError) as e:
        raise ValueError(f'--hil-config {path}: {e}')
    boards = config.get('boards') if isinstance(config, dict) else None
    if not isinstance(boards, list) or not all(isinstance(b, dict) for b in boards):
        raise ValueError(f'--hil-config {path}: expected {{"boards": [{{...}}, ...]}}')
    found = [b for b in boards if b.get('name') == board]
    if len(found) != 1:
        names = ', '.join(sorted(str(b.get('name')) for b in boards))
        raise ValueError(f'--board {board}: {len(found)} entries of that name in {path} (it has: {names})')
    flasher = found[0].get('flasher')
    if not isinstance(flasher, dict) or not isinstance(flasher.get('name'), str):
        raise ValueError(f'--board {board}: no flasher.name in {path}')
    if flasher['name'] not in HIL_PROBE_ROUTES:
        raise ValueError(f"--board {board} is flashed over {flasher['name']!r}, which is no debug-probe route "
                         f"(known: {', '.join(HIL_PROBE_ROUTES)}); name the probe with explicit flags")
    for key in ('uid', 'args', 'vid_pid'):
        if key in flasher and (not isinstance(flasher[key], str) or not flasher[key].strip()):
            raise ValueError(f'--board {board}: flasher.{key} in {path} must be a non-empty string, '
                             f'got {flasher[key]!r}')
    return flasher


def hil_jlink_device(board, flasher):
    """The J-Link device of a jlink entry, whose args may be exactly `-device NAME`: any
    other flag there would be one this script silently drops."""
    if 'args' not in flasher:
        return None
    try:
        words = shlex.split(flasher['args'])
    except ValueError as e:
        raise ValueError(f"--board {board}: flasher.args {flasher['args']!r}: {e}")
    if len(words) != 2 or words[0] != '-device' or not words[1].strip():
        raise ValueError(f"--board {board}: flasher.args must be exactly '-device NAME', got "
                         f"{flasher['args']!r}; pass the probe flags explicitly instead")
    return words[1]


def hil_merge(given, board, **resolved):
    """Fill the flags the caller left out; one the caller gave must agree with the file."""
    for flag, value in resolved.items():
        explicit = getattr(given, flag)
        if value is None:
            continue
        if explicit is not None and explicit.strip() != value.strip():
            raise ValueError(f"--{flag.replace('_', '-')} {explicit!r} contradicts --board {board}, "
                             f"which has {value!r}")
        setattr(given, flag, value)


def main(argv=None):
    p = argparse.ArgumentParser(description=__doc__, formatter_class=argparse.RawDescriptionHelpFormatter)
    p.add_argument('--probe', help='J-Link serial (rigs run several probes)')
    p.add_argument('--device', help='J-Link device name')
    p.add_argument('--hil-config', metavar='FILE',
                   help="the project's HIL config json; with --board it supplies --probe and --device "
                        "of a J-Link board. A flag given as well must agree")
    p.add_argument('--board', metavar='NAME', help='board name in --hil-config')
    p.add_argument('--elf', required=True, help='the flashed firmware ELF, for symbols')
    p.add_argument('--samples', type=int, default=300, help='DWT_PCSR reads (default 300)')
    p.add_argument('--interval-ms', type=int, default=0,
                   help='JLinkExe Sleep between reads, milliseconds (default 0: back to back)')
    p.add_argument('--interface', choices=['swd', 'jtag'], help='J-Link: target interface, no default')
    p.add_argument('--speed', help='J-Link: interface speed in kHz, or "auto"/"adaptive"; no default')
    p.add_argument('--halt-samples', type=int, default=0,
                   help='openocd boards: halt reads when the samples gave no usable PC from a running core')
    p.add_argument('--halt-timeout-ms', type=int, default=500, help='wait for each halt (default 500)')
    p.add_argument('--timeout', type=float, help='bound on each tool run in seconds '
                   '(default: samples x (interval + 50 ms) + 20 s)')
    p.add_argument('--top', type=int, default=15, help='histogram rows (default 15)')
    p.add_argument('--raw', help='also write every sampled PC, one hex value per line')
    p.add_argument('--addr2line', default='arm-none-eabi-addr2line', help='symbolizer (default arm-none-eabi-addr2line)')
    a = p.parse_args(argv)
    if bool(a.hil_config) != bool(a.board):
        p.error('--hil-config and --board go together')
    backend = None
    if a.hil_config:
        try:
            flasher = hil_flasher(a.hil_config, a.board)
            if flasher['name'] == 'openocd':
                if a.probe or a.device:
                    raise ValueError('--probe and --device are J-Link flags; an openocd board takes its HIL entry')
                ps = probe_state_module()
                try:
                    _, _, backend = ps.resolve(a, p)
                except ps.ToolError as e:
                    raise ValueError(str(e))
            elif flasher['name'] != 'jlink':
                raise ValueError(f"--board {a.board} is flashed over {flasher['name']!r}; pc_sample.py "
                                 f"samples over a J-Link or openocd only")
            else:
                hil_merge(a, a.board, probe=flasher.get('uid'), device=hil_jlink_device(a.board, flasher))
        except ValueError as e:
            p.error(str(e))
    if a.halt_samples < 0 or a.halt_timeout_ms <= 0:
        p.error('--halt-samples must be non-negative, --halt-timeout-ms positive')
    if a.halt_samples and backend is None:
        p.error('--halt-samples is for openocd boards: J-Link Commander cannot stop on a failed resume')
    if backend is None:
        if not (a.probe and a.device):
            p.error('need --probe and --device (or --hil-config with --board)')
        if not a.probe.strip() or not a.device.strip():
            p.error('--probe and --device must not be empty')
        if not (a.interface and a.speed):
            p.error('a J-Link needs --interface and --speed')
        if not re.fullmatch(r'[1-9][0-9]*|auto|adaptive', a.speed):
            p.error(f'--speed must be kHz, "auto" or "adaptive", got {a.speed!r}')
    if a.samples <= 0 or a.interval_ms < 0 or a.top <= 0:
        p.error('--samples and --top must be positive, --interval-ms non-negative')
    if a.raw and os.path.lexists(a.raw):
        p.error(f'--raw {a.raw} already exists: name a new file')
    if a.timeout is not None and not (math.isfinite(a.timeout) and a.timeout > 0):
        p.error('--timeout must be a finite positive number of seconds')
    if not os.path.isfile(a.elf):
        p.error(f'ELF not found: {a.elf}')
    timeout = a.timeout if a.timeout is not None else a.samples * (a.interval_ms + 50) / 1000 + 20
    halt_timeout = a.timeout if a.timeout is not None else \
        a.halt_samples * (a.halt_timeout_ms + a.interval_ms + 100) / 1000 + 20
    others, halted_pcs, halted_dhcsr, fallback_error = {}, [], [], None
    try:
        if backend is None:
            pcs, dhcsr = sample(a.probe, a.device, a.interface, a.speed, a.samples, a.interval_ms, timeout)
        else:
            pcs, dhcsr, others = sample_openocd(backend, a.samples, a.interval_ms, timeout)
        if a.raw:
            with open(a.raw, 'w') as f:
                f.writelines(f'{pc:08x}\n' for pc in pcs)
        found_running = not dhcsr[0] & (1 << 17)
        if a.halt_samples and found_running and not any(pc not in (0, SENTINEL) for pc in pcs):
            try:
                halted_pcs, halted_dhcsr = halt_samples(backend, a.halt_samples, a.halt_timeout_ms,
                                                        a.interval_ms, halt_timeout)
            except ToolError as e:
                fallback_error = str(e)
        wanted = {pc for pc in pcs + halted_pcs if pc not in (0, SENTINEL)}
        symbols = symbolize(a.addr2line, a.elf, sorted(wanted), timeout)
    except ToolError as e:
        print(f'error: {e}', file=sys.stderr)
        return 1
    usable = report(pcs, dhcsr, symbols, a.top)
    for name, (before, after) in sorted(others.items()):
        print(f'{name} DHCSR before 0x{before:08x}, after 0x{after:08x}')
    if halted_pcs:
        print('--- halted samples: PC read at each halt, then resumed (not PCSR) ---')
        usable += report(halted_pcs, halted_dhcsr, symbols, a.top, what='halt reads')
    if fallback_error:
        print(f'error: {fallback_error}', file=sys.stderr)
        return 1
    if not usable:
        print('error: no usable sample (core halted, asleep, or no DWT_PCSR on this core)', file=sys.stderr)
        return 1
    return 0


if __name__ == '__main__':
    sys.exit(main())
