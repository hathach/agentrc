"""Check-run and timestamp ordering, as collect.py judges a required check and harvest.py a review bot's run: one rule
for which of several runs is the newest, so both read a head alike; and which Actions runs await a maintainer's
approval, one rule for collect.py and pr-review's prepare.py."""
from datetime import datetime

# An Actions workflow run's statuses before it completes: a run in one of them can still take a job or hold a group.
RUN_PENDING = ('queued', 'in_progress', 'waiting', 'requested', 'pending')


def awaiting_approval(run):
    """A pull_request run held for a maintainer's approval (#545), a fork PR's: completed/action_required, it registers
    no check run, and only the approval starts it, never a re-run. A run another event triggered and GitHub held the same
    way, a fork contributor's review comment (hathach/tinyusb run 43103189104), is no CI of the head."""
    return run.get('event') == 'pull_request' and run.get('status') == 'completed' and run.get('conclusion') == 'action_required'


def gated_runs(repo, head, read, runs=None):
    """The head's runs awaiting_approval, as collect.py and pr-review's prepare.py read them: picked from `runs`, the
    head's whole run listing a caller already read, else from read(path), the caller's own reader of every workflow run a
    paginated Actions runs path lists (a failed read raises there, never an empty list), with GitHub's filter for them."""
    if runs is None:
        runs = read(f'repos/{repo}/actions/runs?head_sha={head}&event=pull_request&status=action_required')
    return [r for r in runs if r.get('head_sha') == head and awaiting_approval(r)]


def stamp(t):
    return datetime.fromisoformat(t.replace('Z', '+00:00')) if t else None


def newest(items, key):
    dated = [x for x in items if x.get(key)]
    return max(dated, key=lambda x: stamp(x[key])) if dated else None


def newest_reading(items, key, reading):
    """(the newest item, '') or (None, why) when one is undated, or the newest share one timestamp and read differently."""
    undated = next((x for x in items if not x.get(key)), None)
    if undated:
        return None, f"{undated.get('id')} has no {key} to order it by"
    top = newest(items, key)
    tied = [x for x in items if top and x.get(key) and stamp(x[key]) == stamp(top[key])]
    if len({repr(reading(x)) for x in tied}) > 1:
        return None, f"{', '.join(str(x.get('id')) for x in tied)} share {top[key]} and read differently"
    return top, ''


def latest_run(runs, title=False):
    """(the newest check run, '') or (None, why) when runs cannot be ordered: one has no start time
    (GitHub leaves a queued run undated), or the newest start differently; output.title counts only with title."""
    undated = [r for r in runs if not r.get('started_at')]
    if undated and len(runs) > 1:
        return None, f"check run {undated[0]['id']} has no start time to order it by"
    if undated:
        return undated[0], ''
    return newest_reading(runs, 'started_at', lambda r: (r['status'], r.get('conclusion'), (r.get('output') or {}).get('summary'),
                                                         (r.get('output') or {}).get('title') if title else None))
