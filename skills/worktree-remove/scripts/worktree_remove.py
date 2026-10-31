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
- a live herdr-core terminal works in PATH or any sibling, the caller's own included, each named with
  its herdr-app space (close them first), or an installed `hc` cannot list them all; checked next, with
  no hc installed not at all (the report says so). Run from an hc terminal instead, proved by
  HC_TERMINAL_ID naming one whose program is an ancestor of this process (Linux), it lists them to
  close: the caller's own goes with the removal, another session's is a pending item bound to its pid
  and creation time; their processes are spared the content check until the worker (below) ends them;
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

Then PATH's content check and its terminal check run again, refused (exit 3) also when its branch or
tip moved; the report goes to
${XDG_STATE_HOME:-~/.local/state}/agentrc/worktree-remove/<UTC time>-<name>.md (the inventory, symlinks
leaving each removed tree, the gate, the log, the command), and
`git worktree remove <PATH>` runs, with --force only for a checkout with submodules (git refuses
those otherwise), then the cleanup of PATH with PATH's branch, which prints its actions and appends
them to the log beside the reports. The content check refuses a tree a process of this user works in
on Linux only: elsewhere such a process can make git fail part way (Windows refuses to delete its
directory); then the siblings and branches are left alone.

With terminals to close, the removal goes to a worker started as its own hc terminal (`hc run --bg
--no-parent --rm`, named wtrm-<op>), which outlives the caller's. One removal per worktree, however its
path is spelled, is admitted (`handoff/<digest>.json` beside the reports): another is refused until the
worker ends, or until its worker terminal and its caller are both gone, never on time alone. The caller
writes the worker an immutable plan (the log, hc and each terminal's identity, nothing taken from the
core's environment), waits up to READY_S for its ready, answers go and returns 0 having printed the
handoff; a worker without that answer within ACK_S closes nothing. The worker checks every terminal still
runs the program planned, then kills each, the caller's own last, after checking it again (hc kills by
address: a program swapped in between that check and the kill goes too), waits up to END_S for them and
their processes to end, runs PATH's content and terminal checks again and removes as above, its cleanup
limited to the siblings the plan let go, logging each step: its terminal goes when it ends.

--dry-run runs every check and lookup (fetching remote-tracking refs) and prints the report
without writing it or removing anything.

