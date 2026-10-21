#!/usr/bin/env python3
"""Remote HIL transport shared by the project adapters (`hil` skill): stage files from a dev
PC's checkout into a tree on a rig over ssh and rsync, run or launch a command there, wait for
it and fetch its artifacts back. Stdlib only, no project imports, no CLI: every host, path,
argv, environment variable and artifact path is a parameter. The adapter keeps its harness file
list, build layout, receipts, report names and CLI.

Two kinds of remote tree (Tree) share one guard (tree_guard): a shared hold on the tree's lease
lock plus this session's token inside the tree, so a transfer or run never lands in a tree
another session rebuilt.
- A wiped tree (remote_lease): rm -rf'd and recreated per run under an exclusive hold on
  <dir>.lock, run over one ssh session (run_remote), fetched (copy_back) while still leased.
Staging goes up by rsync; artifacts come back through the rig helper's `fetch`, which opens every
path component without following a link, so nothing outside the tree is read.
- An attempt (create_attempt): a create-only <base>/<id> dir under its own lease
  <base>/<id>.lease, never wiped or removed here (retention is the adapter's). It is staged
  until a detached launch (launch), then polled (attempt_status, wait_attempt) and fetched
  (collect) any number of times; nothing here launches it twice.

Local run records (run_recorded, wait_local) keep a wrapper's started/done pair under a runs
dir, the started record locked for the wrapper's life, on POSIX and Windows.

Refusals raise RemoteError, a SystemExit carrying `error: <why>`: uncaught, it ends a CLI with
that message and status 1.
"""

from __future__ import annotations

import argparse
import contextlib
import errno
import functools
import glob
import hashlib
import json
import os
import re
import secrets
import shlex
import shutil
import subprocess
import tarfile
import tempfile
import time
from pathlib import Path
from typing import NamedTuple

try:
    import fcntl
except ImportError:  # Windows
    fcntl = None
    import msvcrt


class RemoteError(SystemExit):
    def __init__(self, why):
        super().__init__(f'error: {why}')


def fail(why):
    raise RemoteError(why)


# A stalled link must end the run, not hang it: no subprocess timeout fits a HIL run, so ssh
# itself drops a peer silent for 30 s x 4.
SSH_OPTS = ('-o', 'ServerAliveInterval=30', '-o', 'ServerAliveCountMax=4', '-o', 'ConnectTimeout=20')

# The screen is for the tilde: the remote shell would expand `~/` alone to HOME, which a wiped
# tree rm -rf's. `/` or `~/` plus at least one named component, no `..`, no `//`.
REMOTE_DIR_RE = re.compile(r'^(/|~/)[A-Za-z0-9_.~/-]*[A-Za-z0-9_-]$')
_COMPONENT = re.compile(r'[A-Za-z0-9_.-]+')
_ENV_NAME = re.compile(r'[A-Za-z_][A-Za-z0-9_]*')

TOKEN_FILE = '.hil-remote-run'   # in every tree: the token of the session that owns it
RECORDS = '.attempt'             # an attempt's own records: launch.json, done.json, driver.lock, logs
RECORD_FILES = tuple(f'{RECORDS}/{n}' for n in ('launch.json', 'done.json', 'stdout.log', 'stderr.log'))
RESERVED = (RECORDS, TOKEN_FILE)  # top-level names of a tree the transport owns: never staged
FETCH_RESULT = 'hil-remote:result'   # the fetch stream's status entry; no artifact path can be it
# flasher CLIs live in ~/.local/bin and ~/bin, and a non-interactive ssh shell sources no profile
RIG_PATH = '$HOME/.local/bin:$HOME/bin'

# The run session's exit when the guard fails: nothing of ours ran there. Above a harness's
# 0-125, the shell's 126-127 and 128+signal, and below ssh's own 255.
TREE_REPLACED = 200
RIG_REFUSED = 64     # the rig helper refused before changing anything
RIG_SECS = 180       # one rig helper call: the lease wait (60 s) and a slow link, not a run

# under the Bash tool's 10-min foreground cap, so one wait is one tool call
WAIT_SECS = 570
POLL_SECS = 5
STATUS_MIN_SECS = 15   # a wait's last status poll: past its deadline by at most this
# a background launch writes its started record within this, or it never started
START_SECS = 30
# wait exit statuses beside 0 (done); refusals exit 1
RUNNING, DEAD, INCONCLUSIVE = 3, 4, 5


# --- paths ---------------------------------------------------------------------------------------
def _plain_remote_dir(d):
    return bool(REMOTE_DIR_RE.match(d)) and '..' not in d and '//' not in d


def check_remote_dir(remote_dir, what='REMOTE_DIR'):
    if not _plain_remote_dir(remote_dir):
        fail(f'{what} must be /path or ~/path of [A-Za-z0-9_.~/-], no "..", no "//", no trailing '
             f'slash: {remote_dir}')


def check_relative(path, what):
    """path as a posix string: relative, every component of [A-Za-z0-9_.-] and none `.` or
    `..`. What a tree-relative staging, artifact or build path must be, so it stays inside its
    root on both ends and needs no quoting."""
    parts = Path(path).parts
    if Path(path).is_absolute() or not parts or any(p in ('.', '..') for p in parts) \
            or not all(_COMPONENT.fullmatch(p) for p in parts):
        fail(f'{what} must be a relative path of [A-Za-z0-9_.-] components, none "." or "..": {path}')
    return Path(*parts).as_posix()


def check_run_id(run_id):
    if not re.fullmatch(r'[A-Za-z0-9_-][A-Za-z0-9_.-]{0,63}', run_id):
        fail(f'a run id is 1-64 of [A-Za-z0-9_.-], not starting with a dot: {run_id!r}')


def _write_path(rel, what):
    """A tree-relative path something is written to or run in: check_relative, `.` for the tree
    root, and never the tree's own token or records."""
    if rel == '.':
        return rel
    rel = check_relative(rel, what)
    if rel.split('/')[0] in RESERVED:
        fail(f'{what} must not be the tree\'s own records: {rel}')
    return rel


# --- transport -----------------------------------------------------------------------------------
class Tree(NamedTuple):
    """A remote tree this session may touch: the ssh host, its absolute dir on the rig, the
    lease lock file beside it, this session's token, and the local checkout rsync runs in."""
    host: str
    dir: str
    lock: str
    token: str
    root: Path

    def spec(self, rel='.'):
        """host:path of a tree-relative path, as rsync takes it."""
        return f'{self.host}:{self.dir}' if rel == '.' else f'{self.host}:{self.dir}/{rel}'


def local_operand(root, path):
    """A local file as rsync takes it. Windows rsync reads a drive letter as a remote host, so
    there the file is named relative to the checkout, where rsync runs."""
    if os.name != 'nt':
        return str(path)
    try:
        return Path(os.path.relpath(path, root)).as_posix()
    except ValueError:
        fail(f'{path} is not on the drive of the checkout {root}, so rsync cannot name it')


def rsync_program():
    """The rsync to run, None when there is none. Windows: an rsync.exe in an absolute PATH dir,
    which is all subprocess can start by name, and not one in the cwd, which CreateProcess
    searches first; a relative dir would name it from the cwd, not from the root where it runs."""
    if os.name != 'nt':
        return 'rsync' if shutil.which('rsync') else None
    for d in os.environ.get('PATH', '').split(os.pathsep):
        if os.path.isabs(d) and os.path.isfile(os.path.join(d, 'rsync.exe')):
            return os.path.join(d, 'rsync.exe')
    return None


def require_rsync():
    """Before anything remote: every transfer of a run needs it."""
    if not rsync_program():
        fail('rsync is not on PATH; the staging and the copy-back need it (on Windows, MSYS2\'s or Cygwin\'s rsync.exe)')


def tree_guard(tree, staging=False):
    """Shell that takes a shared hold on the tree's lease (kept by the exec that follows it) and
    checks the tree is still this session's; false once a wiper replaced it. `staging` also
    refuses an attempt whose launch was claimed."""
    guard = (f'exec 9>>{shlex.quote(tree.lock)} && flock -s -w 60 9 && '
             f'[ "$(cat {shlex.quote(tree.dir + "/" + TOKEN_FILE)} 2>/dev/null)" = {shlex.quote(tree.token)} ]')
    if staging:
        guard += f' && [ ! -e {shlex.quote(f"{tree.dir}/{RECORDS}/launch.claim")} ]'
    return guard


