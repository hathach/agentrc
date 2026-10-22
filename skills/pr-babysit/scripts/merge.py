#!/usr/bin/env python3
"""Whether a PR a pr-babysit launch passed is ready to merge, and with --merge, squash-merge it.

  merge.py --pr-url URL --base BRANCH --checkout DIR --output FILE --state-digest D [--merge]

chief's, under its merge exception (agents/chief.md): URL and BRANCH are the PR and base branch the grant names,
FILE the last launch's saved Workflow output and D its stateDigest. Every condition is read again here: the launch
(complete, a `both` lane, pass, no blocker launch_result.py reads, no accepted failure, deferral, hold, reply debt
or pending publication, its state matching D and its own digest, pinned to that PR), one head everywhere (the
checkout's, the state's, the reviewed one, the reviews', CI's and the PR's), a fresh harvest (harvest.py with the
launch's reviewers, from its fingerprint: the same fingerprint, CodeRabbit and Greptile both `reviewed` on the head),
a fresh CI inventory (collect.py with no ignored check: green, mergeable, nothing pending), the base branch's required
checks each succeeded on the head, and the PR (OPEN, not a draft, its base and head repositories and branches the
bound ones, squash allowed, no merge queue, merge state CLEAN, or BLOCKED, when it merges with --admin). Unknown or
unreadable evidence is a blocker.

Without --merge nothing is written. With it and no blocker, it runs
`gh pr merge N -R REPO --squash --match-head-commit HEAD --subject "<title> (#N)" --body ""`, with --admin only on
BLOCKED, and reads the PR back. Only the head is enforced by GitHub at the merge; the rest is this re-read.

stdout ends with one JSON line {pr, checkedHead, outcome, blockers, admin, requirements, mergeCommit, mergedHead,
closingIssues, detail, seal}: requirements is what GitHub last reported (mergeStateStatus, mergeable, reviewDecision)
and each required check with the app it must come from and its state on the head, the evidence behind an --admin
merge; outcome is ready (nothing blocks, nothing merged), not-ready (blockers, or GitHub refused the merge and
the PR is still open: run it again, which checks everything again), merged (this run's merge, read back), observed-
merged (the PR is merged but this run cannot claim it: someone else, or a merge whose answer was lost), closed, or
uncertain (the merge was asked and its outcome is unread: inspect before anything else, never retry blind).
closingIssues are the issues GitHub links the PR to close, with their state. Exit 2 with {"error": ...} when the
arguments are wrong or a fact cannot be read.
"""

import json
import re
import sys
import time
from pathlib import Path

sys.path.insert(0, str(Path(__file__).parent))
from facts import FULL_SHA, Parser, Unusable, attempt, canonical, fnv1a, report  # noqa: E402
import harvest  # noqa: E402
import launch_result  # noqa: E402

BOTS = ('coderabbit', 'greptile')
PR_URL = re.compile(r'\Ahttps://github\.com/([^/\s]+)/([^/\s]+)/pull/([1-9][0-9]*)\Z')
COLLECT = Path(__file__).resolve().parents[2] / 'ci-rerun' / 'scripts' / 'collect.py'
# GitHub counts these as a passed required check.
PASSED = ('success', 'neutral', 'skipped')
ALREADY_MERGED = re.compile(r'was already merged', re.I)
READBACK_TRIES = 5
READBACK_WAIT = 3  # seconds between reads after a merge
PR_QUERY = '''query($owner: String!, $name: String!, $n: Int!) { repository(owner: $owner, name: $name) {
  squashMergeAllowed
  pullRequest(number: $n) { number url state isDraft title headRefOid headRefName headRepository { nameWithOwner }
    baseRefName baseRepository { nameWithOwner } mergeStateStatus mergeable reviewDecision isMergeQueueEnabled mergeCommit { oid }
    closingIssuesReferences(first: 50) { nodes { number state url } } } } }'''


def pr_identity(url):
    m = PR_URL.match(url or '')
    return (f'{m[1]}/{m[2]}'.lower(), int(m[3])) if m else None


