#!/usr/bin/env python3
"""Claude Code PreToolUse hook on Bash and Write: chief's own calls may read, run compact chores and
write its artifacts outside the task worktree; this denies the commands that would make chief a
writer of the worktree or a publisher by mistake.

Only chief's own calls are checked: the payload names agent_type `chief` and carries no agent_id,
which every subagent call does (measured on 2.1.295). A guard against mistakes, not isolation: a
script chief runs may still write or publish, and the grants in agents/chief.md stay the authority.
A write target passes only as a literal absolute path outside the worktree, so no shell state
needs tracking; chief's own directory, `git rev-parse --git-path chief`, passes even inside a
primary checkout. Exit 2 denies; a command it cannot parse is denied too, since Claude Code lets
an erroring hook pass.
"""
import json
import sys

if __name__ == '__main__':
    # every session's Bash and Write reach this hook: leave before importing the rest
    PAYLOAD = json.load(sys.stdin)
    if PAYLOAD.get('agent_type') != 'chief' or PAYLOAD.get('agent_id'):
        sys.exit(0)

import os  # noqa: E402
import re  # noqa: E402
import shlex  # noqa: E402
from pathlib import Path  # noqa: E402

sys.path.insert(0, str(Path(__file__).resolve().parents[2] / 'skills' / 'headless-chief' / 'scripts'))
from shadows import project  # noqa: E402

GIT_READ = {
    'status', 'log', 'diff', 'show', 'rev-parse', 'rev-list', 'ls-files', 'ls-tree', 'ls-remote',
    'merge-base', 'cat-file', 'describe', 'blame', 'grep', 'shortlog', 'for-each-ref', 'name-rev',
    'show-ref', 'show-branch', 'count-objects', 'fetch', 'whatchanged', 'range-diff', 'cherry',
    'check-ignore', 'check-attr', 'diff-tree', 'diff-index', 'diff-files', 'var', 'help', 'version',
}
GIT_GLOBAL_WITH_VALUE = {'-C', '-c', '--git-dir', '--work-tree', '--namespace', '--exec-path', '--config-env'}
# subcommand -> its read-only first words; bare, all but stash list
GIT_READ_FORMS = {
    'stash': {'list', 'show'}, 'worktree': {'list'}, 'notes': {'list', 'show'}, 'reflog': {'show'},
    'submodule': {'status'}, 'remote': {'show', 'get-url', '-v', '--verbose'},
}
LISTING = {'-l', '--list', '--contains', '--no-contains', '--merged', '--no-merged', '--points-at', '-n'}
BRANCH_WRITE = {'-d', '-D', '--delete', '-m', '-M', '--move', '-c', '-C', '--copy', '-f', '--force', '-u',
                '--set-upstream-to', '--unset-upstream', '--edit-description', '-t', '--track', '--no-track'}
TAG_WRITE = {'-a', '--annotate', '-s', '--sign', '-u', '--local-user', '-d', '--delete', '-f', '--force',
             '-m', '--message', '-F', '--file', '-e', '--edit'}
