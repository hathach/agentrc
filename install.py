#!/usr/bin/env python3
"""Link parts of this checkout into ~/.claude, ~/.codex and ~/.local/bin, or into one work tree's
.claude, by explicit choice.

    install.py install --skill --agent --workflow --bin --claude-md --statusline
    install.py remove --skill
    install.py install --project <work tree> --skill --agent --workflow --claude-md [--skip-clashes]

Skills link into ~/.claude/skills and ~/.codex/skills; agents into
~/.claude/agents (the .md) and ~/.codex/agents (the .md and its .toml);
a skill's hooks of the same name into ~/.claude/hooks, with their hooks.json
merged into ~/.claude/settings.json; workflows into ~/.claude/workflows
(Claude only); bin/ commands into ~/.local/bin;
--claude-md links instructions/user.md as ~/.claude/CLAUDE.md and
~/.codex/AGENTS.md; --statusline links the statusline/ files into ~/.claude
and sets statusLine in its settings.json. Each flag takes every entry of its
kind; nothing is selected by default. Everything is a symlink, so edits are
live and a rerun after a change is a no-op.

Refuses before touching anything when a destination holds something that is
not a link, or a directory to link into is a file or a dangling link. `remove`
unlinks the named entries whatever they point to but never deletes a real
file or directory; --claude-md and --statusline links are removed only when
they link into this checkout, and statusLine only when it is the one install
writes.

--project <dir> scopes the install to one work tree instead, and leaves the
user-wide directories alone. <dir> must be a repository's or a linked
worktree's top level: Claude Code finds project skills, agents and workflows
only up to the work tree root and reads project settings only from the
session's start directory, so each worktree that needs them is installed.
Skills, a skill's hooks, agents (the .md only) and workflows link into
<dir>/.claude/{skills,hooks,agents,workflows}, and --claude-md links
instructions/user.md as <dir>/CLAUDE.local.md, Claude Code's per-user
instructions for that project. The hooks are registered in
<dir>/.claude/settings.local.json, never the tracked settings.json, by absolute
path as above, beside env AGENTRC_DIR: this checkout with / separators, which
the `${AGENTRC_DIR:-$HOME/.claude}` paths in the skills, agents and workflows
resolve to; remove takes AGENTRC_DIR out once no link of ours is left there,
and the file too when nothing else is in it. Codex, bin/ and the status line
are user-wide, so --project refuses their flags. Each link, and
settings.local.json unless git already ignores it, is an entry of one marked
block in the repository's exclude file (`git rev-parse --git-common-dir`/info/
exclude), which every worktree shares: an entry stays while any worktree of
the repository has our link there, so `git status` stays clean, and a broken
block (a marker missing, repeated or out of order) refuses. A destination the
repository tracks, or a real file or nonempty directory there such as the
project's own skill of that name, refuses before any change; --skip-clashes
leaves it and skips that entry, and its hook's registration with it, and a
skipped skill's hook. A symlinked directory between <dir> and a destination,
and a settings.local.json that is tracked or a link, refuse both actions;
pruning leaves a tracked link alone.
"""
import argparse
import json
import os
import re
import shlex
import shutil
import subprocess
import sys
import tempfile
from pathlib import Path

REPO = Path(__file__).resolve().parent
KINDS = ('skill', 'agent', 'hook', 'workflow', 'bin')
FLAGS = ('skill', 'agent', 'workflow', 'bin')  # hooks come with the skill of the same name
STATUSLINE = {'type': 'command', 'command': 'bash "$HOME/.claude/statusline.sh"'}
PROJECT = None  # --project: the work tree whose .claude takes the Claude entries
ICASE = False  # --project: git matches PROJECT's names case-insensitively, as on a Windows or macOS file system
SETTINGS_LOCAL = '.claude/settings.local.json'
BLOCK = ('# >>> agentrc install.py --project: managed by its install and remove',
         '# <<< agentrc install.py --project')
GIT_LOCATION = ('GIT_DIR', 'GIT_WORK_TREE', 'GIT_INDEX_FILE', 'GIT_COMMON_DIR')  # would name another repository


