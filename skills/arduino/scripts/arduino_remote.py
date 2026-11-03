#!/usr/bin/env python3
"""Run a library's Arduino HIL harness on a remote rig (default ci.lan) from the dev PC that built the firmware
(agentrc `arduino` skill).

The rig gets binaries only: the library's contract files, the roster, the build records with their images and the
agentrc closure the harness loads, each pinned by digest in manifest.json, in a create-only attempt dir
<P.REMOTE_BASE>/<run-id> (agentrc hil_remote_core). Local records and collections live under .hil-remote/.

A library's `hil_remote.py` calls run(profile, argv) with its HIL profile (arduino_hil.py), which adds:

  REMOTE_BASE    the rig dir attempts go under, one per library (~/hil-remote/<library>)
  REMOTE_ROSTER  the roster `run` and `session open` default to
  HIL            the library's hil_test.py, checkout-relative (what runs on the rig)

The usage and exits are the library's HIL SKILL.md (Execution).
"""

import argparse
import importlib.util
import json
import os
import secrets
import sys
import tempfile
import time
from pathlib import Path

HERE = os.path.dirname(os.path.abspath(__file__))
FILES = ('skills/arduino/scripts/arduino_build.py', 'skills/arduino/scripts/arduino_hil.py', 'skills/hil/boards.json',
         'skills/hil/scripts/esptool_session.py', 'skills/hil/scripts/hil_boards.py', 'skills/hil/scripts/hil_flash.py',
         'skills/hil/scripts/hil_load.py',
         'skills/hil/scripts/hil_lock.py', 'skills/hil/scripts/hil_openocd_esp.py',
         'skills/hil/scripts/hil_openocd_esp_receipt.py', 'skills/hil/scripts/hil_process.py',
         'skills/hil/scripts/hil_remote_core.py', 'skills/hil/scripts/hil_report_core.py',
         'skills/hil/scripts/hil_report_notice.py', 'skills/hil/scripts/pyocd/run_pyocd.py',
         'skills/hil/scripts/pyocd/user_script.py', 'skills/hil/scripts/usb_observe.py',
         'skills/target-debug/scripts/esp_session.py')
SELF = 'skills/arduino/scripts/arduino_remote.py'
RUN_DIR = 'hil-run'
MANIFEST = 'manifest.json'
SESSION_ARTIFACTS = 'build/hil-manual/'   # every session command's run dir
RESTORE_BUNDLE = 'bundles/restore'   # the clean record session close restores from, staged at open
SESSION_CMDS = ('flash', 'gdb', 'restore', 'exec', 'console', 'check')
OWN_OPTS = ('-b', '--board', '--under-hold', '--staged')


def _hil_load():
    spec = importlib.util.spec_from_file_location('agentrc_hil_load', os.path.join(HERE, '..', '..', 'hil',
                                                                                    'scripts', 'hil_load.py'))
    if spec.name not in sys.modules:
        mod = importlib.util.module_from_spec(spec)
        spec.loader.exec_module(mod)
        sys.modules.setdefault(spec.name, mod)
    return sys.modules[spec.name].load(spec.name, os.path.realpath(spec.origin))


hil_load = _hil_load()
hil_load.require(__file__, FILES)
core = hil_load.load_file('skills/hil/scripts/hil_remote_core.py')
H = hil_load.load_file('skills/arduino/scripts/arduino_hil.py')
B = H.B
P = None   # the bound HIL profile


def bind(profile):
    global P
    H.bind(profile)
    P = profile
    return sys.modules[__name__]


def runs():
    return P.ROOT / '.hil-remote'


def rel(path):
    p = Path(path).resolve()
    try:
        return p.relative_to(P.ROOT).as_posix()
    except ValueError:
        core.fail(f'{path} is not inside the checkout {P.ROOT}')


