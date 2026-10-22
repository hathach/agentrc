#!/usr/bin/env python3
"""Farm check engine shared by the HIL projects (the `hil` skill's Farm check). Linux only, stdlib only.

For every board of the host's roster: is exactly one of its flash probes on the bus, and does a
reset through it succeed? A board its probe resets counts as present. No firmware is fetched or
flashed and the board's USB is not checked: whatever image it ran runs again. No recovery: a
wedged probe or board is reported. Farm health only, never firmware-validation evidence.

Row statuses: ok (the reset succeeded); failed (no probe, an ambiguous probe, a reset that
failed or raised, or the board's own error); unsupported (the probe has no reset-only mode:
unverified, not ok); locked (a live holder: skipped, never waited on, no bypass); blocked (a
recovery blocker). `coverage`: reset-not-attempted, reset-attempted, unsupported,
skipped-locked, blocked-before (an incoming recovery blocker, a live cleanup holder or a stale
protected record, refused before the reset) or blocked-after (this check's cleanup is
unresolved; `check_status` keeps the check's own status).

Exit: 0 no failed row (unsupported and locked rows are unverified, not failures); 1 any failed
row, an aborted run, or a report that could not be persisted; 2 refused before any board
(usage, unknown host, profile, state dir, report dir); 4 any blocked row, over 1. Read the
document for detail, never $? alone.

Reservation: a board is taken with hil_lock.take_board_lock(lock, 'farm_check',
protected=True). It is released once every process started under it is proven ended
(hil_process.run_tracked's `done`, or a recheck of its receipt), whether or not the reset
succeeded; ctx.run refuses a new command while an earlier one is not proven ended. Otherwise
the board keeps a recovery blocker: while a process the check started survives, a cleanup
holder (`role: cleanup-holder`, hil_lock.hand_to_cleanup_holder's fresh interpreter given
the lock's descriptor) keeps the flock, and the row names its token. `hil_lock.py release
--token` refuses, naming the open receipts, until every receipt it holds rechecks `done`;
anything else is the contract's Recovery. Every other outcome, a failed handoff to the holder
or a record that cannot be cleared included, leaves the protected record on the free flock
for a recovery takeover. A check killed outright leaves its protected record the same way and
prints no report. SIGINT stops the check after the current board's cleanup; the report then
says `termination: aborted`.

Profile (a module; `__file__` names it in the report, with its digest):
  roster(host, config, args) -> {'path', 'boards': [dict], 'named_only': [dict]}: this host's
      permitted roster (`config` the CLI's CONFIG or None). Raises for an unknown host
      without an explicit roster, before any lock. `named_only` boards are checked only when
      -b names them. Every board dict has a `name`.
  lock_name(board) -> str: the board's lock name.
  probe_name(board) -> str: the board's flasher, shown beside the probe mark.
  probe_candidates(board) -> [busport]: every probe matching the board under the project's
      constraints, best tier only; more than one is reported as ambiguous, never picked.
  reset(ctx, board) -> hil_flash Operation, or None when the probe has no reset-only mode and
      nothing was started. It runs inside the reservation and takes no lock; every process it
      starts goes through `ctx.run` (hil_flash's `run=`: hil_process.run_tracked with the
      engine's state dir). A non-zero returncode is a failed reset; one that exits 0 without
      the target's answer in its transcript raises, carrying its `.operation`, which the row
      keeps. ctx.run raises `Outstanding` instead of starting a command while an earlier one
      is not proven ended; the profile lets it propagate.
  Optional: add_arguments(parser).

main(profile, argv, lock_module=...) takes the project's own loaded hil_lock instance in
place of the engine's (module identity, test isolation); there is no lock-dir option. The
engine loads hil_lock, hil_process, hil_report_core and hil_remote_core (RIG_PATH) through hil_load.
"""

from __future__ import annotations

import argparse
import contextlib
import hashlib
import importlib.util
import io
import json
import os
import socket
import subprocess
import sys
import time
from pathlib import Path

HERE = os.path.dirname(os.path.abspath(__file__))
FILES = ('skills/hil/scripts/hil_load.py', 'skills/hil/scripts/hil_lock.py', 'skills/hil/scripts/hil_process.py',
         'skills/hil/scripts/hil_report_core.py', 'skills/hil/scripts/hil_report_notice.py',
         'skills/hil/scripts/hil_remote_core.py')


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
hil_lock = hil_load.load_file('skills/hil/scripts/hil_lock.py')
hil_process = hil_load.load_file('skills/hil/scripts/hil_process.py')
report_core = hil_load.load_file('skills/hil/scripts/hil_report_core.py')
remote_core = hil_load.load_file('skills/hil/scripts/hil_remote_core.py')

