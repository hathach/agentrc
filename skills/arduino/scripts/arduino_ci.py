#!/usr/bin/env python3
"""A library's Arduino HIL contract in CI (agentrc `arduino` skill): the hosted runner sets up and packs one board's
firmware, the rig runner runs the battery over the packed trees. A library's `hil_ci.py` calls run(profile, argv)
with its HIL profile (arduino_hil.py's names; HIL, the checkout-relative hil_test.py, as arduino_remote.py's).

  setup --board B --ci DIR [--versions FILE]
      (hosted) set B up as adafruit/ci-arduino's build_platform.py does before it builds (DIR: its checkout, the
      library at $GITHUB_WORKSPACE): its install_library_deps() and install_platform() for B's CI platform
      (BOARDS[B]['filters']). Refuses a board whose FQBN's vendor:arch is not the one that installs. Adds DIR and
      arduino-cli's dir to the checkout's info/exclude when inside it (the record needs a clean tree). --versions
      writes {platform: vendor:arch or null, cli: `version`, cores: `core list`} (arduino-cli JSON) there, whatever
      the outcome.
  pack --board B --record FILE --out DIR --run-id N --attempt N [--roster R]
      (hosted) the tree hil_remote.py would stage for B's clean record at HEAD (contract files, roster, agentrc
      closure, record and images, manifest.json), under DIR/B, with provenance.json {board, head, runId, attempt}.
  run --hosted DIR --head SHA --run-id N --attempt N --out DIR [-b B]... [--lock-wait S] [--deadline S]
      [--prune-days N] ROSTER
      (rig) one board at a time: DIR/B admitted (provenance and manifest name this run, B and SHA; every file pinned
      by the manifest, no link, no stray file; its roster this checkout's) and moved to OUT/B/tree, then
      `hil_test.py run --staged` there, each attempt in OUT/B/run-<n>. A locked board is admitted again every
      LOCK_POLL s up to --lock-wait; a fail is retried once (the `hil` Caller rules: a passing retry is flaky).
      Every attempt starts only before the --deadline (seconds from start; a started battery always finishes its
      restore); a board it stops is not-run "deadline", a fail it denies a retry stays failed. Writes
      OUT/summary.json, recording --attempt (admission ignores the tree's), also the last stdout line. Before the
      boards, removes OUT's siblings named <run>-<attempt> older than --prune-days whose summary shows every board
      finished clean; nothing else is ever removed.

Exits: setup/pack 0 done, 2 refused; run 0 every board passed, 1 a board failed or did not run, 2 a blocker (a
failed cleanup, logged and the board released, an older helper's cleanup holder, a run without a report), a run a
signal aborted, or refused before any board.
"""

import argparse
import json
import os
import re
import shutil
import signal
import subprocess
import sys
import tempfile
import time
from pathlib import Path

HERE = os.path.dirname(os.path.abspath(__file__))
FILES = ('skills/arduino/scripts/arduino_remote.py', 'skills/arduino/scripts/arduino_build.py',
         'skills/arduino/scripts/arduino_hil.py', 'skills/hil/boards.json', 'skills/hil/scripts/esptool_session.py',
         'skills/hil/scripts/hil_boards.py', 'skills/hil/scripts/hil_flash.py', 'skills/hil/scripts/hil_load.py',
         'skills/hil/scripts/hil_lock.py', 'skills/hil/scripts/hil_openocd_esp.py',
         'skills/hil/scripts/hil_openocd_esp_receipt.py', 'skills/hil/scripts/hil_process.py',
         'skills/hil/scripts/hil_remote_core.py', 'skills/hil/scripts/hil_report_core.py',
         'skills/hil/scripts/hil_report_notice.py', 'skills/hil/scripts/pyocd/run_pyocd.py',
         'skills/hil/scripts/pyocd/user_script.py', 'skills/hil/scripts/usb_observe.py',
         'skills/target-debug/scripts/esp_session.py')
