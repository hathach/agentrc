#!/usr/bin/env python3
"""PreToolUse hook on every tool, declared in agents/captain.md so it runs wherever that agent does, on any
account: captain only relays and reports, so its own calls may run the installed captain.py with a known
subcommand, read its state and skill, load its skill and Monitor, and ask the human; every other call is denied.

Only captain's own calls are checked, as a backstop to the declaration: the payload names agent_type `captain`
and carries no agent_id, which every subagent call does. An allowlist, so a tool or command added later is
denied until it is named here. A guard against mistakes and drift, not isolation: captain.py itself runs herdr
and types into other panes, and the checks in it stay the authority. Exit 2 denies; Claude Code lets an erroring
hook pass, so any error denies too.
"""
import json
import os
import re
import shlex
import sys


def crashed(kind, value, tb):
    """Claude Code lets a hook that errors pass: any uncaught exception denies instead."""
    try:
        print(f'captain guard: the check failed ({value!a}), so the call is denied', file=sys.stderr)
        sys.stderr.flush()
    finally:
        os._exit(2)


SKILL = os.path.expanduser('~/.claude/skills/captain')
SCRIPTS = ('~/.claude/skills/captain/scripts/captain.py', os.path.join(SKILL, 'scripts', 'captain.py'))
STATE = os.path.expanduser('~/.cache/agentrc/captain')
SUBCOMMANDS = ('list', 'enroll', 'ask', 'collect', 'reports', 'cancel', 'relay', 'withdraw', 'open')
TOKEN = re.compile(r'[A-Za-z0-9][A-Za-z0-9:._=-]*|--[a-z][a-z-]*')
CHECKOUT = re.compile(r'(?:/|~/)[A-Za-z0-9._/-]*')   # a path, only as the value of open's --checkout
SHELL = re.compile(r'[^A-Za-z0-9 :._=/~-]')   # any quoting, expansion, redirection, separator or newline


def command(cmd):
    """Why a Bash or Monitor command is not a plain captain.py call, or None."""
    if not isinstance(cmd, str) or SHELL.search(cmd):
        return 'only a plain `python3 ~/.claude/skills/captain/scripts/captain.py <subcommand> ...` runs here'
    words = shlex.split(cmd)
    if len(words) < 3 or words[0] != 'python3' or words[1] not in SCRIPTS:
        return 'only the installed captain.py runs here, as `python3 ~/.claude/skills/captain/scripts/captain.py`'
    if words[2] not in SUBCOMMANDS:
        return f'captain.py {words[2]} is not a captain subcommand ({", ".join(SUBCOMMANDS)})'
    args = words[3:]
    bad = [w for i, w in enumerate(args)
           if not (TOKEN.fullmatch(w) or (i and args[i - 1] == '--checkout' and CHECKOUT.fullmatch(w)
                                          and '..' not in w.split('/')))]
    if bad:
        return f'unexpected argument {bad[0]!r}'
    return None


def inside(path, root):
    real, root = os.path.realpath(path), os.path.realpath(root)
    return real == root or real.startswith(root + os.sep)


def verdict(payload):
    """Why captain may not make this call, or None."""
    tool, args = payload.get('tool_name'), payload.get('tool_input') or {}
    if tool == 'Bash':
        return command(args.get('command'))
    if tool == 'Monitor':
        return 'Monitor here watches a captain.py command only' if 'ws' in args else command(args.get('command'))
    if tool == 'Read':
        path = args.get('file_path')
        if isinstance(path, str) and os.path.isabs(path) and (inside(path, STATE) or inside(path, SKILL)):
            return None
        return 'captain reads only its state and its skill'
    if tool == 'Skill':
        return None if args.get('skill') == 'captain' else 'captain loads only its own skill'
    if tool == 'ToolSearch':
        return None if args.get('query') == 'select:Monitor' else 'captain loads only Monitor'
    if tool == 'AskUserQuestion':
        return None
    return f'captain relays and reports only; {tool} is not one of its tools'


def main():
    payload = json.load(sys.stdin.buffer)
    if payload.get('agent_type') != 'captain' or payload.get('agent_id'):
        return 0
    why = verdict(payload)
    if why:
        print(f'captain guard: {why}', file=sys.stderr)
        return 2
    return 0


if __name__ == '__main__':
    sys.excepthook = crashed
    sys.exit(main())