def head_of_clean_tree():
    head = core.git(P.ROOT, 'rev-parse', 'HEAD')
    dirty = core.git(P.ROOT, 'status', '--porcelain', '--untracked-files=normal')
    if dirty:
        core.fail(f'the tree is not clean, so the staged harness would not be HEAD\'s:\n{dirty}')
    return head


def record_files(path, head, board, clean=True):
    """The checkout-relative files a build record stages (itself, its images, an experimental record's patch),
    refusing one the rig would refuse (H.record_problems)."""
    try:
        rec = json.loads(Path(path).read_text())
        why = H.record_problems(rec, board, head, not clean, 'HEAD')
        files = [rel(path), *(f for s in rec['sketches'].values() for f in s['sha256'])]
    except (OSError, ValueError, KeyError, TypeError) as e:
        core.fail(f'build record {path}: {e!r}')
    if why:
        core.fail(f'build record {path}: {"; ".join(why)}; rebuild with the build skill\'s --record')
    if rec.get('patch'):
        files.append(rec['patch']['path'])
    return rec, files


def snapshot(into, files, with_agentrc=False, bundle=None):
    """Copy files (checkout-relative) into `into`, under `bundle` when given, and, when asked, the agentrc closure
    the harness loads (under agentrc/); {tree-relative path: sha256} of the copies, which is what gets staged."""
    digests = {f'{bundle}/{k}' if bundle else k: v
               for k, v in core.snapshot(P.ROOT, [P.ROOT / f for f in files], Path(into) / (bundle or '')).items()}
    if with_agentrc:
        ag = Path(B.P.AGENTRC['root'])
        closure = B.P.AGENTRC['files']
        digests.update({f'agentrc/{k}': v for k, v in
                        core.snapshot(ag, [ag / f for f in closure], Path(into) / 'agentrc').items()})
    return digests


def snapshot_records(into, records, bundle=None):
    """snapshot() of the records' files, refusing any whose copy no longer hashes as its record pins."""
    digests = snapshot(into, [f for _, fs in records for f in fs], bundle=bundle)
    for rec, _ in records:
        pinned = {f: d for s in rec['sketches'].values() for f, d in s['sha256'].items()}
        if rec.get('patch'):
            pinned[rec['patch']['path']] = rec['patch']['sha256']
        bad = [f for f, d in pinned.items() if digests.get(f'{bundle}/{f}' if bundle else f) != d]
        if bad:
            core.fail(f'{rec["board"]}: {", ".join(bad[:3])} changed since the build record; rebuild')
    return digests


def stage(tree, into, digests):
    core.stage_tree(tree, into)
    bad = core.verify_remote(tree, digests)
    if bad:
        core.fail(f'staged files differ on {tree.host}: {", ".join(bad[:5])}')


def staged_tree(run_id, host, roster, records, head, bundle=None):
    """Create attempt run_id on host holding the harness, roster, agentrc closure and records (record_files
    already checked; under `bundle` when given), the snapshot pinned by manifest.json; its Tree."""
    with tempfile.TemporaryDirectory(prefix='hil-remote-') as tmp:
        into = Path(tmp)
        digests = snapshot(into, [*P.BUILD.CONTRACT_FILES, roster], True)
        digests.update(snapshot_records(into, records, bundle))
        drift = core.checkout_drift(P.ROOT, head)
        if drift:
            core.fail(f'the checkout moved while it was copied: {"; ".join(drift)}')
        core.write_json(into / MANIFEST, {'head': head, 'files': digests})
        tree = core.create_attempt(P.ROOT, runs(), run_id, host, P.REMOTE_BASE)
        stage(tree, into, {**digests, MANIFEST: core.digest(into / MANIFEST)})
    return tree


def roster_boards(roster):
    try:
        return H.load_roster(P.ROOT / roster)
    except H.Refused as e:
        core.fail(str(e))


