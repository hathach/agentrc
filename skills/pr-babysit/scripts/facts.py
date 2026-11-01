"""What pr-babysit's scripts share: the {"error"} line contract, strict reads and the repository gh names.

A gh call GitHub refused for a rate limit raises RateLimited; a script whose
caller reads it (report(limits=True)) also prints rateLimit {observedAt, resetAt,
secondary} on that error line: resetAt the latest reset of a resource `gh api
rate_limit` (which spends no quota) shows exhausted, null when it shows none, and
secondary when none is exhausted or gh names a secondary limit."""

import argparse
import errno
import json
import os
import re
import subprocess
import sys
import time
from contextlib import contextmanager
from pathlib import Path

try:
    import fcntl
except ImportError:  # Windows
    fcntl = None
    import msvcrt

sys.path.insert(0, str(Path(__file__).resolve().parents[3] / 'lib'))
from seal import canonical, fnv1a, rate_limit, sealed, utc  # noqa: E402,F401  re-exported for the scripts
import gh_repo  # noqa: E402
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


def attempt(*argv, input=None):
    """(exit status, stdout, stderr), replacing bad bytes: unlike run, a failure is part of the receipt."""
    try:
        # bytes in: a text-mode stdin on Windows writes each \n as \r\n
        done = subprocess.run(argv, capture_output=True, env=gh_env(argv),
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
    """Print collect(argv) as the last stdout line, sealed when asked, and return 0, or {"error"} and 2."""
    try:
        facts = collect(argv)
    except Unusable as e:
        print(json.dumps({'error': str(e), **({'rateLimit': e.limit} if limits and isinstance(e, RateLimited) else {})}))
        return 2
    print(json.dumps(sealed(facts) if seal else facts))
    return 0


def collect(argv):
    if argv == ['repo']:
        return {'repo': checkout_repo()}
    raise Unusable('usage: facts.py repo')


if __name__ == '__main__':  # `repo`: the checkout's one repository, for a prompt that must name it (fix-issue's triage)
    sys.exit(report(collect, sys.argv[1:]))