CONFIG_READ = {'--get', '--get-all', '--get-regexp', '--get-urlmatch', '--list', '-l', 'get', 'list'}
GH_READ = {
    'pr': {'view', 'list', 'checks', 'diff', 'status'}, 'issue': {'view', 'list', 'status'},
    'run': {'view', 'list', 'watch'}, 'workflow': {'view', 'list'}, 'repo': {'view', 'list'},
    'release': {'view', 'list'}, 'label': {'list'}, 'cache': {'list'}, 'ruleset': {'view', 'list'},
    'auth': {'status'}, 'search': None, 'status': None,
}
GH_WITH_VALUE = {'-R', '--repo', '-H', '--header', '-q', '--jq', '-t', '--template', '--hostname', '-p', '--preview'}
# workflow-only publishers: Sonar marking rides markSonar, the PR push rides autoPush
WORKFLOW_ONLY = {'push.py', 'sonar.py'}
# command prefixes that run the next word: name -> its options that take a value
WRAPPERS = {
    'command': set(), 'builtin': set(), 'nohup': set(), 'exec': {'-a'}, 'time': {'-f', '-o'},
    'stdbuf': {'-i', '-o', '-e'}, 'nice': {'-n'}, 'timeout': {'-s', '--signal', '-k', '--kill-after'},
    'sudo': {'-u', '-g'}, 'env': {'-u', '--unset', '-C', '--chdir'},
    'xargs': {'-n', '-L', '-P', '-s', '-I', '-d', '-E', '-a', '--max-args', '--max-lines', '--max-procs',
              '--max-chars', '--delimiter', '--eof', '--arg-file'},
}
SHELLS = {'sh', 'bash', 'zsh', 'dash'}
KEYWORDS = {'if', 'then', 'else', 'elif', 'do', 'while', 'until', '!', '{', '}'}
# file writers: every named path counts, only the last for a copy, all but the first for a mode change
WRITERS_ALL = {'rm', 'rmdir', 'mv', 'touch', 'mkdir', 'truncate', 'tee', 'shred', 'unlink'}
WRITERS_LAST = {'cp', 'ln', 'install', 'rsync'}
WRITERS_AFTER_FIRST = {'chmod', 'chown', 'chgrp'}
SEPARATORS = {';', '&&', '||', '|', '&', '(', ')', '|&', ';;'}
REDIRECTS = {'>', '>>', '&>', '&>>', '>|', '>&'}
STREAMS = REDIRECTS | {'<', '<<', '<<<'}
OPERATORS = sorted(SEPARATORS | STREAMS, key=len, reverse=True)
PUNCTUATION = ';&|<>()'
# a quoted or escaped operator character stays a word character: these stand in for it until a path is judged
QUOTED = {c: chr(0xE000 + i) for i, c in enumerate(PUNCTUATION)}
UNQUOTED = str.maketrans({v: k for k, v in QUOTED.items()})
HEREDOC = re.compile(r"<<-?[ \t]*(['\"]?)(\w+)\1([^\n]*)\n(.*?)\n[ \t]*\2[ \t]*(?=\n|$)", re.S)
ASSIGNMENT = re.compile(r'[A-Za-z_][A-Za-z0-9_]*=')
UNKNOWN = '\0'


class Denied(Exception):
    pass


def substitutions(text, quotes=True):
    """(text with each command substitution outside single quotes replaced by an unresolved word and each
    quoted or escaped operator character by its stand-in and each comment dropped, the substitutions' bodies).
    quotes=False reads an unquoted heredoc body, where quotes are plain characters."""
    out, bodies, i, single, double = [], [], 0, False, False
    while i < len(text):
        c = text[i]
        if c == '\\' and not single:
            out.append(c + QUOTED.get(text[i + 1:i + 2], text[i + 1:i + 2]))
            i += 2
            continue
        if quotes and c == "'" and not double:
            single = not single
        elif quotes and c == '"' and not single:
            double = not double
        elif not single and text.startswith('$(', i) and not text.startswith('$((', i):
            depth, j = 1, i + 2
            while j < len(text) and depth:
                depth += {'(': 1, ')': -1}.get(text[j], 0)
                j += 1
            bodies.append(text[i + 2:j - 1])
            out.append(UNKNOWN)
            i = j
            continue
        elif not single and c == '`':
            j = text.find('`', i + 1)
            j = len(text) if j < 0 else j
            bodies.append(text[i + 1:j])
            out.append(UNKNOWN)
            i = j + 1
            continue
        elif quotes and not (single or double) and c == '#' and (not out or out[-1][-1:] in ' \t\n;&|()'):
            i = text.find('\n', i)  # a comment, to its newline: its quotes and substitutions are text
            i = len(text) if i < 0 else i
            continue
        elif (single or double) and c in QUOTED:
            c = QUOTED[c]
        out.append(c)
        i += 1
    return ''.join(out), bodies


def operators(token):
    """shlex joins adjacent punctuation, `);` or `)&&`, into one token: split it into shell operators."""
    if not token or not set(token) <= set(PUNCTUATION):
        return [token]
    out = []
    while token:
        op = next((o for o in OPERATORS if token.startswith(o)), token[0])
        out.append(op)
        token = token[len(op):]
    return out


