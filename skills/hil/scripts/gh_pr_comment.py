#!/usr/bin/env python3
"""The plumbing of a CI report posted on a pull request by a later job (#471): which PR a run is for, which
attempt a job ran in, whether the run may still post, and the safe handling of its artifacts. Stdlib only, `gh`
for the API. Each consumer keeps its renderer, triggers, permissions and where its check runs; this holds what
they share (Adafruit_TinyUSB_Arduino's hil_workflow.py, tinyuf2's hil_comment.py, tinyusb's code_size_ci.py).
The sticky post itself stays marocchino/sticky-pull-request-comment.

It lives with the hil scripts so an install (which links skills/, not lib/) and a pin both carry it: a consumer loads
it through hil_load (`load_file('skills/hil/scripts/gh_pr_comment.py')`, in its closure), or runs it:

  gh_pr_comment.py pr --repo R --head-sha SHA [--head-branch BR --head-repo R2] [--prs JSON]
      the one open PR of R a run is for, from its trusted workflow_run metadata (pull_requests, else a fork's
      head repo, branch and SHA); prints its number, or nothing when there is none or more than one.
  gh_pr_comment.py job-attempt --repo R --run-id N --attempt N --job NAME
      the attempt job NAME of run N attempt N ran in; prints nothing when it was skipped or did not run.
  gh_pr_comment.py fresh --repo R --pr N --run-id N --attempt N --head-sha SHA --workflow FILE [--run-name T]
      exit 0 when run N attempt N is the newest run of FILE for the PR's current head, 1 when it is stale (why on
      stdout); `--run-name` matches runs by their display title (a run-name such as "HIL PR #N") instead of
      pull_requests, which lists every PR sharing a head.

Every command exits 2 when the API cannot tell, with why on stderr: unknown is never read as absent or stale.
"""

import argparse
import json
import re
import shutil
import subprocess
import sys
from pathlib import Path
from urllib.parse import quote

FILES = ()             # loads and launches nothing by path (hil_load.py)
GH_TIMEOUT = 60        # s per gh api call: a stalled one never blocks the job
MAX_JSON = 1 << 20     # bytes of one artifact file read
MAX_TEXT = 300         # characters of one untrusted string shown
MAX_COMMENT = 60000    # GitHub caps a comment body at 65536


class ApiError(Exception):
    """The API could not answer: the caller reports unknown, never absent or stale."""


def gh(path, paginate_key=None, timeout=GH_TIMEOUT):
    """The API's JSON at `path` (a GET); with `paginate_key`, that list across every page."""
    # by PATH: Windows' CreateProcess searches System32 first and only for gh.exe
    argv = [shutil.which('gh') or 'gh', 'api', '-H', 'Accept: application/vnd.github+json']
    if paginate_key:
        argv += ['--paginate', '--jq', f'.{paginate_key}[]']
    try:
        p = subprocess.run([*argv, path], capture_output=True, text=True, timeout=timeout)
    except subprocess.TimeoutExpired:
        raise ApiError(f'gh api {path}: no reply in {timeout} s') from None
    except OSError as e:
        raise ApiError(f'gh api {path}: {e}') from None
    if p.returncode:
        raise ApiError(f'gh api {path}: {p.stderr.strip()[-300:]}')
    try:
        return [json.loads(ln) for ln in p.stdout.splitlines() if ln.strip()] if paginate_key else json.loads(p.stdout)
    except ValueError as e:
        raise ApiError(f'gh api {path}: {e}') from None


