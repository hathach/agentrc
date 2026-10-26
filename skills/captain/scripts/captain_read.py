#!/usr/bin/env python3
"""Read-only git and gh lookups for the captain, checked here and again by captain_guard.py with refusal().

    captain_read.py git -C <checkout> <status | worktree list | branch ... | log ... | reflog show ... | show ...
                                       | diff ... | rev-parse ... | merge-base ...>
    captain_read.py gh <pr|issue|run> <view|list|checks|diff> [<number>] -R <owner>/<repo> [<flag>...]
    captain_read.py list <directory> [<name prefix>]

list prints a directory's entries, newest first, without following a symlink, the first ENTRIES of them; a name
prefix narrows it to reach older ones. The directory is one the captain may read
(readable(), which captain_guard.py applies to Read too), or the projects directory, to find memory directories.

An allowlist of subcommands, flags and operand shapes; anything else is refused with exit 2 and nothing runs. The
tools run without a shell, with repository configuration that could run a program overridden: no pager, hooks,
fsmonitor, external diff, textconv, signature verification or network transport, and a fixed --format, since pretty
placeholders such as %G? run gpg. status and working-tree diff hash files through clean filters (git-lfs among
them, which also writes objects), so they are refused where a tracked file has a filter attribute; a submodule is
shown as its commit, never entered. The checkout must be where git itself puts its worktree and repository:
core.worktree or a gitfile pointing elsewhere is refused. A guard against mistakes, not isolation.
"""
import os
import re
import signal
import subprocess
import sys
import threading
import time

HOME = os.path.expanduser('~')
GIT_ROOTS = (os.path.join(HOME, 'code'), os.path.join(HOME, '.herdr', 'worktrees'))
STATE = os.path.join(HOME, '.cache', 'agentrc', 'captain')
SKILL = os.path.join(HOME, '.claude', 'skills', 'captain')
# every account's agent-memory links to this one, so captains on both accounts share one memory
MEMORY = os.path.join(HOME, '.claude', 'agent-memory', 'captain')
PROJECTS = os.path.join(HOME, '.claude', 'projects')   # the human's memory is <project>/memory
READ_ROOTS = (STATE, SKILL, MEMORY, *GIT_ROOTS, os.path.join(HOME, '.cache', 'chief-runs'))
ENTRIES = 200   # listed per directory
PREFIX = re.compile(r'[A-Za-z0-9._-]+')
CAP = 200_000   # bytes of output kept
TIMEOUT = 60    # seconds

FORMATS = {'oneline': '%h %s', 'dated': '%h %ci %an %s', 'reflog': '%h %gD %gs'}
REV = re.compile(r'(?!.*\.\.)[A-Za-z0-9][A-Za-z0-9._/-]*(?:~[0-9]*)*')
RANGE = re.compile(r'(?P<a>.+?)\.\.\.?(?P<b>.+)')
COUNT = re.compile(r'[0-9]{1,3}')
HARDEN = ['--no-pager', '--no-optional-locks', '-c', 'core.fsmonitor=false', '-c', 'core.hooksPath=/dev/null',
          '-c', 'protocol.allow=never']
NO_PROGRAMS = ['--no-ext-diff', '--no-textconv', '--submodule=short']   # diff output never runs one
ALIASES = {'-n': '--max-count', '-R': '--repo', '-L': '--limit', '-s': '--state', '-c': '--commit', '-b': '--branch'}

REPO = re.compile(r'[A-Za-z0-9_.-]+/[A-Za-z0-9_.-]+')
NUMBER = re.compile(r'[0-9]{1,12}')
SHA = re.compile(r'[0-9a-f]{7,40}')
# scalar --json fields only: a nested one (comments, reviews, files, statusCheckRollup) multiplies the request
PR = ('number,title,state,isDraft,author,url,headRefName,headRefOid,headRepositoryOwner,baseRefName,createdAt,'
      'updatedAt,mergedAt,closedAt,mergeable,mergeStateStatus,reviewDecision')