def available(kind):
    if kind == 'skill':
        # a moved skill's directory can outlive it, kept by an ignored __pycache__
        return sorted(p.name for p in (REPO / 'skills').iterdir() if (p / 'SKILL.md').is_file())
    if kind == 'agent':
        return sorted(p.stem for p in (REPO / 'agents').glob('*.md'))
    if kind == 'workflow':
        return sorted(p.stem for p in (REPO / 'workflows').glob('*.js'))
    if kind == 'bin':
        return sorted(p.name for p in (REPO / 'bin').iterdir() if p.is_file() and os.access(p, os.X_OK))
    return sorted(p.name for p in (REPO / 'hooks').iterdir() if (p / 'hooks.json').exists())


def claude_dir():
    return PROJECT / '.claude' if PROJECT else Path.home() / '.claude'


def dirs(kind):
    """Where entries of a kind are linked to."""
    if PROJECT:
        return [claude_dir() / {'skill': 'skills', 'agent': 'agents', 'hook': 'hooks', 'workflow': 'workflows'}[kind]]
    claude, codex = Path.home() / '.claude', Path.home() / '.codex'
    return {'skill': [claude / 'skills', codex / 'skills'], 'agent': [claude / 'agents', codex / 'agents'],
            'hook': [claude / 'hooks'], 'workflow': [claude / 'workflows'],
            'bin': [Path.home() / '.local' / 'bin']}[kind]


def links(kind, name):
    """(source, destination) pairs for one entry; a relative source is a
    relative symlink."""
    if kind == 'skill':
        return [(REPO / 'skills' / name, d / name) for d in dirs(kind)]
    if kind == 'agent':
        md, toml = REPO / 'agents' / f'{name}.md', REPO / 'agents' / f'{name}.toml'
        pairs = [(md, d / md.name) for d in dirs(kind)]
        return pairs + ([(toml, dirs(kind)[1] / toml.name)] if toml.exists() and not PROJECT else [])
    if kind == 'hook':
        return [(REPO / 'hooks' / name, dirs(kind)[0] / name)]
    if kind == 'workflow':
        return [(REPO / 'workflows' / f'{name}.js', dirs(kind)[0] / f'{name}.js')]
    if kind == 'bin':
        return [(REPO / 'bin' / name, dirs(kind)[0] / name)]
    if kind == 'statusline':  # statusline.sh runs its helpers from $HOME/.claude
        return [(REPO / 'statusline' / f, Path.home() / '.claude' / f)
                for f in ('statusline.sh', 'statusline-codex-usage.py', 'statusline-prs.py')]
    if PROJECT:  # a link, not an @-import: Claude Code skips an import from outside the project until approved
        return [(REPO / 'instructions' / 'user.md', PROJECT / 'CLAUDE.local.md')]
    return [(REPO / 'instructions' / 'user.md', Path.home() / '.claude' / 'CLAUDE.md'),
            (Path('../.claude/CLAUDE.md'), Path.home() / '.codex' / 'AGENTS.md')]


def canonical(path):
    """Comparable absolute path text, including dead Windows symlink targets."""
    value = os.fspath(path)
    if os.name == 'nt':  # stripped first: MSYS2's Python normalizes its \ to /
        value = value.removeprefix('\\\\?\\')
    return os.path.normcase(os.path.abspath(os.path.normpath(value)))


def target(path):
    value = os.readlink(path)
    return canonical(value if os.path.isabs(value) else path.parent / value)


def inside(path, directory):
    try:
        return os.path.commonpath((canonical(path), canonical(directory))) == canonical(directory)
    except ValueError:
        return False


def ours(path):
    """A symlink whose target resolves inside this checkout."""
    return path.is_symlink() and inside(path.resolve(strict=False), REPO)


def refuse(reason):
    print(f'install.py: {reason}', file=sys.stderr)
    sys.exit(1)