def rsync(tree, *args, optional=False, staging=False):
    """rsync with a remote endpoint under tree_guard(tree, staging): the remote rsync server
    starts only under a shared hold on the tree's lease with this session's token still in
    place. Local files are Paths; options and host:path specs are str, passed as they are. A
    staging transfer also excludes the RESERVED names at its root, which stage_tree and
    stage_files refuse outright."""
    args = [local_operand(tree.root, a) if isinstance(a, Path) else a for a in args]
    if staging:
        args = [*(x for n in RESERVED for x in (f'--exclude=/{n}', f'--exclude=/{n}/**')), *args]
    program = rsync_program() if os.name == 'nt' else 'rsync'
    if program is None:
        fail('rsync.exe is no longer on PATH')
    # rsync appends its --server arguments after --rsync-path, so `sh -c '...' rsync` makes
    # them "$@" of the guard shell, which execs the real rsync on them
    server = f'sh -c {shlex.quote(tree_guard(tree, staging) + " && exec rsync \"$@\"")} rsync'
    # an optional fetch (a green run writes no .failed) must not print rsync's code-23 error
    return subprocess.run([program, '-e', shlex.join(['ssh', *SSH_OPTS]), f'--rsync-path={server}', *args],
                          cwd=tree.root, stderr=subprocess.DEVNULL if optional else None).returncode


def ssh_argv(host, command):
    return ['ssh', *SSH_OPTS, host, command]


def _env_args(env):
    env = dict(env or {})
    bad = [k for k in env if not _ENV_NAME.fullmatch(k)]
    if bad:
        fail(f'not environment variable names: {", ".join(map(repr, bad))}')
    return env


def _check_argv(argv):
    if not argv or not all(isinstance(a, str) for a in argv) or '=' in argv[0]:
        fail(f'a remote argv is a non-empty list of str whose program has no "=": {argv!r}')
    return list(argv)


# --- a wiped tree --------------------------------------------------------------------------------
# Runs on the remote. $1 arrives shlex-quoted, so a leading ~/ is expanded here, where $HOME is
# a value; the second gate then sees the real rm -rf target. The lock sits beside the tree,
# which the wipe would take with it: exclusive for the wipe, then shared for the staging while
# the trailing cat keeps this session open, and the run session takes its own shared hold
# (run_command) so a wrapper that dies mid-run leaves the run's tree covered. A wiper needs the
# exclusive lock, which any shared holder refuses. The tree also carries this run's token: a
# run session that lost its lease (wrapper died between staging and the run, a wiper got in)
# finds another run's token, or none, and refuses the replacement tree.
SETUP_SCRIPT = r'''
set -e
case "$1" in
  "~/"*) d="$HOME/${1#"~/"}" ;;
  *) d="$1" ;;
esac
token=$2
shift 2
case "$d" in
  ''|/|"$HOME"|"$HOME"/) echo "refusing to rm -rf '$d'" >&2; exit 1 ;;
esac
command -v flock >/dev/null || { echo "flock not found on the rig" >&2; exit 1; }
mkdir -p -- "$(dirname -- "$d")"
exec 9>>"$d.lock"
flock -n -E 75 9 || {
  rc=$?
  case $rc in
    75) echo "another hil_remote run holds $d.lock -- not wiping its tree" >&2 ;;
    *) echo "flock on $d.lock failed (exit $rc) -- not wiping its tree" >&2 ;;
  esac
  exit 1
}
rm -rf -- "$d"
mkdir -p -- "$d"
for p do mkdir -p -- "$d/$p"; done
printf '%s\n' "$token" >"$d/.hil-remote-run"
flock -s 9
# on its own line: a rig shell that greets on stdout without a newline would glue the marker
# to it, and the wrapper would wait for a line that never comes
printf '\nHIL_REMOTE_DIR=%s\n' "$d"
cat >/dev/null
'''


@contextlib.contextmanager
def remote_lease(host, remote_dir, root, mkdirs=()):
    """Wipe and recreate remote_dir on host with mkdirs (tree-relative) in it, yielding its Tree
    while this ssh session holds its lock: another runner sharing remote_dir would otherwise
    rm -rf this run's tree. Every refusal here comes before the wipe."""
    check_remote_dir(remote_dir)
    mkdirs = [_write_path(p, 'a staging dir') for p in mkdirs]
    require_rsync()
    token = f'{os.getpid()}-{time.time_ns()}'
    # stderr to a file, not a pipe: nothing drains a pipe while the lease is held, and a remote
    # that writes more than the pipe buffer (rm -rf reporting hundreds of entries) would block
    # with the lock taken
    with tempfile.TemporaryFile('w+', encoding='utf-8', errors='replace') as err_file:
        lease = subprocess.Popen(
            ssh_argv(host, shlex.join(['bash', '-c', SETUP_SCRIPT, 'hil-setup', remote_dir, token, *mkdirs])),
            stdin=subprocess.PIPE, stdout=subprocess.PIPE, stderr=err_file, encoding='utf-8', errors='replace')
        marked = next((ln.rstrip('\n').partition('=')[2] for ln in lease.stdout
                       if ln.startswith('HIL_REMOTE_DIR=')), '')
        if not marked.startswith('/'):
            lease.communicate()
            err_file.seek(0)
            fail(f'remote setup failed (exit {lease.returncode}): {err_file.read().strip()}')
        try:
            yield Tree(host, marked, marked + '.lock', token, Path(root))
        finally:
            try:
                lease.communicate(timeout=30)
            except subprocess.TimeoutExpired:
                lease.kill()
                lease.wait()


def run_command(tree, argv, env=None, cwd='.'):
    """The remote shell line that runs argv in the tree (cwd tree-relative) under the guard,
    with env added and RIG_PATH ahead of PATH; it exits TREE_REPLACED when the tree is not
    this session's."""
    argv, env, cwd = _check_argv(argv), _env_args(env), _write_path(cwd, 'a run dir')
    where = tree.dir if cwd == '.' else f'{tree.dir}/{cwd}'
    gone = f"{tree.dir} is not this run's tree any more (wiped by another run?)"
    # fd 9 stays open across the exec, so the shared lock lives as long as the command does
    return (f'{{ {tree_guard(tree)}; }} || {{ echo {shlex.quote(gone)} >&2; exit {TREE_REPLACED}; }}; '
            f'cd {shlex.quote(where)} && export PATH="{RIG_PATH}:$PATH" && '
            f'exec env {shlex.join([*(f"{k}={v}" for k, v in env.items()), *argv])}')


def run_remote(tree, argv, env=None, cwd='.'):
    """Run argv in the tree over one ssh session, its output on ours; its exit status, or
    TREE_REPLACED when nothing of ours ran (no artifact of this run to fetch)."""
    return subprocess.run(ssh_argv(tree.host, run_command(tree, argv, env, cwd)),
                          stdin=subprocess.DEVNULL).returncode


# --- staging -------------------------------------------------------------------------------------
def digest(path):
    return hashlib.sha256(Path(path).read_bytes()).hexdigest()


def _under(base, f):
    try:
        return Path(f).relative_to(base).as_posix()
    except ValueError:
        fail(f'{f} is not under {base}')


def file_digests(base, files):
    """{path relative to base, posix: sha256} of files, each under base."""
    return {_under(base, f): digest(f) for f in files}


def snapshot(root, files, into):
    """Copy files, each under root, to the same root-relative layout under into, returning the
    file_digests of the copies: a build rewriting the tree after a check would otherwise send
    the rig bytes the check did not pin. Stage from into, not from root."""
    for f in files:
        dest = Path(into) / _under(root, f)
        dest.parent.mkdir(parents=True, exist_ok=True)
        shutil.copy2(f, dest)
    return file_digests(into, [Path(into) / _under(root, f) for f in files])


def differing(pinned, staged, prefixes=()):
    """Paths whose staged digest is not the pinned one, and pinned paths under prefixes that are
    not staged: what a run would send that a receipt or manifest does not pin."""
    return sorted({*(p for p in staged if pinned.get(p) != staged[p]),
                   *(p for p in pinned if p.startswith(tuple(prefixes)) and p not in staged)})


@functools.cache
def _git_local_vars():
    names = subprocess.run(['git', 'rev-parse', '--local-env-vars'], capture_output=True, text=True).stdout.split()
    if not names:
        fail('git rev-parse --local-env-vars listed nothing: cannot keep a caller\'s GIT_* off the checkout')
    return frozenset(names)