def pr_of_run(repo, *, head_sha, head_branch=None, head_repo=None, pull_requests=(), api=gh):
    """(number, why) of the one open PR of `repo` a run is for: a same-repo PR from the run's `pull_requests`
    whose base is `repo`, else a fork's, found by its head repo, branch and SHA. (None, why) for none or several.
    ApiError when the lookup fails."""
    numbers = sorted({p.get('number') for p in pull_requests or () if isinstance(p, dict)
                      and ((p.get('base') or {}).get('repo') or {}).get('url', '').endswith(f'/repos/{repo}')})
    if not numbers and head_branch and head_repo:
        head = quote(f'{head_repo.split("/")[0]}:{head_branch}', safe='')   # a branch may hold '#', '+', '&'
        pulls = api(f'repos/{repo}/pulls?state=open&per_page=100&head={head}')
        if not isinstance(pulls, list):
            raise ApiError(f'repos/{repo}/pulls: not a list')
        numbers = sorted({p['number'] for p in pulls if isinstance(p, dict) and (p.get('head') or {}).get('sha') ==
                          head_sha and ((p.get('base') or {}).get('repo') or {}).get('full_name') == repo})
    if len(numbers) == 1:
        return numbers[0], ''
    return None, f'PRs {numbers} share this run' if numbers else 'no open PR of this run'


def job_attempt(repo, run_id, attempt, job, api=gh):
    """The attempt job `job` of run `run_id` attempt `attempt` ran in (a re-run reuses an earlier attempt's
    successful job), or None when it was skipped or has no conclusion. ApiError when the API cannot tell."""
    jobs = api(f'repos/{repo}/actions/runs/{run_id}/attempts/{attempt}/jobs?per_page=100', 'jobs')
    named = [j for j in jobs if isinstance(j, dict) and j.get('name') == job]
    if not named or named[0].get('conclusion') in (None, 'skipped'):
        return None
    return named[0].get('run_attempt') or int(attempt)


def run_is_pr(run, pr, head, run_name=None):
    """Whether pull_request run `run` may be PR `pr`'s, whose head is `head` (the PR's `head` object). With
    `run_name`, by the run's display title alone: pull_requests lists every open PR on a head, so a branch opened
    against two bases lists both. Otherwise by pull_requests, and for a fork's run, which GitHub leaves unlisted,
    by its head branch and repository."""
    if run_name is not None:
        return run.get('display_title') == run_name
    numbers = [p.get('number') for p in run.get('pull_requests') or () if isinstance(p, dict)]
    if numbers:
        return int(pr) in numbers
    return run.get('head_branch') == head.get('ref') and \
        (run.get('head_repository') or {}).get('full_name') == (head.get('repo') or {}).get('full_name')


def fresh(repo, *, pr, head_sha, run_id, attempt, workflow, run_name=None, api=gh):
    """(fresh, why): run `run_id` attempt `attempt` is the newest run of `workflow` (its file name) for the PR's
    current head and that run's newest attempt, so its comment overwrites no newer one. Every run is read, newest
    by run_number, not by list order; the attempt is the run's live one. ApiError when the API cannot tell,
    a malformed answer included."""
    try:
        head = api(f'repos/{repo}/pulls/{pr}')['head']
        if head['sha'] != head_sha:
            return False, f'PR #{pr} head moved to {head["sha"][:7]}; this run tested {head_sha[:7]}'
        runs = [r for r in api(f'repos/{repo}/actions/workflows/{workflow}/runs?event=pull_request'
                               f'&head_sha={head_sha}&per_page=100', 'workflow_runs')
                if isinstance(r, dict) and run_is_pr(r, pr, head, run_name)]
        latest = max(runs, key=lambda r: int(r['run_number']), default=None)
        if latest is None:
            return False, f'no {workflow} run of PR #{pr} listed for {head_sha[:7]}'
        if int(latest['id']) != int(run_id):
            return False, f'run {latest["id"]} is newer than run {run_id} for {head_sha[:7]}'
        now = int(api(f'repos/{repo}/actions/runs/{run_id}')['run_attempt'])
    except (KeyError, TypeError, ValueError) as e:
        raise ApiError(f'malformed API answer: {type(e).__name__}: {e}') from None
    if now != int(attempt):
        return False, f'run {run_id} attempt {now} is newer than attempt {attempt}'
    return True, 'current'


# ---- artifacts, read as untrusted data

