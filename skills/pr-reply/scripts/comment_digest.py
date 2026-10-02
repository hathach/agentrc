"""A reviewer comment's digest, the one pr-babysit's harvest.py and reply.py agree on.

sha256 of the body, 12 hex. CodeRabbit rewrites the end of its own comment
without changing what it raised: its footer says "reply" instead of "comment"
once the thread has one, and a fix or a confirmation adds a status line, at
times with a second footer after it. For a CodeRabbit comment ending in that
structure the digest is its first form's (the comment footer, no status), and
the aliases are the digests of the current body's other forms, which a digest
recorded before this rule may carry. Every other body, and every other byte,
counts as it is.
"""

import hashlib
import re

CODERABBIT = 'coderabbitai[bot]'
FOOTER = '<!-- This is an auto-generated {} by CodeRabbit -->'
# tinyusb, 232 CodeRabbit comments: these are the only tails seen after the footer.
TAIL = re.compile(
    re.escape(FOOTER).replace(r'\{\}', '(?:comment|reply)') +
    r'(?:(?P<status>\n\n✅ (?:Addressed in commit [0-9a-f]{7,40}|Addressed in commits [0-9a-f]{7,40} to [0-9a-f]{7,40}'
    r'|Confirmed as addressed by @[A-Za-z0-9-]+(?:\[bot\])?))'
    r'(?P<again>\n\n' + re.escape(FOOTER.format('comment')) + r')?)?\Z')


def sha12(text):
    return hashlib.sha256(text.encode()).hexdigest()[:12]


def forms(body, author):
    """The body's forms, its first one first; only the body itself when it has no other."""
    body = body or ''
    m = TAIL.search(body) if author == CODERABBIT else None
    if not m:
        return [body]
    head, status, again = body[:m.start()], m['status'] or '', m['again'] or ''
    out = []
    for tail in ('', status, status + again):
        for word in ('comment', 'reply'):
            f = head + FOOTER.format(word) + tail
            if f not in out:
                out.append(f)
    return out


def digests(body, author):
    """The body's digest, then its aliases."""
    return [sha12(f) for f in forms(body, author)]