Exit 0 removed with every sibling and branch the plan let go, handed to the worker (the log has the outcome),
or a dry run with nothing pending,
2 usage, 3 refused, 4 pending, 5 git refused or failed the removal, 6 removed but the cleanup kept
a sibling or branch, or stopped at a replacement parent (the log has why and the retry).
"""
import argparse
import contextlib
import hashlib
import importlib.util
import json
import os
import secrets
import sys
import time
from pathlib import Path

MODULE = Path(__file__).resolve().with_name('worktree_children.py')
THREADS = '''query($owner: String!, $name: String!, $number: Int!, $endCursor: String) {
  repository(owner: $owner, name: $name) { pullRequest(number: $number) {
    reviewThreads(first: 100, after: $endCursor) {
      nodes { isResolved comments(first: 1) { nodes { url } } }
      pageInfo { hasNextPage endCursor } } } } }'''


READY_S, ACK_S, END_S = 30, 20, 30  # s the worker may take to start, the caller to answer it, the terminals to end


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
    hc = wc.hc_path()
    try:  # any sibling, as above
        closing = list({t['address']: t for ts in wc.terminals_in([root, *found]).values() for t in ts}.values())
        caller = caller_terminal(hc) if closing else None
        if closing and not caller:
            raise wc.Keep(wc.busy_with(closing))
        closing = [t for t in (identified(hc, t) for t in closing) if t]
    except wc.Keep as why:
        raise Refused(str(why))
    spared = frozenset(wc.descendants([t['pid'] for t in closing])) if closing else frozenset()
    try:
        common, force = wc.content_check(root, spared)
    except (wc.Keep, OSError) as why:
        raise Refused(f'{root}: {why}')

    _, _, staying = wc.plan_children(found, {}, spared=spared)  # a busy sibling is closing or refused above
    siblings = [dict(path=child, why=staying.get(child), head=wc.head_branch(child), common=wc.common_dir(child / '.git'))
                for child in found]
    leaving = [root] + [s['path'] for s in siblings if s['why'] is None]
    head, common = wc.head_branch(root), os.path.realpath(common)
    checkouts = [dict(path=root, why=None, head=head, common=common)] + siblings
    items = list(pending) + [f'closes terminal {wc.label(t)}: pid {t["pid"]}, created {t["created_ms"]}'
                             for t in closing if t['address'] != caller['address']]
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
                token=token, hc=hc, procs=wc.lists_processes(), closing=closing, caller=caller and caller['address'])


def caller_terminal(hc):
    """The live hc terminal HC_TERMINAL_ID names when its program is an ancestor of this process, as `hc get`
    gives it; else None: the variable alone is inherited, not proof, and without /proc nothing proves it."""
    address = os.environ.get('HC_TERMINAL_ID')
    if not (hc and address and wc.lists_processes()):
        return None
    got = wc.hc_json(hc, 'get', address)
    if not got or got.get('state') != 'live' or not isinstance(got.get('pid'), int):
        return None
    pid = os.getpid()
    while pid > 1:
        try:
            pid = wc.parent_pid(pid)
        except (OSError, IndexError, ValueError):
            return None
        if pid == got['pid']:
            return got
    return None


def identified(hc, t):
    """Terminal t with the pid and creation time `hc get` gives it now, which bind it; None once it ended."""
    got = wc.hc_json(hc, 'get', t['address'])
    if got is None or got.get('state') != 'live':
        return None
    if not (isinstance(got.get('pid'), int) and isinstance(got.get('created_ms'), int)):
        raise wc.Keep(f'cannot identify herdr-core terminal {t["address"]}: `hc get` gave no pid or created_ms')
    return dict(t, pid=got['pid'], created_ms=got['created_ms'])


def unplanned(p, trees):
    """The terminals working in trees now that p does not close as it found them."""
    planned = {(t['address'], t['pid'], t['created_ms']) for t in p['closing']}
    now = (identified(p['hc'], t) for ts in wc.terminals_in(trees).values() for t in ts)
    return [t for t in now if t and (t['address'], t['pid'], t['created_ms']) not in planned]


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
    lines += [f'- closes terminal {wc.label(t)}' + (" (this session's own)" if t['address'] == p['caller'] else '')
              for t in p['closing']]
    if not p['hc']:
        lines.append('- herdr-core terminals working in them: not checked, no hc installed')
    if not p['procs']:
        lines.append('- other processes working in them: not checked, no /proc on this system')
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
    ap.add_argument('--worker', metavar='PLAN', help=argparse.SUPPRESS)
    args = ap.parse_args(argv)
    if args.worker:
        global wc
        wc = wc or load_module()
        return worker(args.worker)
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
        closing = p['closing'] and [t for t in (identified(p['hc'], t) for t in p['closing']) if t]
        wc.content_check(p['root'], frozenset(wc.descendants([t['pid'] for t in closing])) if closing else frozenset())
        if wc.head_branch(p['root']) != p['head']:
            raise wc.Keep('its branch or tip moved since the gate')
        extra = unplanned(p, [p['root']]) if p['closing'] else None  # again: the content check can take long
        if extra:
            raise wc.Keep(wc.busy_with(extra))
        if not p['closing']:
            wc.check_terminals(p['root'])
    except (wc.Keep, OSError) as why:
        print(f'worktree_remove: refused, nothing changed: {p["root"]} changed: {why}', file=sys.stderr)
        return 3
    out = wc.LOG.parent / f'{stamp}-{p["root"].name}.md'
    out.parent.mkdir(parents=True, exist_ok=True)
    out.write_bytes(wc.stored(report(p, f'worktree-remove {p["root"].name}, {stamp}')))
    print(f'report: {out}', flush=True)
    return handoff(p, out) if p['closing'] else remove(p)


def created(path, text):
    """Whether this call created path holding text, whole from its first appearance; False when it exists
    already. A hard link publishes it: it never replaces another's file, nor shows a half-written one."""
    draft = f'{path}.{os.getpid()}.{secrets.token_hex(4)}'
    with open(draft, 'x', encoding='utf-8') as f:
        f.write(text)
    try:
        os.link(draft, path)
        return True
    except FileExistsError:
        return False
    finally:
        os.unlink(draft)


