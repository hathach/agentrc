#!/usr/bin/env python3
"""PreToolUse hook on Bash, declared in agents/coworker.md so it runs wherever that agent does: every command
the relay runs gets COWORK_READ_ONLY exported first, so cowork.py keeps send, reset and kill to read-only lanes,
checked under each lane's lock, whatever flags the relay passed. The marker is also made readonly, so a later
unset or assignment in the same shell fails and leaves it set. A guard against a relay's mistakes, not isolation:
deliberately unexporting it (env -u, export -n, an unset in a child shell) gets past it. Exit 2 denies; Claude Code
lets an erroring hook pass, which would run the command unmarked, so any error denies too.
"""
import json
import os
import sys

MARKER = 'COWORK_READ_ONLY'
PREFIX = f'export {MARKER}=1; readonly {MARKER}'


def crashed(kind, value, tb):
    try:
        print(f'coworker guard: the check failed ({value!a}), so the call is denied', file=sys.stderr)
        sys.stderr.flush()
    finally:
        os._exit(2)


def main():
    tool_input = json.load(sys.stdin.buffer)['tool_input']
    command = tool_input['command']
    print(json.dumps({'hookSpecificOutput': {
        'hookEventName': 'PreToolUse',
        'updatedInput': {**tool_input, 'command': f'{PREFIX}\n{command}'}}}))
    return 0


if __name__ == '__main__':
    sys.excepthook = crashed
    sys.exit(main())
