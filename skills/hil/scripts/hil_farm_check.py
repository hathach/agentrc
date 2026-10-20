#!/usr/bin/env python3
"""Farm check engine shared by the HIL projects (`farm-check` skill). Linux only, stdlib only.

For every board of the host's roster: is its flash probe on the bus, does a light image
flash, does the board come back, and is it left in the profile's final state, verified? A
project's launcher passes its profile module to main(). No recovery: a wedged probe or board
is reported. Farm health only, never firmware-validation evidence.

Row statuses: ok; flash-failed (firmware not delivered or shown not delivered); failed (the
check ran but did not verify); locked (a live holder: skipped, never waited on, no bypass);
blocked (a recovery blocker). `coverage`: probe-only (--scan-only), skipped-locked,
full-attempted, blocked-before (an incoming recovery blocker, a live cleanup holder or a
stale protected record, refused before any mutation) or blocked-after (this check's cleanup
is unresolved; `check_status` keeps the check's own status). `verification` (the light image)
and `cleanup` (the final state) are reported apart.

Exit: 0 every exercised row ok; 1 any failed or flash-failed row, an aborted run, or a report
that could not be persisted; 2 refused before any board (usage, unknown host, profile, state
dir, report dir); 4 any blocked row, over 1. Read the document for detail, never $? alone.

Reservation: a board is taken with hil_lock.take_board_lock(lock, 'farm_check',
protected=True). It is released only when no mutating callback was entered and no process
started under it, or when every process started under it is proven ended
(hil_process.run_tracked's `done`, or a recheck of its receipt) and the profile verified the
final state. Nothing moves on while an earlier process is not proven ended: ctx.run refuses
a new command, and the engine runs neither the observation nor the final state. Otherwise
the board keeps a recovery blocker: while a process the check started survives, a cleanup
holder (`role: cleanup-holder`, this file run as `--hold` in a fresh interpreter, given the
lock's descriptor) keeps the flock, and the row names its token. `hil_lock.py release
--token` ends it only once every receipt it holds rechecks `done`; anything else is the
contract's Recovery. Every other outcome, a failed handoff to the holder included, leaves the
protected record on the free flock for a recovery takeover. A check killed outright leaves
its protected record the same way and prints no report. SIGINT stops the check after the
current board's cleanup; the report then says `termination: aborted`.

Profile (a module; `__file__` names it in the report, with its digest). Calls marked [lock]
run inside the reservation and never take a lock; a process any of them starts goes through
`ctx.run` (hil_flash's `run=`: hil_process.run_tracked with the engine's state dir), and an
exception from one keeps its `.operation`, which the row records. ctx.run raises
`Outstanding` instead of starting a command while an earlier one is not proven ended; a
profile lets it propagate. In-process board I/O (a UART open, poll or write) after any
ctx.run goes through ctx.settle() first, which raises Outstanding the same way.
ctx.record(operation) keeps an Operation on the row as soon as it completed, so a later
exception or interrupt in the same callback cannot lose it; one also returned or raised is
recorded once. ctx.flash_error_line(text) is the engine's reading of a failed flash's output.
observe_before is observation only and must not change the board;
every other [lock] call is taken to change it once entered, whether or not it ran a process.
The others take no lock.
  roster(host, config, args) -> {'path', 'boards': [dict], 'named_only': [dict]}: this host's
      permitted roster (`config` the CLI's CONFIG or None). Raises for an unknown host
      without an explicit roster, before any fetch, lock or flash. `named_only` boards are
      checked only when -b names them. Every board dict has a `name`.
  lock_name(board) -> str: the board's lock name.
  probe_candidates(board) -> [busport]: every probe matching the board under the project's
      constraints, best tier only; more than one is reported as ambiguous, never picked.
  scan_device(board) -> {'device': str, 'notes': [str]}: --scan-only's device cell.
  plan(board, args, prepared, leave_running) -> {'image': IMAGE, 'final': {'postcondition':
      str, 'image': IMAGE | None}, 'notes': [str]} or {'refused': {'status': 'flash-failed'
      | 'failed', 'notes': [str]}}. IMAGE is {'path', 'variant', 'label', 'provenance'};
      provenance is {'kind': 'ci', 'repo', 'commit', 'run', 'artifact', 'conclusion'},
      {'kind': 'local', 'receipt'} or {'kind': 'missing', 'reason'}. Any other key of the
      plan, or of an IMAGE, is the profile's own data: the engine passes the plan unchanged to
      flash, observe and final_state and reads nothing else from it.
  flash(ctx, board, plan) -> hil_flash Operation, or raises with `.operation` [lock]
  observe(ctx, board, plan, before) -> {'status': 'ok' | 'failed' | 'flash-failed', 'device':
      str, 'notes': [str], 'unverified': [str], 'evidence': [str]} [lock]
  final_state(ctx, board, plan, observation | None) -> {'verified': bool, 'evidence': [str],
      'notes': [str]} [lock]
  observe and final_state may add 'operations': the hil_flash Operations they ran.
  Optional: add_arguments(parser); prepare(boards, args, say) -> prepared (full mode, before
  any board; raising refuses); observe_before(ctx, board, plan) -> before [lock];
  LEAVE_RUNNING = True when final_state defines and verifies a "leave running" state, which
  --no-park requires.

main(profile, argv, lock_module=...) takes the project's own loaded hil_lock instance in
place of the engine's (module identity, test isolation); there is no lock-dir option.
"""