@contextlib.contextmanager
def admitting(admission):
    """Serialize every change to the admissions: taking one, releasing a stale one, releasing one's own."""
    import fcntl  # the handoff needs /proc, so Linux
    with open(admission.parent / 'admissions.lock', 'a') as lock:
        fcntl.flock(lock, fcntl.LOCK_EX)
        yield


def admission_of(root):
    """The file whose existence admits one removal of root, however its path was spelled."""
    return wc.LOG.parent / 'handoff' / f'{hashlib.sha256(os.path.realpath(root).encode()).hexdigest()[:16]}.json'


def admit(admission, record):
    """Hold admission for record's removal; Refused while another removal of the same tree may still run.
    A record is released only when its worker terminal is gone and its caller too, never on time alone."""
    with admitting(admission):
        if created(admission, json.dumps(record)):
            return
        try:
            held = json.loads(admission.read_text(encoding='utf-8'))
            worker = wc.hc_json(record['hc'], 'get', held['worker'])
            gone = (worker is None or worker.get('state') != 'live') and not (wc.PROC / str(held['caller'])).exists()
        except (OSError, ValueError, KeyError, TypeError, wc.Keep) as why:
            raise Refused(f'another removal of {record["root"]} holds {admission}, unreadable ({why}); '
                          'remove it once no worktree-remove worker runs')
        if not gone:
            raise Refused(f'another removal of {record["root"]} runs (op {held["op"]}, worker terminal '
                          f'{held["worker"]}); it releases {admission} when it ends')
        admission.unlink()
        wc.log(f'released the stale admission of op {held["op"]} for {record["root"]}: its worker and caller are gone')
        if not created(admission, json.dumps(record)):
            raise Refused(f'another removal of {record["root"]} took {admission} meanwhile')


def release(admission, op):
    with admitting(admission):
        try:
            if json.loads(admission.read_text(encoding='utf-8')).get('op') == op:
                admission.unlink()
        except (OSError, ValueError):
            pass


def handoff(p, out):
    """Start a worker in its own hc terminal that closes p's terminals, this run's own among them, and then
    removes: nothing outlives the terminal it closes. The worker kills nothing until this run answers its
    ready, so this run reports the handoff first."""
    hc, op = p['hc'], secrets.token_hex(6)
    admission, name = admission_of(p['root']), f'wtrm-{op}'
    admission.parent.mkdir(parents=True, exist_ok=True)
    files = {k: str(admission.parent / f'{op}.{k}') for k in ('plan', 'ready', 'ack')}
    try:
        admit(admission, dict(op=op, root=str(p['root']), worker=name, caller=os.getpid(), hc=hc))
    except Refused as why:
        print(f'worktree_remove: refused, nothing changed: {why}', file=sys.stderr)
        return 3
    created(files['plan'], json.dumps(dict(
        op=op, admission=str(admission), root=str(p['root']), common=p['common'], head=p['head'], force=p['force'],
        closing=sorted(p['closing'], key=lambda t: t['address'] == p['caller']), siblings=[str(s['path']) for s in p['checkouts'][1:] if s['why'] is None],
        hc=hc, log=str(wc.LOG), report=str(out), **files)))
    launch = wc.run([hc, 'run', '--bg', '--no-parent', '--no-notify', '--rm', '--cwd', str(admission.parent),
                     '--name', name, '--label', f'agentrc.worktree-remove={p["root"]}', '--',
                     sys.executable, str(Path(__file__).resolve()), '--worker', files['plan']], timeout=wc.HC_TIMEOUT)
    deadline = time.monotonic() + READY_S
    while not launch.returncode and not os.path.exists(files['ready']) and time.monotonic() < deadline:
        time.sleep(0.1)
    if not launch.returncode and os.path.exists(files['ready']) and created(files['ack'], 'go'):
        wc.log(f'handed the removal of {p["root"]} to worker terminal {name} (op {op}): it closes '
               + ', '.join(t['address'] for t in p['closing']))
        print(f'worktree_remove: handed off to hc terminal {name}, which closes '
              + ', '.join(wc.label(t) for t in p['closing']) + f', then removes {p["root"]}; the log has the '
              f'outcome: {wc.LOG}', flush=True)
        return 0
    created(files['ack'], 'withdrawn')  # a worker starting late reads it and closes nothing
    wc.run([hc, 'kill', name], timeout=wc.HC_TIMEOUT)
    why = (f'`hc run` exit {launch.returncode}: {(launch.stdout + launch.stderr).strip()[:300]}' if launch.returncode
           else f'its worker {name} did not answer in {READY_S} s' if not os.path.exists(files['ready'])
           else f'its worker {name} gave up waiting')
    try:
        if wc.hc_json(hc, 'get', name) is None:  # no worker can read them now
            release(admission, op)
            for path in files.values():
                Path(path).unlink(missing_ok=True)
    except wc.Keep:
        pass
    print(f'worktree_remove: nothing removed or closed: {why}', file=sys.stderr)
    return 3


