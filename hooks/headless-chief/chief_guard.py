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
the rest rather than grow general parsing. It is mistake-only, not an allowlist: a program outside them passes
with its arguments by design, even one that runs them as a command (setsid, ssh, awk, ...), and such a program
is not a defect to patch one by one. Likewise a variable named in arithmetic or an array subscript, whose value
bash evaluates as code (#223): `let`, `declare -i`, `${!name}` and `${name@P}`, which evaluate a value the same way,
are denied, but no value is followed; so are a `-c` or eval string the outer shell expands first, where a value
becomes commands, and the forms that run a command later or out of sight: `coproc`, `function`, a trap action and a
script that is a substitution (#229).

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
# (`grep -eO`) is denied too, and so is every word past `--`, which a value-taking option may consume (`grep -e --`)
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
           'EDITOR', 'VISUAL', 'GIT_SEQUENCE_EDITOR', 'GIT_EXTERNAL_DIFF', 'GIT_EXEC_PATH', 'BASHOPTS', 'TAR_OPTIONS'}
# GIT_TRACE* values git reads as off or stderr; any other is an fd, an absolute path or a socket it appends trace to
GIT_TRACE_QUIET = {'', '0', 'false', '1', '2', 'true'}
# git's trace destinations: GIT_TRACE, each GIT_TRACE_* key but two booleans, and the three Trace2 targets; the other
# GIT_TRACE2_* are settings (trace2/tr2_sysenv.c)
GIT_TRACE_TARGET = re.compile(r'GIT_TRACE(2(_EVENT|_PERF)?|_(?!(REDACT|CURL_NO_DATA)$).*)?', re.S)
# builtins whose assignment operands set the environment a later command inherits
DECLARES = {'export', 'declare', 'typeset', 'readonly', 'local'}
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
# commands whose words this guard reads one at a time, so a brace list bash expands into several may hide one
BRACE_CHECKED = INDIRECT_DENY | DECLARES | {'shopt'}
SEPARATORS = {';', '&&', '||', '|', '&', '(', ')', '|&', ';;'}
REDIRECTS = {'>', '>>', '&>', '&>>', '>|', '>&', '<>'}
STREAMS = REDIRECTS | {'<', '<<', '<<<', '<&'}
OPERATORS = sorted(SEPARATORS | STREAMS, key=len, reverse=True)
PUNCTUATION = ';&|<>()'
# unquoted, these expand a write target to several paths or none; a quoted one is a literal path character
GLOB = '{}*?['
GLOBSET = set(GLOB)


def braces(word):
    """Does this word hold an unquoted brace list or sequence; a bare `{}` (xargs -I, find -exec) is no expansion.
    Linear: the regex `\\{.*(,|\\.\\.).*\\}` takes seconds on a long word of `{,` with no `}`."""
    i, k = word.find('{'), word.rfind('}')
    return 0 <= i < k and (',' in word[i + 1:k] or '..' in word[i + 1:k])


# a quoted or escaped operator, glob, newline, `$` or `~` character stays a word character: these stand in for it until
# a path is judged or a `-c` or eval string is read (a `$` in double quotes is bare, bash expands it); a brace
# separator's stand-in lasts only until literal() reads the word
SPECIAL = PUNCTUATION + GLOB + ',.' + '\n' + '$~'
QUOTED = {c: chr(0xE000 + i) for i, c in enumerate(SPECIAL)}
UNQUOTED = str.maketrans({v: k for k, v in QUOTED.items()})
SEPARATORS_BACK = str.maketrans({QUOTED[c]: c for c in ',.'})
BRACES_QUOTED = str.maketrans({c: QUOTED[c] for c in '{}'})
# marks where a quote or escape starts in a word, until segment() reads whether bash takes it as an assignment
QUOTING = chr(0xE000 + len(SPECIAL))


def literal(word):
    """The word with its brace separators back; bash expands no brace whose separators are all quoted, so its braces
    then take their stand-ins."""
    plain = word.translate(SEPARATORS_BACK)
    separators_quoted = braces(plain.replace(QUOTING, '')) and not braces(word.replace(QUOTING, ''))
    return plain.translate(BRACES_QUOTED) if separators_quoted else plain


# the operators bash reads across a line continuation, longest first; `<(` and `>(` are process substitutions
TOKENS = ('<<<', '$((', '<<', '$(', '${', '$[', "$'", '((', '<(', '>(')
# the delimiter as bash reads it, a plain or fully quoted word that a metacharacter ends
DELIMITER = re.compile(r"""(-)?[ \t]*(['"]?)(\w+)\2(?=[ \t\n;&|<>()]|$)""")
# the arithmetic whose bounds this guard reads as bash does: names, numbers, `$NAME`, operators, blanks and parentheses;
# one way to match each character, so a mismatch fails in linear time
ARITHMETIC = re.compile(r'(?:[\w\s+\-*/%<>=!&|^~?:,#\[\]()]|\$(?=[A-Za-z_])|\$\{[A-Za-z_]\w*\})*')
# the `${!...}` forms that list names or keys; any other reads a variable named by a value
NAME_LISTS = re.compile(r'![A-Za-z_]\w*([*@]|\[[*@]\])')
# a case pattern's `)` closes a substitution here, not in bash
CASE = re.compile(r'(?<![^\s;&|()])case(?![^\s;&|()<>])')
CONTINUED = re.compile(r'(?<!\\)(?:\\\\)*\\\n')  # an odd run of backslashes before a newline
WORD_START = ' \t\n;&|()'
CLOSERS = {')': '(', '}': '{'}
ASSIGNMENT = re.compile(r'[A-Za-z_][A-Za-z0-9_]*\+?=')   # bash's `NAME+=value` appends
DECLARED = re.compile(r'[A-Za-z_]\w*(\[[^]]*\])?\+?=')  # declare's assignment, which bash neither splits nor globs
UNKNOWN = '\0'
STDIN_RULE = ('a shell here reads its commands from stdin, which this guard cannot see; pass them to `-c`, run them as '
              'plain words, or dispatch to a unit')
# a script operand naming a descriptor (/dev/stdin, /dev/fd/0, /proc/self/fd/0) reads the commands piped in
STDIN_PATH = re.compile(r'(^|/)(stdin|fd/\d+)$')
WRITE_RULE = 'chief writes only its artifacts, named by a literal absolute path outside the task worktree; a worktree change goes to a writer'
LINK_RULE = ('on Windows a link to the task worktree is a path this guard cannot follow; a link names a literal absolute '
             'source outside it, not one holding it') if os.name == 'nt' else \
    ('a link to the task worktree lets a later write reach it unseen (a hard link through chief\'s directory, a symlink '
     'made in the same command); a link names a literal absolute source outside the worktree, not one holding it')


class Denied(Exception):
    pass


def past(text, j):
    """j moved past any line continuations."""
    while text.startswith('\\\n', j):
        remaining()
        j += 2
    return j


def ahead(text, i):
    """(the operator at i as bash reads it, line continuations dropped, or '', the index past it)."""
    chars, ends, j = '', [], i
    while len(chars) < 3 and (j := past(text, j)) < len(text):
        chars, j = chars + text[j], j + 1
        ends.append(j)
    token = next((t for t in TOKENS if chars.startswith(t)), '')
    return token, ends[len(token) - 1] if token else i


def arithmetic(text, i):
    """The index past the `))` that closes the arithmetic starting at i: the first unmatched `)` directly followed by
    another. Denied unless its body is ARITHMETIC, whose bounds bash and this guard read alike."""
    depth, j = 0, i
    while (j := past(text, j)) < len(text):
        remaining()
        c, j = text[j], j + 1
        if c == '(':
            depth += 1
        elif c == ')' and depth:
            depth -= 1
        elif c == ')':
            body, j = text[i:j - 1], past(text, j)
            if text[j:j + 1] == ')' and ARITHMETIC.fullmatch(body.replace('\\\n', '')):
                return j + 1
            break
    raise Denied('this guard reads arithmetic only as names, numbers, $NAME and operators closed by `))`, and bash may '
                 'read this one otherwise; compute it in a plain command, or dispatch the step to a unit')


def lexed(text, i=0, close=None, mode='shell', depth=0):
    """(text with each substitution replaced by an unresolved word, each quoted or escaped operator character by its
    stand-in, each comment and heredoc body dropped and each line continuation joined; the substitutions, as
    (text, substitutions) frames; the index past `close`). Read in bash's order, so a heredoc opens only where bash
    opens one and its body is read at the frame's next unquoted newline; what this cannot read as bash does is denied.
    mode is shell, body (an unquoted heredoc body: quotes are plain characters) or param (inside `${...}`)."""
    if depth > 8:
        raise Denied('substitutions nested this deep are not checked; run them as plain words, or dispatch them to a unit')
    out, frames, pending, single, double, nested = [], [], [], False, False, 0
    while i < len(text):
        remaining()
        c = text[i]
        if single:
            single = c != "'"
            out.append(QUOTED.get(c, c))
            i += 1
            continue
        if c == '\\':
            if double and text[i + 1:i + 2] in ('$', '`'):  # bash drops this backslash, shlex keeps it
                out.append(QUOTED.get(text[i + 1], text[i + 1]))
            elif text[i + 1:i + 2] != '\n':  # a line continuation: bash removes it before it reads words or operators
                # a trailing backslash stays bare, for shlex to deny
                out.append((QUOTING if mode == 'shell' and i + 1 < len(text) else '') + c
                           + QUOTED.get(text[i + 1:i + 2], text[i + 1:i + 2]))
            i += 2
            continue
        token, j = ahead(text, i) if c in '$<>(' else ('', i)
        bare = mode != 'body' and not double   # where quotes, newlines and closers are syntax
        shell = bare and mode == 'shell'
        if shell and c in '<>' and token not in ('<(', '>('):
            fd(out)
        if token == '$[':
            raise Denied('`$[...]` is arithmetic this guard does not read; use `$((...))`')
        if mode == 'param' and (c == "'" or token == "$'"):
            raise Denied('this guard does not read a single quote inside `${...}`, which bash reads as a plain character '
                         'in double quotes or a heredoc and runs what it quotes in a subscript; set the value in a plain '
                         'assignment first, or dispatch the step to a unit')
        if token == "$'" and bare:
            while j < len(text) and text[j] != "'":
                remaining()
                j += 2 if text[j] == '\\' else 1   # `\'` does not close it
            if j >= len(text):
                raise Denied("this `$'...'` never closes; close it, or dispatch the step to a unit")
            out.append(UNKNOWN)
            i = j + 1
        elif token == '$(' or token in ('<(', '>(') and shell:
            sub, subs, i = lexed(text, j, ')', 'shell', depth + 1)
            if CASE.search(sub):
                raise Denied('a `case` in a substitution ends it at a pattern\'s `)` for this guard, not for bash; run '
                             'the case as its own command')
            frames.append((sub, subs))
            out.append(UNKNOWN)
        elif token in ('<(', '>(') and mode == 'param':
            raise Denied('this guard does not read a process substitution inside `${...}`, which bash may run; run it '
                         'as its own command, or dispatch the step to a unit')
        elif token == '$((' or token == '((' and shell:
            i = arithmetic(text, j)
            out.append(UNKNOWN if token == '$((' else ':')
        elif token == '${':
            body, subs, i = lexed(text, j, '}', 'param', depth + 1)
            if body.startswith('!') and not NAME_LISTS.fullmatch(body) or body.endswith('@P'):
                raise Denied('`${!name}` and `${name@P}` evaluate a value as a name or a prompt, running any '
                             'substitution in it, which this guard cannot see; expand the variable directly')
            frames.extend(subs)
            out.append(UNKNOWN)
        elif token == '<<<' and shell:
            out.append(token)
            i = j
        elif token == '<<' and shell:
            if not (word := DELIMITER.match(text, j)):
                raise Denied("this guard reads a heredoc's delimiter only as a plain word, so it cannot tell where bash "
                             "ends this body; use a plain word delimiter such as EOF or 'EOF'")
            pending.append(word)
            out.append('<< HEREDOC')
            i = word.end()
        elif c == '`':
            end = text.find('`', i + 1)
            if end < 0 or text[end - 1] == '\\':
                raise Denied('this guard reads a backtick substitution only to the next backtick, unescaped; use '
                             '`$(...)` for nesting, and close it')
            body = text[i + 1:end]
            # bash drops the backslash of `\$` and `\\`, and of `\"` inside double quotes, before it reads the body
            if '\\$' in body or '\\\\' in body or double and '\\"' in body:
                raise Denied('this guard does not read the backslashes bash removes from a backtick substitution '
                             'first; use `$(...)`')
            frames.append(lexed(body, depth=depth + 1)[:2])
            out.append(UNKNOWN)
            i = end + 1
        elif shell and c == '#' and (not out or ends_word(out[-1])):
            i = text.find('\n', i)  # a comment, to its newline: its quotes, substitutions and backslashes are text
            i = len(text) if i < 0 else i
        elif c == '\n' and bare:
            if mode == 'param':
                raise Denied('this guard reads `${...}` only on one line; write it on one line')
            i = bodies(text, i + 1, pending, close == ')', frames, depth)
            out.append(c)
        elif close and bare and c in (close, CLOSERS[close]):
            if c == close and not nested:
                if pending:
                    raise Denied('bash reads the body of a heredoc left open when its substitution closes from the '
                                 'lines after it, which this guard does not follow; end the body inside the substitution')
                return ''.join(out), frames, i + 1
            nested += 1 if c != close else -1
            out.append(c)
            i += 1
        else:
            if mode != 'body' and c == '"':
                double = not double
            elif mode != 'body' and c == "'" and not double:
                single = True
            elif double and c in QUOTED and c != '$':
                c = QUOTED[c]
            out.append(QUOTING + c if mode == 'shell' and c in '"\'' else c)
            i += 1
    if close:
        raise Denied(f'this command leaves a substitution open (no `{close}`), which bash does not read as this guard '
                     'does; close it')
    if pending:
        raise unended(pending[0])
    return ''.join(out), frames, i


def fd(out):
    """Drop a redirect's file descriptor, the unquoted digits that make a whole word before it (`2>f`; `2 >f` keeps
    the argument)."""
    k = len(out)
    while k and len(out[k - 1]) == 1 and out[k - 1] in '0123456789':
        k -= 1
    # digits after a redirect operator, blanks between or not, are its operand (`2>&1>f`, `2>& 1>f`, `> 2>f`)
    j = k
    while j and out[j - 1] in (' ', '\t'):
        j -= 1
    if k < len(out) and (not k or ends_word(out[k - 1])) \
            and not ''.join(out[max(0, j - 2):j]).endswith(('<', '>', '<&', '>&')):
        del out[k:]


def ends_word(piece):
    """Does a piece of lexed() output end a word: an unescaped blank or operator character (an escape is the piece
    QUOTING, backslash, character)."""
    return piece[-1:] in WORD_START and '\\' not in piece[:2]


def unended(word):
    return Denied(f'no line after this heredoc ends it with {word.group(3)}, so bash reads the rest of the command as '
                  'its body; end the body with its delimiter alone on a line')


def bodies(text, i, pending, substitution, frames, depth):
    """The index past the bodies of the pending heredocs, read in order from i, a line start; an unquoted body's
    substitutions join frames. A body no line ends is denied, and in a substitution so is a line that starts with
    the delimiter but is not it, which bash takes as the end of both the body and the substitution (`EOF)`)."""
    for word in pending:
        start, delimiter = i, word.group(3)
        while True:
            remaining()
            if i > len(text):
                raise unended(word)
            end = text.find('\n', i)
            end = len(text) if end < 0 else end
            line = text[i:end].lstrip('\t') if word.group(1) else text[i:end]
            if line == delimiter:
                break
            if substitution and line.startswith(delimiter):
                raise Denied(f'in a substitution bash ends this heredoc at a line starting with {delimiter}, which '
                             'this guard does not follow; end the body with its delimiter alone on a line')
            i = end + 1
        if not word.group(2):  # an unquoted body: bash joins its continued lines and runs its substitutions
            if CONTINUED.search(text, start, i):
                raise Denied('a line continuation in an unquoted heredoc body moves where bash ends the body; join '
                             'the line, or quote the delimiter')
            frames.extend(lexed(text[start:i], mode='body', depth=depth + 1)[1])
        i = end + 1
    pending.clear()
    return i


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
        self.payload, self.root, self.evaluated = payload, None, False

    def inside(self, path, ancestors=False):
        """Is this absolute path in the worktree, chief's own directory apart, or with ancestors one holding it; the
        worktree is read once, on first use."""
        if self.root is None:
            try:
                root, chief = project(self.payload, timeout=remaining())
            except subprocess.TimeoutExpired:
                raise Denied(TOO_LONG) from None
            self.root, self.chief = resolved(root), chief and resolved(chief)
        p = resolved(path)
        return (p.is_relative_to(self.root) and not (self.chief and p.is_relative_to(self.chief))
                or ancestors and self.root.is_relative_to(p))

    def outside(self, path, ancestors=False):
        """Is this word a literal absolute path outside the worktree."""
        path = path.translate(UNQUOTED)
        if path == '/dev/null':
            return True
        return not (UNKNOWN in path or '$' in path or not (places := absolute(path)) or any(self.inside(p, ancestors) for p in places))

    def writes(self, path, why, glob=True, verb='write', rule=WRITE_RULE):
        if glob and GLOBSET & set(path):
            raise Denied(f'{why} would {verb} {path.translate(UNQUOTED)}, which names an unquoted glob or brace: it must name '
                         'one literal path; quote it if it is a literal path, or dispatch the step to a unit')
        # a link to a directory holding the worktree reaches it as a link to the worktree does
        if not self.outside(path, ancestors=verb == 'link'):
            raise Denied(f'{why} would {verb} {path.translate(UNQUOTED)}: {rule}')

    def command(self, command, depth=0):
        if not isinstance(command, str):
            raise TypeError(f'the command is a {type(command).__name__}')  # lexed() would read a list's items
        self.frame(*lexed(command)[:2], depth)

    def frame(self, text, subs, depth):
        if depth > 3:
            raise Denied('commands nested this deep are not checked; run them as plain words, or dispatch them to a unit')
        for sub in subs:
            self.frame(*sub, depth + 1)
        lex = shlex.shlex(text.replace('\n', ' ; '), posix=True, punctuation_chars=PUNCTUATION)
        lex.whitespace_split = True
        lex.commenters = ''  # lexed() dropped the comments; a # inside a word is data
        try:
            tokens = list(lex)
        except ValueError as e:
            raise Denied(f'cannot parse this command ({e}); run it as plain words, or dispatch it to a unit')
        seg = []
        for t in [literal(o) for t in tokens for o in operators(t)] + [';']:
            if t in SEPARATORS:
                if seg:
                    self.segment(seg, depth)
                seg = []
            else:
                seg.append(t)

    def segment(self, words, depth, indirect=False):
        remaining()
        marked = [w for k, w in enumerate(words) if w not in STREAMS and not (k and words[k - 1] in STREAMS)]
        words = [w.replace(QUOTING, '') for w in words]
        for k, w in enumerate(words[:-1]):
            if w in REDIRECTS and not (w == '>&' and words[k + 1].isdigit()):
                self.writes(words[k + 1], 'the redirect')
        if os.name == 'nt' and any(re.match(r'(MSYS|CYGWIN)\+?=.*winsymlinks', w) for w in words):
            raise Denied('winsymlinks makes links this guard cannot follow on Windows; dispatch the step to a unit')
        words, xargs, replace, bare = unwrap([w.replace(QUOTING, '') for w in marked])
        if not words:
            if (indirect or xargs) and not bare:
                raise Denied('a wrapper under xargs or find -exec with no program runs one known only at run time; name the '
                             'program')
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
        if cmd in BRACE_CHECKED or cmd.startswith('python'):
            # bash expands a brace list into several words, which may hide an option this guard reads one word at a time;
            # past git's or gh's `--` a word is a pathspec, unless that `--` may be a bare option's value (`grep -e --`)
            end = len(args)
            if cmd in ('git', 'gh'):
                end = next((k for k, a in enumerate(args) if a == '--' and not (k and re.fullmatch(r'-[^=]*', args[k - 1]))),
                           len(args))
            if any(braces(a) for a in args[:end]):
                raise Denied(f'`{cmd}` takes an unquoted brace list, which bash expands into words that may hide an '
                             'option; quote it')
        if os.name == 'nt':
            self.windows(words[0], name)
        for source in link_sources(cmd, args, symlinks=os.name == 'nt'):
            self.writes(source, f'`{cmd}`', verb='link', rule=LINK_RULE)
        if cmd == 'shopt' and 'expand_aliases' in [a.translate(UNQUOTED) for a in args]:
            raise Denied('`shopt expand_aliases` lets a later alias run a command under another name this guard cannot see')
        if cmd in DECLARES:
            for a in args:
                if ASSIGNMENT.match(a):
                    forbidden_env(a)
        if cmd in ('declare', 'typeset', 'local'):
            # bash reads an operand as declare's assignment only past an unquoted command word that keywords and
            # assignments alone precede, and with its name and `=` unquoted; eval's operands it splits and globs first.
            # A leading unquoted `~` expands to $HOME, $PWD or $OLDPWD, any of which may be `-i`
            assigns = not self.evaluated and QUOTING not in marked[-len(words)] and all(
                w in KEYWORDS or ASSIGNMENT.match(w) for w in marked[:len(marked) - len(words)])
            if any(not (assigns and DECLARED.match(m))
                   and (UNKNOWN in a or '$' in a or m.startswith('~') or (GLOBSET - set('{}')) & set(a))
                   for a, m in zip(args[:args.index('--')] if '--' in args else args, marked[len(marked) - len(args):])):
                raise Denied(f'`{cmd}` takes a word expanded at run time, which may supply `-i` where this guard cannot '
                             'see it; give its options literally, a glob quoted')
        if cmd == 'let' or (cmd in DECLARES and spelled(args, {}, 'i')):
            raise Denied(f'`{cmd}` evaluates values as arithmetic, where a name runs any substitution its value holds, '
                         'which this guard cannot see; compute with `$((...))`')
        if cmd == 'alias' and any('=' in a for a in args):
            raise Denied('an alias runs a command under another name this guard cannot see; run the command itself')
        if cmd in ('coproc', 'function'):
            raise Denied(f'`{cmd}` runs a command this guard does not read; run it as a plain command, a function '
                         'defined as `name() {{ ...; }}`')
        if cmd == 'trap':
            # only literal words that list (`-l`, `-p` before any `--`), or reset or ignore (`-`, '' or a lone signal as
            # the action)
            ended = args[:1] == ['--']
            action = args[1:] if ended else args
            if any(map(expanded, args)) or \
                    len(action) > 1 and action[0] not in ('-', '') + (() if ended else ('-l', '-p')):
                raise Denied('a trap action runs later, where this guard cannot see it; run the command directly')
        if cmd in SHELLS:
            return self.shell(args, depth)
        if cmd in ('source', '.'):
            return script(args[1:] if args[:1] == ['--'] else args)
        if cmd == 'eval':
            self.evaluated = True
            return self.command(code(args), depth + 1)
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
        elif cmd == 'tar' and (os.environ.get('TAR_OPTIONS') or
                               not (len(args) == 2 and re.fullmatch(r'-?tv?f', args[0]) and ':' not in args[1])):
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
        if sub in GIT_EXEC and (a := spelled([w for w in rest if w != '--'], *GIT_EXEC[sub])):
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
        if command:
            return self.command(code(args[k:k + 1]), depth + 1)
        script(args[k:])

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


def expanded(word):
    """Does bash expand this word at run time: a variable, a substitution, or an unquoted glob or brace character."""
    return UNKNOWN in word or '$' in word or bool(GLOBSET & set(word))


def code(words):
    """The `-c` or eval words as the inner shell reads them, joined; one the outer shell expands first, by a variable, a
    substitution, a tilde, a glob or a brace list, is denied, since a value there becomes commands."""
    if any(expanded(w) or re.search(r'(^|[=:])~', w) for w in words):
        raise Denied('this string is expanded before bash runs it as commands, so a value becomes commands this guard '
                     'cannot see; single-quote it')
    return ' '.join(words).translate(UNQUOTED)


def script(operands):
    """A shell's or `source`'s script operand, not followed like any program's file, unless it is stdin or a value
    known only at run time."""
    operand = operands[0] if operands else None
    if operand is None or operand == '-' or STDIN_PATH.search(posixpath.normpath(operand.translate(UNQUOTED))):
        raise Denied(STDIN_RULE)
    if expanded(operand):
        raise Denied('this script is a value known only at run time (a substitution, a variable or a glob), which this '
                     'guard cannot read; name the script file, a glob character quoted')


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


# cp's long options that take the next word as their value, with the shortest prefix getopt matches
CP_VALUED = {'--suffix': 4, '--target-directory': 3, '--sparse': 4, '--no-preserve': 6}


def link_sources(cmd, args, symlinks):
    """The paths a command links from. A hard link to a task-worktree file lets a later write reach it through chief's
    exempt directory, which `resolved()` cannot detect; a symlink lets a write in the same command reach it, as every
    path is placed before any of the command runs; on Windows (symlinks) an rsync reference directory into the worktree
    is a path this guard cannot follow too. cp counts both its link options in any getopt spelling."""
    if cmd == 'rsync':
        dest = r'--(link|copy|compare)-dest' if symlinks else r'--link-dest'
        return [a.split('=', 1)[1] if '=' in a else args[k + 1] for k, a in enumerate(args)
                if re.fullmatch(dest + '(=.*)?', a) and ('=' in a or k + 1 < len(args))]
    if cmd in ('link', 'ln') or (cmd == 'cp' and spelled(args, {'--link': 3, '--symbolic-link': 4}, 'ls', 'St', CP_VALUED)):
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
    if os.name != 'nt' or word == '.':  # `.` is bash's builtin, found before any file
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
        # started from a non-MSYS process, msys-2.0.dll globs and brace-expands its arguments (dcrt0.cc globify)
        env = {**os.environ, 'MSYS': f"{os.environ.get('MSYS', '')} noglob".strip()}
        try:
            got = exe and subprocess.run([exe, '-m', posix], capture_output=True, encoding='utf-8', env=env,
                                         timeout=remaining())
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
    with arguments known only at run time; xargs' -I replace string; whether xargs was the last wrapper, so a missing
    command is its echo). An assignment a tool runs as a command, a wrapper option outside the wrapper's known table,
    a wrapper word expanded at run time, and sudo starting a shell with no command, which reads stdin, are denied."""
    xargs, replace, name = False, None, None
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
            return [], False, None, True  # looks the names up, runs none
        if name in WRAPPERS:
            if name == 'xargs' and xargs:
                raise Denied('xargs under xargs substitutes into the inner command at run time; run one xargs')
            xargs = xargs or name == 'xargs'
            given = words[1:]
            words, values = strip_wrapper_options(name, given)
            replace = values.get('-I', replace) if name == 'xargs' else replace
            if name == 'sudo' and not words and ('-s' in values or '-i' in values):
                raise Denied(STDIN_RULE)
            if name == 'timeout' and words:
                words = words[1:]  # the duration
            # bash splits an expansion into words, and expands a glob or brace into several, so one option value or
            # assignment may hold the program
            if any('$' in w or UNKNOWN in w or (GLOBSET - set('{}')) & set(w) or braces(w)
                   for w in given[:len(given) - len(words)]):
                raise Denied(f'`{name}` takes a word expanded at run time, which may split into more words and hide the '
                             'program; give its options and assignments literally, a glob or brace quoted')
        else:
            break
    return words, xargs, replace, name == 'xargs'


def strip_wrapper_options(name, words):
    """(a wrapper's words past its leading options, {option: value, or True for a flag}). Each long option, and each
    letter of a short cluster, must be in the wrapper's table with a known value: a letter taking a value takes the
    rest of its cluster, else the next word."""
    value, flags = WRAPPERS[name], WRAPPER_FLAGS.get(name, set())
    given, ended = {}, False

    def unknown(a):
        return Denied(f'`{name}` has an option `{a}` this guard does not follow; run the command as plain words, or dispatch it to a unit')
    while words:
        a, words = words[0], words[1:]
        if a == '--' and not ended:
            ended = True
            continue
        # env reads every word holding `=` before its program as an assignment, past `--` too; sudo one before `--` not
        # starting with `/`, then reads options again
        if '=' in a and (name == 'env' and (ended or not a.startswith('-'))
                         or name == 'sudo' and not (ended or a.startswith(('-', '/')))):
            forbidden_env(a, bash=False)
            continue
        if ended or not a.startswith('-') or (a == '-' and '-' not in flags):
            return [a] + words, given
        if a == '-' or a.startswith('--'):
            opt, eq, _ = a.partition('=')
            if opt not in value | flags or (eq and opt not in value):
                raise unknown(a)
            if opt in value and not eq:
                given[opt], words = words[0] if words else '', words[1:]
            elif opt in flags:
                given[opt] = True
            continue
        for pos, ch in enumerate(a[1:], 1):
            opt = '-' + ch
            if opt not in value | flags:
                raise unknown(a)
            if opt in flags:
                given[opt] = True
            if opt in value:
                if pos == len(a) - 1:
                    given[opt], words = words[0] if words else '', words[1:]
                else:
                    given[opt] = a[pos + 1:]
                break
    return words, given


def forbidden_env(word, bash=True):
    """An env assignment a tool runs as a command: a pager other than cat, or a variable git executes from; or a
    trace destination naming where git writes trace output. Only bash reads `NAME+=value` as an append; env sets a
    variable named `NAME+`. On Windows git reads the environment without case."""
    name, _, value = word.partition('=')
    append = bash and name.endswith('+')
    name = name.removesuffix('+') if append else name
    key = name.upper() if os.name == 'nt' else name
    # an append extends a value this guard cannot see
    if key in PAGER_ENV and (append or value.translate(UNQUOTED) not in ('cat', '')):
        raise Denied(f'`{word.translate(UNQUOTED)}` runs a command as the pager; only `{name}=cat` passes')
    if key in RUN_ENV or key.startswith('GIT_CONFIG'):
        raise Denied(f'`{name}` makes a tool run a command this guard cannot vet; dispatch the step to a unit')
    if GIT_TRACE_TARGET.fullmatch(key) and (append or value.translate(UNQUOTED).lower() not in GIT_TRACE_QUIET):
        raise Denied(f'`{word.translate(UNQUOTED)}` makes git write trace output to a file this guard cannot vet; only '
                     f'`{name}=1` (stderr) or `{name}=0` passes')


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
    --output and --compress-program (which runs a program) are denied. Words past `--` are read too: a value-taking
    option (`-T --`) may consume it."""
    out, k = [], 0
    while k < len(args):
        a, k = args[k], k + 1
        if a == '--':
            continue
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