PROVENANCE = 'provenance.json'
SUMMARY = 'summary.json'
SUMMARY_KIND = 'arduino-hil-ci'
LOCK_POLL = 30   # seconds between admissions of a locked board
LOCK_WAIT = 600
DEADLINE = 3600
PRUNE_DAYS = 30
RUN_NAME = re.compile(r'[0-9]+-[0-9]+')   # a state dir's <run id>-<attempt>
FINISHED = ('pass', 'fail', 'not-run')   # verdicts that leave nothing for recovery
EARLIER = ('previousCleanupFailure', 'openCleanupFailures', 'previousPending')   # a row's report of an earlier task


def _hil_load():
    import importlib.util
    spec = importlib.util.spec_from_file_location('agentrc_hil_load', os.path.join(HERE, '..', '..', 'hil',
                                                                                    'scripts', 'hil_load.py'))
    if spec.name not in sys.modules:
        mod = importlib.util.module_from_spec(spec)
        spec.loader.exec_module(mod)
        sys.modules.setdefault(spec.name, mod)
    return sys.modules[spec.name].load(spec.name, os.path.realpath(spec.origin))


hil_load = _hil_load()
hil_load.require(__file__, FILES)
R = hil_load.load_file('skills/arduino/scripts/arduino_remote.py')
H = R.H
P = None   # the bound HIL profile


class Refused(Exception):
    pass


def bind(profile):
    global P
    R.bind(profile)
    P = profile
    return sys.modules[__name__]


def emit(obj):
    print(json.dumps(obj), flush=True)


def log(msg):
    print(msg, file=sys.stderr, flush=True)


# ---- hosted: setup and pack

def vendor_arch_of(fqbn):
    return ':'.join(fqbn.split(':', 2)[:2])


def ci_platform(board, ci_dir):
    """(build_platform module, vendor:arch, its ALL_PLATFORMS entry) for `board`'s CI platform."""
    if board not in P.BUILD.BOARDS or 'filters' not in P.BUILD.BOARDS[board]:
        raise Refused(f'{board} is not a rig board of {P.BUILD.LIBRARY_NAME} (no CI platform in BOARDS)')
    sys.path.insert(0, str(Path(ci_dir).resolve()))
    try:
        import build_platform as bp   # ci-arduino's: its flags parsing and BUILD_DIR at import, nothing installed
    except ImportError as e:
        raise Refused(f'{ci_dir} is no adafruit/ci-arduino checkout: {e}')
    platform = P.BUILD.BOARDS[board]['filters']
    entry = bp.ALL_PLATFORMS.get(platform)
    if not isinstance(entry, list):
        raise Refused(f'{platform} (the CI platform of {board}) is no ci-arduino platform')
    vendor_arch = vendor_arch_of(entry[0])
    own = vendor_arch_of(P.BUILD.BOARDS[board]['fqbn'])
    if own != vendor_arch:
        raise Refused(f'{board} builds with {own}, ci-arduino installs {vendor_arch} for {platform}')
    return bp, vendor_arch, entry


def exclude_from_checkout(dirs):
    """Anchored info/exclude entries for each of `dirs` strictly inside the checkout, the file's own kept."""
    root = P.ROOT.resolve()
    p = subprocess.run(['git', 'rev-parse', '--git-path', 'info/exclude'], cwd=root, capture_output=True, text=True)
    if p.returncode:
        raise Refused(f'{root} is no git checkout: {p.stderr.strip()}')
    exclude = root / p.stdout.strip()
    text = exclude.read_text() if exclude.is_file() else ''
    have = text.splitlines()
    new = []
    for d in dirs:
        d = Path(d).resolve()
        if d != root and root in d.parents:
            line = '/' + re.sub(r'([*?\[\\])', r'\\\1', d.relative_to(root).as_posix()) + '/'
            if line not in have + new:
                new.append(line)
    if new:
        exclude.parent.mkdir(parents=True, exist_ok=True)
        with open(exclude, 'a') as f:
            f.write(('\n' if text and not text.endswith('\n') else '') + '\n'.join(new) + '\n')
    return new


