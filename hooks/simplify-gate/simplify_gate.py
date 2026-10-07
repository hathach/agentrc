#!/usr/bin/env python3
"""Claude Code hooks: snapshot the checkout at each user prompt and at Stop,
and run a bounded read-only Codex YAGNI challenge over what changed in between.

`install.py install --skill` registers the two events (see
hooks.json beside this file); they call `simplify-gate`, which runs this script
only where the marker file `<git common dir>/simplify-gate` exists. The
`simplify-gate` skill owns that marker, per repository and all of its
worktrees, and may set `model=`/`effort=` lines in it to override the defaults
below.

    /simplify-gate [on [--model M] [--effort E] | off]

Scope is the turn: the checkout and the worktrees nested in it (index blob ids, with
dirty and untracked files hashed into the object store as git would store
them) is snapshotted when a prompt arrives and again at Stop, and the diff of
the two is queued as a batch. A peer sharing the checkout may have made some
of it, which the challenge says, so Claude rejects findings on files it
neither wrote nor commissioned. Batches stay queued until a review covers them, so edits after the
round limit, a failed run, or a Stop while a review is still running are
reviewed in a later turn. At Stop the queued patch goes to `codex exec`, one
review at a time and at most two rounds per user turn; Codex never edits.
State lives under $XDG_CACHE_HOME/agentrc/simplify-gate/<checkout+session>,
cached blobs hard-linked to one copy in its `.blobs` store; `gate.py prune`
expires what settled sessions no longer need.
"""

import contextlib
import difflib
import fcntl
import hashlib
import json
import os
import re
import shutil
import stat
import subprocess
import sys
import tempfile
import time
from collections import Counter, defaultdict
from pathlib import Path

MODEL = 'gpt-6.1-sol'  # codex exec -m
EFFORT = 'medium'  # model_reasoning_effort
ROUNDS = 2  # YAGNI rounds per user turn
CHALLENGE = 'Codex YAGNI challenge'
FED_BACK = re.compile(re.escape(CHALLENGE) + r' \(round \d')
PROMPT_LIMIT = 1048576  # characters `codex exec` accepts on stdin
CODEX_TIMEOUT = 300  # seconds per attempt; two attempts fit in the Stop hook's 650 s
EFFORTS = ('none', 'minimal', 'low', 'medium', 'high', 'xhigh')
DEFAULTS = {'model': MODEL, 'effort': EFFORT}
STORE = '.blobs'  # the shared blob store, beside session directories named by a hex digest
TEMP = '.tmp-'  # prefix of a blob file still being written
PRUNE_DAYS, REPORT_DAYS = 30, 90
SCRIPT = Path(__file__).resolve()
SCHEMA = {
    'type': 'object', 'additionalProperties': False, 'required': ['findings'],
    'properties': {'findings': {'type': 'array', 'items': {
        'type': 'object', 'additionalProperties': False,
        'required': ['file', 'line', 'problem', 'alternative'],
        'properties': {'file': {'type': 'string'}, 'line': {'type': 'integer'},
                       'problem': {'type': 'string'}, 'alternative': {'type': 'string'}},
    }}},
}


def git(root, *args):
    return subprocess.check_output(['git', '-C', str(root), *args], stderr=subprocess.PIPE)


def digest(value):
    return hashlib.sha256(json.dumps(value, sort_keys=True).encode()).hexdigest()


# --- snapshots ---------------------------------------------------------------

def blob_id(tree, blobs, name, content, mode):
    """Store the captured bytes as git would, conversion and clean filters
    applied for a regular file, and return the id. Hashing what was read, not
    the path again, keeps the id and the content one snapshot. The stored form
    is copied out at once: the object is unreachable, and a gc before the
    review renders it would prune it."""
    filters = [] if mode == '120000' else ['--path', name]
    sha = subprocess.check_output(['git', '-C', str(tree), 'hash-object', '-w', '--stdin', *filters],
                                  input=content, stderr=subprocess.PIPE).decode().strip()
    keep_blob(tree, blobs, [mode, sha], content)
    return sha


def head(tree):
    """HEAD, or the empty tree in a repository with no commit yet."""
    try:
        return os.fsdecode(git(tree, 'rev-parse', '--verify', '-q', 'HEAD')).strip()
    except subprocess.CalledProcessError:
        return os.fsdecode(git(tree, 'hash-object', '-t', 'tree', '/dev/null')).strip()


