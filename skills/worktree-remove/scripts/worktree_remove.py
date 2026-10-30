#!/usr/bin/env python3
"""Remove a linked worktree, with its clean `<name>--*` siblings, once nothing pending would be lost.

    worktree_remove.py [PATH] [--dry-run] [--pending TEXT]... [--proceed TOKEN]

PATH (default: the current checkout) is a linked worktree. This script removes it (`git worktree
remove`), then runs worktree_children's cleanup of PATH, which removes its clean siblings and deletes
the branches it proves pushed or a PR head. The verdicts are that module's: this script predicts them
with its code, gates on pending work, writes a report and removes, last.

Refused (exit 3), nothing changed:
- PATH is a primary checkout or no linked worktree;
- the working directory, this script, the module, the report directory or the log lies inside, or
  is reached through, PATH or any sibling, a kept one included (run it from outside them, naming
  PATH): git cannot close the sessions in it; checked before anything else;
- PATH fails the module's content check, the one each sibling gets (its docstring lists them);
  a chief session in PATH, headless or interactive and the caller's own included, refuses it: a
  chief cannot remove its own worktree, so the human runs this from a plain session;
- a git or GitHub lookup the gate needs fails or comes back incomplete.

Pending (exit 4 unless --proceed names the token the list printed), for PATH's branch and each
sibling's, each against its own repository (its PRs from that repository's branch of that name, a
fork's to its parent included): an open PR; an unresolved review thread on its latest merged PR;
commits on its remote that the remote's default branch lacks, with no PR at that tip; each sibling
and each branch the cleanup will keep, with the reason (a never-pushed commit, say); each --pending
item. The token digests the list, so a list that changed since the human saw it stops again.

Then PATH's content check runs again, refused (exit 3) also when its branch or tip moved; the report
goes to ${XDG_STATE_HOME:-~/.local/state}/agentrc/worktree-remove/<UTC time>-<name>.md (the
inventory, symlinks leaving each removed tree, the gate, the log, the command), and
`git worktree remove <PATH>` runs, with --force only for a checkout with submodules (git refuses
those otherwise), then the cleanup of PATH with PATH's branch, which prints its actions and appends
them to the log beside the reports. A process whose working directory is inside PATH can make git
fail part way (Windows refuses to delete it); then the siblings and branches are left alone.

--dry-run runs every check and lookup (fetching remote-tracking refs) and prints the report
without writing it or removing anything.

Exit 0 removed with every sibling and branch the plan let go (or a dry run with nothing pending),
2 usage, 3 refused, 4 pending, 5 git refused or failed the removal, 6 removed but the cleanup kept
a sibling or branch, or stopped at a replacement parent (the log has why and the retry).
"""
import argparse
import hashlib
import importlib.util
import json
import os
import sys
import time
from pathlib import Path

MODULE = Path(__file__).resolve().parents[3] / 'lib' / 'worktree_children.py'
THREADS = '''query($owner: String!, $name: String!, $number: Int!, $endCursor: String) {
  repository(owner: $owner, name: $name) { pullRequest(number: $number) {
    reviewThreads(first: 100, after: $endCursor) {
      nodes { isResolved comments(first: 1) { nodes { url } } }
      pageInfo { hasNextPage endCursor } } } } }'''


class Refused(Exception):
    pass


def load_module():
    try:
        spec = importlib.util.spec_from_file_location('worktree_children', MODULE)
        module = importlib.util.module_from_spec(spec)
        spec.loader.exec_module(module)
        return module
    except Exception as err:  # the verdicts are the module's: without it nothing can be predicted
        raise Refused(f'cannot load worktree_children from {MODULE}: {err!r}')


wc = None


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
    wc = wc or load_module()
    top = wc.run(['git', '-C', str(path), 'rev-parse', '--show-toplevel'])
    if top.returncode:
        raise Refused(f'{path} is in no git checkout: {top.stderr.strip()}')
    root = Path(os.path.realpath(top.stdout.strip()))
    found = wc.children(root)
    # any sibling: the cleanup may find a kept one clean
    why = wc.outside([root, *found], [('this script', Path(__file__).resolve()), ('the reports', wc.LOG.parent)])
    if why:
        raise Refused(why)
    try:
        common, force = wc.content_check(root)
    except (wc.Keep, OSError) as why:
        raise Refused(f'{root}: {why}')

    siblings = []
    for child in found:
        try:
            wc.content_check(child)
            why = None
        except (wc.Keep, OSError) as err:
            why = str(err)
        siblings.append(dict(path=child, why=why, head=wc.head_branch(child), common=wc.common_dir(child / '.git')))
    _, held = wc.removal_plan([s['path'] for s in siblings if s['why'] is None],
                              [s['path'] for s in siblings if s['why']])
    for s in siblings:
        s['why'] = s['why'] or held.get(s['path'])
    leaving = [root] + [s['path'] for s in siblings if s['why'] is None]
    head, common = wc.head_branch(root), os.path.realpath(common)
    checkouts = [dict(path=root, why=None, head=head, common=common)] + siblings
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
    return dict(root=root, common=common, head=head, force=force, checkouts=checkouts, links=links, items=items,
                token=token)


def command(p):
    return wc.remove_argv(p['root'], p['common'], p['force'])


def report(p, heading):
    lines = [f'# {heading}', '',
             f'Plan to remove {p["root"]}; the log records what the removal does: {wc.LOG}', '',
             f'Command: `{" ".join(command(p))}`, then the cleanup of its siblings and branches', '',
             '## Inventory', '']
    for s in p['checkouts']:
        fate = ('git removes it' if s['path'] == p['root'] else
                'the cleanup removes it' if s['why'] is None else f'stays: {s["why"]}')
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
        if wc.head_branch(p['root']) != p['head']:
            raise wc.Keep('its branch or tip moved since the gate')
    except (wc.Keep, OSError) as why:
        print(f'worktree_remove: refused, nothing changed: {p["root"]} changed: {why}', file=sys.stderr)
        return 3
    out = wc.LOG.parent / f'{stamp}-{p["root"].name}.md'
    out.parent.mkdir(parents=True, exist_ok=True)
    out.write_bytes(wc.stored(report(p, f'worktree-remove {p["root"].name}, {stamp}')))
    print(f'report: {out}', flush=True)
    return remove(p)


def remove(p):
    """git's removal of the worktree, then the cleanup of its siblings and branches."""
    root, common, head = p['root'], p['common'], p['head']
    wc.log(f'running: {" ".join(command(p))}')
    try:
        wc.remove(root, common, p['force'])
    except wc.Keep as why:
        wc.log(f'git did not remove {root}; siblings and branches untouched')
        print(f'worktree_remove: {root}: {why}', file=sys.stderr)
        return 5
    wc.log(f'removed {root}')
    left = wc.cleanup(root, [(common, *head)] if head else [])
    if left == 2:
        print(f'worktree_remove: removed {root}, but something appeared at that path again, so its siblings '
              f'and branches were left alone; the log has the retry: {wc.LOG}', file=sys.stderr)
    elif left:
        print(f'worktree_remove: removed {root}, but the cleanup kept a sibling or branch; the log has why '
              f'and the retry: {wc.LOG}', file=sys.stderr)
    return 6 if left else 0


if __name__ == '__main__':
    sys.stdout.reconfigure(errors='backslashreplace')  # a narrow code page must not stop a removal part way
    sys.exit(main())