REASON = 'farm_check'
HOLDER = 'hil_farm_check'
SCHEMA_VERSION = 2
SETTLE = 2.0          # s run_tracked waits for an operation's descendants once it ended
RECHECK_WAIT = 5.0    # s an operation not yet proven ended gets before the board's cleanup is decided
HOLDER_READY = 10.0   # s the cleanup holder gets for each step of its handoff
STATUSES = ('ok', 'failed', 'unsupported', 'locked', 'blocked')
MARKS = {'ok': '✅ ok', 'failed': '❌ failed', 'unsupported': '➖ unsupported', 'locked': '🔒 locked',
         'blocked': '⛔ blocked'}
REQUIRED = ('roster', 'lock_name', 'probe_name', 'probe_candidates', 'reset')

_t0 = time.monotonic()


class Refused(Exception):
    """Refused before any board: exit 2."""


def say(msg: str) -> None:
    print(f'[{time.monotonic() - _t0:6.1f}s] {msg}', file=sys.__stdout__, flush=True)


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


# --- the reservation's processes ------------------------------------------------------------
class Outstanding(RuntimeError):
    """ctx.run refused to start a command: an earlier one is not proven ended."""
    cleanup, receipt = 'done', None    # it started nothing


def _note_cleanup(entry: dict, source) -> None:
    cleanup = getattr(source, 'cleanup', None)
    entry['cleanup'] = cleanup if cleanup in ('done', 'pending', 'unknown') else 'unknown'
    entry['receipt'] = getattr(source, 'receipt', None)


class Context:
    """What the profile's reset gets: `run` (call it as hil_flash's `run=`) and `args`. `ledger`
    keeps every run's cleanup and receipt; a run is entered before it starts, so an interrupt
    mid-call still counts."""

    def __init__(self, args, state_dir: str):
        self.args, self.state_dir = args, state_dir
        self.ledger = []
        self.recheck_by = None   # the reservation's one RECHECK_WAIT, set when a run is first found open

    def _recheck(self) -> None:
        """Recheck every run not proven ended until all are, none left has a receipt to recheck,
        or the reservation's one RECHECK_WAIT, counted from when a run was first found open,
        passed: a later call rechecks once without waiting. A receipt proven done is removed,
        nothing else is; time alone never clears a run."""
        def rechecking():
            return [e for e in self.ledger if e['cleanup'] != 'done' and e['receipt'] is not None]
        while True:
            for e in rechecking():
                e['cleanup'] = hil_process.recheck(e['receipt'])
                if e['cleanup'] == 'done':
                    hil_process.tidy(e['receipt'])
                    e['receipt'] = None
            if not rechecking():
                return
            if self.recheck_by is None:
                self.recheck_by = time.monotonic() + RECHECK_WAIT
            if time.monotonic() >= self.recheck_by:
                return
            time.sleep(0.1)

    def open_runs(self) -> list:
        """The runs not proven ended, after rechecking them within the reservation's RECHECK_WAIT."""
        self._recheck()
        return [e for e in self.ledger if e['cleanup'] != 'done']

    def run(self, cmd, cwd=None, *, timeout, **kw) -> subprocess.CompletedProcess:
        if open_runs := self.open_runs():
            raise Outstanding(f'not started, earlier runs not proven ended: {_evidence(open_runs)}')
        entry = {'cmd': hil_process._cmd_label(cmd), 'cleanup': 'unknown', 'receipt': None}
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


def operation_record(op) -> dict:
    attempts = getattr(op, 'attempts', ())
    return {'returncode': getattr(op, 'returncode', None), 'cleanup': getattr(op, 'cleanup', 'unknown'),
            'receipts': list(getattr(op, 'receipts', ())),
            'attempts': [{'cmd': hil_process._cmd_label(a.cmd), 'kind': a.kind, 'returncode': a.result.returncode,
                          'cleanup': a.cleanup, 'error': None if a.error is None else repr(a.error)[:200]}
                         for a in attempts]}


def error_line(out: str) -> str:
    """Most informative line of a failed command's output: last error-looking line,
    else the last non-empty one."""
    lines = [l.strip() for l in out.splitlines() if l.strip()]
    for l in reversed(lines):
        if any(k in l.lower() for k in ('error', 'fail', 'unknown', 'cannot', 'timeout',
                                        'no valid', 'not found', 'unable')):
            return l[:90]
    return lines[-1][:90] if lines else ''