def read_artifact_json(base, rel, max_bytes=MAX_JSON):
    """An artifact file as data: a regular file inside `base`, not a link, at most `max_bytes`, a JSON object;
    None otherwise."""
    base = Path(base)
    p = base / rel
    try:
        if p.is_symlink() or not p.is_file() or base.resolve() not in p.resolve().parents:
            return None
        if p.stat().st_size > max_bytes:
            return None
        data = json.loads(p.read_text(encoding='utf-8'))
        return data if isinstance(data, dict) else None
    except (OSError, ValueError, RecursionError):
        return None


def literal(v, table=False, max_text=MAX_TEXT):
    """Untrusted text on one line as an inline code span, so no Markdown, HTML, mention or link in it renders;
    pipes escaped in a table cell (GFM unescapes them inside the span)."""
    text = ' '.join(str(v).split())[:max_text]
    if not text:
        return ''
    fence = '`' * (max(map(len, re.findall('`+', text)), default=0) + 1)
    pad = ' ' if text[0] in '` ' or text[-1] in '` ' else ''
    if table:
        text = text.replace('|', '\\|')
    return f'{fence}{pad}{text}{pad}{fence}'


def neutralize_mentions(text):
    """`text` with every @mention broken by a zero-width space: a report pings no one."""
    return re.sub(r'@(?=[\w-])', '@​', text)


def cap(text, limit=MAX_COMMENT, note='…truncated: see the run\'s artifacts.'):
    """`text` cut at the last line end within `limit`, so no code span is left open, with `note` after it."""
    if len(text) <= limit:
        return text
    cut = text.rfind('\n', 0, limit - len(note) - 2) + 1
    return text[:cut] + '\n' + note + '\n'


# ---- CLI

def main(argv=None, api=gh):
    ap = argparse.ArgumentParser(description=__doc__.split('\n\n')[0])
    sub = ap.add_subparsers(dest='cmd', required=True)
    p = sub.add_parser('pr')
    p.add_argument('--repo', required=True)
    p.add_argument('--head-sha', required=True)
    p.add_argument('--head-branch')
    p.add_argument('--head-repo')
    p.add_argument('--prs', default='[]', help="the run's pull_requests, as JSON")
    j = sub.add_parser('job-attempt')
    j.add_argument('--repo', required=True)
    j.add_argument('--run-id', required=True)
    j.add_argument('--attempt', required=True)
    j.add_argument('--job', required=True)
    f = sub.add_parser('fresh')
    f.add_argument('--repo', required=True)
    f.add_argument('--pr', required=True)
    f.add_argument('--run-id', required=True)
    f.add_argument('--attempt', required=True)
    f.add_argument('--head-sha', required=True)
    f.add_argument('--workflow', required=True, help='the workflow file name, e.g. hil.yml')
    f.add_argument('--run-name', help='match runs by this display title instead of pull_requests')
    a = ap.parse_args(argv)
    try:
        if a.cmd == 'pr':
            try:
                prs = json.loads(a.prs)
            except ValueError:
                prs = []
            number, why = pr_of_run(a.repo, head_sha=a.head_sha, head_branch=a.head_branch, head_repo=a.head_repo,
                                    pull_requests=prs if isinstance(prs, list) else [], api=api)
            if number is None:
                print(f'{why}: no comment', file=sys.stderr)
            else:
                print(number)
            return 0
        if a.cmd == 'job-attempt':
            n = job_attempt(a.repo, a.run_id, a.attempt, a.job, api=api)
            if n is not None:
                print(n)
            return 0
        ok, why = fresh(a.repo, pr=a.pr, head_sha=a.head_sha, run_id=a.run_id, attempt=a.attempt,
                        workflow=a.workflow, run_name=a.run_name, api=api)
    except ApiError as e:
        print(f'unknown: {e}', file=sys.stderr)
        return 2
    print(why if ok else f'stale: {why}')
    return 0 if ok else 1


if __name__ == '__main__':
    sys.exit(main())