from __future__ import annotations

import argparse
import contextlib
import hashlib
import importlib.util
import io
import json
import os
import select
import secrets
import signal
import socket
import subprocess
import sys
import time
from pathlib import Path

HERE = os.path.dirname(os.path.abspath(__file__))


def _load(name):
    spec = importlib.util.spec_from_file_location(f'agentrc_{name}', os.path.join(HERE, f'{name}.py'))
    mod = importlib.util.module_from_spec(spec)
    spec.loader.exec_module(mod)
    return mod


hil_lock = _load('hil_lock')
hil_process = _load('hil_process')
report_core = _load('hil_report_core')

REASON = 'farm_check'
HOLDER = 'hil_farm_check'
SCHEMA_VERSION = 1
SETTLE = 2.0          # s run_tracked waits for an operation's descendants once it ended
RECHECK_WAIT = 5.0    # s an operation not yet proven ended gets before the board's cleanup is decided
HOLDER_READY = 10.0   # s the cleanup holder gets to record itself
STATUSES = ('ok', 'flash-failed', 'failed', 'locked', 'blocked')
MARKS = {'ok': '✅ ok', 'flash-failed': '❌ flash-failed', 'failed': '❌ failed', 'locked': '🔒 locked',
         'blocked': '⛔ blocked'}
REQUIRED = ('roster', 'lock_name', 'probe_candidates', 'scan_device', 'plan', 'flash', 'observe', 'final_state')
PROVENANCE = {'ci': ('repo', 'commit', 'run', 'artifact', 'conclusion'), 'local': ('receipt',),
              'missing': ('reason',)}

_t0 = time.monotonic()


class Refused(Exception):
    """Refused before any board: exit 2."""


def say(msg: str) -> None:
    print(f'[{time.monotonic() - _t0:6.1f}s] {msg}', file=sys.__stdout__, flush=True)


def _label(cmd) -> str:
    return cmd if isinstance(cmd, str) else ' '.join(map(str, cmd))


def _sha256(path) -> str:
    return hashlib.sha256(Path(path).read_bytes()).hexdigest()


def _strings(value, what) -> list:
    if not isinstance(value, list) or not all(isinstance(v, str) for v in value):
        raise ValueError(f'{what} must be a list of strings: {value!r}')
    return value


# --- profile --------------------------------------------------------------------------------
def profile_info(profile) -> dict:
    missing = [f for f in REQUIRED if not callable(getattr(profile, f, None))]
    if missing:
        raise Refused(f'profile lacks {", ".join(missing)}')
    path = getattr(profile, '__file__', None)
    try:
        return {'path': os.path.realpath(path), 'digest': _sha256(path)}
    except (TypeError, OSError) as e:
        raise Refused(f'profile file unreadable: {e}')


def _image(img, what) -> dict:
    if not isinstance(img, dict) or not all(isinstance(img.get(k), str) and img[k] for k in ('path', 'variant', 'label')):
        raise ValueError(f'{what}: want path, variant and label strings: {img!r}')
    prov = img.get('provenance')
    kind = prov.get('kind') if isinstance(prov, dict) else None
    if kind not in PROVENANCE or any(k not in prov for k in PROVENANCE[kind]):
        raise ValueError(f'{what}: provenance must be one of {sorted(PROVENANCE)} with its fields: {prov!r}')
    return img


def _plan(profile, board, args, prepared) -> dict:
    plan = profile.plan(board, args, prepared, args.no_park)
    if isinstance(plan, dict) and 'refused' in plan:
        r = plan['refused']
        if not isinstance(r, dict) or r.get('status') not in ('flash-failed', 'failed'):
            raise ValueError(f'plan refusal: want status flash-failed or failed: {r!r}')
        _strings(r.get('notes'), 'plan refusal notes')
        return plan
    if not isinstance(plan, dict) or not isinstance(plan.get('final'), dict) \
            or not isinstance(plan['final'].get('postcondition'), str):
        raise ValueError(f'plan: want image, final {{postcondition, image}} and notes: {plan!r}')
    _image(plan.get('image'), 'plan image')
    if plan['final'].get('image') is not None:
        _image(plan['final']['image'], 'plan final image')
    _strings(plan.get('notes'), 'plan notes')
    return plan


def _observation(obs) -> dict:
    if not isinstance(obs, dict) or obs.get('status') not in ('ok', 'failed', 'flash-failed') \
            or not isinstance(obs.get('device'), str):
        raise ValueError(f'observe: want status ok, failed or flash-failed and a device cell: {obs!r}')
    for k in ('notes', 'unverified', 'evidence'):
        _strings(obs.get(k), f'observe {k}')
    return obs


def _final(out) -> dict:
    if not isinstance(out, dict) or not isinstance(out.get('verified'), bool):
        raise ValueError(f'final_state: want verified (bool), evidence and notes: {out!r}')
    _strings(out.get('evidence'), 'final_state evidence')
    _strings(out.get('notes'), 'final_state notes')
    return out


# --- the reservation's processes ------------------------------------------------------------
class Outstanding(RuntimeError):
    """ctx.run refused to start a command: an earlier one is not proven ended."""
    cleanup, receipt = 'done', None    # it started nothing


def _note_cleanup(entry: dict, source) -> None:
    cleanup = getattr(source, 'cleanup', None)
    entry['cleanup'] = cleanup if cleanup in ('done', 'pending', 'unknown') else 'unknown'
    entry['receipt'] = getattr(source, 'receipt', None)