def versions(platform):
    out = {'platform': platform}
    for key, argv in (('cli', ['version']), ('cores', ['core', 'list'])):
        try:
            p = subprocess.run(['arduino-cli', *argv, '--json'], capture_output=True, text=True, timeout=120)
            out[key] = json.loads(p.stdout) if p.returncode == 0 else {'error': p.stderr.strip()[-400:]}
        except (OSError, ValueError, subprocess.TimeoutExpired) as e:
            out[key] = {'error': str(e)}
    return out


def cmd_setup(a):
    vendor_arch = None
    try:
        bp, vendor_arch, entry = ci_platform(a.board, a.ci)
        cli = shutil.which('arduino-cli')
        exclude_from_checkout([a.ci] + ([os.path.dirname(cli)] if cli else []))
        bp.install_library_deps()   # exits non-zero itself on a failed install
        bp.install_platform(vendor_arch, entry)
    finally:
        if a.versions:
            Path(a.versions).write_text(json.dumps(versions(vendor_arch)) + '\n')
    emit({'board': a.board, 'platform': vendor_arch, 'ciPlatform': P.BUILD.BOARDS[a.board]['filters']})
    return 0


def cmd_pack(a):
    roster = R.rel(a.roster)
    head = R.head_of_clean_tree()
    record = R.record_files(a.record, head, a.board)
    out = Path(a.out) / a.board
    if out.exists():
        raise Refused(f'{out} exists: pack each board once into a fresh dir')
    Path(a.out).mkdir(parents=True, exist_ok=True)
    tmp = Path(tempfile.mkdtemp(prefix=f'.{a.board}-', dir=a.out))
    try:
        R.pack(tmp, roster, [record], head)
        R.core.write_json(tmp / PROVENANCE, {'board': a.board, 'head': head, 'runId': a.run_id,
                                             'attempt': a.attempt})
        tmp.rename(out)
    finally:
        shutil.rmtree(tmp, ignore_errors=True)
    emit({'board': a.board, 'head': head, 'out': str(out)})
    return 0


# ---- rig: admission

def read_json(path):
    try:
        return json.loads(Path(path).read_text())
    except (OSError, ValueError, RecursionError) as e:
        raise Refused(f'{path}: {e}')


def admit_tree(src, board, head, run_id, roster):
    """Refused unless the packed dir `src` is this run's tree of `board` at `head`: provenance, manifest and files
    agree, nothing is a link or unpinned, and its roster is this checkout's `roster`."""
    src = Path(src)
    if src.is_symlink() or not src.is_dir():
        raise Refused(f'no packed tree at {src}')
    entries = list(src.rglob('*'))
    # read, not pinned: refuse_unsafe_paths below covers only what the manifest names
    if links := [name for name in (PROVENANCE, R.MANIFEST) if (src / name).is_symlink()]:
        raise Refused(f'{src}: {", ".join(links)} is a link')
    prov, manifest = read_json(src / PROVENANCE), read_json(src / R.MANIFEST)
    if not isinstance(prov, dict) or not isinstance(manifest, dict) or not isinstance(manifest.get('files'), dict):
        raise Refused(f'{src}: provenance or manifest is no JSON object')
    want = {'board': board, 'head': head, 'runId': run_id}
    wrong = {k: prov.get(k) for k, v in want.items() if prov.get(k) != v}
    if manifest.get('head') != head:
        wrong['manifest head'] = manifest.get('head')
    if wrong:
        raise Refused(f'{src} is not run {run_id}\'s {board} at {head[:12]}: {wrong}')
    pinned = manifest['files']
    if roster not in pinned:
        raise Refused(f'{src}: its manifest pins no {roster}')
    try:
        R.H.refuse_unsafe_paths(src, pinned)
    except R.H.Refused as e:
        raise Refused(f'{src}: {e}') from None
    # a link the manifest does not pin is as unpinned as a file
    files = {p.relative_to(src).as_posix() for p in entries if p.is_file() or p.is_symlink()}
    stray = sorted(files - set(pinned) - {R.MANIFEST, PROVENANCE})
    missing = sorted(set(pinned) - files)
    changed = sorted(r for r in set(pinned) & files if R.core.digest(src / r) != pinned[r])
    if stray or missing or changed:
        raise Refused(f'{src}: unpinned {stray[:5]}, missing {missing[:5]}, changed {changed[:5]}')
    if R.core.digest(src / roster) != R.core.digest(P.ROOT / roster):
        raise Refused(f'{src}: its {roster} is not this checkout\'s')


