"""Text we sent to GitHub as gh reads it back, for a script that compares a comment it posted with the live one. Stdlib
only.

gh sanitizes every JSON response before anything reads it (go-gh v2.17.0's asciisanitizer, JSON mode, in gh 2.103.3):
each C0 or C1 control escape `\\u00XX` in the JSON text, one a literal backslash precedes included, and each raw C1
character become caret notation. A body we sent with the text `\\u001b` so reads back `\\^[` (#461). The rewrite is
not idempotent: only our outgoing text is mapped, never text already read.

A script reaches it through its resolved path, as it reaches lib/windows.py:
`sys.path.insert(0, str(Path(__file__).resolve().parents[N] / 'lib'))`, then `import gh_reads`."""
import json


def _caret(k):
    return '^\\\\' if k == 28 else '^' + chr(k + 64)


CARET = {c: _caret(c) for c in range(32) if c not in (9, 10, 11, 13)} | {0x80 + k: _caret(k) for k in range(32)}
CARET_ESCAPE = {f'{c:02x}': s for c, s in CARET.items()}


def as_gh_reads(text):
    """`text`, which GitHub stores as sent, as gh returns it: the sanitizer run over the text's JSON encoding, short
    forms for \\b \\f \\n \\r \\t and `\\u00XX` for the other control characters, as Ruby's generator writes them
    (GitHub's own \\b and \\f unobserved)."""
    src, out, i, escape = json.dumps(text, ensure_ascii=False), [], 0, False
    while i < len(src):
        ch = src[i]
        if ord(ch) in CARET:
            out.append(CARET[ord(ch)])
            i += 1
        elif src.startswith('\\u00', i) and src[i + 4:i + 6].lower() in CARET_ESCAPE:
            # A backslash pair's second half starts the escape: one backslash more keeps the JSON valid.
            out.append(('\\' if escape else '') + CARET_ESCAPE[src[i + 4:i + 6].lower()])
            escape = False
            i += 6
        else:
            out.append(ch)
            escape = not escape if ch == '\\' else False
            i += 1
    return json.loads(''.join(out))


def reads_as_sent(live, sent):
    """Whether `live`, a body as gh read it, is the `sent` text we posted."""
    return live == as_gh_reads(sent)