# --- one board ------------------------------------------------------------------------------
def new_row(name: str, **cells) -> dict:
    return {'name': name, 'probe': '–', 'location': '–', 'note': [], 'status': 'failed', 'operations': [],
            'cleanup': None, **cells}


def _probe(profile, board, row) -> str | None:
    tool = profile.probe_name(board)
    if not isinstance(tool, str) or not tool:
        raise ValueError(f'probe_name: want a string: {tool!r}')
    cands = _strings(list(profile.probe_candidates(board)), 'probe candidates')
    if len(cands) == 1:
        row['probe'], row['location'] = f'✅ {tool}', cands[0]
        return cands[0]
    if cands:
        row['probe'], row['location'] = f'⚠ {tool}', ', '.join(cands)
        row['note'].append('probe ambiguous: every location matches; none picked')
        say(f'{row["name"]:26} probe AMBIGUOUS ({", ".join(cands)})')
    else:
        row['probe'] = f'❌ {tool}'
        row['note'].append('probe not on the bus')
        say(f'{row["name"]:26} probe MISSING')
    return None


def check_board(profile, board: dict, args, state_dir) -> dict:
    row = new_row(board['name'])
    if not _probe(profile, board, row):
        return row
    lock = profile.lock_name(board)
    try:
        fh = hil_lock.take_board_lock(lock, REASON, protected=True)
    except hil_lock.RecoveryBlocked as e:
        return _not_taken(row, 'blocked', str(e))
    except hil_lock.BoardLocked as e:
        return _not_taken(row, 'locked', str(e))
    except hil_lock.LockUnavailable as e:
        return _not_taken(row, 'failed', str(e))
    ctx = Context(args, state_dir)
    try:
        try:
            _reset(profile, ctx, board, row)
        finally:
            _end_reservation(fh, lock, ctx, row)
    except KeyboardInterrupt:
        row['interrupted'] = True
    # an interrupt that came before finalization could defer it: finalize from the same ledger
    while row['cleanup'] is None:
        try:
            _end_reservation(fh, lock, ctx, row)
        except KeyboardInterrupt:
            row['interrupted'] = True
    return row


def _not_taken(row: dict, status: str, why: str) -> dict:
    row['status'] = status
    row['note'].append(why)
    say(f'{row["name"]:26} {status}: {why}')
    return row


def _record(row: dict, op) -> None:
    if op is not None:
        row['operations'].append(operation_record(op))


def _reset(profile, ctx: Context, board: dict, row: dict) -> None:
    """Run the profile's reset; the row records what happened and every exception stays here."""
    try:
        try:
            op = profile.reset(ctx, board)
        except BaseException as e:
            _record(row, getattr(e, 'operation', None))
            raise
        if op is None and not ctx.ledger:
            row['status'] = 'unsupported'
            row['note'].append('the probe has no reset-only mode')
        elif not isinstance(getattr(op, 'returncode', None), int):
            raise ValueError(f'reset: want an Operation, or None having started nothing: {op!r}')
        else:
            _record(row, op)
            if op.returncode == 0:
                row['status'] = 'ok'
            else:
                line = error_line(hil_process.cmd_stdout_text(op.stdout) + '\n' + hil_process.cmd_stdout_text(op.stderr))
                row['note'].append(f'reset rc={op.returncode}' + (f': {line}' if line else ''))
        say(f'{row["name"]:26} {MARKS[row["status"]]}')
    except KeyboardInterrupt:
        row['interrupted'] = True
        row['note'].append('interrupted')
    except Exception as e:
        row['note'].append(f'reset: {e!r}'[:120])


def _end_reservation(fh, lock: str, ctx: Context, row: dict) -> None:
    """Release the board, or keep its recovery blocker; never raises. An interrupt is deferred
    until the decision is carried out, its waits all bounded, and then only marks the row."""
    hold = hil_process.InterruptHold()
    try:
        _finalize(fh, lock, ctx, row)
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
    cleanup = row['cleanup'] or {'result': 'interrupted', 'evidence': [], 'receipts': [], 'blocker': None,
                                 'released': False}
    row['cleanup'] = cleanup
    if cleanup['released']:
        return
    if row['status'] != 'blocked':
        row['check_status'], row['status'] = row['status'], 'blocked'
    cleanup['blocker'] = cleanup['blocker'] or 'stale-record'
    cleanup['evidence'].append(why)
    with contextlib.suppress(OSError):
        fh.close()   # never clear_record: the record is the blocker


