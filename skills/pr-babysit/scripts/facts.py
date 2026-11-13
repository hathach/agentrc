"""What pr-babysit's scripts share: the {"error"} line contract, strict reads and the repository gh names.

A gh call GitHub refused for a rate limit raises RateLimited; a script whose
caller reads it (report(limits=True)) also prints rateLimit {observedAt, resetAt,
secondary} on that error line: resetAt the latest reset of a resource `gh api
rate_limit` (which spends no quota) shows exhausted, null when it shows none, and
secondary when none is exhausted or gh names a secondary limit."""

import argparse
import base64
import errno
import json
import os
import re
import shutil
import subprocess
import sys
import tempfile
import time
from contextlib import contextmanager
from pathlib import Path

try:
    import fcntl
except ImportError:  # Windows
    fcntl = None
    import msvcrt

sys.path.insert(0, str(Path(__file__).resolve().parents[3] / 'lib'))
from seal import canonical, emit, fnv1a, module_text, rate_limit, sealed, utc  # noqa: E402,F401  re-exported for the scripts
import gh_repo  # noqa: E402
import windows  # noqa: E402
from gh_repo import HOST, REPO, github_repo  # noqa: E402,F401  re-exported for the scripts

FULL_SHA = re.compile(r'\A[0-9a-f]{40}\Z')  # \Z: $ would let a trailing newline through
# The repository bind_repo named: every gh call run and attempt make carries it as GH_REPO.
GH_REPO = None
# The human is the sole author: no line may credit an agent, model, tool or session, nor link a Claude or ChatGPT session anywhere.
# ASCII on purpose: \b ends at a non-ASCII letter, and Unicode case folds (ſession) are not recognized spellings.
ATTRIBUTION = tuple(re.compile(p, re.I | re.ASCII) for p in (
    r'^[ \t]*co-authored-by[ \t]*:',
    r'^[ \t]*(([a-z]+-)+session(-[a-z]+)*|session-(url|id|link))[ \t]*:',
    r'^[ \t]*(🤖[ \t]*)?(generated|authored|written|created|made)[ \t-]*(with|by)[ \t]*:?[ \t]*\[?'
    r'(claude|codex|chatgpt|gpt|copilot|openai|anthropic|an? (ai|llm|agent))\b',
    r'claude\.ai/code/session_|chatgpt\.com/(c|codex|share)/',
))


def attribution_in(text):
    """The first line of text that credits an agent or links a session, stripped; None when there is none."""
    return next((line.strip() for line in text.split('\n') if any(p.search(line) for p in ATTRIBUTION)), None)


class Unusable(Exception):
    pass


class RateLimited(Unusable):
    def __init__(self, message, limit):
        super().__init__(message)
        self.limit = limit


class Parser(argparse.ArgumentParser):
    def error(self, message):
        raise Unusable(f'usage: {message}')


def checkout_repo():
    try:
        return gh_repo.checkout_repo()
    except gh_repo.Unresolved as e:
        raise Unusable(f"{e}: name the PR's repository (--repo OWNER/NAME)") from None


def bind_repo(name=None):
    """Name OWNER/NAME, else the checkout's one repository, in every later gh call: GH_REPO, which `gh pr` and gh api's
    {owner}/{repo} follow over the checkout's remotes (`gh repo view` does not, so no script asks it)."""
    global GH_REPO
    name = checkout_repo() if name is None else name
    if not REPO.match(name):
        raise Unusable(f'not a repository OWNER/NAME: {name!r}')
    GH_REPO = name
    return name


def gh_env(argv):
    return {**os.environ, 'GH_REPO': GH_REPO} if argv[0] == 'gh' and GH_REPO else None