ISSUE = 'number,title,state,author,url,createdAt,updatedAt,closedAt'
RUN = 'databaseId,name,workflowName,displayTitle,status,conclusion,event,headBranch,headSha,url,createdAt,updatedAt'
CHECKS = 'name,state,bucket,workflow,link,startedAt,completedAt'
# (noun, verb): (value flags besides --repo, --json fields allowed, default); None for pr diff, which takes none
GH = {('pr', 'view'): ((), PR, PR),
      ('pr', 'checks'): ((), CHECKS, 'name,bucket'),   # a big PR has hundreds of checks
      ('pr', 'diff'): None,
      ('pr', 'list'): (('--limit', '--state', '--head', '--base'), PR,
                       'number,title,state,isDraft,author,headRefName,updatedAt,url'),
      ('issue', 'view'): ((), ISSUE + ',body', ISSUE + ',body'),
      ('issue', 'list'): (('--limit', '--state'), ISSUE, 'number,title,state,author,updatedAt,url'),
      ('run', 'view'): ((), RUN, RUN),
      ('run', 'list'): (('--limit', '--branch', '--commit'), RUN, RUN)}
LIMIT, DEFAULT_LIMIT = 30, 10


class Refused(Exception):
    pass


def rev(word):
    if not REV.fullmatch(word):
        raise Refused(f'{word!r} is not a revision this reader takes')
    return word


def revs(word):
    """A revision or an A..B / A...B range."""
    m = RANGE.fullmatch(word)
    if m:
        rev(m['a'])
        rev(m['b'])
        return word
    return rev(word)


def inside(path, root):
    real, root = os.path.realpath(path), os.path.realpath(root)
    return real == root or real.startswith(root + os.sep)


def approved(path):
    return any(inside(path, root) and os.path.realpath(path) != os.path.realpath(root) for root in GIT_ROOTS)


def readable(path):
    if not (isinstance(path, str) and os.path.isabs(path)):
        return False
    if any(inside(path, root) for root in READ_ROOTS):
        return True
    rel = os.path.relpath(os.path.realpath(path), os.path.realpath(PROJECTS)).split(os.sep)
    return len(rel) >= 2 and rel[0] != '..' and rel[1] == 'memory'


def directory(args):
    if len(args) not in (1, 2) or (len(args) == 2 and not PREFIX.fullmatch(args[1])):
        raise Refused('list takes one directory and an optional name prefix')
    path = os.path.expanduser(args[0])
    if not (readable(path) or (os.path.isabs(path) and os.path.realpath(path) == os.path.realpath(PROJECTS))):
        raise Refused(f'{args[0]!r} is not a directory the captain may read')
    return os.path.realpath(path), (args[1] if len(args) == 2 else '')


def checkout(path):
    if not os.path.isabs(os.path.expanduser(path)):
        raise Refused(f'{path!r} is not an absolute checkout path')
    if not approved(os.path.expanduser(path)):
        raise Refused(f'{path!r} is outside {", ".join(GIT_ROOTS)}')
    return os.path.realpath(os.path.expanduser(path))


def options(args, flags=(), valued=()):
    """Split args into (set of bare flags, {long flag: value}, operands); a short alias counts as its long name, and
    a value flag given twice, or any other dash word, is refused."""
    bare, values, rest = set(), {}, []
    it = iter(args)
    for word in it:
        name, eq, value = word.partition('=')
        name = ALIASES.get(name, name)
        if word in flags:
            bare.add(word)
        elif name in valued:
            if name in values:
                raise Refused(f'{name} is given twice')
            values[name] = value if eq else next(it, None)
            if values[name] is None:
                raise Refused(f'{name} needs a value')
        elif word.startswith('-'):
            raise Refused(f'{word} is not an option this reader takes')
        else:
            rest.append(word)
    return bare, values, rest


