#!/usr/bin/env python3
"""The Arduino HIL harness mechanics a library's HIL contract runs (agentrc `arduino` skill). Linux only.

A library's `hil_test.py` is its profile: it defines the names below, keeps its own entry point and calls
run(profile, argv), loading this file from the pinned agentrc (its `agentrc_pin.py`). One profile per process
(bind()).

  ROOT                  the library checkout (Path)
  BUILD                 the library's build profile (its check_build module; agentrc arduino_build.py)
  TESTS                 {test: the sketch name (its dir's basename) it runs}
  ROLE_TESTS            {role: (tests a board of that role may run)}
  RESTORE_TEST          {role: the test that checks the role's restoration sketch (the first HIL sketch)}
  TEST_FUNCS            {device test: fn(dev, nonce, deadline, tracker, run_dir, board) -> (ok, detail)}
  REPORT_SCHEMA         the run report's schema name
  check_board(name, entry, path)   optional: the library's roster checks; raise Refused
  step_host(name, board, image, test, run_dir, tracker)  required when a roster board has role host

The usage, exits and report are the library's HIL SKILL.md; the shared rules are agentrc's `hil` and `arduino`
skills. Locks and the board's cleanup log are agentrc's `hil_lock.py`, the same for every library, so two libraries
on one physical board exclude each other; a lock ends with its run or session command, a failed cleanup logged
(agentrc#464). Each invocation claims the board before its first tool (agentrc#467, CLAIM_KIND), so a lock alone is
no permission to touch the probe. PENDING_KIND names the pending-session record an older engine kept: reported, never a
refusal.
"""

import argparse
import contextlib
import fcntl
import functools
import glob
import hashlib
import importlib.util
import json
import os
import re
import secrets
import shlex
import shutil
import signal
import socket
import subprocess
import sys
import time
from pathlib import Path

HERE = os.path.dirname(os.path.abspath(__file__))
LOCK_SCRIPT = 'skills/hil/scripts/hil_lock.py'
SCRIPTS = (LOCK_SCRIPT, 'skills/hil/scripts/hil_flash.py', 'skills/hil/scripts/hil_process.py',
           'skills/hil/scripts/hil_report_core.py', 'skills/hil/scripts/usb_observe.py',
           'skills/hil/scripts/hil_openocd_esp.py')
FILES = ('skills/arduino/scripts/arduino_build.py', 'skills/hil/boards.json', 'skills/hil/scripts/esptool_session.py',
         'skills/hil/scripts/hil_boards.py', 'skills/hil/scripts/hil_flash.py', 'skills/hil/scripts/hil_load.py',
         'skills/hil/scripts/hil_lock.py', 'skills/hil/scripts/hil_openocd_esp.py',
         'skills/hil/scripts/hil_openocd_esp_receipt.py', 'skills/hil/scripts/hil_process.py',
         'skills/hil/scripts/hil_report_core.py', 'skills/hil/scripts/hil_report_notice.py',
         'skills/hil/scripts/pyocd/run_pyocd.py', 'skills/hil/scripts/pyocd/user_script.py',
         'skills/hil/scripts/usb_observe.py', 'skills/target-debug/scripts/esp_session.py')
DETACH_WAIT = 5
FLASH_TIMEOUT, VERIFY_TIMEOUT, ENUM_TIMEOUT, TEST_TIMEOUT, SETTLE = 120, 60, 20, 30, 2
CONSOLE_MAX = 120   # one console capture, seconds
BOARD_BUDGET = 900
EXEC_MAX = 600   # one session exec or gdb child
NOT_HELD = 3   # a readable holder record shows no live hold under the caller's reason
RELEASE_FAILED = 5   # restore --release: the hold may still stand
RESTORE_FAILED = 6   # restore --release: the restore failed or was refused, the hold released
GDB_SERVER = ['-c', 'gdb port pipe', '-c', 'gdb memory_map disable', '-c', 'gdb breakpoint_override hard']
# An RP2040 SMP resume restores core1 only after each core is polled (target-debug gdb.md, verified on the Pico)
GDB_DETACH_RESUME = {'target/rp2040.cfg': '{ foreach u [target names] { $u arp_poll }; resume }'}   # by debug cfg
# Espressif's gdb per chip whose datasheet names its core, Xtensa or RISC-V (hil-farm-setup farm.toml installs them):
# gdb-multiarch has no Xtensa ESP target
ESP_GDB = {'esp32': 'xtensa-esp32-elf-gdb', 'esp32s2': 'xtensa-esp32s2-elf-gdb', 'esp32s3': 'xtensa-esp32s3-elf-gdb',
           'esp32c3': 'riscv32-esp-elf-gdb', 'esp32c6': 'riscv32-esp-elf-gdb', 'esp32p4': 'riscv32-esp-elf-gdb'}
# Probe commands, by the openocd flasher's target cfg, that take the DUT off the bus before a flash or console reset.
# A probe reset leaves the RP2040/RP2350 USB controller out of reset, and a USB stack's init resets and reconnects it
# within microseconds, so without this the host never sees the new image arrive: clear SIE_CTRL.PULLUP_EN
# (USBCTRL_REGS_BASE 0x50110000 + 0x4c, bit 16) through its atomic clear alias (+0x3000). RP2040 datasheet 2.1.2,
# 4.1.4 (SIE_CTRL); RP2350 datasheet 2.1.3, 12.7.5 (same layout).
DETACH = {'target/rp2040.cfg': ['mww 0x5011304c 0x00010000'], 'target/rp2350.cfg': ['mww 0x5011304c 0x00010000']}
REAP = 10   # agentrc hil_process.REAP_GRACE, per tracked child
STEP_MAX = VERIFY_TIMEOUT + DETACH_WAIT + FLASH_TIMEOUT + VERIFY_TIMEOUT + ENUM_TIMEOUT + TEST_TIMEOUT + \
    4 * (REAP + SETTLE)
CLEANUP_RESERVE = STEP_MAX   # the restore is one more such step
BASE_MAX = 2 * VERIFY_TIMEOUT   # a base image's check and rewrite before a flash (ensure_base), added per step
PENDING_KIND = 'arduino-hil'   # an older engine's pending-session record, every Arduino library's
CLAIM_KIND = 'arduino-hil-claim'   # the invocations claiming a board (agentrc#467), every Arduino library's
CLAIM_SETTLE = 30   # s an earlier invocation's still-running children get to end before an attempt gives the board up


def _hil_load():
    """hil_load.py from this agentrc, the process's one `agentrc_hil_load`."""
    spec = importlib.util.spec_from_file_location('agentrc_hil_load', os.path.join(HERE, '..', '..', 'hil',
                                                                                    'scripts', 'hil_load.py'))
    if spec.name not in sys.modules:
        mod = importlib.util.module_from_spec(spec)
        spec.loader.exec_module(mod)
        sys.modules.setdefault(spec.name, mod)
    return sys.modules[spec.name].load(spec.name, os.path.realpath(spec.origin))


hil_load = _hil_load()
hil_load.require(__file__, FILES)
B = hil_load.load_file('skills/arduino/scripts/arduino_build.py')
A = argparse.Namespace(lock=hil_load.load_file(SCRIPTS[0]), flash=hil_load.load_file(SCRIPTS[1]),
                       process=hil_load.load_file(SCRIPTS[2]), report=hil_load.load_file(SCRIPTS[3]),
                       usb=hil_load.load_file(SCRIPTS[4]), esp=lambda: hil_load.load_file(SCRIPTS[5]),
                       root=Path(hil_load.ROOT))
P = None   # the bound profile


def bind(profile):
    """Bind the library's HIL profile (and its BUILD profile to arduino_build); a second, different one is refused."""
    global P
    if P is not None and P is not profile:
        raise RuntimeError(f'arduino_hil is bound to another profile ({P.ROOT}), not {profile.ROOT}\'s')
    problems = [f'RESTORE_TEST {r!r} runs {profile.TESTS.get(t)!r}, not the first HIL sketch {first!r}'
                for r in profile.ROLE_TESTS for t in [profile.RESTORE_TEST.get(r)]
                for first in [Path(profile.BUILD.HIL_SKETCHES[r][0]).name] if profile.TESTS.get(t) != first]
    if set(profile.TESTS) != {t for tests in profile.ROLE_TESTS.values() for t in tests}:
        problems.append('TESTS and the union of ROLE_TESTS name different tests')
    if set(profile.TEST_FUNCS) != set(profile.ROLE_TESTS.get('device', ())):
        problems.append('TEST_FUNCS and ROLE_TESTS["device"] name different tests')
    if problems:
        raise RuntimeError(f'{profile.ROOT}: profile tables disagree: {"; ".join(problems)}')
    B.bind(profile.BUILD)
    P = profile
    return sys.modules[__name__]


class Refused(Exception):
    """Refused before any hardware access."""


class NotHeld(Refused):
    """The board's holder record is readable and is no live hold under the caller's reason: exit NOT_HELD."""


class InUse(Refused):
    """Another invocation on the board is alive, or an earlier one's children still run: not run. `found` is the
    board's open failures, those it logged included."""

    def __init__(self, msg, found):
        super().__init__(msg)
        self.found = found


def refusal_report(e):
    """The report keys a refusal carries: an InUse's open failures, the ones its claim logged included."""
    return A.lock.failure_report(e.found) if isinstance(e, InUse) else {}


class Unclean(Exception):
    """A child's end is not proven: nothing more may touch the board."""


def emit(obj):
    print(json.dumps(obj), flush=True)


# ---- roster, staged manifest, build record

def role(board):
    return B.role(board)


def restore_sketch(name):
    return Path(B.hil_sketches(name)[0]).name