def _finalize(fh, lock: str, ctx: Context, row: dict) -> None:
    ctx._recheck()
    open_ops = [e for e in ctx.ledger if e['cleanup'] != 'done']
    receipts = [e['receipt'] for e in open_ops if e['receipt'] is not None]
    cleanup = {'result': 'unresolved' if open_ops else 'release-failed', 'receipts': receipts, 'blocker': None,
               'released': False, 'evidence': [f'not proven ended ({e["cleanup"]}): {e["cmd"]}' for e in open_ops]}
    row['cleanup'] = cleanup
    if not open_ops:
        # proven clear through our own descriptor, while the flock is still ours
        if hil_lock.clear_record(fh) and os.fstat(fh.fileno()).st_size == 0:
            cleanup['result'], cleanup['released'] = 'released', True
            with contextlib.suppress(OSError):
                fh.close()
            return
        cleanup['evidence'].append('the record could not be cleared')
    row['check_status'], row['status'] = row['status'], 'blocked'
    detail = (f'farm check cleanup unresolved: {cleanup["result"]}; ' + '; '.join(cleanup['evidence']))[:500]
    token = None
    if any(e['cleanup'] == 'pending' for e in open_ops):
        try:     # fh was taken protected, as the handoff requires: a failure leaves a recovery blocker
            token = hil_lock.hand_to_cleanup_holder({lock: fh}, REASON, detail=detail, receipts=receipts,
                                                    wait=HOLDER_READY, holder=HOLDER)
        except KeyboardInterrupt:
            row['interrupted'] = True
        except hil_lock.LockUnavailable as e:
            cleanup['evidence'].append(str(e))
    else:
        with contextlib.suppress(OSError):
            hil_lock.write_record(fh, REASON, protected=True, detail=detail, receipts=receipts)
    if token:
        cleanup['blocker'], cleanup['token'] = 'cleanup-holder', token
        row['note'].append(f'cleanup holder (token {token}) keeps the board for recovery, which ends it with '
                           f'`hil_lock.py release --token {token}`')
    else:
        cleanup['blocker'] = 'stale-record'
        row['note'].append(f'protected record kept: recovery takes it over (`hil_lock.py hold --take-over {lock}`)')
    with contextlib.suppress(OSError):
        fh.close()   # never clear_record: the record is the blocker
    say(f'{row["name"]:26} BLOCKED: {cleanup["blocker"]} ({cleanup["result"]})')


# --- the farm -------------------------------------------------------------------------------
def check_board_safe(profile, board, args, state_dir) -> dict:
    """Isolate one board's exceptions: a crashing board must not discard every other row."""
    try:
        return check_board(profile, board, args, state_dir)
    except Exception as e:
        name = board.get('name', '?') if isinstance(board, dict) else '?'
        say(f'{name:26} INTERNAL ERROR: {e!r}')
        return new_row(name, note=[repr(e)[:120]])


def check_farm(profile, boards: list, args, *, state_dir) -> tuple:
    """(rows, aborted): one row per board in order; SIGINT ends the loop after the board in
    hand, whose cleanup has run."""
    rows = []
    # silence the runner's COMMAND FAILED dumps; say() writes to sys.__stdout__
    with contextlib.nullcontext() if args.verbose else contextlib.redirect_stdout(io.StringIO()):
        for b in boards:
            try:
                rows.append(check_board_safe(profile, b, args, state_dir))
            except KeyboardInterrupt:
                return rows, True
            if rows[-1].get('interrupted'):
                return rows, True
    return rows, False


def _coverage(row: dict) -> str:
    entered = row['cleanup'] is not None   # every reservation the engine entered is finalized
    if row['status'] == 'locked':
        return 'skipped-locked'
    if row['status'] == 'unsupported':
        return 'unsupported'
    if row['status'] == 'blocked':
        return 'blocked-after' if entered else 'blocked-before'
    return 'reset-attempted' if entered else 'reset-not-attempted'


def farm_document(host: str, config: str, rows: list, profile: dict, elapsed_s: float) -> dict:
    """The one result the table, the JSON and a persisted report all come from."""
    counts = dict.fromkeys(STATUSES, 0)
    for r in rows:
        counts[r['status']] += 1
        r['coverage'] = _coverage(r)
    return {'schema_version': SCHEMA_VERSION, 'check': 'farm-check', 'host': host, 'config': config,
            'profile': profile, 'rows': rows, 'counts': counts, 'elapsed_s': round(elapsed_s, 1)}


def exit_status(doc: dict, aborted: bool = False) -> int:
    c = doc['counts']
    if c['blocked']:
        return 4
    return 1 if c['failed'] or aborted else 0


