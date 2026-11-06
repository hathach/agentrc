#!/usr/bin/env python3
"""Run the repository's pre-commit hooks on the given paths and report what they did.

  hooks.py [--deadline-seconds N] PATH...

Run from the checkout's top level. Prints the tree status and a snapshot of
PATH plus every path the status names, runs `pre-commit run --files PATH...`
when .pre-commit-config.yaml exists (once more if it failed after a hook
modified files), then the status and snapshot again. All runs, the HEAD reruns
below included, get N seconds (default 540) from the start, and removing HEAD's
tree after them CLEANUP (20) more, inside the 600 a relay's Bash call allows;
past N the pre-commit process tree is killed: on the first pass the result is an
error, and on a later pass the failure `(pre-commit)`. A HEAD rerun the deadline
or an error cuts short leaves its hook and those after it in notRerun. Status
lines are `git status --porcelain -z` records, each untracked file its own;
snapshot lines are `<644|755> <blob> <path>` or `absent - <path>`.

stdout ends with one JSON line {ran, passed, modifiedBy, failed, baselineFailed,
notRerun, before, after, snapshotBefore, snapshotAfter, seal} (seal:
facts.sealed): passed is the exit status of the first pass (the run and its
retry), modifiedBy the ids of the hooks whose files pre-commit itself found
modified in any run. Without a config: ran false, passed true, and the lists empty.

What each hook did is pre-commit's own account, never read off its stdout: every
run goes through hook_runner.py under pre-commit's own interpreter, named by the
#! line of pre-commit's entry script, or on Windows of the archive pip's
pre-commit.exe launcher carries, as an absolute path to a python, when that
entry only calls pre-commit's main() as pip writes it; it records the
hooks pre-commit selected once the config and manifests are resolved and, per
hook, whether it ran, failed or modified files, and its whole output. A run
through any other pre-commit, such as a wrapper (it then runs as is), or whose records are
missing, partial, inconsistent or from a pre-commit version hook_runner.py does
not support, names no hook: when it failed, failed holds `(pre-commit)` saying
why, modifiedBy nothing from it, and it gets no retry.

failed and baselineFailed explain a failure: failed, {hook, error} for each hook
the first pass's last run failed, error the first line with a word in its
output; and baselineFailed, the ids among those that, rerun alone in a detached
worktree of HEAD on the same PATHs, fail there with the output they printed in
that run (comparable: its lines in any order, the tree's path read as the
checkout's, and each number standing alone, such as a duration, line number,
count, pid or address, each time, worker number and temporary name masked), so
they are no evidence against the change (#378). The change's side is the
original ordered run, never a later lone run, so a failure caused by what an
earlier hook left is judged as it happened. A hook that ever modified files is
never rerun there, nor one SKIP naming it would not select alone (it cannot
name an id with a comma, or the padding the config gives it) among the hooks the
run selected and those pre-commit resolves at any stage, aliases a remote
manifest gives included (the commit's SKIP skips a commit-msg hook too, and a
fail_fast stop at one leaves the rest unchecked), nor any when hook_runner.py's
record of every stage's hooks is missing or malformed; notRerun is {hook, why} for each
failed hook not rerun there; failed's error and notRerun's why hold no terminal
escapes or control characters, as commits.py's detail. Each rerun gets a clean
HEAD tree, checked out and reset without the repository's git hooks; removing it
removes its own registration, no other, and stderr names what stays. The tree
has only what HEAD tracks: a hook that needs the checkout's ignored files
(fetched dependencies) fails there differently and stays counted. There
pre-commit must select that hook alone, by its id, before running it, and the
hook must fail without modifying files. A change that leaves a hook's output the
same once masked is still excused: a hook that prints only a count of its
violations, or a new violation replacing one of HEAD's that differs from it only
in numbers; CI runs the hook on the pushed head. When every failure is excused
and a hook may stop the run early (fail_fast on the config or a hook as
pre-commit resolved it, a remote manifest's included), the hooks run once
more on the change, with its retry, and SKIP naming the excused ones, as the
commit will, so a fail_fast stop cannot hide a hook the change breaks: what
fails there joins failed and is rerun on HEAD in turn, the hooks running again
while every new failure is excused, and a failed run that names no hook adds the
failure `(pre-commit)`.
pre-commit runs with core.abbrev pinned at the length HEAD abbreviates to
when the first run starts (#377), possibly a digit over git's default, which
the hooks' own git commands inherit. git still lengthens an abbreviation a
hook's new object makes ambiguous, so rarely a hook that changed nothing is in
modifiedBy.
Exit 0 with that line; exit 2 with {"error": ...} when the facts cannot be
collected or a run's records on the change say a hook failed while pre-commit
exited 0.
"""