def load_roster(path):
    try:
        boards = B.rig_roster(path)
    except ValueError as e:
        raise Refused(str(e))
    for name, b in boards.items():
        flasher = b['flasher']['name']
        if flasher not in B.IMAGE_KIND:
            raise Refused(f'roster {path}: {name}: flasher must be one of {sorted(B.IMAGE_KIND)}')
        if (flasher == 'esptool') != ('chip' in B.P.BOARDS[name]):
            raise Refused(f'roster {path}: {name}: esptool flashes exactly the boards with an ESP chip profile')
        wrong = set(b.get('arduino', {}).get('tests', [])) - set(P.ROLE_TESTS[role(name)])
        if wrong:
            raise Refused(f'roster {path}: {name}: tests {sorted(wrong)} are not {role(name)} tests')
        if role(name) == 'host' and not getattr(P, 'step_host', None):
            raise Refused(f'roster {path}: {name}: a host board, and this harness has no host step')
        if 'usb_port' in b and not A.usb.BUSPORT_RE.fullmatch(b['usb_port']):
            raise Refused(f'roster {path}: {name}: usb_port {b["usb_port"]!r} is not a sysfs bus port')
        if b.get('no_serial') and 'usb_port' not in b:
            raise Refused(f'roster {path}: {name}: no_serial needs usb_port, the only identity left')
        if flasher == 'esptool' and 'usb_port' not in b:
            # Arduino on ESP32 enumerates without an iSerial: the DUT is the device at its port
            raise Refused(f'roster {path}: {name}: an esptool board needs usb_port')
        dbg = b.get('debug', {})
        if not isinstance(dbg, dict):
            raise Refused(f'roster {path}: {name}: debug must be an object, or absent for no GDB route')
        tool = dbg.get('tool', 'openocd')
        if tool not in ('openocd', 'openocd-esp32') or (tool == 'openocd-esp32') != ('uid' in dbg):
            raise Refused(f'roster {path}: {name}: debug.tool is openocd, or openocd-esp32 with the debug '
                          f'probe\'s debug.uid')
        chip = B.P.BOARDS[name].get('chip')
        if tool == 'openocd-esp32' and ('vid_pid' in dbg or dbg.get('openocd') != f'-f board/{chip}-builtin.cfg' or
                                        not A.usb.BUSPORT_RE.fullmatch(dbg.get('usb_port', ''))
                                        or len(dbg['usb_port']) > A.esp().USB_LOCATION_MAX):
            # the USB-Serial/JTAG is pinned by its bus-port, never by vid_pid
            raise Refused(f'roster {path}: {name}: an openocd-esp32 debug route is "-f board/{chip}-builtin.cfg", '
                          f'debug.uid and debug.usb_port (its bus-port), with no vid_pid')
        if getattr(P, 'check_board', None):
            P.check_board(name, b, path)
    return boards


def selection(roster, boards, tests):
    """{board: its tests} for -b/-t: the roster's battery, or with -t the named tests of each board's role, boards with
    none dropped; Refused when nothing is left."""
    names = boards or list(roster)
    unknown = [b for b in names if b not in roster]
    if unknown:
        raise Refused(f'not in the roster: {unknown}')
    picked = {b: [t for t in tests if t in P.ROLE_TESTS[role(b)]] if tests else
              roster[b].get('arduino', {}).get('tests', []) for b in names}
    picked = {b: ts for b, ts in picked.items() if ts}
    if not picked:
        raise Refused(f'no selected board runs any of {tests or "its tests"}')
    return picked


def verify_staged(manifest_path):
    """A rig tree staged by hil_remote.py: every staged file must hash as the manifest written on the dev PC says."""
    m = json.loads(Path(manifest_path).read_text())
    bad = [rel for rel, digest in m['files'].items()
           if not (P.ROOT / rel).is_file() or B.sha256(P.ROOT / rel) != digest]
    if bad:
        raise Refused(f'staged files differ from the manifest: {bad[:5]}')
    return m


def git_identity():
    def git(*a):
        return subprocess.run(['git', '-C', str(P.ROOT), *a], capture_output=True, text=True).stdout
    return git('rev-parse', 'HEAD').strip(), B.worktree_status().strip()


def record_problems(rec, board, head, experimental_ok, where):
    """Why a build record is not admissible for `board` built at `head` (None: any head), its images aside; [] when
    it is. hil_remote.py asks the same before staging."""
    if rec.get('kind') != 'arduino-hil-build' or rec.get('version') != B.SCHEMA:
        return [f'not a version {B.SCHEMA} arduino-hil-build record: rebuild it with the build contract\'s --record']
    why = []
    if rec.get('library') != B.P.LIBRARY_NAME:
        why.append(f'it is a {rec.get("library")} record, this harness is {B.P.LIBRARY_NAME}\'s')
    current = B.contract_identity()
    changed = sorted(f for f in {*rec.get('contract', {}), *current['contract']}
                     if rec.get('contract', {}).get(f) != current['contract'].get(f))
    if changed:
        why.append(f'the contract files {changed} differ from the ones it was built with: rebuild it')
    drift = B.agentrc_drift(current['agentrc']['files'])
    if drift:
        why.append(f'agentrc changed since this harness loaded it: {drift}; rerun')
    built = rec.get('agentrc') or {}
    if built.get('files') != current['agentrc']['files']:   # the commit is provenance only
        why.append(f'it was built with other agentrc files (agentrc {str(built.get("commit"))[:12]}) than this '
                   f'harness loads (agentrc {current["agentrc"]["commit"][:12]}): rebuild it')
    if rec['board'] != board:
        why.append(f'it is for {rec["board"]}, not {board}')
    if rec['experimental'] and not experimental_ok:
        why.append('it is experimental: the battery and restore take clean records only')
    want, have = sorted(B.P.BOARDS.get(board, {}).get('base') or ()), sorted((rec.get('base') or {}).get('files', {}))
    if want != have:
        why.append(f'its base image {have} is not the profile\'s {want}: rebuild it')
    if head is not None and rec['source']['head'] != head:
        why.append(f'it was built at {rec["source"]["head"][:12]}, the {where} is at {head[:12]}: rebuild it')
    return why


def admit(record_path, board, roster_path, run_dir, *, manifest=None, experimental_ok=False, base=None):
    """The record's images (checkout-relative paths, under `base` for a staged bundle) copied into the run dir once
    they hash as recorded, or Refused."""
    try:
        rec = json.loads(Path(record_path).read_text())
    except (OSError, ValueError) as e:
        raise Refused(f'build record {record_path}: {e}')
    head, status, where = None, '', 'checkout'
    if manifest is not None:
        head, where = manifest['head'], 'staged tree'
    else:
        head, status = git_identity()
        if rec.get('experimental'):   # same HEAD, a dirty tree allowed: its diff is saved beside the record
            status = ''
    why = record_problems(rec, board, head, experimental_ok, where)
    if why:
        raise Refused(f'{record_path}: {"; ".join(why)}')
    if status:
        raise Refused(f'the checkout has changes since the record: {status.splitlines()[:5]}')
    if B.sha256(roster_path) != rec['roster']['sha256']:
        raise Refused(f'{roster_path} changed since the record was built: rebuild it')
    flasher = load_roster(roster_path)[board]['flasher']['name']
    want = B.IMAGE_KIND[flasher]
    if rec.get('role', 'device') != role(board):
        raise Refused(f'{record_path}: a {rec.get("role", "device")} record, {board} is a {role(board)} board')
    images = {}
    for name, s in rec['sketches'].items():
        if s.get('image') != want:
            raise Refused(f'{record_path}: {name} records a {s.get("image")} image, a {flasher} board flashes the '
                          f'{want}: rebuild against this roster')
        dest = Path(run_dir) / 'images' / name
        for rel, digest in s['sha256'].items():
            pinned_copy(Path(base or P.ROOT) / rel, digest, dest / Path(rel).name, rel)
        images[name] = {'elf': str(dest / Path(s['elf']).name),
                        'hex': str(dest / Path(s['hex']).name) if s.get('hex') else None,
                        'vidPid': s['vidPid'], 'sha256': s['sha256'], 'dir': str(dest),
                        'fqbn': s.get('fqbn', rec['fqbn']),
                        'segments': s.get('segments'), 'chip': rec.get('chip')}
    if rec.get('base'):
        base_image = admit_base(rec, board, Path(base or P.ROOT), Path(run_dir))
        for image in images.values():
            image['base'] = base_image
    return rec, images


def pinned_copy(src, digest, dest, name):
    """Copy src to dest once its bytes hash as `digest`, so the copy is the bytes checked; Refused naming `name`."""
    data = src.read_bytes() if src.is_file() else None
    if data is None or hashlib.sha256(data).hexdigest() != digest:
        raise Refused(f'{name} is missing or changed since the record')
    dest.parent.mkdir(parents=True, exist_ok=True)
    dest.write_bytes(data)


def admit_base(rec, board, root, run_dir):
    """The record's base image prepared in run_dir/base: {'hex' (its runs outside the excluded ranges), 'bins'
    [(start, file)], 'state' (set by ensure_base)}, parsed from the run dir's copies of its files once those hash as
    recorded and their runs are the recorded segments; Refused otherwise."""
    out = run_dir / 'base'
    for rel_path, digest in rec['base']['files'].items():
        pinned_copy(root / rel_path, digest, out / 'files' / rel_path, f'base image {rel_path}')
    runs, why = B.base_runs(board, root=out / 'files')
    if why or runs is None:
        raise Refused(f'base image: {why or "the profile names none"}')
    if [[s, s + len(d)] for s, d in runs] != rec['base']['segments']:
        raise Refused('base image runs differ from the record\'s segments: rebuild it')
    B.write_hex(runs, out / 'base.hex')
    return {'hex': str(out / 'base.hex'), 'bins': write_bins(runs, out / 'base'), 'state': None}


def write_bins(runs, prefix):
    """[(start, file)]: each run as <prefix>-<start>.bin, what a verifybin compares."""
    bins = []
    for start, data in runs:
        path = Path(f'{prefix}-{start:08x}.bin')
        path.write_bytes(data)
        bins.append((start, str(path)))
    return bins


# ---- tracked children and flashing

class Tracker:
    """Every hardware child runs through agentrc's run_tracked; one not proven ended stops the board."""

    def __init__(self, run_dir):
        self.state_dir = A.process.state_dir(str(run_dir), 'tracked')
        self.receipts, self.pending, self.unclean, self.log = [], [], '', Path(run_dir) / 'hil.log'

    def run(self, argv, timeout, **kw):
        self.check()
        try:
            r = A.process.run_tracked(argv, timeout=timeout, settle=SETTLE, state_dir=self.state_dir, **kw)
        except BaseException as e:
            self._note(getattr(e, 'cleanup', 'unknown'), getattr(e, 'receipt', None), argv)
            raise
        self._note(getattr(r, 'cleanup', 'unknown'), getattr(r, 'receipt', None), argv)
        with open(self.log, 'a') as f:
            f.write(f'$ {" ".join(map(str, argv))}\n[exit {r.returncode}]\n{_text(r.stdout)}{_text(r.stderr)}\n')
        return r

    def _note(self, cleanup, receipt, argv):
        if receipt is not None:
            self.receipts.append(receipt)
        if cleanup != 'done':
            self.unclean = f'{Path(str(argv[0])).name}: cleanup {cleanup}'
            # no receipt (the supervisor died): a session nothing can recheck, so its logged failure stays open
            self.pending.append({'receipt': receipt if receipt is not None else
                                 {'unrecheckable': [str(a) for a in argv], 'at': time.time()}})

    def check(self):
        if self.unclean:
            raise Unclean(self.unclean)


