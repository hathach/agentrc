#!/usr/bin/env python3
"""Check the files a fix group may edit or create, before a writer is given them.

  scope_check.py --path P...

Pass values as --path=P when one may start with a dash. Run from the checkout's
top level. Each path is model output, a validator's or a scope step's list of
what a fix must change, and is accepted as one of:

  existing  a tracked regular file, named literally;
  new       nothing there yet, reached through real directories inside the
            checkout, its nearest existing ancestor a directory that holds
            tracked files, and neither it nor an ancestor ignored: the file a
            writer creates, which staging can then publish.

Anything else is refused with its reason. stdout ends with one JSON line
{accepted: [{path, kind}], refused: [{path, why}], seal} (seal: facts.sealed),
in the order given; exit 0 with that line, exit 2 with {"error": ...} when the
checkout cannot be read.
"""

import os
import sys
from pathlib import Path

sys.path.insert(0, str(Path(__file__).parent))
from facts import Parser, Unusable, checkout_top, git, report, run  # noqa: E402


def tracked(path):
    """Tracked files at or under path ('' for the whole checkout), literally."""
    return [p for p in git('--literal-pathspecs', 'ls-files', '-z', '--', path or '.').split('\0') if p]


def ignored(path):
    # check-ignore takes no literal magic; a leading ./ keeps a name starting with ':' from reading as magic.
    # It also reports a path under an ignored directory, a negated pattern there included.
    return run('git', 'check-ignore', '-q', '--no-index', '--', f'./{path}', ok=(0, 1))[0] == 0


def classify(top, path):
    """(kind, None) or (None, why)."""
    parts = path.split('/')
    # on Windows a drive (C:), a backslash or an NTFS stream (a.c:x) would name a file outside the plain path
    if (not path or path.startswith('/') or any(p in ('', '.', '..') for p in parts)
            or (os.name == 'nt' and any(c in path for c in '\\:'))):
        return None, 'not a plain repository-relative path'
    full = os.path.join(top, path)
    # a path through a symbolic link is one git will not stage, wherever the link points
    if os.path.realpath(full) != os.path.normpath(full):
        return None, 'reached through a symbolic link'
    if os.path.lexists(full):
        if not os.path.isfile(full):
            return None, 'not a regular file'
        if tracked(path) != [path]:
            return None, 'exists but is not tracked'
        # without core.symlinks (Windows) a symlink is checked out as a file holding its target: editing it retargets it
        if os.name == 'nt' and any(e.split(' ')[0] not in ('100644', '100755')
                                   for e in git('--literal-pathspecs', 'ls-files', '-s', '-z', '--', path).split('\0') if e):
            return None, 'not a regular file in the index'
        return 'existing', None
    ancestor = next(i for i in range(len(parts) - 1, -1, -1) if os.path.lexists(os.path.join(top, *parts[:i])))
    base = '/'.join(parts[:ancestor])
    if not os.path.isdir(os.path.join(top, base)):
        return None, f'{base} is not a directory'
    if not tracked(base):
        return None, f'{base or "the checkout"} holds no tracked file'
    if ignored(path):
        return None, 'ignored'
    return 'new', None


def collect(argv):
    p = Parser(prog='scope_check.py')
    p.add_argument('--path', action='append', required=True)
    a = p.parse_args(argv)
    top = os.path.realpath(checkout_top())
    accepted, refused = [], []
    for path in a.path:
        kind, why = classify(top, path)
        if kind:
            accepted.append({'path': path, 'kind': kind})
        else:
            refused.append({'path': path, 'why': why})
    return {'accepted': accepted, 'refused': refused}


if __name__ == '__main__':
    sys.exit(report(collect, sys.argv[1:], seal=True))