def run(*argv, ok=(0,)):
    """(exit status, stdout); a status outside ok, or stdout that is not UTF-8, is Unusable."""
    try:
        done = subprocess.run(argv, capture_output=True, env=gh_env(argv))
    except FileNotFoundError:
        raise Unusable(f'{argv[0]} is not installed')
    if done.returncode not in ok:
        err = done.stderr.decode(errors='replace').strip()
        message = f"{' '.join(argv)}: {err}"
        limit = rate_limit(err) if argv[0] == 'gh' else None
        raise RateLimited(message, limit) if limit else Unusable(message)
    try:
        return done.returncode, done.stdout.decode()
    except UnicodeDecodeError:
        # Replacing the bad bytes could make two different paths read as one.
        raise Unusable(f"{' '.join(argv)}: output is not UTF-8")


def attempt(*argv, input=None, env=None):
    """(exit status, stdout, stderr), replacing bad bytes: unlike run, a failure is part of the receipt."""
    try:
        # bytes in: a text-mode stdin on Windows writes each \n as \r\n
        done = subprocess.run(argv, capture_output=True, env=env or gh_env(argv),
                              input=None if input is None else input.encode('utf-8', 'replace'))
    except FileNotFoundError:
        raise Unusable(f'{argv[0]} is not installed')
    text = lambda b: b.decode('utf-8', 'replace').replace('\r\n', '\n').replace('\r', '\n')  # as text mode reads it
    return done.returncode, text(done.stdout), text(done.stderr)


def git(*argv):
    return run('git', *argv)[1]


def status(*paths):
    """`git status --porcelain -z` records under PATHS, taken literally (all when none): each untracked file its own,
    not one `?? dir/` record no file scope can hold (#322); a rename's or copy's source is the record after it."""
    return [r for r in git('--literal-pathspecs', 'status', '--porcelain', '-z', '--untracked-files=all', '--', *paths).split('\0') if r]


def checkout_top():
    """The checkout's top level, Unusable unless it is the current directory."""
    top = git('rev-parse', '--show-toplevel').strip()
    if os.path.realpath(top) != os.path.realpath('.'):
        raise Unusable(f'run from the checkout top level {top}, not {os.getcwd()}')
    return top


class Busy(Unusable):
    """Another process held the lock past the wait asked for."""


@contextmanager
def locked(path, timeout=None):
    """path's .lock beside it held exclusively for the block, waiting while another process holds it: for good, or up
    to timeout seconds, then Busy."""
    path.parent.mkdir(parents=True, exist_ok=True)
    lock = path.with_suffix('.lock')
    deadline = None if timeout is None else time.monotonic() + timeout

    def wait():
        if deadline is not None and time.monotonic() >= deadline:
            raise Busy(f'{lock} stayed held by another process for {timeout:.0f} seconds')
        time.sleep(0.05)
    with open(lock, 'a') as fd:
        if fcntl:
            while True:
                try:
                    fcntl.flock(fd, fcntl.LOCK_EX | (0 if deadline is None else fcntl.LOCK_NB))
                    break
                except BlockingIOError:
                    wait()
            yield
        else:
            # msvcrt locks are mandatory and per handle: lock a byte past the content; LK_LOCK gives up after ~10 s.
            os.lseek(fd.fileno(), 1 << 30, os.SEEK_SET)
            while True:
                try:
                    msvcrt.locking(fd.fileno(), msvcrt.LK_NBLCK, 1)
                    break
                except OSError as e:
                    if e.errno not in (errno.EACCES, errno.EDEADLOCK):
                        raise
                    wait()
            try:
                yield
            finally:
                # Windows may release a closed handle's lock only after a delay.
                os.lseek(fd.fileno(), 1 << 30, os.SEEK_SET)
                msvcrt.locking(fd.fileno(), msvcrt.LK_UNLCK, 1)


def fsync_dir(path):
    """path's entries on disk: a file created or renamed in it survives a host crash (POSIX; Windows has no such call)."""
    if os.name == 'nt':
        return
    fd = os.open(path, os.O_RDONLY)
    try:
        os.fsync(fd)
    finally:
        os.close(fd)