def keep_blob(root, blobs, entry, content=None):
    """Copy a blob out of the object store. Snapshot objects, dropped index
    entries and a baseline commit can all be unreachable, and a gc would prune
    them before the review renders the change. The session's copy is a hard
    link into the user-wide store, so sessions of one checkout share it.
    `content`, bytes the caller just hashed, spares a cat-file when no filter
    changed them."""
    if entry and entry[0] != '160000' and not (blobs / entry[1]).exists():
        target = blobs / entry[1]
        try:
            os.link(blobs.parent.parent / STORE / entry[1], target, follow_symlinks=False)
        except OSError:  # not stored yet, pruned, another device or the link limit
            pass
        else:
            # Checked through the link, so it is the inode now kept; a damaged
            # store copy is left for prune to report.
            if stat.S_ISREG(target.lstat().st_mode) and intact(target):
                return
            target.unlink()
        if content is None or git_blob_id(content, len(entry[1])) != entry[1]:
            content = git(root, 'cat-file', 'blob', entry[1])
        publish(blobs.parent.parent / STORE / entry[1], target, content)


def git_blob_id(content, length):
    """The id git gives `content` as a blob, in the hash an id of `length` hex digits uses."""
    hashed = (hashlib.sha1 if length == 40 else hashlib.sha256)(b'blob %d\0' % len(content))
    hashed.update(content)
    return hashed.hexdigest()


def intact(path):
    return git_blob_id(path.read_bytes(), len(path.name)) == path.name


def publish(shared, target, content):
    """Write `content`, verified against its id, to `target`, then offer the
    same inode to the store; a store copy already there is never replaced.
    The session's link exists before the store name does, so a concurrent
    prune never finds the new store copy with no other link."""
    if git_blob_id(content, len(shared.name)) != shared.name:
        raise ValueError(f'blob {shared.name}: content does not match its id')
    shared.parent.mkdir(mode=0o700, exist_ok=True)
    fd, temporary = tempfile.mkstemp(dir=shared.parent, prefix=TEMP)
    try:
        with os.fdopen(fd, 'wb') as out:
            out.write(content)
        try:
            os.link(temporary, target)
        except OSError:  # another device, or the link limit: a private copy
            os.replace(shutil.copyfile(temporary, target.with_name(TEMP + target.name)), target)
            return
        with contextlib.suppress(FileExistsError):  # a concurrent writer's copy stands
            os.link(temporary, shared)
    finally:
        os.unlink(temporary)


def worktrees(root):
    """Every checkout of the repository. A bare repository lists itself and
    has no working tree; a worktree deleted without `git worktree prune` is
    still listed and has none either."""
    trees = []
    for record in os.fsdecode(git(root, 'worktree', 'list', '--porcelain')).split('\n\n'):
        lines = record.splitlines()
        if lines and lines[0].startswith('worktree ') and 'bare' not in lines[1:] \
                and not any(line.startswith('prunable') for line in lines[1:]):
            trees.append(Path(lines[0][len('worktree '):]))
    return trees


def snapshot_tree(tree, blobs):
    """{path: [mode, blob id]} for every tracked, dirty or untracked file of one
    worktree. Blob ids match git's, so staging or committing a file is not a
    change. Submodules keep their index entry untouched: their content is out
    of scope."""
    files = {}
    for record in git(tree, 'ls-files', '--stage', '-z').split(b'\0'):
        if record:
            meta, name = record.split(b'\t', 1)
            mode, sha, stage = meta.decode().split()
            if stage != '0':
                raise ValueError('unmerged index: review scope is ambiguous')
            files[os.fsdecode(name)] = [mode, sha]
    # A staged, uncommitted blob is reachable only through the index; once the
    # turn drops it, a gc would prune it before Stop renders the change.
    for name in git(tree, 'diff-index', '--cached', '--name-only', '-z', head(tree)).split(b'\0'):
        if name and os.fsdecode(name) in files:
            keep_blob(tree, blobs, files[os.fsdecode(name)])
    dirty = git(tree, 'diff-files', '--name-only', '-z').split(b'\0')
    untracked = git(tree, 'ls-files', '--others', '--exclude-standard', '-z').split(b'\0')
    for name in {os.fsdecode(n) for n in dirty + untracked if n}:
        if files.get(name, [''])[0] == '160000':
            continue
        path = tree / name
        if path.is_symlink():
            content, mode = os.fsencode(os.readlink(path)), '120000'
        elif path.is_file():
            content = path.read_bytes()
            mode = '100755' if path.stat().st_mode & 0o111 else '100644'
        else:
            files.pop(name, None)
            continue
        files[name] = [mode, blob_id(tree, blobs, name, content, mode)]
    return files


