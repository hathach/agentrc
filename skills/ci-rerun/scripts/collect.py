#!/usr/bin/env python3
"""Collect a PR's CI state for a watcher, so no model spends turns waiting or listing.

  collect.py inventory --repo OWNER/NAME --pr N --head SHA [--wait-seconds S]

inventory: waits up to S seconds (default 0) while any check is pending, then
prints one JSON object {repo, pr, head, baseRef, baseSha, status, waited,
counts, checks, error}. `status` is green, red (a check failed or was
cancelled) or running (still pending, or no checks registered yet). `checks`
lists only the checks that did not pass or skip, each {name, workflow, bucket,
link, attempt}: `attempt` is the per-run id in a link that has one (an Actions
job, a Read the Docs build, a CircleCI job; each re-run mints a new one), and
null for a link that stays the same across runs (a docs preview, a review
bot's page, none), so only a non-null attempt can tell one run from the next.
`repo` must own the PR number, which for a fork PR is not the head repository. Exit 0
with the object, 1 with `error` set (a gh failure, or the PR head is no longer
--head), 2 on a usage error.
"""

import argparse
import json
import re
import subprocess
import sys
import time

POLL = 30
ATTEMPT = (
    ('actions', re.compile(r'^https://github\.com/[^/]+/[^/]+/actions/runs/\d+/job/(\d+)')),
    ('readthedocs', re.compile(r'^https://app\.readthedocs\.(?:org|com)/projects/[\w-]+/builds/(\d+)/?$')),
    ('circleci', re.compile(r'^https://circleci\.com/gh/[^/]+/[^/]+/(\d+)$')),
)


class Failed(Exception):
    pass


def gh(*args):
    try:
        done = subprocess.run(['gh', *args], capture_output=True, text=True)
    except OSError as e:
        raise Failed(f'gh: {e}')
    if done.returncode != 0:
        raise Failed(f'gh {args[0]} {args[1]}: {done.stderr.strip() or f"exit {done.returncode}"}')
    try:
        return json.loads(done.stdout)
    except ValueError:
        raise Failed(f'gh {args[0]} {args[1]}: not JSON: {done.stdout[:200]!r}')


def pull(repo, pr):
    return gh('pr', 'view', str(pr), '--repo', repo, '--json', 'headRefOid,baseRefName,baseRefOid')


def listing(repo, pr):
    try:
        return gh('pr', 'checks', str(pr), '--repo', repo, '--json', 'name,workflow,bucket,link')
    except Failed as e:
        if 'no checks reported' in str(e):
            return []
        raise


def attempt(link):
    for kind, pattern in ATTEMPT:
        m = pattern.match(link or '')
        if m:
            return f'{kind}:{m.group(1)}'
    return None


def summary(checks):
    counts = {}
    for c in checks:
        counts[c['bucket']] = counts.get(c['bucket'], 0) + 1
    if not checks or counts.get('pending'):
        return 'running', counts
    return ('red' if counts.get('fail') or counts.get('cancel') else 'green'), counts


def inventory(repo, pr, head, wait):
    before = pull(repo, pr)
    if before['headRefOid'] != head:
        raise Failed(f'PR #{pr} head is {before["headRefOid"]}, not {head}')
    start = time.monotonic()
    while True:
        checks = listing(repo, pr)
        status, counts = summary(checks)
        waited = int(time.monotonic() - start)
        if status != 'running' or waited + POLL > wait:
            break
        time.sleep(POLL)
    # The listing reads the PR's last commit: a push during it would describe another head.
    after = pull(repo, pr)['headRefOid']
    if after != head:
        raise Failed(f'PR #{pr} head moved to {after} while collecting')
    listed = [{**c, 'attempt': attempt(c.get('link'))} for c in checks if c['bucket'] not in ('pass', 'skipping')]
    return {'repo': repo, 'pr': pr, 'head': head, 'baseRef': before['baseRefName'], 'baseSha': before['baseRefOid'],
            'status': status, 'waited': waited, 'counts': counts, 'checks': listed, 'error': None}


def main(argv=None):
    p = argparse.ArgumentParser(description=__doc__, formatter_class=argparse.RawDescriptionHelpFormatter)
    p.add_argument('command', choices=['inventory'])
    p.add_argument('--repo', required=True, help='OWNER/NAME of the repository that owns the PR number')
    p.add_argument('--pr', type=int, required=True)
    p.add_argument('--head', required=True, help='the head SHA the caller expects')
    p.add_argument('--wait-seconds', type=int, default=0)
    a = p.parse_args(argv)
    if not re.fullmatch(r'[0-9a-f]{40}', a.head):
        p.error('--head must be a full 40-hex SHA')
    try:
        out, rc = inventory(a.repo, a.pr, a.head, max(0, a.wait_seconds)), 0
    except Failed as e:
        out, rc = {'repo': a.repo, 'pr': a.pr, 'head': a.head, 'error': str(e)}, 1
    print(json.dumps(out))
    return rc


if __name__ == '__main__':
    sys.exit(main())