def _text(b):
    return b.decode(errors='replace') if isinstance(b, bytes) else (b or '')


def jlink_device(flasher):
    words = shlex.split(flasher.get('args') or '')   # as hil_flash splits it for the flash session
    return words[words.index('-device') + 1]


@functools.cache
def esptool_exe():
    return A.flash.esptool_runtime()['exe']   # checked once per invocation, as its docstring asks of a caller


def esptool_setup(flasher):
    """(the bridge tty, the esptool executable), the tty resolved afresh under the board's reservation, or
    RuntimeError."""
    return A.flash.esptool_port(flasher['uid']), esptool_exe()


def flash_esptool(flasher, image, tracker):
    """The build's flash_args through esptool write_flash, whose per-image hash check is the verify."""
    flash_args = ' '.join(Path(image['dir'], 'flash_args').read_text().split())
    why = A.flash.esptool_input_problem(image['chip'], flash_args, image['dir'])
    if why:
        return f'esptool inputs: {why}'
    try:
        port, exe = esptool_setup(flasher)
    except RuntimeError as e:
        return f'esptool: {e}'
    op = A.flash.flash_esptool(flasher, chip=image['chip'], port=port, flash_args=flash_args, cwd=image['dir'],
                               timeout=FLASH_TIMEOUT, run=tracker.run, esptool=exe)
    tracker.check()
    why = A.flash.esptool_problem(op)
    if why:
        return f'esptool flash: {why}'
    verified = (_text(op.stdout) + _text(op.stderr)).count('Hash of data verified.')
    want = len(image['segments'])
    return '' if verified == want else f'esptool verified {verified} of {want} images'


def flash(flasher, image, run_dir, tracker):
    """Program and verify one image. Returns '' or why it failed; raises Unclean when a child outlived it."""
    if flasher['name'] == 'esptool':
        return flash_esptool(flasher, image, tracker)
    if flasher['name'] == 'openocd':
        op = A.flash.flash_openocd(flasher, image['elf'], timeout=FLASH_TIMEOUT, run=tracker.run)
        tracker.check()
        out = _text(op.stdout) + _text(op.stderr)
        if op.returncode:
            return f'openocd exit {op.returncode}: {out.strip().splitlines()[-1:]}'
        need = ['** Programming Finished **'] + (['** Verified OK **'] if flasher.get('verify', True) else [])
        missing = [n for n in need if n not in out]
        return f'openocd transcript lacks {missing}' if missing else ''
    if not image['hex']:
        return 'no HEX for a J-Link flash'
    if image.get('base'):
        why = ensure_base(flasher, image['base'], run_dir, tracker)
        if why:
            return why
    op = A.flash.flash_jlink(flasher, image['hex'], timeout=FLASH_TIMEOUT, run=tracker.run, workdir=str(run_dir))
    tracker.check()
    why = A.flash.jlink_problem(op)
    if why:
        return f'J-Link flash: {why}'
    bins = write_bins(B.hex_runs(image['hex']), Path(run_dir) / f'verify-{Path(image["hex"]).stem}')
    op = jlink_session(flasher, [f'verifybin {path},{addr:#x}' for addr, path in bins] + ['r', 'go'], run_dir,
                       tracker)
    why = A.flash.jlink_problem(op)
    return f'J-Link verify: {why}' if why else ''


def jlink_session(flasher, cmds, run_dir, tracker):
    op = A.flash.run_jlink_commands(flasher, cmds, interface='SWD', speed=4000, autoconnect=True,
                                    device=jlink_device(flasher), timeout=VERIFY_TIMEOUT, run=tracker.run,
                                    workdir=str(run_dir))
    tracker.check()
    return op


def ensure_base(flasher, base, run_dir, tracker):
    """Check the board holds its base image (each run verified in place) and rewrite it only on a proven
    difference, with no reset or go after it (the sketch's flash resets the target); '' or why it failed. A session
    that proves nothing (a read failure, no connection) writes nothing. `state` names this check's outcome, None until
    it has one (the dict is shared by every image and step)."""
    base['state'] = None
    verify = [f'verifybin {path},{start:#x}' for start, path in base['bins']]
    op = jlink_session(flasher, ['halt', *verify], run_dir, tracker)
    why = A.flash.jlink_problem(op)
    if not why:
        base['state'] = 'matched'
        return ''
    differs = A.flash.jlink_verify_differs(op)
    if not differs:
        return f'base check: {why} (nothing written)'
    at = differs.rsplit(',', 1)[1]
    # loadfile resets the target unless told noreset, which needs the address its .hex form ignores (J-Link
    # Commander, LoadFile)
    op = jlink_session(flasher, ['halt', f'loadfile {base["hex"]} 0 noreset', *verify], run_dir, tracker)
    why = A.flash.jlink_problem(op)
    if why:
        return f'base rewrite after the run at {at} differed: {why}'
    base['state'] = f'rewritten: the run at {at} differed'
    return ''


def detach(name, board, vid_pid, tracker):
    """Take the DUT off the bus through the probe, where DETACH names the openocd flasher's target cfg; evidence
    text. A failed write is evidence, not a failure: a firmware without USB has nothing to detach, and the enumeration
    check after the flash decides. vid_pid None (no record, as for a console reset): the DUT is whatever carries its
    serial."""
    if board['flasher']['name'] != 'openocd' or role(name) == 'host':
        return ''
    cfgs = shlex.split(board['flasher'].get('args', ''))
    cmds = next((c for cfg, c in DETACH.items() if cfg in cfgs), None)
    if not cmds:
        return ''
    op = A.flash.run_openocd_commands(board['flasher'], cmds, timeout=VERIFY_TIMEOUT, run=tracker.run)
    tracker.check()
    out = _text(op.stdout) + _text(op.stderr)
    if op.returncode or 'Error' in out:
        return f'detach session failed (exit {op.returncode})'
    deadline = time.monotonic() + DETACH_WAIT
    while time.monotonic() < deadline:
        if not (dut_devices(vid_pid, board) if vid_pid else A.usb.usb_scan(serial=board['uid'])):
            return 'detached'
        time.sleep(0.2)
    return f'still enumerated {DETACH_WAIT}s after the detach'


def flash_and_enumerate(name, board, image, run_dir, tracker):
    """Note the DUT's enumeration, detach, flash, then wait for it to enumerate anew: (why the flash failed or '',
    detach evidence, the new device or None)."""
    before = enumeration_of(image['vidPid'], board)
    detached = detach(name, board, image['vidPid'], tracker)
    why = flash(board['flasher'], image, run_dir, tracker)
    if why:
        return why, detached, None
    flashed = time.monotonic()
    dev = wait_enumerated(image['vidPid'], board, before)
    if dev:   # seconds from the flash's end, the 1 s stability hold included
        dev = {**dev, 'after': round(time.monotonic() - flashed, 1)}
    return '', detached, dev


def reset(flasher, tracker):
    if flasher['name'] == 'esptool':
        try:
            port, exe = esptool_setup(flasher)
        except RuntimeError as e:
            return f'esptool: {e}'
        op = A.flash.reset_esptool(flasher, port=port, timeout=VERIFY_TIMEOUT, run=tracker.run, esptool=exe)
        tracker.check()
        return A.flash.esptool_problem(op)
    if flasher['name'] == 'openocd':
        op = A.flash.reset_openocd(flasher, timeout=VERIFY_TIMEOUT, run=tracker.run)
        tracker.check()
        return f'openocd reset exit {op.returncode}' if op.returncode else A.flash.reset_problem('openocd', op)
    op = A.flash.reset_jlink(flasher, timeout=VERIFY_TIMEOUT, run=tracker.run)
    tracker.check()
    return A.flash.jlink_problem(op)


# ---- USB

def scan(vid_pid, serial=None):
    return A.usb.usb_scan(vid_pid=tuple(vid_pid.split(':')), serial=serial)


def devices(vid_pid):
    return A.usb.usb_devices(vid_pid=tuple(vid_pid.split(':')))


def no_serial(dev):
    """No cached sysfs `serial` within one enumeration: Arduino on ESP32 points iSerialNumber at an empty string. A
    failed serial read leaves none too, so this picks the candidate; the test run on it is the evidence."""
    gone = not (Path(dev['dir']) / 'serial').exists()
    return gone and A.usb.enumeration_id(dev['dir']) == dev['ino']


def dut_devices(vid_pid, board):
    """The DUT's enumerations with this VID:PID. Two independent roster settings: `no_serial` (the firmware reports
    none: ESP32 under Arduino) picks the serial-less device at `usb_port` (an ESP32-S3's ROM USB-Serial/JTAG,
    303a:1001 with its MAC as serial, shows at the same port while esptool runs); otherwise the serial must be the
    roster uid, and a `usb_port` must hold it too."""
    if board.get('no_serial'):
        return [d for d in devices(vid_pid) if d['busport'] == board['usb_port'] and no_serial(d)]
    found = scan(vid_pid, board['uid'])
    return [d for d in found if d['busport'] == board['usb_port']] if 'usb_port' in board else found


def dut_name(board):
    if board.get('no_serial'):
        return f'at {board["usb_port"]} with no serial'
    return f'with serial {board["uid"]}' + (f' at {board["usb_port"]}' if 'usb_port' in board else '')


def wait_enumerated(vid_pid, board, before, timeout=ENUM_TIMEOUT):
    """The DUT with this VID:PID once it enumerated anew (an enumeration id other than `before`) and stayed for a
    second, or None."""
    deadline, seen, since = time.monotonic() + timeout, None, 0
    while time.monotonic() < deadline:
        devs = [d for d in dut_devices(vid_pid, board) if d['ino'] != before]
        if len(devs) == 1:
            if seen and seen['ino'] == devs[0]['ino'] and time.monotonic() - since >= 1:
                return devs[0]
            if not seen or seen['ino'] != devs[0]['ino']:
                seen, since = devs[0], time.monotonic()
        else:
            seen = None
        time.sleep(0.25)
    return None