import contextlib
import functools
import io
import json
import ntpath
import os
import posixpath
import re
import shutil
import signal
import subprocess
import sys
import tempfile
import threading
import time
import zipfile
from pathlib import Path

sys.path.insert(0, str(Path(__file__).parent))
from commits import bounded as copyable, with_skip  # noqa: E402
from facts import Unusable, checkout_top, git, report, status  # noqa: E402
sys.path.insert(0, str(Path(__file__).resolve().parents[3] / 'lib'))
import windows  # noqa: E402

CONFIG = '.pre-commit-config.yaml'
DEADLINE = 540
RUNNER = Path(__file__).resolve().with_name('hook_runner.py')
ERROR_CHARS = 200
# Seconds past the deadline the HEAD worktree's removal may take.
CLEANUP = 20
# Any CPython or PyPy build's name: python3, python3.12, python3.13t, python3-dbg, pypy3, python.exe.
SHEBANG_PYTHON = re.compile(r'(?:python|pypy)[\w.-]*')
# The #! line pip's Windows launcher (distlib's ScriptMaker) puts between its stub and the archive it runs.
LAUNCHER_SHEBANG = re.compile(rb'#!([^\r\n]+)\r?\n(?=PK\x03\x04)')
# What follows the #! line of the console script pip (distlib) and installer write for pre-commit's main(), quotes as
# distlib's: the only entry hook_runner.py stands in for, as it does nothing else; a wrapper may set up more.
ENTRY_SCRIPT = ("import re\nimport sys\nfrom pre_commit.main import main\nif __name__ == '__main__':\n"
                "    sys.argv[0] = re.sub(r'(-script\\.pyw|\\.exe)?$', '', sys.argv[0])\n    sys.exit(main())\n")
# pip's own template in place of distlib's (wheel.py PipScriptMaker), from pip 25.2 and from 26.0; the same in the
# __main__.py of its Windows launcher.
PIP_ENTRY_SCRIPTS = (
    "import sys\nfrom pre_commit.main import main\nif __name__ == '__main__':\n"
    "    if sys.argv[0].endswith('.exe'):\n        sys.argv[0] = sys.argv[0][:-4]\n    sys.exit(main())\n",
    "import sys\nfrom pre_commit.main import main\nif __name__ == '__main__':\n"
    "    sys.argv[0] = sys.argv[0].removesuffix('.exe')\n    sys.exit(main())\n")
# Each exact form of them an installer writes: distlib's quotes or installer's, a coding line or not, LF or CRLF.
ENTRY_SCRIPTS = {(coding + body).replace('\n', eol).encode()
                 for body in (ENTRY_SCRIPT, ENTRY_SCRIPT.replace("'", '"'), *PIP_ENTRY_SCRIPTS)
                 for coding in ('', '# -*- coding: utf-8 -*-\n') for eol in ('\n', '\r\n')}
# Bytes past which an entry script is no standard one.
ENTRY_MAX = 1024
# The failure a run that failed naming no hook adds: not a hook id, so never excused or skipped.
UNOWNED = '(pre-commit)'
# What points git at a repository or its index; set by a caller, it would send HEAD's tree's resets and reruns to the
# caller's checkout. The object store stays: HEAD's tree shares it, and resolves HEAD only there.
OTHER_REPO = ('GIT_DIR', 'GIT_WORK_TREE', 'GIT_COMMON_DIR', 'GIT_INDEX_FILE')
# A C-quoted string from its opening quote to its closing one, each escape one quote.c unquote_c_style accepts.
C_QUOTED = re.compile(r'"((?:[^"\\]|\\(?:[abfnrtv"\\]|[0-3][0-7]{2}))*)"', re.DOTALL)
C_ESCAPES = {b'a': b'\a', b'b': b'\b', b'f': b'\f', b'n': b'\n', b'r': b'\r', b't': b'\t', b'v': b'\v'}
# What differs between two runs of the same failure: a number standing alone (a duration with its units, a line
# number, a count, a pid, an address), an ISO 8601 time or a pytest-xdist worker's number. Any other digit inside a word
# (usb2.c, E501) names a file, rule or symbol, so it stays.
UNIT = r'(?:[hm]|min|sec|[mµun]?s)'
VARYING = re.compile(r'\b\d{4}-\d\d-\d\dT\d\d:\d\d(?::\d\d(?:[.,]\d+)?)?Z?'
                     rf'|\b(?:0x[0-9a-fA-F]+|\d+(?:\.\d+)*(?:{UNIT}(?:\d+(?:\.\d+)?{UNIT})*)?)\b|(?<=\[gw)\d+(?=\])')
