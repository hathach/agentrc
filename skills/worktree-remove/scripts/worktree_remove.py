#!/usr/bin/env python3
"""Remove a herdr worktree, with its clean `<name>--*` siblings, once nothing pending would be lost.

    worktree_remove.py [PATH] [--dry-run] [--pending TEXT]... [--proceed TOKEN]

PATH (default: the current checkout) is a linked worktree open as a herdr workspace. herdr
removes it (`herdr worktree remove`); agentrc's worktree-children plugin, run by herdr on that
removal, removes its clean siblings and deletes the branches it proves pushed or a PR head.
This script acts on nothing the plugin decides: it predicts the plugin's verdicts with the
plugin's own code, gates on pending work, writes a report and asks herdr to remove, last.

Refused (exit 3), nothing changed:
- PATH is a primary checkout or no linked worktree, or herdr lists it open in no workspace
  (`herdr worktree open --path <PATH>` first); herdr unreachable;
- herdr has the plugin unlinked, disabled, or linked from another checkout than this script's;
- PATH fails the plugin's content check: uncommitted, untracked or flagged changes (submodules
  included), an operation in progress, a lock, a nested repository, unsafe submodule storage, a
  detached HEAD on a commit nothing else holds, a running headless chief (so a chief cannot
  remove its own worktree), or a cowork lane holding a request;
- this script, the plugin, the report directory or the plugin's log lies inside PATH or a
  sibling to be removed;
- a git or GitHub lookup the gate needs fails or comes back incomplete.

Pending (exit 4 unless --proceed names the token the list printed), for PATH's branch and each
sibling's, each against its own repository: an open PR; an unresolved review thread on its latest
merged PR; commits on its remote that the remote's default branch lacks, with no PR at that tip; each
sibling and each branch the plugin will keep, with the reason (a never-pushed commit, say);
each --pending item. The token digests the list, so a list that changed since the human saw it
stops again.

Then PATH's content check runs again, the report goes to
${XDG_STATE_HOME:-~/.local/state}/agentrc/worktree-remove/<UTC time>-<name>.md (the inventory,
symlinks leaving each removed tree, the gate, the plugin log, the command), and
`herdr worktree remove --workspace <id>` runs, with --force only for a checkout with submodules
(git refuses those otherwise; herdr then stops the workspace's panes first). herdr closes the
workspace, this script's pane included, so the plugin log holds the outcome.

--dry-run runs every check and lookup (fetching remote-tracking refs) and prints the report
without writing it or removing anything.

Exit 0 removal requested (or a dry run with nothing pending), 2 usage, 3 refused, 4 pending,
5 herdr refused or failed the removal.
"""
import argparse
import hashlib
import importlib.util
import json
import os
import re
import sys
import time
from pathlib import Path

PLUGIN_DIR = Path(__file__).resolve().parents[3] / 'plugins' / 'worktree-children'
PLUGIN_ID = 'agentrc.worktree-children'
STATE = Path(os.environ.get('XDG_STATE_HOME') or Path.home() / '.local' / 'state')
REPORTS = STATE / 'agentrc' / 'worktree-remove'
PLUGIN_LOG = STATE / 'herdr' / 'plugins' / PLUGIN_ID / 'log'
GITHUB = re.compile(r'github\.com[:/]')


class Refused(Exception):
    pass


def load_plugin():
    try:
        spec = importlib.util.spec_from_file_location('worktree_children', PLUGIN_DIR / 'worktree_children.py')
        module = importlib.util.module_from_spec(spec)
        spec.loader.exec_module(module)
        return module
    except Exception as err:  # the verdicts are the plugin's: without it nothing can be predicted
        raise Refused(f'cannot load the worktree-children plugin from {PLUGIN_DIR}: {err!r}')


wc = None


def herdr(*args):
    done = wc.run([wc.HERDR, *args])
    try:
        if done.returncode:
            raise ValueError
        return json.loads(done.stdout)['result']
    except (ValueError, KeyError, TypeError):
        raise Refused(f'`herdr {" ".join(args)}` failed (exit {done.returncode}): {done.stderr.strip()[:300]}')


