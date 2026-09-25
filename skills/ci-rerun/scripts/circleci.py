#!/usr/bin/env python3
"""CircleCI by job number, which is all a GitHub check's details URL carries.

  circleci.py rerun <job-number>... [--repo OWNER/NAME]
  circleci.py log <job-number> [--lines N] [--repo OWNER/NAME]

rerun: each job's workflow, deduplicated, is re-run from its failed jobs with
the circleci CLI (`circleci workflow rerun <uuid> --from-failed`, token from
~/.circleci/cli.yml); stdout ends with one JSON line
{"reruns": [{"workflow", "jobs", "newWorkflow"}], "errors": [...]}. Exit 0 when
every workflow was re-run, 1 otherwise, 2 on a usage error.
log: prints the last N lines (default 150) of every failed step of the job.

The job lookup is the public v1.1 endpoint, no token needed.
"""

import argparse
import json
import re
import subprocess
import sys
import urllib.request

UUID = re.compile(r'^[0-9a-f]{8}-[0-9a-f]{4}-[0-9a-f]{4}-[0-9a-f]{4}-[0-9a-f]{12}$')


class Failed(Exception):
    pass


def fetch(url):
    try:
        with urllib.request.urlopen(url, timeout=60) as r:
            return json.load(r)
    except (OSError, ValueError) as e:
        raise Failed(f'{url}: {e}')


def job(repo, number):
    return fetch(f'https://circleci.com/api/v1.1/project/github/{repo}/{number}')


def workflow_of(repo, number):
    wid = (job(repo, number).get('workflows') or {}).get('workflow_id')
    if not isinstance(wid, str) or not UUID.match(wid):
        raise Failed(f'job {number}: no workflow id in its record ({wid!r})')
    return wid


def rerun(repo, numbers):
    by_workflow, errors = {}, []
    for n in numbers:
        try:
            by_workflow.setdefault(workflow_of(repo, n), []).append(n)
        except Failed as e:
            errors.append(str(e))
    reruns = []
    for wid, jobs in by_workflow.items():
        done = subprocess.run(['circleci', 'workflow', 'rerun', wid, '--from-failed', '--json'],
                              capture_output=True, text=True)
        try:
            new = json.loads(done.stdout)['workflow_id'] if done.returncode == 0 else None
        except (ValueError, KeyError, TypeError):
            new = None
        if not isinstance(new, str) or not UUID.match(new):
            errors.append(f'workflow {wid} (jobs {", ".join(map(str, jobs))}): rerun failed: '
                          f'{(done.stderr or done.stdout).strip() or f"exit {done.returncode}"}')
            continue
        reruns.append({'workflow': wid, 'jobs': jobs, 'newWorkflow': new})
    print(json.dumps({'reruns': reruns, 'errors': errors}))
    return 0 if not errors else 1


def failed_steps(repo, number, lines):
    """(step name, its output's last `lines` lines) for each failed step."""
    failed = [(s['name'], a['output_url']) for s in job(repo, number).get('steps', [])
              for a in s.get('actions', []) if a.get('status') not in ('success', 'skipped') and a.get('output_url')]
    if not failed:
        raise Failed(f'job {number}: no failed step with output')
    return [(name, '\n'.join(''.join(x.get('message', '') for x in fetch(url)).splitlines()[-lines:])) for name, url in failed]


def log(repo, number, lines):
    for name, tail in failed_steps(repo, number, lines):
        print(f'== {name}\n{tail}')
    return 0


def main(argv=None):
    p = argparse.ArgumentParser(description=__doc__, formatter_class=argparse.RawDescriptionHelpFormatter)
    p.add_argument('command', choices=['rerun', 'log'])
    p.add_argument('jobs', nargs='+', type=int, metavar='job-number')
    p.add_argument('--repo', help='OWNER/NAME (default: gh repo view)')
    p.add_argument('--lines', type=int, default=150)
    a = p.parse_args(argv)
    repo = a.repo
    if not repo:
        done = subprocess.run(['gh', 'repo', 'view', '--json', 'nameWithOwner', '-q', '.nameWithOwner'],
                              capture_output=True, text=True)
        if done.returncode != 0:
            print(f'circleci.py: {done.stderr.strip()}', file=sys.stderr)
            return 2
        repo = done.stdout.strip()
    if a.command == 'log' and len(a.jobs) != 1:
        p.error('log takes one job number')
    try:
        return rerun(repo, a.jobs) if a.command == 'rerun' else log(repo, a.jobs[0], a.lines)
    except Failed as e:
        print(f'circleci.py: {e}', file=sys.stderr)
        return 1


if __name__ == '__main__':
    sys.exit(main())