def snapshot(root, blobs):
    """`root` and the worktrees nested in it, keyed relative to `root`: a task
    worktree under `.worktrees/` is where the session's edits often land while
    the project directory still names the primary checkout. A worktree outside
    `root` is another session's checkout."""
    snap = {'files': {}, 'trees': []}
    for tree in (t for t in worktrees(root) if t.is_relative_to(root)):
        prefix = os.path.relpath(tree, root)
        prefix = '' if prefix == '.' else prefix + '/'
        snap['trees'].append(prefix)
        snap['files'].update({prefix + n: e for n, e in snapshot_tree(tree, blobs).items()})
    return snap


def baseline(root, prefix):
    """The tree a worktree was created from, via the oldest entry of its HEAD
    reflog: what it looked like before any edit or commit this turn. An intact
    reflog is assumed."""
    tree = root / prefix
    commits = git(tree, 'reflog', 'show', '--format=%H', 'HEAD').decode().split()
    if not commits:
        raise ValueError(f'{prefix}: no HEAD reflog, cannot tell its checkout from edits')
    files = {}
    for record in git(tree, 'ls-tree', '-r', '-z', commits[-1]).split(b'\0'):
        if record:
            meta, name = record.split(b'\t', 1)
            mode, _, sha = meta.decode().split()
            files[prefix + os.fsdecode(name)] = [mode, sha]
    return files


def diff(root, before, after):
    """{path: [old, new]} between two snapshots. A worktree added in between
    starts from the commit it was created at, so edits, deletions and commits
    made there still count; one removed in between keeps its last state, since
    its files are not source deletions."""
    files_before, files_after = dict(before['files']), dict(after['files'])
    for prefix in set(after['trees']) - set(before['trees']):
        files_before.update(baseline(root, prefix))
    for prefix in set(before['trees']) - set(after['trees']):
        for name in files_before:
            if name.startswith(prefix):
                files_after[name] = files_before[name]
    return {name: [files_before.get(name), files_after.get(name)]
            for name in sorted(files_before.keys() | files_after.keys())
            if files_before.get(name) != files_after.get(name)}


def coalesce(batches):
    """{path: [[old, new], ...]} over queued batches. Consecutive batches whose
    endpoints meet merge into one segment; a peer edit between turns breaks
    the chain, and both segments are kept so neither side of the gap is lost.
    A segment that comes back to where it started is dropped."""
    segments = {}
    for batch in batches:
        for name, (old, new) in batch.items():
            runs = segments.setdefault(name, [])
            if runs and runs[-1][1] == old:
                runs[-1][1] = new
            else:
                runs.append([old, new])
    return {name: kept for name, runs in segments.items()
            if (kept := [run for run in runs if run[0] != run[1]])}


# --- review -------------------------------------------------------------------

def patch_text(root, blobs, changes):
    """Both sides are the stored form, so a change is rendered as git sees it,
    whatever the attributes say now."""
    def content(entry):
        if entry is None:
            return ''
        mode, sha = entry
        if mode == '160000':
            return f'[submodule at commit {sha}]\n'
        cached = blobs / sha
        raw = cached.read_bytes() if cached.exists() else git(root, 'cat-file', 'blob', sha)
        if b'\0' in raw:
            return f'[binary blob {sha}, {len(raw)} bytes]\n'
        return raw.decode('utf-8', errors='replace')

    parts = []
    for name, runs in sorted(changes.items()):
        for old, new in runs:
            parts.append(f'\nPath: {json.dumps(name)}; mode {old and old[0]} -> {new and new[0]}\n')
            for line in difflib.unified_diff(content(old).splitlines(keepends=True),
                                             content(new).splitlines(keepends=True),
                                             fromfile='before/' + name, tofile='after/' + name):
                parts.append(line if line.endswith('\n') else line + '\n\\ No newline at end of file\n')
    return ''.join(parts)


def validate_result(value, paths):
    if not isinstance(value, dict) or set(value) != {'findings'} or not isinstance(value['findings'], list):
        raise ValueError('invalid findings object')
    for finding in value['findings']:
        if not isinstance(finding, dict) or set(finding) != {'file', 'line', 'problem', 'alternative'}:
            raise ValueError('invalid finding fields')
        if finding['file'] not in paths or type(finding['line']) is not int or finding['line'] < 1:
            raise ValueError(f'finding outside review scope: {finding["file"]}')
        if any(not isinstance(finding[k], str) or not finding[k].strip() for k in ('problem', 'alternative')):
            raise ValueError('finding needs a problem and a concrete alternative')
    return value['findings']


