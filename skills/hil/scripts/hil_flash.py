#!/usr/bin/env python3
"""Flash and reset commands per flasher backend, shared by the HIL harnesses (`hil` skill,
Flashers). Linux only, stdlib only.

Every operation takes a flasher dict (`name`, `uid`, `args`, optional `vid_pid` and
`verify`), never a board, roster, example or build root; it builds the backend's command as
an argv list, `args` split as a shell would split it but never expanded, and runs it
through `run` (default hil_process.run_cmd, the instance loaded here as
`hil_flash.hil_process`), called as `run(argv, timeout=...)` or, for esptool,
`run(argv, cwd=..., env=..., timeout=...)`, so a caller injects its own runner and never
parses a shell string. Every operation takes `timeout` explicitly. A flash or reset returns
an Operation of its Attempts; what their kinds, cleanup and receipts mean to a caller is the
`hil` skill's (SKILL.md, Flashers).
"""

from __future__ import annotations

import contextlib
import glob
import importlib.util
import json
import os
import re
import shlex
import shutil
import signal
import stat
import subprocess
import sys
import tempfile
import time
from pathlib import Path
from typing import NamedTuple

HERE = os.path.dirname(os.path.abspath(__file__))
PYOCD_LAUNCHER = os.path.join(HERE, 'pyocd', 'run_pyocd.py')
FILES = ('skills/hil/scripts/hil_load.py', 'skills/hil/scripts/hil_process.py',
         'skills/hil/scripts/pyocd/run_pyocd.py', 'skills/hil/scripts/pyocd/user_script.py',
         'skills/hil/scripts/hil_openocd_esp.py', 'skills/hil/scripts/usb_observe.py',
         'skills/target-debug/scripts/esp_session.py', 'skills/hil/scripts/esptool_session.py',
         'skills/hil/scripts/hil_openocd_esp_receipt.py')


def _hil_load():
    """hil_load.py from beside this file, the process's one `agentrc_hil_load`."""
    spec = importlib.util.spec_from_file_location('agentrc_hil_load', os.path.join(HERE, 'hil_load.py'))
    if spec.name not in sys.modules:
        if not os.path.isfile(spec.origin):
            raise ImportError(f'{spec.origin} is missing')
        mod = importlib.util.module_from_spec(spec)
        spec.loader.exec_module(mod)
        sys.modules.setdefault(spec.name, mod)
    return sys.modules[spec.name].load(spec.name, spec.origin)


hil_load = _hil_load()
hil_load.require(__file__, FILES)
hil_process = hil_load.load_file('skills/hil/scripts/hil_process.py')

_VID_PID_WARNED: set = set()   # one warning per probe, not per command


def _runner(run):
    return hil_process.run_cmd if run is None else run


UID_RE = re.compile(r'[A-Za-z0-9_.:+-]+')


def _uid(flasher: dict) -> str:
    """The flasher's probe serial, refused unless it is one plain word: the builders splice it into
    command files and Tcl `-c` commands."""
    uid = flasher.get('uid')
    if not isinstance(uid, str) or not UID_RE.fullmatch(uid):
        raise ValueError(f'{flasher.get("name")} uid {uid!r}: want a probe serial (letters, digits and _ . : + -)')
    return uid


CLEANUP = ('done', 'unknown', 'pending')   # least to most outstanding


class Attempt(NamedTuple):
    """One run of a tool: the command, its command file's contents if it has one, the runner's
    result (returncode None and any partial output when it raised), the exception it raised,
    its cleanup ('done' only when the runner said so explicitly, else 'unknown' or the runner's
    'pending') and the receipt of a session still outstanding."""
    cmd: str | list
    script: str | None
    result: subprocess.CompletedProcess
    error: BaseException | None
    cleanup: str
    receipt: object

    @property
    def kind(self) -> str:
        e = self.error
        if isinstance(e, subprocess.TimeoutExpired) or e is None and self.result.returncode == 124:
            return 'timed_out'
        if e is None:
            return 'exited'
        return 'interrupted' if not isinstance(e, Exception) or getattr(e, 'interrupted', False) else 'error'

    @property
    def timed_out(self) -> bool:
        return self.kind == 'timed_out'

    @property
    def interrupted(self) -> bool:
        return self.kind == 'interrupted'

    @property
    def errored(self) -> bool:
        return self.kind == 'error'


def _record(cmd, script, result, error) -> Attempt:
    source = result if error is None else error
    cleanup = getattr(source, 'cleanup', None)
    return Attempt(cmd, script, result, error, cleanup if cleanup in CLEANUP else 'unknown',
                   getattr(source, 'receipt', None))


class Operation(subprocess.CompletedProcess):
    """The last attempt's CompletedProcess fields, so a caller reading returncode and stdout
    keeps working, plus `attempts`: every run in order with its own output, never
    concatenated. Every attempt but the last was retried. A runner's exception carries the
    operation so far as `.operation`. `cleanup` is the worst attempt's ('pending' over
    'unknown' over 'done'), `receipts` every outstanding session's receipt in order."""

    def __init__(self, attempts: list, retry_refused=None):
        r = attempts[-1].result
        super().__init__(r.args, r.returncode, r.stdout, r.stderr)
        self.attempts = tuple(attempts)
        self.cleanup = max((a.cleanup for a in attempts), key=CLEANUP.index)
        self.receipts = tuple(a.receipt for a in attempts if a.receipt is not None)
        self.retry_refused = retry_refused

    @property
    def retried(self) -> tuple:
        return self.attempts[:-1]


def _attempt(attempts: list, run, cmd, script=None, **kw) -> subprocess.CompletedProcess:
    try:
        ret = run(cmd, **kw)
    except BaseException as e:
        partial = subprocess.CompletedProcess(cmd, None, getattr(e, 'stdout', None), getattr(e, 'stderr', None))
        attempts.append(_record(cmd, script, partial, e))
        e.operation = Operation(attempts)
        raise
    attempts.append(_record(cmd, script, ret, None))
    return ret


def _args(flasher: dict) -> list:
    """The roster's `args` as argv words: split as a shell splits them, never expanded."""
    return shlex.split(flasher.get('args') or '')


def _once(run, cmd, **kw) -> Operation:
    attempts = []
    _attempt(attempts, _runner(run), cmd, **kw)
    return Operation(attempts)


