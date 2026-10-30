"""Check-run and timestamp ordering, as collect.py judges a required check and harvest.py a review bot's run: one rule
for which of several runs is the newest, so both read a head alike."""
from datetime import datetime


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