# ---- rig: the board loop

class Clock:
    def __init__(self, deadline):
        self.t0, self.deadline = time.monotonic(), deadline

    def elapsed(self):
        return time.monotonic() - self.t0

    def left(self):
        return self.deadline - self.elapsed()


def battery(tree, board, run_dir, roster):
    """One `hil_test.py run` of `board` in its tree and that run's report row with its verdict (None: no report)."""
    argv = [sys.executable, '-u', P.HIL, 'run', '--staged', R.MANIFEST, '--run-dir', str(run_dir), '-b', board, roster]
    run_dir.parent.mkdir(parents=True, exist_ok=True)
    with open(run_dir.with_suffix('.log'), 'w') as out:   # a signal to the job ends the battery its own way
        code = subprocess.run(argv, cwd=tree, stdout=out, stderr=subprocess.STDOUT).returncode
    rep = subprocess.run([sys.executable, P.HIL, 'report', str(run_dir), '-b', board], cwd=tree,
                         capture_output=True, text=True)
    try:
        row = json.loads(rep.stdout.strip().splitlines()[-1])['results'][0] if rep.returncode == 0 else None
    except (ValueError, IndexError, KeyError, TypeError):
        row = None
    return code, row


def attempt_entry(run_dir, code, row, seconds):
    base = {'runDir': str(run_dir), 'exit': code, 'seconds': round(seconds)}
    if row is None:
        return {**base, 'verdict': 'blocker',
                'detail': f'no readable report (exit {code}): see {run_dir.with_suffix(".log").name}'}
    return {**base, 'verdict': row['verdict'],
            'locked': bool(row.get('locked')) and not row.get('recoveryBlocker'), 'detail': row.get('detail', ''),
            'tests': [{k: t.get(k) for k in ('test', 'pass', 'detail')} for t in row.get('tests', [])],
            'cleanup': {k: row.get('cleanup', {}).get(k) for k in ('pass', 'state', 'released')},
            'firmware': row.get('firmware'), **{k: row[k] for k in EARLIER if row.get(k)}}