def state_digest(state):
    """The workflow's sealOf: fnv1a over the canonical JSON of the state without its digest, nulls kept."""
    return fnv1a(canonical({k: v for k, v in state.items() if k != 'digest'}, nulls=True))


def launch(output, path, digest, tree, url):
    """(blockers, facts) of the launch's saved output: the state, the observation and the heads it names."""
    if output is None:
        raise Unusable(f'--output {path} is empty: the launch threw before returning')
    reading = launch_result.summarize(output, path, None, tree)
    blockers = [f'launch: {b}' for b in reading['blockers']]
    result = output.get('result')
    if not isinstance(result, dict) or not isinstance(result.get('state'), dict):
        raise Unusable(f'--output {path} holds no result with a state')
    state, obs = result['state'], result.get('observation') or {}
    reviews = obs.get('reviews') or {}
    if result.get('stateDigest') != digest or state.get('digest') != digest:
        blockers.append(f'launch: its state is {state.get("digest")!r} (stateDigest {result.get("stateDigest")!r}), not --state-digest {digest}')
    elif state_digest(state) != digest:
        blockers.append('launch: its state does not match its own digest')
    if (result.get('status'), result.get('pass'), obs.get('lane')) != ('complete', True, 'both'):
        stopped = f" (stopped {result['reason']})" if result.get('reason') else ''
        blockers.append(f"launch: status {result.get('status')}, pass {result.get('pass')}, lane {obs.get('lane')}{stopped}: "
                        'a merge needs a complete `both` launch that passed')
    for key, what in (('acceptedFailures', 'accepted CI failure(s)'), ('deferrals', 'deferred finding(s)'), ('holds', 'held finding(s)'),
                      ('debt', 'comment(s) still owed a reply'), ('reanswer', 'comment(s) to answer again')):
        if state.get(key):
            blockers.append(f'launch: {len(state[key])} {what}')
    if state.get('pending'):
        blockers.append(f"launch: a commit pending publication: {state['pending']}")
    pinned = pr_identity((state.get('pin') or {}).get('prUrl'))
    if pinned != url:
        blockers.append(f"launch: its state is pinned to {(state.get('pin') or {}).get('prUrl')!r}, not the PR the grant names")
    heads = {'checkout': (tree or {}).get('head'), 'state': state.get('expectedHead'), 'reviewed': obs.get('reviewedHead'),
             'reviews': reviews.get('headSha'), 'ci': (obs.get('ci') or {}).get('headSha')}
    if len(set(heads.values())) != 1 or not all(FULL_SHA.match(h or '') for h in heads.values()):
        blockers.append(f'launch: heads disagree or are not full SHAs: {heads}')
    if not reviews.get('fingerprint'):
        blockers.append('launch: no review fingerprint (an older output, or a harvest that failed): it cannot authorize a merge')
    return blockers, {'state': state, 'head': state.get('expectedHead'), 'fingerprint': reviews.get('fingerprint') or ''}


def view(repo, n):
    owner, name = repo.split('/')
    got = harvest.gh_json('api', 'graphql', '-f', f'query={PR_QUERY}', '-F', f'owner={owner}', '-F', f'name={name}', '-F', f'n={n}')
    try:
        data = got['data']['repository']
        pr = data['pullRequest']
        return {**pr, 'squashMergeAllowed': data['squashMergeAllowed'], 'closingIssues': pr['closingIssuesReferences']['nodes']}
    except (KeyError, TypeError) as e:
        raise Unusable(f'the PR query answered in a shape merge.py does not read ({e!r}): {json.dumps(got)[:300]}')


def settled_view(repo, n):
    """The PR once GitHub has computed its mergeability, which it does lazily: UNKNOWN on the first read is common."""
    for tried in range(READBACK_TRIES):
        if tried:
            time.sleep(READBACK_WAIT)
        pr = view(repo, n)
        if pr['state'] != 'OPEN' or 'UNKNOWN' not in (pr['mergeable'], pr['mergeStateStatus']):
            break
    return pr