def cmd_run(a):
    roster = rel(a.roster)
    try:
        names = list(H.selection(roster_boards(roster), a.board, a.test))
    except H.Refused as e:
        core.fail(f'{roster}: {e}')
    head = head_of_clean_tree()
    records = [record_files(P.ROOT / 'build' / 'hil' / f'{b}.build.json', head, b) for b in names]
    tree = staged_tree(a.run_id, a.host, roster, records, head)
    argv = ['python3', '-u', P.HIL, 'run', '--staged', MANIFEST, '--run-dir', RUN_DIR,
            *(w for b in names for w in ('-b', b)), *(w for t in a.test or [] for w in ('-t', t)), roster]
    launch = core.launch(tree, argv)
    print(json.dumps({'runId': a.run_id, 'host': tree.host, 'dir': tree.dir, 'boards': names, 'launch': launch,
                      'next': f'hil_remote.py wait {a.run_id}'}))
    return 0


def report_present(path):
    return [] if (Path(path) / RUN_DIR / 'hil_report.json').is_file() else [f'{RUN_DIR}/hil_report.json']


def summary(run_id, st, dest):
    done = Path(dest) / core.RECORDS / 'done.json'
    out = {'runId': run_id, 'state': st.get('state'), 'dest': str(dest)}
    if done.is_file():
        out['rigExit'] = json.loads(done.read_text()).get('exit')
    if (Path(dest) / RUN_DIR / 'hil_report.json').is_file():
        out['report'] = f'python3 {P.HIL} report {Path(dest) / RUN_DIR}'
    return out


def cmd_wait(a):
    tree = core.load_attempt(P.ROOT, runs(), a.run_id)
    dest = runs() / a.run_id
    rc, st = core.wait_attempt(tree, a.timeout, dest=dest, groups=[[f'{RUN_DIR}/']], accept=report_present)
    out = summary(a.run_id, st, dest)
    if 'collection' in st:
        out['collection'] = st['collection']['state']
    print(json.dumps(out))
    return rc


def cmd_collect(a):
    tree = core.load_attempt(P.ROOT, runs(), a.run_id)
    dest = runs() / a.run_id
    session = session_record(a.run_id, required=False)
    if session:
        record = core.collect(tree, dest, optional=[SESSION_ARTIFACTS])
        # a session never launches, so it is never `complete`: it needs only every member fetched or absent
        ok = all(record['outcomes'].get(m) in ('ok', 'absent') for m in (*core.RECORD_FILES, SESSION_ARTIFACTS))
    else:
        record = core.collect(tree, dest, groups=[[f'{RUN_DIR}/']], accept=report_present)
        ok = record['state'] == 'complete'
    print(json.dumps({**summary(a.run_id, {'state': record['remoteState']}, dest), 'collection': record['state']}))
    return 0 if ok else 1


# ---- sessions

def session_path(run_id):
    core.check_run_id(run_id)
    return runs() / f'{run_id}.session.json'


def session_record(run_id, required=True):
    p = session_path(run_id)
    if not p.is_file():
        if required:
            core.fail(f'no session {run_id} was opened from this checkout')
        return None
    return json.loads(p.read_text())


def held_session(run_id):
    s = session_record(run_id)
    if not s.get('held'):
        core.fail(f'session {run_id} does not hold {s["board"]} (its hold was never taken, or it is closed)')
    return s


def rig(tree, argv):
    return core.run_remote(tree, ['python3', '-u', *argv])


