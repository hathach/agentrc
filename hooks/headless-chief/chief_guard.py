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
an erroring hook pass, and any error denies.

The boundary is deny what it cannot interpret, within the command families it knows (git, gh, shells,
wrappers, xargs and find -exec, writers and linkers): it follows a few plain forms of those and refuses
the rest rather than grow general parsing. A program outside them passes with its arguments.

The cost is what chief can no longer run directly: piping commands into a shell, shell or wrapper options
outside the small tables, git or a writer fed by xargs or find -exec, globs in a write target even
outside the worktree, any curl beyond a plain GET, any tar but a listing, wget, patch and find -delete.
It runs those as plain words or dispatches them to a unit.

On Windows, where Git Bash runs the command, a /-rooted path is judged both as bash maps it and as
Windows reads it, command names match without case or .exe, and the programs whose command lines or
links this cannot follow (cmd, PowerShell, WSL, mount, ...) are denied; it needs a native Windows
Python there.
"""
import json
import os
import sys


def crashed(kind, value, tb):
    """Claude Code lets a hook that errors pass: any uncaught exception denies instead."""
    try:
        print(f'chief guard: the check failed ({value!a}), so the call is denied', file=sys.stderr)
        sys.stderr.flush()
    finally:
        os._exit(2)


if __name__ == '__main__':
    sys.excepthook = crashed
    # every session's Bash and Write reach this hook: leave before importing the rest
    PAYLOAD = json.load(sys.stdin.buffer)
    if PAYLOAD.get('agent_type') != 'chief' or PAYLOAD.get('agent_id'):
        sys.exit(0)

import posixpath  # noqa: E402
import re  # noqa: E402
import shlex  # noqa: E402
import subprocess  # noqa: E402
import time  # noqa: E402
from pathlib import Path, PureWindowsPath  # noqa: E402

sys.path.insert(0, str(Path(__file__).resolve().parents[2] / 'skills' / 'headless-chief' / 'scripts'))
from shadows import on_path, project  # noqa: E402

GIT_READ = {
    'status', 'log', 'diff', 'show', 'rev-parse', 'rev-list', 'ls-files', 'ls-tree', 'ls-remote',
    'merge-base', 'cat-file', 'describe', 'blame', 'grep', 'shortlog', 'for-each-ref', 'name-rev',
    'show-ref', 'show-branch', 'count-objects', 'fetch', 'whatchanged', 'range-diff', 'cherry',
    'check-ignore', 'check-attr', 'diff-tree', 'diff-index', 'diff-files', 'var', 'help', 'version',
}
GIT_GLOBAL_WITH_VALUE = {'-C', '--git-dir', '--work-tree', '--namespace', '--exec-path'}
# read subcommands' options that run a program, in any spelling git's option parser accepts: (long options with their
# shortest prefix matched, short letters); the whole short cluster is read, so a letter that is an earlier one's value
# (`grep -eO`) is denied too
GIT_EXEC = {'fetch': ({'--upload-pack': 3, '--exec': 3}, 'u'), 'ls-remote': ({'--upload-pack': 3, '--exec': 3}, 'u'),
            'grep': ({'--open-files-in-pager': 3}, 'O')}
# `git -c key=value` is vetted against this allowlist: any value git may run a command from is kept out
GIT_C_ALLOW = {'core.pager=cat', 'core.quotepath=off', 'core.quotepath=false', 'core.quotepath=on',
               'core.quotepath=true', 'color.ui=never', 'color.ui=always', 'color.ui=auto'}
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
PY_LAUNCHERS = {'py', 'pyw'} if os.name == 'nt' else set()
# command prefixes that run the next word: name -> its options that take a value
WRAPPERS = {
    'command': set(), 'builtin': set(), 'nohup': set(), 'exec': {'-a'}, 'time': {'-f', '--format'},
    'stdbuf': {'-i', '-o', '-e', '--input', '--output', '--error'}, 'nice': {'-n', '--adjustment'},
    'timeout': {'-s', '--signal', '-k', '--kill-after'},
    'sudo': {'-u', '--user', '-g', '--group', '-p', '--prompt', '-C', '--close-from', '-r', '--role', '-t', '--type',
             '-h', '--host', '-U', '--other-user'},
    'env': {'-u', '--unset', '-C', '--chdir'},
    'xargs': {'-n', '-L', '-P', '-s', '-I', '-d', '-E', '-a', '--max-args', '--max-lines', '--max-procs',
              '--max-chars', '--delimiter', '--eof', '--arg-file', '--process-slot-var'},
}
# per-wrapper options that take no value; any option outside both tables is denied
WRAPPER_FLAGS = {
    'command': {'-p', '-v', '-V'}, 'builtin': set(), 'nohup': set(), 'exec': {'-c', '-l'},
    'time': {'-p', '-a', '-v', '--append', '--verbose', '--portability', '--quiet'}, 'stdbuf': set(), 'nice': set(),
    'timeout': {'--preserve-status', '--foreground', '-v', '--verbose'},
    'sudo': {'-b', '-E', '-H', '-i', '-K', '-k', '-n', '-P', '-S', '-s', '-v', '-A'},
    'env': {'-i', '-', '-0', '--null', '-v', '--debug', '--ignore-environment'},
    'xargs': {'-0', '-r', '-t', '-p', '-x', '--null', '--no-run-if-empty', '--verbose', '--interactive', '--exit',
              '--open-tty'},
}
# env assignments a tool runs as a command: a pager that is not cat, anything git executes from configuration, and
# every GIT_CONFIG* variable (they inject configuration, core.fsmonitor included)
PAGER_ENV = {'PAGER', 'GIT_PAGER'}
RUN_ENV = {'GIT_SSH', 'GIT_SSH_COMMAND', 'GIT_PROXY_COMMAND', 'GIT_ASKPASS', 'SSH_ASKPASS', 'GIT_EDITOR',
           'EDITOR', 'VISUAL', 'GIT_SEQUENCE_EDITOR', 'GIT_EXTERNAL_DIFF', 'GIT_EXEC_PATH', 'BASHOPTS'}
SHELLS = {'sh', 'bash', 'zsh', 'dash'}
SHELL_LONG = {'--login', '--noprofile', '--norc', '--posix'}
# Windows programs a bash command can start that run a command line of their own: powershell reads bare words
# as -Command, cmd runs /k too, start and the terminals run their arguments
FOREIGN_SHELLS = {'cmd', 'powershell', 'pwsh', 'wsl', 'start', 'winpty', 'mintty', 'conhost', 'git-bash', 'git-cmd',
                  'forfiles', 'cscript', 'wscript', 'schtasks', 'mshta', 'rundll32', 'runas', 'explorer', 'wt', 'wmic',
                  'pcalua', 'msiexec', 'regsvr32', 'bitsadmin', 'sc'}
# Windows writers whose targets this does not read, and the mounts, shares and drive aliases resolve() cannot see
WINDOWS_WRITERS = {'robocopy', 'xcopy', 'fsutil', 'certutil', 'expand', 'mklink', 'mount', 'subst', 'net', 'mountvol',
                   'replace', 'esentutl', 'extrac32', 'makecab', 'attrib', 'icacls', 'takeown', 'compact'}
# WSL distro launchers, which a bash command word reaches by name through WindowsApps
DISTROS = re.compile(r'(ubuntu[\d.]*|debian|kali(-linux)?|opensuse[\w.-]*|sles[\w.-]*|suse[\w.-]*|fedora[\w.-]*|arch(linux)?|'
                     r'alpine|oraclelinux[\w.-]*|almalinux[\w.-]*|pengwin[\w.-]*|centos[\w.-]*|rocky(linux)?[\w.-]*|'
                     r'gentoo|nixos|openeuler[\w.-]*|amazonlinux[\w.-]*|wsl\w*)')
# MSYS /dev entries that are never a file, placed without asking cygpath
DEVICES = re.compile(r'/dev/(null|tty|console|stdin|stdout|stderr|fd/\d+)')
DEADLINE = time.monotonic() + 6   # the whole check, well inside Claude Code's 10 s hook timeout
MAPPED = {}   # posix path -> cygpath -m's answer, None when it had none
KEYWORDS = {'if', 'then', 'else', 'elif', 'do', 'while', 'until', '!', '{', '}'}
# file writers: every named path counts, only the last for a copy, all but the first for a mode change
WRITERS_ALL = {'rm', 'rmdir', 'mv', 'touch', 'mkdir', 'truncate', 'tee', 'shred', 'unlink'}
WRITERS_LAST = {'cp', 'ln', 'install', 'rsync'}
WRITERS_AFTER_FIRST = {'chmod', 'chown', 'chgrp'}
# curl: a plain GET only; any other option is denied, an output is judged as a write target
CURL_FLAGS = {'--silent', '--show-error', '--fail', '--location'}
CURL_VALUES = {'-H': 'header', '--header': 'header', '-X': 'request', '--request': 'request', '-o': 'output',
               '--output': 'output'}
# find primaries that delete or write files: the paths they act on are only known at run time
FIND_WRITES = {'-delete', '-fprint', '-fprint0', '-fprintf', '-fls'}
# what xargs or find -exec may run: a read; git, gh, shells, eval, interpreters and every writer are denied there
INDIRECT_DENY = ({'git', 'gh', 'eval', 'sed', 'perl', 'curl', 'tar', 'dd', 'sort', 'find', 'link'} | SHELLS | WORKFLOW_ONLY
                 | PY_LAUNCHERS | WRITERS_ALL | WRITERS_LAST | WRITERS_AFTER_FIRST)
SEPARATORS = {';', '&&', '||', '|', '&', '(', ')', '|&', ';;'}
REDIRECTS = {'>', '>>', '&>', '&>>', '>|', '>&'}
STREAMS = REDIRECTS | {'<', '<<', '<<<'}
OPERATORS = sorted(SEPARATORS | STREAMS, key=len, reverse=True)
PUNCTUATION = ';&|<>()'
# unquoted, these expand a write target to several paths or none; a quoted one is a literal path character
GLOB = '{}*?['
GLOBSET = set(GLOB)
# a quoted or escaped operator or glob character stays a word character: these stand in for it until a path is judged
SPECIAL = PUNCTUATION + GLOB
QUOTED = {c: chr(0xE000 + i) for i, c in enumerate(SPECIAL)}
UNQUOTED = str.maketrans({v: k for k, v in QUOTED.items()})
HEREDOC = re.compile(r"<<-?[ \t]*(['\"]?)(\w+)\1([^\n]*)\n(.*?)\n[ \t]*\2[ \t]*(?=\n|$)", re.S)
ASSIGNMENT = re.compile(r'[A-Za-z_][A-Za-z0-9_]*=')
UNKNOWN = '\0'
WRITE_RULE = 'chief writes only its artifacts, named by a literal absolute path outside the task worktree; a worktree change goes to a writer'
LINK_RULE = ('on Windows a link to the task worktree is a path this guard cannot follow; a link names a literal absolute '
             'source outside it') if os.name == 'nt' else \
    ('a hard link to a task-worktree file lets a later write reach it through chief; a link names a literal absolute '
     'source outside the worktree')


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
            try:
                root, chief = project(self.payload, timeout=remaining())
            except subprocess.TimeoutExpired:
                raise Denied(TOO_LONG) from None
            self.root, self.chief = resolved(root), chief and resolved(chief)
        p = resolved(path)
        return p.is_relative_to(self.root) and not (self.chief and p.is_relative_to(self.chief))

    def outside(self, path):
        """Is this word a literal absolute path outside the worktree."""
        path = path.translate(UNQUOTED)
        if path == '/dev/null':
            return True
        return not (UNKNOWN in path or '$' in path or not (places := absolute(path)) or any(self.inside(p) for p in places))

    def writes(self, path, why, glob=True, verb='write', rule=WRITE_RULE):
        if glob and GLOBSET & set(path):
            raise Denied(f'{why} would {verb} {path.translate(UNQUOTED)}, which names an unquoted glob or brace: it must name '
                         'one literal path; quote it if it is a literal path, or dispatch the step to a unit')
        if not self.outside(path):
            raise Denied(f'{why} would {verb} {path.translate(UNQUOTED)}: {rule}')

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

    def segment(self, words, depth, indirect=False):
        remaining()
        for k, w in enumerate(words[:-1]):
            if w in REDIRECTS and not (w == '>&' and words[k + 1].isdigit()):
                self.writes(words[k + 1], 'the redirect')
        if os.name == 'nt' and any(re.match(r'(MSYS|CYGWIN)=.*winsymlinks', w) for w in words):
            raise Denied('winsymlinks makes links this guard cannot follow on Windows; dispatch the step to a unit')
        words, xargs, replace = unwrap([w for k, w in enumerate(words)
                                        if w not in STREAMS and not (k and words[k - 1] in STREAMS)])
        if not words:
            return
        if indirect and '{}' in words[0].translate(UNQUOTED):
            raise Denied('`find -exec {}` runs a found path as the command, known only at run time; name the program')
        if replace and replace.translate(UNQUOTED) in words[0].translate(UNQUOTED):
            raise Denied('`xargs -I` puts its input in the command word, known only at run time; name the program')
        if UNKNOWN in words[0] or '$' in words[0] or GLOBSET & set(words[0]):
            raise Denied('the command word is set at runtime, so this guard cannot tell what runs; name the program directly')
        name = program(os.path.basename(words[0]))
        # a dashed builtin runs the subcommand without the `git` word: judged as `git <sub>`
        cmd, args = ('git', [name[4:]] + words[1:]) if name.startswith('git-') else (name, words[1:])
        if (indirect or xargs) and (cmd in INDIRECT_DENY or cmd.startswith('python')):
            raise Denied(f'`{cmd}` under xargs or find -exec takes arguments known only at run time; run it with literal '
                         'arguments, or dispatch the step to a unit')
        if os.name == 'nt':
            self.windows(words[0], name)
        for source in link_sources(cmd, args, symlinks=os.name == 'nt'):
            self.writes(source, f'`{cmd}`', verb='link', rule=LINK_RULE)
        if cmd == 'shopt' and 'expand_aliases' in [a.translate(UNQUOTED) for a in args]:
            raise Denied('`shopt expand_aliases` lets a later alias run a command under another name this guard cannot see')
        if cmd == 'alias' and any('=' in a for a in args):
            raise Denied('an alias runs a command under another name this guard cannot see; run the command itself')
        if cmd in SHELLS:
            return self.shell(args, depth)
        if cmd == 'eval':
            return self.command(' '.join(args).translate(UNQUOTED), depth + 1)
        if cmd == 'find':
            self.find_exec(args, depth)
        if cmd == 'git':
            self.git(args)
        elif cmd == 'scalar':
            raise Denied('`scalar` clones, registers and runs maintenance on a repository; chief only reads: dispatch a writer or the workflow')
        elif cmd == 'gh':
            gh(args)
        elif cmd in WORKFLOW_ONLY or ((cmd.startswith('python') or cmd in PY_LAUNCHERS)
                                      and any(program(os.path.basename(a)) in WORKFLOW_ONLY for a in args)):
            raise Denied('this publisher runs only inside its workflow (autoPush, markSonar)')
        elif cmd in ('sed', 'perl') and any(re.match(r'-[a-zA-Z]*i|--in-place', a) for a in args):
            raise Denied(f'`{cmd}` in place edits a file; chief writes its artifacts whole with Write, and a worktree change goes to a writer')
        elif cmd == 'wget':
            raise Denied('`wget` writes files and logs at paths this guard cannot place; fetch with `curl -o <absolute path outside the worktree>`, or dispatch to a unit')
        elif cmd == 'patch':
            raise Denied('`patch` edits files in place; chief writes its artifacts whole with Write, and a worktree change goes to a writer')
        elif cmd == 'tar' and not (len(args) == 2 and re.fullmatch(r'-?tv?f', args[0]) and ':' not in args[1]):
            raise Denied('`tar` other than a listing (`tar tf <local file>`) extracts, writes or runs a program this guard '
                         'cannot place; dispatch the step to a unit')
        elif cmd == 'curl':
            self.curl(args)
        for path in written(cmd, args):
            self.writes(path, f'`{cmd}`')

    def windows(self, word, name):
        """Git Bash on Windows: a program that starts a command line or writes paths this guard cannot follow (git-bash
        and git-cmd are named before the dashed-git rule reads them as git)."""
        if name in FOREIGN_SHELLS or wsl(word, name):
            raise Denied(f'`{name}` runs a command line this guard cannot follow; run it as plain bash words, or dispatch it to a unit')
        if name in WINDOWS_WRITERS:
            raise Denied(f'`{name}` writes or maps paths this guard cannot follow on Windows; dispatch the step to a unit')

    def git(self, args):
        i = 0
        while i < len(args) and args[i].startswith('-'):
            a = args[i]
            if a == '--config-env' or a.startswith(('--config-env=', '--exec-path=')):
                raise Denied(f'`git {a}` takes configuration or programs this guard cannot vet; dispatch the step to a unit')
            if a == '-c':
                self.git_c(args[i + 1] if i + 1 < len(args) else '')
                i += 2
                continue
            if a.startswith('-c') and len(a) > 2:
                self.git_c(a[2:])
                i += 1
                continue
            i += 2 if a in GIT_GLOBAL_WITH_VALUE else 1
        if i >= len(args):
            return
        sub, rest = args[i], args[i + 1:]
        if UNKNOWN in sub or '$' in sub:
            raise Denied('the git subcommand is set at runtime, so this guard cannot tell what runs; name it directly')
        for k, a in enumerate(rest):
            out = a.split('=', 1)[1] if a.startswith('--output=') else rest[k + 1] if a == '--output' and k + 1 < len(rest) else None
            if out is not None:
                self.writes(out, f'`git {sub} --output`')
        if sub in GIT_EXEC and (a := spelled(rest, *GIT_EXEC[sub])):
            raise Denied(f'`git {sub} {a}` runs a program this guard cannot vet (locally for a `.` remote); use the plain read')
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

    def git_c(self, kv):
        """`git -c key=value`: only a value git cannot run a command from passes."""
        if kv.translate(UNQUOTED) not in GIT_C_ALLOW:
            raise Denied(f'`git -c {kv.translate(UNQUOTED)}` sets configuration this guard cannot vet (some keys run a command); '
                         'only the read allowlist passes: core.pager=cat, core.quotepath, color.ui')

    def shell(self, args, depth):
        """A shell runs its `-c` string (checked) or a script operand (not followed, like any program). Any option outside
        a small known set is denied, and so is reading commands from stdin, which this guard cannot see."""
        command, k = False, 0
        while k < len(args) and args[k][:1] in ('-', '+') and args[k] != '-':
            a = args[k]
            k += 1
            if a == '--':
                break
            if a not in SHELL_LONG and not re.fullmatch(r'-[celuvx]+', a):
                raise Denied(f'`{a}` is a shell option this guard does not follow; pass the commands to `-c`, or dispatch to a unit')
            command = command or 'c' in a[1:] and not a.startswith('--')
        operand = args[k] if k < len(args) else None
        if command:
            return self.command((operand or '').translate(UNQUOTED), depth + 1)
        if operand in (None, '-'):
            raise Denied('a shell here reads its commands from stdin, which this guard cannot see; pass them to `-c`, run them as plain words, or dispatch to a unit')

    def find_exec(self, args, depth):
        """`find -exec`/`-execdir`/`-ok`/`-okdir` runs a command on the paths it finds, known only at run time: only a
        read passes there."""
        if writes := FIND_WRITES.intersection(a.translate(UNQUOTED) for a in args):
            raise Denied(f'`find {sorted(writes)[0]}` writes or deletes at paths this guard cannot place (the found paths are '
                         'unknown here); dispatch the step to a unit')
        i = 0
        while i < len(args):
            if args[i].translate(UNQUOTED) in ('-exec', '-execdir', '-ok', '-okdir'):
                clause, i = [], i + 1
                while i < len(args) and args[i].translate(UNQUOTED) not in (';', '+'):
                    clause.append(args[i])
                    i += 1
                if clause:
                    self.segment(clause, depth, indirect=True)
            else:
                i += 1

    def curl(self, args):
        """curl passes only as a plain GET with the options chief needs; each output is judged as a write target."""
        k = 0
        while k < len(args):
            a = args[k]
            k += 1
            if a == '--':
                return
            if not a.startswith('-') or a in CURL_FLAGS or re.fullmatch(r'-[sSfL]+', a):
                continue
            name, eq, value = a.partition('=') if a.startswith('--') else (a[:2], '', a[2:])
            kind = CURL_VALUES.get(name)
            if kind is None:
                raise Denied(f'`curl {a}` is not a plain GET this guard follows; use -sSfL, -H, -X GET and -o <absolute path '
                             'outside the worktree>, or dispatch the fetch to a unit')
            if not (eq or value):
                value, k = (args[k] if k < len(args) else ''), k + 1
            if kind == 'request' and value != 'GET':
                raise Denied(f'`curl -X {value}` publishes; it goes through the workflow or the unit holding the grant')
            if kind == 'output' and value != '-':
                if '#' in value:
                    raise Denied('`curl -o` with `#` names its file from the URL at run time; give one literal path')
                self.writes(value, '`curl -o`')

def spelled(args, longs, letters, valued='', valued_longs=None):
    """The first word before `--` that spells one of these options as getopt reads it: a long option (longs maps it to
    the shortest prefix length matched) abbreviated or not, or one of letters in a short cluster before the first
    letter in valued, which takes the rest as its value, else the next word, as a long option in valued_longs does
    without `=`; None when none does."""
    def matches(name, table):
        return any(len(name) >= n and o.startswith(name) for o, n in table.items())
    words = iter(args)
    for a in words:
        if a == '--':
            return None
        if a.startswith('--'):
            name, eq, _ = a.partition('=')
            if matches(name, longs):
                return a
            if not eq and matches(name, valued_longs or {}):
                next(words, None)
        elif a.startswith('-'):
            for k, ch in enumerate(a[1:], 1):
                if ch in letters:
                    return a
                if ch in valued:
                    if k == len(a) - 1:
                        next(words, None)
                    break
    return None


LN_VALUED = {'--suffix': 4, '--target-directory': 3}


def link_sources(cmd, args, symlinks):
    """The paths a command links from. A hard link to a task-worktree file lets a later write reach it through chief's
    exempt directory, which `resolved()` cannot detect; on Windows (symlinks) a symlink or an rsync reference directory
    into the worktree is a path this guard cannot follow too. Elsewhere an ln symlink needs no check, as a write through
    one resolves into the worktree and is denied at the write; cp counts both its link options in any getopt spelling."""
    if cmd == 'rsync':
        dest = r'--(link|copy|compare)-dest' if symlinks else r'--link-dest'
        return [a.split('=', 1)[1] if '=' in a else args[k + 1] for k, a in enumerate(args)
                if re.fullmatch(dest + '(=.*)?', a) and ('=' in a or k + 1 < len(args))]
    if cmd == 'link' or (cmd == 'ln' and (symlinks or not spelled(args, {'--symbolic': 4}, 's', 'St', LN_VALUED))) or \
            (cmd == 'cp' and spelled(args, {'--link': 3, '--symbolic-link': 4}, 'ls', 'St')):
        operands, directories = getopt(args)
        # link, unlisted as a writer, names its target too; ln's lone operand is linked into the cwd
        return operands if cmd == 'link' or directories or len(operands) < 2 else operands[:-1]
    return []


def getopt(args):
    """cp's, ln's or install's operands and the directories -t names, as GNU getopt reads them: options anywhere
    before `--`, clustered or abbreviated, -S/--suffix and -t/--target-directory taking the next word or their rest."""
    operands, directories, k = [], [], 0
    while k < len(args):
        a = args[k]
        if a == '--':
            operands += args[k + 1:]
            break
        value = option = None
        if a.startswith('--'):
            name, eq, value = a.partition('=')
            # the shortest prefix each stays unique at among cp's, ln's and install's options
            option = next((o for o, n in (('--suffix', 4), ('--target-directory', 3)) if len(name) >= n and o.startswith(name)), None)
            value = value if eq else None
        elif a.startswith('-') and a != '-':
            if m := re.match(r'-[^St]*([St])(.*)', a):
                option, value = m.group(1), m.group(2) or None
        else:
            operands.append(a)
        if option and value is None and k + 1 < len(args):
            k += 1
            value = args[k]
        if option in ('t', '--target-directory'):
            directories.append(value)
        k += 1
    return operands, directories


TOO_LONG = 'checking this call took too long; split the command, or name its paths as drive paths (C:/...)'


def remaining():
    """Seconds left of the run's deadline; past it the call is denied, as Claude Code would let it pass at its timeout."""
    left = DEADLINE - time.monotonic()
    if left <= 0:
        raise Denied(TOO_LONG)
    return left