def record_dir(kind):
    """pr-babysit/<kind> under the checkout's git common dir, where a script keeps the records of what it sent; each
    directory made for it durable in its parent."""
    path = Path(git('rev-parse', '--path-format=absolute', '--git-common-dir').strip()) / 'pr-babysit' / kind
    missing, p = [], path
    while not p.exists():
        missing.append(p)
        p = p.parent
    path.mkdir(parents=True, exist_ok=True)
    for made in reversed(missing):
        fsync_dir(made.parent)
    return path


def read_record(path):
    """The JSON record at path, None when there is none; Unusable when it does not read. Its shape is the caller's."""
    try:
        return json.loads(path.read_text(encoding='utf-8'))
    except FileNotFoundError:
        return None
    except (OSError, ValueError) as e:
        raise Unusable(f'attempt record {path} unreadable: {e}')


def write_record(path, record, new=False):
    """record on disk, durably, before anything acts on it: a new one created exclusively (FileExistsError when one is
    there), a later one replaced whole through path.tmp; the file fsynced, then its directory, so a step written before
    its request never reverts after a host crash while the request landed."""
    target = path if new else path.with_name(path.name + '.tmp')
    fd = os.open(target, os.O_CREAT | os.O_WRONLY | (os.O_EXCL if new else os.O_TRUNC))
    with os.fdopen(fd, 'w', encoding='utf-8') as f:
        json.dump(record, f)
        f.flush()
        os.fsync(f.fileno())
    if not new:
        windows.replace(target, path)  # Windows: retried while another process has the record open
    fsync_dir(path.parent)


def report(collect, argv, seal=False, limits=False):
    """Emit collect(argv) as the last stdout line (the AGENTRC_EMIT file instead, seal.emit), sealed when asked, and
    return 0, or {"error"} and 2."""
    try:
        facts = collect(argv)
    except Unusable as e:
        return emit({'error': str(e), **({'rateLimit': e.limit} if limits and isinstance(e, RateLimited) else {})}, 2)
    return emit(sealed(facts) if seal else facts, 0)


EMIT_KEEP = 14 * 86400  # seconds an emit directory outlives its last write: a launch resumed later reloads its files
# In each emit directory, written once at its creation: when its launch began, which bounds the transcripts a stage reads.
LAUNCH_MARK = '.launch'


def emit_dir():
    """A new directory for one workflow launch's emitted lines, under the checkout's git common dir, its LAUNCH_MARK
    written; directories there unwritten for EMIT_KEEP go."""
    parent = Path(git('rev-parse', '--path-format=absolute', '--git-common-dir').strip()) / 'agentrc-emit'
    parent.mkdir(exist_ok=True)
    for old in parent.iterdir():
        try:
            if old.is_dir() and time.time() - old.stat().st_mtime > EMIT_KEEP:
                shutil.rmtree(old)
        except OSError:
            pass
    made = Path(tempfile.mkdtemp(prefix='', dir=parent))
    with open(made / LAUNCH_MARK, 'x', encoding='utf-8') as f:
        f.write(f'{time.time()}\n')
    return {'dir': made.as_posix()}


def launch_began(directory):
    """When the launch whose emit directory this is began: its LAUNCH_MARK, written once by emit_dir; None for a
    directory an emit_dir before the mark made, which a resumed launch still names (never marked now: that would leave
    out the transcripts written before)."""
    mark = Path(directory) / LAUNCH_MARK
    try:
        return float(mark.read_text(encoding='utf-8'))
    except FileNotFoundError:
        return None
    except (OSError, ValueError) as e:
        raise Unusable(f'facts.py stage: {mark} does not tell when the launch began ({e}): no transcript scan is bounded by it') from None


