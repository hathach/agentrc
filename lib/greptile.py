"""Greptile's confidence check, as collect.py reads it for a CI gate and harvest.py for Greptile's review: one recogniser
for the result that scores a review below the repository's threshold, so both read a run alike."""
import re

APP = 'greptile-apps'
CHECK = 'Greptile Review'
BELOW = re.compile(r'\bConfidence (\d+)/(\d+) — below your required (\d+)/(\d+)$')
GATE = "Greptile reviewed this pull request successfully — this check reflects your team's confidence threshold, not a review failure."


def threshold(run):
    """'complete' when the run concluded failure with the threshold result's title and summary, 'partial' when it shows
    only some of that, else None."""
    output = run.get('output') or {}
    title, summary = ((output.get(k) or '').strip() for k in ('title', 'summary'))
    marks = (bool(BELOW.search(title)), summary.startswith(GATE))
    if run.get('conclusion') == 'failure' and all(marks):
        return 'complete'
    return 'partial' if any(marks) else None