def directories_for(path):
    """Refuse unless each missing directory above path can be made: the nearest existing one is a real directory."""
    for parent in path.parents:
        if parent.is_dir():
            break
        if parent.exists() or parent.is_symlink():
            refuse(f'{parent} is not a directory; move it aside first')


def preflight(pairs):
    """Every destination must be absent, a symlink or an empty directory; every
    directory linked into, and each missing one's ancestors, must be a real
    directory or absent."""
    if os.name == 'nt' and pairs:  # links need Developer Mode or an elevated shell: find out before the first
        with tempfile.TemporaryDirectory() as probe:
            try:
                os.symlink('target', os.path.join(probe, 'link'))
            except OSError as err:
                refuse(f'cannot create symlinks ({err.strerror}); turn on Developer Mode or run elevated')
    for _, dst in pairs:
        directories_for(dst)
        if dst.is_symlink() or not dst.exists():
            continue
        if dst.is_dir() and not any(dst.iterdir()):
            continue
        refuse(f'{dst} exists and is not a symlink; move it aside first')


def link(src, dst):
    expected = src if src.is_absolute() else dst.parent / src
    if dst.is_symlink() and target(dst) == canonical(expected):
        return
    dst.parent.mkdir(parents=True, exist_ok=True)
    if dst.is_symlink() or dst.exists():
        staging = Path(tempfile.mkdtemp(prefix='.agentrc-', dir=dst.parent))
        aside = staging / dst.name
        os.rename(dst, aside)  # atomic, so whatever is there is inspected before it is removed
        if aside.is_symlink():
            aside.unlink()
        elif aside.is_dir() and not any(aside.iterdir()):
            aside.rmdir()
        else:
            refuse(f'{dst} was not a link; what was there is now in {aside}')
        staging.rmdir()
    try:
        # never overwrites; a relative Windows link resolves only with \, which MSYS2's Python does not write
        os.symlink(str(src).replace('/', '\\') if os.name == 'nt' else src, dst)
    except FileExistsError:
        refuse(f'{dst} appeared while installing; move it aside first')
    print(f'{dst} -> {src}')


def unlink(dst):
    if dst.is_symlink():
        dst.unlink()
        print(f'removed {dst}')
    elif dst.exists():
        print(f'{dst} is not a link, left alone')


def prune(kind):
    """Drop our links whose source left the repo, or is a skill directory without SKILL.md; leave every other link
    alone, and never the directory a link names."""
    source = REPO / {'skill': 'skills', 'agent': 'agents', 'hook': 'hooks', 'workflow': 'workflows', 'bin': 'bin'}[kind]
    dead = [path for parent in dirs(kind) for path in (parent.iterdir() if parent.is_dir() else ())
            if path.is_symlink() and (not path.exists() or kind == 'skill' and not (path / 'SKILL.md').is_file())
            and inside(target(path), source)]
    listed = tracked([relative(path) for path in dead]) if PROJECT else set()
    for path in dead:
        if PROJECT and is_tracked(relative(path), listed):
            print(f'{path} is tracked by the repository, left alone')
        else:
            unlink(path)


# --- hook registration in settings.json --------------------------------------

def hook_entry(hook, name):
    """Is this settings entry ours: does the command run out of the linked hook
    directory?"""
    root = claude_dir() / 'hooks' / name
    # bash quoting as written now; on Windows also the list2cmdline form earlier versions wrote
    for posix in (True, False) if os.name == 'nt' else (True,):
        try:
            words = shlex.split(hook.get('command', ''), posix=posix)
        except ValueError:
            continue
        # only absolute words: a relative one would resolve against the cwd
        paths = [p for p in (word.strip('"\'') for word in words) if os.path.isabs(p)]
        if any(inside(p, root) for p in paths):
            return True
    return False


def quote_command(path):
    """For the shell Claude Code runs hooks in, bash also on Windows: there a backslash is an escape. On
    Windows a Python hook names this interpreter: bash may find no python3 there, and a hook that cannot
    start lets the call through."""
    command = shlex.quote(Path(path).as_posix())
    if os.name == 'nt' and Path(path).suffix == '.py':
        command = f'{shlex.quote(Path(sys.executable).as_posix())} {command}'
    return command