def staged_bytes(text, digest, file):
    """text, base64 of UTF-8 JSON, as bytes once its fnv1a is digest."""
    try:
        data = base64.b64decode(text, validate=True)
        decoded = data.decode('utf-8')
        json.loads(decoded)
    except ValueError as e:  # binascii.Error, UnicodeDecodeError and JSONDecodeError among them
        raise Unusable(f'facts.py stage: the payload for {file} is not base64 of UTF-8 JSON: {e}') from None
    if fnv1a(decoded) != digest:
        raise Unusable(f'facts.py stage: the payload for {file} is not the JSON sent')
    return data


SESSION_ID = re.compile(r'\A[0-9a-f]{8}-[0-9a-f]{4}-[0-9a-f]{4}-[0-9a-f]{4}-[0-9a-f]{12}\Z')
PAYLOAD = 'AGENTRC-PAYLOAD'
# The transcripts written since the launch began that are read, the bytes read from them in all, and the lines read
# for each one's first user record.
SCAN_FILES = 400
SCAN_SLACK_S = 2  # a filesystem's coarsest mtime step: a transcript the launch wrote is never older than its mark by more
SCAN_BYTES = 64 << 20
SCAN_LINES = 8
SCAN_RETRY_S = 2  # a relay's prompt is in its transcript before its first command runs; the one wait is for a slow write
TASK_FRAME = 'The computed task text follows:\n'


class Bounded(Exception):
    """The scan stopped at one of its bounds before reading every transcript."""


def task_text(record):
    """The computed task a workflow agent's first user record holds, each line as given: the harness indents every line
    of it two spaces, past its frame."""
    content = (record.get('message') or {}).get('content')
    text = content if isinstance(content, str) else ''.join(
        b.get('text') or '' for b in content or [] if isinstance(b, dict) and b.get('type') == 'text') if isinstance(content, list) else ''
    if TASK_FRAME in text:
        text = text.split(TASK_FRAME, 1)[1]
        return '\n'.join(line[2:] if line.startswith('  ') else line for line in text.split('\n'))
    return text


def mtime(path):
    try:
        return path.stat().st_mtime
    except OSError:
        return 0


def payloads(sid, file, digest, since):
    """(the payload texts this session's subagent transcripts written since `since` give FILE and DIGEST in their first
    user record, newest transcript first, how many were read); Bounded when there are more of them than SCAN_FILES, or
    the bytes bound stopped the scan first. An older transcript is an earlier launch's: none holds this launch's file.
    With since None (a launch with no LAUNCH_MARK) the newest are read up to either bound, which counts only when what
    was read holds no match."""
    root = Path(os.environ.get('CLAUDE_CONFIG_DIR') or Path.home() / '.claude')
    found = sorted((p for p in root.glob(f'projects/*/{sid}/subagents/**/agent-*.jsonl')
                    if since is None or mtime(p) >= since - SCAN_SLACK_S), key=mtime, reverse=True)
    if since is not None and len(found) > SCAN_FILES:
        raise Bounded(f'{len(found)} transcripts written since the launch began, over {SCAN_FILES}')
    head = f'{PAYLOAD} {file} {digest} '
    got, left = [], SCAN_BYTES
    for n, path in enumerate(found):
        if n == SCAN_FILES:  # since is None
            if got:
                return got, n
            raise Bounded(f'the {SCAN_FILES} newest of {len(found)} transcripts read, the launch having no {LAUNCH_MARK}')
        try:
            with open(path, encoding='utf-8', errors='replace') as f:
                for _ in range(SCAN_LINES):
                    line = f.readline(left + 1)
                    left -= len(line)
                    if left < 0:
                        if since is None and got:
                            return got, n
                        raise Bounded(f'{SCAN_BYTES} bytes read, in {n + 1} of {len(found)} transcripts')
                    if not line:
                        break
                    try:
                        record = json.loads(line)
                    except ValueError:
                        break  # a record still being written
                    if isinstance(record, dict) and record.get('type') == 'user':
                        got += [x[len(head):] for x in task_text(record).split('\n') if x.startswith(head)]
                        break
        except OSError:
            continue
    return got, len(found)