def git(root, *argv):
    """git in root, deaf to the GIT_* that would aim it elsewhere or reconfigure it (a hook's
    GIT_DIR, GIT_INDEX_FILE, GIT_CONFIG_PARAMETERS...)."""
    env = {k: v for k, v in os.environ.items() if k not in _git_local_vars()}
    out = subprocess.run(['git', '-C', str(root), *argv], capture_output=True, text=True, env=env)
    if out.returncode:
        fail(f'git {" ".join(argv)}: {out.stderr.strip()}')
    return out.stdout.strip()


def checkout_drift(root, head, files=None):
    """Why the checkout at root is not head on a clean tracked tree with files ({path, absolute
    or root-relative: sha256}) as pinned; empty when it is. What is staged from the live
    checkout is head's only then."""
    now = git(root, 'rev-parse', 'HEAD')
    dirty = git(root, '-c', 'core.fsmonitor=false', 'status', '--porcelain', '--untracked-files=no')
    why = [head != now and f'it is for {head[:12]} but the checkout is at {now[:12]}',
           dirty and f'tracked files changed since HEAD: {", ".join(ln.split(None, 1)[1] for ln in dirty.splitlines()[:5])}',
           *(f'{p} is not the one it was built from' for p, d in (files or {}).items() if digest(Path(root) / p) != d)]
    return [w for w in why if w]


def stage_tree(tree, src, dest='.'):
    """Copy the contents of the local dir src to tree-relative dest (its parent made at the
    tree's creation), refusing a symlink in src -- it would point back into the dev PC on the
    rig --, the RESERVED names at the top of src, and, through the guard, an attempt whose
    launch was claimed. Every refusal comes before anything is transferred."""
    src = Path(src)
    dest = _write_path(dest, 'a staging path')
    if not src.is_dir() or src.is_symlink():
        fail(f'{src} is not a directory to stage')
    links = [p for p in src.rglob('*') if p.is_symlink()]
    if links:
        fail(f'{src} holds symlinks, which would not resolve on the rig: {", ".join(map(str, links[:5]))}')
    _refuse_reserved(p.name for p in src.iterdir())
    if rsync(tree, '-aq', f'{local_operand(tree.root, src)}/', f'{tree.spec(dest)}/', staging=True) != 0:
        fail(f'could not stage {src} into {tree.spec(dest)}')


def _refuse_reserved(names):
    hit = sorted(set(names) & set(RESERVED))
    if hit:
        fail(f'the staged contents name {", ".join(hit)}, which the transport owns in the tree; nothing was staged')


def stage_files(tree, files, dest='.'):
    """Copy files (paths relative to the checkout root, kept as such) to tree-relative dest,
    refusing paths that leave the root and the RESERVED names at the top of the transfer."""
    dest = _write_path(dest, 'a staging path')
    files = [check_relative(f, 'a staged path') for f in files]
    _refuse_reserved(f.split('/')[0] for f in files)
    if rsync(tree, '-aqR', *files, f'{tree.spec(dest)}/', staging=True) != 0:
        fail(f'could not stage {len(files)} file(s) into {tree.spec(dest)}')


def verify_remote(tree, digests, base='.'):
    """The paths of digests ({path relative to tree-relative base: sha256}) whose bytes on the
    rig are missing or differ, read under the tree's guard."""
    base = _write_path(base, 'a verify dir')
    manifest = ''.join(f'{d}  {check_relative(p, "a staged path")}\n' for p, d in digests.items())
    where = tree.dir if base == '.' else f'{tree.dir}/{base}'
    cmd = (f'{{ {tree_guard(tree)}; }} || exit {TREE_REPLACED}; '
           f'cd {shlex.quote(where)} && exec sha256sum --check --quiet --strict -')
    r = subprocess.run(ssh_argv(tree.host, cmd), input=manifest, capture_output=True, text=True)
    if r.returncode == TREE_REPLACED:
        fail(f"{tree.spec()} is not this session's tree any more")
    if r.returncode not in (0, 1):
        fail(f'could not verify the staged files on {tree.host} (exit {r.returncode}): {r.stderr.strip()}')
    bad = sorted({ln.rsplit(': FAILED', 1)[0] for ln in r.stdout.splitlines() if ': FAILED' in ln})
    if r.returncode and not bad:
        fail(f'sha256sum on {tree.host} failed without naming a file: {r.stderr.strip()}')
    return bad


# --- copy-back -----------------------------------------------------------------------------------
def _artifact(member):
    """The member's canonical spelling: its tree-relative path, ending in / when it names a
    directory tree."""
    return check_relative(member.rstrip('/'), 'an artifact path') + ('/' if member.endswith('/') else '')


def _remove(p):
    if p.is_dir() and not p.is_symlink():
        shutil.rmtree(p)
    else:
        p.unlink(missing_ok=True)


def _local(dest, rel, create=False):
    """dest/rel, refusing a parent under dest that is a link or not a directory: a write through
    it would land outside dest. `create` makes the missing parents."""
    path = Path(dest)
    for part in rel.split('/')[:-1]:
        path = path / part
        if path.is_symlink() or path.exists() and not path.is_dir():
            fail(f'{path} is not a directory inside {dest}, so {rel} cannot be written there')
        if create and not path.exists():
            path.mkdir()
    return path / rel.split('/')[-1]


def _entry_path(name):
    """The parts of a fetched entry's name, None when it could name anything outside its root."""
    parts = name.split('/')
    if not name or any(p in ('', '.', '..') or '\\' in p or ':' in p or '\0' in p for p in parts):
        return None
    return parts


def read_fetch(stream, members, into):
    """Unpack a fetch stream of members (a tree ends in /) into the empty local dir into and
    return each member's outcome, or None when the stream is malformed or contradicts itself:
    an entry not asked for, of the wrong type (a file member must arrive as a file, a tree as a
    directory) or twice, a status entry that is not one JSON object of strings, an `ok` member
    that never arrived, or an `absent` one some of whose entries did. Nothing is published from
    here: into is only staging."""
    wanted = {m.rstrip('/'): m.endswith('/') for m in members}
    received, seen, sent, status = set(), set(), set(), None
    try:
        with tarfile.open(fileobj=stream, mode='r|') as tar:
            for ti in tar:
                if ti.name in seen:
                    return None
                seen.add(ti.name)
                if ti.name == FETCH_RESULT:
                    if not ti.isfile():
                        return None
                    status = json.loads(tar.extractfile(ti).read())
                    if not isinstance(status, dict) or not all(isinstance(v, str) for v in status.values()):
                        return None
                    continue
                if _entry_path(ti.name) is None or not (ti.isfile() or ti.isdir()):
                    return None
                if ti.name in wanted:
                    if wanted[ti.name] != ti.isdir():
                        return None
                    received.add(ti.name)
                    sent.add(ti.name)
                else:
                    owner = next((m for m, is_tree in wanted.items() if is_tree and ti.name.startswith(m + '/')), None)
                    if owner is None:
                        return None
                    sent.add(owner)
                path = _local(into, ti.name, create=True)
                if ti.isdir():
                    path.mkdir(exist_ok=True)
                else:
                    with open(path, 'xb') as f:
                        shutil.copyfileobj(tar.extractfile(ti), f)
    except (tarfile.TarError, ValueError, OSError, RemoteError):
        return None
    if status is None:
        return None
    if any(status.get(m) == 'ok' and m.rstrip('/') not in received or
           status.get(m) == 'absent' and m.rstrip('/') in sent for m in members):
        return None
    return {m: status.get(m, 'transport') for m in members}


# A fetched member's outcome, beside `error: <why>` for a failed read on the rig and
# `vanished: <why>` for a tree something left while it was sent: ok; absent (the member itself
# is not in the tree); unsafe (reached through a link, or a link itself); not-a-file (a file
# member that is not a regular file); truncated (shorter than its size when sent); replaced
# (the tree is not this session's); transport (the session or its stream failed).
OUTCOMES = ('ok', 'absent', 'unsafe', 'not-a-file', 'truncated', 'replaced', 'transport')


class CopyBack(tuple):
    """copy_back's (copied, incomplete), with .outcomes: {member: outcome} of every member."""

    def __new__(cls, copied, incomplete, outcomes):
        self = super().__new__(cls, (copied, incomplete))
        self.outcomes = outcomes
        return self