def program(word):
    """A command word as Windows finds its program: without case, an executable suffix, trailing dots and spaces or a
    `:stream`, all of which Windows drops from a file name; elsewhere the word itself."""
    if os.name != 'nt':
        return word
    name = os.path.basename(word).split(':')[0].rstrip('. ').lower()
    return re.sub(r'\.(exe|com|bat|cmd)$', '', name).rstrip('. ')


def resolved(path):
    """Path(path).resolve(), on Windows without the `\\\\?\\` form it takes for a component ending in a dot or space."""
    p = str(Path(path).resolve())
    if os.name == 'nt' and (m := re.match(r'[\\/]{2}\?[\\/](UNC[\\/])?', p)):
        p = p[:2] + p[m.end():] if m.group(1) else p[m.end():]
    return Path(p)


def within(path, root):
    """Windows: is path, resolved, root or under it, without case."""
    path, root = os.path.normcase(os.path.realpath(path)), os.path.normcase(os.path.realpath(root))
    return path == root or path.startswith(root.rstrip('\\/') + os.sep)


def wsl(word, cmd):
    """Windows: does this command word start WSL, System32's bash or a distro launcher (by name or under WindowsApps)."""
    if DISTROS.fullmatch(cmd):
        return True
    if not re.search(r'[\\/]', word):
        return False   # a bare name: Git Bash finds its own bash first
    places = absolute(word.translate(UNQUOTED))
    apps = os.environ.get('LOCALAPPDATA') and os.path.join(os.environ['LOCALAPPDATA'], 'Microsoft', 'WindowsApps')
    system = os.environ.get('SystemRoot') or 'C:\\Windows'
    return (cmd == 'bash' and (not places or any(within(p, system) for p in places))) or \
        bool(apps and any(within(p, apps) for p in places))