def enumeration_of(vid_pid, board):
    devs = dut_devices(vid_pid, board)
    return devs[0]['ino'] if len(devs) == 1 else None


def node(dev, pattern, deadline):
    """/dev/<name> of an interface child of `dev` matching the sysfs glob, once it is there and accessible."""
    while time.monotonic() < deadline:
        hits = sorted(glob.glob(os.path.join(dev['dir'], pattern)))
        if hits:
            path = '/dev/' + os.path.basename(hits[0])
            if os.access(path, os.R_OK | os.W_OK):
                return path
        time.sleep(0.2)
    raise TimeoutError(f'no accessible {pattern} under {dev["busport"]} (missing node or permission)')


def openocd_probes(flasher):
    """The openocd probe's enumerations on this host by its serial, pinned to the roster vid_pid ("0xVVVV 0xPPPP")."""
    vid_pid = tuple(v[2:] for v in flasher['vid_pid'].split()) if 'vid_pid' in flasher else None
    return A.usb.usb_scan(vid_pid=vid_pid, serial=flasher['uid'])


def read_console(port, deadline, buf, times, t0, done=None):
    """Append what `port` delivers to `buf` until `deadline` or done(buf); one {t, offset, len} per chunk in `times`,
    t its capture time after t0 (not the wire time: the bridge and the kernel buffer)."""
    while time.monotonic() < deadline and not (done and done(buf)):
        chunk = port.read(4096)
        if chunk:
            times.append({'t': round(time.monotonic() - t0, 3), 'offset': len(buf), 'len': len(chunk)})
            buf += chunk


def pulse_en(port):
    """esptool's hard reset on the open bridge (esptool reset.py HardReset): RTS asserted with DTR released holds EN
    low, 100 ms (well over the S3's and P4's minimum reset pulse); releasing both lets the chip boot from flash. The
    open asserted both lines, which keeps EN high."""
    try:
        port.dtr = False
        port.rts = True
        time.sleep(0.1)
    finally:
        port.rts = False


def console_tty(board, deadline):
    """The board's console: its CMSIS-DAP probe's VCOM (the target's UART0, Serial1), or an esptool board's
    USB-UART bridge (UART0). TimeoutError or RuntimeError naming why not."""
    fl = board['flasher']
    if fl['name'] == 'esptool':
        return A.flash.esptool_port(fl['uid'])
    if fl['name'] != 'openocd' or 'vid_pid' not in fl:
        raise RuntimeError(f'no console route for a {fl["name"]} probe without vid_pid')
    probes = openocd_probes(fl)
    if len(probes) != 1:
        raise RuntimeError(f'probe {fl["uid"]}: want one on the bus, found {len(probes)}')
    return node(probes[0], '*:*/tty/tty*', deadline)


def base_state(image):
    """The row field naming what the operation's base check did (None: it proved nothing), for a board with one."""
    return {'base': image['base']['state']} if image.get('base') else {}


def step(name, board, image, test, run_dir, tracker):
    """Flash the test's sketch, wait for the DUT to enumerate anew, run the host check. A dict row."""
    if role(name) == 'host':
        return P.step_host(name, board, image, test, run_dir, tracker)
    started = time.monotonic()
    vid_pid = image['vidPid']
    why, detached, dev = flash_and_enumerate(name, board, image, run_dir, tracker)
    if why:
        return {'test': test, 'pass': False, 'detail': why, 'detach': detached, **base_state(image)}
    if not dev:
        others = [d['busport'] for d in devices(vid_pid)]
        return {'test': test, 'pass': False, 'detail': f'no {vid_pid} {dut_name(board)} enumerated anew within '
                                                       f'{ENUM_TIMEOUT}s (that VID:PID at: {others})',
                **base_state(image)}
    ok, detail = run_test(test, dev, tracker, run_dir, board)
    return {'test': test, 'pass': ok, 'detail': detail, 'device': dev['busport'], 'detach': detached,
            'seconds': round(time.monotonic() - started, 1), **base_state(image)}


def run_test(test, dev, tracker, run_dir, board):
    """(ok, detail) of a device test's host check on `dev` within TEST_TIMEOUT."""
    try:
        return P.TEST_FUNCS[test](dev, secrets.token_hex(4), time.monotonic() + TEST_TIMEOUT, tracker, run_dir,
                                  board)
    except Unclean:
        raise
    except Exception as e:   # a host-side failure of the check is the test's, once its children ended
        tracker.check()
        return False, f'{type(e).__name__}: {e}'


def restore(name, board, images, run_dir, tracker):
    sketch = restore_sketch(name)
    row = step(name, board, images[sketch], P.RESTORE_TEST[role(name)], run_dir, tracker)
    return {'pass': row['pass'], 'state': f'{sketch} restored' if row['pass'] else 'unknown',
            'evidence': row['detail'], **({'base': row['base']} if 'base' in row else {})}


# ---- the battery

def proven_ended(receipt):
    """A run_tracked receipt that rechecks done; an unrecheckable entry (Tracker._note) never is."""
    return 'unrecheckable' not in receipt and A.process.recheck(receipt) == 'done'


def legacy_pending(name):
    """'' unless an older engine's pending-session record lists children not proven ended (agentrc PendingSessions):
    reported, never a refusal; cleared once they are."""
    try:
        A.lock.PendingSessions(name, PENDING_KIND).clear(lambda s: proven_ended(s['receipt']))
    except (A.lock.PendingRefused, OSError) as e:
        return f'children an older engine recorded pending: {e}'
    return ''


def note_restored(name, failures, evidence):
    """The board was restored: agentrc hil_lock marks restored each open failure this task found whose own evidence is
    resolved and keeps every other open. '' or why some were not marked."""
    try:
        return '; '.join(A.lock.resolve_restored(name, failures, evidence=evidence))
    except Exception as e:
        return f'restorations not logged: {e}'


def log_failure(name, detail, pending, task=None):
    """A cleanup this session command (no flock of its own), or an earlier invocation (`task`, its claim), could not
    prove; '' or why it was not logged."""
    try:
        A.lock.log_cleanup_failure(name, 'hil_test.py', detail=detail, task=task,
                                   receipts=[p['receipt'] for p in pending])
    except Exception as e:
        return f'cleanup log not written: {e}'
    return ''


def with_logged(found, fresh):
    """`found` (a take's or a session's open failures) plus each of `fresh`, the log read now, it lacks: by id, or
    whole for one whose id is not a string (the log keeps those open, and they may be unhashable)."""
    ids = {f['id'] for f in found if isinstance(f.get('id'), str)}
    return found + [f for f in fresh if (f['id'] not in ids if isinstance(f.get('id'), str) else f not in found)]


def add_claim_note(d, why):
    """Append `why`, when there is one, to `d`'s claimNote."""
    if why:
        d['claimNote'] = '; '.join(filter(None, (d.get('claimNote'), why)))


def claim(name, tracker, found):
    """Name this invocation in the board's claim record before its first tool, under the board's lock or the caller's
    hold, and judge the earlier claims there (agentrc `hil` skill, Board locks). Returns (entry, `found` with what the
    log holds now, '' or what was not logged or removed); Refused when the record cannot name this invocation, InUse
    while another invocation is alive or a child of an earlier one still runs after CLAIM_SETTLE, this claim removed
    and that earlier one kept."""
    record = A.lock.PendingSessions(name, CLAIM_KIND)
    try:
        entry = A.lock.claim_entry(tracker.state_dir, 'hil_test.py')
        others = record.claim(entry)
    except (OSError, ValueError, A.lock.PendingRefused) as e:
        raise Refused(f"the board's claim record could not name this invocation: {type(e).__name__}: {e}")
    notes, earlier, logged = [], [], False
    try:
        if alive := [s['receipt'] for s in others if A.lock.claim_alive(s)]:
            raise InUse(f'another invocation on {name} is running: {alive}', found)
        # read after the gate: a claimant alive at the take may have logged a receipt since, to report and keep
        found = with_logged(found, A.lock.open_cleanup_failures(name))
        keep = A.lock.logged_tokens(found)
        for s in others:
            state, receipts = A.lock.invocation_state(s, keep)
            if state == 'alive':   # not after the gate, but a live claim is never removed
                continue
            why = ''
            if new := [r for r in receipts if A.lock.receipt_token(r) not in keep]:
                why = log_failure(name, 'an earlier invocation ended with children not proven ended',
                                  [{'receipt': r} for r in new], task=s['receipt'])
                logged = logged or not why
                if why:
                    notes.append(why)
            earlier.append((s, receipts, bool(why)))
        if logged:
            found = with_logged(found, A.lock.open_cleanup_failures(name))
        states = iter(A.lock.settle([r for _, receipts, _ in earlier for r in receipts], CLAIM_SETTLE)[0])
        running = []
        for s, receipts, unlogged in earlier:
            # only a proven-live child holds the board; its claim stays for the next attempt to judge again
            if pending := [r for r in receipts if next(states) == 'pending']:
                running += pending
            elif not unlogged:   # one whose log failed stays, for the next attempt to log
                try:
                    record.remove(s)
                except (OSError, A.lock.PendingRefused) as e:
                    notes.append(f'earlier claim not removed: {e}')
        if running:
            raise InUse(f'children of an earlier invocation on {name} still run after {CLAIM_SETTLE}s: '
                        f'{A.lock.open_runs(running, ["pending"] * len(running))}', found)
    except InUse:
        with contextlib.suppress(OSError, A.lock.PendingRefused):
            record.remove(entry)
        raise
    return entry, found, '; '.join(notes)


def unclaim(name, entry, tracker):
    """Remove this invocation's claim once recheck_dir of its operations answers done, the files of those it logged
    kept for resolve_restored; before its lock goes, so the next taker does not find it alive. '' or why it stays."""
    if entry is None:
        return ''
    try:
        keep = {t for p in tracker.pending if (t := A.lock.receipt_token(p['receipt']))}
        verdict, _ = A.process.recheck_dir(tracker.state_dir, keep)
        if verdict != 'done':
            return f'claim kept: its operations recheck {verdict}'
        A.lock.PendingSessions(name, CLAIM_KIND).remove(entry)
    except Exception as e:   # the lock is released after this whatever it found
        return f'claim not removed: {type(e).__name__}: {e}'
    return ''