class Guard:
    def __init__(self, payload):
        self.payload, self.root = payload, None

    def inside(self, path):
        """Is this absolute path in the worktree, chief's own directory apart; the worktree is read once, on first use."""
        if self.root is None:
            root, chief = project(self.payload)
            self.root, self.chief = root.resolve(), chief and chief.resolve()
        p = Path(path).resolve()
        return p.is_relative_to(self.root) and not (self.chief and p.is_relative_to(self.chief))

    def writes(self, path, why):
        path = path.translate(UNQUOTED)
        if path == '/dev/null':
            return
        if UNKNOWN in path or '$' in path or not os.path.isabs(path) or self.inside(path):
            raise Denied(f'{why} would write {path}: chief writes only its artifacts, named by a literal absolute path '
                         'outside the task worktree; a worktree change goes to a writer')

    def command(self, command, depth=0):
        if depth > 3:
            raise Denied('commands nested this deep are not checked; run them as plain words, or dispatch them to a unit')
        bodies = []

        def heredoc(m):
            if not m.group(1):  # an unquoted body still runs its substitutions
                bodies.extend(substitutions(m.group(4), quotes=False)[1])
            return '<<HEREDOC' + m.group(3) + '\n'
        text, subs = substitutions(HEREDOC.sub(heredoc, command))
        for body in bodies + subs:
            self.command(body, depth + 1)
        lex = shlex.shlex(text.replace('\n', ' ; '), posix=True, punctuation_chars=PUNCTUATION)
        lex.whitespace_split = True
        lex.commenters = ''  # substitutions() dropped the comments; a # inside a word is data
        try:
            tokens = list(lex)
        except ValueError as e:
            raise Denied(f'cannot parse this command ({e}); run it as plain words, or dispatch it to a unit')
        seg = []
        for t in [o for t in tokens for o in operators(t)] + [';']:
            if t in SEPARATORS:
                if seg:
                    self.segment(seg, depth)
                seg = []
            else:
                seg.append(t)

    def segment(self, words, depth):
        for k, w in enumerate(words[:-1]):
            if w in REDIRECTS and not (w == '>&' and words[k + 1].isdigit()):
                self.writes(words[k + 1], 'the redirect')
        words = unwrap([w for k, w in enumerate(words) if w not in STREAMS and not (k and words[k - 1] in STREAMS)])
        if not words:
            return
        cmd, args = os.path.basename(words[0]), words[1:]
        if cmd in SHELLS:
            for k, a in enumerate(args):
                if re.fullmatch(r'-[a-zA-Z]*c[a-zA-Z]*', a):
                    return self.command((args[k + 1] if k + 1 < len(args) else '').translate(UNQUOTED), depth + 1)
        if cmd == 'eval':
            return self.command(' '.join(args).translate(UNQUOTED), depth + 1)
        if cmd == 'git':
            self.git(args)
        elif cmd == 'gh':
            gh(args)
        elif cmd in WORKFLOW_ONLY or (cmd.startswith('python') and any(os.path.basename(a) in WORKFLOW_ONLY for a in args)):
            raise Denied('this publisher runs only inside its workflow (autoPush, markSonar)')
        elif cmd in ('sed', 'perl') and any(re.match(r'-[a-zA-Z]*i|--in-place', a) for a in args):
            raise Denied(f'`{cmd}` in place edits a file; chief writes its artifacts whole with Write, and a worktree change goes to a writer')
        for path in written(cmd, args):
            self.writes(path, f'`{cmd}`')

    def git(self, args):
        i = 0
        while i < len(args) and args[i].startswith('-'):
            i += 2 if args[i] in GIT_GLOBAL_WITH_VALUE else 1
        if i >= len(args):
            return
        sub, rest = args[i], args[i + 1:]
        for k, a in enumerate(rest):
            out = a.split('=', 1)[1] if a.startswith('--output=') else rest[k + 1] if a == '--output' and k + 1 < len(rest) else None
            if out is not None:
                self.writes(out, f'`git {sub} --output`')
        if sub in GIT_READ:
            return
        flags = {a.split('=')[0] for a in rest if a.startswith('-')}
        positional = [a for a in rest if not a.startswith('-')]
        if sub in GIT_READ_FORMS:
            if (not rest and sub != 'stash') or (rest and rest[0] in GIT_READ_FORMS[sub]):
                return
        elif sub == 'branch':
            if not flags & BRANCH_WRITE and (not positional or flags & (LISTING | {'--format'})):
                return
        elif sub == 'tag':
            if not flags & TAG_WRITE and (not positional or flags & LISTING):
                return
        elif sub == 'config':
            if flags & CONFIG_READ or (positional and positional[0] in CONFIG_READ):
                return
        elif sub == 'symbolic-ref':
            if len(positional) <= 1 and not flags & {'-d', '--delete'}:
                return
        raise Denied(f'`git {sub}` changes the repository; chief only reads it: dispatch a writer, or the workflow that owns the step')


