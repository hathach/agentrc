#!/usr/bin/env python3
"""Claude Code PreToolUse hook on Bash and Write: chief's own calls may read, run compact chores and
write its artifacts outside the task worktree; this denies the commands that would make chief a
writer of the worktree or a publisher by mistake.

Only chief's own calls are checked: the payload names agent_type `chief` and carries no agent_id,
which every subagent call does (measured on 2.1.295). A guard against mistakes, not isolation: a
script chief runs may still write or publish, and the grants in agents/chief.md stay the authority.
Exit 2 denies; a command it cannot parse is denied too, since Claude Code lets an erroring hook pass.
"""
import json
import os
import re
import shlex
import subprocess
import sys
from pathlib import Path

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
WRAPPERS = {'command', 'builtin', 'exec', 'nohup', 'time', 'stdbuf'}
WRAPPERS_WITH_VALUE = {
    'nice': {'-n'}, 'timeout': {'-s', '--signal', '-k', '--kill-after'}, 'sudo': {'-u', '-g'},
    'env': {'-u', '--unset', '-C', '--chdir'},
    'xargs': {'-n', '-L', '-P', '-s', '-I', '-d', '-E', '-a', '--max-args', '--max-lines', '--max-procs',
              '--max-chars', '--delimiter', '--eof', '--arg-file'},
}
SHELLS = {'sh', 'bash', 'zsh', 'dash'}
# file writers: every named path counts, only the last for a copy, all but the first for a mode change
WRITERS_ALL = {'rm', 'rmdir', 'mv', 'touch', 'mkdir', 'truncate', 'tee', 'shred', 'unlink'}
WRITERS_LAST = {'cp', 'ln', 'install', 'rsync'}
WRITERS_AFTER_FIRST = {'chmod', 'chown', 'chgrp'}
SEPARATORS = {';', '&&', '||', '|', '&', '(', ')', '|&', ';;'}
REDIRECTS = {'>', '>>', '&>', '&>>', '>|', '>&'}
INPUTS = {'<', '<<', '<<<'}
HEREDOC = re.compile(r"<<-?[ \t]*(['\"]?)(\w+)\1([^\n]*)\n(.*?)\n[ \t]*\2[ \t]*(?=\n|$)", re.S)
KEYWORDS = {'if', 'then', 'else', 'elif', 'do', 'while', 'until', '!', '{', '}'}
ASSIGNMENT = re.compile(r'^([A-Za-z_][A-Za-z0-9_]*)=(.*)$', re.S)
VARIABLE = re.compile(r'\$(?:\{([A-Za-z_][A-Za-z0-9_]*)\}|([A-Za-z_][A-Za-z0-9_]*))')
UNKNOWN = '\0'


class Denied(Exception):
    pass


class Tree:
    """The session's task worktree, apart from chief's own directory, `git rev-parse --git-path chief`,
    which sits inside the tree in a primary checkout."""

    def __init__(self, payload):
        # the session's project, not a directory a Bash cd moved the payload's cwd to; its GIT_* would name another repository
        start = os.environ.get('CLAUDE_PROJECT_DIR') or payload.get('cwd') or '.'
        env = {k: v for k, v in os.environ.items() if not k.startswith('GIT_')}
        out = subprocess.run(['git', '-C', start, 'rev-parse', '--show-toplevel', '--path-format=absolute', '--git-path', 'chief'],
                             capture_output=True, text=True, env=env).stdout.split('\n')
        self.root = Path(out[0] if len(out) > 1 and out[0] else start).resolve()
        self.chief = Path(out[1]).resolve() if len(out) > 2 and out[1] else None

    def holds(self, path, cwd):
        p = Path(os.path.expanduser(path))
        if UNKNOWN in path or (cwd is None and not p.is_absolute()):
            return True  # an unresolved variable or directory could name the worktree
        p = (p if p.is_absolute() else cwd / p).resolve()
        if self.chief and (p == self.chief or self.chief in p.parents):
            return False
        return p == self.root or self.root in p.parents