def cmd_session_open(a):
    roster = rel(a.roster)
    if a.board not in roster_boards(roster):
        core.fail(f'{a.board} is not in {roster}')
    head = head_of_clean_tree()
    record = record_files(P.ROOT / 'build' / 'hil' / f'{a.board}.build.json', head, a.board)
    tree = staged_tree(a.run_id, a.host, roster, [record], head, RESTORE_BUNDLE)
    # unique per attempt: the rig admits a session command by its reason alone, and run ids repeat across libraries
    reason = f'arduino-hil session {tree.dir} {secrets.token_hex(8)}'
    data = {'runId': a.run_id, 'board': a.board, 'reason': reason, 'roster': roster, 'head': head,
            'restoreRecord': record[1][0], 'held': False}
    core.write_json(session_path(a.run_id), data)
    rc = rig(tree, [P.HIL, 'lock', 'hold', a.board, '--reason', reason])
    if rc:
        print(json.dumps({**data, 'error': f'the hold was not taken (exit {rc}): the session acts under none; '
                                           f'`session close {a.run_id}` releases one the rig took anyway (a dropped '
                                           f'connection) under this reason, with no restore'}))
        return 1
    data['held'] = True
    core.write_json(session_path(a.run_id), data)
    print(json.dumps({**data, 'dir': tree.dir, 'next': f'hil_remote.py session exec {a.run_id} ...; '
                                                      f'hil_remote.py session close {a.run_id}'}))
    return 0


def cmd_session_stage(a):
    """Stage the records into a new bundle dir of their own: records share build/hil/<board>/<sketch>/, so one
    staged over another would replace the images an earlier record, the restore's included, pins."""
    s = session_record(a.run_id)
    tree = core.load_attempt(P.ROOT, runs(), a.run_id)
    records = [record_files(r, s['head'], s['board'], clean=False) for r in a.records]
    bundle = f'bundles/{time.strftime("%Y%m%d-%H%M%S")}-{secrets.token_hex(4)}'   # unique: stages may run at once
    with tempfile.TemporaryDirectory(prefix='hil-remote-') as tmp:
        digests = snapshot_records(Path(tmp), records, bundle)
        stage(tree, Path(tmp), digests)
    print(json.dumps({'runId': a.run_id, 'bundle': bundle,
                      'use': [f'--bundle {bundle} --build-record {fs[0]}' for _, fs in records]}))
    return 0


def session_argv(s, cmd, opts, argv):
    clash = [o for o in opts if o.split('=')[0] in OWN_OPTS]
    if clash:
        core.fail(f'{", ".join(clash)} come from the session; leave them out')
    if cmd == 'exec' and not argv:
        core.fail('session exec ID exec needs `-- ARGV...`')
    return [P.HIL, cmd, '-b', s['board'], '--under-hold', s['reason'], '--staged', MANIFEST, *opts, s['roster'],
            *(['--', *argv] if cmd == 'exec' else [])]


def cmd_session_exec(run_id, rest):
    s = held_session(run_id)
    if not rest or rest[0] not in SESSION_CMDS:
        core.fail(f'session exec ID takes one of {", ".join(SESSION_CMDS)}, then its options')
    cmd, rest = rest[0], rest[1:]
    opts, argv = (rest[:rest.index('--')], rest[rest.index('--') + 1:]) if '--' in rest else (rest, [])
    tree = core.load_attempt(P.ROOT, runs(), run_id)
    return rig(tree, session_argv(s, cmd, opts, argv))


def close_unheld(run_id, s):
    """A hold whose `held:` never reached us may still be taken (ssh dropped after the holder's ack): the rig releases
    one recorded under this attempt's own reason, never another session's. Nothing ran under it, so nothing is
    restored; the record stays `held: false`."""
    tree = core.load_attempt(P.ROOT, runs(), run_id)
    rc = rig(tree, [P.HIL, 'release', '-b', s['board'], '--under-hold', s['reason']])
    out = {'runId': run_id, 'board': s['board'], 'closed': rc in (0, H.NOT_HELD), 'restoreExit': None,
           'releaseExit': rc}
    if out['closed']:
        core.write_json(session_path(run_id), {**s, 'closed': True})
    out['note'] = ("released the hold the rig took under this session's reason; nothing was restored" if rc == 0 else
                   'held no hold: nothing was held, released or restored' if rc == H.NOT_HELD else
                   'not closed: the release did not complete (its JSON above, or no answer from the rig); rerun '
                   'session close')
    print(json.dumps(out))
    return 0 if out['closed'] else rc