def cygpath(posix):
    """posix as MSYS bash maps it (`cygpath -m`), None when it cannot; past the run's budget the call is denied."""
    if posix not in MAPPED:
        # the first cygpath on PATH: another MSYS install than the bash running the command may mount / elsewhere
        exe = on_path('cygpath', ('.exe',))
        try:
            got = exe and subprocess.run([exe, '-m', posix], capture_output=True, encoding='utf-8', timeout=remaining())
        except subprocess.TimeoutExpired:
            raise Denied(TOO_LONG) from None
        except (OSError, ValueError, subprocess.SubprocessError):
            got = None
        MAPPED[posix] = got.stdout.strip() if got and not got.returncode else None
    return MAPPED[posix]


def absolute(path):
    """The absolute paths a write target can name, [] when it is not absolute or cannot be placed. On Windows a
    /-rooted path is named both as MSYS bash maps it and as Windows reads it, and only a drive-letter path with no
    other `:` is absolute: a device or UNC form can reach the worktree by a name its resolved path does not start
    with, and Python 3.12 resolves a stream name (`a.c:s`) to a relative path."""
    remaining()
    if os.name != 'nt':
        return [path] if os.path.isabs(path) else []
    if '..' in re.split(r'[\\/]', path):
        return []   # Cygwin follows a link before a later `..` (/dev/fd/../cwd): no folding tells where it lands
    if path.startswith('/') and not path.startswith('//'):
        posix = posixpath.normpath(path)   # `.` and repeated slashes only
        native = absolute(os.path.abspath(path))
        if m := re.match(r'/([a-zA-Z])(/|$)', posix):
            mapped = f'{m.group(1)}:/{posix[3:]}'
        elif DEVICES.fullmatch(posix):
            return native   # no file for bash
        elif re.match(r'/(dev|proc)(/|$)', posix):
            return []   # links to anywhere: /dev/fd, /proc/self/cwd, /proc/cygdrive
        elif (mapped := cygpath(posix)) is None or (mapped.startswith('/') and not mapped.startswith('//')):
            return []   # no bash mapping to judge it by
        bash = absolute(mapped)
        return bash + native if bash and native else []
    p = PureWindowsPath(path)
    return [path] if re.fullmatch(r'[A-Za-z]:', p.drive) and p.root and ':' not in path[2:] else []