# A name tempfile or mktemp made, as any component of an absolute path outside the checkout, since a source such as
# src/tmpcounter1.c reads alike; the path runs to a space or quote, so a name past a space in it stays. A gcc name is
# masked only directly in the temp dir (temp_gcc()), as a source such as cc2640r2.c or ccdriver.c reads alike.
ABSOLUTE = re.compile(r'''(?<![\w.~/\\<>-])(?:[A-Za-z]:)?[/\\][^\s'"<>]*''')
TEMP_NAME = re.compile(r'(?<=[/\\])tmp(?:[a-z0-9_]{8}|\.[A-Za-z0-9]{10})\b')


def status_paths(records):
    """Every path the records name, a rename's or copy's source included."""
    paths, i = [], 0
    while i < len(records):
        paths.append(records[i][3:])
        if records[i][0] in 'RC':
            i += 1
            paths.append(records[i])
        i += 1
    return paths


def index_mode(f):
    """The mode git add gives f with core.fileMode false, Windows' default (true there is unsupported): a tracked
    file keeps its index mode, a new one is 644. A symlink (120000) checked out as a file stays 644, as os.access
    reports it on POSIX, so it never matches ls-tree's 000 and its commit is refused."""
    for entry in git('--literal-pathspecs', 'ls-files', '-s', '-z', '--', f).split('\0'):
        meta, _, path = entry.partition('\t')
        if path == f and meta.startswith('100'):
            return meta[3:6]
    return '644'


def snapshot(paths, records):
    lines = []
    for f in sorted(set(paths) | set(status_paths(records))):
        if not os.path.lexists(f):
            lines.append(f'absent - {f}')
        elif not os.path.isdir(f):
            mode = index_mode(f) if os.name == 'nt' else '755' if os.access(f, os.X_OK) else '644'
            lines.append(f"{mode} {git('hash-object', '--', f).strip()} {f}")
    return lines


class Unattributed(Exception):
    """Why a run's records attribute nothing to any hook."""


def attributed(text, code):
    """(the roster record, the hook records) hook_runner.py left in text for a run that exited code, checked whole:
    one start of a supported version, the roster, a hook record per roster entry in order up to the end or a fail_fast
    stop at a failed one, and the end with code. Unattributed when they are not; Unusable when they say a hook
    failed and pre-commit exited 0. The roster's everywhere is the hooks record between start and roster, every hook
    at any stage, or None when it is missing or malformed."""
    if text is None:
        raise Unattributed('the pre-commit on PATH is no entry hook_runner.py stands in for, so it ran as is')
    try:
        records = [json.loads(line) for line in text.splitlines()]
        kinds = [r['record'] for r in records]
    except (ValueError, TypeError, KeyError):
        raise Unattributed('its records are malformed')
    if kinds[:1] != ['start']:
        raise Unattributed('it left no records')
    if not records[0].get('supported'):
        raise Unattributed(f"pre-commit {records[0].get('version')} is not a version hook_runner.py supports")
    everywhere = None
    if kinds[1:2] == ['hooks']:
        everywhere = records.pop(1).get('hooks')
        kinds.pop(1)
    if kinds[1:2] != ['roster'] or kinds[-1] != 'end' or set(kinds[2:-1]) - {'hook'}:
        raise Unattributed('its records stop short')
    roster, ran, end = records[1], records[2:-1], records[-1]
    try:
        ids = [h['id'] for h in roster['hooks']]
        if roster.get('refused'):
            raise Unattributed(f"it selected {', '.join(ids) or 'no hook'}, not that hook alone")
        failed = [r['id'] for r in ran if r['failed']]
        if failed and not code:
            raise Unusable(f'pre-commit exited 0 but its records say hook {failed[0]} failed')
        if ([r['index'] for r in ran] != list(range(len(ran))) or [r['id'] for r in ran] != ids[:len(ran)]
                or len(ran) > len(ids) or any(r['modified'] and not r['failed'] for r in ran)):
            raise Unattributed('its records contradict themselves')
        if len(ran) < len(ids) and not (ran and ran[-1]['failed'] and (
                roster['failFast'] or roster['hooks'][len(ran) - 1]['failFast'])):
            raise Unattributed('its records contradict themselves')
        if end['code'] != code or bool(failed) != bool(code):
            raise Unattributed(f"its records contradict pre-commit's exit status {code}")
    except (KeyError, TypeError, IndexError):
        raise Unattributed('its records are malformed')
    whole = isinstance(everywhere, list) and all(
        isinstance(h, dict) and isinstance(h.get('id'), str) and isinstance(h.get('alias'), str)
        and isinstance(h.get('stages'), list) for h in everywhere)
    return {**roster, 'everywhere': everywhere if whole else None}, ran