def strip(hooks, name):
    for event in list(hooks):
        groups = []
        for group in hooks[event]:
            kept = [h for h in group.get('hooks', []) if not hook_entry(h, name)]
            if kept:
                groups.append({**group, 'hooks': kept})
        if groups:
            hooks[event] = groups
        else:
            del hooks[event]


def settings():
    return PROJECT / SETTINGS_LOCAL if PROJECT else Path.home() / '.claude' / 'settings.json'


def plan_settings(names, install, statusline, keep_env=False, drop=()):
    """(before, after) texts for registering or unregistering these hooks and
    the status line, and with --project AGENTRC_DIR, or None when nothing would
    change; after is None for a project file left empty. Computed before any
    link moves, so a settings file of the wrong shape refuses instead of
    leaving a half job. keep_env: links of ours stay, so remove keeps AGENTRC_DIR. drop: hooks unregistered
    whatever the action."""
    path = settings()
    before = path.read_text(encoding='utf-8') if path.exists() else ''
    data = json.loads(before) if before else {}
    if names or drop:
        hooks = data.setdefault('hooks', {})
        for name in drop:
            strip(hooks, name)
        for name in names:
            strip(hooks, name)
            if install:
                linked = claude_dir() / 'hooks' / name
                for event, groups in json.loads((REPO / 'hooks' / name / 'hooks.json').read_text(encoding='utf-8')).items():
                    for group in groups:
                        entries = [{**h, 'command': quote_command(linked / h['command'])} for h in group['hooks']]
                        hooks.setdefault(event, []).append({**group, 'hooks': entries})
        if not hooks:
            del data['hooks']
    if statusline and install:
        if data.get('statusLine', STATUSLINE) != STATUSLINE:
            refuse(f'{path} has another statusLine; remove it first')
        data['statusLine'] = STATUSLINE
    elif statusline and data.get('statusLine') == STATUSLINE:
        del data['statusLine']
    if PROJECT:
        mine, env = REPO.as_posix(), data.get('env', {})
        if not isinstance(env, dict):
            raise TypeError('env is not an object')
        if install:
            if env.get('AGENTRC_DIR', mine) != mine:
                refuse(f'{path} sets AGENTRC_DIR to {env["AGENTRC_DIR"]}, another checkout; remove it first')
            data['env'] = {**env, 'AGENTRC_DIR': mine}
        elif not keep_env and env.get('AGENTRC_DIR') == mine:
            del env['AGENTRC_DIR']
            if not env:
                del data['env']
    if data == (json.loads(before) if before else {}):
        return None
    return before, (json.dumps(data, indent=2) + '\n' if data or not PROJECT else None)


def write_settings(before, after):
    path = settings()
    if after is None:  # a project's local file with nothing left in it
        path.unlink()
        print(f'removed {path}')
        return
    backup = path.with_name(path.name + '.before-agentrc')
    if before and not backup.exists() and not PROJECT:  # in a work tree it would be one more untracked file
        backup.write_text(before, encoding='utf-8')
    path.parent.mkdir(parents=True, exist_ok=True)
    path.write_text(after, encoding='utf-8')
    print(f'updated {path}')


# --- project mode: the work tree, clashes and git's exclude file ---------------

def git(top, *args, check=True, stdin=None):
    """git run in top, without fsmonitor, which would leave a daemon behind per repository."""
    exe = shutil.which('git')
    if not exe:
        refuse('git is not on PATH; --project needs it')
    done = subprocess.run([exe, '-c', 'core.fsmonitor=false', '-C', str(top), *args], capture_output=True, input=stdin,
                          env={k: v for k, v in os.environ.items() if k not in GIT_LOCATION})
    if check and done.returncode:
        refuse(f'`git {" ".join(args)}` in {top} failed: {os.fsdecode(done.stderr).strip()}')
    return done