def bounded(value, default, cap, what):
    if value is None:
        return default
    if not COUNT.fullmatch(value) or not 0 < int(value) <= cap:
        raise Refused(f'{what} is 1..{cap}')
    return int(value)


def fmt(values, default):
    name = values.get('--format', default)
    if name not in FORMATS:
        raise Refused(f'--format takes {", ".join(FORMATS)}')
    return f'--format={FORMATS[name]}'


def git_command(args):
    """(checkout, git argv after the hardening, whether it reads the working tree)."""
    if len(args) < 3 or args[0] != '-C':
        raise Refused('git takes -C <checkout> first')
    where, sub, rest = checkout(args[1]), args[2], args[3:]
    if sub == 'status':
        if rest:
            raise Refused('status takes no arguments')
        return where, ['status', '--porcelain=v1', '--branch', '--untracked-files=normal',
                       '--ignore-submodules=all'], True
    if sub == 'worktree':
        if rest != ['list']:
            raise Refused('worktree takes only list')
        return where, ['worktree', 'list', '--porcelain'], False
    if sub == 'branch':
        bare, _, ops = options(rest, flags=('--show-current', '-a', '-r', '-v', '-vv'))
        if ops or not bare or ('--show-current' in bare and len(bare) > 1):
            raise Refused('branch takes --show-current, or -a/-r/-v to list')
        return where, ['branch', *sorted(bare)], False
    if sub == 'log':
        bare, values, ops = options(rest, flags=('--stat', '--name-status', '--all'),
                                    valued=('--max-count', '--format'))
        if len(ops) > 1:
            raise Refused('log takes one revision or range')
        argv = ['log', f'--max-count={bounded(values.get("--max-count"), 20, 100, "a count")}',
                fmt(values, 'oneline'), '--no-show-signature', *NO_PROGRAMS, *sorted(bare), *map(revs, ops), '--']
        return where, argv, False
    if sub == 'reflog':
        if rest[:1] != ['show']:
            raise Refused('reflog takes only show')
        _, values, ops = options(rest[1:], valued=('--max-count', '--format'))
        if len(ops) > 1:
            raise Refused('reflog show takes one ref')
        return where, ['reflog', 'show', f'--max-count={bounded(values.get("--max-count"), 20, 100, "a count")}',
                       fmt(values, 'reflog'), '--date=iso-strict', '--no-show-signature', *map(rev, ops), '--'], False
    if sub == 'show':
        bare, values, ops = options(rest, flags=('--stat', '--name-status', '--no-patch'), valued=('--format',))
        if len(ops) != 1:
            raise Refused('show takes one revision')
        return where, ['show', fmt(values, 'dated'), '--no-show-signature', *NO_PROGRAMS, *sorted(bare),
                       rev(ops[0]), '--'], False
    if sub == 'diff':
        bare, _, ops = options(rest, flags=('--stat', '--name-status', '--numstat', '--cached'))
        if len(ops) > 2:
            raise Refused('diff takes at most two revisions or one range')
        ops = [revs(w) for w in ops]
        tree = '--cached' not in bare and (not ops or (len(ops) == 1 and '..' not in ops[0]))
        # dirty would run status inside a submodule, whose filters the preflight does not cover
        return where, ['diff', *NO_PROGRAMS, '--ignore-submodules=dirty', *sorted(bare), *ops, '--'], tree
    if sub == 'rev-parse':
        if rest == ['--show-toplevel']:
            return where, ['rev-parse', '--show-toplevel'], False
        if len(rest) == 2 and rest[0] == '--verify':
            return where, ['rev-parse', '--verify', '--quiet', rev(rest[1])], False
        raise Refused('rev-parse takes --verify <rev> or --show-toplevel')
    if sub == 'merge-base':
        bare, _, ops = options(rest, flags=('--is-ancestor',))
        if len(ops) != 2:
            raise Refused('merge-base takes two revisions')
        return where, ['merge-base', *sorted(bare), *map(rev, ops)], False
    raise Refused(f'git {sub} is not a lookup this reader runs')