class Context:
    """What a profile call inside the reservation gets: `run` (call it as hil_flash's `run=`),
    `settle`, `record`, `flash_error_line`, `board`, `args` and `say`. `ledger` keeps every
    run's cleanup and receipt; a run is entered before it starts, so an interrupt mid-call
    still counts. `touched` is set before any callback that may change the board is entered;
    `step` names the callback running."""

    def __init__(self, board: dict, args, state_dir: str, row: dict):
        self.board, self.args, self.say = board, args, say
        self.state_dir = state_dir
        self.row = row
        self.ledger = []
        self.touched = False
        self.step = None
        self._recorded = []

    def open_runs(self) -> list:
        """The runs not proven ended, after rechecking them for at most RECHECK_WAIT."""
        _settle(self.ledger)
        return [e for e in self.ledger if e['cleanup'] != 'done']

    def settle(self) -> None:
        """Before in-process board I/O: raises Outstanding unless every run is proven ended."""
        if open_runs := self.open_runs():
            raise Outstanding(f'earlier runs not proven ended: {_evidence(open_runs)}')

    def record(self, op) -> None:
        """Keep `op` on the row under the current step, once however often it is given."""
        if op is None or any(o is op for o in self._recorded):
            return
        self._recorded.append(op)
        self.row['operations'].append(operation_record(op, self.step))

    @staticmethod
    def flash_error_line(text: str) -> str:
        return flash_error_line(text)

    def run(self, cmd, cwd=None, *, timeout, **kw) -> subprocess.CompletedProcess:
        if open_runs := self.open_runs():
            raise Outstanding(f'not started, earlier runs not proven ended: {_evidence(open_runs)}')
        entry = {'cmd': _label(cmd), 'cleanup': 'unknown', 'receipt': None}
        self.ledger.append(entry)
        try:
            r = hil_process.run_tracked(cmd, cwd, timeout=timeout, settle=SETTLE, state_dir=self.state_dir, **kw)
        except BaseException as e:
            _note_cleanup(entry, e)
            raise
        _note_cleanup(entry, r)
        return r


def _evidence(open_runs: list) -> str:
    return '; '.join(f'{e["cmd"]} ({e["cleanup"]})' for e in open_runs)


def _tidy(receipt: dict) -> None:
    for key in ('lock', 'record', 'log'):
        with contextlib.suppress(OSError, TypeError, KeyError):
            os.unlink(receipt[key])


def _settle(ledger: list) -> None:
    """Recheck every run not proven ended until all are or RECHECK_WAIT passed; a receipt
    proven done is removed, nothing else is."""
    deadline = time.monotonic() + RECHECK_WAIT
    while True:
        for e in ledger:
            if e['cleanup'] != 'done' and e['receipt'] is not None:
                e['cleanup'] = hil_process.recheck(e['receipt'])
                if e['cleanup'] == 'done':
                    _tidy(e['receipt'])
                    e['receipt'] = None
        if all(e['cleanup'] == 'done' for e in ledger) or time.monotonic() >= deadline:
            return
        time.sleep(0.1)


def operation_record(op, step: str) -> dict:
    attempts = getattr(op, 'attempts', ())
    return {'step': step, 'returncode': getattr(op, 'returncode', None), 'cleanup': getattr(op, 'cleanup', 'unknown'),
            'receipts': list(getattr(op, 'receipts', ())),
            'attempts': [{'cmd': _label(a.cmd), 'kind': a.kind, 'returncode': a.result.returncode,
                          'cleanup': a.cleanup, 'error': None if a.error is None else repr(a.error)[:200]}
                         for a in attempts]}


def flash_error_line(out: str) -> str:
    """Most informative line of a failed flash's output: last error-looking line,
    else the last non-empty one."""
    lines = [l.strip() for l in out.splitlines() if l.strip()]
    for l in reversed(lines):
        if any(k in l.lower() for k in ('error', 'fail', 'unknown', 'cannot', 'timeout',
                                        'no valid', 'not found', 'unable')):
            return l[:90]
    return lines[-1][:90] if lines else ''


# --- one board ------------------------------------------------------------------------------
def new_row(name: str, **cells) -> dict:
    return {'name': name, 'probe': '❌ missing', 'probe_busport': None, 'flash': '–', 'device': '–',
            'note': [], 'status': 'failed', 'image': None, 'operations': [], 'observations': [],
            'verification': None, 'cleanup': None, **cells}


def _image_record(img) -> dict | None:
    if img is None:
        return None
    return {'variant': img['variant'], 'label': img['label'], 'path': img['path'],
            'digest': _sha256(img['path']), 'provenance': dict(img['provenance'])}


def _probe(profile, board, row) -> str | None:
    cands = _strings(list(profile.probe_candidates(board)), 'probe candidates')
    if len(cands) == 1:
        row['probe'], row['probe_busport'] = f'✅ {cands[0]}', cands[0]
        return cands[0]
    if cands:
        row['probe'] = '⚠ ambiguous'
        row['note'].append(f'probe ambiguous: {", ".join(cands)} all match; none picked')
        say(f'{row["name"]:26} probe AMBIGUOUS ({", ".join(cands)})')
    else:
        say(f'{row["name"]:26} probe MISSING')
    return None


