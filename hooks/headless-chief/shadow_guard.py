#!/usr/bin/env python3
"""Claude Code PreToolUse hook on Workflow and Agent: deny both while the session's project
carries a `.claude` entry that would shadow agentrc's installed copy (#59), so an interactive
chief in a stale worktree cannot run it.

A workflow's own agent() dispatches never reach a hook (measured on 2.1.294), so any stale
entry denies every Workflow and Agent call, not only the one it names. The chief prompt itself
loads before any tool call and is not covered. Exit 2 denies; Claude Code lets a hook that
errors proceed, so an entry it cannot read, or any other error, denies too.
"""
import json
import os
import sys
from pathlib import Path


def crashed(kind, value, tb):
    """Claude Code lets a hook that errors pass: any uncaught exception denies instead."""
    try:
        print(f'shadow guard: the check failed ({value!a}), so the call is denied', file=sys.stderr)
        sys.stderr.flush()
    finally:
        os._exit(2)


if __name__ == '__main__':
    sys.excepthook = crashed

sys.path.insert(0, str(Path(__file__).resolve().parents[2] / 'skills' / 'headless-chief' / 'scripts'))
from shadows import project, refusal  # noqa: E402


def main():
    why = refusal(project(json.load(sys.stdin.buffer), timeout=6)[0], 'restart the session')   # a hung git denies, not times out
    if why:
        print(why, file=sys.stderr)
        return 2
    return 0


if __name__ == '__main__':
    if os.name == 'nt':
        sys.stderr.reconfigure(encoding='utf-8', errors='backslashreplace')   # the reason names paths, whatever the locale
    sys.exit(main())
