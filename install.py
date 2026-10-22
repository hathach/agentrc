#!/usr/bin/env python3
"""Link parts of this checkout into ~/.claude, ~/.codex and ~/.local/bin, by explicit choice.

    install.py install --skill --agent --workflow --bin --claude-md --statusline --herdr-plugin
    install.py remove --skill

Skills link into ~/.claude/skills and ~/.codex/skills; agents into
~/.claude/agents (the .md) and ~/.codex/agents (the .md and its .toml);
a skill's hooks of the same name into ~/.claude/hooks, with their hooks.json
merged into ~/.claude/settings.json; workflows into ~/.claude/workflows
(Claude only); bin/ commands into ~/.local/bin;
--claude-md links instructions/user.md as ~/.claude/CLAUDE.md and
~/.codex/AGENTS.md; --statusline links the statusline/ files into ~/.claude
and sets statusLine in its settings.json; --herdr-plugin links each plugins/*
herdr plugin into herdr with `herdr plugin link <dir> --enabled` (herdr keeps
the registration, so edits are live too). Each flag takes every entry of its
kind; nothing is selected by default. Everything else is a symlink, so edits are
live and a rerun after a change is a no-op.

Refuses before touching anything when a destination holds something that is
not a link, or a directory to link into is a file or a dangling link. `remove`
unlinks the named entries whatever they point to but never deletes a real
file or directory; --claude-md and --statusline links are removed only when
they link into this checkout, and statusLine only when it is the one install
writes. A herdr plugin is linked unless herdr holds its id from elsewhere,
and unlinked only when linked from this checkout; herdr steps run before any
link, so herdr's own refusals (min_herdr_version, unlink without a running
server) change nothing.
"""
import argparse
import json
import os
import shlex
import shutil
import subprocess
import sys
import tempfile
import tomllib
from pathlib import Path

REPO = Path(__file__).resolve().parent
KINDS = ('skill', 'agent', 'hook', 'workflow', 'bin')
FLAGS = ('skill', 'agent', 'workflow', 'bin')  # hooks come with the skill of the same name
STATUSLINE = {'type': 'command', 'command': 'bash "$HOME/.claude/statusline.sh"'}


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


def dirs(kind):
    """Where entries of a kind are linked to."""
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
        return pairs + ([(toml, dirs(kind)[1] / toml.name)] if toml.exists() else [])
    if kind == 'hook':
        return [(REPO / 'hooks' / name, dirs(kind)[0] / name)]
    if kind == 'workflow':
        return [(REPO / 'workflows' / f'{name}.js', dirs(kind)[0] / f'{name}.js')]
    if kind == 'bin':
        return [(REPO / 'bin' / name, dirs(kind)[0] / name)]
    if kind == 'statusline':  # statusline.sh runs its helpers from $HOME/.claude
        return [(REPO / 'statusline' / f, Path.home() / '.claude' / f)
                for f in ('statusline.sh', 'statusline-codex-usage.py', 'statusline-prs.py')]
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
        for parent in dst.parents:
            if parent.is_dir():
                break
            if parent.exists() or parent.is_symlink():
                refuse(f'{parent} is not a directory; move it aside first')
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
    for parent in dirs(kind):
        for path in parent.iterdir() if parent.is_dir() else ():
            if not path.is_symlink():
                continue
            gone = not path.exists() or kind == 'skill' and not (path / 'SKILL.md').is_file()
            if gone and inside(target(path), source):
                unlink(path)


# --- hook registration in settings.json --------------------------------------

def hook_entry(hook, name):
    """Is this settings entry ours: does the command run out of the linked hook
    directory?"""
    root = Path.home() / '.claude' / 'hooks' / name
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
    return Path.home() / '.claude' / 'settings.json'


def plan_settings(names, install, statusline):
    """(before, after) texts for registering or unregistering these hooks and
    the status line, or None when nothing would change. Computed before any
    link moves, so a settings file of the wrong shape refuses instead of
    leaving a half job."""
    path = settings()
    before = path.read_text(encoding='utf-8') if path.exists() else ''
    data = json.loads(before) if before else {}
    if names:
        hooks = data.setdefault('hooks', {})
        for name in names:
            strip(hooks, name)
            if install:
                linked = Path.home() / '.claude' / 'hooks' / name
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
    if data == (json.loads(before) if before else {}):
        return None
    return before, json.dumps(data, indent=2) + '\n'


def write_settings(before, after):
    path = settings()
    backup = path.with_name(path.name + '.before-agentrc')
    if before and not backup.exists():
        backup.write_text(before, encoding='utf-8')
    path.parent.mkdir(parents=True, exist_ok=True)
    path.write_text(after, encoding='utf-8')
    print(f'updated {path}')


# --- herdr plugins ------------------------------------------------------------