def check_board(profile, board: dict, args, state_dir, prepared=None) -> dict:
    name = board['name']
    row = new_row(name)
    note = row['note']
    probe = _probe(profile, board, row)

    if args.scan_only:
        scan = profile.scan_device(board)
        if not isinstance(scan, dict) or not isinstance(scan.get('device'), str):
            raise ValueError(f'scan_device: want a device cell: {scan!r}')
        note.extend(_strings(scan.get('notes'), 'scan_device notes'))
        row['device'] = scan['device']
        row['status'] = 'ok' if probe else 'flash-failed'
        if probe:
            say(f'{name:26} probe ✅ {probe}  {row["device"]}')
        return row
    if not probe:
        row['status'] = 'flash-failed'
        return row
    plan = _plan(profile, board, args, prepared)
    if 'refused' in plan:
        row['status'] = plan['refused']['status']
        note.extend(plan['refused']['notes'])
        return row
    note.extend(plan['notes'])
    images = [plan['image'], plan['final']['image']]
    gone = [img['path'] for img in images if img is not None and not os.path.isfile(img['path'])]
    if gone:
        # checked before the lock: a board flashed without its final image cannot be restored
        row['status'] = 'flash-failed'
        note.append(f'image missing: {", ".join(gone)}')
        return row
    row['image'] = _image_record(plan['image'])

    lock = profile.lock_name(board)
    try:
        fh = hil_lock.take_board_lock(lock, REASON, protected=True)
    except hil_lock.RecoveryBlocked as e:
        return _not_taken(row, 'blocked', str(e))
    except hil_lock.BoardLocked as e:
        live_holder = hil_lock.is_cleanup_holder(hil_lock.read_record(lock))
        return _not_taken(row, 'blocked' if live_holder else 'locked', str(e))
    except hil_lock.LockUnavailable as e:
        return _not_taken(row, 'failed', str(e))
    ctx = Context(board, args, state_dir, row)
    try:
        _reserved(profile, ctx, plan, row, fh, lock)
    except KeyboardInterrupt:
        row['interrupted'] = True
    # an interrupt that came before finalization could defer it: finalize from the same ledger
    while row['cleanup'] is None:
        try:
            _end_reservation(fh, lock, ctx, plan, row, {'result': 'interrupted', 'verified': False,
                                                          'evidence': ['interrupted before the cleanup was decided']})
        except KeyboardInterrupt:
            row['interrupted'] = True
    return row


def _not_taken(row: dict, status: str, why: str) -> dict:
    row['status'] = status
    row['flash'] = {'locked': '🔒 locked', 'blocked': '⛔ blocked'}.get(status, '❌ lock')
    row['note'].append(why)
    say(f'{row["name"]:26} {status}: {why}')
    return row


def _reserved(profile, ctx: Context, plan, row, fh, lock) -> None:
    final = {'result': 'not-run', 'verified': False, 'evidence': []}
    try:
        observation = _attempt(profile, ctx, plan, row)
        final = _final_state(profile, ctx, plan, row, observation)
    except KeyboardInterrupt:
        row['interrupted'] = True
    finally:
        _end_reservation(fh, lock, ctx, plan, row, final)


def _call(ctx: Context, step: str, fn, *a):
    """A profile callback; an exception's `.operation` goes on the row before it propagates."""
    ctx.step = step
    try:
        return fn(*a)
    except BaseException as e:
        ctx.record(getattr(e, 'operation', None))
        raise


def _operations(ctx: Context, out: dict) -> None:
    ops = out.get('operations', [])
    if not isinstance(ops, list):
        raise ValueError(f'{ctx.step}: operations must be a list: {ops!r}')
    for op in ops:
        ctx.record(op)


def _flash_failed(row: dict, label: str, why: str) -> None:
    row['note'].append(f'flash: {why}')
    row['flash'] = f'❌ {label}'
    row['status'] = 'flash-failed'
    row['verification'] = {'result': 'unverified', 'reasons': ['flash failed']}
    say(f'{row["name"]:26} flash FAILED ({label})')


def _attempt(profile, ctx: Context, plan: dict, row: dict) -> dict | None:
    """Flash and observe; the row records what happened and every exception stays here."""
    label = plan['image']['label']
    try:
        before = None
        if hasattr(profile, 'observe_before'):
            before = _call(ctx, 'observe_before', profile.observe_before, ctx, ctx.board, plan)
        if open_runs := ctx.open_runs():
            row['note'].append(f'flash not run, the observation is not proven ended: {_evidence(open_runs)}')
            row['verification'] = {'result': 'unverified', 'reasons': ['observation not proven ended']}
            return None
        ctx.touched = True
        try:
            op = _call(ctx, 'flash', profile.flash, ctx, ctx.board, plan)
        except Exception as e:
            _flash_failed(row, label, repr(e)[:90])
            return None
        ctx.record(op)
        if op.returncode != 0:
            out = hil_process.cmd_stdout_text(op.stdout) + '\n' + hil_process.cmd_stdout_text(op.stderr)
            _flash_failed(row, label, flash_error_line(out) or f'rc={op.returncode}')
            return None
        row['flash'] = f'✅ {label}'
        if open_runs := ctx.open_runs():
            row['note'].append(f'observation not run, the flash is not proven ended: {_evidence(open_runs)}')
            row['verification'] = {'result': 'unverified', 'reasons': ['flash not proven ended']}
            return None
        obs = _observation(_call(ctx, 'observe', profile.observe, ctx, ctx.board, plan, before))
        _operations(ctx, obs)
        row['status'], row['device'] = obs['status'], obs['device']
        row['note'].extend(obs['notes'])
        row['observations'].extend(obs['evidence'])
        reasons = list(obs['unverified']) + ([] if obs['status'] == 'ok' else [obs['status']])
        row['verification'] = {'result': 'unverified' if reasons else 'verified', 'reasons': reasons}
        say(f'{row["name"]:26} {row["flash"]}  {row["device"]}')
        return obs
    except KeyboardInterrupt:
        row['interrupted'] = True
        row['note'].append('interrupted')
    except Exception as e:
        row['note'].append(f'error: {e!r}'[:120])
        row['device'] = '❌ error'
    if row['status'] == 'ok':
        row['status'] = 'failed'
    row['verification'] = {'result': 'unverified', 'reasons': row['note'][-1:]}
    return None


