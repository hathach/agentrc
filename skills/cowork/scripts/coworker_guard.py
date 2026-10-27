#!/usr/bin/env python3
"""PreToolUse hook on Bash, declared in agents/coworker.md so it runs wherever that agent does: every command
the relay runs gets COWORK_READ_ONLY exported first, so cowork.py keeps send, reset and kill to read-only lanes,
checked under each lane's lock, whatever flags the relay passed. A command naming the marker is denied, so the
relay cannot clear it by mistake. A guard against a relay's mistakes, not isolation. Exit 2 denies; Claude Code
lets an erroring hook pass, which would run the command unmarked, so any error denies too.
"""
import json
import os
import sys

MARKER = 'COWORK_READ_ONLY'


def crashed(kind, value, tb):
    try:
        print(f'coworker guard: the check failed ({value!a}), so the call is denied', file=sys.stderr)
        sys.stderr.flush()
    finally:
        os._exit(2)


def main():
    tool_input = json.load(sys.stdin.buffer)['tool_input']
    command = tool_input['command']
    if MARKER in command:
        print(f'coworker guard: {MARKER} is this hook\'s to set; run the command without naming it', file=sys.stderr)
        return 2
    print(json.dumps({'hookSpecificOutput': {
        'hookEventName': 'PreToolUse',
        'updatedInput': {**tool_input, 'command': f'export {MARKER}=1\n{command}'}}}))
    return 0


if __name__ == '__main__':
    sys.excepthook = crashed
    sys.exit(main())