def first_error(record):
    """The first line with a word the hook printed, skipping rules such as unittest's `=====`, bounded for a relay to
    copy; what failed it when it printed none."""
    for line in record['output'].splitlines():
        text = copyable(line).strip()
        if re.search(r'[^\W_]', text):
            return text[:ERROR_CHARS]
    return 'files were modified by this hook' if record['modified'] else f"exit code: {record['retcode']}"


def results(text, code):
    """(ids of the hooks that modified files, {hook, index, error, output} of each failed hook, attributed()'s roster)
    of one run from its records; a run they cannot attribute modified nothing it can name and, when it failed, has one
    failure UNOWNED says why, with no roster."""
    try:
        roster, ran = attributed(text, code)
    except Unattributed as e:
        return [], [{'hook': UNOWNED, 'error': copyable(f'pre-commit exited {code} naming no hook: {e}')}] if code \
            else [], None
    return ([r['id'] for r in ran if r['modified']],
            [{'hook': r['id'], 'index': r['index'], 'error': first_error(r), 'output': r['output']}
             for r in ran if r['failed']], roster)


def unselected(failure, roster):
    """Why SKIP naming failure's hook would not skip that hook alone, as pre-commit resolved the config and the
    manifests, aliases included: among roster's hooks, those its run selected, and its everywhere, those of every
    stage, which the commit runs with the same SKIP; '' when it would."""
    names = {s.strip() for s in failure['hook'].split(',') if s.strip()}
    selected = [i for i, h in enumerate(roster['hooks']) if h['id'] in names or h['alias'] in names]
    if failure['index'] not in selected:
        return 'SKIP cannot name it'
    # The run's hooks are among every stage's: none named there is a record that contradicts the run.
    named = sum(h['id'] in names or h['alias'] in names for h in roster['everywhere'] or ())
    if not named:
        return "hook_runner.py's record of the hooks at every stage is missing or malformed"
    if len(selected) > 1 or named > 1:
        return 'SKIP would skip another hook by it'
    return ''


def may_stop(roster):
    """Whether a hook may stop roster's run early: fail_fast on the config, the run or a hook as pre-commit resolved
    it, a remote manifest's included."""
    return roster['failFast'] or any(h['failFast'] for h in roster['hooks'])


def kill_tree(proc):
    if os.name == 'nt':
        windows.kill_tree(proc, 10)
    else:
        try:
            os.killpg(proc.pid, signal.SIGKILL)
        except ProcessLookupError:
            pass


@functools.cache
def abbrev_length():
    """The length of HEAD's unique abbreviation in the checkout, measured at the first run, before any hook writes an
    object: git's default length, or more where HEAD's prefix is ambiguous."""
    return len(git('rev-parse', '--short', 'HEAD').strip())


def pinned_abbrev(env, length):
    """env with core.abbrev at length. pre-commit decides a hook modified files by comparing `git diff` text before
    and after it; git's automatic length follows the packed object count, so a hook that adds a pack mid-run can
    lengthen the unstaged diff's abbreviated `index` hashes at the count's boundary, and that alone failed it (#377).
    Any fixed length skips that count (object-name.c, find_unique_abbrev_r); the checkout's measured one gives the
    hooks' git commands about the abbreviations a commit's hooks get, where 40 would not. Below 40 git still lengthens
    a diffed blob's hash when a hook's new object shares its prefix (about 2 x files x new objects / 16^length)."""
    count = env.get('GIT_CONFIG_COUNT') or '0'
    if not count.isdigit():
        raise Unusable(f'GIT_CONFIG_COUNT is {count!r}, not a count')
    n = int(count)
    return {**env, 'GIT_CONFIG_COUNT': str(n + 1), f'GIT_CONFIG_KEY_{n}': 'core.abbrev',
            f'GIT_CONFIG_VALUE_{n}': str(length)}


def other_repo_env(names=OTHER_REPO):
    """os.environ without names, by default what points git at a repository, so a command run in HEAD's tree finds
    that tree, not the caller's checkout or index; a relative object store it names is made absolute against the
    checkout, as the tree's commands run elsewhere."""
    env = {k: v for k, v in os.environ.items() if k not in names}
    if env.get('GIT_OBJECT_DIRECTORY'):
        env['GIT_OBJECT_DIRECTORY'] = os.path.abspath(env['GIT_OBJECT_DIRECTORY'])
    if env.get('GIT_ALTERNATE_OBJECT_DIRECTORIES'):
        env['GIT_ALTERNATE_OBJECT_DIRECTORIES'] = absolute_alternates(env['GIT_ALTERNATE_OBJECT_DIRECTORIES'])
    return env