def _final_state(profile, ctx: Context, plan: dict, row: dict, observation) -> dict:
    """The final state, run only once every earlier process is proven ended."""
    if not ctx.touched and not ctx.ledger:
        return {'result': 'untouched', 'verified': True, 'evidence': []}
    if open_runs := ctx.open_runs():
        return {'result': 'not-run', 'verified': False,
                'evidence': [f'not proven ended ({e["cleanup"]}): {e["cmd"]}' for e in open_runs]}
    try:
        out = _final(_call(ctx, 'final', profile.final_state, ctx, ctx.board, plan, observation))
        _operations(ctx, out)
    except KeyboardInterrupt:
        row['interrupted'] = True
        return {'result': 'interrupted', 'verified': False, 'evidence': ['interrupted']}
    except Exception as e:
        return {'result': 'error', 'verified': False, 'evidence': [repr(e)[:200]]}
    row['note'].extend(out['notes'])
    return {'result': 'verified' if out['verified'] else 'unverified', 'verified': out['verified'],
            'evidence': out['evidence']}


def _end_reservation(fh, lock: str, ctx: Context, plan: dict, row: dict, final: dict) -> None:
    """Release the board, or keep its recovery blocker; never raises. An interrupt is deferred
    until the decision is carried out, its waits all bounded, and then only marks the row."""
    hold = hil_process.InterruptHold()
    try:
        _finalize(fh, lock, ctx, plan, row, final)
    except Exception as e:
        _keep_blocked(row, fh, f'finalization failed: {e!r}'[:200])
    finally:
        try:
            hold.release()
        except KeyboardInterrupt:
            row['interrupted'] = True


def _keep_blocked(row: dict, fh, why: str) -> None:
    """A finalization cut short: unless the board was released, the row is blocked and the
    record, whatever it now says, stays the blocker."""
    cleanup = row['cleanup'] or {'postcondition': None, 'image': None, 'result': 'interrupted', 'evidence': [],
                                 'receipts': [], 'blocker': None, 'released': False}
    row['cleanup'] = cleanup
    if cleanup['released']:
        return
    if row['status'] != 'blocked':
        row['check_status'], row['status'] = row['status'], 'blocked'
    cleanup['blocker'] = cleanup['blocker'] or 'stale-record'
    cleanup['evidence'].append(why)
    with contextlib.suppress(OSError):
        fh.close()   # never clear_record: the record is the blocker


def _finalize(fh, lock: str, ctx: Context, plan: dict, row: dict, final: dict) -> None:
    _settle(ctx.ledger)
    open_ops = [e for e in ctx.ledger if e['cleanup'] != 'done']
    receipts = [e['receipt'] for e in open_ops if e['receipt'] is not None]
    cleanup = {'postcondition': plan['final']['postcondition'], 'image': None, 'result': final['result'],
               'evidence': list(final['evidence']), 'receipts': receipts, 'blocker': None, 'released': False}
    row['cleanup'] = cleanup
    with contextlib.suppress(OSError):
        cleanup['image'] = _image_record(plan['final']['image'])
    if final['verified'] and not open_ops:
        # proven clear through our own descriptor, while the flock is still ours
        if hil_lock.clear_record(fh) and os.fstat(fh.fileno()).st_size == 0:
            cleanup['released'] = True
            with contextlib.suppress(OSError):
                fh.close()
            return
        cleanup['result'] = 'release-failed'
        cleanup['evidence'].append('the record could not be cleared')
    row['check_status'], row['status'] = row['status'], 'blocked'
    detail = (f'farm check cleanup unresolved: {final["result"]}; ' + '; '.join(cleanup['evidence']))[:500]
    with contextlib.suppress(OSError):
        hil_lock.write_record(fh, REASON, protected=True, detail=detail, receipts=receipts)
    token = None
    if any(e['cleanup'] == 'pending' for e in open_ops):
        token, why = _cleanup_holder(fh, lock, detail, receipts)
        if why == 'interrupted':
            row['interrupted'] = True
        elif why:
            cleanup['evidence'].append(f'cleanup holder handoff failed: {why}')
    if token:
        cleanup['blocker'], cleanup['token'] = 'cleanup-holder', token
        row['note'].append(f'cleanup holder (token {token}) keeps the board for recovery, which ends it with '
                           f'`hil_lock.py release --token {token}`')
    else:
        cleanup['blocker'] = 'stale-record'
        row['note'].append(f'protected record kept: recovery takes it over (`hil_lock.py hold --take-over {lock}`)')
    with contextlib.suppress(OSError):
        fh.close()   # never clear_record: the record is the blocker
    say(f'{row["name"]:26} BLOCKED: {cleanup["blocker"]} ({final["result"]})')