def pr_blockers(pr, url, base, state):
    pin = state.get('pin') or {}
    blockers = []
    if pr_identity(pr.get('url')) != url or (pr.get('baseRepository') or {}).get('nameWithOwner', '').lower() != url[0]:
        blockers.append(f"github: the PR is {pr.get('url')} in {(pr.get('baseRepository') or {}).get('nameWithOwner')}, not the one the grant names")
    if pr.get('state') != 'OPEN' or pr.get('isDraft') is not False:
        blockers.append(f"github: the PR is {pr.get('state')}{', a draft' if pr.get('isDraft') else ''}")
    if pr.get('baseRefName') != base:
        blockers.append(f"github: its base is {pr.get('baseRefName')!r}, not {base!r}")
    head_repo = ((pr.get('headRepository') or {}).get('nameWithOwner') or '').lower()
    if (head_repo, pr.get('headRefName')) != ((pin.get('prRepo') or '').lower(), pin.get('prBranch')):
        blockers.append(f"github: its head is {head_repo}:{pr.get('headRefName')}, the state's {pin.get('prRepo')}:{pin.get('prBranch')}")
    if pr.get('squashMergeAllowed') is not True:
        blockers.append('github: the repository does not allow squash merging')
    if pr.get('isMergeQueueEnabled') is not False:
        blockers.append(f"github: merge queue {pr.get('isMergeQueueEnabled')!r}: merging through a queue is not supported")
    if pr.get('mergeable') != 'MERGEABLE' or pr.get('mergeStateStatus') not in ('CLEAN', 'BLOCKED'):
        blockers.append(f"github: mergeable {pr.get('mergeable')}, merge state {pr.get('mergeStateStatus')}")
    return blockers


def rules(repo, base):
    """(the base branch's required checks as {context, app}, app the GitHub App id that must provide it or None for any,
    whether its rules allow a squash merge, whether they require a merge queue)."""
    branch = harvest.gh_json('api', f'repos/{repo}/branches/{base}')
    checks = (((branch.get('protection') or {}).get('required_status_checks')) or {})
    source = lambda app: None if app in (None, -1) else app
    required = [{'context': c, 'app': None} for c in checks.get('contexts') or []]
    required += [{'context': c['context'], 'app': source(c.get('app_id'))} for c in checks.get('checks') or []]
    squash, queue = True, False
    for rule in harvest.pages(f'repos/{repo}/rules/branches/{base}'):
        params = rule.get('parameters') or {}
        if rule.get('type') == 'required_status_checks':
            required += [{'context': c['context'], 'app': source(c.get('integration_id'))} for c in params.get('required_status_checks') or []]
        if rule.get('type') == 'pull_request' and params.get('allowed_merge_methods') is not None:
            squash = squash and 'squash' in params['allowed_merge_methods']
        queue = queue or rule.get('type') == 'merge_queue'
    # A context required from a named app and also from any app needs the named one.
    named = {r['context'] for r in required if r['app'] is not None}
    unique = {(r['context'], r['app']): r for r in required if r['app'] is not None or r['context'] not in named}
    return sorted(unique.values(), key=lambda r: (r['context'], r['app'] or 0)), squash, queue


def required_checks(repo, base, head):
    """(blockers, each required check with what the head shows for it): a check required from an app passes only on
    that app's run; a commit status names no app, so it passes only a check any source may provide."""
    required, squash, queue = rules(repo, base)
    blockers = [] if squash else ['github: the base branch rules do not allow a squash merge']
    if queue:
        blockers.append('github: the base branch rules require a merge queue')
    if not required:
        return blockers, []
    runs = harvest.pages(f'repos/{repo}/commits/{head}/check-runs', 'check_runs')
    statuses = harvest.pages(f'repos/{repo}/commits/{head}/statuses')
    seen = []
    for r in required:
        mine = [x for x in runs if x.get('name') == r['context'] and (r['app'] is None or (x.get('app') or {}).get('id') == r['app'])]
        latest_run = max(mine, key=lambda x: x['id'], default=None)
        latest_status = None if r['app'] is not None else harvest.newest([x for x in statuses if x.get('context') == r['context']], 'updated_at')
        if latest_run:
            state, ok = f"{latest_run.get('status')}/{latest_run.get('conclusion')}", latest_run.get('status') == 'completed' and latest_run.get('conclusion') in PASSED
        elif latest_status:
            state, ok = latest_status.get('state'), latest_status.get('state') == 'success'
        else:
            state, ok = 'not registered' + (f" by app {r['app']}" if r['app'] is not None else ''), False
        seen.append({**r, 'state': state})
        if not ok:
            blockers.append(f"github: required check {r['context']!r} on the head: {state}")
    return blockers, seen