def probe_present(flasher):
    """'' when exactly one probe with the flasher's serial is on this host's bus, else why not."""
    if flasher['name'] == 'esptool':
        try:
            A.flash.esptool_port(flasher['uid'])
            return ''
        except RuntimeError as e:
            return str(e)
    if flasher['name'] == 'jlink':
        serials = {d['busport']: d['serial'] for d in A.usb.usb_scan(vid='1366')}
        found = A.usb.jlink_serial_candidates(flasher['uid'], serials)
    else:
        found = [d['busport'] for d in openocd_probes(flasher)]
    if len(found) == 1:
        return ''
    return f'probe {flasher["uid"]} is not on this host\'s bus' if not found else \
        f'probe {flasher["uid"]} is ambiguous: {found}'


def run_board(name, board, tests, record_path, roster_path, run_root, manifest):
    run_dir = Path(run_root) / name
    run_dir.mkdir(parents=True, exist_ok=True)
    row = {'board': name, 'ran': False, 'pass': False, 'locked': False, 'recoveryBlocker': False, 'tests': [],
           'cleanup': {'pass': True, 'state': 'untouched', 'evidence': '', 'released': False, 'receipts': []},
           'firmware': None, 'detail': ''}
    why = probe_present(board['flasher'])
    if why:
        row['detail'] = f'not run: {why}'
        return row
    try:
        tracker = Tracker(run_dir)
    except OSError as e:
        row['detail'] = f'refused: run dir {run_dir}: {e}'
        return row
    try:
        fh = A.lock.take_board_lock(name, 'hil_test.py', protected=True, locks=[name], runDir=str(run_dir))
    except A.lock.RecoveryBlocked as e:
        row.update(locked=True, recoveryBlocker=True, detail=f'recovery blocker: {e}')
        return row
    except A.lock.BoardLocked as e:
        row.update(locked=True, detail=f'board locked: {e}')
        return row
    except A.lock.LockUnavailable as e:
        row.update(detail=f'board lock unavailable: {e}')
        return row
    started, unclean, found, entry = time.monotonic(), '', fh.cleanup_failures, None
    row.update(A.lock.failure_report(found))
    try:
        if why := legacy_pending(name):
            row['previousPending'] = why
        try:
            entry, found, why = claim(name, tracker, found)
        except InUse as e:
            row.update(locked=True, detail=f'board in use: {e}', **A.lock.failure_report(e.found))
            return row
        except Refused as e:
            row['detail'] = f'refused: {e}'
            return row
        row.update(A.lock.failure_report(found))
        add_claim_note(row, why)
        try:
            rec, images = admit(record_path, name, roster_path, run_dir, manifest=manifest)
        except Refused as e:
            row['detail'] = f'refused: {e}'
            return row
        row['firmware'] = {'buildRecord': str(record_path), 'head': rec['source']['head'], 'fqbn': rec['fqbn'],
                           'platform': rec['platform'], 'sketches': {k: v['sha256'] for k, v in images.items()},
                           'fqbns': {k: v['fqbn'] for k, v in images.items()},   # a core's sketches may differ
                           'driftedHeaders': sorted({h['counterpart'] for s in rec['sketches'].values()
                                                     for h in s.get('foreignHeaders', []) if not h['matches']})}
        extra = BASE_MAX if rec.get('base') else 0   # each step and the restore may check and rewrite the base
        budget, need = BOARD_BUDGET + (len(tests) + 1) * extra, STEP_MAX + CLEANUP_RESERVE + 2 * extra
        for test in tests:
            if budget - (time.monotonic() - started) < need:
                row['tests'].append({'test': test, 'pass': False, 'detail': 'not run: board budget left too small'})
                continue
            row['ran'] = True
            row['cleanup']['state'] = 'unknown'
            row['tests'].append(step(name, board, images[P.TESTS[test]], test, run_dir, tracker))
        if row['ran']:
            row['cleanup'].update(restore(name, board, images, run_dir, tracker))
        row['pass'] = row['ran'] and all(t['pass'] for t in row['tests']) and row['cleanup']['pass']
    except Unclean as e:
        unclean = str(e)
        row['cleanup'].update({'pass': False, 'state': 'pending', 'evidence': unclean})
        row['detail'] = f'a child outlived its operation: {unclean}'
    except BaseException as e:   # interrupted or a harness bug: the board's state is unknown
        unclean = f'{type(e).__name__}: {e}'
        row['cleanup'].update({'pass': False, 'state': 'unknown', 'evidence': unclean})
        row['detail'] = f'aborted: {unclean}'
        row['interrupted'] = not isinstance(e, Exception)
    finally:
        row['cleanup']['receipts'] = list(tracker.receipts)
        if tracker.pending:   # unrecheckable ones included: the board's cleanup log keeps them
            row['cleanup']['pending'] = list(tracker.pending)
        add_claim_note(row, unclaim(name, entry, tracker))
        end_run(fh, name, row, tracker, found)
    return row


def end_run(fh, name, row, tracker, found):
    """The board's lock ends with this run (agentrc#464): released, or ended through end_failed with the cleanup it
    could not prove logged; a restore that passed marks the open failures the take found restored."""
    c = row['cleanup']
    if c['pass']:
        A.lock.release_board_lock(fh)
        if row['ran'] and (why := note_restored(name, found, f'{name}: {c["state"]}')):
            c['restoredNote'] = why
    else:
        end_failed(fh, name, c, c['evidence'] or row['detail'], tracker)
    c['released'] = True


def end_failed(fh, name, cleanup, detail, tracker):
    """End the board's lock through agentrc's end_failed, the tracker's unproven children logged; `cleanup` (a row's)
    records the release and any log error."""
    errors = A.lock.end_failed({name: fh}, 'hil_test.py', detail=detail,
                               receipts=[p['receipt'] for p in tracker.pending])
    cleanup['released'] = True
    if errors.get(name):
        cleanup['logError'] = '; '.join(errors[name])


REPORT_BODY_KEYS = ('pass', 'caveat', 'exitCode', 'results')


def schema():
    def check(body):
        if not all(k in body for k in REPORT_BODY_KEYS) or not isinstance(body['results'], list):
            raise ValueError(f'report body needs {REPORT_BODY_KEYS}')

    return A.report.BodySchema(name=P.REPORT_SCHEMA, version=1, stem='hil_report', evidence='firmware-validation',
                               check=check, empty=lambda b: not b['results'], render=render)


def render(body):
    tests = sorted({t['test'] for r in body['results'] for t in r['tests']})
    mark = {True: '✅', False: '❌', None: '⚪'}
    rows = []
    for r in body['results']:
        cells = {t['test']: mark[t['pass']] for t in r['tests']}
        status = 'locked' if r['locked'] else ('' if r['ran'] else 'not run')
        rows.append([r['board']] + [cells.get(t, '⚪') for t in tests] +
                    [mark[r['cleanup']['pass']] + ' ' + r['cleanup']['state'], status or r['detail'][:80]])
    table = A.report.table(['board'] + tests + ['cleanup', 'note'], rows)
    lines = ['# HIL report', '', table, '', f'pass: {body["pass"]}, exit {body["exitCode"]}']
    if body['caveat']:
        lines.append(f'caveat: {body["caveat"]}')
    for r in body['results']:
        for t in r['tests']:
            if not t['pass']:
                lines.append(f'- {r["board"]} {t["test"]}: {t["detail"]}')
        if r['detail']:
            lines.append(f'- {r["board"]}: {r["detail"]}')
        drifted = (r.get('firmware') or {}).get('driftedHeaders')
        if drifted:
            lines.append(f'- {r["board"]}: its core\'s bundled copy of the library differs from the checkout in '
                         f'{", ".join(drifted)}; the result covers that mix')
    return '\n'.join(lines) + '\n'


def verdict(row):
    """The `hil` skill's verdict of one report row."""
    c = row['cleanup']
    if row['recoveryBlocker'] or not c['pass'] or c['state'] in ('pending', 'unknown') and row['ran']:
        return 'blocker'
    if not row['ran']:
        return 'not-run'
    return 'pass' if row['pass'] else 'fail'


def exit_code(rows):
    if any(not r['cleanup']['pass'] for r in rows):     # a failed cleanup: logged, the board released
        return 4
    return 0 if rows and all(r['pass'] for r in rows) else 1


def cmd_run(args):
    staged = verify_staged(args.staged) if args.staged else None
    roster = load_roster(args.roster)
    picked = selection(roster, args.board, args.test)
    names = list(picked)
    try:
        import serial  # noqa: F401
    except ImportError:
        raise Refused('pyserial is missing (python3 -m pip install pyserial, or the distro package)')
    for b in names:
        tool = {'openocd': 'openocd', 'jlink': 'JLinkExe', 'esptool': 'esptool'}[roster[b]['flasher']['name']]
        if not shutil.which(tool):
            raise Refused(f'{b}: {tool} not on PATH')
    run_root = Path(args.run_dir or P.ROOT / 'build' / 'hil-run' / time.strftime('%Y%m%d-%H%M%S'))
    run_root.mkdir(parents=True, exist_ok=True)
    sch = schema()
    attempt = A.report.start_attempt(run_root, sch, accumulate=False)
    rows, termination = [], 'completed'
    try:
        for b in names:
            record = Path(args.record_dir) / f'{b}.build.json'
            rows.append(run_board(b, roster[b], picked[b], record, Path(args.roster), run_root, staged))
            if rows[-1].pop('interrupted', False):
                termination = 'aborted'
                break
    except KeyboardInterrupt:   # between boards: none is held
        termination = 'aborted'
    code = exit_code(rows) if termination == 'completed' else 4
    body = {'pass': code == 0, 'caveat': '' if termination == 'completed' else 'aborted by a signal',
            'exitCode': code, 'results': rows}
    meta = A.report.make_meta(sch, attempt, termination=termination, invocation=sys.argv, selection=names,
                              host=socket.gethostname(), roster=A.report.roster_identity(args.roster))
    out = dict(body)
    try:
        out['reportPath'] = str(A.report.publish(run_root, sch, body, meta)[0])
    except A.report.ReportError as e:
        recovery = A.report.recover_publish(run_root, sch, attempt, e)
        out['reportPath'] = str(run_root / 'hil_report.json') if recovery.outcome == 'repaired' else None
        out['reportError'] = f'{e}; recovery {recovery.outcome}: {recovery.problems}'
        if recovery.outcome != 'repaired':   # no report of this run stands: it cannot pass
            code = max(code, 1)
            out.update({'pass': False, 'exitCode': code})
    emit(out)
    return code