def workspace(root):
    """The id of the herdr workspace that has root open as a linked worktree."""
    try:
        mine = [w for w in herdr('worktree', 'list', '--cwd', str(root))['worktrees']
                if os.path.realpath(w['path']) == str(root)]
    except (KeyError, TypeError):
        raise Refused('herdr answered `worktree list` without its worktrees')
    if len(mine) != 1 or not mine[0].get('is_linked_worktree') or not mine[0].get('open_workspace_id'):
        raise Refused(f'herdr has {root} open as no linked worktree workspace; open it with '
                      f'`herdr worktree open --path {root}` and rerun')
    return mine[0]['open_workspace_id']


def check_plugin():
    try:
        mine = [p for p in herdr('plugin', 'list', '--json')['plugins'] if p.get('plugin_id') == PLUGIN_ID]
    except (KeyError, TypeError, AttributeError):
        raise Refused('herdr answered `plugin list` without its plugins')
    if not mine or not mine[0].get('enabled'):
        raise Refused(f'herdr has no enabled {PLUGIN_ID}; `install.py install --herdr-plugin` links it')
    if os.path.realpath(mine[0].get('plugin_root') or '') != os.path.realpath(PLUGIN_DIR):
        raise Refused(f'herdr runs {PLUGIN_ID} from {mine[0].get("plugin_root")}, not {PLUGIN_DIR}, '
                      'so its verdicts may differ from these')


def lookup(fn, *args, **kwargs):
    try:
        return fn(*args, **kwargs)
    except wc.Keep as why:
        raise Refused(str(why))


def gate(common, branch, tip):
    """The pending items of branch: its PRs' state on GitHub, or commits on its remote that no
    default branch has. A branch with no remote has nothing there; its keep says why."""
    try:
        remote = wc.branch_remote(common, branch)
    except wc.Keep:
        return []
    default = lookup(wc.refresh, common, remote)
    pushed = not lookup(wc.git, 'rev-list', tip, '--not', f'--remotes={remote}', gitdir=common).strip()
    merged = not wc.run(['git', '--git-dir', common, 'merge-base', '--is-ancestor', tip,
                         f'refs/remotes/{remote}/{default}']).returncode
    url = lookup(wc.git, 'remote', 'get-url', remote, gitdir=common).strip()
    if not GITHUB.search(url):
        return [f'{branch}: commits on {remote} that {default} lacks'] if pushed and not merged else []
    repo = lookup(wc.github_repo, common, remote)
    prs = lookup(wc.pulls, repo, branch)
    items = [f'{branch}: open PR #{pr["number"]}' for pr in prs if pr['state'] == 'OPEN']
    last = max((pr for pr in prs if pr['merged_at']), key=lambda pr: pr['merged_at'], default=None)
    if last:
        items += [f'{branch}: unresolved review thread {url}'
                  for url in lookup(wc.unresolved_threads, repo, last['number'])]
    if pushed and not merged and not any(pr['head'] == tip for pr in prs):
        items.append(f'{branch}: commits on {remote} that {default} lacks, and no PR has them as its head')
    return items


def links_leaving(tree):
    """(link, target) of each symlink under tree whose target lies outside it."""
    links = [os.path.join(top, name) for top, _, _, names in wc.walk(str(tree)) for name in names]
    return [(link, os.readlink(link)) for link in links if not wc.is_within(link, [tree])]  # is_within resolves


def plan(path, pending):
    """Every check and fact the removal needs, changing nothing but fetched remote-tracking refs."""
    global wc
    wc = wc or load_plugin()
    top = wc.run(['git', '-C', str(path), 'rev-parse', '--show-toplevel'])
    if top.returncode:
        raise Refused(f'{path} is in no git checkout: {top.stderr.strip()}')
    root = Path(os.path.realpath(top.stdout.strip()))
    try:
        common, force = wc.content_check(root)
    except (wc.Keep, OSError) as why:
        raise Refused(f'{root}: {why}')
    ws = workspace(root)
    check_plugin()

    siblings = []
    for child in wc.children(root):
        try:
            removal, why = wc.check(child), None
        except (wc.Keep, OSError) as err:
            removal, why = None, str(err)
        done = wc.run(['git', '-C', str(child), 'rev-parse', '--path-format=absolute', '--git-common-dir'])
        siblings.append(dict(path=child, removal=removal, why=why, head=wc.head_branch(child),
                             common=None if done.returncode else os.path.realpath(done.stdout.strip())))
    leaving = [root] + [s['path'] for s in siblings if s['removal']]
    for needed in (Path(__file__).resolve(), PLUGIN_DIR, REPORTS, PLUGIN_LOG):
        if wc.is_within(needed, leaving):
            raise Refused(f'{needed} lies inside a worktree this removes')

    checkouts = [dict(path=root, removal=(common, force), why=None, head=wc.head_branch(root),
                   common=os.path.realpath(common))] + siblings
    items = list(pending)
    for s in checkouts:
        s['branch'] = None
        if s['why']:
            items.append(f'{s["path"].name} stays: {s["why"]}')
        if not s['head'] or not s['common']:
            continue
        name, tip = s['head']
        if s['removal']:
            try:
                s['branch'] = f'branch {name} goes: {wc.branch_proof(s["common"], name, tip, leaving)}'
            except wc.Keep as why:
                s['branch'] = f'branch {name} stays: {why}'
                items.append(f'{s["path"].name}: {s["branch"]}')
        items += gate(s['common'], name, tip)
    links = [l for tree in leaving for l in links_leaving(tree)]
    token = hashlib.sha256('\n'.join(items).encode()).hexdigest()[:12]
    return dict(root=root, workspace=ws, force=force, checkouts=checkouts, links=links, items=items, token=token)


