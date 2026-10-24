#!/usr/bin/env python3
"""Remove a linked worktree, with its clean `<name>--*` siblings, once nothing pending would be lost.

    worktree_remove.py [PATH] [--dry-run] [--pending TEXT]... [--proceed TOKEN]

PATH (default: the current checkout) is a linked worktree. When herdr has it open as a workspace,
herdr removes it (`herdr worktree remove`) and agentrc's worktree-children plugin, run by herdr on
that removal, removes its clean siblings and deletes the branches it proves pushed or a PR head.
Otherwise, with no herdr installed too, this script removes it (`git worktree remove`) and then
runs that plugin's cleanup itself, as the event would. Either way the verdicts are the plugin's:
this script predicts them with the plugin's own code, gates on pending work, writes a report and
removes, last.

Refused (exit 3), nothing changed:
- PATH is a primary checkout or no linked worktree; herdr found but unreachable, or HERDR_ENV set
  with no herdr found (whether herdr has PATH open is unknown);
- open in herdr: herdr has the plugin unlinked, disabled, or linked from another checkout than
  this script's;
- open in no herdr workspace: the working directory lies inside PATH or a sibling to be removed
  (run it from outside, naming PATH);
- PATH fails the plugin's content check, the one each sibling gets (its docstring lists them);
  a chief session in PATH, headless or interactive and the caller's own included, refuses it: a
  chief cannot remove its own worktree, so the human runs this from a plain session;
- this script, the plugin, the report directory or the log lies inside PATH or a sibling to be
  removed;
- a git or GitHub lookup the gate needs fails or comes back incomplete.

Pending (exit 4 unless --proceed names the token the list printed), for PATH's branch and each
sibling's, each against its own repository (its PRs from that repository's branch of that name, a
fork's to its parent included): an open PR; an unresolved review thread on its latest merged PR;
commits on its remote that the remote's default branch lacks, with no PR at that tip; each sibling
and each branch the plugin will keep, with the reason (a never-pushed commit, say); each --pending
item. The token digests the list, so a list that changed since the human saw it stops again.

Then PATH's content check runs again; for PATH open in no herdr workspace, so is herdr's (refused,
exit 3, when a workspace opened PATH during the lookups or herdr cannot say); the report goes to
${XDG_STATE_HOME:-~/.local/state}/agentrc/worktree-remove/<UTC time>-<name>.md (the inventory,
symlinks leaving each removed tree, the gate, the log, the command), and the removal runs, with
--force only for a checkout with submodules (git refuses those otherwise):
- open in herdr: `herdr worktree remove --workspace <id>` (herdr then stops the workspace's panes
  first). herdr closes the workspace, this script's pane included, so the plugin log holds the
  outcome.
- otherwise: `git worktree remove <PATH>`, then the plugin's cleanup of PATH with PATH's branch,
  which prints its actions and appends them to the log beside the reports. A process whose
  working directory is inside PATH can make git fail part way (Windows refuses to delete it).

--dry-run runs every check and lookup (fetching remote-tracking refs) and prints the report
without writing it or removing anything.

Exit 0 removal requested or done (or a dry run with nothing pending), 2 usage, 3 refused,
4 pending, 5 herdr or git refused or failed the removal.
"""
import argparse
import hashlib
import importlib.util
import json
import os
import sys
import time
from pathlib import Path

PLUGIN_DIR = Path(__file__).resolve().parents[3] / 'plugins' / 'worktree-children'
PLUGIN_ID = 'agentrc.worktree-children'
STATE = Path(os.environ.get('XDG_STATE_HOME') or Path.home() / '.local' / 'state')
REPORTS = STATE / 'agentrc' / 'worktree-remove'
LOG = REPORTS / 'log'  # the plugin's cleanup, run here when no herdr removes the worktree
PLUGIN_LOG = STATE / 'herdr' / 'plugins' / PLUGIN_ID / 'log'
THREADS = '''query($owner: String!, $name: String!, $number: Int!, $endCursor: String) {
  repository(owner: $owner, name: $name) { pullRequest(number: $number) {
    reviewThreads(first: 100, after: $endCursor) {
      nodes { isResolved comments(first: 1) { nodes { url } } }
      pageInfo { hasNextPage endCursor } } } } }'''


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
    """The id of the herdr workspace that has root open, None when none has or there is no herdr."""
    try:
        return wc.herdr_found() and wc.herdr_entry(root).get('open_workspace_id') or None
    except wc.Keep as why:
        raise Refused(f'herdr and {root}: {why}')


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