def cmd_report(args):
    sch = schema()
    reading = A.report.read_document(Path(args.run_dir) / 'hil_report.json', sch)
    if reading.outcome != 'valid':
        raise Refused(f'report in {args.run_dir}: {reading.outcome}')
    body, meta = reading.body, reading.meta
    requested = args.board or list(meta['selection'])
    rows = {r['board']: r for r in body['results']}
    verdicts = [{'board': b, 'verdict': verdict(rows[b]), 'detail': rows[b]['detail'], 'evidence': []}
                for b in requested if b in rows]
    folded = A.report.apply_run_rules(sch, requested, verdicts, meta['termination'], body['caveat'])
    results = [dict(rows.get(v['board'], {'board': v['board'], 'ran': None, 'pass': False}), verdict=v['verdict'],
                    verdictDetail=v['detail']) for v in folded['rows']]
    emit({'pass': folded['verdict'] == 'pass', 'results': results, 'caveat': body['caveat'],
          'wedged': [r['board'] for r in results if r.get('ran') and
                     r.get('cleanup', {}).get('state') in ('pending', 'unknown')],
          'reportPath': str(Path(args.run_dir) / 'hil_report.json')})
    return 0


def held_by(name, reason):
    state, rec = A.lock.record_state(name)
    if state == 'unreadable':   # is_locked reads the record, not the flock: the hold may still stand
        raise Refused(f'{name} has an unreadable holder record: whether {reason!r} holds it is unknown')
    if state != 'record' or rec.get('reason') != reason or not A.lock.holder_alive(rec):
        raise NotHeld(f'{name} is not held under reason {reason!r} by a live holder (record: {rec})')


@contextlib.contextmanager
def one_session_command(name):
    """One session command at a time per tree and board: the hold admits every command under its reason, and a
    command a dropped ssh left running must finish, its pending children recorded, before the next one starts."""
    d = P.ROOT / 'build' / 'hil-manual'
    d.mkdir(parents=True, exist_ok=True)
    with open(d / f'.{name}.lock', 'a') as f:
        try:
            fcntl.flock(f, fcntl.LOCK_EX | fcntl.LOCK_NB)
        except BlockingIOError:
            raise Refused(f'another session command on {name} is still running in {P.ROOT}')
        yield


def manual(args):
    with one_session_command(args.board):
        return _manual(args)


def _manual(args):
    """A session command on a board the caller holds: OPS[args.cmd](args, board, run_dir, tracker, manifest) -> row.
    The JSON goes to stdout and to result.json in the run dir, which outlives a dropped ssh session."""
    held_by(args.board, args.under_hold)   # first: restore --release releases after any later refusal
    if args.cmd == 'restore' and args.release:
        return _restore_and_release(args)
    out, code, _ = _session_op(args, session_run_dir(args))
    emit(out)
    return code


def _session_inputs(args):
    manifest = verify_staged(args.staged) if args.staged else None
    roster = load_roster(args.roster)
    if args.board not in roster:
        raise Refused(f'{args.board} is not in {args.roster}')
    return roster, manifest


def session_run_dir(args):
    return P.ROOT / 'build' / 'hil-manual' / f'{args.board}-{args.cmd}-{time.strftime("%Y%m%d-%H%M%S")}'


def _session_op(args, run_dir):
    """(the command's JSON, its exit code, the interrupt it turned into exit 4 or None), the JSON also in run_dir's
    result.json. A restore records its outcome in the board's cleanup log: one that failed, was refused or raised as a
    failure, a passing one as the restoration of the failure seen before it ran."""
    found = A.lock.open_cleanup_failures(args.board)
    tracker, extra, interrupt, entry = None, {}, None, None
    try:
        roster, manifest = _session_inputs(args)
        run_dir.mkdir(parents=True, exist_ok=True)
        tracker = Tracker(run_dir)
        if why := legacy_pending(args.board):
            extra['previousPending'] = why
        entry, found, why = claim(args.board, tracker, found)
        extra.update(A.lock.failure_report(found))
        add_claim_note(extra, why)
        row = OPS[args.cmd](args, roster[args.board], run_dir, tracker, manifest)
        out, code = {'pass': row['pass'], 'board': args.board, **row, 'survivors': [], 'log': str(tracker.log),
                     **extra}, 0 if row['pass'] else 1
    except BaseException as e:   # an interrupt or crash can leave a child as unproven as Unclean does
        pending = tracker.pending if tracker else []
        if not pending and not isinstance(e, Unclean):
            why = args.cmd == 'restore' and log_failure(
                args.board, f'restore refused or raised: {type(e).__name__}: {e}', [])
            if isinstance(e, InUse):   # its claim may have logged an earlier invocation's failure: kept with it
                with contextlib.suppress(OSError):
                    (run_dir / 'result.json').write_text(json.dumps(
                        {'pass': False, 'board': args.board, 'error': '; '.join(filter(None, (str(e), why))),
                         **extra, **refusal_report(e)}) + '\n')
            if why and isinstance(e, Refused):
                e.args = (f'{e}; {why}',)   # its type and report kept
            elif why:
                e.add_note(why)
            raise
        error = f'a child outlived its operation: {type(e).__name__}: {e}'
        out, code = {'pass': False, 'board': args.board, 'error': error, 'survivors': pending,
                     'logError': log_failure(args.board, error, pending),
                     'note': "logged in the board's cleanup log, which the next task on it reports", **extra}, 4
        interrupt = None if isinstance(e, Exception) else e
    else:
        if args.cmd == 'restore':
            why = (note_restored(args.board, found, f'{args.board}: {row.get("state")}') if row['pass'] else
                   log_failure(args.board, f'restore failed: {row.get("evidence")}', tracker.pending))
            if why:
                out['cleanupLog'] = why
    finally:
        claim_note = unclaim(args.board, entry, tracker)
    add_claim_note(out, claim_note)
    (run_dir / 'result.json').write_text(json.dumps(out) + '\n')
    return out, code, interrupt


def _restore_and_release(args):
    """restore --release: the hold is released whatever the restore did, inside the caller's one_session_command.
    Exit 0 restored and released, RESTORE_FAILED released after a restore that failed or was refused,
    RELEASE_FAILED not released (the hold may still stand); an interrupt propagates once the release ran."""
    out, code, interrupted, hold = {'pass': False, 'board': args.board}, None, None, None
    run_dir = session_run_dir(args)
    try:
        try:
            out, code, interrupted = _session_op(args, run_dir)
        except Exception as e:
            out['error'] = '; '.join([str(e) if isinstance(e, Refused) else f'{type(e).__name__}: {e}',
                                      *getattr(e, '__notes__', ())])
            out.update(refusal_report(e))
        except BaseException as e:
            out['error'], interrupted = f'{type(e).__name__}: {e}', e
        finally:
            # a signal from here on waits until the release ran and its verdict is out
            hold = A.process.InterruptHold()
            out['released'] = release_hold(args.board)
        restored = code == 0 and out.get('pass') is True
        out['restored'] = restored
        if out['released']['exit'] != 0:
            code = RELEASE_FAILED
        else:
            code = 0 if restored else RESTORE_FAILED
        out['pass'] = code == 0
        try:   # the final verdict, released included, replaces the restore's own result.json
            run_dir.mkdir(parents=True, exist_ok=True)
            (run_dir / 'result.json').write_text(json.dumps(out) + '\n')
        except OSError as e:
            out['resultError'] = f'{run_dir / "result.json"} not written: {e}'
        emit(out)
    finally:
        if hold is not None:
            hold.release()
    if interrupted is not None:
        raise interrupted
    return code


def bundle_root(bundle):
    """The tree-relative dir a session staged a record bundle into (hil_remote.py), or the checkout."""
    if not bundle:
        return P.ROOT
    d = (P.ROOT / bundle).resolve()
    if not d.is_relative_to(P.ROOT.resolve()) or not d.is_dir():
        raise Refused(f'--bundle {bundle} is not a directory inside {P.ROOT}')
    return d


def admit_sketch(args, sketch, run_dir, manifest, experimental_ok):
    base = bundle_root(args.bundle)
    rec, images = admit(base / args.build_record, args.board, args.roster, run_dir, manifest=manifest,
                        experimental_ok=experimental_ok, base=base)
    if sketch not in images:
        raise Refused(f'{args.build_record} has no sketch {sketch}; it has {sorted(images)}')
    return {'record': str(args.build_record), 'experimental': rec['experimental']}, images[sketch], images


def op_flash(args, board, run_dir, tracker, manifest):
    meta, image, _ = admit_sketch(args, args.sketch, run_dir, manifest, True)
    return {**meta, **flash_only(args.board, board, image, run_dir, tracker)}


def op_restore(args, board, run_dir, tracker, manifest):
    if args.report:
        failed = json.loads(Path(args.report).read_text())
        cleanups = [row['cleanup'] for row in failed['results'] if row['board'] == args.board]
        receipts = [r for c in cleanups for r in c.get('receipts', [])]
        receipts += [p['receipt'] for c in cleanups for p in c.get('pending', []) if p['receipt'] not in receipts]
        open_receipts = [r for r in receipts if not proven_ended(r)]
        if open_receipts:
            raise Refused(f'children of the failed run are not proven ended: {open_receipts}')
    meta, _, images = admit_sketch(args, restore_sketch(args.board), run_dir, manifest, False)
    return {**meta, **restore(args.board, board, images, run_dir, tracker)}


def release_hold(name):
    """End the caller's hold once its board is restored; under the session lock, so no command slips in between."""
    r = subprocess.run([sys.executable, str(A.root / LOCK_SCRIPT), 'release', name],
                       capture_output=True, text=True, timeout=30)
    return {'exit': r.returncode, 'output': (r.stdout + r.stderr).strip()[-1000:]}