def _holder_argv(job: dict) -> list:
    return [sys.executable, '-I', os.path.abspath(__file__), '--hold', json.dumps(job)]


def _read_line(fd: int, deadline: float) -> bytes:
    """One line from `fd` within `deadline`; b'' at EOF or past it."""
    buf = b''
    while not buf.endswith(b'\n'):
        left = deadline - time.monotonic()
        if left <= 0 or not select.select([fd], [], [], left)[0]:
            return b''
        chunk = os.read(fd, 4096)
        if not chunk:
            return b''
        buf += chunk
    return buf


def _cleanup_holder(fh, lock: str, detail: str, receipts: list) -> tuple:
    """Hand `fh`'s flock to a cleanup holder (`--hold`, a fresh interpreter given the lock's
    descriptor): (token, '') once its record names it, else (None, why) with the holder gone
    and the protected record, or a record naming the dead holder, left as the blocker. Every
    wait is bounded by HOLDER_READY; an interrupt ends the handoff the same way."""
    fds, proc = {}, None
    try:
        fds['hello_r'], fds['hello_w'] = os.pipe()
        fds['go_r'], fds['go_w'] = os.pipe()
        job = {'lock_fd': fh.fileno(), 'hello_fd': fds['hello_w'], 'go_fd': fds['go_r'], 'receipts': receipts,
               'wait': HOLDER_READY}
        proc = subprocess.Popen(_holder_argv(job), stdin=subprocess.DEVNULL, stdout=subprocess.DEVNULL,
                                stderr=subprocess.DEVNULL, pass_fds=(fh.fileno(), fds['hello_w'], fds['go_r']),
                                start_new_session=True)
        for k in ('hello_w', 'go_r'):
            os.close(fds.pop(k))
        line = _read_line(fds['hello_r'], time.monotonic() + HOLDER_READY)
        try:
            hello = json.loads(line)
            pid, start = hello['pid'], hello['start']
        except (ValueError, TypeError, KeyError):
            raise RuntimeError(f'no readiness within {HOLDER_READY}s: {line[:100]!r}') from None
        if pid != proc.pid or start is None or hil_lock.proc_start(pid) != start:
            raise RuntimeError(f'readiness from pid {pid} is not the holder started as {proc.pid}')
        token = secrets.token_hex(8)
        hil_lock.write_record(fh, REASON, protected=True, holder=HOLDER, role=hil_lock.CLEANUP_ROLE, detail=detail,
                              receipts=receipts, pid=pid, start=start, token=token, locks=[lock])
        os.write(fds['go_w'], b'1')
        if _read_line(fds['hello_r'], time.monotonic() + HOLDER_READY) != b'held\n':
            raise RuntimeError('the holder did not confirm the handoff')
        return token, ''
    except BaseException as e:
        if proc is not None:
            with contextlib.suppress(OSError):
                proc.kill()
            with contextlib.suppress(subprocess.TimeoutExpired, KeyboardInterrupt):
                proc.wait(timeout=HOLDER_READY)
        return None, 'interrupted' if isinstance(e, KeyboardInterrupt) else repr(e)[:200]
    finally:
        for fd in fds.values():
            with contextlib.suppress(OSError):
                os.close(fd)


def _hold(job: dict) -> int:
    """The cleanup holder: report readiness, then keep the inherited flock once the engine has
    recorded it as the holder, and confirm. Never handed over within `wait`, it exits, dropping
    its copy.
    SIGUSR1 (release_token) ends it only once every receipt rechecks `done`."""
    for sig in (signal.SIGINT, signal.SIGTERM, signal.SIGHUP):
        signal.signal(sig, signal.SIG_IGN)
    fh = os.fdopen(job['lock_fd'], 'r+')
    receipts = job['receipts']

    def bow_out(*_):
        if all(hil_process.recheck(r) == 'done' for r in receipts):
            hil_lock.release_board_lock(fh)
            os._exit(0)

    signal.signal(signal.SIGUSR1, bow_out)
    pid = os.getpid()
    os.write(job['hello_fd'], json.dumps({'pid': pid, 'start': hil_lock.proc_start(pid)}).encode() + b'\n')
    ready = select.select([job['go_fd']], [], [], job['wait'])[0]
    if not ready or os.read(job['go_fd'], 1) != b'1':
        return 1
    os.write(job['hello_fd'], b'held\n')
    os.close(job['hello_fd'])
    os.close(job['go_fd'])
    while True:
        signal.pause()


# --- the farm -------------------------------------------------------------------------------
def check_board_safe(profile, board, args, state_dir, prepared=None) -> dict:
    """Isolate one board's exceptions: a crashing board must not discard every other row."""
    try:
        return check_board(profile, board, args, state_dir, prepared)
    except Exception as e:
        name = board.get('name', '?') if isinstance(board, dict) else '?'
        say(f'{name:26} INTERNAL ERROR: {e!r}')
        return new_row(name, probe='–', device='❌ error', note=[repr(e)[:120]])