def _fetch(tree, members, into):
    """Stream members from the rig into the empty local dir into, under the tree's guard:
    {member: OUTCOMES or `error: ...`}. A malformed or inconsistent stream is a transport failure
    of every member."""
    cmd = (f'{{ {tree_guard(tree)}; }} || exit {TREE_REPLACED}; '
           f'exec {shlex.join(["python3", "-c", RIG_PY, "fetch", tree.dir, tree.token, *members])}')
    with tempfile.TemporaryFile() as err:
        p = subprocess.Popen(ssh_argv(tree.host, cmd), stdin=subprocess.DEVNULL, stdout=subprocess.PIPE, stderr=err)
        try:
            status = read_fetch(p.stdout, members, into)
        finally:
            p.stdout.close()
            rc = p.wait()
    if rc == TREE_REPLACED:
        return {m: 'replaced' for m in members}
    if rc or status is None:
        return {m: 'transport' for m in members}
    return status


def copy_back(tree, dest, groups=(), optional=()):
    """Fetch tree artifacts into dest at the same relative paths, in one guarded ssh session:
    each group all or none, each optional member when present. A member is a file, or a
    directory tree named with a trailing /; a fetched tree replaces the local one whole and is
    never merged with it (merging, as of create-only archives, is the adapter's).

    Containment: every path is the adapter's, checked lexically, never one read from a fetched
    file. On the rig each component is opened relative to its already-open parent without
    following a link, so a member reached through a symlinked file or directory is `unsafe`
    and its group incomplete; inside a tree, links and special files are skipped. A component
    swapped for a link before it is opened refuses the member; one swapped after is read
    through the descriptor already open, so nothing outside the tree is read either way.
    Locally a parent under dest that is a link or not a directory refuses the copy-back.

    A group lands only whole: half of a report pair would publish last run's table beside this
    run's data, so a member in two groups, or in a group and optional, is refused. A stale
    local copy of a group that did not all arrive, or of an optional member that did not
    arrive, is removed. Nothing lands before the whole stream is validated.
    Returns a CopyBack: (copied, the dest-relative members written; incomplete, the groups that
    did not all arrive), its .outcomes telling an absent optional member from a failed fetch.
    A tree is named with its trailing / throughout."""
    dest = Path(dest)
    groups = [[_artifact(m) for m in g] for g in groups]
    optional = [_artifact(m) for m in optional]
    grouped = [m for g in groups for m in dict.fromkeys(g)]
    shared = sorted({m for m in grouped if grouped.count(m) > 1} | (set(grouped) & set(optional)))
    if shared:
        fail(f'the artifact(s) {", ".join(shared)} belong to two groups, or a group and optional: '
             f'a group lands all or none')
    members = list(dict.fromkeys([*grouped, *optional]))
    for m in members:
        _local(dest, m.rstrip('/'))
        inside = [t for t in members if t.endswith('/') and t != m and m.startswith(t)]
        if inside:
            fail(f'the artifact {m} is inside the tree {inside[0]}, which already fetches it')
    dest.mkdir(parents=True, exist_ok=True)
    stage = dest / f'.hil-remote-fetch.{os.getpid()}'
    _remove(stage)
    stage.mkdir()
    copied, incomplete = [], []
    try:
        status = _fetch(tree, members, stage) if members else {}

        def place(m):
            if m in copied:
                return
            rel = m.rstrip('/')
            target = _local(dest, rel, create=True)
            _remove(target)
            os.replace(stage / rel, target)
            copied.append(m)
        for group in groups:
            if all(status.get(m) == 'ok' for m in group):
                for m in group:
                    place(m)
            else:
                for m in group:
                    _remove(_local(dest, m.rstrip('/')))
                incomplete.append(list(group))
        for m in optional:
            if m in copied:
                continue
            _remove(_local(dest, m.rstrip('/')))
            if status.get(m) == 'ok':
                place(m)
    finally:
        _remove(stage)
    return CopyBack(copied, incomplete, status)