# -------------------------------------------------------------
# J-Link
# -------------------------------------------------------------
# libjlinkarm.so (V9.66 and V9.78) corrupts its heap on on-board J-Link probes (OB, LPC-Link 2,
# reflashed ST-Link) and dies by SIGSEGV or SIGABRT ("double free"), 3 of 810 runs on feather's OB.
# Every crash seen came right after reading the probe's S/N, before target access, so the whole
# script can rerun. rc is 128+N through the shell, -N when it execs directly.
JLINK_CRASH_RCS = {128 + signal.SIGSEGV, -signal.SIGSEGV, 128 + signal.SIGABRT, -signal.SIGABRT}


@contextlib.contextmanager
def _command_file(script: list, *, prefix='hil_jlink_', suffix='.jlink', workdir=None):
    # A unique file per invocation in `workdir` (default the current directory): a stale or
    # concurrent file is never the one run, and none outlives the invocation. Yields (path, text).
    fd, path = tempfile.mkstemp(prefix=prefix, suffix=suffix, dir=workdir or os.getcwd())
    text = ''.join(f'{s}\n' for s in script)
    try:
        with os.fdopen(fd, 'w') as f:
            f.write(text)
        yield path, text
    finally:
        Path(path).unlink(missing_ok=True)


def _run_jlink(flasher: dict, script: list, *, timeout, crash_retries: int, run, workdir=None) -> Operation:
    # crash_retries reruns share one deadline, taken once the command file is written, and
    # never start over a session the runner reports pending or names by a receipt. A rerun is
    # no proof the target was untouched.
    run, attempts, uid = _runner(run), [], _uid(flasher)
    with _command_file(script, workdir=workdir) as (path, text):
        cmd = ['JLinkExe', '-USB', uid, *_args(flasher), '-if', 'swd', '-JTAGConf', '-1,-1', '-speed', 'auto',
               '-NoGui', '1', '-ExitOnError', '1', '-CommandFile', path]
        deadline = time.monotonic() + timeout
        ret = _attempt(attempts, run, cmd, text, timeout=timeout)
        for _ in range(crash_retries):
            left = deadline - time.monotonic()
            if ret.returncode not in JLINK_CRASH_RCS or left <= 0 \
                    or attempts[-1].cleanup == 'pending' or attempts[-1].receipt is not None:
                break
            print(f'JLinkExe -USB {uid} crashed (rc {ret.returncode}), rerunning', flush=True)
            ret = _attempt(attempts, run, cmd, text, timeout=left)
    return Operation(attempts)


def flash_jlink(flasher: dict, firmware: str, *, timeout,
                crash_retries: int = 0, run=None, workdir=None) -> Operation:
    # loadfile resets and halts the target itself by default (J-Link Commander, LoadFile)
    script = ['halt', f'loadfile {firmware}', 'r', 'go', 'exit']
    return _run_jlink(flasher, script, timeout=timeout, crash_retries=crash_retries, run=run, workdir=workdir)


def reset_jlink(flasher: dict, *, timeout,
                crash_retries: int = 0, run=None, workdir=None) -> Operation:
    script = ['halt', 'r', 'go', 'exit']
    return _run_jlink(flasher, script, timeout=timeout, crash_retries=crash_retries, run=run, workdir=workdir)


def _retry_bounds(retries, retry_delay) -> None:
    if not isinstance(retries, int) or isinstance(retries, bool) or retries < 0:
        raise ValueError(f'retries {retries!r}: want a whole number of reruns, 0 or more')
    if not isinstance(retry_delay, (int, float)) or isinstance(retry_delay, bool) or not 0 <= retry_delay < 3600:
        raise ValueError(f'retry_delay {retry_delay!r}: want seconds, 0 to under 3600')


def run_jlink_commands(flasher: dict, commands: list, *, interface: str, speed, autoconnect: bool, device: str,
                       timeout, run=None, workdir=None, retry_if=None, retries: int = 1,
                       retry_delay: float = 0) -> Operation:
    """One JLinkExe session running `commands`, then `exit`, from a command file in `workdir`,
    with the caller's connection profile: `device` replaces the flasher's `args` (a session
    may connect as the core), `autoconnect` False leaves the flag out. The raw result only:
    judging the transcript and recovering stay the caller's.

    `retry_if(attempt)`, the caller's judgement of an Attempt, reruns the whole session from the
    same command file while it answers true, at most `retries` times, `retry_delay` s apart,
    each run bounded by `timeout`. A rerun is refused after an attempt whose cleanup is not an
    explicit `done` or that names a receipt: something of it may still hold the probe. The
    refusal is the Operation's `retry_refused` (why), its attempts ending with the one judged."""
    if retry_if is not None:
        _retry_bounds(retries, retry_delay)
    run, attempts, script, uid = _runner(run), [], [*commands, 'exit'], _uid(flasher)
    with _command_file(script, workdir=workdir) as (path, text):
        argv = ['JLinkExe', '-USB', uid, '-device', device, '-if', interface, '-speed', str(speed),
                *(['-autoconnect', '1'] if autoconnect else []), '-NoGui', '1', '-ExitOnError', '1',
                '-CommandFile', path]
        _attempt(attempts, run, argv, text, timeout=timeout)
        refused = None
        for _ in range(retries if retry_if is not None else 0):
            last = attempts[-1]
            try:
                again = retry_if(last)
            except BaseException as e:
                e.operation = Operation(attempts)
                raise
            if not again:
                break
            if last.cleanup != 'done' or last.receipt is not None:
                refused = (f'cleanup {last.cleanup} of the attempt judged for a rerun'
                           + (', which names a receipt' if last.receipt is not None else ''))
                break
            if retry_delay:
                time.sleep(retry_delay)
            _attempt(attempts, run, argv, text, timeout=timeout)
    return Operation(attempts, retry_refused=refused)