def check_farm(profile, boards: list, args, *, state_dir, prepared=None) -> tuple:
    """(rows, aborted): one row per board in order; SIGINT ends the loop after the board in
    hand, whose cleanup has run."""
    rows = []
    # silence the runner's COMMAND FAILED dumps; say() writes to sys.__stdout__
    with contextlib.nullcontext() if args.verbose else contextlib.redirect_stdout(io.StringIO()):
        for b in boards:
            try:
                rows.append(check_board_safe(profile, b, args, state_dir, prepared))
            except KeyboardInterrupt:
                return rows, True
            if rows[-1].get('interrupted'):
                return rows, True
    return rows, False


def _coverage(row: dict, mode: str) -> str:
    if mode == 'scan-only':
        return 'probe-only'
    if row['status'] == 'locked':
        return 'skipped-locked'
    if row['status'] == 'blocked':
        return 'blocked-after' if 'check_status' in row else 'blocked-before'
    return 'full-attempted'


def farm_document(host: str, config: str, mode: str, rows: list, profile: dict, elapsed_s: float) -> dict:
    """The one result the table, the JSON and a persisted report all come from."""
    counts = dict.fromkeys(STATUSES, 0)
    for r in rows:
        counts[r['status']] += 1
        r['coverage'] = _coverage(r, mode)
    return {'schema_version': SCHEMA_VERSION, 'check': 'farm-check', 'host': host, 'config': config,
            'mode': mode, 'profile': profile, 'rows': rows, 'counts': counts, 'elapsed_s': round(elapsed_s, 1)}


def exit_status(doc: dict, aborted: bool = False) -> int:
    c = doc['counts']
    if c['blocked']:
        return 4
    return 1 if c['failed'] or c['flash-failed'] or aborted else 0


def render_table(doc: dict) -> str:
    headers = ['Board', 'Probe', 'Flash', 'Device', 'Status', 'Note']
    cells = [[r['name'], r['probe'], r['flash'], r['device'], MARKS.get(r['status'], r['status']),
              '; '.join(r['note'])] for r in doc['rows']]
    c = doc['counts']
    return '\n'.join(['', report_core.table(headers, cells), '',
                      f'farm check: {c["ok"]} ok · {c["flash-failed"]} flash-failed · {c["failed"]} failed '
                      f'· {c["locked"]} locked · {c["blocked"]} blocked · in {doc["elapsed_s"]:.0f}s']) + '\n'


def _check_body(body: dict) -> None:
    if body.get('check') != 'farm-check' or body.get('schema_version') != SCHEMA_VERSION:
        raise ValueError(f'not a farm-check body of schema {SCHEMA_VERSION}')
    if not isinstance(body.get('rows'), list) or not isinstance(body.get('counts'), dict):
        raise ValueError('rows a list, counts an object')
    for r in body['rows']:
        if not isinstance(r, dict) or not isinstance(r.get('name'), str) or r.get('status') not in STATUSES:
            raise ValueError(f'a row has a name and a status in {STATUSES}: {r!r}')


# its own namespace (farm_check.json/.md, farm_check.attempts), apart from any harness report
FARM_SCHEMA = report_core.BodySchema(name='farm-check', version=SCHEMA_VERSION, stem='farm_check',
                                     evidence='farm-health', check=_check_body, empty=lambda b: not b['rows'],
                                     render=render_table)


@contextlib.contextmanager
def stdout_to_stderr():
    """fd 1 onto stderr, so no flasher or fetch child can write to stdout either."""
    sys.stdout.flush()
    saved = os.dup(1)
    try:
        os.dup2(2, 1)
        yield
    finally:
        sys.stdout.flush()
        os.dup2(saved, 1)
        os.close(saved)


def default_state_dir() -> str:
    """Where run_tracked keeps receipts a recovery may need: persistent, ours, mode 0700."""
    base = Path(os.environ.get('XDG_STATE_HOME') or Path.home() / '.local' / 'state')
    return str(base / 'hil-farm-check')


def _state_dir(path: str) -> str:
    try:
        os.makedirs(os.path.dirname(path), exist_ok=True)
        try:
            os.mkdir(path, 0o700)
        except FileExistsError:
            pass
        # the receipts it holds decide `done`, so another user must not be able to replace them
        return hil_process._trusted_dir(path)
    except OSError as e:
        raise Refused(f'state dir {path}: {e}')


def _roster(profile, host: str, args) -> tuple:
    """(path, boards to check), selection validated."""
    try:
        roster = profile.roster(host, args.config, args)
    except Exception as e:
        raise Refused(f'roster for host {host}: {e}')
    if not isinstance(roster, dict) or not isinstance(roster.get('path'), str) \
            or not isinstance(roster.get('boards'), list) or not isinstance(roster.get('named_only'), list):
        raise Refused(f'profile roster: want path, boards and named_only: {roster!r}')
    boards = list(roster['boards'])
    if args.board:
        boards += roster['named_only']
        unknown = set(args.board) - {b.get('name') for b in boards if isinstance(b, dict)}
        if unknown:
            raise Refused(f'board(s) not in {roster["path"]}: {", ".join(sorted(unknown))}')
        boards = [b for b in boards if isinstance(b, dict) and b.get('name') in args.board]
    return roster['path'], boards