def released_on_rig(tree, board):
    """The run dir of a restore in this session's tree that released the hold, or None: its result.json outlives a
    dropped ssh session, and the tree's every session command ran under the session's one reason."""
    with tempfile.TemporaryDirectory(prefix='hil-remote-') as tmp:
        if core.copy_back(tree, tmp, optional=[SESSION_ARTIFACTS]).outcomes.get(SESSION_ARTIFACTS) != 'ok':
            return None
        for f in sorted((Path(tmp) / SESSION_ARTIFACTS).glob(f'{board}-restore-*/result.json')):
            try:
                r = json.loads(f.read_text())
            except ValueError:
                continue
            if isinstance(r, dict) and r.get('pass') and (r.get('released') or {}).get('exit') == 0:
                return f.parent.name
    return None


def cmd_session_close(a):
    s = session_record(a.run_id)
    if not s.get('held'):
        return close_unheld(a.run_id, s)
    tree = core.load_attempt(P.ROOT, runs(), a.run_id)
    rc = rig(tree, session_argv(s, 'restore', ['--bundle', RESTORE_BUNDLE, '--build-record', s['restoreRecord'],
                                               '--release'], []))
    released = rc != 0 and released_on_rig(tree, s['board'])
    closed = rc == 0 or bool(released)
    core.write_json(session_path(a.run_id), {**s, 'held': not closed, 'closed': closed})
    out = {'runId': a.run_id, 'board': s['board'], 'closed': closed, 'restoreExit': rc}
    if released:
        out['note'] = f'the rig restored and released the board ({released}/result.json); its answer was lost'
    elif rc:
        out['note'] = 'not released: the restore or the release failed (its JSON above); recover under the hold'
    print(json.dumps(out))
    return 0 if closed else rc


def parse(argv):
    ap = argparse.ArgumentParser(description=__doc__.split('\n')[0])
    sub = ap.add_subparsers(dest='cmd', required=True)
    r = sub.add_parser('run')
    r.add_argument('--run-id', required=True)
    r.add_argument('--host', default='ci.lan')
    r.add_argument('--roster', default=str(P.ROOT / P.REMOTE_ROSTER))
    r.add_argument('-b', '--board', action='append')
    r.add_argument('-t', '--test', action='append', choices=sorted(P.TESTS))
    w = sub.add_parser('wait')
    w.add_argument('run_id')
    w.add_argument('--timeout', type=lambda t: core.wait_secs(t), default=570)
    c = sub.add_parser('collect')
    c.add_argument('run_id')
    s = sub.add_parser('session').add_subparsers(dest='session', required=True)
    o = s.add_parser('open')
    o.add_argument('--run-id', required=True)
    o.add_argument('-b', '--board', required=True)
    o.add_argument('--host', default='ci.lan')
    o.add_argument('--roster', default=str(P.ROOT / P.REMOTE_ROSTER))
    st = s.add_parser('stage')
    st.add_argument('run_id')
    st.add_argument('records', nargs='+')
    s.add_parser('exec', help='session exec ID flash|gdb|restore|exec|console|check ... (H.py parses it on '
                              'the rig)')
    cl = s.add_parser('close')
    cl.add_argument('run_id')
    return ap.parse_args(argv)


def run(profile, argv=None):
    """The library's hil_remote.py entry point: bind `profile` (its HIL profile), then main()."""
    bind(profile)
    return main(argv)


def main(argv=None):
    argv = sys.argv[1:] if argv is None else argv
    if argv[:2] == ['session', 'exec']:   # its options are H.py's, `--` included: not ours to parse
        if len(argv) < 3:
            core.fail('session exec needs a session id')
        return cmd_session_exec(argv[2], argv[3:])
    a = parse(argv)
    core.check_run_id(a.run_id)
    if a.cmd == 'session':
        return {'open': cmd_session_open, 'stage': cmd_session_stage, 'close': cmd_session_close}[a.session](a)
    return {'run': cmd_run, 'wait': cmd_wait, 'collect': cmd_collect}[a.cmd](a)