def run_board(board, tree, out, roster, clock, lock_wait, stop):
    """The board's attempts and result. Locked: admitted again every LOCK_POLL s within lock_wait; a fail: one
    retry, the fail standing until a retry runs. No attempt starts past the deadline, the lock wait or a signal."""
    res = new_result(board)
    lock_since, waited, failed = None, 0.0, None   # lock_since: awaiting admission; failed: the fail awaiting a retry

    def unresolved(why):
        if failed:
            res['retry'] = f'retry not run: {why}'
            return finish(res, 'fail', failed['detail'], failed)
        return finish(res, 'not-run', f'not run: {why}')
    while True:
        last = res['attempts'][-1] if res['attempts'] else None
        if stop['signal'] or clock.left() <= 0:
            why = 'aborted by a signal' if stop['signal'] else 'deadline'
            return unresolved(why + (f' (last: {last["detail"]})' if last and last.get('locked') else ''))
        if lock_since is not None:
            wait = waited + time.monotonic() - lock_since
            res['waitedS'] = round(wait)
            if wait >= lock_wait:
                return unresolved(f'locked past the {lock_wait:g}s lock wait: {last["detail"]}')
        run_dir = out / board / f'run-{len(res["attempts"]) + 1}'
        log(f'{board}: attempt {len(res["attempts"]) + 1} ({run_dir})')
        t = time.monotonic()
        code, row = battery(tree, board, run_dir, roster)
        a = attempt_entry(run_dir, code, row, time.monotonic() - t)
        res['attempts'].append(a)
        log(f'{board}: {a["verdict"]} {a["detail"]}')
        if f := a.get('previousCleanupFailure'):
            log(f'{board}: an earlier task\'s cleanup failed: {H.A.lock.failure_text(f)}'[:300])
        if a.get('previousPending'):
            log(f'{board}: {a["previousPending"]}'[:300])
        if a.get('locked'):
            lock_since = t if lock_since is None else lock_since
            nap = min(LOCK_POLL, lock_wait - waited - (time.monotonic() - lock_since), clock.left())
            while nap > 0 and not stop['signal']:
                time.sleep(min(1.0, nap))
                nap -= 1.0
            continue
        if lock_since is not None:   # admitted: the wait ends where this attempt started
            waited += t - lock_since
            res['waitedS'], lock_since = round(waited), None
        if a['verdict'] == 'fail' and failed is None:
            failed = a
            continue
        if failed and a['verdict'] == 'not-run':   # the retry was refused, not run
            return unresolved(a['detail'])
        res['flaky'] = bool(failed) and a['verdict'] == 'pass'
        return finish(res, a['verdict'], a['detail'])


def new_result(board):
    return {'board': board, 'attempts': [], 'flaky': False, 'retry': '', 'waitedS': 0}


def finish(res, verdict, detail, src=None):
    """res with its verdict, and the tests, cleanup and firmware of `src` (default: its last attempt that ran); an
    earlier task's failed cleanup or pending children from the first attempt reporting them, which a passing restore
    may have resolved before a later one ran."""
    src = src or next((a for a in reversed(res['attempts']) if 'tests' in a and not a.get('locked')), None)
    res.update(verdict=verdict, detail=detail, tests=src['tests'] if src else [],
               cleanup=src['cleanup'] if src else None, firmware=src['firmware'] if src else None,
               runS=sum(a['seconds'] for a in res['attempts']))
    earlier = next((a for a in res['attempts'] if any(k in a for k in EARLIER)), {})
    res.update({k: earlier[k] for k in EARLIER if k in earlier})
    return res


def prune(state, days, keep):
    """Remove state's <run>-<attempt> dirs (not keep) older than `days` whose summary shows every board finished
    clean; a dir without one, or with a blocker, is kept whatever its age. Never follows a link."""
    removed, cutoff = [], time.time() - days * 86400
    for d in sorted(Path(state).iterdir()):
        if d == keep or d.is_symlink() or not d.is_dir() or not RUN_NAME.fullmatch(d.name):
            continue
        summary = d / SUMMARY
        try:
            if summary.is_symlink() or not summary.is_file():
                raise ValueError('no summary of its own')
            s = json.loads(summary.read_text())
            done = (s.get('kind') == SUMMARY_KIND and s.get('termination') == 'completed' and
                    all(b.get('verdict') in FINISHED for b in s.get('boards', [])))
        except (OSError, ValueError, AttributeError, TypeError):
            done = False
        try:
            if done and d.stat().st_mtime < cutoff:
                shutil.rmtree(d)
                removed.append(d.name)
        except FileNotFoundError:   # a run sharing the state dir pruned it first
            pass
        except OSError as e:        # a stuck dir must not stop the boards' run
            log(f'prune: {d}: {e}')
    return removed