def herdr_plugins():
    """(id, directory) of every plugins/* herdr plugin."""
    return [(tomllib.loads(toml.read_text(encoding='utf-8'))['id'], toml.parent)
            for toml in sorted((REPO / 'plugins').glob('*/herdr-plugin.toml'))]


def herdr(*args):
    exe = os.environ.get('HERDR_BIN_PATH') or shutil.which('herdr')
    if not exe:
        refuse('herdr is not on PATH; install it or leave out --herdr-plugin')
    return subprocess.run([exe, *args], capture_output=True, text=True)


def plan_herdr(install):
    """[(herdr argv, message)] to link or unlink our plugins. herdr's own refusals (an older herdr
    than min_herdr_version, unlink without a running server) come from run_herdr, which runs before
    any other change."""
    listed = herdr('plugin', 'list', '--json')
    try:
        plugins = {p['plugin_id']: p for p in json.loads(listed.stdout)['result']['plugins']}
    except (ValueError, KeyError, TypeError):
        refuse(f'`herdr plugin list --json` gave no plugin list: {(listed.stderr or listed.stdout).strip()}')
    steps = []
    for pid, d in herdr_plugins():
        cur = plugins.get(pid)
        mine = bool(cur) and cur.get('source', {}).get('kind') == 'local' and \
            canonical(os.path.realpath(cur.get('plugin_root', ''))) == canonical(os.path.realpath(d))
        if install:
            if cur and not mine:
                refuse(f'herdr already has plugin {pid} from {cur.get("plugin_root")}; unlink it first')
            if not (cur and cur.get('enabled')):  # link replaces a disabled entry of the same id
                steps.append((['plugin', 'link', str(d), '--enabled'], f'herdr plugin {pid} -> {d}'))
        elif mine:
            steps.append((['plugin', 'unlink', pid], f'unlinked herdr plugin {pid}'))
        elif cur:
            print(f'herdr plugin {pid} is not linked from this checkout, left alone')
    return steps


def run_herdr(steps):
    for argv, message in steps:
        done = herdr(*argv)
        if done.returncode:
            refuse(f'`herdr {" ".join(argv)}` failed: {(done.stderr or done.stdout).strip()}')
        print(message)


# --- command line -------------------------------------------------------------

def selection(parser, args):
    chosen = [(kind, name) for kind in FLAGS if getattr(args, kind) for name in available(kind)]
    chosen += [('hook', name) for kind, name in chosen if kind == 'skill' and name in available('hook')]
    if args.claude_md:
        chosen.append(('claude-md', None))
    if args.statusline:
        chosen.append(('statusline', None))
    if not chosen and not args.herdr_plugin:
        parser.error('nothing selected')
    return chosen


def main(argv=None):
    parser = argparse.ArgumentParser(description=__doc__, formatter_class=argparse.RawDescriptionHelpFormatter)
    parser.add_argument('action', choices=('install', 'remove'))
    for kind in FLAGS:
        parser.add_argument(f'--{kind}', action='store_true', help=f'every {kind}')
    parser.add_argument('--claude-md', action='store_true', help='instructions/user.md as ~/.claude/CLAUDE.md, also ~/.codex/AGENTS.md')
    parser.add_argument('--statusline', action='store_true', help='the Claude Code status line')
    parser.add_argument('--herdr-plugin', action='store_true', help='every plugins/* herdr plugin, through herdr')
    args = parser.parse_args(argv)
    if args.herdr_plugin and os.name == 'nt':
        refuse('--herdr-plugin: herdr runs on Linux and macOS only')
    chosen = selection(parser, args)
    pairs = [(kind, pair) for kind, name in chosen for pair in links(kind, name)]
    herdr_steps = plan_herdr(args.action == 'install') if args.herdr_plugin else []
    hooks = [name for kind, name in chosen if kind == 'hook']
    planned = None
    if hooks or args.statusline:
        try:
            planned = plan_settings(hooks, args.action == 'install', args.statusline)
        except (ValueError, TypeError, AttributeError, KeyError) as err:
            refuse(f'{settings()} cannot be edited ({err!r}); fix it first')

    if args.action == 'remove':
        run_herdr(herdr_steps)
        foreign = [dst for kind, (_, dst) in pairs if kind in ('claude-md', 'statusline') and not ours(dst)]
        for kind, (_, dst) in pairs:  # ownership settled before any unlink: the Codex link is relative
            if dst in foreign:
                print(f'{dst} does not link into this checkout, left alone')
            else:
                unlink(dst)
        if planned:
            write_settings(*planned)
        return

    preflight([pair for _, pair in pairs])
    run_herdr(herdr_steps)
    for _, (src, dst) in pairs:
        link(src, dst)
    for kind in {kind for kind, _ in chosen if kind in KINDS}:
        prune(kind)
    if planned:
        write_settings(*planned)


if __name__ == '__main__':
    main()