# --- attempts ------------------------------------------------------------------------------------
# Runs on the rig as `python3 -c RIG_PY <op> ...`.
# create: the create-only attempt dir and its token under an exclusive lease.
# launch: under an exclusive lease, the create-only launch.claim (an attempt launches once),
#   the driver lock, then a supervisor forked into its own session, stdio on files in the
#   attempt. The supervisor alone publishes launch.json, then done.json when the command ends;
#   it holds driver.lock for its life and passes that fd to the command. Its acknowledgement to
#   the launcher is a courtesy: a launcher or ssh session already gone changes nothing.
# status: the driver lock probed first, then the records, since done.json is written before
#   the lock goes.
# fetch: a tar stream of the asked members on stdout, each path component opened without
#   following a link, then a FETCH_RESULT entry with each member's outcome.
_RIG_BODY = r'''
import errno, fcntl, io, json, os, signal, stat, subprocess, sys, tarfile, time


def refuse(why):
    print(why, file=sys.stderr)
    sys.exit(REFUSED)


def proc_stat(pid):
    """(state, start time) of pid, None when it is gone."""
    try:
        with open(f'/proc/{int(pid)}/stat') as f:
            fields = f.read().rsplit(')', 1)[1].split()
        return fields[0], fields[19]
    except (OSError, IndexError, ValueError, TypeError):
        return None


def lease_flock(fd, mode, secs, poll):
    """flock(mode) on fd, retried every poll s for secs; False when it stayed held."""
    deadline = time.monotonic() + secs
    while True:
        try:
            fcntl.flock(fd, mode | fcntl.LOCK_NB)
            return True
        except BlockingIOError:
            if time.monotonic() > deadline:
                return False
            time.sleep(poll)


def token_of(d):
    try:
        with open(f'{d}/{TOKEN_FILE}') as f:
            return f.read().strip()
    except OSError:
        return None


def create(base, attempt, token, *dirs):
    home = os.environ.get('HOME', '')
    if base.startswith('~/'):
        base = os.path.join(home, base[2:])
    base = os.path.abspath(base)
    if base in ('/', os.path.abspath(home or '/')):
        refuse(f'refusing the attempt base {base}')
    os.makedirs(base, exist_ok=True)
    lease = os.open(f'{base}/{attempt}.lease', os.O_WRONLY | os.O_CREAT | os.O_APPEND, 0o644)
    try:
        fcntl.flock(lease, fcntl.LOCK_EX | fcntl.LOCK_NB)
    except BlockingIOError:
        refuse(f'another session holds {base}/{attempt}.lease -- not creating attempt {attempt}')
    d = f'{base}/{attempt}'
    try:
        os.mkdir(d)
    except FileExistsError:
        refuse(f'attempt {attempt} exists on the rig ({d}): an attempt id is used once')
    os.mkdir(f'{d}/{RECORDS}')
    for p in dirs:
        os.makedirs(f'{d}/{p}', exist_ok=True)
    with open(f'{d}/{TOKEN_FILE}', 'x') as f:
        f.write(token + '\n')
    print(f'\nHIL_REMOTE_DIR={d}', flush=True)


def publish(records, name, data):
    tmp = f'{records}/.{name}.{os.getpid()}.tmp'
    with open(tmp, 'w') as f:
        f.write(json.dumps(data) + '\n')
        f.flush()
        os.fsync(f.fileno())
    os.replace(tmp, f'{records}/{name}')


def supervise(d, records, token, cwd, env, argv, held, w):
    os.setsid()
    signal.signal(signal.SIGHUP, signal.SIG_DFL)
    for fd, path, flags in ((0, os.devnull, os.O_RDONLY), (1, f'{records}/stdout.log', os.O_WRONLY | os.O_CREAT | os.O_APPEND),
                            (2, f'{records}/stderr.log', os.O_WRONLY | os.O_CREAT | os.O_APPEND)):
        opened = os.open(path, flags, 0o644)
        if opened != fd:
            os.dup2(opened, fd)
            os.close(opened)
    rig_path = RIG_PATH.replace('$HOME', os.environ.get('HOME', ''))
    run_env = {**os.environ, 'PATH': f'{rig_path}:{os.environ.get("PATH", "")}', **env}
    record = {'token': token, 'argv': list(argv), 'cwd': cwd, 'env': sorted(env), 'launchedAt': time.time(),
              'pid': os.getpid(), 'start': proc_stat(os.getpid())[1], 'sid': os.getsid(0)}
    try:
        p = subprocess.Popen(argv, cwd=d if cwd == '.' else f'{d}/{cwd}', env=run_env,
                             stdin=subprocess.DEVNULL, pass_fds=(held,))
        record['childPid'] = p.pid
    except OSError as e:
        p, record['error'] = None, str(e)
    publish(records, 'launch.json', record)
    try:
        os.write(w, json.dumps(record).encode())
        os.close(w)
    except OSError:
        pass
    publish(records, 'done.json', {'exit': 127, 'error': record['error'], 'endedAt': time.time()} if p is None
            else {'exit': p.wait(), 'endedAt': time.time()})


def launch(d, lease_path, token, cwd, env, *argv):
    try:
        lease = os.open(lease_path, os.O_WRONLY | os.O_APPEND)
    except OSError as e:
        refuse(f'the attempt lease {lease_path}: {e}')
    if not lease_flock(lease, fcntl.LOCK_EX, 60, 0.1):
        refuse(f'{lease_path} stayed held for 60 s -- not launching')
    if token_of(d) != token:
        refuse(f"{d} is not this attempt's tree any more -- not launching")
    records = f'{d}/{RECORDS}'
    try:
        os.close(os.open(f'{records}/launch.claim', os.O_WRONLY | os.O_CREAT | os.O_EXCL, 0o644))
    except FileExistsError:
        refuse(f'attempt {d} was launched before: wait for or collect it, it is never launched twice')
    held = os.open(f'{records}/driver.lock', os.O_WRONLY | os.O_CREAT, 0o644)
    fcntl.flock(held, fcntl.LOCK_EX)
    # ignored across the fork until the supervisor's setsid takes it out of this session
    signal.signal(signal.SIGHUP, signal.SIG_IGN)
    r, w = os.pipe()
    if os.fork() == 0:
        try:
            os.close(r)
            os.close(lease)
            supervise(d, records, token, cwd, env, argv, held, w)
        finally:
            os._exit(0)
    os.close(w)
    os.close(held)
    with os.fdopen(r) as f:
        ack = f.read()
    if not ack:
        print('the supervisor ended before it acknowledged the launch', file=sys.stderr)
        sys.exit(1)
    print('\nHIL_REMOTE_LAUNCH=' + ack, flush=True)


def status(d, lease_path, token):
    out = {'tokenOk': token_of(d) == token}
    if out['tokenOk']:
        probe(d, lease_path, out)
    print('\nHIL_REMOTE_STATUS=' + json.dumps(out), flush=True)


def probe(d, lease_path, out):
    # a shared hold on the lease for the whole probe: launch's claim-to-fork section holds it
    # exclusively, so the probe sees a launch before or after that section, never inside it
    try:
        lease = os.open(lease_path, os.O_RDONLY)
    except OSError:
        out['lease'] = 'missing'
        return
    if not lease_flock(lease, fcntl.LOCK_SH, 5, 0.05):
        out['leaseBusy'] = True
        return
    records = f'{d}/{RECORDS}'
    try:
        fd = os.open(f'{records}/driver.lock', os.O_RDONLY)
    except FileNotFoundError:
        out['driverLock'] = 'missing'
    else:
        try:
            fcntl.flock(fd, fcntl.LOCK_SH | fcntl.LOCK_NB)
            out['driverLock'] = 'free'
        except BlockingIOError:
            out['driverLock'] = 'held'
        finally:
            os.close(fd)
    out['claimed'] = os.path.exists(f'{records}/launch.claim')
    for name in ('launch', 'done'):
        try:
            with open(f'{records}/{name}.json') as f:
                out[name] = json.loads(f.read())
        except FileNotFoundError:
            out[name] = None
        except (OSError, ValueError):
            out[name] = 'unreadable'
    launch = out['launch']
    if isinstance(launch, dict) and 'pid' in launch:
        st = proc_stat(launch['pid'])
        out['driverAlive'] = bool(st) and st[0] != 'Z' and st[1] == launch.get('start')


def opendir(parent, name):
    return os.open(name, os.O_RDONLY | os.O_DIRECTORY | os.O_NOFOLLOW, dir_fd=parent)


def walk(root, parts):
    """The fd of the directory parts name under root, no component followed if a link."""
    fd = os.dup(root)
    for part in parts:
        try:
            nxt = opendir(fd, part)
        finally:
            os.close(fd)
        fd = nxt
    return fd


class Bounded:
    """The first `size` bytes of fd, read in the chunks tarfile asks for. The header already
    promised size bytes, so a short or failed read is padded with NULs to keep the stream whole
    and named in .problem; bytes appended after the size was taken are not sent."""

    def __init__(self, fd, size):
        self.fd, self.left, self.problem = fd, size, None

    def read(self, n):
        n = min(n, self.left)
        out = bytearray()
        while len(out) < n and self.problem is None:
            try:
                chunk = os.read(self.fd, n - len(out))
            except OSError as e:
                self.problem = f'error: {e}'
                break
            if not chunk:
                self.problem = 'truncated'
                break
            out += chunk
        out += bytes(n - len(out))
        self.left -= n
        return bytes(out)


def add_file(tar, parent, name, as_name):
    """Stream the regular file name in parent: ok, not-a-file, truncated or error: ..."""
    fd = os.open(name, os.O_RDONLY | os.O_NOFOLLOW | os.O_NONBLOCK, dir_fd=parent)
    try:
        st = os.fstat(fd)
        if not stat.S_ISREG(st.st_mode):
            return 'not-a-file'
        ti = tarfile.TarInfo(as_name)
        ti.size, ti.mtime, ti.mode = st.st_size, int(st.st_mtime), st.st_mode & 0o777
        src = Bounded(fd, st.st_size)
        tar.addfile(ti, src)
        return src.problem or 'ok'
    finally:
        os.close(fd)


def add_tree(tar, fd, as_name):
    """Stream the tree at fd: ok, or the first file's problem."""
    ti = tarfile.TarInfo(as_name)
    ti.type, ti.mode = tarfile.DIRTYPE, 0o755
    tar.addfile(ti)
    with os.scandir(fd) as it:
        entries = sorted(it, key=lambda e: e.name)
    worst = 'ok'
    for e in entries:
        if e.is_dir(follow_symlinks=False):
            sub = opendir(fd, e.name)
            try:
                got = add_tree(tar, sub, f'{as_name}/{e.name}')
            finally:
                os.close(sub)
        elif e.is_file(follow_symlinks=False):
            got = add_file(tar, fd, e.name, f'{as_name}/{e.name}')
            got = 'ok' if got == 'not-a-file' else got   # replaced by a non-file since the scan: skipped, as one
        else:
            continue
        worst = got if worst == 'ok' else worst
    return worst


def fetch(d, token, *members):
    if token_of(d) != token:
        refuse(f"{d} is not this session's tree any more")
    root = os.open(d, os.O_RDONLY | os.O_DIRECTORY)
    outcome = {}
    tar = tarfile.open(fileobj=sys.stdout.buffer, mode='w|', format=tarfile.PAX_FORMAT)
    for m in members:
        is_tree, parts = m.endswith('/'), m.rstrip('/').split('/')
        # absent only when the member itself cannot be found: the walk to it, or a file member's
        # own open; anything gone from a tree whose root is already open vanished mid-fetch
        try:
            fd = walk(root, parts if is_tree else parts[:-1])
        except FileNotFoundError:
            outcome[m] = 'absent'
            continue
        except OSError as e:
            outcome[m] = 'unsafe' if e.errno in (errno.ELOOP, errno.ENOTDIR) else f'error: {e}'
            continue
        try:
            outcome[m] = add_tree(tar, fd, m.rstrip('/')) if is_tree else add_file(tar, fd, parts[-1], m)
        except FileNotFoundError as e:
            outcome[m] = f'vanished: {e}' if is_tree else 'absent'
        except OSError as e:
            outcome[m] = 'unsafe' if e.errno in (errno.ELOOP, errno.ENOTDIR) else f'error: {e}'
        finally:
            os.close(fd)
    data = json.dumps(outcome).encode()
    ti = tarfile.TarInfo(FETCH_RESULT)
    ti.size = len(data)
    tar.addfile(ti, io.BytesIO(data))
    tar.close()
    sys.stdout.buffer.flush()


op, args = sys.argv[1], sys.argv[2:]
if op == 'create':
    create(*args)
elif op == 'launch':
    launch(*args[:4], json.loads(args[4]), *args[6:])
elif op == 'status':
    status(*args)
elif op == 'fetch':
    fetch(*args)
else:
    refuse(f'unknown op {op}')
'''
RIG_PY = (f'TOKEN_FILE, RECORDS, REFUSED, FETCH_RESULT, RIG_PATH = {TOKEN_FILE!r}, {RECORDS!r}, {RIG_REFUSED}, '
          f'{FETCH_RESULT!r}, {RIG_PATH!r}\n' + _RIG_BODY)