# Each JLinkExe -CommandFile command's completion line in its own output chunk, by its first
# word, formatted with its arguments, from recorded transcripts (V9.78 and V9.84; SEGGER
# documents the commands, not their output): `g`/`go` print none, and any line after `r`'s
# fails. A device script's AfterResetTarget() may close either form of `r` (the MK64 and
# LPC4088 reset via SYSRESETREQ, V9.78).
_AFTER_RESET = r'(?:AfterResetTarget\(\) start\n(?:AfterResetTarget\(\)\n)?AfterResetTarget\(\) end - Took .*\n)?'
_PC = r'^PC = [0-9A-F]{{8}}, '
JLINK_DONE = {'h': _PC, 'halt': _PC, 'regs': _PC,
              'r': r'^Reset delay: \d+ ms\n(?:(?:.*\n)*?ResetTarget\(\) end - Took .*\nDevice specific reset executed\.\n'
                   + _AFTER_RESET +
                   r'|(?![\s\S]*^ResetTarget\(\) start$)(?:.*\n)*?Reset: Reset device via AIRCR\.SYSRESETREQ\.\n'
                   + _AFTER_RESET + r')\s*\Z',
              'g': '', 'go': '', 'sleep': r'^Sleep\({0}\)$',
              'mem32': r'^{0} = [0-9A-F]{{8}}', 'w4': r'^Writing {1} -> {0}$', 'verifybin': r'^Verify successful\.$',
              'loadfile': r'^O\.K\.$', 'exit': r'^Script processing completed\.$'}
# A failure line in any command's chunk, from JLinkExe V9.84's own diagnostics (its strings:
# "****** Error: %s" for the DLL's errors, "Error occurred: ...", "Error while ...", "Could not
# read memory." and the like), and the connect and reset failures V9.78 prints; a connect's own
# notes, such as "Could not find valid reset vector.", are judged by its commands completing.
JLINK_ERRORS = (r'(?m)^\*+ Error|^Error (occurred|while)|ERROR|Verify failed|Cannot connect|Could not connect|'
                r'ResetTarget failed|Failed to (halt|reconnect|write memory)|CPU (is )?not halted|'
                r'Could not (read|write) memory|Could not read current PC|CPU may not have been reset|'
                r'CPU did not halt after reset')


def jlink_echoed(out: str) -> list:
    """(command, output chunk) for each command a JLinkExe -CommandFile transcript echoes, in
    order, each chunk ending at the next echo."""
    parts = re.split(r'(?m)^J-Link>(.*)$', out)
    return [(c.strip(), chunk) for c, chunk in zip(parts[1::2], parts[2::2])]


def jlink_after(out: str, command: str) -> list:
    """The output chunk of each echo of `command`."""
    return [chunk for c, chunk in jlink_echoed(out) if c == command]


def jlink_judgment(out: str, commands: list) -> dict:
    """Whether a JLinkExe -CommandFile transcript shows `commands` (without the closing `exit`),
    then `exit`, each echoed once per occurrence and in order with its completion line
    (JLINK_DONE) and no failure line (JLINK_ERRORS) in its own chunk. JLinkExe can exit 0 with
    every command failed (no probe with the serial), and -ExitOnError ends a script at a failed
    verifybin but not at a failed reset, which the next commands outlive, so no exit code or
    transcript-wide match stands in for a command's own output. A command with no completion
    rule fails. Returns {'ok', 'problem' ('' when ok), 'commands': [{'command', 'done', 'error'}]}."""
    want, got = [*commands, 'exit'], jlink_echoed(out)
    judged = []
    for c, chunk in got:
        rule = JLINK_DONE.get(c.split()[0] if c else '')
        try:          # an echo missing the arguments its rule names completes nothing
            done = rule is not None and bool(re.search(rule.format(*c.split()[1:]), chunk, re.M))
        except (IndexError, KeyError, ValueError, re.error):
            done = False
        error = re.search(JLINK_ERRORS, chunk)
        judged.append({'command': c, 'done': done, 'error': error[0] if error else None})
    if [c for c, _ in got] != want:
        problem = f'echoed {[c for c, _ in got]!r:.300}, not the script {want!r:.300}'
    elif bad := next((j for j in judged if j['error'] or not j['done']), None):
        what = f'failed ({bad["error"]!r})' if bad['error'] else (
            'has no completion rule' if bad['command'].split()[0] not in JLINK_DONE else 'did not complete')
        problem = f'{bad["command"]!r} {what}'
    else:
        problem = ''
    return {'ok': not problem, 'problem': problem, 'commands': judged}


def _last_exited(op, tool: str) -> tuple:
    """(problem, stdout text) of an Operation's last attempt: the problem '' only when it
    exited 0. An earlier attempt never counts."""
    last = op.attempts[-1]
    if last.kind != 'exited':
        return f'{tool} {last.kind.replace("_", " ")}', ''
    if last.result.returncode != 0:
        return f'{tool} exit {last.result.returncode}', ''
    return '', hil_process.cmd_stdout_text(last.result.stdout)


def jlink_problem(op) -> str:
    """Why a J-Link session Operation (flash_jlink, reset_jlink, run_jlink_commands,
    sysresetreq_jlink) did not complete its command file, '' when it did: its last attempt,
    which must have exited 0, judged by jlink_judgment against the command file it ran, with
    no failure line (JLINK_ERRORS) before the first echo or on stderr."""
    problem, out = _last_exited(op, 'JLinkExe')
    if problem:
        return problem
    lines = (op.attempts[-1].script or '').splitlines()
    if not lines or lines[-1] != 'exit':
        return 'no command file ending in exit to judge against'
    preamble = re.split(r'(?m)^J-Link>', out, maxsplit=1)[0]
    for where, text in (('before the first command', preamble),
                        ('on stderr', hil_process.cmd_stdout_text(op.attempts[-1].result.stderr))):
        if said := re.search(JLINK_ERRORS, text):
            return f'failed {where} ({said[0]!r})'
    return jlink_judgment(out, lines[:-1])['problem']


# Cortex-M SCB AIRCR and the SYSRESETREQ write tinyuf2's runner issues to it
SCB_AIRCR, AIRCR_SYSRESETREQ = 0xE000ED0C, 0x05FA0004


def sysresetreq_jlink(flasher: dict, *, interface: str, speed, autoconnect: bool, device: str,
                      timeout, run=None, workdir=None, retry_if=None, retries: int = 1,
                      retry_delay: float = 0) -> Operation:
    """SYSRESETREQ through a J-Link session, not the probe's reset (reset_jlink). The reset may
    drop the connection mid-write, so the caller judges the write's issue, not the exit;
    `retry_if` as run_jlink_commands'."""
    return run_jlink_commands(flasher, [f'w4 {SCB_AIRCR:08X} {AIRCR_SYSRESETREQ:08X}'], interface=interface,
                              speed=speed, autoconnect=autoconnect, device=device, timeout=timeout, run=run,
                              workdir=workdir, retry_if=retry_if, retries=retries, retry_delay=retry_delay)