def review_blockers(repo, n, fingerprint, head):
    reviewers, auto, _ = (fingerprint.split('|') + ['', '', ''])[:3] if fingerprint else ('', '', '')
    reviewers, auto = [r for r in reviewers.split(',') if r], [r for r in auto.split(',') if r]
    missing = [b for b in BOTS if b not in auto]
    if not fingerprint or missing:
        return [f"reviews: the launch's harvest did not settle {', '.join(missing or BOTS)}"]
    fresh = harvest.collect(['--pr', str(n), '--reviewers', ','.join(reviewers), '--auto-run', ','.join(auto), '--repo', repo])
    blockers = []
    if fresh['headSha'] != head:
        blockers.append(f"reviews: harvested head {fresh['headSha']}, not {head}")
    if fresh['fingerprint'] != fingerprint:
        blockers.append('fingerprint: the review evidence changed since the launch judged it: relaunch a `both` launch on the same state')
    for b in fresh['bots']:
        if b['bot'] in BOTS and (b['state'], b['sha']) != ('reviewed', head):
            blockers.append(f"{b['bot']}-unreviewed: {b['state']}{'/' + b['kind'] if b.get('kind') else ''}: {launch_result.cut(b.get('reason') or '')}")
    return blockers


def ci_blockers(repo, n, head):
    code, out, err = attempt(sys.executable, str(COLLECT), 'inventory', '--repo', repo, '--pr', str(n), '--head', head)
    try:
        inv = json.loads(out.strip().splitlines()[-1])
    except (ValueError, IndexError):
        raise Unusable(f'collect.py inventory exited {code} with no JSON line: {err.strip()[-300:]}')
    if inv.get('error'):
        return [f"ci: {launch_result.cut(inv['error'])}"]
    got = (inv.get('status'), inv.get('head'), inv.get('mergeable'), inv.get('pending'), inv.get('ignored'))
    if got != ('green', head, 'MERGEABLE', 0, []) or not inv.get('registered'):
        failed = ', '.join(c.get('name', '') for c in inv.get('checks') or [])
        return [f"ci: status {got[0]}, head {got[1]}, mergeable {got[2]}, {got[3]} pending, {inv.get('registered')} registered"
                f"{f'; failed: {failed}' if failed else ''}"]
    return []


def readback(repo, n, merged_any):
    """(outcome, the PR) after a merge request: merged_any says GitHub accepted it."""
    pr = None
    for tried in range(READBACK_TRIES):
        if tried:
            time.sleep(READBACK_WAIT)
        try:
            pr = view(repo, n)
        except Unusable:
            continue
        if pr['state'] != 'OPEN' or not merged_any:
            break
    if pr is None:
        return 'uncertain', None
    if pr['state'] == 'MERGED':
        return ('merged' if merged_any else 'observed-merged'), pr
    if pr['state'] == 'CLOSED':
        return 'closed', pr
    return ('uncertain' if merged_any else 'not-ready'), pr


def receipt(n, head, outcome, blockers, admin=False, pr=None, detail='', requirements=None):
    pr = pr or {}
    return {'pr': n, 'checkedHead': head, 'outcome': outcome, 'blockers': blockers, 'admin': admin, 'requirements': requirements,
            'mergeCommit': (pr.get('mergeCommit') or {}).get('oid') if outcome in ('merged', 'observed-merged') else None,
            'mergedHead': pr.get('headRefOid') if outcome in ('merged', 'observed-merged') else None,
            'closingIssues': [{'number': i.get('number'), 'state': i.get('state'), 'url': i.get('url')} for i in pr.get('closingIssues') or []],
            'detail': detail}