def _rig(host, op, *args, timeout=RIG_SECS):
    """Run the rig helper's op over ssh: (exit status, stdout, stderr); exit 255 with the reason
    when ssh did not return within timeout seconds."""
    try:
        r = subprocess.run(ssh_argv(host, shlex.join(['python3', '-c', RIG_PY, op, *args])),
                           stdin=subprocess.DEVNULL, capture_output=True, text=True, encoding='utf-8',
                           errors='replace', timeout=timeout)
    except subprocess.TimeoutExpired:
        return 255, '', f'ssh to {host} did not return within {timeout:g} s'
    return r.returncode, r.stdout, r.stderr.strip()


def _marked(out, key):
    """The value of the last `key=` line: a rig shell may greet on stdout first."""
    vals = [ln.partition('=')[2] for ln in out.splitlines() if ln.startswith(f'{key}=')]
    return vals[-1] if vals else None


def attempt_record(runs, run_id):
    check_run_id(run_id)
    return Path(runs) / f'{run_id}.attempt.json'


def _attempt_tree(root, data):
    d = data['dir']
    return Tree(data['host'], d, f'{d}.lease', data['token'], Path(root))


def create_attempt(root, runs, run_id, host, base, dirs=()):
    """Create the attempt <base>/<run_id> on host, with dirs (tree-relative) in it, and return
    its Tree. The local record <runs>/<run_id>.attempt.json is created first and only once, and
    the rig refuses an existing attempt dir, so a run id names one attempt; a later run never
    touches an earlier attempt's dir."""
    record = attempt_record(runs, run_id)
    check_remote_dir(base, 'the attempt base')
    dirs = [_write_path(p, 'an attempt dir') for p in dirs]
    require_rsync()
    record.parent.mkdir(parents=True, exist_ok=True)
    data = {'runId': run_id, 'host': host, 'base': base, 'token': secrets.token_hex(16), 'createdAt': time.time()}
    try:
        with open(record, 'x', encoding='utf-8') as f:
            f.write(json.dumps(data) + '\n')
    except FileExistsError:
        fail(f'run id {run_id} was used before ({record.parent.name}/{record.name}); pick a new one')
    rc, out, err = _rig(host, 'create', base, run_id, data['token'], *dirs)
    d = _marked(out, 'HIL_REMOTE_DIR')
    if rc or not d:
        fail(f'creating attempt {run_id} on {host} failed (exit {rc}): {err}')
    if not d.startswith('/') or not _plain_remote_dir(d):
        fail(f'the rig named attempt {run_id} {d!r}, not a plain absolute path')
    write_json(record, {**data, 'dir': d})
    return _attempt_tree(root, {**data, 'dir': d})


def load_attempt(root, runs, run_id):
    """The Tree of an attempt created from this checkout, from its local record."""
    record = attempt_record(runs, run_id)
    try:
        data = json.loads(record.read_text(encoding='utf-8'))
    except FileNotFoundError:
        fail(f'no attempt {run_id} was created from {Path(runs).parent}')
    except (OSError, ValueError) as e:
        fail(f'the record of attempt {run_id} is unreadable: {e}')
    if not isinstance(data, dict) or not data.get('dir'):
        fail(f'attempt {run_id} was never created on its rig (its record names no remote dir)')
    return _attempt_tree(root, data)


def launch(tree, argv, env=None, cwd='.'):
    """Launch argv detached in the attempt (cwd tree-relative), env added and RIG_PATH ahead of
    PATH, and return its launch record. It runs in its own session on the rig, stdio on
    <attempt>/.attempt/std{out,err}.log, so neither this process nor the ssh session ending
    signals it, and its supervisor publishes the launch and completion records itself. Refused
    once the attempt's launch was claimed; when the acknowledgement is lost the attempt may
    still have started, which attempt_status tells."""
    argv, env, cwd = _check_argv(argv), _env_args(env), _write_path(cwd, 'a run dir')
    rc, out, err = _rig(tree.host, 'launch', tree.dir, tree.lock, tree.token, cwd, json.dumps(env), '--', *argv)
    record = _marked(out, 'HIL_REMOTE_LAUNCH')
    if rc == 0 and record:
        return json.loads(record)
    if rc == RIG_REFUSED:
        fail(err)
    fail(f'the launch of {tree.spec()} was not acknowledged (exit {rc}){": " + err if err else ""}; it may have '
         f'started: wait for or collect it, never launch it again')


def attempt_state(probe):
    """The state the rig's evidence supports: gone (not this session's tree), not-launched, done
    (a completion record), running (the driver lock held), dead (lock free, no completion
    record, the recorded driver gone) or inconclusive (the evidence is incomplete or
    contradicts itself, or a launch held the lease through the probe). Nothing but a completion
    record is done. Probed under the lease, a claimed launch with no
    launch record yet is running while the driver lock is held -- the launcher takes it before
    the fork and the supervisor and command inherit it -- and dead once nothing holds it."""
    if not probe.get('tokenOk'):
        return 'gone'
    if probe.get('leaseBusy') or probe.get('lease') == 'missing':
        return 'inconclusive'   # a launch (or create) held the lease through the probe's whole wait
    launch, done = probe.get('launch'), probe.get('done')
    if isinstance(done, dict):
        return 'done'
    if launch is None and done is None:
        if not probe.get('claimed'):
            return 'not-launched'
        return 'running' if probe.get('driverLock') == 'held' else 'dead'
    if not isinstance(launch, dict) or 'pid' not in launch or done is not None:
        return 'inconclusive'
    if probe.get('driverLock') == 'held':
        return 'running'
    if probe.get('driverLock') == 'free' and probe.get('driverAlive') is False:
        return 'dead'
    return 'inconclusive'


def attempt_status(tree, timeout=RIG_SECS):
    """The rig's evidence on the attempt, with its `state` (attempt_state, or unreachable when
    the rig did not answer within timeout seconds)."""
    rc, out, err = _rig(tree.host, 'status', tree.dir, tree.lock, tree.token, timeout=timeout)
    raw = _marked(out, 'HIL_REMOTE_STATUS')
    where = {'host': tree.host, 'dir': tree.dir}
    if raw is None:
        return {'state': 'unreachable', **where, 'detail': f'exit {rc}: {err}'}
    probe = json.loads(raw)
    return {'state': attempt_state(probe), **where, **probe}


def wait_attempt(tree, timeout, *, poll=POLL_SECS, dest=None, groups=(), optional=(), accept=None):
    """Poll the attempt for up to timeout seconds, each poll a new ssh session: (0, status) once
    done, collected into dest when given (collect, without probing the done attempt again);
    (DEAD, ...) dead or never launched; (RUNNING, ...) still running at the timeout;
    (INCONCLUSIVE, ...) evidence incomplete or the rig unreachable at the timeout. Each poll
    gets the time left, at least STATUS_MIN_SECS, so polling ends by then. Waiting never
    launches, signals or cancels the attempt."""
    deadline = time.monotonic() + timeout
    while True:
        st = attempt_status(tree, min(RIG_SECS, max(STATUS_MIN_SECS, deadline - time.monotonic())))
        if st['state'] == 'gone':
            fail(f"{tree.spec()} is not this attempt's tree any more (removed or replaced)")
        if st['state'] == 'done':
            if dest is not None:
                st['collection'] = _collect(tree, dest, groups, optional, accept, st)
            return 0, st
        if st['state'] in ('dead', 'not-launched'):
            return DEAD, st
        left = deadline - time.monotonic()
        if left <= 0:
            return (RUNNING if st['state'] == 'running' else INCONCLUSIVE), st
        time.sleep(min(poll, left))


COLLECTION = 'collection.json'


def _collection(dest):
    """dest's collection record, None when there is none; a corrupt one refuses."""
    path = Path(dest) / COLLECTION
    try:
        held = json.loads(path.read_text(encoding='utf-8'))
    except FileNotFoundError:
        return None
    except (OSError, ValueError) as e:
        fail(f'{path} is unreadable ({e}); collect into a new dest')
    if not isinstance(held, dict):
        fail(f'{path} is not a collection record; collect into a new dest')
    return held