# -------------------------------------------------------------
# ST-Link
# -------------------------------------------------------------
def flash_stlink(flasher: dict, firmware: str, *, timeout, run=None) -> Operation:
    # --verify catches the partial/corrupt write that exits 0 and sends the test phase
    # off to exercise bad firmware. Opt-IN here ("verify": true), unlike flash_openocd's
    # opt-out: a default-on read-back silently changes every roster entry that lacks the
    # key, including boards on rigs this was never validated against.
    verify = ['--verify'] if flasher.get('verify', False) else []
    return _once(run, ['STM32_Programmer_CLI', '--connect', 'port=swd', f'sn={_uid(flasher)}', '--write', str(firmware),
                       *verify, '--go'], timeout=timeout)


def reset_stlink(flasher: dict, *, timeout, run=None) -> Operation:
    return _once(run, ['STM32_Programmer_CLI', '--connect', 'port=swd', f'sn={_uid(flasher)}', '--rst', '--go'],
                 timeout=timeout)


def run_stlink_commands(flasher: dict, commands: list, *, connect_mode: str, timeout, run=None,
                        workdir=None) -> Operation:
    """One STM32_Programmer_CLI script session: an SWD connect to the flasher's ST-LINK in
    `connect_mode`, then `commands`, from a script file in `workdir`. The raw result only:
    judging the transcript, retrying and recovering stay the caller's."""
    script = [f'-c port=SWD sn={_uid(flasher)} mode={connect_mode}', *commands]
    with _command_file(script, prefix='hil_stlink_', suffix='.prg', workdir=workdir) as (path, text):
        return _once(run, ['STM32_Programmer_CLI', '-script', path], script=text, timeout=timeout)


def sysresetreq_stlink(flasher: dict, *, connect_mode: str, timeout, run=None, workdir=None) -> Operation:
    """SYSRESETREQ through an ST-LINK session, not the probe's reset (reset_stlink); `-nv`
    skips the read-back the reset would fail. The caller judges the write's report."""
    return run_stlink_commands(flasher, [f'-w32 0x{SCB_AIRCR:08X} 0x{AIRCR_SYSRESETREQ:08X} -nv'],
                               connect_mode=connect_mode, timeout=timeout, run=run, workdir=workdir)


# -------------------------------------------------------------
# OpenOCD
# -------------------------------------------------------------
def openocd_cmd_base(flasher: dict) -> list:
    # Optional roster field vid_pid, openocd-verbatim (e.g. "0x1a86 0x8010"), pins probe
    # discovery to the probe's IDs so openocd never opens foreign usbfs nodes to read
    # strings -- a wedged node makes that open hang unkillably (the 2026-08-10 convoy).
    # BEFORE args, because the rescue cfgs run `init` internally and reject (or never see)
    # a config command that follows it.
    uid, vid_pid = _uid(flasher), []
    if 'vid_pid' in flasher:
        # Validated HERE too, not just in convoy_safe: openocd only warns ("incomplete
        # vid_pid configuration directive") and exits 0 on a malformed value, so the pin
        # silently does not apply and discovery goes back to opening every usbfs node --
        # the convoy this field exists to stop. The same key name carries a DIFFERENT
        # syntax under tinyusb's tests.dev_attached ('1a86_55d4'), so the typo is one copy away.
        # Refused, not dropped: running unpinned reopens the convoy the explicit pin asked to avoid.
        if not valid_vid_pid(flasher['vid_pid']):
            raise ValueError(f'openocd flasher {flasher.get("uid")!r} has a malformed vid_pid '
                             f'{flasher["vid_pid"]!r}: want "0xVVVV 0xPPPP" pairs')
        vid_pid = ['-c', f'adapter usb vid_pid {flasher["vid_pid"]}']
    elif not convoy_safe(flasher) and flasher.get('uid') not in _VID_PID_WARNED:
        # (unpinned yet convoy-safe is openocd over a CONVOY_SAFE_CFGS driver: no warning)
        # stderr, once per probe: a captured stdout would swallow this on a passing run and
        # the operator would never learn discovery still opens every usbfs node
        _VID_PID_WARNED.add(flasher.get('uid'))
        print(f'warning: openocd flasher {flasher.get("uid", "?")} has no vid_pid pin; '
              f'probe discovery will open every usbfs node (hangs on a wedged one)',
              file=sys.stderr, flush=True)
    return ['openocd', '-c', 'tcl_port disabled', '-c', 'gdb_port disabled', '-c', 'telnet_port disabled',
            '-c', f'adapter serial {uid}', *vid_pid, *_args(flasher)]


# `verify` is on by default, opted out per board with "verify": false. ch32v103r and
# ch582m opt out: a raw dump_image of their flash is not the image just written (~85% of
# bytes differ, the same at 0x0 and 0x08000000, shifting with halt vs reset halt, and
# abstract memory reads fail), so verify_image always fails there.
# ch32v203 and ch32v307 read back byte-exact and verify, and catch a one-byte negative
# control, over progbuf and abstract alike (ci.lan, 2026-09-21). Do NOT drop verify
# fleet-wide -- without it a partial or corrupt write exits 0 and the tests run bad firmware.
# `program` = init; reset init; write_image erase; verify; reset run. Its reset-init event is
# what some targets need (kx.cfg's `kinetis disable_wdog`; without it frdm_k64f's verify takes
# 21 s). stm32f0x/f4x/f7x/l4x.cfg raise `adapter speed` to 8000 there, which their J-Link
# recovery entries cannot run, so those entries empty the event in the roster instead.
def flash_openocd(flasher: dict, firmware: str, *, timeout, run=None) -> Operation:
    verify = ' verify' if flasher.get('verify', True) else ''
    return _once(run, [*openocd_cmd_base(flasher), '-c', f'program {firmware}{verify} reset exit'], timeout=timeout)