def parser_for(profile) -> argparse.ArgumentParser:
    p = argparse.ArgumentParser(description='Farm check: probe, light flash, return and final state of '
                                            "every board of this host's roster")
    p.add_argument('config', nargs='?', help="roster file (default: the profile's for this host)")
    p.add_argument('-b', '--board', action='append', default=[], help='only these boards')
    p.add_argument('--scan-only', action='store_true', help='probe presence only: no locks, flashing or fetch')
    p.add_argument('--no-park', action='store_true',
                   help="leave the light image running; refused unless the profile verifies that state")
    p.add_argument('--json', action='store_true',
                   help='print only the result document on stdout; progress goes to stderr')
    p.add_argument('--report-dir', help='also persist farm_check.json/.md there, under its own attempt records')
    p.add_argument('-v', '--verbose', action='store_true')
    if hasattr(profile, 'add_arguments'):
        profile.add_arguments(p)
    return p


LOCK_FUNCTIONS = ('take_board_lock', 'read_record', 'write_record', 'clear_record', 'is_cleanup_holder',
                  'proc_start')
LOCK_EXCEPTIONS = ('BoardLocked', 'RecoveryBlocked', 'LockUnavailable')


def _lock_api_problem(module):
    """Why `module` cannot stand in for hil_lock, or None."""
    bad = [a for a in LOCK_FUNCTIONS if not callable(getattr(module, a, None))]
    bad += [a for a in LOCK_EXCEPTIONS
            if not (isinstance(getattr(module, a, None), type) and issubclass(getattr(module, a), BaseException))]
    if not isinstance(getattr(module, 'CLEANUP_ROLE', None), str):
        bad.append('CLEANUP_ROLE')
    if bad:
        return f'lock module lacks {", ".join(bad)}'
    reasons = getattr(module, 'PROTECTED_REASONS', None)
    if not isinstance(reasons, (set, frozenset, tuple, list)) or REASON not in reasons:
        return f'lock module lacks the {REASON} reason'
    return None


def main(profile, argv=None, *, state_dir=None, host=None, lock_module=None) -> int:
    """The launcher's entry point: returns the exit status (module docstring). `lock_module`
    is the project's own hil_lock instance, used for this call in place of the engine's."""
    global hil_lock
    saved = hil_lock
    if lock_module is not None:
        problem = _lock_api_problem(lock_module)
        if problem:
            print(f'farm check: refused: {problem}', file=sys.stderr)
            return 2
        hil_lock = lock_module
    try:
        return _main(profile, argv, state_dir, host)
    finally:
        hil_lock = saved


def _main(profile, argv, state_dir, host) -> int:
    global _t0
    _t0 = time.monotonic()
    args = parser_for(profile).parse_args(argv)
    host = host or socket.gethostname()
    mode = 'scan-only' if args.scan_only else 'full'
    with stdout_to_stderr() if args.json else contextlib.nullcontext():
        try:
            info = profile_info(profile)
            if args.no_park and getattr(profile, 'LEAVE_RUNNING', False) is not True:
                raise Refused('--no-park: the profile defines no verified "leave running" final state')
            cfg, boards = _roster(profile, host, args)
            try:
                roster = {'path': os.path.realpath(cfg), 'digest': _sha256(cfg)}
            except OSError as e:
                raise Refused(f'roster {cfg}: {e}')
            sdir = None if args.scan_only else _state_dir(state_dir or default_state_dir())
            if args.report_dir:
                try:
                    attempt = report_core.start_attempt(args.report_dir, FARM_SCHEMA, False)
                except report_core.ReportError as e:
                    raise Refused(f'report dir {args.report_dir}: {e}')
            else:
                attempt = report_core.Attempt(report_core.new_attempt_id(), None, None, None, None)
            prepared = None
            if not args.scan_only and hasattr(profile, 'prepare'):
                try:
                    prepared = profile.prepare(boards, args, say)
                except Exception as e:
                    raise Refused(f'profile prepare: {e!r}')
        except Refused as e:
            print(f'farm check: refused: {e}', file=sys.stderr)
            return 2
        say(f'farm check: host {host}, config {roster["path"]}, {len(boards)} boards, {mode}')
        rows, aborted = check_farm(profile, boards, args, state_dir=sdir, prepared=prepared)
    doc = farm_document(host, roster['path'], mode, rows, info, time.monotonic() - _t0)
    code = exit_status(doc, aborted)
    meta = report_core.make_meta(FARM_SCHEMA, attempt,
                                 termination='aborted' if aborted else 'completed' if boards else 'no-boards',
                                 invocation=[str(a) for a in (argv if argv is not None else sys.argv[1:])],
                                 selection=[str(b.get('name', '?')) if isinstance(b, dict) else '?' for b in boards],
                                 host=host, roster=roster)
    try:
        text = json.dumps(json.loads(report_core.serialise(FARM_SCHEMA, doc, meta)), ensure_ascii=False)
    except report_core.ReportError as e:
        print(f'farm check: document invalid: {e}', file=sys.stderr)
        text, code = json.dumps({**doc, report_core.META: meta}, ensure_ascii=False, default=repr), max(code, 1)
    sys.stdout.write(text + '\n' if args.json else render_table(doc))
    sys.stdout.flush()
    if args.report_dir:
        try:
            report_core.publish(args.report_dir, FARM_SCHEMA, doc, meta)
        except report_core.ReportError as e:
            print(f'farm check: report not persisted: {e}', file=sys.stderr)
            code = max(code, 1)
    return code


if __name__ == '__main__':
    if len(sys.argv) != 3 or sys.argv[1] != '--hold':
        sys.exit('usage: hil_farm_check.py --hold JOB  (started by the farm check engine)')
    sys.exit(_hold(json.loads(sys.argv[2])))