def render_table(doc: dict) -> str:
    headers = ['Board', 'Probe', 'Location', 'Reset/Status', 'Note']
    cells = [[r['name'], r['probe'], r['location'], MARKS.get(r['status'], r['status']), '; '.join(r['note'])]
             for r in doc['rows']]
    c = doc['counts']
    return '\n'.join(['', report_core.table(headers, cells), '',
                      f'farm check: {c["ok"]} ok · {c["failed"]} failed · {c["unsupported"]} unsupported '
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
    """fd 1 onto stderr, so no reset child can write to stdout either."""
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
        return hil_process.trusted_dir(path)
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
    p = argparse.ArgumentParser(description="Farm check: each board of this host's roster is present when its "
                                            'one matching probe resets it')
    p.add_argument('config', nargs='?', help="roster file (default: the profile's for this host)")
    p.add_argument('-b', '--board', action='append', default=[], help='only these boards')
    p.add_argument('--json', action='store_true',
                   help='print only the result document on stdout; progress goes to stderr')
    p.add_argument('--report-dir', help='also persist farm_check.json/.md there, under its own attempt records')
    p.add_argument('-v', '--verbose', action='store_true')
    if hasattr(profile, 'add_arguments'):
        profile.add_arguments(p)
    return p


LOCK_FUNCTIONS = ('take_board_lock', 'write_record', 'clear_record', 'hand_to_cleanup_holder')
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
    is the project's own hil_lock instance, used for this call in place of the engine's. The
    user bin dirs go on PATH for the call (_user_bins_on_path). The report's `_hil.roster` is
    the roster's portable identity (hil_report_core.roster_identity); the document's `config`
    keeps its real path."""
    global hil_lock
    saved, path = hil_lock, os.environ.get('PATH')
    if lock_module is not None:
        problem = _lock_api_problem(lock_module)
        if problem:
            print(f'farm check: refused: {problem}', file=sys.stderr)
            return 2
        hil_lock = lock_module
    try:
        _user_bins_on_path()
        return _main(profile, argv, state_dir, host)
    finally:
        hil_lock = saved
        if path is None:
            os.environ.pop('PATH', None)
        else:
            os.environ['PATH'] = path


def _user_bins_on_path() -> None:
    """The rig's PATH prefix (hil_remote_core.RIG_PATH) ahead of PATH, in its order: flasher
    CLIs live in those user bin dirs, which a non-login shell may lack. A dir is added only
    when it exists and is not on PATH already."""
    for d in reversed(remote_core.RIG_PATH.replace('$HOME', str(Path.home())).split(':')):
        if os.path.isdir(d) and d not in os.environ.get('PATH', '').split(os.pathsep):
            os.environ['PATH'] = f'{d}{os.pathsep}{os.environ.get("PATH", "")}'



def _main(profile, argv, state_dir, host) -> int:
    global _t0
    _t0 = time.monotonic()
    args = parser_for(profile).parse_args(argv)
    host = host or socket.gethostname()
    with stdout_to_stderr() if args.json else contextlib.nullcontext():
        try:
            info = profile_info(profile)
            cfg, boards = _roster(profile, host, args)
            try:
                config, roster = os.path.realpath(cfg), report_core.roster_identity(cfg)
            except OSError as e:
                raise Refused(f'roster {cfg}: {e}')
            sdir = _state_dir(state_dir or default_state_dir())
            if args.report_dir:
                try:
                    attempt = report_core.start_attempt(args.report_dir, FARM_SCHEMA, False)
                except report_core.ReportError as e:
                    raise Refused(f'report dir {args.report_dir}: {e}')
            else:
                attempt = report_core.Attempt(report_core.new_attempt_id(), None, None, None, None)
        except Refused as e:
            print(f'farm check: refused: {e}', file=sys.stderr)
            return 2
        say(f'farm check: host {host}, config {config}, {len(boards)} boards')
        rows, aborted = check_farm(profile, boards, args, state_dir=sdir)
    doc = farm_document(host, config, rows, info, time.monotonic() - _t0)
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
        except (report_core.ReportError, OSError) as e:
            print(f'farm check: report not persisted: {e}', file=sys.stderr)
            code = max(code, 1)
            # the predecessor stays live until withdrawn: it must not read as this check's result
            try:
                rec = report_core.recover_publish(args.report_dir, FARM_SCHEMA, attempt, e)
            except (report_core.ReportError, OSError) as again:
                rec = report_core.Recovery('unresolved', [str(again)], [])
            print(f'farm check: report {rec.outcome}' + ''.join(f'; {p}' for p in rec.problems), file=sys.stderr)
    return code