def cmd_release(args):
    """End a hold its session never saw confirmed (an ssh drop after the holder's ack): only one recorded under the
    session's own reason, so a session that lost the race cannot end the winner's. Nothing ran under it: no restore."""
    with one_session_command(args.board):
        try:
            held_by(args.board, args.under_hold)
        except NotHeld as e:
            emit({'pass': False, 'board': args.board, 'held': False, 'error': str(e)})
            return NOT_HELD
        released = release_hold(args.board)
    emit({'pass': released['exit'] == 0, 'board': args.board, 'released': released})
    return 0 if released['exit'] == 0 else 1


def run_child(argv, timeout, tracker):
    r = tracker.run(argv, timeout=timeout)
    tracker.check()
    out = _text(r.stdout) + _text(r.stderr)
    return {'pass': r.returncode == 0, 'exit': r.returncode, 'output': out[-4000:]}


def op_exec(args, board, run_dir, tracker, manifest):
    return run_child(args.argv, args.timeout, tracker)


def usj_at(dbg):
    """The roster's USB-Serial/JTAG bus-port (debug.usb_port), once the device there is 303a:1001 with serial debug.uid
    within one enumeration. agentrc's usj_location walks the whole bus and refuses while any 303a:1001 has no readable
    serial, which the S3's Arduino DUT never has."""
    d = str(Path(A.usb.SYS_USB_DEVICES) / dbg['usb_port'])
    ino = A.usb.enumeration_id(d)
    try:
        ids = tuple((Path(d) / a).read_text().strip() for a in ('idVendor', 'idProduct'))
    except OSError:
        ids = None
    serial = A.usb.read_sysfs(str(Path(d) / 'serial'))
    if ino is None or ids != A.esp().USJ_VID_PID or serial != dbg['uid'] or A.usb.enumeration_id(d) != ino:
        raise RuntimeError(f'no USB-Serial/JTAG {dbg["uid"]} at {dbg["usb_port"]} (found {ids}, serial {serial!r})')
    return dbg['usb_port']


def esp_openocd(dbg, chip):
    """openocd-esp32 on the board's USB-Serial/JTAG through agentrc's hil_openocd_esp checks: the verified build and its
    own scripts, the USJ pinned by bus-port before its serial (a serial alone opens every 303a:1001 on the host first),
    and ESP_FLASH_SIZE 0 before the target cfg, so gdb-attach skips its `reset halt` for memory protection
    (esp_common.cfg:321). ValueError or RuntimeError when the route cannot be built."""
    esp = A.esp()
    found, cfgs = esp.admit({'name': 'openocd_esp', 'uid': dbg['uid'], 'args': dbg['openocd']})
    if found != chip:
        raise ValueError(f'debug.openocd names {found}, the board is an {chip}')
    tool = esp.find_openocd(dbg['tool'])
    esp.check_cfgs(found, cfgs, tool['scripts'])
    files = [os.path.join(tool['scripts'], c) for c in cfgs]
    return [tool['exe'], '-s', tool['scripts'], '-c', 'tcl port disabled', '-c', 'telnet port disabled',
            '-c', 'set ESP_FLASH_SIZE 0', '-f', files[0], '-c', f'adapter usb location {usj_at(dbg)}',
            '-c', f'adapter serial {dbg["uid"]}', *(w for f in files[1:] for w in ('-f', f))]


def gdb_argv(name, board, elf, commands):
    """gdb in batch mode (gdb-multiarch; Espressif's for the chip on debug.tool openocd-esp32) over an openocd it starts on a pipe, configured by the roster's debug.openocd and
    pinned to the board's probe. The firmware must run on once gdb ends:
    - openocd halts the target on gdb's connect and has no default gdb-detach handler, so one resumes it; it runs when
      the connection closes (gdb_server.c gdb_connection_closed), i.e. when gdb exits, not at `detach`;
    - with a memory map openocd probes every flash bank on connect (gdb_server.c gdb_new_connection), and the
      RP2040's probe leaves the QSPI flash out of XIP mode: no map, no probe;
    - without a map gdb takes flash for RAM and would patch a software breakpoint into it, so every breakpoint is
      made a hardware one (openocd.texi, gdb_breakpoint_override).
    debug.tool openocd-esp32 runs Espressif's openocd on the chip's USB-Serial/JTAG (debug.uid, esp_openocd)."""
    dbg = board['debug']
    gdb = 'gdb-multiarch'
    if dbg.get('tool') == 'openocd-esp32':
        chip = B.P.BOARDS[name]['chip']
        openocd = esp_openocd(dbg, chip)
        gdb = ESP_GDB.get(chip)
        if not gdb:
            raise RuntimeError(f'no Espressif gdb listed for {chip} (ESP_GDB)')
        if not shutil.which(gdb):
            raise RuntimeError(f'{gdb}, the {chip} gdb, is not on PATH')
    else:
        probe = {'name': 'openocd', 'uid': board['flasher']['uid'], 'args': dbg['openocd']}
        if board['flasher']['name'] == 'openocd' and 'vid_pid' in board['flasher']:
            probe['vid_pid'] = board['flasher']['vid_pid']
        openocd = A.flash.openocd_cmd_base(probe)
    cfgs = shlex.split(dbg['openocd'])   # the silicon: the RP2350 boards share the rp2040 build family
    handler = next((h for cfg, h in GDB_DETACH_RESUME.items() if cfg in cfgs), '{ resume }')
    openocd += [*GDB_SERVER, '-c', f'foreach t [target names] {{ $t configure -event gdb-detach {handler} }}']
    return [gdb, '-batch', '-nx', '-ex', f'target extended-remote | {shlex.join(openocd)}',
            *(w for c in commands for w in ('-ex', c)), elf]


def op_gdb(args, board, run_dir, tracker, manifest):
    if not board.get('debug', {}).get('openocd'):
        raise Refused(f'{args.board} has no debug.openocd in {args.roster}')
    meta, image, _ = admit_sketch(args, args.sketch, run_dir, manifest, True)
    try:
        argv = gdb_argv(args.board, board, image['elf'], args.ex)
    except (ValueError, RuntimeError) as e:
        return {**meta, 'pass': False, 'detail': f'debug route: {e}'}
    return {**meta, **run_child(argv, args.timeout, tracker)}


def op_console(args, board, run_dir, tracker, manifest):
    """Capture the board's console for --seconds into console.log, with console.times (one JSON line per chunk, and
    the reset's mark). --reset resets with the console open: an esptool board through its bridge's EN line (pulse_en),
    any other through its probe."""
    import serial
    try:
        tty = console_tty(board, time.monotonic() + 5)
    except (RuntimeError, TimeoutError) as e:
        return {'pass': False, 'detail': f'console: {e}'}
    buf, times, why = bytearray(), [], ''
    try:
        with serial.Serial(tty, args.baud, timeout=0.2) as port:
            t0 = time.monotonic()
            port.reset_input_buffer()
            if args.reset:
                # an RP2040 core reset keeps USBCTRL attached: without the detach the host keeps a dead device
                detached = '' if board['flasher']['name'] == 'esptool' else detach(args.board, board, None, tracker)
                if detached not in ('', 'detached'):
                    why = f'detach: {detached}'
                else:
                    times.append({'t': round(time.monotonic() - t0, 3), 'reset': True})
                    if board['flasher']['name'] == 'esptool':
                        pulse_en(port)
                    else:
                        why = reset(board['flasher'], tracker)
                        why = why and f'reset: {why}'
            if not why:
                read_console(port, time.monotonic() + args.seconds, buf, times, t0)
    except (serial.SerialException, OSError) as e:   # pyserial's line setters raise a bare OSError
        why = f'{tty}: {e}'   # the capture so far is kept; the hold stays with the caller
    except KeyboardInterrupt:   # SIGTERM too (main): stopping a capture keeps it
        if tracker.pending:
            raise   # _manual records the detach's or reset's unproven child
        why = f'{tty}: interrupted'
    finally:
        log = run_dir / 'console.log'
        log.write_bytes(buf)
        (run_dir / 'console.times').write_text(''.join(json.dumps(t) + '\n' for t in times))
    return {'pass': not why, 'detail': why or f'{tty}: {len(buf)} bytes in {args.seconds:g} s', 'console': str(log),
            'tail': buf.decode(errors='replace')[-2000:]}


def op_check(args, board, run_dir, tracker, manifest):
    """A device test against the firmware as it runs, no flash: --test (default the restoration test) on the one DUT
    enumerated now with the VID:PID the record gives that test's sketch. It shows the function works, not which image
    is flashed."""
    if role(args.board) == 'host':
        raise Refused(f'{args.board} is a host board: read its console')
    test = args.test or P.RESTORE_TEST['device']
    meta, image, _ = admit_sketch(args, P.TESTS[test], run_dir, manifest, True)
    devs = dut_devices(image['vidPid'], board)
    if len(devs) != 1:
        return {**meta, 'test': test, 'pass': False,
                'detail': f'want one {image["vidPid"]} {dut_name(board)} enumerated, found {len(devs)}'}
    ok, detail = run_test(test, devs[0], tracker, run_dir, board)
    return {**meta, 'test': test, 'pass': ok, 'detail': detail,
            'device': {'busport': devs[0]['busport'], 'devnum': devs[0].get('devnum')}}


OPS = {'flash': op_flash, 'restore': op_restore, 'gdb': op_gdb, 'exec': op_exec, 'console': op_console,
       'check': op_check}


def written(flasher, image):
    """What the flasher wrote: the ELF for openocd, the HEX for J-Link, the flash_args images for esptool."""
    if flasher['name'] == 'esptool':
        return f'{len(image["segments"])} flash_args images'
    return Path(image['hex'] if flasher['name'] == 'jlink' else image['elf']).name


def flash_only(name, board, image, run_dir, tracker):
    what = written(board['flasher'], image)
    if role(name) == 'host':   # no DUT on this host's bus: the console shows what runs
        why = flash(board['flasher'], image, run_dir, tracker)
        return {'pass': not why, 'detail': why or f'flashed {what} (a host board: read its console)',
                **base_state(image)}
    why, detached, dev = flash_and_enumerate(name, board, image, run_dir, tracker)
    if why:
        return {'pass': False, 'detail': why, 'detach': detached, **base_state(image)}
    if not dev:
        return {'pass': True, 'detail': f'flashed {what}; no {image["vidPid"]} {dut_name(board)} enumerated (the '
                                        f'image may not run USB)', **base_state(image)}
    return {'pass': True, 'detail': f'flashed {what}; enumerated as {image["vidPid"]} at {dev["busport"]}',
            'device': {'busport': dev['busport'], 'devnum': dev.get('devnum'), 'after': dev['after']},
            **base_state(image)}