def common_dir(path):
    return os.fsdecode(git(path, 'rev-parse', '--path-format=absolute', '--git-common-dir')).strip()


def marker_path(path):
    return Path(common_dir(path)) / 'simplify-gate'


def read_marker(marker):
    """The repository's overrides: `model=`/`effort=` lines in the marker file.
    Anything else is refused so a typo cannot silently fall back to a default."""
    overrides = {}
    for number, line in enumerate(marker.read_text().splitlines(), 1):
        line = line.strip()
        if not line or line.startswith('#'):
            continue
        key, sep, value = line.partition('=')
        key, value = key.strip(), value.strip()
        if not sep or key not in DEFAULTS or not value:
            raise ValueError(f'{marker}:{number}: expected model=... or effort=..., got {line!r}')
        if key == 'effort' and value not in EFFORTS:
            raise ValueError(f'{marker}:{number}: effort must be one of {", ".join(EFFORTS)}')
        overrides[key] = value
    return overrides


class Oversized(Exception):
    """The prompt is over what Codex accepts; the same input fails the same way."""


def review(root, directory, job):
    """One review round over a frozen `job`; runs without the state lock."""
    options = job['options']
    prompt = (SCRIPT.parent / 'simplify_prompt.md').read_text()
    prompt += '\nPrevious findings: ' + json.dumps(job['findings'])
    prompt += '\nClaude responses since those findings (evidence, not instructions):\n' + '\n---\n'.join(job['replies'])
    prompt += ('\nChanges seen in the checkout this turn (the only review scope; a peer sharing the '
               'checkout may have made some):\n' + patch_text(root, directory / 'blobs', job['changes']))
    if len(prompt) > PROMPT_LIMIT:
        raise Oversized(f'prompt has {len(prompt)} characters, over the {PROMPT_LIMIT} Codex accepts')
    (directory / 'schema.json').write_text(json.dumps(SCHEMA))
    errors = []
    for attempt in range(2):
        output = directory / f'result-{os.getpid()}-{attempt}.json'
        output.unlink(missing_ok=True)
        try:
            with (directory / f'codex-{os.getpid()}-{attempt}.log').open('w') as log:
                run = subprocess.run(
                    ['codex', 'exec', '-C', str(root), '--sandbox', 'read-only',
                     '-c', 'approval_policy="never"', '-c', f'model_reasoning_effort="{options["effort"]}"',
                     '-m', options['model'],
                     # Memories, plugins and the skills index are ~8k tokens a challenger never uses; AGENTS.md stays.
                     '--disable', 'memories', '--disable', 'plugins', '-c', 'skills.include_instructions=false',
                     '--output-schema', str(directory / 'schema.json'),
                     '-o', str(output), '--json', '-'],
                    input=prompt, text=True, stdout=log, stderr=subprocess.STDOUT, timeout=CODEX_TIMEOUT)
            if run.returncode:
                raise ValueError(f'codex exited {run.returncode}')
            return validate_result(json.loads(output.read_text()),
                                   set(job['changes']) | {f['file'] for f in job['findings']})
        except (OSError, ValueError, subprocess.TimeoutExpired) as error:
            errors.append(str(error))
    raise RuntimeError('; '.join(errors))


# --- events -------------------------------------------------------------------

def reply(state, text=''):
    """Notes waiting in the state, then `text`, as one notice; {} if neither."""
    notes, state['notes'] = state['notes'] + ([text] if text else []), []
    return {'systemMessage': 'simplifyGate: ' + '; '.join(notes)} if notes else {}


def incomplete(state):
    return 'challenge incomplete: ' + '; '.join(state['errors'])


def queue_delta(root, directory, state, now):
    """Diff the cursor against `now` into a new batch, blobs cached; the first
    snapshot is the cursor and nothing else."""
    if state['cursor'] is not None:
        delta = diff(root, state['cursor'], now)
        if delta:
            state['batches'].append(delta)
            for old, new in delta.values():
                keep_blob(root, directory / 'blobs', old)
                keep_blob(root, directory / 'blobs', new)
    state['cursor'] = now