def stage(file, digest, since):
    """Write to file the JSON the workflow sent, once it decodes to UTF-8 JSON whose fnv1a is digest: the bytes as sent,
    so no shell quoting meets the text, from the AGENTRC-PAYLOAD line naming file and digest in this session's subagent
    transcripts written since the launch began (`since`): the relay's own prompt, which no model retypes (#540)."""
    sid = os.environ.get('CLAUDE_CODE_SESSION_ID') or ''
    if not SESSION_ID.match(sid):
        raise Unusable(f'facts.py stage: CLAUDE_CODE_SESSION_ID {sid!r} names no session, so no transcript holds the payload')
    for wait in (SCAN_RETRY_S, None):
        try:
            got, scanned = payloads(sid, file, digest, since)
        except Bounded as e:
            raise Unusable(f'facts.py stage: the transcript scan stopped at its bound ({e}) before finding the payload for {file}') from None
        if got or wait is None:
            break
        time.sleep(wait)
    if not got:
        which = 'written since the launch began' if since is not None else f'newest (the launch has no {LAUNCH_MARK})'
        raise Unusable(f'facts.py stage: no {PAYLOAD} line for {file} {digest} in the first user record of '
                       f"this session's {scanned} subagent transcript(s) {which}, read twice")
    if len(set(got)) > 1:
        raise Unusable(f'facts.py stage: {len(set(got))} different {PAYLOAD} lines name {file} {digest}: none is taken')
    data = staged_bytes(got[0], digest, file)
    try:
        Path(file).write_bytes(data)
    except OSError as e:
        raise Unusable(f'facts.py stage: staged input not written: {e}') from None
    return {'staged': file}


def stage_line(path, line):
    """The stage's own line at path, as lib/seal.py's emit writes a script's, so the workflow tells a refused stage from
    a script whose outcome is unknown. Replaced whole on every run, never claimed once as an AGENTRC_EMIT file is: a
    resumed command stages again and goes on to its script."""
    tmp = Path(f'{path}.tmp')
    try:
        tmp.write_text(module_text(path, line), encoding='utf-8', newline='\n')
        windows.replace(tmp, path)
    except OSError as e:
        raise Unusable(f'facts.py stage: its line not written to {path}: {e}') from None


def collect(argv):
    if argv == ['repo']:
        return {'repo': checkout_repo()}
    if argv == ['emit-dir']:
        return emit_dir()
    if argv[:1] == ['stage']:
        p = Parser(prog='facts.py stage')
        p.add_argument('file')
        p.add_argument('digest')
        p.add_argument('--from-transcript', action='store_true', required=True)
        p.add_argument('--line', required=True)
        p.add_argument('--claimed', required=True)
        a = p.parse_args(argv[1:])
        # Whether an earlier run of the command claimed its script's AGENTRC_EMIT file: that script may have acted.
        claimed = Path(a.claimed).exists()
        try:
            out = stage(a.file, a.digest, launch_began(Path(a.line).parent))
        except Unusable as e:
            stage_line(a.line, {'error': str(e), 'claimed': claimed})
            raise
        stage_line(a.line, {**out, 'claimed': claimed})
        return out
    raise Unusable('usage: facts.py repo | emit-dir | stage FILE DIGEST --from-transcript --line PATH --claimed PATH')


# `repo`: the checkout's one repository, for a prompt that must name it (fix-issue's triage); `emit-dir`: a workflow's
# AGENTRC_EMIT directory; `stage`: a relay command's script input, written before the script runs, from the relay's
# own prompt (CLAUDE_CONFIG_DIR, else ~/.claude, and CLAUDE_CODE_SESSION_ID name its transcripts), its line written to
# --line PATH in the launch's emit directory, saying whether --claimed PATH (the script's emit file) existed.
if __name__ == '__main__':
    sys.exit(report(collect, sys.argv[1:]))