def reset_openocd(flasher: dict, *, timeout, run=None) -> Operation:
    # timeout: a post-hang recovery bounds this; an unbounded reset there would outlive the
    # caller's outer kill and orphan openocd on the probe, which is the stray the recovery
    # exists to avoid.
    return _once(run, [*openocd_cmd_base(flasher), '-c', 'init; reset run; exit'], timeout=timeout)


def run_openocd_commands(flasher: dict, commands: list, *, timeout, run=None, workdir=None) -> Operation:
    """One openocd session: `init`, then `commands` (openocd Tcl commands) in order, then
    `shutdown`, from a script file in `workdir` run after the flasher's own config. openocd
    logs its transcript to stderr, exits 0 on the `shutdown`, and stops at the first failing
    command with exit 1, skipping the rest. The raw result only: judging the transcript,
    retrying and recovering stay the caller's."""
    # `shutdown`, not `exit`: outside a telnet/Tcl session `exit` is a deprecated alias of it
    # (server.c handle_exit_command). Without either openocd stays in its server loop.
    base, script = openocd_cmd_base(flasher), ['init', *commands, 'shutdown']
    with _command_file(script, prefix='hil_openocd_', suffix='.tcl', workdir=workdir) as (path, text):
        return _once(run, [*base, '-f', path], script=text, timeout=timeout)


def sysresetreq_openocd(flasher: dict, *, timeout, run=None, workdir=None) -> Operation:
    """SYSRESETREQ through an openocd session, not the probe's reset (reset_openocd). The reset
    may drop the connection mid-write, so the caller judges the write's issue, not the exit."""
    return run_openocd_commands(flasher, [f'mww 0x{SCB_AIRCR:08X} 0x{AIRCR_SYSRESETREQ:08X}'], timeout=timeout,
                                run=run, workdir=workdir)


# openocd interface cfgs whose discovery filters by VID/PID before opening; see convoy_safe
CONVOY_SAFE_CFGS = ('interface/jlink.cfg', 'interface/stlink.cfg', 'interface/ti-icdi.cfg')


# OpenOCD's messages for "the target's debug port did not answer". The probe is fine when
# these appear ("CMSIS-DAP: Interface ready" is still logged); the chip's debug clock is
# gone, which no probe-driven reset fixes -- the CMSIS-DAP probe has no nRESET line. Which
# message appears depends on DAP topology, not the board, so both are accepted for both
# chips; RESCUE_CFG below picks the rescue.
DAP_WEDGED = ('Failed to connect multidrop', 'Error connecting DP: cannot read IDR')

# How each RP target reaches its Rescue DP, keyed by the target cfg named in flasher args:
# (cfg substitution, pre args, post args). rp2040.cfg drives the Rescue DP behind a RESCUE
# flag and init/shutdowns itself; rp2350-rescue.cfg never shuts down, so it needs an
# explicit one or it sits in the server loop until the timeout.
RESCUE_CFG = {
    'target/rp2040.cfg': ('target/rp2040.cfg', ['-c', 'set RESCUE 1'], []),
    'target/rp2350.cfg': ('target/rp2350-rescue.cfg', [], ['-c', 'shutdown']),
}


def rescue_openocd(flasher: dict, flash_out: str = '', *, timeout, run=None) -> bool:
    """Power-on-reset a wedged RP2040/RP2350 through its Rescue DP, the one debug port not
    gated by the system clock (RP2040 datasheet 2.3.4.2): CDBGPWRUPREQ hard-resets the
    chip and the bootrom halts it ready to be flashed. Without it the board needs a
    physical replug -- the probe carries no reset line.

    No-op (False) unless this is an openocd RP flasher AND the flash output shows the wedge,
    so a flash that failed for any other reason still just retries. True when the rescue
    command exited 0, which is not proof the chip came back; the caller retries the flash
    afterwards either way."""
    if flasher['name'].lower() != 'openocd' or not any(m in flash_out for m in DAP_WEDGED):
        return False
    for cfg, (rescue_cfg, pre, post) in RESCUE_CFG.items():
        if cfg in flasher['args']:
            base = openocd_cmd_base({**flasher, 'args': ''})
            # in each word, as the pre-argv string replace did: a directory-qualified cfg keeps its directory
            args = [a.replace(cfg, rescue_cfg) for a in _args(flasher)]
            return _runner(run)([*base, *pre, *args, *post], timeout=timeout).returncode == 0
    return False


# openocd's own syntax: one or more complete "0xVVVV 0xPPPP" pairs. Validated rather than merely
# tested for truthiness -- `vid_pid` is a hand-edited roster field whose NAME is also used,
# with a different syntax, by tinyusb's tests.dev_attached, and convoy_safe reads a non-empty
# value as PROOF the flasher can deliver a recovery past a poisoned node. A typo there
# silently promised a recovery that openocd would reject at startup. Spaces and tabs only:
# the value goes verbatim into a Tcl `-c` command, where a newline starts another command.
_VID_PID_RE = re.compile(r'0x[0-9a-fA-F]{4}[ \t]+0x[0-9a-fA-F]{4}([ \t]+0x[0-9a-fA-F]{4}[ \t]+0x[0-9a-fA-F]{4})*')


def valid_vid_pid(value) -> bool:
    return isinstance(value, str) and bool(_VID_PID_RE.fullmatch(value.strip(' \t')))