def collect(argv):
    p = Parser(prog='merge.py', add_help=False)
    p.add_argument('--pr-url', required=True)
    p.add_argument('--base', required=True)
    p.add_argument('--checkout', required=True)
    p.add_argument('--output', required=True)
    p.add_argument('--state-digest', required=True)
    p.add_argument('--merge', action='store_true')
    a = p.parse_args(argv)
    url = pr_identity(a.pr_url)
    if not url:
        raise Unusable(f'--pr-url must be https://github.com/OWNER/REPO/pull/N, not {a.pr_url!r}')
    repo, n = url
    path = Path(a.output).resolve().as_posix()
    blockers, facts = launch(launch_result.load_output(a.output), path, a.state_digest, launch_result.checkout(a.checkout), url)
    head = facts['head']
    pr = settled_view(repo, n)
    if pr['state'] == 'MERGED':
        return receipt(n, head, 'observed-merged', blockers, pr=pr, detail='the PR is already merged')
    if pr['state'] == 'CLOSED':
        return receipt(n, head, 'closed', blockers, pr=pr, detail='the PR is closed')
    if not FULL_SHA.match(head or ''):
        raise Unusable(f"the launch's state names no full head SHA: {head!r}")
    if pr['headRefOid'] != head:
        blockers.append(f"github: the PR head is {pr['headRefOid']}, not the launch's {head}: it rejoins through adoptHead and needs fresh reviews")
    blockers += pr_blockers(pr, url, a.base, facts['state'])
    required, checks = required_checks(repo, a.base, head)
    blockers += required
    blockers += review_blockers(repo, n, facts['fingerprint'], head)
    blockers += ci_blockers(repo, n, head)
    # Read just before merging: the checks above take a while, and the PR may have been retargeted or moved meanwhile.
    last = settled_view(repo, n)
    # A merge state that changed may name a requirement the checks above never read: run again to read it.
    if (last['state'], last['headRefOid'], last['mergeStateStatus']) != ('OPEN', head, pr['mergeStateStatus']):
        blockers.append(f"github: the PR changed during the checks: {last['state']}, head {last['headRefOid']}, merge state {last['mergeStateStatus']}")
    blockers += [b for b in pr_blockers(last, url, a.base, facts['state']) if b not in blockers]
    admin = last['mergeStateStatus'] == 'BLOCKED'
    # What GitHub reported and the required checks read: the evidence behind an --admin merge.
    requirements = {'mergeStateStatus': last['mergeStateStatus'], 'mergeable': last['mergeable'],
                    'reviewDecision': last.get('reviewDecision'), 'requiredChecks': checks}
    if blockers or not a.merge:
        return receipt(n, head, 'not-ready' if blockers else 'ready', blockers, admin=admin and not blockers, pr=last, requirements=requirements)
    cmd = ['gh', 'pr', 'merge', str(n), '-R', repo, '--squash', '--match-head-commit', head,
           '--subject', f"{last['title']} (#{n})", '--body', '', *(['--admin'] if admin else [])]
    code, out, err = attempt(*cmd)
    said = (err.strip() or out.strip()).splitlines()
    # gh exits 0 on a PR someone else merged first: that merge is not this run's.
    ours = code == 0 and not ALREADY_MERGED.search(out + err)
    outcome, after = readback(repo, n, ours)
    detail = f"gh pr merge exited {code}{': ' + launch_result.cut(said[-1]) if said else ''}"
    refused = [f'github: the merge was refused: {detail}'] if outcome == 'not-ready' else []
    return receipt(n, head, outcome, refused, admin=admin, pr=after or last, detail=detail, requirements=requirements)


if __name__ == '__main__':
    sys.exit(report(collect, sys.argv[1:], seal=True))