def unwrap(words):
    """(the command a word list runs, past assignments, keywords and wrappers with their options; whether xargs runs it,
    with arguments known only at run time; xargs' -I replace string). An assignment a tool runs as a command, and a wrapper option outside the
    wrapper's known table, are denied."""
    xargs, replace = False, None
    while words:
        w = words[0]
        if w in KEYWORDS:
            words = words[1:]
            continue
        if ASSIGNMENT.match(w):
            forbidden_env(w)
            words = words[1:]
            continue
        name = program(os.path.basename(w))
        if name == 'command' and any(re.fullmatch(r'-[pvV]*[vV][pvV]*', o) for o in options(words[1:])):
            return [], False, None  # looks the names up, runs none
        if name in WRAPPERS:
            if name == 'xargs' and xargs:
                raise Denied('xargs under xargs substitutes into the inner command at run time; run one xargs')
            xargs = xargs or name == 'xargs'
            words, values = strip_wrapper_options(name, words[1:])
            replace = values.get('-I', replace) if name == 'xargs' else replace
            if name == 'timeout' and words:
                words = words[1:]  # the duration
        else:
            break
    return words, xargs, replace


def strip_wrapper_options(name, words):
    """(a wrapper's words past its leading options, {option: value}). Each long option, and each letter of a short
    cluster, must be in the wrapper's table with a known value: a letter taking a value takes the rest of its cluster,
    else the next word."""
    value, flags = WRAPPERS[name], WRAPPER_FLAGS.get(name, set())
    given = {}

    def unknown(a):
        return Denied(f'`{name}` has an option `{a}` this guard does not follow; run the command as plain words, or dispatch it to a unit')
    while words:
        a, words = words[0], words[1:]
        if a == '--':
            return words, given
        if name == 'env' and ASSIGNMENT.match(a):
            forbidden_env(a)
            continue
        if not a.startswith('-') or (a == '-' and '-' not in flags):
            return [a] + words, given
        if a == '-' or a.startswith('--'):
            opt, eq, _ = a.partition('=')
            if opt not in value | flags or (eq and opt not in value):
                raise unknown(a)
            if opt in value and not eq:
                given[opt], words = words[0] if words else '', words[1:]
            continue
        for pos, ch in enumerate(a[1:], 1):
            opt = '-' + ch
            if opt not in value | flags:
                raise unknown(a)
            if opt in value:
                if pos == len(a) - 1:
                    given[opt], words = words[0] if words else '', words[1:]
                else:
                    given[opt] = a[pos + 1:]
                break
    return words, given