def absolute_alternates(value):
    """value, a GIT_ALTERNATE_OBJECT_DIRECTORIES, with each relative entry made absolute. As git parses it
    (object-file.c parse_alt_odb_entry), a C-quoted entry, which may hold the separator, runs to its closing quote
    and one character more, and is read unquoted; one quoted wrongly is a plain entry. A # comment is kept as given."""
    out, pos = [], 0
    while pos < len(value):
        quoted = C_QUOTED.match(value, pos)
        sep = value.find(os.pathsep, pos)
        end = quoted.end() if quoted else len(value) if sep < 0 else sep
        entry = value[pos:end]
        path = c_unquoted(quoted[1]) if quoted else entry
        if path and not entry.startswith('#') and not os.path.isabs(path):
            entry = c_quoted(os.path.abspath(path)) if quoted else os.path.abspath(path)
        out += [entry, value[end:end + 1]]
        pos = end + 1
    return ''.join(out)


def c_unquoted(body):
    """body, what a C-quoted string holds between its quotes, unquoted as git does."""
    return os.fsdecode(re.sub(rb'\\([0-3][0-7]{2}|.)', lambda m: bytes([int(m[1], 8)]) if len(m[1]) == 3
                              else C_ESCAPES.get(m[1], m[1]), os.fsencode(body), flags=re.DOTALL))


def c_quoted(path):
    """path C-quoted, each backslash, quote or control byte an octal escape."""
    return '"' + os.fsdecode(re.sub(rb'[\\"\x00-\x1f\x7f]', lambda m: b'\\%03o' % m[0][0], os.fsencode(path))) + '"'


class Late(Unusable):
    """A command the deadline cut short."""


def bounded(argv, deadline, start, what, cwd=None, env=None, name=None):
    """(exit code, stdout, stderr) of argv, its whole process tree killed once `deadline` seconds from `start` pass
    (Late, naming the command name, by default argv[0])."""
    group = {'creationflags': subprocess.CREATE_NEW_PROCESS_GROUP} if os.name == 'nt' else {'start_new_session': True}
    proc = subprocess.Popen(argv, stdout=subprocess.PIPE, stderr=subprocess.PIPE, encoding='utf-8',
                            errors='replace', cwd=cwd, env=env, **group)
    try:
        out, err = proc.communicate(timeout=max(deadline - (time.monotonic() - start), 0))
    except subprocess.TimeoutExpired:
        kill_tree(proc)
        late = f'{name or argv[0]} did not finish within {deadline} s ({what})'
        try:
            proc.communicate(timeout=5)
        except subprocess.TimeoutExpired:
            # A descendant in another session outlived the kill and still holds the output pipe.
            raise Late(f'{late}; a process it started survived the kill and may still be running')
        raise Late(f'{late}; its process group was killed, though a process that left it may survive')
    except BaseException:
        # Its own process group spares the hooks the caller's Ctrl-C or kill, so they go down with this run.
        kill_tree(proc)
        raise
    return proc.returncode, out, err


def pre_commit(paths, deadline, start, what, hook=None, cwd=None, skip=()):
    """(exit code, hook_runner.py's records) of one pre-commit run, skip added to the SKIP it inherits; the records
    None when pre-commit's interpreter is unknown and it ran as is. With hook, only that hook alone runs."""
    env = pinned_abbrev(other_repo_env() if cwd else os.environ, abbrev_length())
    if skip:
        env = with_skip(env, skip)
    argv = ['run', '--color', 'never', *([hook] if hook else []), '--files', *paths]
    found = pre_commit_python()
    if not found:
        # The pre-commit judged, by path: Windows' CreateProcess looks a bare name up in its own order (its own
        # directory first, .exe only), not shutil.which's.
        on_path = shutil.which('pre-commit')
        try:
            return bounded([os.path.abspath(on_path) if on_path else 'pre-commit', *argv], deadline, start, what,
                           cwd, env, 'pre-commit')[0], None
        except FileNotFoundError:
            raise Unusable(f'{CONFIG} exists but pre-commit is not installed')
    python, entry = found
    # What the entry script has as sys.path[0]: its directory, or the launcher itself, run as a zip archive.
    path0 = entry if entry.lower().endswith('.exe') else os.path.dirname(entry)
    fd, records = tempfile.mkstemp(prefix='hooks-records-')
    os.close(fd)
    try:
        code = bounded([*python, str(RUNNER), path0, records, *(['--only', hook] if hook else []), *argv], deadline,
                       start, what, cwd, env, 'pre-commit')[0]
        return code, Path(records).read_text(encoding='utf-8', errors='replace')
    except FileNotFoundError:
        raise Unusable(f"pre-commit's {python[0]} cannot run")
    finally:
        os.unlink(records)