def command(p):
    return [wc.HERDR, 'worktree', 'remove', '--workspace', p['workspace'], *['--force'] * p['force']]


def report(p, heading):
    lines = [f'# {heading}', '',
             f'Removal of {p["root"]} (herdr workspace {p["workspace"]}): removal requested; the plugin log '
             f'has the result: {PLUGIN_LOG}', '', f'Command: `{" ".join(command(p))}`', '', '## Inventory', '']
    for s in p['checkouts']:
        fate = ('removed by herdr' if s['path'] == p['root'] else
                'removed by the plugin' if s['removal'] else f'stays: {s["why"]}')
        lines.append(f'- {s["path"]}: {fate}' + (f'; {s["branch"]}' if s['branch'] else ''))
    lines += ['', '## Symlinks leaving the removed trees (their targets stay)', '']
    lines += [f'- {link} -> {target}' for link, target in p['links']] or ['- none']
    lines += ['', f'## Pending (token {p["token"]})', '']
    lines += [f'- {item}' for item in p['items']] or ['- none']
    return '\n'.join(lines) + '\n'


def main(argv=None):
    ap = argparse.ArgumentParser(description=__doc__, formatter_class=argparse.RawDescriptionHelpFormatter)
    ap.add_argument('path', nargs='?', default='.', help='the worktree to remove (default: the current one)')
    ap.add_argument('--dry-run', action='store_true', help='check and print the report; change nothing')
    ap.add_argument('--pending', action='append', default=[], metavar='TEXT',
                    help='a follow-up from this task neither filed nor done (repeatable)')
    ap.add_argument('--proceed', metavar='TOKEN', help='the human agreed to the pending list printed with TOKEN')
    args = ap.parse_args(argv)
    try:
        p = plan(Path(args.path), args.pending)
    except Refused as e:
        print(f'worktree_remove: refused, nothing changed: {e}', file=sys.stderr)
        return 3
    stamp = time.strftime('%Y%m%dT%H%M%SZ', time.gmtime())
    if args.dry_run:
        print(report(p, f'worktree-remove dry run, {p["root"].name}, {stamp}'), end='')
    if p['items'] and args.proceed != p['token']:
        print(f'pending; nothing changed. Rerun with --proceed {p["token"]} once the human agrees:\n'
              + '\n'.join(f'  - {item}' for item in p['items']))
        return 4
    if args.dry_run:
        return 0
    try:
        wc.content_check(p['root'])
    except (wc.Keep, OSError) as why:
        print(f'worktree_remove: refused, nothing changed: {p["root"]} changed: {why}', file=sys.stderr)
        return 3
    out = REPORTS / f'{stamp}-{p["root"].name}.md'
    out.parent.mkdir(parents=True, exist_ok=True)
    out.write_text(report(p, f'worktree-remove {p["root"].name}, {stamp}'))
    print(f'report: {out}\nrunning: {" ".join(command(p))} (this pane closes with the workspace)', flush=True)
    done = wc.run(command(p))
    if done.returncode:
        print(f'worktree_remove: herdr did not remove {p["root"]} (exit {done.returncode}): '
              f'{(done.stdout + done.stderr).strip()}', file=sys.stderr)
        return 5
    return 0


if __name__ == '__main__':
    sys.exit(main())