@contextlib.contextmanager
def _collector(dest):
    """Hold <dest>.collect.lock beside dest, refusing while another collector holds it: the
    cache check and the publication must not interleave with another collector's."""
    dest.parent.mkdir(parents=True, exist_ok=True)
    path = dest.with_name(f'.{dest.name}.collect.lock')
    fd = os.open(path, os.O_RDWR | os.O_CREAT, 0o644)
    try:
        _lock_ex(fd, block=False)
    except BlockingIOError:
        os.close(fd)
        fail(f'another collector holds {path}: one collector per destination at a time')
    try:
        yield
    finally:
        try:
            _unlock(fd)
        finally:
            os.close(fd)


def _aside(dest):
    """Where a publication keeps dest's previous collection until its replacement is in place."""
    return dest.with_name(f'.{dest.name}.old')


def _settle(dest):
    """Finish a publication a collector was interrupted in: with dest gone its previous collection
    goes back, else dest is the replacement and the one set aside goes."""
    old = _aside(dest)
    if (old.exists() or old.is_symlink()) and not (dest.exists() or dest.is_symlink()):
        old.rename(dest)
    _drop(dest)


DROP_TOKEN_BYTES = 8   # debris names end in this many random bytes, hex


def _drop(dest):
    """Remove the collection dest has replaced, renamed first to a name _settle never restores, so
    a removal that fails part-way leaves debris rather than a collection; debris that resists
    waits for the next _settle."""
    old = _aside(dest)
    if old.exists() or old.is_symlink():
        with contextlib.suppress(OSError):
            old.rename(dest.with_name(f'.{dest.name}.drop-{secrets.token_hex(DROP_TOKEN_BYTES)}'))
    for debris in dest.parent.glob(f'.{glob.escape(dest.name)}.drop-' + '[0-9a-f]' * (2 * DROP_TOKEN_BYTES)):
        with contextlib.suppress(OSError):
            _remove(debris)


def _publish(tmp, dest):
    """Rename tmp to dest without ever removing dest first: Windows cannot rename onto an existing
    directory, and a failed or interrupted publication must leave the previous collection."""
    old = _aside(dest)
    aside = dest.exists() or dest.is_symlink()
    if aside:
        dest.rename(old)
    try:
        tmp.rename(dest)
    except BaseException:
        if aside and not (dest.exists() or dest.is_symlink()):   # interrupted after the rename landed: keep it
            old.rename(dest)
        raise
    if aside:
        _drop(dest)


def _uncovered(held, groups, optional):
    """What a cached complete collection did not fetch of these requirements, by type: a group
    member needs that exact entry in the inventory (a tree with its /, so a file never answers
    for a tree), an optional member to have been asked for as the same type."""
    copied = set(held.get('copied') or ())
    asked = set((held.get('requested') or {}).get('optional') or ())
    return [*(m for g in groups for m in g if m not in copied), *(o for o in optional if o not in asked)]


def _gone(held, dest):
    """The members a cached collection copied that dest no longer holds as that type: an
    adoption moves a report and its records out, for one."""
    def there(m):
        p = dest / m.rstrip('/')
        return not p.is_symlink() and (p.is_dir() if m.endswith('/') else p.is_file())
    return [m for m in held.get('copied') or () if not there(m)]


def _unaccepted(accept, path):
    """accept(path)'s missing evidence, checked to be a list of strings."""
    missing = accept(path)
    if not isinstance(missing, (list, tuple)) or not all(isinstance(m, str) for m in missing):
        fail(f'a collection predicate returns a list of strings, not {missing!r}')
    return list(missing)


def collect(tree, dest, groups=(), optional=(), accept=None):
    """Fetch the attempt's artifacts and its own records into dest, whatever its state, and
    return the collection record written beside them (dest/collection.json): `complete` only
    when the attempt was done before the fetch began, every group and the completion record
    arrived, every optional member arrived or is absent (not lost to a failed fetch) and
    `accept` finds nothing missing, else `incomplete`; the record keeps each member's outcome.
    The record names the attempt (host, dir, token) and what was asked for.

    `accept(path)` is the caller's completeness predicate over a collected tree (a requirement
    the member lists cannot state, such as artifacts that depend on how far the run got): the
    evidence missing from it as a list of strings, empty when acceptable. It runs on the
    fetched tree before publication, its answer kept as the record's `unaccepted`, and on a
    cached complete collection, which it may find short: that one is collected again in the
    same call and replaced only by a complete one. A refresh that is not complete (a failed
    fetch, or still short) refuses with RemoteError and leaves the cached files and record as
    they were: they stay complete by transport but are never returned as accepted.

    A collection complete by the caller's predicate is final: collecting it again returns it
    unfetched when it covers what is asked and dest still holds all it copied, and refuses
    when it does not; partial evidence never replaces it. A dest holding another attempt's
    collection, or files without a record, refuses. Collectors of one dest are serialised by a
    lock beside it, across the cache check, the predicate, the fetch and the publication."""
    return _collect(tree, dest, groups, optional, accept, None)


def _collect(tree, dest, groups, optional, accept, st):
    """collect, `st` the attempt status a caller already saw `done`, or None to probe it."""
    dest = Path(dest)
    groups = [[_artifact(m) for m in g] for g in groups]
    optional = [_artifact(m) for m in optional]
    if COLLECTION in [m.rstrip('/') for g in groups for m in g] + [o.rstrip('/') for o in optional]:
        fail(f'{COLLECTION} is the collection record itself, not an artifact path')
    identity = {'host': tree.host, 'dir': tree.dir, 'token': tree.token}
    with _collector(dest):
        _settle(dest)
        held = _collection(dest)
        if held is None and dest.exists() and any(dest.iterdir()):
            fail(f'{dest} holds files but no {COLLECTION}; collect into a new dest')
        refreshing = None   # what the predicate finds missing from a cached complete collection
        if held is not None:
            if held.get('identity') != identity:
                fail(f'{dest} holds the collection of another attempt ({held.get("remote")}); collect into a new dest')
            if held.get('state') == 'complete':
                gone = _gone(held, dest)
                if gone:
                    fail(f'{dest} holds a complete collection whose {", ".join(gone)} are gone (moved out, by an '
                         f'adoption, or removed); collect into a new dest')
                short = _uncovered(held, groups, optional)
                if short:
                    fail(f'{dest} holds a complete collection that did not fetch {", ".join(short)}; '
                         f'collect into a new dest')
                refreshing = [] if accept is None else _unaccepted(accept, dest)
                if not refreshing:
                    return held
        if st is None:
            st = attempt_status(tree)
            if st['state'] == 'gone':
                fail(f"{tree.spec()} is not this attempt's tree any more (removed or replaced)")
            if st['state'] == 'unreachable':
                fail(f'cannot reach {tree.host} to collect {tree.dir}: {st["detail"]}')
        tmp = dest.with_name(f'.{dest.name}.{os.getpid()}.tmp')
        _remove(tmp)
        tmp.mkdir()
        try:
            grouped = {m for g in groups for m in g}
            records = [r for r in RECORD_FILES if r not in grouped]
            got = copy_back(tree, tmp, groups, (*records, *optional))
            copied, incomplete = got
            unaccepted = [] if accept is None else _unaccepted(accept, tmp)
            complete = (st['state'] == 'done' and not incomplete and f'{RECORDS}/done.json' in copied
                        and all(got.outcomes.get(o) in ('ok', 'absent') for o in (*RECORD_FILES, *optional))
                        and not unaccepted)
            record = {'state': 'complete' if complete else 'incomplete', 'remoteState': st['state'],
                      'remote': tree.spec(), 'identity': identity,
                      'requested': {'groups': groups, 'optional': optional}, 'copied': copied,
                      'missing': incomplete, 'unaccepted': unaccepted, 'outcomes': got.outcomes,
                      'collectedAt': time.time()}
            if refreshing and not complete:
                lost = [f'{m} {o}' for m, o in got.outcomes.items() if o not in ('ok', 'absent')]
                fail(f'{dest} holds a complete collection the predicate finds short '
                     f'({", ".join(refreshing)}); its refresh is not complete either '
                     f'({"; ".join([*lost, *map(str, incomplete), *unaccepted]) or st["state"]}), '
                     f'so it is kept as it was')
            write_json(tmp / COLLECTION, record)
            _publish(tmp, dest)
            return record
        finally:
            _remove(tmp)