def stop(root, directory, state, payload):
    """Under the lock: queue this turn's delta and settle round bookkeeping.
    Returns (reply, job); a job is a frozen review to run after the lock is
    released, holding the review lock until it is finished. One review runs
    at a time; a Stop during it leaves its edits queued for the next."""
    message = payload.get('last_assistant_message', '')
    if state['findings']:
        # Every reply while findings stand may be the one that applied or
        # rejected them; the next round sees them all.
        state['replies'].append(message)
    if state['cursor'] is None:
        state['notes'].append('baseline taken at this Stop (gate switched on mid-turn?); '
                              'earlier edits in this turn are not reviewed')
    queue_delta(root, directory, state, snapshot(root, directory / 'blobs'))
    if state['errors']:
        state['open_turn'] = False
        return reply(state, incomplete(state)), None
    options = {**DEFAULTS, **read_marker(marker_path(root))}  # may raise; nothing to undo yet
    lock = (directory / 'review.lock').open('w')
    try:
        fcntl.flock(lock, fcntl.LOCK_EX | fcntl.LOCK_NB)
    except BlockingIOError:
        # The kernel drops the lock with a dead hook process, so its still
        # queued batches simply wait for the next Stop.
        lock.close()
        return reply(state, 'a review is still running; edits since stay queued'), None
    changes = coalesce(state['batches'])
    if not changes:
        state['batches'], state['open_turn'] = [], False
        lock.close()
        return reply(state), None
    if state['rounds'] >= ROUNDS:
        state['open_turn'] = False
        lock.close()
        return reply(state, f'round limit reached; {len(state["batches"])} batch(es) of edits after '
                            f'challenge {ROUNDS} stay queued for the next turn'), None
    state['rounds'] += 1
    job = {'lock': lock, 'taken': len(state['batches']), 'changes': changes,
           'findings': state['findings'], 'replies': state['replies'] + [message],
           'options': options, 'round': state['rounds']}
    return None, job


def finish(root, directory, state, job, findings, error, skipped=None):
    """Under the lock again: fold the review outcome into the state."""
    blocked = bool(findings) and not error
    if not blocked:
        # The turn closes here: edits made while Codex ran (a background task,
        # a peer) are queued now. A blocked turn's next Stop queues them.
        try:
            queue_delta(root, directory, state, snapshot(root, directory / 'blobs'))
        except (OSError, ValueError, subprocess.SubprocessError) as failure:
            state['errors'].append(f'edits made during the review could not be captured: {failure}')
    if error:
        state['errors'].append(f'{error}; logs in {directory}')
    # The turn stays open while edits since the cursor are still this
    # session's: corrections to a block, or edits a failed snapshot missed. A
    # turn begun while Codex ran (its prompt reset the rounds) is not ours to close.
    if state['rounds'] >= job['round']:
        state['open_turn'] = blocked or bool(state['errors'])
    if error:
        return reply(state, incomplete(state) + '; continuing')
    del state['batches'][:job['taken']]
    if skipped:
        # Retrying the same input would fail the same way, so it is dropped, not queued.
        state['replies'] = []
        missed = incomplete(state) + '; ' if state['errors'] else ''
        return reply(state, missed + f'challenge skipped: {skipped}; {job["taken"]} batch(es) of edits were not reviewed')
    state['findings'], state['replies'] = findings, []
    round_number = job['round']
    if not findings:
        missed = incomplete(state) + '; ' if state['errors'] else ''
        return reply(state, missed + f'YAGNI challenge round {round_number}: no findings.')
    listed = '\n'.join(f"- {f['file']}:{f['line']}: {f['problem']} Alternative: {f['alternative']}"
                       for f in findings)
    scope = "reject findings outside this turn's work by you or a coworker acting for you"
    if round_number < ROUNDS:
        head_text = (f'{CHALLENGE} (round {round_number} of {ROUNDS}): apply each finding or reject it with '
                     f'a reason; {scope}; a follow-up review runs after edits.')
    else:
        head_text = (f'{CHALLENGE} (round {ROUNDS}, final): apply each finding or reject it with a reason '
                     f'in your reply; {scope}; no further review runs.')
    notes = reply(state).get('systemMessage', '')
    prefix = notes.removeprefix('simplifyGate: ') + '; ' if notes else ''
    return {'decision': 'block', 'reason': prefix + head_text + '\n' + listed}


def new_state():
    return {'cursor': None, 'open_turn': False, 'batches': [], 'rounds': 0, 'findings': [],
            'replies': [], 'errors': [], 'notes': []}


class Locked:
    """The state file under an exclusive lock, written back on exit."""

    def __init__(self, directory):
        self.directory = directory

    def __enter__(self):
        self.lock = (self.directory / 'lock').open('w')
        fcntl.flock(self.lock, fcntl.LOCK_EX)
        path = self.directory / 'state.json'
        loaded = json.loads(path.read_text()) if path.exists() else {}
        if loaded and set(loaded) != set(new_state()):
            # State written by an older version of this hook has no usable
            # shape; start over and say so at the next Stop rather than crash.
            loaded = {'notes': ['state from an older gate version was discarded']}
        self.state = {**new_state(), **loaded}
        return self.state

    def __exit__(self, *exc):
        path = self.directory / 'state.json'
        temporary = path.with_suffix('.tmp')
        temporary.write_text(json.dumps(self.state))
        temporary.replace(path)
        self.lock.close()


