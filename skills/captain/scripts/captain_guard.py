#!/usr/bin/env python3
"""PreToolUse hook on every tool, declared in agents/captain.md so it runs wherever that agent does, on any
account: captain relays, reports and checks, so its own calls may run the installed captain.py with a known
subcommand or captain_read.py with a lookup it accepts, read the places sessions leave evidence, write its
own memory, load its skill and Monitor, and ask the human; every other call is denied.

Only captain's calls are checked, as a backstop to the declaration: the payload names agent_type `captain`,
whether captain runs as the session's agent or as another session's subagent. An allowlist, so a tool or command
added later is denied until it is named here. A guard against mistakes and drift, not isolation: captain.py itself
runs herdr and types into other panes, and the checks in it stay the authority. Exit 2 denies; Claude Code lets an
erroring hook pass, so any error denies too.
"""
import json
import os
import re
import sys


def crashed(kind, value, tb):
    """Claude Code lets a hook that errors pass: any uncaught exception denies instead."""
    try:
        print(f'captain guard: the check failed ({value!a}), so the call is denied', file=sys.stderr)
        sys.stderr.flush()
    finally:
        os._exit(2)


sys.path.insert(0, os.path.dirname(os.path.abspath(__file__)))
try:
    import captain_read
except BaseException as err:  # the hook has no excepthook yet, and an erroring hook passes
    crashed(type(err), err, None)

STATE, SKILL, MEMORY, PROJECTS = captain_read.STATE, captain_read.SKILL, captain_read.MEMORY, captain_read.PROJECTS
# the home-expanded form takes forward slashes: a Windows backslash is an escape in the Bash tool's shell
SCRIPTS = ('~/.claude/skills/captain/scripts/captain.py',
           os.path.join(SKILL, 'scripts', 'captain.py').replace(os.sep, '/'))
READERS = tuple(s.replace('captain.py', 'captain_read.py') for s in SCRIPTS)
SUBCOMMANDS = ('list', 'enroll', 'ask', 'collect', 'reports', 'cancel', 'relay', 'withdraw', 'open')
TOKEN = re.compile(r'[A-Za-z0-9][A-Za-z0-9:._=-]*|--[a-z][a-z-]*')
CHECKOUT = re.compile(r'(?:/|~/)[A-Za-z0-9._/-]*')   # a path, only as the value of open's --checkout
SHELL = re.compile(r'[^A-Za-z0-9 :._=/~,-]')   # any quoting, expansion, redirection, separator or newline


def command(cmd):
    """Why a Bash or Monitor command is not a plain captain.py call, or None."""
    if not isinstance(cmd, str) or SHELL.search(cmd):
        return 'only a plain `python3 ~/.claude/skills/captain/scripts/captain.py|captain_read.py ...` runs here'
    words = cmd.split()
    if len(words) >= 2 and words[0] == 'python3' and words[1] in READERS:
        return captain_read.refusal(words[2:])
    if len(words) < 3 or words[0] != 'python3' or words[1] not in SCRIPTS:
        return ('only the installed captain.py or captain_read.py runs here, as '
                '`python3 ~/.claude/skills/captain/scripts/<script>`')
    if words[2] not in SUBCOMMANDS:
        return f'captain.py {words[2]} is not a captain subcommand ({", ".join(SUBCOMMANDS)})'
    args = words[3:]
    bad = [w for i, w in enumerate(args)
           if not (TOKEN.fullmatch(w) or (i and args[i - 1] == '--checkout' and CHECKOUT.fullmatch(w)
                                          and '..' not in w.split('/')))]
    if bad:
        return f'unexpected argument {bad[0]!r}'
    return None


def verdict(payload):
    """Why captain may not make this call, or None."""
    tool, args = payload.get('tool_name'), payload.get('tool_input') or {}
    if tool == 'Bash':
        return command(args.get('command'))
    if tool == 'Monitor':
        return 'Monitor here watches a captain.py command only' if 'ws' in args else command(args.get('command'))
    if tool == 'Read':
        if captain_read.readable(args.get('file_path')):
            return None
        return 'captain reads only checkouts, chief runs and memory; captain_read.py list shows a directory'
    if tool in ('Write', 'Edit'):
        path = args.get('file_path')
        if (isinstance(path, str) and os.path.isabs(path) and path.endswith('.md')
                and captain_read.inside(path, MEMORY)):
            return None
        return f'captain writes only Markdown in its memory, {MEMORY}'
    if tool == 'Skill':
        return None if args.get('skill') == 'captain' else 'captain loads only its own skill'
    if tool == 'ToolSearch':
        return None if args.get('query') == 'select:Monitor' else 'captain loads only Monitor'
    if tool == 'AskUserQuestion':
        return None
    return f'captain relays and reports only; {tool} is not one of its tools'


def main():
    payload = json.load(sys.stdin.buffer)
    if payload.get('agent_type') != 'captain':
        return 0
    why = verdict(payload)
    if why:
        print(f'captain guard: {why}', file=sys.stderr)
        return 2
    return 0


if __name__ == '__main__':
    sys.excepthook = crashed
    sys.exit(main())