def convoy_safe(flasher: dict) -> bool:
    """Can this flasher DELIVER a recovery while a usbfs node on the rig is poisoned?

    A post-HUNG reflash only helps if the flasher reaches its probe without opening the
    wedged node. Following shapes qualify:

    * openocd pinned with the roster's `vid_pid` -- the match is made from the cached
      descriptor and the loop `continue`s BEFORE libusb_open, so a foreign node is never
      opened. On 2026-08-12 it was the only flasher that still reached its probe.
    * esptool -- delivery is `-p <ttyACM>`, a named port; it never enumerates usbfs.
    * stlink (STM32_Programmer_CLI v2.23) -- its libSTLinkUSBDriver.so lists devices through
      libusb's lock-free sysfs walk (descriptors, busnum, devnum, speed, uevent) and opens
      only ST-Link (0483) nodes. strace on ci.lan 2026-10-07; with a testusb D-state on the
      wedged DUT's node, `--rst --go` returned 0 in 1.7 s and the holder reaped.
    * openocd over interface/jlink.cfg -- libjaylink opens SEGGER devices only (below).
    * openocd over interface/stlink.cfg or interface/ti-icdi.cfg -- discovery is VID/PID
      filtered by the cfg's own `adapter usb vid_pid` (below).
    * pyocd with one `vid_pid` pair -- run_pyocd.py makes pyusb drop every other device
      before pyocd's matcher opens it, and leaves only the CMSIS-DAP probe plugin.

    Everything else enumerates by OPENING nodes, would block in D state on the poisoned
    one, survive SIGKILL and become a second stray. JLinkExe cannot be pinned: selection
    is serial-only (-USB/-SelectEmuBySN, no VID/PID filter in J-Link Commander V9.66), and
    it reads the serial from sysfs: strace on ci.lan (2026-09-21) shows it opening only its
    own probe's usbfs node but reading `product`, `serial` and `bNumInterfaces` -- served
    under the device lock -- of every USB device on the host. So those boards can only
    become convoy-safe by moving to openocd (or behind usb_recover.sh's shield).

    Verified against openocd 0ce743125 (the rig's build), because the INVERSE is what
    bites: cmsis_dap_usb_bulk.c:107 skips on `id_filter && !id_match`, and `id_filter` is
    only `vids[0] || pids[0]` -- so without the pin nothing is skipped and every device on
    the bus is opened, which the code itself expects to mostly fail. Enumeration cannot
    block: libusb reads the `descriptors` sysfs attribute, and descriptors_read (v6.12.101
    drivers/usb/core/sysfs.c) is a memcpy from udev->rawdescriptors under no lock.

    The pin gates the BULK backend, which is the one that runs: `auto` tries usb_bulk ->
    hid -> tcp (cmsis_dap.c:62) and stops at the first that opens, so a CMSIS-DAP v2 probe
    never reaches the rest. It does NOT cover the HID fallback that a v1 probe or a failed
    bulk open takes -- cmsis_dap_usb_hid.c:91 calls hid_enumerate(0x0, 0x0), pin ignored,
    and filters afterwards, while hidapi's hidraw backend reads `manufacturer` and
    `product` for every HID device it lists (linux/hid.c:744), both usb_string_attr and so
    served under the device lock. A wedged DUT running hid_generic_inout,
    hid_boot_interface or hid_composite_freertos is a HID device and would stall that walk
    -- interruptibly, so it hangs rather than joining the D-state convoy and run_cmd's
    timeout ends it, but "never opens a foreign node" is true of the bulk path, not of
    every path openocd can take.
    """
    name = (flasher.get('name') or '').lower()
    if name in ('esptool', 'stlink'):
        return True
    if name == 'pyocd':
        return pyocd_vid_pid(flasher) is not None
    # EXACT, not startswith: rescue_openocd and a harness's flash dispatch both require the
    # exact name, so an 'openocd_wch'-style entry would pass this gate, reserve the
    # Rescue-DP legs, and then find no recovery path at all -- paying for a path that cannot
    # fire, which is the precise cost this gate exists to avoid.
    if name != 'openocd':
        return False
    if valid_vid_pid(flasher.get('vid_pid')):
        return True
    # openocd over the JLINK driver is safe WITHOUT a pin, and cannot use one: jlink.c
    # never reads adapter_usb_get_vids/pids (selection is adapter serial / usb address /
    # usb location), but libjaylink's discovery returns early unless idVendor == 0x1366 and
    # the PID is in its table, and only THEN calls libusb_open (discovery_usb.c). So it
    # never opens a foreign node. Verified against openocd 0ce743125 and libjaylink 0.4.0,
    # and by strace on ci.lan (2026-09-21): only SEGGER usbfs nodes opened, and the only
    # locking sysfs attribute read is the selected probe's bConfigurationValue.
    # stlink_usb.c:3403 and ti_icdi_usb.c:675 discover through jtag_libusb_open with the
    # cfg's vid_pid list, which skips every non-matching descriptor before libusb_open
    # (libusb_helper.c:170-176); both cfgs always set that list. Verified against
    # openocd 0ce743125.
    return any(cfg in (flasher.get('args') or '') for cfg in CONVOY_SAFE_CFGS)


# -------------------------------------------------------------
# esptool, lm4flash
# -------------------------------------------------------------
# esptool 5.2.0 and 5.3.1 emit cursor CSI codes off a TTY when TERM is xterm-like (logger.py); NO_COLOR
# turns them off, output only
ESPTOOL_ENV = {'NO_COLOR': '1'}


def esptool_argv(esptool: str, *args) -> list:
    """An esptool command line: `esptool` (an executable name or path, default 'esptool' on
    PATH, as a caller's pinned install) and its arguments, each one argv word."""
    if not isinstance(esptool, (str, os.PathLike)) or not str(esptool):
        raise ValueError(f'esptool {esptool!r}: want an executable name or path')
    return [os.fspath(esptool), *map(str, args)]


def flash_esptool(flasher: dict, *, chip: str, port: str, flash_args: str, cwd: str, timeout,
                  run=None, esptool: str = 'esptool') -> Operation:
    """`chip` is the build's IDF_TARGET, `flash_args` its flash_args file on one line, `cwd`
    the build directory the file's relative paths name, `port` the probe's serial device,
    `esptool` the executable run (esptool_argv), with ESPTOOL_ENV."""
    argv = esptool_argv(esptool, '--chip', chip, '-p', port, *_args(flasher), '--before=default_reset',
                        '--after=hard_reset', 'write_flash', *shlex.split(flash_args))
    return _once(run, argv, cwd=cwd, env=dict(ESPTOOL_ENV), timeout=timeout)


def flash_args_tokens(text) -> tuple:
    """(options, pairs) of an ESP-IDF flash_args text, split as a shell would: each leading
    option as (token, value), its value the next token unless the token carries `=` or none is
    left (None), then the "<offset> <file>" pairs, None unless whole pairs follow. shlex.split's
    errors propagate."""
    tokens, options = shlex.split(text), []
    while tokens and tokens[0].startswith('-'):
        opt = tokens.pop(0)
        options.append((opt, tokens.pop(0) if '=' not in opt and tokens else None))
    pairs = list(zip(tokens[::2], tokens[1::2])) if tokens and not len(tokens) % 2 else None
    return options, pairs