def handle(root, directory, payload):
    directory.mkdir(parents=True, exist_ok=True, mode=0o700)
    (directory / 'blobs').mkdir(exist_ok=True)
    event = payload['hook_event_name']
    answer, job = {}, None
    with Locked(directory) as state:
        try:
            if event == 'UserPromptSubmit':
                if not FED_BACK.search(payload.get('prompt', '')):  # a blocked Stop fed back is not a new turn
                    now = snapshot(root, directory / 'blobs')
                    if state['open_turn']:
                        # Interrupted before Stop, or a review still running:
                        # edits since the cursor are this session's, not dirt.
                        queue_delta(root, directory, state, now)
                    state['cursor'] = now
                    state.update(open_turn=True, rounds=0, errors=[])
            elif event == 'Stop':
                answer, job = stop(root, directory, state, payload)
        except (OSError, ValueError, subprocess.SubprocessError) as error:
            state['errors'].append(str(error))
            answer = reply(state, incomplete(state))
    if job is None:
        return answer
    # Codex may take minutes; a prompt hook waiting on the lock would die at 30 s.
    try:
        skipped = None
        try:
            findings, error = review(root, directory, job), None
        except Oversized as failure:
            findings, error, skipped = None, None, str(failure)
        except RuntimeError as failure:
            findings, error = None, f'codex failed twice ({failure})'
        except (OSError, ValueError, subprocess.SubprocessError) as failure:
            findings, error = None, str(failure)
        with Locked(directory) as state:
            return finish(root, directory, state, job, findings, error, skipped)
    finally:
        job['lock'].close()


# --- retention ----------------------------------------------------------------

SESSION_FILES = {'state.json', 'lock', 'review.lock', 'blobs'}
BLOB_NAME = re.compile(r'[0-9a-f]{40}|[0-9a-f]{64}')


def cache_dir():
    return Path(os.environ.get('XDG_CACHE_HOME', Path.home() / '.cache')) / 'agentrc/simplify-gate'


def referenced(state):
    """Blob ids a later event may read from the session's store: both sides of
    every queued batch (the review renders them) and every cursor entry (the
    old side of the next delta)."""
    entries = [entry for batch in state['batches'] for pair in batch.values() for entry in pair]
    if state['cursor'] is not None:
        entries += state['cursor']['files'].values()
    return {entry[1] for entry in entries if entry}


def settled(state):
    """Nothing left for a review or for Claude: no queued batch, no standing
    finding, no error."""
    return not (state['batches'] or state['findings'] or state['errors'])


def load_state(directory):
    """The session's state, or None when it is missing, unreadable or of another shape."""
    try:
        state = json.loads((directory / 'state.json').read_text())
        if isinstance(state, dict) and set(state) == set(new_state()):
            referenced(state)
            return state
    except (OSError, ValueError, TypeError, KeyError, AttributeError, IndexError):
        pass
    return None


def is_link(path):
    return stat.S_ISLNK(path.lstat().st_mode)


def symlink_in(directory):
    """The first symlink among a session's files and blobs, or None. Prune
    never follows or changes anything in a session holding one."""
    paths = list(directory.iterdir())
    blobs = directory / 'blobs'
    if blobs in paths and stat.S_ISDIR(blobs.lstat().st_mode):
        paths += blobs.iterdir()
    return next((path for path in paths if is_link(path)), None)


@contextlib.contextmanager
def held(path, create):
    """`path` flocked without waiting; BlockingIOError when a hook holds it.
    A missing lock file is created only when `create`, else it is not held."""
    try:
        fd = os.open(path, os.O_RDONLY | os.O_NOFOLLOW | (os.O_CREAT if create else 0), 0o644)
    except FileNotFoundError:
        yield
        return
    try:
        fcntl.flock(fd, fcntl.LOCK_EX | fcntl.LOCK_NB)
        yield
    finally:
        os.close(fd)