def substitutions(text, quotes=True):
    """(text with each command substitution outside single quotes replaced by an unresolved word, their bodies).
    quotes=False reads an unquoted heredoc body, where single quotes are plain characters."""
    out, bodies, i, single, double = [], [], 0, False, False
    while i < len(text):
        c = text[i]
        if c == '\\' and not single:
            out.append(text[i:i + 2])
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
        out.append(c)
        i += 1
    return ''.join(out), bodies


OPERATORS = sorted(SEPARATORS | REDIRECTS | INPUTS, key=len, reverse=True)


def operators(token):
    """shlex joins adjacent punctuation, `);` or `)&&`, into one token: split it into shell operators."""
    if not token or not set(token) <= set(';&|<>()'):
        return [token]
    out = []
    while token:
        op = next((o for o in OPERATORS if token.startswith(o)), token[0])
        out.append(op)
        token = token[len(op):]
    return out


class Check:
    def __init__(self, tree, cwd):
        self.tree, self.cwd, self.vars = tree, cwd, {}

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
            self.child().command(body, depth + 1)
        lex = shlex.shlex(text.replace('\n', ' ; '), posix=True, punctuation_chars=';&|<>()')
        lex.whitespace_split = True
        try:
            tokens = list(lex)
        except ValueError as e:
            raise Denied(f'cannot parse this command ({e}); run it as plain words, or dispatch it to a unit')
        seg, scopes = [], []
        for t in [o for t in tokens for o in operators(t)] + [';']:
            if t in SEPARATORS:
                if seg:
                    self.segment(seg, depth)
                seg = []
                # a ( ) subshell's cd and assignments end with it
                if t == '(':
                    scopes.append((self.cwd, dict(self.vars)))
                elif t == ')' and scopes:
                    self.cwd, self.vars = scopes.pop()
            else:
                seg.append(t)

    def child(self):
        c = Check(self.tree, self.cwd)
        c.vars = dict(self.vars)
        return c

    def expand(self, word):
        def value(m):
            name = m.group(1) or m.group(2)
            return self.vars.get(name, os.environ.get(name, UNKNOWN) if name in ('HOME', 'TMPDIR') else UNKNOWN)
        return VARIABLE.sub(value, word)

    def writes(self, path, why):
        if path != '/dev/null' and self.tree.holds(self.expand(path), self.cwd):
            raise Denied(f'{why} would write {path} inside the task worktree; chief writes only its artifacts outside it '
                         '(name them by literal path): dispatch a writer for the worktree')

    def segment(self, words, depth):
        for k, w in enumerate(words[:-1]):
            if w in REDIRECTS and not (w == '>&' and words[k + 1].isdigit()):
                self.writes(words[k + 1], 'the redirect')
        words = [w for k, w in enumerate(words) if w not in REDIRECTS | INPUTS
                 and not (k and words[k - 1] in REDIRECTS | INPUTS)]
        words = self.unwrap(words)
        if not words:
            return
        cmd, args = os.path.basename(words[0]), words[1:]
        if cmd == 'cd':
            args = [a for a in args if a not in ('--', '-L', '-P')]
            target = self.expand(args[0]) if args else os.path.expanduser('~')
            self.cwd = None if UNKNOWN in target or target == '-' or self.cwd is None else (self.cwd / os.path.expanduser(target)).resolve()
            return
        if cmd in SHELLS:
            for k, a in enumerate(args):
                if re.fullmatch(r'-[a-zA-Z]*c[a-zA-Z]*', a):
                    return self.child().command(args[k + 1] if k + 1 < len(args) else '', depth + 1)
        if cmd == 'eval':
            return self.command(' '.join(args), depth + 1)
        if cmd == 'git':
            self.git(args)
        elif cmd == 'gh':
            self.gh(args)
        elif cmd in WORKFLOW_ONLY or (cmd.startswith('python') and any(os.path.basename(a) in WORKFLOW_ONLY for a in args)):
            raise Denied('this publisher runs only inside its workflow (autoPush, markSonar)')
        self.file_writer(cmd, args)

    def unwrap(self, words):
        while words:
            w = words[0]
            m = ASSIGNMENT.match(w)
            if m:
                # a bare assignment holds for later segments; one prefixing a command only for that command
                if len(words) == 1:
                    self.vars[m.group(1)] = self.expand(m.group(2))
                words = words[1:]
            elif w in KEYWORDS or w in WRAPPERS:
                words = words[1:]
            elif w in WRAPPERS_WITH_VALUE:
                words = words[1:]
                while words and (words[0].startswith('-') or (w == 'env' and '=' in words[0])):
                    words = words[2:] if words[0] in WRAPPERS_WITH_VALUE[w] else words[1:]
                if w == 'timeout' and words:
                    words = words[1:]  # the duration
            else:
                break
        return words

    def git(self, args):
        i, where = 0, self.cwd
        while i < len(args) and args[i].startswith('-'):
            if args[i] == '-C' and i + 1 < len(args):
                d = self.expand(args[i + 1])
                where = None if UNKNOWN in d or (where is None and not os.path.isabs(d)) else (where or Path('/')) / os.path.expanduser(d)
            i += 2 if args[i] in GIT_GLOBAL_WITH_VALUE else 1
        if i >= len(args):
            return
        sub, rest = args[i], args[i + 1:]
        # --output names a file relative to git's own directory, -C included
        for k, a in enumerate(rest):
            out = a.split('=', 1)[1] if a.startswith('--output=') else rest[k + 1] if a == '--output' and k + 1 < len(rest) else None
            if out is not None and out != '/dev/null' and self.tree.holds(self.expand(out), where):
                raise Denied(f'`git {sub} --output` would write {out} inside the task worktree; name a path outside it')
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

    def gh(self, args):
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
            fields = any(re.match(r'^(-[fF]|--field|--raw-field|--input)(=|$)', a) or re.match(r'^-[fF].', a) for a in args)
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

    def file_writer(self, cmd, args):
        names = [a for a in args if not a.startswith('-')]
        if cmd in ('sed', 'perl') and any(re.match(r'^(-i|--in-place)', a) or re.match(r'^-[a-zA-Z]*i', a) for a in args if a.startswith('-')):
            names = [a for a in names if self.cwd is not None and (self.cwd / self.expand(a)).exists()]
        elif cmd in WRITERS_LAST:
            # -t/--target-directory names the destination where the command has it (rsync's -t keeps times)
            target = [] if cmd == 'rsync' else \
                [args[k + 1] for k, a in enumerate(args[:-1]) if a in ('-t', '--target-directory')] + \
                [a.split('=', 1)[1] for a in args if a.startswith('--target-directory=')] + \
                [a[2:] for a in args if a.startswith('-t') and len(a) > 2 and not a.startswith('--')]
            names = target or names[-1:]
        elif cmd in WRITERS_AFTER_FIRST:
            names = names[1:]
        elif cmd not in WRITERS_ALL:
            return
        for p in names:
            self.writes(p, f'`{cmd}`')


def main():
    payload = json.load(sys.stdin)
    if payload.get('agent_type') != 'chief' or payload.get('agent_id'):
        return 0
    tree = Tree(payload)
    cwd = Path(payload.get('cwd') or tree.root)
    tool = payload.get('tool_name')
    data = payload.get('tool_input') or {}
    try:
        if tool == 'Write':
            if tree.holds(data.get('file_path', ''), cwd):
                raise Denied('chief writes only its artifacts outside the task worktree; a worktree change goes to a writer')
        elif tool == 'Bash':
            Check(tree, cwd).command(data.get('command', ''))
    except Denied as e:
        print(f'chief guard: {e}', file=sys.stderr)
        return 2
    return 0


if __name__ == '__main__':
    sys.exit(main())