def gh_command(args):
    """gh argv, every flag checked and a bounded --json always given."""
    if len(args) < 2 or tuple(args[:2]) not in GH:
        raise Refused(f'gh {" ".join(args[:2])} is not a lookup this reader runs')
    noun, verb, rest = args[0], args[1], args[2:]
    spec = GH[noun, verb]
    valued, allowed, default = spec or ((), None, None)
    bare, values, ops = options(rest, flags=() if spec else ('--name-only',),
                                valued=('--repo', *valued, *(('--json',) if spec else ())))
    repo = values.pop('--repo', None)
    if not (repo and REPO.fullmatch(repo)):
        raise Refused('gh takes -R <owner>/<repo>')
    if verb == 'list':
        if ops:
            raise Refused(f'gh {noun} list takes no operand')
    elif len(ops) != 1 or not NUMBER.fullmatch(ops[0]):
        raise Refused(f'gh {noun} {verb} takes one number')
    out = [noun, verb, *ops, '-R', repo]
    if not spec:
        if '--name-only' not in bare:
            raise Refused('gh pr diff takes --name-only')
        return out + ['--name-only']
    fields = values.pop('--json', default)
    if not fields or set(fields.split(',')) - set(allowed.split(',')):
        raise Refused(f'--json fields are among {allowed}')
    if verb == 'list':
        out += ['--limit', str(bounded(values.pop('--limit', None), DEFAULT_LIMIT, LIMIT, '--limit'))]
    for name, value in values.items():
        if name == '--state' and value not in ('open', 'closed', 'merged', 'all'):
            raise Refused('--state is open, closed, merged or all')
        if name == '--commit' and not SHA.fullmatch(value):
            raise Refused('--commit is a hex SHA')
        out += [name, rev(value) if name in ('--head', '--base', '--branch') else value]
    return out + ['--json', fields]


def refusal(args):
    """Why captain_read.py would not run these arguments, or None; the guard asks the same question."""
    try:
        build(args)
    except Refused as why:
        return str(why)
    return None


def build(args):
    if not args or args[0] not in ('git', 'gh', 'list'):
        raise Refused('the reader runs git, gh or list')
    if args[0] == 'list':
        return 'list', directory(args[1:]), None, False
    if args[0] == 'git':
        where, argv, tree = git_command(args[1:])
        return 'git', where, argv, tree
    return 'gh', None, gh_command(args[1:]), False


def environment(tool):
    env = {k: v for k, v in os.environ.items() if not k.startswith('GIT_')}
    env.update(GIT_NO_LAZY_FETCH='1', GIT_TERMINAL_PROMPT='0', NO_COLOR='1', PAGER='cat')
    if tool == 'gh':
        env.update(GH_HOST='github.com', GH_PAGER='cat', GH_PROMPT_DISABLED='1', GH_NO_UPDATE_NOTIFIER='1')
    return env


def run(argv, env):
    """Exit code, with stdout copied up to CAP bytes; past that, or after TIMEOUT, the tool's whole process group is
    stopped, since a child holding the pipe would keep the read waiting."""
    late = threading.Event()
    with subprocess.Popen(argv, stdout=subprocess.PIPE, stdin=subprocess.DEVNULL, env=env,
                          start_new_session=True) as proc:
        def stop():
            try:
                os.killpg(proc.pid, signal.SIGKILL)
            except ProcessLookupError:
                pass
        timer = threading.Timer(TIMEOUT, lambda: (late.set(), stop()))
        timer.start()
        kept = proc.stdout.read(CAP + 1)
        if len(kept) > CAP:
            stop()
        proc.wait()
        timer.cancel()
    sys.stdout.buffer.write(kept[:CAP])
    sys.stdout.flush()
    if late.is_set():
        print(f'\ncaptain_read: stopped after {TIMEOUT}s', file=sys.stderr)
        return 124
    if len(kept) > CAP:
        print(f'\ncaptain_read: output cut at {CAP} bytes', file=sys.stderr)
        return 0
    return proc.returncode