def esptool_input_problem(chip, flash_args, cwd) -> str:
    """Why flash_esptool could not flash with these inputs ('' when it could): `chip` one plain
    token, `flash_args` options then "<offset> <file>" pairs, every file present relative to
    `cwd`. For a caller to check before it locks a board, so a flash cannot fail on its inputs;
    finding the build's chip and flash_args is the caller's."""
    if not isinstance(chip, str) or not re.fullmatch(r'[A-Za-z0-9_-]+', chip):
        return f'chip {chip!r}: want one plain token (an IDF_TARGET)'
    if not isinstance(cwd, (str, os.PathLike)) or not os.path.isdir(cwd):
        return f'cwd {cwd!r} is not a directory'
    try:
        _, pairs = flash_args_tokens(flash_args)
    except (ValueError, AttributeError) as e:
        return f'flash_args: {e!r}'[:200]
    if pairs is None:
        return 'flash_args: want "<offset> <file>" pairs after the options'
    for offset, f in pairs:
        try:
            int(offset, 0)
        except ValueError:
            return f'flash_args: offset {offset!r}'
        if not (Path(cwd) / f).is_file():
            return f'flash_args names {f}, which is missing from {cwd}'
    return ''


def reset_esptool(flasher: dict, *, port: str, timeout, run=None, esptool: str = 'esptool') -> Operation:
    """A reset through the ROM loader: `chip_id` without the stub needs the loader's SYNC reply
    and writes no flash, then `--after=hard_reset` leaves the chip running. Exit 0 with `Chip
    was NOT reset` in the output (a port refusing RTS/DTR) is no reset; judging that is the
    caller's."""
    argv = esptool_argv(esptool, '-p', port, *_args(flasher), '--no-stub', '--before=default_reset',
                        '--after=hard_reset', 'chip_id')
    return _once(run, argv, env=dict(ESPTOOL_ENV), timeout=timeout)


def esptool_runtime(esptool: str = 'esptool', *, timeout: float = 60) -> dict:
    """The esptool a flash and its helper sessions run, checked with no device: `esptool` (a
    name on PATH or a path) resolved once to an absolute path, its interpreter by tool_python
    (the venv path kept, never resolved past its symlink), that interpreter importing one esptool
    package both from the sys.path the script gets (its resolved directory first, unless
    PYTHONSAFEPATH) and from the one a helper session beside this file gets, and the executable's
    `version` naming the same version. A relative or empty PYTHONPATH entry is refused: each run
    (a flash in its build dir, a session in its caller's) would resolve it in its own directory.
    {'exe', 'python', 'version', 'package'}; RuntimeError otherwise. Not cached: a caller keeps
    the identity for its run."""
    exe = shutil.which(esptool)
    if exe is None:
        raise RuntimeError(f'{esptool} is not on PATH')
    exe = os.path.abspath(exe)
    relative = [e for e in os.environ.get('PYTHONPATH', '').split(os.pathsep) if not os.path.isabs(e)]
    if os.environ.get('PYTHONPATH') and relative:
        raise RuntimeError(f'PYTHONPATH entries {relative!r} are not absolute: each esptool run would resolve them '
                           'in its own working directory')
    python = tool_python(exe)
    probe = ('import json, sys\n'
             'if not getattr(sys.flags, "safe_path", False): sys.path[0] = sys.argv[1]\n'
             'import esptool\n'
             'print(json.dumps([esptool.__version__, esptool.__file__]))')
    helpers = os.path.realpath(HERE)
    try:
        imported, helper = (subprocess.run([python, '-B', '-c', probe, path0], capture_output=True, text=True,
                                           timeout=timeout, cwd='/')
                            for path0 in (os.path.dirname(os.path.realpath(exe)), helpers))
        said = subprocess.run([exe, 'version'], capture_output=True, text=True, timeout=timeout, cwd='/',
                              env={**os.environ, **ESPTOOL_ENV})
    except (OSError, subprocess.TimeoutExpired) as e:
        raise RuntimeError(f'esptool runtime {exe}: {e}') from None

    def package_of(probed):
        try:
            version, package = json.loads(probed.stdout.strip().splitlines()[-1]) if probed.returncode == 0 else ()
        except (ValueError, IndexError, TypeError):
            return None
        return (version, package) if isinstance(version, str) and isinstance(package, str) else None
    runtime = package_of(imported)
    if runtime is None:
        raise RuntimeError(f'{python}, the interpreter of {exe}, imports no esptool package: '
                           f'{(imported.stderr or imported.stdout).strip()[-200:]!r}')
    version, package = runtime
    found = package_of(helper)
    if found != runtime:
        raise RuntimeError(f'{python} imports {found[1] if found else "no esptool package"} for helper sessions in '
                           f'{helpers}, not {package}, the package {exe} imports')
    if said.returncode or re.findall(r'^esptool v(\S+)$', said.stdout, re.M) != [version]:
        raise RuntimeError(f'{exe} version does not report {version}, the package its interpreter imports: '
                           f'rc {said.returncode}, {said.stdout.strip()[-200:]!r}')
    return {'exe': exe, 'python': python, 'version': version, 'package': package}


def esptool_problem(op) -> str:
    """Why a flash_esptool Operation did not flash, '' when it did: its last attempt exited 0
    and its output shows none of the known failures esptool exits 0 on."""
    problem, out = _last_exited(op, 'esptool')
    if problem:
        return problem
    out += '\n' + hil_process.cmd_stdout_text(op.attempts[-1].result.stderr)
    # besides the refused reset, esptool 5.2.0/5.3.1 write_flash warns and skips an empty image
    said = next((m for m in (*RESET_REFUTED['esptool'], "' is empty.") if m in out), None)
    return f'exit 0, but the output says {said!r}' if said else ''


SERIAL_BY_ID = '/dev/serial/by-id'
USJ_SERIAL = re.compile(r'[0-9A-F]{2}(?::[0-9A-F]{2}){5}')   # a chip's USB-Serial/JTAG serial: its MAC


