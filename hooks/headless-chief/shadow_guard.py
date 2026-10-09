#!/usr/bin/env python3
"""Claude Code PreToolUse hook on Workflow and Agent: deny both while the session's project
carries a `.claude` entry that would shadow agentrc's installed copy (#59), so an interactive
chief in a stale worktree cannot run it.

A workflow's own agent() dispatches never reach a hook (measured on 2.1.294), so any stale
entry denies every Workflow and Agent call, not only the one it names. The chief prompt itself
loads before any tool call and is not covered. Exit 2 denies; Claude Code lets a hook that
errors proceed, so an entry it cannot read denies too.
"""
import json
import sys
from pathlib import Path

sys.path.insert(0, str(Path(__file__).resolve().parents[2] / 'skills' / 'headless-chief' / 'scripts'))
from shadows import project, refusal  # noqa: E402


def main():
    why = refusal(project(json.load(sys.stdin))[0], 'restart the session')
    if why:
        print(why, file=sys.stderr)
        return 2
    return 0


if __name__ == '__main__':
    sys.exit(main())