# --- local run records ---------------------------------------------------------------------------
def run_paths(runs, run_id):
    check_run_id(run_id)
    runs = Path(runs)
    return runs / f'{run_id}.started.json', runs / f'{run_id}.done.json'


def write_json(path, data):
    """Atomic: a reader sees the whole record or none."""
    tmp = path.with_name(f'.{path.name}.{os.getpid()}.tmp')
    tmp.write_text(json.dumps(data) + '\n')
    tmp.replace(path)


def publish_locked(path, data):
    """Publish a record under an exclusive flock taken before it appears, refusing an existing
    one; returns the fd that holds the lock, which the kernel drops when this process dies."""
    tmp = path.with_name(f'.{path.name}.{os.getpid()}.tmp')
    try:
        f = open(tmp, 'x')
    except FileExistsError:
        if tmp.stat().st_nlink > 1:   # a killed run's hidden name under a reused pid: writing it rewrites that record
            raise
        tmp.unlink()
        f = open(tmp, 'x')
    with f:
        f.write(json.dumps(data) + '\n')
    fd = os.open(tmp, os.O_RDONLY)
    try:
        _lock_ex(fd)
        os.link(tmp, path)
    except BaseException:
        os.close(fd)
        tmp.unlink(missing_ok=True)
        raise
    if fcntl:   # Windows cannot remove a name of a file still open: run_recorded does, after the close
        tmp.unlink(missing_ok=True)
    return fd


# msvcrt locks are mandatory and per byte: lock one far past the record, so readers still read it
WIN_LOCK_OFFSET = 0x7FFFFFFE


def win_lock(fd, block=True):
    """The Windows stand-in for flock(LOCK_EX), held until win_unlock() or the handle closes;
    BlockingIOError when not `block` and another handle holds it."""
    pos = os.lseek(fd, 0, os.SEEK_CUR)
    os.lseek(fd, WIN_LOCK_OFFSET, os.SEEK_SET)
    try:
        while True:
            try:
                msvcrt.locking(fd, msvcrt.LK_NBLCK, 1)
                return
            except OSError as e:
                if e.errno not in (errno.EACCES, errno.EDEADLOCK):
                    raise
                if not block:
                    raise BlockingIOError
                time.sleep(0.05)   # LK_LOCK gives up after about 10 s
    finally:
        os.lseek(fd, pos, os.SEEK_SET)


def win_unlock(fd):
    """Release win_lock() now: Windows drops a closed handle's lock only "after a delay"."""
    pos = os.lseek(fd, 0, os.SEEK_CUR)
    os.lseek(fd, WIN_LOCK_OFFSET, os.SEEK_SET)
    try:
        msvcrt.locking(fd, msvcrt.LK_UNLCK, 1)
    finally:
        os.lseek(fd, pos, os.SEEK_SET)


def _lock_ex(fd, block=True):
    """flock(LOCK_EX), or win_lock on Windows; BlockingIOError when not `block` and it is held."""
    if fcntl:
        fcntl.flock(fd, fcntl.LOCK_EX | (0 if block else fcntl.LOCK_NB))
    else:
        win_lock(fd, block)


def _unlock(fd):
    """Before closing a _lock_ex descriptor: Windows needs win_unlock, POSIX's close releases it."""
    if not fcntl:
        win_unlock(fd)


# a waiter's probe holds the lock for a moment; a run holds it for its whole life
PROBE_TRIES, PROBE_SECS = 10, 0.02


def win_probe(fd):
    """flock(LOCK_SH | LOCK_NB) for run_alive(): msvcrt has no shared lock, so two waiters' probes
    contend; BlockingIOError only when the lock stays held across the retries."""
    for _ in range(PROBE_TRIES):
        try:
            win_lock(fd, block=False)
        except BlockingIOError:
            time.sleep(PROBE_SECS)
            continue
        win_unlock(fd)
        return
    raise BlockingIOError


def pop_option(argv, name):
    """(the option's value or None, argv without it): the rest goes to the remote harness."""
    for i, a in enumerate(argv):
        if a == name:
            value, rest = (argv[i + 1] if i + 1 < len(argv) else ''), argv[:i] + argv[i + 2:]
        elif a.startswith(f'{name}='):
            value, rest = a.partition('=')[2], argv[:i] + argv[i + 1:]
        else:
            continue
        if not value:
            fail(f'{name} needs a value')
        return value, rest
    return None, argv


def run_recorded(runs, run_id, body, record=None):
    """body() -> (exit status, reports) between a started record (`record`'s fields added),
    locked while it lives, and a done record written whatever ends it short of a kill: what
    wait_local reads, since nothing else outlives a background run. Returns the exit status."""
    started, done = run_paths(runs, run_id)
    started.parent.mkdir(exist_ok=True)
    if not fcntl:
        sweep_stale_tmps(started.parent)
    try:
        lock = publish_locked(started, {'runId': run_id, 'pid': os.getpid(), 'startedAt': time.time(), **(record or {})})
    except FileExistsError:
        fail(f'run id {run_id} was used before ({started.parent.name}/{started.name}); pick a new one')
    rc, reports = 1, []
    try:
        rc, reports = body()
        return rc
    except SystemExit as e:
        rc = e.code if isinstance(e.code, int) else int(e.code is not None)
        raise
    finally:
        try:
            write_json(done, {'runId': run_id, 'exit': rc, 'reports': reports})
        finally:   # no done record: the lock still goes, so a waiter reads the run dead, never running
            try:
                _unlock(lock)
            finally:
                os.close(lock)
            if not fcntl:
                with contextlib.suppress(OSError):   # a waiter's probe holding the file open makes Windows refuse; the next sweep takes it
                    started.with_name(f'.{started.name}.{os.getpid()}.tmp').unlink(missing_ok=True)


def sweep_stale_tmps(runs):
    """Windows: remove the hidden second name of each started record a killed run left behind
    (run_recorded removes its own after the close, which a kill skips). A live run holds its
    file open, which Windows refuses to unlink, and a launch not yet linked has one name."""
    for tmp in Path(runs).glob('.*.started.json.*.tmp'):
        try:
            if tmp.stat().st_nlink > 1:
                tmp.unlink()
        except OSError:
            pass


def run_alive(started):
    """Its run still holds the started record's lock."""
    fd = os.open(started, os.O_RDONLY)
    try:
        if fcntl:
            fcntl.flock(fd, fcntl.LOCK_SH | fcntl.LOCK_NB)
        else:
            win_probe(fd)
        return False
    except BlockingIOError:
        return True
    finally:
        os.close(fd)


def wait_secs(text):
    """An argparse type: one wait must end inside the Bash tool's 10-min cap."""
    secs = float(text)
    if not 0 <= secs <= WAIT_SECS:
        raise argparse.ArgumentTypeError(f'0 to {WAIT_SECS} s: one wait must end inside the Bash tool\'s 10-min cap')
    return secs


def wait_local(runs, run_id, timeout, *, poll=POLL_SECS, start_grace=START_SECS):
    """Wait up to timeout seconds for a run_recorded wrapper: (0, done status), (RUNNING, ...)
    still alive at the timeout, (DEAD, ...) ended without a done record. It watches the local
    wrapper only: a wrapper's death proves nothing about a remote run it started."""
    started, done = run_paths(runs, run_id)
    begun = time.monotonic()
    deadline = begun + timeout
    while True:
        if done.is_file():
            return 0, {'state': 'done', **json.loads(done.read_text())}
        try:
            rec = json.loads(started.read_text())
        except FileNotFoundError:
            grace = min(begun + start_grace, deadline) - time.monotonic()
            if grace > 0:
                time.sleep(min(poll, grace))
                continue
            fail(f'no run {run_id} was started from {Path(runs).parent}')
        if not run_alive(started):
            if done.is_file():
                continue    # it wrote the done record between the two checks
            return DEAD, {'state': 'dead', 'runId': run_id, 'pid': rec['pid'],
                          'detail': 'the run ended without a done record (killed, or its session died): '
                                    'no local report is from it'}
        left = deadline - time.monotonic()
        if left <= 0:
            return RUNNING, {'state': 'running', 'runId': run_id, 'pid': rec['pid'],
                             'elapsedSecs': round(time.time() - rec['startedAt'])}
        time.sleep(min(poll, left))