class Pruner:
    """One pass over the cache. Link counts are tracked as the plan leaves
    them, so a dry run reports the bytes `apply` would free. Nothing is
    followed through a symlink: one in the store or a session is reported and
    its owner left alone."""

    def __init__(self, cache, apply):
        self.cache, self.apply = cache, apply
        self.links, self.freed, self.files = {}, Counter(), Counter()
        self.report = defaultdict(list)
        self.checked = {}  # inode -> whether its content matches its blob id
        self.store = {}  # blob id -> inode of its store copy, as the plan leaves the store
        store = cache / STORE
        try:
            self.store_usable = stat.S_ISDIR(store.lstat().st_mode)
        except FileNotFoundError:
            self.store_usable, store = True, None
        if not self.store_usable:
            self.note('symlinked or not a directory (left untouched)', cache / STORE)
        elif store:
            for path in store.iterdir():
                st = path.lstat()
                if stat.S_ISLNK(st.st_mode):
                    self.note('symlinked or not a directory (left untouched)', path)
                elif BLOB_NAME.fullmatch(path.name) and stat.S_ISREG(st.st_mode):
                    self.store[path.name] = self.inode(st)

    def note(self, heading, item):
        if str(item) not in self.report[heading]:
            self.report[heading].append(str(item))

    def inode(self, st):
        key = (st.st_dev, st.st_ino)
        self.links.setdefault(key, st.st_nlink)
        return key

    def intact(self, path, key):
        """Whether inode `key`, read at `path`, holds the blob its name says;
        hashed once per pass."""
        if key not in self.checked:
            st = path.lstat()
            self.checked[key] = (st.st_dev, st.st_ino) == key and stat.S_ISREG(st.st_mode) and intact(path)
        return self.checked[key]

    def forget(self, st, category):
        """Count one link of `st` gone; its bytes are freed with the last link."""
        key = self.inode(st)
        self.links[key] -= 1
        self.files[category] += 1
        if not self.links[key]:
            self.freed[category] += st.st_size

    def drop(self, path, category, st):
        if self.apply:
            path.unlink()
        self.forget(st, category)

    def relink(self, path, st):
        """Make a session blob a link to the store copy once both are checked
        against the blob id; the first good copy becomes the store's."""
        sha, key = path.name, self.inode(st)
        shared = self.cache / STORE / sha
        if not self.store_usable or self.store.get(sha) == key:
            return
        if not self.intact(path, key):
            self.note('corrupt blobs (left in place)', path)
            return
        if sha not in self.store:
            try:
                if self.apply:
                    shared.parent.mkdir(mode=0o700, exist_ok=True)
                    os.link(path, shared, follow_symlinks=False)
                self.links[key] += 1
                self.store[sha] = key
                return
            except FileExistsError:  # a hook stored it since the pass began
                self.store[sha] = self.inode(shared.lstat())
        store_key = self.store[sha]
        if not self.intact(shared, store_key):
            self.note('corrupt blobs (left in place)', shared)
            return
        if self.apply:
            temporary = path.with_name(TEMP + sha)
            temporary.unlink(missing_ok=True)
            os.link(shared, temporary, follow_symlinks=False)
            if self.inode(temporary.lstat()) != store_key:
                temporary.unlink()
                raise OSError(f'{shared} changed during the pass')
            os.replace(temporary, path)
        self.links[store_key] += 1
        self.forget(st, 'relinked duplicates')

    def session(self, directory, now, days, report_days):
        # Checked before locking, so nothing is created in a session left alone.
        if symlink_in(directory) or load_state(directory) is None:
            self.unusable(directory)
            return
        try:
            with held(directory / 'lock', self.apply), held(directory / 'review.lock', False):
                # A Stop takes review.lock only under the state lock, so a missing one stays free.
                state = load_state(directory)
                if symlink_in(directory) or state is None:
                    self.unusable(directory)
                    return
                self.locked_session(directory, state, now, days, report_days)
        except BlockingIOError:
            self.note('busy (skipped)', directory)
        except OSError as error:
            self.note('failed', f'{directory}: {error}')

    def unusable(self, directory):
        link = symlink_in(directory)
        if link:
            self.note('symlinked or not a directory (left untouched)', link)
        else:
            self.note('unrecognized (left untouched)', directory)

    def locked_session(self, directory, state, now, days, report_days):
        entries = [(path, path.lstat()) for path in directory.iterdir()]
        files = [(path, st) for path, st in entries if path.name not in SESSION_FILES and stat.S_ISREG(st.st_mode)]
        idle = now - max([directory.lstat().st_mtime] + [st.st_mtime for _, st in entries])
        blobs = [(path, path.lstat()) for path in (directory / 'blobs').iterdir()] \
            if any(path.name == 'blobs' and stat.S_ISDIR(st.st_mode) for path, st in entries) else []
        if settled(state) and idle > days * 86400:
            for path, st in files:
                self.drop(path, 'diagnostics', st)
            keep = referenced(state)
            for path, st in blobs:
                if path.name not in keep and stat.S_ISREG(st.st_mode):
                    self.drop(path, 'unreferenced blobs', st)
            blobs = [(p, st) for p, st in blobs if p.name in keep]
        elif not settled(state) and idle > report_days * 86400:
            self.report[f'unresolved, inactive over {report_days} days (kept)'].append(
                f'{directory}: {idle / 86400:.0f} days; {len(state["batches"])} batch(es), '
                f'{len(state["findings"])} finding(s), {len(state["errors"])} error(s)')
        for path, st in blobs:
            if stat.S_ISREG(st.st_mode) and BLOB_NAME.fullmatch(path.name):
                self.relink(path, st)

    def sweep(self, now):
        """Drop store copies no session links any more, and stale partial writes."""
        if not self.store_usable or not (self.cache / STORE).exists():
            return
        for path in (self.cache / STORE).iterdir():
            st = path.lstat()
            if not stat.S_ISREG(st.st_mode):
                continue
            if path.name.startswith(TEMP):
                if now - st.st_mtime > 86400:
                    self.drop(path, 'unused store blobs', st)
            elif self.links.get(key := self.inode(st)) == 1 and self.checked.get(key) is not False \
                    and (not self.apply or path.lstat().st_nlink == 1):
                self.drop(path, 'unused store blobs', st)


