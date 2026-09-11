#!/usr/bin/env python3
"""Link parts of this checkout into ~/.claude and ~/.codex, by explicit choice.

    install.py install --skill cowork --skill read-doc     # named skills
    install.py install --skill all --agent all --hook all --claude-md
    install.py remove --hook simplify-gate

Skills link into ~/.claude/skills and ~/.codex/skills; agents into
~/.claude/agents (the .md) and ~/.codex/agents (the .md and its .toml);
hooks into ~/.claude/hooks, with their hooks.json merged into
~/.claude/settings.json; --claude-md links ~/.claude/CLAUDE.md and
~/.codex/AGENTS.md. Nothing is selected by default. Everything is a symlink,
so edits are live and a rerun after a change is a no-op.

Refuses before touching anything when a destination holds something that is
not a link, or a directory to link into is a file or a dangling link. `remove`
unlinks the named entries whatever they point to but never deletes a real
file or directory; --claude-md is removed only when it links into this
checkout.
"""
import argparse
import json
import os
import shlex
import sys
import tempfile
from pathlib import Path

REPO = Path(__file__).resolve().parent
KINDS = ('skill', 'agent', 'hook')


def available(kind):
    if kind == 'skill':
        return sorted(p.name for p in (REPO / 'skills').iterdir() if p.is_dir())
    if kind == 'agent':
        return sorted(p.stem for p in (REPO / 'agents').glob('*.md'))
    return sorted(p.name for p in (REPO / 'hooks').iterdir() if (p / 'hooks.json').exists())


def dirs(kind):
    """Where entries of a kind are linked to."""
    claude, codex = Path.home() / '.claude', Path.home() / '.codex'
    return {'skill': [claude / 'skills', codex / 'skills'], 'agent': [claude / 'agents', codex / 'agents'],
            'hook': [claude / 'hooks']}[kind]


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
    return [(REPO / 'CLAUDE.md', Path.home() / '.claude' / 'CLAUDE.md'),
            (Path('../.claude/CLAUDE.md'), Path.home() / '.codex' / 'AGENTS.md')]


def ours(path):
    """A symlink whose target resolves inside this checkout."""
    return path.is_symlink() and path.resolve().is_relative_to(REPO)


def refuse(reason):
    print(f'install.py: {reason}', file=sys.stderr)
    sys.exit(1)


def preflight(pairs):
    """Every destination must be absent, a symlink, an empty directory, or one
    of our skills seen through the whole-dir link an earlier installer made;
    every directory linked into must be a real directory or absent."""
    for _, dst in pairs:
        parent = dst.parent
        if (parent.exists() or parent.is_symlink()) and not parent.is_dir():
            refuse(f'{parent} is not a directory; move it aside first')
        if dst.is_symlink() or not dst.exists():
            continue
        if dst.is_dir() and not any(dst.iterdir()):
            continue
        if parent.resolve() == REPO / 'skills':
            continue
        refuse(f'{dst} exists and is not a symlink; move it aside first')


def link(src, dst):
    if dst.is_symlink() and os.readlink(dst) == str(src):
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
        os.symlink(src, dst)  # never overwrites
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
    """Drop our links whose source left the repo; leave every other link alone."""
    source = REPO / {'skill': 'skills', 'agent': 'agents', 'hook': 'hooks'}[kind]
    for parent in dirs(kind):
        for path in parent.iterdir() if parent.is_dir() else ():
            if not path.is_symlink() or path.exists():
                continue
            if Path(os.path.normpath(parent / os.readlink(path))).is_relative_to(source):
                unlink(path)


# --- hook registration in settings.json --------------------------------------

def hook_entry(hook, name):
    """Is this settings entry ours: the command runs out of the linked hook
    directory, or out of this repo's hooks (an older install)?"""
    try:
        words = shlex.split(hook.get('command', ''))
    except ValueError:
        return False
    dirs = (str(Path.home() / '.claude' / 'hooks' / name) + '/', str(REPO / 'hooks') + '/')
    return any(word.startswith(dirs) for word in words)


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


def plan_settings(names, install):
    """(before, after) texts for registering or unregistering these hooks, or
    None when nothing would change. Computed before any link moves, so a
    settings file of the wrong shape refuses instead of leaving a half job."""
    path = settings()
    before = path.read_text() if path.exists() else ''
    data = json.loads(before) if before else {}
    hooks = data.setdefault('hooks', {})
    for name in names:
        strip(hooks, name)
        if install:
            linked = Path.home() / '.claude' / 'hooks' / name
            for event, groups in json.loads((REPO / 'hooks' / name / 'hooks.json').read_text()).items():
                for group in groups:
                    entries = [{**h, 'command': shlex.quote(str(linked / h['command']))} for h in group['hooks']]
                    hooks.setdefault(event, []).append({**group, 'hooks': entries})
    if not hooks:
        del data['hooks']
    if data == (json.loads(before) if before else {}):
        return None
    return before, json.dumps(data, indent=2) + '\n'


def write_settings(before, after):
    path = settings()
    backup = path.with_name(path.name + '.before-agentrc')
    if before and not backup.exists():
        backup.write_text(before)
    path.parent.mkdir(parents=True, exist_ok=True)
    path.write_text(after)
    print(f'updated {path}')


# --- command line -------------------------------------------------------------

def selection(parser, args):
    chosen = []
    for kind in KINDS:
        names = getattr(args, kind)
        if not names:
            continue
        have = available(kind)
        if 'all' in names:
            if names != ['all']:
                parser.error(f'--{kind} all cannot be combined with names')
            names = have
        for name in names:
            if name not in have:
                parser.error(f'no {kind} named {name}; available: {", ".join(have) or "none"}')
        chosen += [(kind, name) for name in names]
    if args.claude_md:
        chosen.append(('claude-md', None))
    if not chosen:
        parser.error('nothing selected')
    return chosen


def main(argv=None):
    parser = argparse.ArgumentParser(description=__doc__, formatter_class=argparse.RawDescriptionHelpFormatter)
    parser.add_argument('action', choices=('install', 'remove'))
    for kind in KINDS:
        parser.add_argument(f'--{kind}', action='append', metavar='NAME|all', default=[])
    parser.add_argument('--claude-md', action='store_true', help='CLAUDE.md, also as ~/.codex/AGENTS.md')
    args = parser.parse_args(argv)
    chosen = selection(parser, args)
    pairs = [(kind, pair) for kind, name in chosen for pair in links(kind, name)]
    hooks = [name for kind, name in chosen if kind == 'hook']
    planned = None
    if hooks:
        try:
            planned = plan_settings(hooks, args.action == 'install')
        except (ValueError, TypeError, AttributeError, KeyError) as err:
            refuse(f'{settings()} cannot be edited ({err!r}); fix it first')

    if args.action == 'remove':
        foreign = [dst for kind, (_, dst) in pairs if kind == 'claude-md' and not ours(dst)]
        for kind, (_, dst) in pairs:  # ownership settled before any unlink: the Codex link is relative
            if dst in foreign:
                print(f'{dst} does not link into this checkout, left alone')
            else:
                unlink(dst)
        if planned:
            write_settings(*planned)
        return

    preflight([pair for _, pair in pairs])
    if any(kind == 'skill' for kind, _ in chosen):
        for d in dirs('skill'):
            if d.is_symlink() and d.resolve() == REPO / 'skills':
                d.unlink()  # the whole-dir link an earlier installer made
    for _, (src, dst) in pairs:
        link(src, dst)
    for kind in {kind for kind, _ in chosen if kind in KINDS}:
        prune(kind)
    if planned:
        write_settings(*planned)


if __name__ == '__main__':
    main()
