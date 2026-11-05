#!/usr/bin/env python3
"""Claude Code hook (SessionStart after a compaction or /clear, and Stop): join the captain's open watch when it
covers this session and hand the session its brief; `captain.hook` decides. With no watch open it reads one marker
file and prints nothing. Any failure exits 0 with a note on stderr: this hook never stops a session's own work, only
its deliberate Stop block does."""
import json
import sys
from pathlib import Path


def main():
    try:
        sys.path.insert(0, str(Path(__file__).resolve().parents[2] / 'skills' / 'captain' / 'scripts'))
        import captain
        out = captain.hook(json.load(sys.stdin))
    except BaseException as e:   # SystemExit included: a refusal deep in captain.py must not fail the hook
        print(f'captain hook: {e!r}', file=sys.stderr)
        return 0
    if out:
        print(out)
    return 0


if __name__ == '__main__':
    sys.exit(main())