def esptool_port(uid: str) -> str:
    """The serial device esptool opens for a roster uid (UID_RE, as every flasher's): a
    USB-Serial/JTAG serial (USJ_SERIAL) through hil_openocd_esp.usj_tty, loaded only then (it
    starts nothing); a USB-UART bridge serial through its /dev/serial/by-id link, exactly one,
    resolving to a character device. RuntimeError naming the candidates otherwise. Resolve it
    just before use, under the board's reservation: tty numbering moves with re-enumeration."""
    if not isinstance(uid, str) or not UID_RE.fullmatch(uid):
        raise RuntimeError(f'uid {uid!r}: want a probe serial')
    if USJ_SERIAL.fullmatch(uid):
        try:
            openocd_esp = hil_load.load_file('skills/hil/scripts/hil_openocd_esp.py')
        except ImportError as e:
            raise RuntimeError(f'USJ serial {uid}: {e}') from None
        return openocd_esp.usj_tty(uid)
    links = sorted(glob.glob(os.path.join(SERIAL_BY_ID, f'*_{glob.escape(uid)}-if*')))
    if len(links) != 1:
        raise RuntimeError(f'bridge serial {uid}: want one {SERIAL_BY_ID} link, found {links or "none"}')
    tty = os.path.realpath(links[0])
    try:
        is_chr = stat.S_ISCHR(os.stat(tty).st_mode)
    except OSError as e:
        raise RuntimeError(f'bridge serial {uid}: {links[0]} -> {tty}: {e}') from None
    if not is_chr:
        raise RuntimeError(f'bridge serial {uid}: {links[0]} -> {tty} is not a character device')
    return tty


def flash_lm4flash(flasher: dict, firmware: str, *, timeout, run=None) -> Operation:
    # TI Tiva-C / Stellaris ICDI: lightweight lm4flash, resets and runs after write
    return _once(run, ['lm4flash', '-s', _uid(flasher), *_args(flasher), str(firmware)], timeout=timeout)


# no reset_lm4flash: lm4flash has no reset-only mode; it resets+runs on flash


# -------------------------------------------------------------
# pyOCD
# -------------------------------------------------------------
def pyocd_vid_pid(flasher: dict) -> list | None:
    """[vid, pid] a pyocd entry filters discovery to, or None when it lacks a uid or exactly one
    valid `vid_pid` pair: unfiltered, pyocd opens every CMSIS-DAP-class device (run_pyocd.py)."""
    vid_pid = flasher.get('vid_pid')
    if not (flasher.get('uid') and valid_vid_pid(vid_pid) and len(vid_pid.split()) == 2):
        return None
    return vid_pid.split()


def tool_python(tool: str) -> str:
    """The interpreter of a Python console script `tool` (a name on PATH or a path), from its
    absolute python shebang: a helper that imports the tool's package runs under it."""
    exe = shutil.which(tool)
    if exe is None:
        raise RuntimeError(f'{tool} is not on PATH')
    try:
        with open(exe, 'rb') as f:
            line = f.readline().decode(errors='replace').strip()
    except OSError as e:
        raise RuntimeError(f'{exe}: {e}') from None
    py = line[2:] if line.startswith('#!') else ''
    if not (os.path.isabs(py) and os.path.basename(py).startswith('python') and os.access(py, os.X_OK)):
        raise RuntimeError(f'{exe}: want an absolute python shebang, got {line!r}')
    return py


def _pyocd_python() -> str:
    # the launcher must patch the pyocd it runs, so it runs under pyocd's own interpreter
    return tool_python('pyocd')


def _pyocd_argv(flasher: dict, verb: str, *extra: str, project_dir: str) -> list:
    uid, vid_pid = _uid(flasher), pyocd_vid_pid(flasher)
    if vid_pid is None:
        raise ValueError(f'pyocd flasher {flasher.get("uid")!r} needs a uid and one "vid_pid" pair')
    # -W: with no matching probe pyocd otherwise waits for one to be plugged in
    return [_pyocd_python(), PYOCD_LAUNCHER, '--project-dir', str(project_dir), *vid_pid, verb, '-W',
            '-u', f'cmsisdap:{uid}', *shlex.split(flasher.get('args', '')), *extra]


def flash_pyocd(flasher: dict, firmware: str, *, project_dir: str, timeout,
                run=None) -> Operation:
    """`project_dir` is where pyocd runs and resolves relative roster args (run_pyocd.py)."""
    return _once(run, _pyocd_argv(flasher, 'flash', os.path.abspath(firmware), project_dir=project_dir),
                 timeout=timeout)


def reset_pyocd(flasher: dict, *, project_dir: str, timeout, run=None) -> Operation:
    return _once(run, _pyocd_argv(flasher, 'reset', project_dir=project_dir), timeout=timeout)


# -------------------------------------------------------------
# Judging a reset
# -------------------------------------------------------------
# A reset_* that exits 0 shows the target answered only with one of these in its output, read
# from each tool's source at the rig's versions: JLinkExe V9.78 core identification,
# STM32CubeProgrammer 2.23 device info, openocd 0ce743125 DP/core examination. A backend not
# listed: its exit status alone says whether the target answered.
RESET_CONTACT = {
    'stlink': ('Device ID',),
    'openocd': ('Examination succeed', 'Examined RISC-V core', 'processor detected', 'SWD DPIDR'),
}
# Output showing no reset although the tool exited 0: openocd only warns on a failed examine and
# only logs a failed event hook (a reset-assert hook then replaces the reset); pyocd only logs an
# inaccessible core or an invalid reset method; esptool exits 0 when the port refuses RTS/DTR.
RESET_REFUTED = {
    'openocd': ('examination failed', 'Execution of event'),
    'pyocd': ('is not accessible after reset', 'Invalid reset method'),
    'esptool': ('Chip was NOT reset',),
}


def reset_problem(name: str, op) -> str:
    """Why the `name` backend's reset Operation `op`, which exited 0, does not show the target
    answering and resetting ('' when it does). A non-zero exit is the caller's to judge. J-Link
    is jlink_problem's: its `halt` completing is the target answering."""
    name = name.lower()
    if name == 'jlink':
        return jlink_problem(op)
    out = hil_process.cmd_stdout_text(op.stdout) + '\n' + hil_process.cmd_stdout_text(op.stderr)
    if said := next((m for m in RESET_REFUTED.get(name, ()) if m in out), None):
        return f'exit 0, but the output says {said!r}'
    if name in RESET_CONTACT and not any(m in out for m in RESET_CONTACT[name]):
        return 'exit 0, but no line shows the target answering'
    return ''