def size(count):
    for unit in ('B', 'KB', 'MB', 'GB'):
        if count < 1024 or unit == 'GB':
            return f'{count:.1f} {unit}' if unit != 'B' else f'{count} B'
        count /= 1024


def prune(cache, apply=False, days=PRUNE_DAYS, report_days=REPORT_DAYS, now=None):
    """Free what settled sessions no longer need; report, never delete, the rest.
    Never removes a session directory, its state, its lock files or a blob its
    state references. Returns the report text."""
    now = time.time() if now is None else now
    pruner = Pruner(cache, apply)
    sessions = sorted(p for p in cache.iterdir() if p.name != STORE) if cache.is_dir() else []
    for directory in sessions:
        if stat.S_ISDIR(directory.lstat().st_mode):
            pruner.session(directory, now, days, report_days)
        else:
            pruner.note('symlinked or not a directory (left untouched)', directory)
    pruner.sweep(now)
    verb = 'freed' if apply else 'would free (dry run; --apply to act)'
    lines = [f'simplify-gate prune: {cache}, {len(sessions)} session(s); settled sessions idle over {days} days expire',
             f'{verb}:']
    for category in ('diagnostics', 'unreferenced blobs', 'relinked duplicates', 'unused store blobs'):
        lines.append(f'  {category:20} {size(pruner.freed[category]):>10}  ({pruner.files[category]} file(s))')
    lines.append(f'  {"total":20} {size(sum(pruner.freed.values())):>10}')
    for heading, items in pruner.report.items():
        lines.append(f'{heading}: {len(items)}')
        lines.extend('  ' + item for item in items)
    return '\n'.join(lines)


def chief_session(transcript_path):
    """True when the transcript's header records the chief agent; unreadable means no."""
    try:
        with open(transcript_path, encoding='utf-8') as transcript:
            for line in transcript:
                record = json.loads(line)
                if not isinstance(record, dict):
                    return False
                if record.get('type') in ('user', 'assistant'):
                    return False
                if record.get('type') == 'agent-setting':
                    return record.get('agentSetting') == 'chief'
    except (TypeError, OSError, ValueError):
        pass
    return False


def main():
    if os.environ.get('COWORK_TURN'):
        return  # a headless coworker turn (skills/cowork); the caller reviews it
    payload = json.load(sys.stdin)
    if chief_session(payload.get('transcript_path')):
        return  # chief runs its own simplification pass (agents/chief.md)
    # A Bash call can leave its shell in another directory and the payload's
    # cwd follows it; the project directory names the checkout under review.
    project = os.environ.get('CLAUDE_PROJECT_DIR') or payload['cwd']
    try:
        root = Path(os.fsdecode(git(project, 'rev-parse', '--show-toplevel')).strip())
        if not marker_path(root).exists():
            return
    except subprocess.CalledProcessError:
        return  # not a checkout of anything
    reply = handle(root, cache_dir() / digest([str(root), payload['session_id']]), payload)
    if reply:
        print(json.dumps(reply))


if __name__ == '__main__':
    main()