def unresolved_threads(repo, number):
    owner, name = repo.split('/')
    return wc.gh_lines('api', 'graphql', '--paginate', '-f', f'query={THREADS}', '-F', f'owner={owner}',
                       '-F', f'name={name}', '-F', f'number={number}', '--jq',
                       '.data.repository.pullRequest.reviewThreads.nodes[] | select(.isResolved | not)'
                       ' | .comments.nodes[0].url')


def fork_parent(repo):
    """owner/name of the repository repo is a fork of, None for no fork; Keep when gh cannot tell."""
    lines = wc.gh_lines('repo', 'view', repo, '--json', 'isFork,parent')
    try:
        view = json.loads(''.join(lines))
        return f'{view["parent"]["owner"]["login"]}/{view["parent"]["name"]}' if view['isFork'] else None
    except (ValueError, KeyError, TypeError):
        raise wc.Keep(f'gh names no parent for {repo}: {lines}')


def gate(common, branch, tip):
    """The pending items of branch: its PRs' state on GitHub, or commits on its remote that no
    default branch has. A branch with no remote has nothing there; its keep says why."""
    try:
        remote = wc.branch_remote(common, branch)
    except wc.Keep:
        return []
    try:
        default = wc.refresh(common, remote)
        pushed = not wc.git('rev-list', tip, '--not', f'--remotes={remote}', gitdir=common).strip()
        merged = not wc.run(['git', '--git-dir', common, 'merge-base', '--is-ancestor', tip,
                             f'refs/remotes/{remote}/{default}']).returncode
        repo = wc.github_repo(common, remote)
        if not repo:
            return [f'{branch}: commits on {remote} that {default} lacks'] if pushed and not merged else []
        owner = repo.split('/')[0]  # the search spans forks; a same-named branch of another is not ours
        prs = [dict(pr, repo=repo, ref=f'#{pr["number"]}') for pr in wc.pulls(repo, branch, owner=owner)]
        parent = fork_parent(repo)
        if parent:  # a fork's PRs to its parent live in the parent
            prs += [dict(pr, repo=parent, ref=f'{parent}#{pr["number"]}')
                    for pr in wc.pulls(parent, branch, owner=owner)]
        items = [f'{branch}: open PR {pr["ref"]}' for pr in prs if pr['state'] == 'OPEN']
        last = max((pr for pr in prs if pr['merged_at']), key=lambda pr: pr['merged_at'], default=None)
        if last:
            items += [f'{branch}: unresolved review thread {url}'
                      for url in unresolved_threads(last['repo'], last['number'])]
    except wc.Keep as why:
        raise Refused(str(why))
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
    if ws:
        check_plugin()

    siblings = []
    for child in wc.children(root):
        try:
            wc.check(child)
            why = None
        except (wc.Keep, OSError) as err:
            why = str(err)
        siblings.append(dict(path=child, why=why, head=wc.head_branch(child), common=wc.common_dir(child / '.git')))
    _, held = wc.removal_plan([s['path'] for s in siblings if s['why'] is None],
                              [s['path'] for s in siblings if s['why']])
    for s in siblings:
        s['why'] = s['why'] or held.get(s['path'])
    leaving = [root] + [s['path'] for s in siblings if s['why'] is None]
    log = PLUGIN_LOG if ws else LOG
    for needed in (Path(__file__).resolve(), PLUGIN_DIR, REPORTS, log):
        if wc.is_within(needed, leaving):
            raise Refused(f'{needed} lies inside a worktree this removes')
    if not ws and wc.is_within(os.getcwd(), leaving):  # herdr closes the panes in it; git cannot
        raise Refused(f'the working directory {os.getcwd()} lies inside a worktree this removes; '
                      'run this from outside it, naming the worktree')

    checkouts = [dict(path=root, why=None, head=wc.head_branch(root), common=os.path.realpath(common))] + siblings
    items = list(pending)
    for s in checkouts:
        s['branch'] = None
        if s['why']:
            items.append(f'{s["path"].name} stays: {s["why"]}')
        if not s['head'] or not s['common']:
            continue
        name, tip = s['head']
        if s['why'] is None:
            try:
                s['branch'] = f'branch {name} goes: {wc.branch_proof(s["common"], name, tip, leaving)}'
            except wc.Keep as why:
                s['branch'] = f'branch {name} stays: {why}'
                items.append(f'{s["path"].name}: {s["branch"]}')
        items += gate(s['common'], name, tip)
    try:
        links = [l for tree in leaving for l in links_leaving(tree)]
    except (wc.Keep, OSError) as why:  # a tree changed since its check
        raise Refused(f'listing the symlinks leaving the removed trees: {why}')
    token = hashlib.sha256('\n'.join(items).encode()).hexdigest()[:12]
    return dict(root=root, workspace=ws, log=log, force=force, checkouts=checkouts, links=links, items=items,
                token=token)