def work_tree(path):
    """(the work tree's top level, its repository's exclude file, every work tree of the repository)."""
    if not Path(path).is_dir():
        refuse(f'--project {path} is not a directory')
    out = os.fsdecode(git(path, 'rev-parse', '--path-format=absolute', '--show-toplevel', '--git-common-dir').stdout)
    top, common = map(Path, out.splitlines())
    if canonical(os.path.realpath(top)) != canonical(os.path.realpath(path)):
        refuse(f'--project {path} is not a work tree top level ({top} is): Claude Code reads project settings only '
               'from the directory a session starts in')
    listed = os.fsdecode(git(top, 'worktree', 'list', '--porcelain', '-z').stdout).split('\0')
    trees = [Path(f[len('worktree '):]) for f in listed if f.startswith('worktree ')]
    return top, common / 'info' / 'exclude', trees


def relative(dst):
    return dst.relative_to(PROJECT).as_posix()


def ignorecase(top):
    """Whether git treats the work tree's names case-insensitively, as on a Windows or macOS file system."""
    return git(top, 'config', '--bool', 'core.ignorecase', check=False).stdout.strip() == b'true'


def tracked(rels):
    """The paths the repository tracks among rels and below them, matched as git matches names."""
    if not rels:
        return set()
    magic = 'literal,icase' if ICASE else 'literal'
    listed = git(PROJECT, 'ls-files', '-z', '--', *(f':({magic}){rel}' for rel in rels)).stdout
    return set(os.fsdecode(listed).split('\0')) - {''}


def is_tracked(rel, listed):
    if ICASE:
        rel, listed = rel.casefold(), {t.casefold() for t in listed}
    return any(t == rel or t.startswith(rel + '/') for t in listed)


def is_link(path):
    """A symlink, or on Windows a junction, which is_symlink() does not count."""
    return path.is_symlink() or getattr(path, 'is_junction', lambda: False)()


def contained(paths):
    """Refuse a symlinked directory between the work tree and a destination, which would carry the install
    and a remove out of the work tree, and a settings.local.json that is tracked or a link: it is rewritten."""
    for path in paths:
        for parent in path.parents:
            if parent == PROJECT:
                break
            if is_link(parent):
                refuse(f'{parent} is a symlink or junction; a project install stays inside {PROJECT}, '
                       'so make it a directory')
    local = PROJECT / SETTINGS_LOCAL
    if is_link(local) or is_tracked(SETTINGS_LOCAL, tracked([SETTINGS_LOCAL])):
        refuse(f'{local} is tracked or a link, and install.py rewrites it; make it an untracked file first')


def clashes(pairs):
    """{destination: why} for each one the repository tracks, or that holds a real file or a nonempty directory."""
    rels = {relative(dst): dst for _, (_, dst) in pairs}
    listed = tracked(list(rels))
    found = {}
    for rel, dst in rels.items():
        if is_tracked(rel, listed):
            found[dst] = 'is tracked by the repository'
        elif dst.is_symlink() or not dst.exists() or (dst.is_dir() and not any(dst.iterdir())):
            continue
        elif dst.is_dir() and (files := [p for p in dst.rglob('*') if p.is_file()]) and \
                all('__pycache__' in p.relative_to(dst).parts for p in files):
            found[dst] = 'holds only Python bytecode caches, left by an earlier copy of the project; delete it'
        else:
            found[dst] = 'is a real file or directory'
    return found


def pattern(rel):
    """An exclude line naming exactly the path rel."""
    return '/' + re.sub(r'([][*?\\])', r'\\\1', rel)


def read_exclude(exclude):
    """The exclude file's lines as git numbers them, split on \\n only; bytes that are not UTF-8 survive a rewrite."""
    text = exclude.read_bytes().decode('utf-8', 'surrogateescape') if exclude.exists() else ''
    lines = text.split('\n')
    return lines[:-1] if lines[-1] == '' else lines