def worker(plan_file):
    """handoff's worker: after the caller's go, close the planned terminals as they were found, see them end,
    check the worktree again and remove it; every step is logged, since this terminal goes when it ends."""
    p = json.loads(Path(plan_file).read_text(encoding='utf-8'))
    wc.LOG, hc, op = Path(p['log']), p['hc'], p['op']  # the core's environment is not the caller's
    wc.hc_path = lambda: hc
    admission, root = Path(p['admission']), Path(p['root'])
    p.update(root=root, caller=None)
    try:
        try:
            held = json.loads(admission.read_text(encoding='utf-8'))
        except (OSError, ValueError):
            held = {}
        if held.get('op') != op:
            wc.log(f'worker {op}: not admitted to remove {root}; closed nothing')
            return 3
        created(p['ready'], 'ready')
        deadline = time.monotonic() + ACK_S
        while not os.path.exists(p['ack']) and time.monotonic() < deadline:
            time.sleep(0.1)
        created(p['ack'], 'lapsed')
        word = Path(p['ack']).read_text(encoding='utf-8')
        if word != 'go':
            wc.log(f'worker {op}: the handoff was {word}; closed nothing, removed nothing')
            return 3
        def moved(t):
            n = identified(hc, t)
            if n and (n['pid'], n['created_ms']) != (t['pid'], t['created_ms']):
                raise wc.Keep(f'another program runs in {t["address"]} than the plan found')
            return n

        for t in p['closing']:  # all before any kill, and each again right before its own: hc kills by address
            moved(t)
        for t in p['closing']:  # the caller's own last
            if moved(t):
                wc.log(f'closing terminal {wc.label(t)} for the removal of {root}')
                wc.run([hc, 'kill', t['address']], timeout=wc.HC_TIMEOUT)
        deadline = time.monotonic() + END_S
        while (left := [t for t in p['closing'] if identified(hc, t) or (wc.PROC / str(t['pid'])).exists()]):
            if time.monotonic() > deadline:
                raise wc.Keep('terminals still running after their kill: ' + ', '.join(t['address'] for t in left))
            time.sleep(0.2)
        wc.content_check(root)
        if wc.head_branch(root) != (tuple(p['head']) if p['head'] else None):
            raise wc.Keep('its branch or tip moved since the gate')
        wc.check_terminals(root)
        return remove(p, {Path(s) for s in p['siblings']})
    except (wc.Keep, OSError) as why:
        wc.log(f'worker {op}: nothing removed: {root}: {why}')
        return 3
    finally:
        release(admission, op)
        for k in ('plan', 'ready', 'ack'):
            Path(p[k]).unlink(missing_ok=True)


def remove(p, approved=None):
    """git's removal of the worktree, then the cleanup of its siblings and branches, only the approved ones
    when given."""
    root, common, head = p['root'], p['common'], p['head']
    wc.log(f'running: {" ".join(command(p))}')
    try:
        wc.remove(root, common, p['force'])
    except wc.Keep as why:
        wc.log(f'git did not remove {root}; siblings and branches untouched')
        print(f'worktree_remove: {root}: {why}', file=sys.stderr)
        return 5
    wc.log(f'removed {root}')
    left = wc.cleanup(root, [(common, *head)] if head else [], approved)
    if left == 2:
        print(f'worktree_remove: removed {root}, but something appeared at that path again, so its siblings '
              f'and branches were left alone; the log has the retry: {wc.LOG}', file=sys.stderr)
    elif left:
        print(f'worktree_remove: removed {root}, but the cleanup kept a sibling or branch; the log has why '
              f'and the retry: {wc.LOG}', file=sys.stderr)
    return 6 if left else 0


if __name__ == '__main__':
    if sys.stdout:  # None under pythonw
        sys.stdout.reconfigure(errors='backslashreplace')  # a narrow code page must not stop a removal part way
    sys.exit(main())
