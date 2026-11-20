"""A reviewer comment's digest, the one pr-babysit's harvest.py and reply.py agree on.

sha256 of the body, 12 hex. CodeRabbit rewrites the end of its own comment
without changing what it raised: its footer says "reply" instead of "comment"
once the thread has one, and a fix or a confirmation adds a status line, at
times with a second footer after it, which flips the same way. For a CodeRabbit
comment ending in that structure the digest is its first form's (the comment
footer, no status). Its summary comment (the walkthrough) swaps notice banners
(the Trigger review box, review in progress, the rate limit), its approval
notice and its checkbox panels in and out, and relabels its merge risk, as
reviews are asked for, heads pushed and threads resolved; its digest is that of
the body without them. The aliases are the digests of the current body's other
forms, which a digest recorded before these rules may carry. Every other body,
and every other byte, counts as it is.
"""

import hashlib
import re

CODERABBIT = 'coderabbitai[bot]'
FOOTER = '<!-- This is an auto-generated {} by CodeRabbit -->'
# tinyusb, 232 CodeRabbit comments: these are the only tails seen after the footer.
EITHER = re.escape(FOOTER).replace(r'\{\}', '(?:comment|reply)')
TAIL = re.compile(
    EITHER +
    r'(?:(?P<status>\n\n✅ (?:Addressed in commit [0-9a-f]{7,40}|Addressed in commits [0-9a-f]{7,40} to [0-9a-f]{7,40}'
    r'|Confirmed as addressed by @[A-Za-z0-9-]+(?:\[bot\])?))'
    r'(?P<again>\n\n' + EITHER + r')?)?\Z')
# hathach/agentrc#556's summary comment, 42 saved versions: what changed there with no finding changing.
SUMMARY = '<!-- This is an auto-generated comment: summarize by coderabbit.ai -->'
WALKTHROUGH = re.compile(r'\A\s*(?:<!--(?:(?!-->).)*-->\s*)*?' + re.escape(SUMMARY), re.S)
CHROME = re.compile(
    r'(?:<!-- This is an auto-generated comment: (?!summarize )(?P<notice>[^>]*?) by coderabbit\.ai -->.*?'
    r'<!-- end of auto-generated comment: (?P=notice) by coderabbit\.ai -->'
    r'|<!-- (?P<region>approval_notice|finishing_touch_checkbox)_start -->.*?<!-- (?P=region)_end -->'
    r'|<!-- autopilot:start -->.*?<!-- autopilot:end -->)\n*', re.S)
# The merge risk's label once the head has moved past the review it rates.
PREVIOUS = re.compile(r'(Merge Risk:</strong> .*?</strong> · )previous review (<code>)')


def sha12(text):
    return hashlib.sha256(text.encode()).hexdigest()[:12]


def walkthrough(body):
    """Whether body is CodeRabbit's summary comment, under any leading markers."""
    return bool(WALKTHROUGH.match(body or ''))


def forms(body, author):
    """The body's forms, its first one first; only the body itself when it has no other."""
    body = body or ''
    if author == CODERABBIT and walkthrough(body):
        return list(dict.fromkeys((PREVIOUS.sub(r'\1up to \2', CHROME.sub('', body)), body)))
    m = TAIL.search(body) if author == CODERABBIT else None
    if not m:
        return [body]
    head, status = body[:m.start()], m['status'] or ''
    agains = [f'\n\n{FOOTER.format(w)}' for w in ('comment', 'reply')] if m['again'] else []
    out = []
    for tail in ('', status, *(status + a for a in agains)):
        for word in ('comment', 'reply'):
            f = head + FOOTER.format(word) + tail
            if f not in out:
                out.append(f)
    return out


def digests(body, author):
    """The body's digest, then its aliases."""
    return [sha12(f) for f in forms(body, author)]