def forbidden_env(word):
    """An env assignment a tool runs as a command: a pager other than cat, or a variable git executes from."""
    name, _, value = word.partition('=')
    if name in PAGER_ENV and value.translate(UNQUOTED) not in ('cat', ''):
        raise Denied(f'`{name}={value.translate(UNQUOTED)}` runs a command as the pager; only cat passes')
    if name in RUN_ENV or name.startswith('GIT_CONFIG'):
        raise Denied(f'`{name}` makes a tool run a command this guard cannot vet; dispatch the step to a unit')


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
    if cmd == 'dd':
        return [a.split('=', 1)[1] for a in args if a.startswith('of=')]
    if cmd == 'sort':
        return sort_outputs(args)
    if cmd in WRITERS_LAST:
        if cmd == 'rsync':    # its -t keeps times
            return names[-1:]
        operands, directories = getopt(args)
        return directories or operands[-1:]
    if cmd in WRITERS_AFTER_FIRST:
        # --reference takes the place of the mode or owner: every operand is a target
        return names if any(a.startswith('--reference') for a in args) else names[1:]
    return names if cmd in WRITERS_ALL else []


def sort_outputs(args):
    """The files sort writes: only a separate `-o PATH` or `--output=PATH`; a cluster holding o, another spelling of
    --output and --compress-program (which runs a program) are denied."""
    out, k = [], 0
    while k < len(args):
        a, k = args[k], k + 1
        if a == '--':
            break
        if a == '-o':
            out.append(args[k] if k < len(args) else '')
            k += 1
        elif a.startswith('--output='):
            out.append(a.split('=', 1)[1])
        elif spelled([a], {'--output': 3, '--compress-program': 3}, 'o'):
            raise Denied(f'`sort {a}` writes or runs what this guard does not follow; use `-o <absolute path>` as its own word')
    return out


def main(payload):
    guard = Guard(payload)
    data = payload.get('tool_input') or {}
    try:
        if sys.platform in ('cygwin', 'msys'):   # os.name 'posix' there would skip every Windows check
            raise Denied(f'chief_guard needs a native Windows Python; this is {sys.platform}')
        if payload.get('tool_name') == 'Write':
            guard.writes(data.get('file_path', ''), 'this Write', glob=False)
        elif payload.get('tool_name') == 'Bash':
            guard.command(data.get('command', ''))
    except Denied as e:
        print(f'chief guard: {e}', file=sys.stderr)
        return 2
    return 0


if __name__ == '__main__':
    if os.name == 'nt':
        sys.stderr.reconfigure(encoding='utf-8', errors='backslashreplace')   # the reason names paths, whatever the locale
    sys.exit(main(PAYLOAD))