def git_within(deadline, start, *argv, cwd=None):
    """git argv, held to the same deadline as the hooks: a checkout filter or hook of its own can hang it."""
    # `worktree add` checks HEAD out into GIT_INDEX_FILE when set: the caller's index, its staged changes lost.
    env = other_repo_env() if cwd else other_repo_env(('GIT_INDEX_FILE',))
    command = next(a for a in argv if not a.startswith('-') and '=' not in a)
    code, _, err = bounded(['git', *argv], deadline, start, f'git {command}', cwd, env)
    if code:
        # git's own error first: a long temp dir in argv would otherwise push it past what copyable() keeps.
        raise Unusable(f"git {command}: {err.strip()} (git {' '.join(argv)})")


def spellings(path):
    """The ways a hook may print path, longest first: as given and resolved, each native, with forward slashes,
    and on Windows as MSYS's /c/..."""
    found = set()
    for p in (path, os.path.realpath(path)):
        for q in (p, p.replace('\\', '/')):
            found.add(q)
            if re.match(r'[A-Za-z]:/', q):
                found.add(f'/{q[0].lower()}{q[2:]}')
    return sorted(found, key=len, reverse=True)


def as_checkout(text, root):
    """text with root, where the hook ran, read as one placeholder, so HEAD's tree and the checkout compare."""
    for s in spellings(root):
        text = re.sub(re.escape(s), '<checkout>', text, flags=re.IGNORECASE if os.name == 'nt' else 0)
    return text


@functools.cache
def temp_gcc():
    """A gcc temporary name directly in the temp dir this run and its hooks share, where an all-lowercase one is
    gcc's too; the dir as a whole path, not one ending a source path such as src/tmp. Group 1 is the dir."""
    dirs = '|'.join(re.escape(s) for s in spellings(tempfile.gettempdir()))
    return re.compile(rf'(?<![\w.~/\\-])((?:{dirs})[/\\])cc[A-Za-z0-9]{{6}}(?=\.)',
                      re.IGNORECASE if os.name == 'nt' else 0)


def comparable(text, root):
    """What of a hook's output its HEAD rerun must repeat: its lines, in any order (a parallel runner's), with root
    read as the checkout and what VARYING, TEMP_NAME and temp_gcc() name masked: each number standing alone (a
    duration, a line number, a count), a time, a pytest-xdist worker's number and a temporary file's name. A violation
    the change adds is a line more."""
    text = ABSOLUTE.sub(lambda path: TEMP_NAME.sub('#', path[0]), as_checkout(temp_gcc().sub(r'\1#', text), root))
    lines = (VARYING.sub('#', line).strip() for line in text.splitlines())
    return sorted(line for line in lines if line)


def standard_entry(body):
    """Whether body, all an entry runs after its #! line, is one of ENTRY_SCRIPTS, byte for byte."""
    return body in ENTRY_SCRIPTS


def launcher_entry(data):
    """(the #! line pip's pre-commit.exe launcher data carries, the __main__.py of the archive it runs), or None."""
    found = list(LAUNCHER_SHEBANG.finditer(data))
    if not found:
        return None
    try:
        with zipfile.ZipFile(io.BytesIO(data[found[-1].end():])) as archive:
            if archive.namelist() != ['__main__.py']:
                return None
            return found[-1][1], archive.read('__main__.py')
    except (zipfile.BadZipFile, OSError, ValueError):
        return None


@functools.cache
def pre_commit_python():
    """(the interpreter and argument the pre-commit on PATH starts, that entry's resolved path), or None when they are
    not known or the entry does more than call pre-commit's main() as pip's own does (standard_entry()): then
    hook_runner.py cannot stand in for it. A POSIX entry script's #! line must name a python by absolute path: not a
    wrapper script, env, or distlib's /bin/sh form for a long or spaced path. pip's pre-commit.exe on Windows runs the
    #! line it carries right before its appended archive, an absolute path to a python.exe, quoted when it holds a
    space."""
    found = shutil.which('pre-commit')
    if not found:
        return None
    entry = os.path.realpath(found)
    launcher = entry.lower().endswith('.exe')
    try:
        with open(entry, 'rb') as f:
            # Read whole: an entry script past ENTRY_MAX is no standard one, whatever its start.
            data = f.read() if launcher else f.read(ENTRY_MAX + 1)
    except OSError:
        return None
    if launcher:
        found = launcher_entry(data)
        if not found:
            return None
        line, body = found
        text = line.decode('utf-8', 'replace').strip()
        if text.startswith('"'):
            python, closed, rest = text[1:].partition('"')
            words = [python, *rest.split(None, 0)] if closed else []
        else:
            words = text.split(None, 1)
        # distlib's launcher pastes the rest into the command line ("<python>" <rest> "<exe>" <args>), which python.exe
        # splits on spaces and tabs; quoting rules are not modelled, so a quote leaves it to the launcher.
        if len(words) == 2:
            words = [] if '"' in words[1] else [words[0], *re.split(r'[ \t]+', words[1])]
        absolute, name = ntpath.isabs, ntpath.basename
    else:
        head, _, body = data.partition(b'\n') if len(data) <= ENTRY_MAX else (b'', b'', b'')
        # The kernel passes what follows the interpreter as one argument.
        words = head[2:].decode('utf-8', 'replace').strip().split(None, 1) if head.startswith(b'#!') else []
        # A #! path is POSIX's: absolute from '/', whatever os.path.isabs says on Windows (3.13+ wants a drive there).
        absolute, name = (lambda p: p.startswith('/')), posixpath.basename
    if not (words and absolute(words[0]) and SHEBANG_PYTHON.fullmatch(name(words[0])) and standard_entry(body)):
        return None
    return words, entry