def unwrap(words):
    """The command a word list runs, past assignments, keywords and wrappers with their options."""
    while words:
        w = words[0]
        if ASSIGNMENT.match(w) or w in KEYWORDS:
            words = words[1:]
        elif w == 'command' and any(re.fullmatch(r'-[pvV]*[vV][pvV]*', o) for o in options(words[1:])):
            return []  # looks the names up, runs none
        elif w in WRAPPERS:
            words = words[1:]
            while words and (words[0].startswith('-') or (w == 'env' and '=' in words[0])):
                words = words[2:] if words[0] in WRAPPERS[w] else words[1:]
            if w == 'timeout' and words:
                words = words[1:]  # the duration
        else:
            break
    return words


def options(words):
    """The leading options of a word list, up to the first operand or `--`."""
    out = []
    for w in words:
        if w == '--' or not w.startswith('-'):
            break
        out.append(w)
    return out


def gh(args):
    words, i = [], 0
    while i < len(args):
        if args[i] in GH_WITH_VALUE:
            i += 2
            continue
        if not args[i].startswith('-'):
            words.append(args[i])
        i += 1
    if not words:
        return
    group = words[0]
    if group == 'api':
        method = None
        for k, a in enumerate(args):
            if a in ('-X', '--method') and k + 1 < len(args):
                method = args[k + 1]
            elif a.startswith('--method='):
                method = a.split('=', 1)[1]
            elif a.startswith('-X') and len(a) > 2:
                method = a[2:]
        # -f/-F, attached or not, send fields, which make gh default to POST
        fields = any(re.match(r'-[fF]|--(raw-)?field\b|--input\b', a) for a in args)
        if 'graphql' in words[1:2]:
            if any(re.search(r'\bmutation\b', a, re.I) for a in args):
                raise Denied('a GraphQL mutation publishes; it goes through the workflow or the unit holding the grant')
            return
        if (method or ('POST' if fields else 'GET')).upper() != 'GET':
            raise Denied('`gh api` with a non-GET method (or fields without -X GET) publishes; it goes through the workflow or the unit holding the grant')
        return
    verbs = GH_READ.get(group, set())
    if verbs is None or (len(words) > 1 and words[1] in verbs):
        return
    raise Denied(f'`gh {" ".join(words[:2])}` is not a read; publishing goes through the workflow or the unit holding the grant')


def written(cmd, args):
    """The paths a file-writing command names as its targets."""
    names = [a for a in args if not a.startswith('-')]
    if cmd in WRITERS_LAST:
        # -t/--target-directory names the destination where the command has it (rsync's -t keeps times)
        target = [] if cmd == 'rsync' else \
            [args[k + 1] for k, a in enumerate(args[:-1]) if a in ('-t', '--target-directory')] + \
            [a.split('=', 1)[1] for a in args if a.startswith('--target-directory=')] + \
            [a[2:] for a in args if a.startswith('-t') and len(a) > 2 and not a.startswith('--')]
        return target or names[-1:]
    if cmd in WRITERS_AFTER_FIRST:
        # --reference takes the place of the mode or owner: every operand is a target
        return names if any(a.startswith('--reference') for a in args) else names[1:]
    return names if cmd in WRITERS_ALL else []


def main(payload):
    guard = Guard(payload)
    data = payload.get('tool_input') or {}
    try:
        if payload.get('tool_name') == 'Write':
            guard.writes(data.get('file_path', ''), 'this Write')
        elif payload.get('tool_name') == 'Bash':
            guard.command(data.get('command', ''))
    except Denied as e:
        print(f'chief guard: {e}', file=sys.stderr)
        return 2
    return 0


if __name__ == '__main__':
    sys.exit(main(PAYLOAD))