def read_block(exclude):
    """(the exclude file's lines outside our block, the block's entries, the block's line range); refuses a block
    whose markers are missing, repeated or out of order, which a rewrite would lose the user's lines to."""
    lines = read_exclude(exclude)
    bare = [line.removesuffix('\r') for line in lines]
    starts, ends = ([i for i, line in enumerate(bare) if line == mark] for mark in BLOCK)
    if not starts and not ends:
        return lines, [], range(0)
    if len(starts) != 1 or len(ends) != 1 or ends[0] < starts[0]:
        refuse(f'{exclude} has a broken agentrc block: each marker line must appear once, in order; fix it first')
    start, end = starts[0], ends[0]
    return lines[:start] + lines[end + 1:], [line for line in bare[start + 1:end] if line.startswith('/')], \
        range(start + 2, end + 1)  # git's 1-based numbers of the lines between the markers


def ignored_elsewhere(exclude, block, rel):
    """Whether a pattern outside our block, as read_block parsed it, already ignores rel."""
    done = git(PROJECT, 'check-ignore', '-v', '-z', '--no-index', '--stdin', check=False, stdin=os.fsencode(rel) + b'\0')
    if done.returncode:
        return False
    source, line, rule = os.fsdecode(done.stdout).split('\0')[:3]  # -z: no quoting of the source's path
    if rule.startswith('!'):
        return False
    ours = canonical(PROJECT / source) == canonical(exclude) and int(line) in block[2]
    return not ours


def write_exclude(exclude, block, trees, added):
    """Our block: its entries and the added ones, each kept while a work tree of the repository has our link there,
    not a link or file of the project's own."""
    rest, entries, _ = block
    paths = {rel: pattern(rel) for rel in added}
    paths.update({re.sub(r'\\(.)', r'\1', e[1:]): e for e in entries})
    keep = [entry for rel, entry in sorted(paths.items())
            if any((tree / rel).is_file() if rel == SETTINGS_LOCAL else ours(tree / rel) for tree in trees)]
    lines = rest + ([BLOCK[0], *keep, BLOCK[1]] if keep else [])
    after = ('\n'.join(lines) + '\n' if lines else '').encode('utf-8', 'surrogateescape')
    if after == (exclude.read_bytes() if exclude.exists() else b''):
        return
    exclude.parent.mkdir(parents=True, exist_ok=True)
    exclude.write_bytes(after)
    print(f'updated {exclude}')


# --- command line -------------------------------------------------------------

def selection(parser, args):
    chosen = [(kind, name) for kind in FLAGS if getattr(args, kind) for name in available(kind)]
    chosen += [('hook', name) for kind, name in chosen if kind == 'skill' and name in available('hook')]
    if args.claude_md:
        chosen.append(('claude-md', None))
    if args.statusline:
        chosen.append(('statusline', None))
    if not chosen:
        parser.error('nothing selected')
    return chosen


def project_clashes(args, pairs):
    """(the pairs left once the project's own entries are set aside, the hook destinations of skipped skills):
    install refuses on one unless --skip-clashes, and remove leaves each alone."""
    found = clashes(pairs)
    if found and args.action == 'install' and not args.skip_clashes:
        refuse('\n'.join(f'{dst} {found[dst]}; agentrc\'s {kind} of that name would shadow it or be shadowed'
                         for kind, (_, dst) in pairs if dst in found) +
               '\nmove it aside first, or rerun with --skip-clashes to leave it and skip that entry')
    for dst, why in found.items():
        print(f'{dst} {why}, ' + ('skipped' if args.action == 'install' else 'left alone'))
    kept = [(kind, pair) for kind, pair in pairs if pair[1] not in found]
    orphaned = []  # agentrc's hook of a skill the project has itself would guard the wrong skill
    if args.action == 'install':
        skipped = {dst.name for kind, (_, dst) in pairs if kind == 'skill' and dst in found}
        orphaned = [dst for kind, (_, dst) in kept if kind == 'hook' and dst.name in skipped]
        for dst in orphaned:
            print(f'{dst} belongs to the skipped skill {dst.name}, skipped')
        kept = [(kind, pair) for kind, pair in kept if pair[1] not in orphaned]
    return kept, orphaned


