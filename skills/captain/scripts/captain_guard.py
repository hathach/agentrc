#!/usr/bin/env python3
"""PreToolUse hook on every tool, declared in agents/captain.md so it runs wherever that agent does, on any
account: captain relays, reports and checks, so its own calls may run the installed captain.py with a known
subcommand or captain_read.py with a lookup it accepts, read the places sessions leave evidence, write its
own memory and drafts, load its skill and Monitor, and ask the human; every other call is denied. Its one
publication: the installed followup-issue publish.py, creating or commenting on a hathach/agentrc follow-up issue
whose title and body it drafted under DRAFTS; whether the human asked for it stays captain's judgment.

Only captain's calls are checked, as a backstop to the declaration: the payload names agent_type `captain`,
whether captain runs as the session's agent or as another session's subagent. An allowlist, so a tool or command
added later is denied until it is named here. A guard against mistakes and drift, not isolation: captain.py itself
runs hc and types into other terminals, and the checks in it stay the authority. Exit 2 denies; Claude Code lets an
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
PUBLISHERS = ('~/.claude/skills/followup-issue/scripts/publish.py',
              os.path.join(captain_read.FOLLOWUP, 'scripts', 'publish.py').replace(os.sep, '/'))
DRAFTS = os.path.join(STATE, 'drafts')
REPO = 'hathach/agentrc'
# publish.py's options for each action, every one given once as `--option value`
PUBLISH = {'create': {'--repo': REPO, '--allow-repo': REPO, '--title-file': None, '--body-file': None,
                      '--label': 'follow-up'},
           'comment': {'--repo': REPO, '--allow-repo': REPO, '--issue': None, '--body-file': None}}
ISSUE = re.compile(r'[1-9][0-9]{0,11}')
SUBCOMMANDS = ('list', 'enroll', 'ask', 'watch', 'watches', 'collect', 'reports', 'cancel', 'relay',
               'queue', 'ingest', 'verdict', 'start')
TOKEN = re.compile(r'[A-Za-z0-9][A-Za-z0-9:._=-]*|--[a-z][a-z-]*')
SHELL = re.compile(r'[^A-Za-z0-9 :._=/~,-]')   # any quoting, expansion, redirection, separator or newline
CHECKOUT = re.compile(r'(?:/|~/)[A-Za-z0-9/._-]*')   # start's one path operand: never ~user
START_OPTIONS = ('--name', '--space', '--tab', '--entry')
START_FLAGS = ('--task',)
ACCOUNTS = ('main', 'ada')


def markdown_in(path, root):
    """A Markdown file inside root, symlinks resolved. No `..`: realpath steps up from a link's target, a tool that
    normalizes the path first from the link itself."""
    return (isinstance(path, str) and os.path.isabs(path) and '..' not in path.replace(os.sep, '/').split('/')
            and os.path.realpath(path).endswith('.md') and captain_read.inside(path, root))


def publication(args):
    """Why args are not one create or comment that PUBLISH describes, or None."""
    if not args or args[0] not in PUBLISH:
        return f'publish.py runs only {" or ".join(PUBLISH)}'
    want, pairs = PUBLISH[args[0]], args[1:]
    names = pairs[::2]
    if len(pairs) % 2 or sorted(names) != sorted(want):
        return f'publish.py {args[0]} takes exactly {" ".join(want)}, each once with its value'
    for name, value in zip(names, pairs[1::2]):
        if name in ('--title-file', '--body-file'):
            if not markdown_in(os.path.expanduser(value), DRAFTS):
                return f'{name} is a Markdown draft in {DRAFTS}'
        elif name == '--issue':
            if not ISSUE.fullmatch(value):
                return '--issue is an issue number'
        elif value != want[name]:
            return f'{name} is {want[name]}'
    return None


def start_args(args):
    """Why args are not one start: a checkout path, an optional account, each option at most once with a value, each
    flag at most once."""
    operands, seen, i = [], set(), 0
    while i < len(args):
        if args[i] in START_FLAGS and args[i] not in seen:
            seen.add(args[i])
            i += 1
        elif args[i].startswith('-'):
            value = args[i + 1] if i + 1 < len(args) else ''
            if args[i] not in START_OPTIONS or args[i] in seen or value.startswith('-') or not TOKEN.fullmatch(value):
                return f'start takes {", ".join(START_OPTIONS)}, each at most once with a value'
            seen.add(args[i])
            i += 2
        else:
            operands.append(args[i])
            i += 1
    if not operands or not CHECKOUT.fullmatch(operands[0]) or '..' in operands[0].split('/'):
        return 'start takes the checkout as an absolute or ~/ path, with no .. in it'
    if len(operands) > 2 or operands[1:] and operands[1] not in ACCOUNTS:
        return f'start takes one checkout and at most an account, {" or ".join(ACCOUNTS)}'
    return None


def command(cmd, monitor=False):
    """Why a Bash or Monitor command is not a plain captain.py call, or None."""
    if not isinstance(cmd, str) or SHELL.search(cmd):
        return 'only a plain `python3 ~/.claude/skills/captain/scripts/captain.py|captain_read.py ...` runs here'
    words = cmd.split()
    if len(words) >= 2 and words[0] == 'python3' and words[1] in READERS:
        return captain_read.refusal(words[2:])
    if len(words) >= 2 and words[0] == 'python3' and words[1] in PUBLISHERS:
        return 'Monitor never publishes' if monitor else publication(words[2:])
    if len(words) < 3 or words[0] != 'python3' or words[1] not in SCRIPTS:
        return ('only the installed captain.py or captain_read.py runs here, as '
                '`python3 ~/.claude/skills/captain/scripts/<script>`')
    if words[2] not in SUBCOMMANDS:
        return f'captain.py {words[2]} is not a captain subcommand ({", ".join(SUBCOMMANDS)})'
    if words[2] == 'start':
        return 'Monitor never starts a session' if monitor else start_args(words[3:])
    bad = [w for w in words[3:] if not TOKEN.fullmatch(w)]
    if bad:
        return f'unexpected argument {bad[0]!r}'
    return None


def verdict(payload):
    """Why captain may not make this call, or None."""
    tool, args = payload.get('tool_name'), payload.get('tool_input') or {}
    if tool == 'Bash':
        return command(args.get('command'))
    if tool == 'Monitor':
        return ('Monitor here watches a captain.py command only' if 'ws' in args
                else command(args.get('command'), monitor=True))
    if tool == 'Read':
        if captain_read.readable(args.get('file_path')):
            return None
        return 'captain reads only checkouts, chief runs and memory; captain_read.py list shows a directory'
    if tool in ('Write', 'Edit'):
        path = args.get('file_path')
        if markdown_in(path, MEMORY) or markdown_in(path, DRAFTS):
            return None
        return f'captain writes only Markdown in its memory, {MEMORY}, or its drafts, {DRAFTS}'
    if tool == 'Skill':
        if args.get('skill') in ('captain', 'followup-issue'):
            return None
        return 'captain loads only its own skill and followup-issue'
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