def cmd_commission(args):
    """Find the DUT's identity under Arduino firmware: exactly one device of the record's VID:PID must re-enumerate
    across the flash's reset and again across a probe reset, with one serial (a no_serial board: at one port); the
    restoration sketch left running must then pass its role's restore test."""
    roster = load_roster(args.roster)
    if args.board not in roster:
        raise Refused(f'{args.board} is not in {args.roster}')
    if role(args.board) == 'host':
        raise Refused(f'{args.board} is a host board: no DUT enumerates here, its battery checks the console')
    board = roster[args.board]
    by_port = bool(board.get('no_serial'))

    def enumerated(vid_pid):   # {identity: enumeration id}; a serial board's usb_port, when set, must hold it
        if by_port:
            return {d['busport']: d['ino'] for d in devices(vid_pid) if no_serial(d)}
        return {d['serial']: d['ino'] for d in scan(vid_pid)
                if 'usb_port' not in board or d['busport'] == board['usb_port']}
    run_dir = P.ROOT / 'build' / 'hil-manual' / f'commission-{args.board}-{time.strftime("%Y%m%d-%H%M%S")}'
    run_dir.mkdir(parents=True, exist_ok=True)
    try:   # before the lock: a failure after it would leave a record the next task logs as a failed cleanup
        tracker = Tracker(run_dir)
    except OSError as e:
        raise Refused(f'run dir {run_dir}: {e}')
    try:
        fh = A.lock.take_board_lock(args.board, 'hil_test.py commission', protected=True, locks=[args.board])
    except (A.lock.BoardLocked, A.lock.RecoveryBlocked, A.lock.LockUnavailable) as e:
        raise Refused(f'{args.board}: {e}')
    out, touched, entry = {'board': args.board, 'rosterUid': board['uid']}, False, None

    def unclaimed():
        add_claim_note(out, unclaim(args.board, entry, tracker))
    try:
        out.update(A.lock.failure_report(fh.cleanup_failures))
        if why := legacy_pending(args.board):
            out['previousPending'] = why
        entry, found, why = claim(args.board, tracker, fh.cleanup_failures)
        out.update(A.lock.failure_report(found))
        add_claim_note(out, why)
        _, images = admit(Path(args.record_dir) / f'{args.board}.build.json', args.board, args.roster, run_dir)
        image = images[restore_sketch(args.board)]
        observed = []
        for action in ('flash', 'reset'):
            before = enumerated(image['vidPid'])
            touched = True
            if action == 'flash':
                detach(args.board, board, image['vidPid'], tracker)
                why = flash(board['flasher'], image, run_dir, tracker)
                out.update(base_state(image))
            else:
                detach(args.board, board, image['vidPid'], tracker)
                why = reset(board['flasher'], tracker)
            if why:
                raise RuntimeError(f'{action}: {why}')
            time.sleep(ENUM_TIMEOUT / 4)
            deadline, changed = time.monotonic() + ENUM_TIMEOUT, set()
            while time.monotonic() < deadline and not changed:
                changed = {k for k, ino in enumerated(image['vidPid']).items() if before.get(k) != ino}
                time.sleep(0.5)
            observed.append(sorted(changed))
        out['observed'] = observed
        found = {s for obs in observed for s in obs}
        if not (all(len(o) == 1 for o in observed) and len(found) == 1):
            raise RuntimeError(f'ambiguous: not exactly one device re-enumerated with one '
                               f'{"port" if by_port else "serial"} both times')
        if by_port:
            out['port'] = found.pop()
            out['matchesRoster'] = out['port'] == board['usb_port']
            dev = next(d for d in devices(image['vidPid']) if d['busport'] == out['port'] and no_serial(d))
        else:
            out['serial'] = found.pop()
            out['matchesRoster'] = out['serial'].lower() == board['uid'].lower()
            devs = [d for d in scan(image['vidPid'], out['serial'])
                    if 'usb_port' not in board or d['busport'] == board['usb_port']]
            if len(devs) != 1:
                raise RuntimeError(f'want one {image["vidPid"]} with serial {out["serial"]}, found {len(devs)}')
            dev = devs[0]
        test = P.RESTORE_TEST['device']
        ok, out['cleanup'] = run_test(test, dev, tracker, run_dir, board)
        if not ok:
            raise RuntimeError(f'{restore_sketch(args.board)} left running fails {test}: {out["cleanup"]}')
    except Refused:
        unclaimed()
        A.lock.release_board_lock(fh)
        raise
    except BaseException as e:   # after hardware access: the board's state is unknown, its failure logged
        unclaimed()
        if not touched:
            A.lock.release_board_lock(fh)
            raise
        out['error'] = f'{type(e).__name__}: {e}'
        row = {'board': args.board, 'cleanup': {'evidence': out['error'], 'receipts': list(tracker.receipts)}}
        if tracker.pending:
            row['cleanup']['pending'] = list(tracker.pending)
        end_failed(fh, args.board, row['cleanup'], out['error'], tracker)
        out['results'] = [row]   # result.json is a `restore --report` input
        (run_dir / 'result.json').write_text(json.dumps(out) + '\n')
        emit(out)
        return 4
    else:   # outside the handlers: an interrupt during the release must not end the board as failed
        unclaimed()
        A.lock.release_board_lock(fh)
    emit(out)
    return 0 if out.get('matchesRoster') else 1


def cmd_lock(rest):
    os.execv(sys.executable, [sys.executable, str(A.root / LOCK_SCRIPT), *rest])


def parse(argv):
    ap = argparse.ArgumentParser(description=(P.__doc__ or __doc__).split('\n')[0])
    sub = ap.add_subparsers(dest='cmd', required=True)
    r = sub.add_parser('run')
    r.add_argument('-b', '--board', action='append')
    r.add_argument('-t', '--test', action='append', choices=sorted(P.TESTS))
    r.add_argument('--record-dir', default=str(P.ROOT / 'build' / 'hil'))
    r.add_argument('--run-dir')
    r.add_argument('--staged', help='the manifest of a tree hil_remote.py staged (rig side)')
    r.add_argument('roster')
    c = sub.add_parser('commission')
    c.add_argument('-b', '--board', required=True)
    c.add_argument('--record-dir', default=str(P.ROOT / 'build' / 'hil'))
    c.add_argument('roster')
    for name in OPS:
        m = sub.add_parser(name)
        m.add_argument('-b', '--board', required=True)
        m.add_argument('--under-hold', required=True, metavar='REASON')
        m.add_argument('--staged', help='the manifest of a tree hil_remote.py staged (rig side)')
        if name == 'console':
            m.add_argument('--seconds', type=float, default=10, help=f'at most {CONSOLE_MAX}')
            m.add_argument('--baud', type=int, default=115200)
            m.add_argument('--reset', action='store_true', help='reset the target with the console open')
        if name == 'check':
            m.add_argument('--test', choices=P.ROLE_TESTS['device'], help=f'default {P.RESTORE_TEST["device"]}')
        if name not in ('exec', 'console'):
            m.add_argument('--build-record', required=True)
            m.add_argument('--bundle', help='a staged record bundle (tree-relative); --build-record is inside it')
        if name in ('flash', 'gdb'):
            m.add_argument('--sketch', required=True)
        if name == 'restore':
            m.add_argument('--report', help="a failed run's report: its children must be proven ended first")
            m.add_argument('--release', action='store_true',
                           help='then release the hold whatever the restore did (exit 0 restored, '
                                f'{RESTORE_FAILED} restore failed, {RELEASE_FAILED} not released)')
        if name in ('gdb', 'exec'):
            m.add_argument('--timeout', type=float, default=60, help=f'seconds, at most {EXEC_MAX}')
        if name == 'gdb':
            m.add_argument('--ex', action='append', default=[], help='a gdb command, run in order after the attach')
        m.add_argument('roster')
        if name == 'exec':
            m.add_argument('argv', nargs='+')
    rl = sub.add_parser('release')
    rl.add_argument('-b', '--board', required=True)
    rl.add_argument('--under-hold', required=True, metavar='REASON')
    rp = sub.add_parser('report')
    rp.add_argument('run_dir')
    rp.add_argument('-b', '--board', action='append')
    return ap.parse_args(argv)


def run(profile, argv=None):
    """The library's hil_test.py entry point: bind `profile`, then main()."""
    bind(profile)
    return main(argv)


def main(argv=None):
    argv = sys.argv[1:] if argv is None else argv
    if argv[:1] == ['lock']:
        return cmd_lock(argv[1:])
    args = parse(argv)
    if args.cmd in ('gdb', 'exec') and not 0 < args.timeout <= EXEC_MAX:
        emit({'pass': False, 'error': f'--timeout must be in (0, {EXEC_MAX}]'})
        return 2
    if args.cmd == 'console' and not 0 < args.seconds <= CONSOLE_MAX:
        emit({'pass': False, 'error': f'--seconds must be in (0, {CONSOLE_MAX}]'})
        return 2
    if args.cmd == 'console' and args.baud <= 0:
        emit({'pass': False, 'error': '--baud must be positive'})
        return 2
    if args.cmd in (*OPS, 'release'):
        # a session command runs over ssh: a dropped link must not end it between a child's start and its proof
        signal.signal(signal.SIGHUP, signal.SIG_IGN)
    if args.cmd in ('run', 'commission', *OPS):
        # SIGTERM unwinds as SIGINT does: run_tracked ends the child, and run_board, cmd_commission and _manual
        # record one it could not prove ended
        signal.signal(signal.SIGTERM, signal.default_int_handler)
    try:
        if args.cmd == 'run':
            return cmd_run(args)
        if args.cmd == 'commission':
            return cmd_commission(args)
        if args.cmd in OPS:
            return manual(args)
        if args.cmd == 'release':
            return cmd_release(args)
        return cmd_report(args)
    except Refused as e:
        emit({'pass': False, 'error': str(e), **refusal_report(e)})
        return NOT_HELD if isinstance(e, NotHeld) else 2