def plain(git, env, *args, data=None):
    return subprocess.run([*git, *args], input=data, stdin=None if data is not None else subprocess.DEVNULL,
                          capture_output=True, env=env, timeout=TIMEOUT)


def located(git, env):
    """(worktree root, None), or (None, why) when git would work outside the approved roots: core.worktree or a
    gitfile can send it there."""
    found = plain(git, env, 'rev-parse', '--path-format=absolute', '--show-toplevel', '--git-dir', '--git-common-dir')
    places = found.stdout.decode(errors='replace').splitlines()
    if found.returncode or len(places) != 3:
        return None, 'not a checkout with a worktree'
    outside = [p for p in map(os.path.realpath, places) if not approved(p)]
    if outside:
        return None, f'git works in {outside[0]}, outside {", ".join(GIT_ROOTS)}'
    return places[0], None


def filtered(top, env):
    """Why a working-tree read would run a filter, or None: a tracked file with a filter attribute is hashed through
    it, so the read could run a program or, for git-lfs, write objects. check-attr prints a driver named unset or
    unspecified like those states, so such a driver counts as used."""
    git = ['git', *HARDEN, '-C', top]
    files = plain(git, env, 'ls-files', '-z')
    attrs = plain(git, env, 'check-attr', '-z', '--stdin', 'filter', data=files.stdout)
    named = plain(git, env, 'config', '--get-regexp', r'^filter\.(unset|unspecified)\.')
    if files.returncode or attrs.returncode:
        return 'could not read which files take a filter'
    values = attrs.stdout.split(b'\0')[2::3]
    if named.stdout.strip() or any(v not in (b'unspecified', b'unset') for v in values):
        return ('a tracked file here takes a clean filter, which status and a working-tree diff would run; '
                'compare commits instead')
    return None


def listing(path, prefix):
    entries = []
    with os.scandir(path) as it:
        for entry in it:
            if not entry.name.startswith(prefix):
                continue
            st = entry.stat(follow_symlinks=False)
            kind = 'l' if entry.is_symlink() else 'd' if entry.is_dir(follow_symlinks=False) else 'f'
            # an undecodable byte shows as \xNN, not as a surrogate a strict UTF-8 stdout cannot encode
            name = os.fsencode(entry.name).decode(errors='backslashreplace')
            entries.append((st.st_mtime, kind, st.st_size, name))
    entries.sort(reverse=True)
    for mtime, kind, size, name in entries[:ENTRIES]:
        print(f'{time.strftime("%Y-%m-%d %H:%M:%S", time.localtime(mtime))} {kind} {size:>10} {name}')
    if len(entries) > ENTRIES:
        print(f'captain_read: {len(entries) - ENTRIES} older entries not listed; a name prefix narrows the list',
              file=sys.stderr)
    return 0


def main(argv):
    try:
        tool, where, args, tree = build(argv)
    except Refused as why:
        print(f'captain_read: {why}', file=sys.stderr)
        return 2
    try:
        return dispatch(tool, where, args, tree)
    except (OSError, subprocess.SubprocessError) as err:
        print(f'captain_read: {err}', file=sys.stderr)
        return 1


def dispatch(tool, where, args, tree):
    if tool == 'list':
        return listing(*where)
    env = environment(tool)
    if tool == 'gh':
        return run(['gh', *args], env)
    git = ['git', *HARDEN, '-C', where]
    top, why = located(git, env)
    why = why or (tree and filtered(top, env))
    if why:
        print(f'captain_read: {why}', file=sys.stderr)
        return 2
    return run([*git, *args], env)


if __name__ == '__main__':
    sys.exit(main(sys.argv[1:]))
