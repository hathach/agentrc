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
retry), modifiedBy the ids of the hooks pre-commit itself said modified files
in any run. Without a config: ran false, passed true, and the lists empty.

failed and baselineFailed explain a failure: failed, {hook, error} for each hook
the first pass's last run failed, error its first output line; and
baselineFailed, the ids among those that, rerun alone in a detached worktree of
HEAD on the same PATHs, fail there with the same output (comparable: its lines
in any order, the tree's path read as the checkout's, and each number standing
alone, such as a duration, line number, count, pid or address, each time,
worker number and temporary name masked), so they are no evidence against the
change (#378). A hook that ever modified files is never rerun there, nor one
whose id SKIP cannot name (a comma, or padding the config gives it), more than
one config entry uses or another hook in the config takes as its alias (SKIP
skips those hooks too, and a fail_fast stop at one leaves the rest unchecked),
nor any when the config cannot be read: with PyYAML, or without it by
pre-commit's own interpreter when the #! line of pre-commit's entry script names
a python by absolute path (not on Windows); notRerun is {hook, why} for each
failed hook not rerun there; failed's error and notRerun's why hold no terminal
escapes or control characters, as commits.py's detail. Each rerun gets a clean
HEAD tree, checked out and reset without the repository's git hooks; removing it
removes its own registration, no other, and stderr names what stays. The tree
has only what HEAD tracks: a hook that needs the checkout's ignored files
(fetched dependencies) fails there differently and stays counted. Run alone
there, all a hook prints after its block is its own; on the change its output
may end at any later result shaped as pre-commit's whose block, if one follows,
names a hook the config runs after it, as a result it printed may look alike, or
at the end, and it is excused only when HEAD's matches it at one of those. A
result inside its output at each of those whose block names an id the config
lacks is one it printed, no other hook's failure or rewrite; one naming a
configured id, or any when the config is unread, stays counted. A line only the
change prints keeps it counted, unless it follows all HEAD's output under such a
result, as a nested pre-commit run naming a later hook's id would print. A
change that leaves a hook's output the same once masked is still excused: a hook
that prints only a count of its violations, or a new
violation replacing one of HEAD's that differs from it only in numbers; CI runs
the hook on the pushed head. When every failure is excused and a hook may stop
the run early (fail_fast set on the whole config or a hook, or left to a remote
repository's manifest by a hook's entry), the hooks run once more on the change,
with its retry, and SKIP naming the excused ones, as the commit will, so a
fail_fast stop cannot hide a hook the change breaks: what fails there joins
failed and is rerun on HEAD in turn, the hooks running again while every new
failure is excused, and a failed run that names no hook adds the failure
`(pre-commit)`. An alias only a remote repository's own manifest declares is not
seen, so such a hook may go unrun on the change until CI.
pre-commit runs with core.abbrev pinned at the length HEAD abbreviates to
when the first run starts (#377), possibly a digit over git's default, which
the hooks' own git commands inherit. git still lengthens an abbreviation a
hook's new object makes ambiguous, so rarely a hook that changed nothing is in
modifiedBy.
Exit 0 with that line; exit 2 with {"error": ...} when the facts cannot be
collected or pre-commit's output on the change contradicts its exit status.
"""

import contextlib
import functools
import json
import os
import re
import shutil
import signal
import subprocess
import sys
import tempfile
import threading
import time
import unicodedata
from pathlib import Path

sys.path.insert(0, str(Path(__file__).parent))
from commits import bounded as copyable, with_skip  # noqa: E402
from facts import Unusable, checkout_top, git, report, status  # noqa: E402
sys.path.insert(0, str(Path(__file__).resolve().parents[3] / 'lib'))
import windows  # noqa: E402

try:
    import yaml
except ImportError:
    yaml = None
YAML_ERRORS = (yaml.YAMLError,) if yaml else ()

CONFIG = '.pre-commit-config.yaml'
DEADLINE = 540
HOOK_ID = '- hook id: '
MODIFIED = '- files were modified by this hook'
# pre-commit pads a hook's name with dots to its result, then prints the
# hook's `- ` block only when it failed or runs verbose (commands/run.py).
RESULT = re.compile(r'\.(Passed|Failed)$')
# Any hook's result line, `(no files to check)Skipped` included.
NEXT = re.compile(r'[.)](Passed|Failed|Skipped)$')
# The `- ` block pre-commit prints under each result when it prints one (commands/run.py _run_single_hook): the hook
# id, the duration when it ran verbose, its exit code when not 0, and the modified-files line.
BLOCKS = {'Skipped': re.compile(r'- hook id: .+'),
          'Passed': re.compile(r'- hook id: .+\n- duration: [\d.]+s'),
          'Failed': re.compile(r'- hook id: .+(?:\n- duration: [\d.]+s)?'
                               r'(?:\n- exit code: -?\d+(?:\n- files were modified by this hook)?'
                               r'|\n- files were modified by this hook)')}
ERROR_CHARS = 200
# Seconds past the deadline the HEAD worktree's removal may take.
CLEANUP = 20
# Run by pre-commit's own interpreter, which depends on PyYAML, when this one lacks it: the config as JSON.
LOAD_CONFIG = 'import json, sys, yaml; print(json.dumps(yaml.safe_load(open(sys.argv[1], encoding="utf-8"))))'
# Any CPython or PyPy build's name: python3, python3.12, python3.13t, python3-dbg, pypy3.
SHEBANG_PYTHON = re.compile(r'(?:python|pypy)[\w.-]*')
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


def width(line):
    """line's width as pre-commit pads its result lines (commands/run.py _len_cjk): a wide character counts two."""
    return sum(2 if unicodedata.east_asian_width(c) in 'FW' else 1 for c in line)


def own_runs(lines, cols):
    """The indexes from which the rest of lines can be pre-commit's own output, len(lines) included: a result line
    padded to cols, the block BLOCKS allows under it, then the end, the next such run, or, under a block not a
    Skipped one, a blank line and what the hook printed, which may be anything (commands/run.py _run_single_hook)."""
    n = len(lines)
    starts = {n}
    for j in range(n - 1, -1, -1):
        found = NEXT.search(lines[j])
        if not found or width(lines[j]) != cols:
            continue
        k = j + 1
        while k < n and lines[k].startswith('- '):
            k += 1
        status, block = found[1], '\n'.join(lines[j + 1:k])
        shaped = BLOCKS[status].fullmatch(block) if block else status != 'Failed'
        # pre-commit prints a hook's output stripped, so its first line is never blank.
        if shaped and (k in starts or block and status != 'Skipped' and not lines[k] and k + 1 < n
                       and lines[k + 1].strip()):
            starts.add(j)
    return starts


def ends(lines, k, starts):
    """Each (end, after) where what the hook whose `- ` block ends above lines[k] printed may end: each own run
    (starts) under the blank line pre-commit ends a hook's output with, under its last line, which may be
    pre-commit's next result or one the hook printed itself, as that cannot be told, then the end of lines. after is
    the id the first `- ` block from there names, None when only result lines follow. A run right under the block
    ends a hook that printed nothing, and lines in no shape pre-commit prints there run to the end."""
    n = len(lines)
    if k in starts:
        found = [k]
    elif lines[k]:
        found = [n]
    else:
        found = [*(m for m in sorted(starts) if k + 1 < m < n and not lines[m - 1] and lines[m - 2].strip()), n]
    return [(m, next((x[len(HOOK_ID):].strip() for x in lines[m:] if x.startswith(HOOK_ID)), None)) for m in found]


def first_error(lines, block):
    """The first line with a word the hook printed, skipping rules such as unittest's `=====`, bounded for a relay to
    copy; its last `- ` line when it printed none."""
    for line in lines:
        text = copyable(line).strip()
        if re.search(r'[^\W_]', text):
            return text[:ERROR_CHARS]
    return block[-1][2:] if block else 'no output'


def results(output, exit_code, order=None):
    """(ids of the hooks whose own `- ` block says they modified files, {hook, error, outputs} of each failed hook,
    outputs an (output, after) for each of its ends: its whole `- ` block and what it printed up to there).

    A block counts only directly under a result line; the hook's output follows
    the block after a blank line, so a marker it echoes is not read as one. Under
    a failed hook's block, a result naming an id order (the config's, None
    unread) lacks counts only at or past an end of its output where pre-commit's
    next result may be (follows()): pre-commit's own results name configured ids,
    so one only inside it at each is a result the hook printed itself."""
    lines = output.splitlines()
    runs = {}
    ids, failures = [], []
    first_end = 0  # of the last failed hook's output, where pre-commit's next result may be
    for i, line in enumerate(lines):
        if not (line.startswith(HOOK_ID) and i and RESULT.search(lines[i - 1])):
            continue
        hook = line[len(HOOK_ID):].strip()
        failed = lines[i - 1].endswith('Failed')
        block = []
        for tail in lines[i + 1:]:
            if not tail.startswith('- '):
                break
            block.append(tail)
        if not hook:
            raise Unusable(f'pre-commit printed an empty hook id under: {lines[i - 1]}')
        if i - 1 < first_end and order is not None and hook not in order:
            continue
        if failed and not exit_code:
            raise Unusable(f'pre-commit exited 0 but reported hook {hook} failed')
        if MODIFIED in block:
            if not failed:
                raise Unusable(f'pre-commit reported hook {hook} passed and modified files')
            ids.append(hook)
        if failed:
            cols = width(lines[i - 1])
            if cols not in runs:
                runs[cols] = own_runs(lines, cols)
            k = i + 1 + len(block)
            cuts = ends(lines, k, runs[cols])
            # No end accepted: the whole output, which then skips only results naming unconfigured ids.
            first_end = min((m for m, after in cuts if follows(hook, after, order)), default=len(lines))
            failures.append({'hook': hook, 'error': first_error(lines[k:cuts[0][0]], block),
                             'outputs': [('\n'.join([*block, *lines[k:m]]).rstrip(), after) for m, after in cuts]})
    return ids, failures


def follows(hook, after, order):
    """Whether pre-commit's next result after hook's output may be one whose block names after (None: no block
    follows): any when order, the config's ids in run order, is None, else one order runs after hook."""
    return after is None or order is None or hook in order and after in order[order.index(hook) + 1:]


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


def bounded(argv, deadline, start, what, cwd=None, env=None):
    """(exit code, stdout, stderr) of argv, its whole process tree killed once `deadline` seconds from `start` pass
    (Late)."""
    group = {'creationflags': subprocess.CREATE_NEW_PROCESS_GROUP} if os.name == 'nt' else {'start_new_session': True}
    proc = subprocess.Popen(argv, stdout=subprocess.PIPE, stderr=subprocess.PIPE, encoding='utf-8',
                            errors='replace', cwd=cwd, env=env, **group)
    try:
        out, err = proc.communicate(timeout=max(deadline - (time.monotonic() - start), 0))
    except subprocess.TimeoutExpired:
        kill_tree(proc)
        late = f'{argv[0]} did not finish within {deadline} s ({what})'
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
    """(exit code, stdout) of one pre-commit run, skip added to the SKIP it inherits."""
    env = pinned_abbrev(other_repo_env() if cwd else os.environ, abbrev_length())
    if skip:
        env = with_skip(env, skip)
    # Neither -v nor --show-diff-on-failure, and stdout alone: pre-commit prints nothing past its last hook, so all
    # a hook run alone prints after its block is its own (baseline).
    try:
        code, out, _ = bounded(['pre-commit', 'run', '--color', 'never', *([hook] if hook else []), '--files', *paths],
                               deadline, start, what, cwd, env)
    except FileNotFoundError:
        raise Unusable(f'{CONFIG} exists but pre-commit is not installed')
    return code, out


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


def pre_commit_python():
    """(the interpreter and argument the #! line of the pre-commit on PATH names, its entry script's directory), or
    None when that line names no python by absolute path: a Windows launcher, a wrapper script, env, or distlib's
    /bin/sh form for a long or spaced path."""
    found = shutil.which('pre-commit')
    if not found:
        return None
    script = os.path.realpath(found)
    try:
        with open(script, 'rb') as f:
            line = f.readline(512)
    except OSError:
        return None
    # The kernel passes what follows the interpreter as one argument.
    words = line[2:].decode('utf-8', 'replace').strip().split(None, 1) if line.startswith(b'#!') else []
    # A #! path is POSIX's: absolute from '/', whatever os.path.isabs says on Windows (3.13+ wants a drive there).
    if not (words and words[0].startswith('/') and SHEBANG_PYTHON.fullmatch(os.path.basename(words[0]))):
        return None
    return words, os.path.dirname(script)


def pre_commits_load(deadline, start):
    """(the config as pre-commit's own interpreter loads it, '') or (None, why it was not read)."""
    found = pre_commit_python()
    if not found:
        return None, "PyYAML is not installed, nor is pre-commit's interpreter known, so the config was not read"
    argv, where = found
    try:
        # Run where the entry script lies, so it imports as pre-commit does, nothing from the checkout.
        code, out, err = bounded([*argv, '-c', LOAD_CONFIG, os.path.abspath(CONFIG)], deadline, start,
                                 'reading the config', cwd=where)
    except OSError as e:
        return None, copyable(f"pre-commit's {argv[0]} cannot run ({type(e).__name__})")
    if code:
        last = copyable((err.strip().splitlines() or [f'exit {code}'])[-1]).strip()
        return None, copyable(f"pre-commit's {argv[0]} cannot read {CONFIG} ({last[:ERROR_CHARS]})")
    return json.loads(out), ''


def config_ids(deadline, start):
    """({each id SKIP cannot skip that hook alone by: why}, each entry's id stripped, as hooks' blocks are read, in the
    order pre-commit runs them, whether a hook may stop the run early, ''), or (None, None, None, why) when it cannot be
    read; a remote repository's manifest may set a hook's fail_fast unless the hook's entry sets it.
    SKIP's names would also skip another hook: an alias the config gives a hook under another id, and an id more than
    one entry uses; and it strips each name, so a padded id it cannot name (pre-commit's commands/run.py)."""
    try:
        if yaml:
            config = yaml.safe_load(Path(CONFIG).read_text(encoding='utf-8'))
        else:
            config, why = pre_commits_load(deadline, start)
            if why:
                return None, None, None, why
        entries = [h for repo in config['repos'] for h in repo['hooks']]
        ids = [h['id'] for h in entries]
        shared = {i: 'SKIP would skip another hook by it' for i in ids if ids.count(i) > 1}
        shared |= {h['alias']: 'SKIP would skip another hook by it' for h in entries
                   if h.get('alias') not in (None, '', h['id'])}
        shared |= {i.strip(): 'SKIP cannot name it' for i in ids if i != i.strip()}
        stops = bool(config.get('fail_fast')) or any(h.get('fail_fast', repo['repo'] not in ('local', 'meta'))
                                                     for repo in config['repos'] for h in repo['hooks'])
        return shared, [i.strip() for i in ids], stops, ''
    except (OSError, ValueError, KeyError, TypeError, AttributeError, *YAML_ERRORS) as e:
        return None, None, None, f'{CONFIG} cannot be read ({type(e).__name__})'


def unexcusable(hook, ids, shared, unread):
    """Why a failure of hook is never rerun on HEAD, or '' when it is. A hook that rewrote files is judged by what it
    wrote; the commit could not skip an id SKIP cannot name, and SKIP naming one it shares, or one whose aliases are
    unknown, would also skip another hook, unrun on the change or, past a fail_fast stop, unchecked; shared is
    config_ids'."""
    return ('it modified files' if hook in ids else 'SKIP cannot name it' if ',' in hook else unread
            or shared.get(hook, ''))


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
    """(the ids among expected's ({hook: for each of its Failed blocks on the change, its outputs at each end}) that
    fail on HEAD as they failed on the change, {hook: why} of those left unjudged): each rerun alone on
    the same paths in a detached worktree of HEAD, so what the host lacks (a compiler, a codepage, /dev/null) fails it
    there in the same words, without the change. Alone, all the hook prints after its first Failed block, to the end,
    is its own; every block on the change must end with that at one of its ends. Never pre-commit refusing an id
    HEAD's config does not have."""
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
            code, out = pre_commit(paths, deadline, start, f'HEAD {hook}', hook=hook, cwd=tree)
            whole = next((comparable(f['outputs'][-1][0], tree) for f in results(out, code)[1] if f['hook'] == hook),
                         None)
            if whole is not None and all(whole in each for each in outputs):
                failing.append(hook)
            judged += 1
    except Unusable as e:
        # A checkout filter, git or pre-commit failing in HEAD's tree is no verdict on the change: its failures stay.
        why = f'HEAD ran out of time: {e}' if isinstance(e, Late) else f'HEAD could not judge it: {e}'
        return failing, {hook: copyable(why) for hook in list(expected)[judged:]}
    finally:
        forget(tmp, deadline, start)
    return failing, {}


def compared(failure, order):
    """comparable() of the failed hook's output at each end pre-commit's next result may be: the end, or a result
    whose block, when one follows, names a hook order runs after it. One the hook printed may name any, so a change
    that adds a nested pre-commit run's result under HEAD's output stays counted unless it names such a hook."""
    return [comparable(out, os.getcwd()) for out, after in failure['outputs'] if follows(failure['hook'], after, order)]


def passes(paths, deadline, start, what, config, skip=()):
    """(exit code, ids of the hooks that modified files, the last run's failures) of pre-commit on paths, run once
    more when it failed after a hook modified files; config() is config_ids', read once a run fails."""
    ids = []
    for attempt in (1, 2):
        code, out = pre_commit(paths, deadline, start, f'{what} {attempt}', skip=skip)
        modified, failures = results(out, code, config()[1] if code else None)
        ids += [h for h in modified if h not in ids]
        # A rerun repairs what a hook rewrote; the same failure again only spends another pass (#140).
        if not code or not modified:
            break
    return code, ids, failures


def run_hooks(paths, deadline=DEADLINE, start=None):
    if not os.path.exists(CONFIG):
        return False, True, [], [], [], []
    start = time.monotonic() if start is None else start
    config = functools.cache(lambda: config_ids(deadline, start))
    code, ids, failures = passes(paths, deadline, start, 'run', config)
    shared, order, stops, unread = config() if failures else ({}, [], False, '')
    found, excused, not_rerun = failures, [], {}
    while True:
        expected = {}
        for f in found:
            why = unexcusable(f['hook'], ids, shared, unread)
            if why:
                not_rerun.setdefault(f['hook'], why)
            elif f['hook'] not in excused:
                expected.setdefault(f['hook'], []).append(compared(f, order))
        newly, late = baseline(expected, paths, deadline, start) if expected else ([], {})
        excused += newly
        for hook, why in late.items():
            not_rerun.setdefault(hook, why)
        if not newly or not {f['hook'] for f in found} <= set(excused) or not stops:
            break
        # fail_fast may have stopped the run at an excused hook: every other hook must pass on the change, as the
        # commit will run them with the same SKIP, or fail on HEAD too.
        try:
            again, more, found = passes(paths, deadline, start, 'without the excused', config, skip=excused)
        except Late as e:
            failures.append({'hook': UNOWNED, 'error': copyable(str(e))})
            break
        ids += [h for h in more if h not in ids]
        failures += found
        if again and not found:
            failures.append({'hook': UNOWNED, 'error': copyable(
                f"pre-commit exited {again} with {','.join(excused)} skipped, naming no hook")})
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
