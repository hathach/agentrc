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
from seal import canonical, emit, fnv1a, rate_limit, sealed, utc  # noqa: E402,F401  re-exported for the scripts
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


@contextmanager
def locked(path):
    """path's .lock beside it held exclusively for the block, waiting while another process holds it."""
    path.parent.mkdir(parents=True, exist_ok=True)
    with open(path.with_suffix('.lock'), 'a') as fd:
        if fcntl:
            fcntl.flock(fd, fcntl.LOCK_EX)
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
                    time.sleep(0.05)
            try:
                yield
            finally:
                # Windows may release a closed handle's lock only after a delay.
                os.lseek(fd.fileno(), 1 << 30, os.SEEK_SET)
                msvcrt.locking(fd.fileno(), msvcrt.LK_UNLCK, 1)


def report(collect, argv, seal=False, limits=False):
    """Emit collect(argv) as the last stdout line (the AGENTRC_EMIT file instead, seal.emit), sealed when asked, and
    return 0, or {"error"} and 2."""
    try:
        facts = collect(argv)
    except Unusable as e:
        return emit({'error': str(e), **({'rateLimit': e.limit} if limits and isinstance(e, RateLimited) else {})}, 2)
    return emit(sealed(facts) if seal else facts, 0)


EMIT_KEEP = 14 * 86400  # seconds an emit directory outlives its last write: a launch resumed later reloads its files


def emit_dir():
    """A new directory for one workflow launch's emitted lines, under the checkout's git common dir; directories there
    unwritten for EMIT_KEEP go."""
    parent = Path(git('rev-parse', '--path-format=absolute', '--git-common-dir').strip()) / 'agentrc-emit'
    parent.mkdir(exist_ok=True)
    for old in parent.iterdir():
        try:
            if old.is_dir() and time.time() - old.stat().st_mtime > EMIT_KEEP:
                shutil.rmtree(old)
        except OSError:
            pass
    return {'dir': Path(tempfile.mkdtemp(prefix='', dir=parent)).as_posix()}


def staged_bytes(text, digest, where):
    """text, base64 of UTF-8 JSON, as bytes once its fnv1a is digest."""
    try:
        data = base64.b64decode(text, validate=True)
        decoded = data.decode('utf-8')
        json.loads(decoded)
    except ValueError as e:  # binascii.Error, UnicodeDecodeError and JSONDecodeError among them
        raise Unusable(f'facts.py stage: {where} is not base64 of UTF-8 JSON: {e}') from None
    if fnv1a(decoded) != digest:
        raise Unusable(f'facts.py stage: {where} is not the JSON sent')
    return data


SESSION_ID = re.compile(r'\A[0-9a-f]{8}-[0-9a-f]{4}-[0-9a-f]{4}-[0-9a-f]{4}-[0-9a-f]{12}\Z')
PAYLOAD = 'AGENTRC-PAYLOAD'
# The newest transcripts read, the bytes read from them in all, and the lines read for each one's first user record.
SCAN_FILES = 400
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


def payloads(sid, file, digest):
    """The payload texts this session's subagent transcripts give FILE and DIGEST in their first user record, newest
    transcript first; Bounded when a bound stopped the scan first."""
    root = Path(os.environ.get('CLAUDE_CONFIG_DIR') or Path.home() / '.claude')
    found = sorted(root.glob(f'projects/*/{sid}/subagents/**/agent-*.jsonl'), key=mtime, reverse=True)
    head = f'{PAYLOAD} {file} {digest} '
    got, left = [], SCAN_BYTES
    for n, path in enumerate(found):
        if n == SCAN_FILES:
            raise Bounded(f'{SCAN_FILES} of {len(found)} transcripts read')
        try:
            with open(path, encoding='utf-8', errors='replace') as f:
                for _ in range(SCAN_LINES):
                    line = f.readline(left + 1)
                    left -= len(line)
                    if left < 0:
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


def stage(file, digest, from_transcript=False):
    """Write to file the JSON the workflow sent, once it decodes to UTF-8 JSON whose fnv1a is digest: the bytes as sent,
    so no shell quoting meets the text. From stdin as base64, or, from_transcript, from the AGENTRC-PAYLOAD line naming
    file and digest in this session's subagent transcripts: the relay's own prompt, which no model retypes (#540)."""
    if from_transcript:
        sid = os.environ.get('CLAUDE_CODE_SESSION_ID') or ''
        if not SESSION_ID.match(sid):
            raise Unusable(f'facts.py stage: CLAUDE_CODE_SESSION_ID {sid!r} names no session, so no transcript holds the payload')
        for wait in (SCAN_RETRY_S, None):
            try:
                got, scanned = payloads(sid, file, digest)
            except Bounded as e:
                raise Unusable(f'facts.py stage: the transcript scan stopped at its bound ({e}) before finding the payload for {file}') from None
            if got or wait is None:
                break
            time.sleep(wait)
        if not got:
            raise Unusable(f'facts.py stage: no {PAYLOAD} line for {file} {digest} in the first user record of '
                           f"this session's {scanned} subagent transcript(s), read twice")
        if len(set(got)) > 1:
            raise Unusable(f'facts.py stage: {len(set(got))} different {PAYLOAD} lines name {file} {digest}: none is taken')
        data = staged_bytes(got[0], digest, f'the payload for {file}')
    else:
        data = staged_bytes(sys.stdin.buffer.read(), digest, 'staged input')
    try:
        Path(file).write_bytes(data)
    except OSError as e:
        raise Unusable(f'facts.py stage: staged input not written: {e}') from None
    return {'staged': file}


def stage_line(path, line):
    """The stage's own line at path, as lib/seal.py's emit writes a script's, so the workflow tells a refused stage from
    a script whose outcome is unknown. Replaced whole on every run, never claimed once as an AGENTRC_EMIT file is: a
    resumed command stages again and goes on to its script."""
    if path is None:
        return
    tmp = Path(f'{path}.tmp')
    try:
        tmp.write_text("export const meta = { name: 'emit', description: 'a script line' }\n"
                       f"return {json.dumps({'emit': path, 'line': line}, separators=(',', ':'))}\n", encoding='utf-8', newline='\n')
        windows.replace(tmp, path)
    except OSError as e:
        raise Unusable(f'facts.py stage: its line not written to {path}: {e}') from None


def collect(argv):
    if argv == ['repo']:
        return {'repo': checkout_repo()}
    if argv == ['emit-dir']:
        return emit_dir()
    if argv[:1] == ['stage'] and len(argv) == 3:
        return stage(*argv[1:])
    if argv[:1] == ['stage'] and argv[3:4] == ['--from-transcript'] and len(argv) in (4, 6) and argv[4:5] in ([], ['--line']):
        to = argv[5] if len(argv) == 6 else None
        try:
            out = stage(*argv[1:3], from_transcript=True)
        except Unusable as e:
            stage_line(to, {'error': str(e)})
            raise
        stage_line(to, out)
        return out
    raise Unusable('usage: facts.py repo | emit-dir | stage FILE DIGEST [--from-transcript [--line PATH]]')


# `repo`: the checkout's one repository, for a prompt that must name it (fix-issue's triage); `emit-dir`: a workflow's
# AGENTRC_EMIT directory; `stage`: a relay command's script input, written before the script runs, --from-transcript
# from the relay's own prompt (CLAUDE_CONFIG_DIR, else ~/.claude, and CLAUDE_CODE_SESSION_ID name its transcripts), its
# line also written to --line PATH.
if __name__ == '__main__':
    sys.exit(report(collect, sys.argv[1:]))