def command(p):
    if p['workspace']:
        return [wc.HERDR, 'worktree', 'remove', '--workspace', p['workspace'], *['--force'] * p['force']]
    return ['git', '--git-dir', p['checkouts'][0]['common'], 'worktree', 'remove', *['--force'] * p['force'],
            str(p['root'])]


def report(p, heading):
    where = f'herdr workspace {p["workspace"]}' if p['workspace'] else 'open in no herdr workspace'
    then = '' if p['workspace'] else ", then the plugin's cleanup"
    lines = [f'# {heading}', '',
             f'Plan to remove {p["root"]} ({where}); the log records what the removal does: {p["log"]}', '',
             f'Command: `{" ".join(command(p))}`{then}', '', '## Inventory', '']
    for s in p['checkouts']:
        fate = (('herdr' if p['workspace'] else 'git') + ' removes it' if s['path'] == p['root'] else
                'the plugin removes it' if s['why'] is None else f'stays: {s["why"]}')
        lines.append(f'- {s["path"]}: {fate}' + (f'; {s["branch"]}' if s['branch'] else ''))
    lines += ['', '## Symlinks leaving the removed trees', '']
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
    if not p['workspace']:  # git would bypass a workspace herdr opened during the lookups, and its panes
        try:
            ws = workspace(p['root'])
        except Refused as e:
            ws, why = None, e
        else:
            why = ws and f'{p["root"]} is now open in herdr workspace {ws}'
        if why:
            print(f'worktree_remove: refused, nothing changed: {why}', file=sys.stderr)
            return 3
    out = REPORTS / f'{stamp}-{p["root"].name}.md'
    out.parent.mkdir(parents=True, exist_ok=True)
    out.write_text(report(p, f'worktree-remove {p["root"].name}, {stamp}'))
    if not p['workspace']:
        print(f'report: {out}', flush=True)
        return remove_here(p)
    print(f'report: {out}\nrunning: {" ".join(command(p))} (this pane closes with the workspace)', flush=True)
    done = wc.run(command(p))
    if done.returncode:
        print(f'worktree_remove: herdr did not remove {p["root"]} (exit {done.returncode}): '
              f'{(done.stdout + done.stderr).strip()}', file=sys.stderr)
        return 5
    return 0


def remove_here(p):
    """What herdr's removal and the plugin's worktree.removed event do, for a worktree open in no herdr workspace."""
    wc.LOG = p['log']
    root, common, head = p['root'], p['checkouts'][0]['common'], p['checkouts'][0]['head']
    wc.log(f'running: {" ".join(command(p))}')
    done = wc.run(command(p))
    if done.returncode:
        wc.log(f'git did not remove {root} (exit {done.returncode}), possibly part way: {done.stderr.strip()}')
        return 5
    wc.log(f'removed {root}')
    wc.cleanup(root, [(common, *head)] if head else [])
    return 0


if __name__ == '__main__':
    sys.exit(main())