def main(argv=None):
    global PROJECT, ICASE
    PROJECT, ICASE = None, False
    parser = argparse.ArgumentParser(description=__doc__, formatter_class=argparse.RawDescriptionHelpFormatter)
    parser.add_argument('action', choices=('install', 'remove'))
    for kind in FLAGS:
        parser.add_argument(f'--{kind}', action='store_true', help=f'every {kind}')
    parser.add_argument('--claude-md', action='store_true', help='instructions/user.md as ~/.claude/CLAUDE.md, also '
                                                                    '~/.codex/AGENTS.md; with --project as its CLAUDE.local.md')
    parser.add_argument('--statusline', action='store_true', help='the Claude Code status line')
    parser.add_argument('--project', metavar='DIR', help="into this work tree's .claude, for Claude Code only")
    parser.add_argument('--skip-clashes', action='store_true', help="with --project, skip an entry the project has itself")
    args = parser.parse_args(argv)
    if args.skip_clashes and not args.project:
        parser.error('--skip-clashes goes with --project')
    if args.project and (user_wide := [f for f in ('bin', 'statusline') if getattr(args, f)]):
        parser.error(f'--project takes only --skill, --agent, --workflow and --claude-md: '
                     f'--{user_wide[0]} is user-wide')
    chosen = selection(parser, args)
    if args.project:
        PROJECT, exclude, trees = work_tree(args.project)
        ICASE = ignorecase(PROJECT)
        block = read_block(exclude)  # a broken block refuses before any change
    pairs = [(kind, pair) for kind, name in chosen for pair in links(kind, name)]
    orphaned = []
    if PROJECT:
        contained([dst for _, (_, dst) in pairs] + [settings()])
        pairs, orphaned = project_clashes(args, pairs)
        if args.action == 'install' and not pairs:  # every entry skipped: no AGENTRC_DIR or exclude entry for none
            return
    hooks = [dst.name for kind, (_, dst) in pairs if kind == 'hook']  # a skipped hook's link is not registered
    planned = None
    if hooks or args.statusline or PROJECT:
        removing = {dst for _, (_, dst) in pairs}
        keep_env = PROJECT and any(ours(p) and p not in removing for p in [PROJECT / 'CLAUDE.local.md'] + [
            p for kind in ('skill', 'agent', 'hook', 'workflow') for d in dirs(kind) if d.is_dir() for p in d.iterdir()])
        try:
            planned = plan_settings(hooks, args.action == 'install', args.statusline, keep_env,
                                    [dst.name for dst in orphaned if ours(dst)])  # a project's own hook keeps its own
        except (ValueError, TypeError, AttributeError, KeyError) as err:
            refuse(f'{settings()} cannot be edited ({err!r}); fix it first')

    if args.action == 'remove':
        foreign = [dst for kind, (_, dst) in pairs if kind in ('claude-md', 'statusline') and not ours(dst)]
        for kind, (_, dst) in pairs:  # ownership settled before any unlink: the Codex link is relative
            if dst in foreign:
                print(f'{dst} does not link into this checkout, left alone')
            else:
                unlink(dst)
        if planned:
            write_settings(*planned)
        if PROJECT:
            write_exclude(exclude, block, trees, [])
        return

    preflight([pair for _, pair in pairs])
    if planned and planned[1] is not None:  # CLAUDE.local.md alone does not check the .claude it is written into
        directories_for(settings())
    for _, (src, dst) in pairs:
        link(src, dst)
    for dst in orphaned:  # linked by an earlier install, before the project had the skill
        if ours(dst):
            unlink(dst)
    for kind in {kind for kind, _ in chosen if kind in KINDS}:
        prune(kind)
    if planned:
        write_settings(*planned)
    if PROJECT:
        local = [] if ignored_elsewhere(exclude, block, SETTINGS_LOCAL) else [SETTINGS_LOCAL]
        write_exclude(exclude, block, trees, [relative(dst) for _, (_, dst) in pairs] + local)


if __name__ == '__main__':
    main()