def registers(admin, tree):
    """Whether admin, an administrative directory, registers tree as a worktree."""
    try:
        gitdir = (admin / 'gitdir').read_text(encoding='utf-8').strip()
    except (OSError, ValueError):  # unreadable or not UTF-8: another worktree's entry
        return False
    # worktree.useRelativePaths writes it relative to the admin dir (git's worktree.c), never the cwd.
    return os.path.normcase(os.path.realpath(admin / gitdir)) == os.path.normcase(
        os.path.realpath(os.path.join(tree, '.git')))


def registration(tree):
    """The administrative directory registering tree as a worktree, or None."""
    try:
        common = Path(git('rev-parse', '--git-common-dir').strip()).resolve()
    except Unusable:
        return None
    return next((admin for admin in (common / 'worktrees').glob('*') if registers(admin, tree)), None)


@contextlib.contextmanager
def signals_held():
    """Ctrl-C and SIGTERM delivered once the block ends, not inside it; a no-op off the main thread, where Python runs
    no handler."""
    if threading.current_thread() is not threading.main_thread():
        yield
        return
    caught = []
    held = {s: signal.signal(s, lambda n, _: caught.append(n)) for s in (signal.SIGINT, signal.SIGTERM)}
    try:
        yield
    finally:
        for s, handler in held.items():
            signal.signal(s, signal.SIG_DFL if handler is None else handler)
        if caught:
            signal.raise_signal(caught[0])


def forget(tmp, deadline, start):
    """Remove the HEAD worktree in tmp and its registration, one a kill left half made and locked included, within
    CLEANUP past the deadline, a signal held till done so it cannot cut that short; a failure here never hides the
    run's own result, and stderr names what stays."""
    tree = os.path.join(tmp, 'head')
    with signals_held():
        try:
            git_within(deadline + CLEANUP, start, 'worktree', 'remove', '--force', '--force', tree)
        except Unusable:
            pass
        admin = registration(tree)
        # What a failed remove left can take long to delete: past the budget it stays, and is named, a second at least.
        removal = threading.Thread(target=shutil.rmtree, args=(tmp, True), daemon=True)
        removal.start()
        removal.join(max(deadline + CLEANUP - (time.monotonic() - start), 1))
        # Read again: once its tree is gone, another session's prune and `worktree add` may reuse the entry's name.
        if admin and not os.path.lexists(tree) and registers(admin, tree):
            # What `git worktree prune` does to this entry alone: prune drops every unlocked entry whose tree is
            # missing, another session's moved or unmounted worktree included.
            shutil.rmtree(admin, ignore_errors=True)
        left = [str(p) for p in (tmp, admin) if p and os.path.lexists(p)]
        if left:
            print(f"hooks.py: HEAD's worktree could not be removed: {', '.join(left)}", file=sys.stderr)


def baseline(expected, paths, deadline, start):
    """(the ids among expected's ({hook: comparable() of its output in each run it failed on the change}) that fail on
    HEAD as they failed on the change, {hook: why} of those left unjudged): each rerun alone on the same paths in a
    detached worktree of HEAD, so what the host lacks (a compiler, a codepage, /dev/null) fails it there in the same
    words, without the change. HEAD's pre-commit must select that hook alone by its id before running it, and the hook
    fail there without modifying files; never pre-commit refusing an id HEAD's config does not have."""
    failing, judged = [], 0
    tmp = tempfile.mkdtemp(prefix='hooks-base-')
    try:
        tree = os.path.join(tmp, 'head')
        # tmp holds no hook: a post-checkout or reference-transaction hook would change or fail HEAD's tree.
        no_hooks = ('-c', f'core.hooksPath={tmp}')
        # Locked till forget: another session's prune could drop the entry once the tree goes, and its name be reused.
        git_within(deadline, start, *no_hooks, 'worktree', 'add', '--lock', '--detach', '--quiet', tree, 'HEAD')
        for hook, outputs in expected.items():
            if judged:
                # What an earlier rerun rewrote or left behind could fail the next one: each gets HEAD as is.
                git_within(deadline, start, *no_hooks, 'reset', '--quiet', '--hard', 'HEAD', cwd=tree)
                git_within(deadline, start, 'clean', '-qffdx', cwd=tree)
            if fails_alike(hook, outputs, *pre_commit(paths, deadline, start, f'HEAD {hook}', hook=hook, cwd=tree),
                           tree):
                failing.append(hook)
            judged += 1
    except Unusable as e:
        # A checkout filter, git or pre-commit failing in HEAD's tree is no verdict on the change: its failures stay.
        why = f'HEAD ran out of time: {e}' if isinstance(e, Late) else f'HEAD could not judge it: {e}'
        return failing, {hook: copyable(why) for hook in list(expected)[judged:]}
    finally:
        forget(tmp, deadline, start)
    return failing, {}