def cmd_run(a):
    roster_rel = R.rel(a.roster)
    roster = H.load_roster(P.ROOT / roster_rel)
    boards = a.board or sorted(roster)
    unknown = [b for b in boards if b not in roster]
    if unknown:
        raise Refused(f'not in {roster_rel}: {unknown}')
    out = Path(a.out).resolve()
    if not RUN_NAME.fullmatch(out.name):
        raise Refused(f'{out}: the run dir is named <run id>-<attempt>')
    out.mkdir(parents=True, exist_ok=False)
    pruned = prune(out.parent, a.prune_days, out)
    clock, stop = Clock(a.deadline), {'signal': False}

    def on_signal(signum, frame):   # no new attempt starts; the one running ends as its battery does on a signal
        stop['signal'] = True
    for s in (signal.SIGINT, signal.SIGTERM):
        signal.signal(s, on_signal)
    results = []
    for b in boards:
        if stop['signal']:
            results.append(finish(new_result(b), 'not-run', 'not run: aborted by a signal'))
            continue
        try:
            admit_tree(Path(a.hosted) / b, b, a.head, a.run_id, roster_rel)
            tree = out / b / 'tree'
            tree.parent.mkdir()   # the parent first: a rename, not move()'s copy fallback
            shutil.move(Path(a.hosted) / b, tree)
        except (Refused, OSError) as e:
            results.append(finish(new_result(b), 'not-run', f'not run: {e}'))
            continue
        results.append(run_board(b, tree, out, roster_rel, clock, a.lock_wait, stop))
    if stop['signal']:   # the `hil` run rules: an aborted run leaves every requested board a blocker
        for r in results:
            if r['verdict'] != 'blocker':
                r.update(verdict='blocker', detail=f'run aborted by a signal ({r["verdict"]}: {r["detail"]})')
    code = 2 if any(r['verdict'] == 'blocker' for r in results) else \
        0 if results and all(r['verdict'] == 'pass' for r in results) else 1
    summary = {'kind': SUMMARY_KIND, 'version': 1, 'head': a.head, 'runId': a.run_id, 'attempt': a.attempt,
               'roster': roster_rel, 'termination': 'aborted' if stop['signal'] else 'completed',
               'deadlineS': a.deadline, 'elapsedS': round(clock.elapsed()), 'exit': code, 'pruned': pruned,
               'boards': results}
    R.core.write_json(out / SUMMARY, summary)
    emit(summary)
    return code


def parse(argv):
    ap = argparse.ArgumentParser(description=__doc__.split('\n')[0])
    sub = ap.add_subparsers(dest='cmd', required=True)
    s = sub.add_parser('setup')
    s.add_argument('--board', required=True)
    s.add_argument('--ci', required=True, help='the adafruit/ci-arduino checkout')
    s.add_argument('--versions', help='write the platform and arduino-cli and core versions (JSON) here')
    p = sub.add_parser('pack')
    p.add_argument('--board', required=True)
    p.add_argument('--record', required=True)
    p.add_argument('--out', required=True)
    p.add_argument('--run-id', required=True)
    p.add_argument('--attempt', required=True, type=int)
    p.add_argument('--roster', default=P.REMOTE_ROSTER)
    r = sub.add_parser('run')
    r.add_argument('--hosted', required=True, help='the dir holding each board\'s packed tree')
    r.add_argument('--head', required=True)
    r.add_argument('--run-id', required=True)
    r.add_argument('--attempt', required=True, type=int)
    r.add_argument('--out', required=True, help='<state>/<run id>-<attempt>, created')
    r.add_argument('-b', '--board', action='append')
    r.add_argument('--lock-wait', type=float, default=LOCK_WAIT)
    r.add_argument('--deadline', type=float, default=DEADLINE)
    r.add_argument('--prune-days', type=float, default=PRUNE_DAYS)
    r.add_argument('roster')
    return ap.parse_args(argv)


def run(profile, argv=None):
    """The library's hil_ci.py entry point: bind `profile`, then main()."""
    bind(profile)
    return main(argv)


def main(argv=None):
    a = parse(sys.argv[1:] if argv is None else argv)
    try:
        return {'setup': cmd_setup, 'pack': cmd_pack, 'run': cmd_run}[a.cmd](a)
    except (Refused, H.Refused, R.core.RemoteError) as e:
        emit({'pass': False, 'error': str(e).removeprefix('error: ')})
        return 2