def fails_alike(hook, outputs, code, records, tree):
    """Whether HEAD's run of hook alone, which exited code leaving records, failed with each of outputs and modified
    nothing."""
    try:
        roster, ran = attributed(records, code)
    except Unattributed:
        return False
    return ([h['id'] for h in roster['hooks']] == [hook] and len(ran) == 1 and ran[0]['failed']
            and not ran[0]['modified'] and all(comparable(ran[0]['output'], tree) == each for each in outputs))


def passes(paths, deadline, start, what, skip=()):
    """(exit code, ids of the hooks that modified files, the last run's failures, its roster) of pre-commit on paths,
    run once more when it failed after a hook modified files."""
    ids = []
    for attempt in (1, 2):
        code, records = pre_commit(paths, deadline, start, f'{what} {attempt}', skip=skip)
        modified, failures, roster = results(records, code)
        ids += [h for h in modified if h not in ids]
        # A rerun repairs what a hook rewrote; the same failure again only spends another pass (#140).
        if not code or not modified:
            break
    return code, ids, failures, roster


def run_hooks(paths, deadline=DEADLINE, start=None):
    if not os.path.exists(CONFIG):
        return False, True, [], [], [], []
    start = time.monotonic() if start is None else start
    code, ids, failures, roster = passes(paths, deadline, start, 'run')
    found, excused, not_rerun = failures, [], {}
    while True:
        expected = {}
        for f in found:
            if f['hook'] == UNOWNED:
                continue
            # A hook that rewrote files is judged by what it wrote.
            why = 'it modified files' if f['hook'] in ids else unselected(f, roster)
            if why:
                not_rerun.setdefault(f['hook'], why)
            elif f['hook'] not in excused:
                expected.setdefault(f['hook'], []).append(comparable(f['output'], os.getcwd()))
        newly, late = baseline(expected, paths, deadline, start) if expected else ([], {})
        excused += newly
        for hook, why in late.items():
            not_rerun.setdefault(hook, why)
        if not newly or not {f['hook'] for f in found} <= set(excused) or not may_stop(roster):
            break
        # fail_fast may have stopped the run at an excused hook: every other hook must pass on the change, as the
        # commit will run them with the same SKIP, or fail on HEAD too.
        try:
            _, more, found, roster = passes(paths, deadline, start, 'without the excused', skip=excused)
        except Late as e:
            failures.append({'hook': UNOWNED, 'error': copyable(str(e))})
            break
        ids += [h for h in more if h not in ids]
        failures += found
    return True, not code, ids, failures, excused, [{'hook': h, 'why': w} for h, w in not_rerun.items()]


def collect(argv):
    deadline = DEADLINE
    if argv[:1] == ['--deadline-seconds']:
        if len(argv) < 2 or not argv[1].isdigit() or int(argv[1]) < 1:
            raise Unusable('usage: hooks.py [--deadline-seconds N] PATH...')
        deadline, argv = int(argv[1]), argv[2:]
    paths = argv
    if not paths:
        raise Unusable('usage: hooks.py [--deadline-seconds N] PATH...')
    start = time.monotonic()
    checkout_top()
    before = status()
    snapshot_before = snapshot(paths, before)
    ran, passed, ids, failures, base_failed, not_rerun = run_hooks(paths, deadline, start)
    after = status()
    failed = [{'hook': f['hook'], 'error': f['error']} for f in failures]
    return {'ran': ran, 'passed': passed, 'modifiedBy': ids, 'failed': failed, 'baselineFailed': base_failed,
            'notRerun': not_rerun, 'before': before, 'after': after, 'snapshotBefore': snapshot_before,
            'snapshotAfter': snapshot(paths, after)}


if __name__ == '__main__':
    # SIGTERM unwinds like Ctrl-C, through pre_commit's kill of the hook group.
    signal.signal(signal.SIGTERM, lambda *_: sys.exit(128 + signal.SIGTERM))
    sys.exit(report(collect, sys.argv[1:], seal=True))
