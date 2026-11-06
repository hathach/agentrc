#!/usr/bin/env python3
"""Count how the audited repository's scripts, skills, agents, workflows and docs were used, from Claude Code and
Codex transcripts inside the run's window.

  usage.py --run DIR [--claude-root PATH ...] [--codex-root PATH ...] --out FILE

DIR is audit.py start's run directory (repository, since, until). At least one root is required. Events are
filtered by their own timestamps, each tool call is counted once (Claude repeats calls in api-request records;
those are skipped), and a command is parsed, not grepped: only a script in command position counts as an
invocation, after sudo/env/timeout/interpreter prefixes, literal `VAR=path` assignments, `cd` and `ssh` are
resolved; heredoc bodies and `-c` code are skipped. A path counts for the repository when it resolves (installed
symlinks included) into one of its worktrees; a path that names a repository file but does not resolve there
(a removed worktree, another host) is reported as unattributed; a same-name file elsewhere never counts.

stdout holds aggregates only: repository file names, the argparse vocabulary of each script (subcommands and long
flags; other arguments become an unrecognised count), and the repository's own skill, agent and workflow names
(others are `other`). FILE (create-only, mode 0600) adds per-key locators, transcript path:line and tool-call id,
never command text. Exit 0 complete, 1 partial (a root missing or unreadable), 2 refused.
"""
import argparse
import functools
import json
import os
import re
import sys
from collections import Counter, defaultdict
from pathlib import Path
sys.path.insert(0, str(Path(__file__).resolve().parent))
from audit import Refused, aware, create, git  # noqa: E402

INTERPRETERS = re.compile(r'^(?:python(?:3(?:\.\d+)?)?|py|bash|sh|zsh|node|pwsh)$')
PREFIXES = {'sudo', 'env', 'timeout', 'nohup', 'exec', 'time', 'command', 'nice', 'stdbuf', 'setsid'}
READERS = {'cat', 'sed', 'head', 'tail', 'less', 'grep', 'rg', 'wc', 'vim', 'vi', 'nano', 'diff', 'ls', 'awk',
           'chmod', 'cp', 'mv', 'rm', 'git', 'stat', 'file', 'readlink', 'realpath', 'test', '['}
OPERATORS = {';', '&', '|', '&&', '||', '(', ')', ';;', '|&'}
SUBST = re.compile(r'\$\((.*)\)|`([^`]*)`')
SHELL_KEYWORDS = {'if', 'then', 'elif', 'else', 'fi', 'for', 'while', 'until', 'do', 'done', 'case', 'esac', '{',
                  '}', '!', 'function', 'exit', 'return'}
# the only statement shapes an exec script may hold for its calls to count: [const x =] [text(] [await] CALL [)] [;]
JS_PLAIN = re.compile(r'(?:\s*(?:(?:const|let|var)\s+\w+\s*=\s*)?(?:text\(\s*)?(?:await\s+)?CALL\s*\)?\s*;?)*\s*')
HEREDOC = re.compile(r'<<-?\s*([\'"]?)(\w+)\1')
VAR = re.compile(r'\$\{(\w+)(?::?-([^}]*))?\}|\$(\w+)')
SUB = re.compile(r"""add_parser\(\s*['"]([\w-]+)['"]""")
FLAG = re.compile(r"""['"](--[a-z][\w-]*)['"]""")
SH_ARM = re.compile(r'^\s*([a-z][\w-]*|--[a-z][\w-]*)(?:\|[^)]*)?\)', re.M)
CODEX_CALL = re.compile(r'(?:^|[;\n(]|\bawait)\s*tools\.exec_command\(\s*\{([^{}]*)\}\s*\)')
CODEX_LITERAL = r'"(?:[^"\\]|\\.)*"|-?\d+|true|false|null'
CODEX_OBJECT = re.compile(r'\s*(?:\w+\s*:\s*(?:%s)\s*(?:,\s*|$))*' % CODEX_LITERAL)
CODEX_FIELD = r'''\b%s\s*:\s*("(?:[^"\\]|\\.)*")'''
BUILTIN_AGENTS = {'general-purpose', 'Explore', 'Plan', 'fork', 'claude-code-guide', 'statusline-setup',
                  'web-fetch'}
LOCATORS = 50


def shell_abs(path):
    """Absolute as bash sees it: git-bash on Windows takes /x as absolute; ntpath.isabs does not since 3.13, nor
    does pathlib."""
    return str(path).startswith('/') or os.path.isabs(path)


class Repo:
    """The audited repository: its worktrees, tracked files at head, and the vocabulary of its scripts."""

    def __init__(self, path, head):
        self.top = Path(git(path, 'rev-parse', '--show-toplevel').stdout.strip())
        self.name = self.top.name
        self.checkouts = [Path(os.path.realpath(line[9:])) for line in
                          git(path, 'worktree', 'list', '--porcelain').stdout.splitlines()
                          if line.startswith('worktree ')]
        self.files = set(git(path, 'ls-tree', '-r', '--name-only', '-z', head).stdout.split('\0')) - {''}
        self.head = head
        self.skills = {f.split('/')[1] for f in self.files if re.fullmatch(r'skills/[^/]+/SKILL\.md', f)}
        self.agents = {f[7:-3] for f in self.files if re.fullmatch(r'agents/[^/]+\.md', f)}
        self.workflows = {f[10:-3] for f in self.files if re.fullmatch(r'workflows/[^/]+\.js', f)}
        self._vocab = {}
        self._realpath = functools.lru_cache(maxsize=4096)(os.path.realpath)

    def rel(self, path, cwd=None, remote=False):
        """(repo-relative path, 'attributed'|'unattributed') for a path naming a repository file, else None. A path
        on another host never attributes: it names a file there, not in these worktrees."""
        p = Path(path)
        if not shell_abs(path):
            if not cwd and not remote:
                return None
            p = Path(cwd or '/') / p
        real = self.real(p) if not remote else None
        for c in self.checkouts if not remote else ():
            if real.is_relative_to(c):
                rel = real.relative_to(c).as_posix()
                return (rel, 'attributed') if rel in self.files else None
        parts = p.parts
        for i in range(len(parts) - 1):         # the longest tail that is a repository file
            tail = '/'.join(parts[i:])
            if tail in self.files and '/' in tail:
                return tail, 'unattributed'
        return None

    def vocab(self, rel):
        if rel not in self._vocab:
            try:
                src = git(self.top, 'show', f'{self.head}:{rel}').stdout
            except Refused:
                src = ''
            if rel.endswith('.sh'):
                arms = set(SH_ARM.findall(src))
                self._vocab[rel] = ({a for a in arms if not a.startswith('-')}, {a for a in arms if a.startswith('-')})
            else:
                self._vocab[rel] = (set(SUB.findall(src)), set(FLAG.findall(src)))
        return self._vocab[rel]

    def real(self, path):
        """realpath, cached for an absolute path: tool calls name the same few directories thousands of times."""
        return Path(self._realpath(str(path)) if os.path.isabs(path) else os.path.realpath(path))

    def cwd_class(self, cwd):
        if not cwd:
            return 'none'
        real = self.real(cwd)
        if any(real.is_relative_to(c) for c in self.checkouts):
            return 'repo'
        if any(part == self.name or part.startswith(f'{self.name}-') for part in Path(cwd).parts):
            return 'repo-inferred'      # a removed worktree named after the repository
        return 'other'


class Tally:
    def __init__(self, repo):
        self.repo = repo
        self.scripts = defaultdict(lambda: {'invoke': 0, 'ok': 0, 'error': 0, 'result_unknown': 0, 'sessions': set(),
                                            'days': set(), 'cwd': Counter(), 'caller': Counter(),
                                            'subcommands': Counter(), 'flags': Counter(), 'unrecognised_args': 0,
                                            'reads': 0})
        self.unattributed = Counter()
        self.unknown = Counter()
        self.skills, self.agents, self.workflows, self.docs = Counter(), Counter(), Counter(), Counter()
        self.locators = defaultdict(list)
        self.pending = {}           # tool-call id -> [(script key, mode)] awaiting the call's result

    def locate(self, key, where):
        if len(self.locators[key]) < LOCATORS:
            self.locators[key].append(where)

    def invocation(self, rel, how, args, ctx):
        if how == 'unattributed':
            self.unattributed[rel] += 1
            self.locate(f'unattributed:{rel}', ctx['where'])
            return None
        s = self.scripts[rel]
        s['invoke'] += 1
        s['sessions'].add(ctx['session'])
        s['days'].add(ctx['day'])
        s['cwd'][ctx['cwd_class']] += 1
        s['caller'][ctx['caller']] += 1
        subs, flags = self.repo.vocab(rel)
        positional = [a for a in args if not a.startswith('-')]
        if positional and positional[0] in subs:
            s['subcommands'][positional[0]] += 1
        for a in args:
            if a.startswith('--'):
                name = a.split('=', 1)[0]
                if name in flags:
                    s['flags'][name] += 1
                else:
                    s['unrecognised_args'] += 1
        self.locate(rel, ctx['where'])
        return rel

    def read(self, rel, how):
        if how == 'attributed':
            if rel.endswith('.md'):
                self.docs[rel] += 1
            elif rel in self.scripts or rel.endswith(('.py', '.sh', '.mjs', '.js')):
                self.scripts[rel]['reads'] += 1

    def result(self, call_id, is_error):
        """Success proves a script ran and succeeded when only && follows it ('proven'; 'after-and' for one that
        ran only if the commands before it succeeded, counted only then); failure proves a failure only for a
        script run alone."""
        for rel, mode, deferred in self.pending.pop(call_id, ()):
            if mode == 'after-and':
                if is_error:
                    self.unknown['conditional_invocation'] += 1
                elif self.invocation(*deferred):
                    self.scripts[rel]['ok'] += 1
            else:
                outcome = 'error' if is_error and mode == 'only' else \
                    'ok' if not is_error and mode in ('only', 'proven') else 'result_unknown'
                self.scripts[rel][outcome] += 1

    def unresolved_results(self):
        for keys in self.pending.values():
            for rel, mode, _ in keys:
                if mode == 'after-and':
                    self.unknown['conditional_invocation'] += 1
                else:
                    self.scripts[rel]['result_unknown'] += 1
        self.pending.clear()


def strip_heredocs(text):
    out, delim = [], None
    for line in text.split('\n'):
        if delim:
            if line.strip() == delim:
                delim = None
            continue
        m = HEREDOC.search(line)
        if m:
            delim = m.group(2)
        out.append(line)
    return '\n'.join(out)


def expand(token, env):
    if getattr(token, 'opaque', False):
        raise KeyError(token)
    if getattr(token, 'literal', False):
        return str(token)

    def sub(m):
        name = m.group(1) or m.group(3)
        if name in env:
            return env[name]
        if m.group(2) is not None:
            return expand(m.group(2), env)
        raise KeyError(name)
    return os.path.expanduser(VAR.sub(sub, token))


class Word(str):
    """A shell word with its quotes removed. literal: wholly single-quoted, never expanded. opaque: its meaning
    needs more than parameter expansion (command substitution, a quoted or escaped $), so it is never resolved."""
    literal = opaque = False


def tokenize(text):
    """Words and control operators of a shell command; a newline is ';'. Redirections and their targets are
    dropped. Raises ValueError on an unterminated quote."""
    out, buf, parts, opaque, single_dollar, i, skip_next = [], [], set(), False, False, 0, False

    def flush():
        nonlocal buf, parts, opaque, single_dollar, skip_next
        if parts:
            w = Word(''.join(buf))
            w.literal = parts == {'single'}
            w.opaque = opaque or (single_dollar and len(parts) > 1)
            if skip_next:
                skip_next = False
            else:
                out.append(w)
        buf, parts, opaque, single_dollar = [], set(), False, False

    while i < len(text):
        c = text[i]
        if c in ' \t':
            flush()
        elif c == '\n':
            flush()
            out.append(';')
        elif c == '#' and not parts:
            while i < len(text) and text[i] != '\n':
                i += 1
            continue
        elif c == "'":
            j = text.index("'", i + 1)       # ValueError when unterminated
            buf.append(text[i + 1:j])
            parts.add('single')
            single_dollar |= '$' in text[i + 1:j]
            i = j
        elif c == '"':
            j, chunk = i + 1, []
            while text[j] != '"':           # IndexError when unterminated
                if text[j] == '\\' and text[j + 1] in '"\\$`':
                    opaque |= text[j + 1] in '$`'
                    chunk.append(text[j + 1])
                    j += 2
                    continue
                chunk.append(text[j])
                j += 1
            chunk = ''.join(chunk)
            opaque |= '$(' in chunk or '`' in chunk
            buf.append(chunk)
            parts.add('double')
            i = j
        elif c == '\\':
            if i + 1 < len(text) and text[i + 1] != '\n':
                opaque |= text[i + 1] in '$`'
                buf.append(text[i + 1])
                parts.add('plain')
            i += 1
        elif c in '<>':
            if parts and ''.join(buf).isdigit():
                buf, parts = [], set()
            flush()
            j = i
            while j < len(text) and text[j] in '<>&|':
                j += 1
            while j < len(text) and (text[j].isdigit() or text[j] == '-'):
                j += 1
            skip_next = text[j - 1] in '<>|' if j > i else False     # a file target follows unless &N/&-
            i = j
            continue
        elif c in ';&|()':
            flush()
            op = text[i:i + 2] if text[i:i + 2] in ('&&', '||', ';;', '|&') else c
            out.append(op)
            i += len(op)
            continue
        elif c == '`' or text.startswith('$(', i):     # command substitution: part of the word, never resolved
            j, depth = i + 1, 0
            if c == '`':
                j = text.index('`', i + 1)
            else:
                while depth >= 0:
                    j += 1
                    depth += {'(': 1, ')': -1}.get(text[j], 0)      # IndexError when unterminated
            buf.append(text[i:j + 1])
            parts.add('plain')
            opaque = True
            i = j
        else:
            buf.append(c)
            parts.add('plain')
        i += 1
    flush()
    return out


def segments(command):
    """[(operator before it, [words], [operators after it])] per simple command; a trailing ';' is dropped."""
    segs, words, before, ops = [], [], None, []
    for tok in tokenize(strip_heredocs(command)):
        if isinstance(tok, Word):
            words.append(tok)
            continue
        if words:
            segs.append((before, words, len(ops)))
        ops.append(tok)
        words, before = [], tok
    if words:
        segs.append((before, words, len(ops)))
    else:
        while ops and ops[-1] == ';':
            ops.pop()
    return [(b, w, ops[i:]) for b, w, i in segs]


def assign(tok, env):
    name, value = tok.split('=', 1)
    w = Word(value)
    w.literal, w.opaque = tok.literal, tok.opaque
    try:
        env[name] = expand(w, env)
    except KeyError:
        env.pop(name, None)


def parse(command, tally, ctx, cwd, env, remote=False):
    """Every repository script the command runs or reads; returns [(script key, mode, deferred)]. mode says what
    the call's status proves: 'only' (the sole command), 'proven' (only && follows: success proves it succeeded),
    'after-and' (ran only if the commands before it succeeded: counted once the call succeeds), 'none'. From the
    first ||, ( or ) or shell keyword on, or after an exec, nothing is known to run: script-like commands count as
    conditional and assignments and cd are not applied."""
    invoked = []
    try:
        segs = segments(command)
    except (ValueError, IndexError):
        tally.unknown['unparsed_command'] += 1
        return invoked
    uncertain = False       # from the first ||, grouping or shell keyword on, nothing is known to run
    background = bool(segs) and '&' in segs[0][2]  # & backgrounds a whole list: no state applies anywhere
    for before, seg, after in segs:
        uncertain |= before in ('||', '(', ')') or seg[0] in SHELL_KEYWORDS
        # a pipeline element or a background command runs in a subshell: its assignments and cd stay there
        subshell = background or before in ('|', '|&') or bool(after) and after[0] in ('|', '|&')
        if any(re.search(r'[/.](?:py|sh|mjs)\b|/', inner) for t in seg for pair in SUBST.findall(t) for inner in pair):
            tally.unknown['command_substitution'] += 1      # a script may run inside it; never resolved
        only_and = all(op == '&&' for op in after)     # a later failure fails the call; ; & | || would not
        if uncertain:
            mode = 'conditional'
        elif before == '&&':
            mode = 'after-and' if only_and else 'conditional'
        else:
            mode = 'only' if len(segs) == 1 and not after else 'proven' if only_and else 'none'
        uncertain |= seg[0] == 'exec'   # it replaced the shell (or only redirected it: unknown) for later segments
        if mode == 'conditional':      # nothing applied: no assignment, no cd, no invocation
            if not all(re.fullmatch(r'\w+=.*', t) for t in seg) and any('/' in t or '$' in t or t.endswith(('.py', '.sh', '.mjs'))
                                                                       for t in seg):
                tally.unknown['conditional_invocation'] += 1
            continue
        if seg and all(re.fullmatch(r'\w+=.*', t) for t in seg):
            for t in seg:
                if not subshell:            # a bare assignment sets the variable for later commands
                    assign(t, env)
                elif background:            # whether it reached this shell is unknown
                    env.pop(t.split('=', 1)[0], None)
            continue
        if seg and seg[0] == 'export':
            for t in seg[1:]:
                if '=' in t and not subshell:
                    assign(t, env)
                elif '=' in t and background:
                    env.pop(t.split('=', 1)[0], None)
            continue
        while seg and (seg[0] in PREFIXES or re.fullmatch(r'\w+=.*', seg[0])):
            seg = seg[1:]
            while seg and (seg[0].startswith('-') or re.fullmatch(r'\d+[smh]?', seg[0])):
                seg = seg[1:]
        if not seg:
            continue
        head = seg[0]
        if head == 'cd' and len(seg) > 1 and subshell:
            if background:
                cwd = None
                ctx = dict(ctx, cwd_class='none')
            continue
        if head == 'cd' and len(seg) > 1:
            try:
                target = expand(seg[1], env)
                cwd = target if shell_abs(target) else os.path.join(cwd or '/', target)
            except KeyError:
                cwd = None
            ctx = dict(ctx, cwd_class='remote' if remote else tally.repo.cwd_class(cwd))
            continue
        if head == 'ssh':
            rest = [t for t in seg[1:] if not t.startswith('-')]
            if len(rest) > 1:
                if len(rest) == 2 and not rest[1].opaque:
                    invoked += parse(str(rest[1]), tally, dict(ctx, cwd_class='remote'), None, dict(env), True)
                else:
                    tally.unknown['unparsed_ssh'] += 1
            continue
        args = seg[1:]
        if INTERPRETERS.match(os.path.basename(head)):
            while args and args[0].startswith('-'):
                if args[0] in ('-c', '-m', '-e', '-'):
                    args = []
                    break
                args = args[2:] if args[0] in ('-X', '-W') else args[1:]
            if not args:
                continue
            head, args = args[0], args[1:]
        elif os.path.basename(head) in READERS:
            for t in args:
                if '/' in t:
                    try:
                        hit = tally.repo.rel(expand(t, env), cwd, remote)
                    except KeyError:
                        continue
                    if hit:
                        tally.read(*hit)
            continue
        try:
            path = expand(head, env)
        except KeyError:
            tally.unknown['unresolved_variable'] += 1
            continue
        if '/' not in path and not path.endswith(('.py', '.sh', '.mjs')):
            continue
        if not shell_abs(path) and cwd is None and not remote:
            tally.unknown['unknown_cwd'] += 1       # a relative script after a cd whose effect is unknown
            continue
        hit = tally.repo.rel(path, cwd, remote)
        if hit and re.search(r'\.(py|sh|mjs|js)$|^bin/', hit[0]):
            if mode == 'conditional':
                tally.unknown['conditional_invocation'] += 1
                continue
            if mode == 'after-and':         # counted once the call's success proves it ran
                invoked.append((hit[0], mode, (hit[0], hit[1], args, ctx)))
                continue
            key = tally.invocation(hit[0], hit[1], args, ctx)
            if key:
                invoked.append((key, mode, None))
    return invoked


def base_env():
    home = os.path.expanduser('~')
    return {'HOME': home, 'AGENTRC_DIR': os.environ.get('AGENTRC_DIR', f'{home}/.claude')}


def claude_caller(path, entrypoint):
    """workflow or subagent by where the transcript lives; a root session as interactive (cli) or headless
    (sdk-cli: chief, a cowork lane, claude -p); anything else unknown."""
    parts = Path(path).parts
    if 'workflows' in parts:
        return 'workflow'
    if 'subagents' in parts:
        return 'subagent'
    return {'cli': 'root', 'sdk-cli': 'headless'}.get(entrypoint, 'unknown')


def scan_claude(path, tally, window, stats):
    with open(path, encoding='utf-8', errors='replace') as f:
        for n, line in enumerate(f, 1):
            if '"tool_use"' not in line and '"tool_result"' not in line:
                continue
            try:
                rec = json.loads(line)
            except ValueError:
                stats['unparsed_records'] += 1
                continue
            if rec.get('type') not in ('assistant', 'user') or not rec.get('timestamp'):
                continue
            t = aware(rec['timestamp'])
            if not window[0] <= t < window[1]:
                continue
            content = (rec.get('message') or {}).get('content')
            if not isinstance(content, list):
                continue
            cwd = rec.get('cwd')
            for b in content:
                if not isinstance(b, dict):
                    continue
                if b.get('type') == 'tool_result':
                    tally.result(b.get('tool_use_id'), bool(b.get('is_error')))
                    continue
                if b.get('type') != 'tool_use' or b.get('id') in stats['seen']:
                    continue
                stats['seen'].add(b.get('id'))
                stats['tool_calls'] += 1
                ctx = {'session': rec.get('sessionId'), 'day': t.astimezone().date().isoformat(),
                       'caller': claude_caller(path, rec.get('entrypoint')),
                       'cwd_class': tally.repo.cwd_class(cwd), 'where': f'{path}:{n}#{b.get("id")}'}
                tool(b.get('name'), b.get('input') or {}, b.get('id'), tally, ctx, cwd)


def tool(name, inp, call_id, tally, ctx, cwd):
    repo = tally.repo
    if name in ('Bash', 'Monitor') and isinstance(inp.get('command'), str):
        keys = parse(inp['command'], tally, ctx, cwd, base_env())
        if keys and call_id:
            tally.pending[call_id] = keys
    elif name == 'Skill':
        s = str(inp.get('skill', ''))
        tally.skills[s if s in repo.skills else 'other'] += 1
    elif name in ('Agent', 'Task'):
        a = str(inp.get('subagent_type') or 'general-purpose')
        tally.agents[a if a in repo.agents or a in BUILTIN_AGENTS else 'other'] += 1
    elif name == 'Workflow':
        w = str(inp.get('name') or Path(str(inp.get('scriptPath') or '')).stem)
        w = re.sub(r'-wf_[\w-]+$', '', w)
        tally.workflows[w if w in repo.workflows else 'other'] += 1
    elif name in ('Read', 'Edit', 'Write') and isinstance(inp.get('file_path'), str):
        hit = repo.rel(inp['file_path'], cwd)
        if hit and name == 'Read':
            tally.read(*hit)


def js_code(src):
    """src with comments and string literals blanked, except the double-quoted fields of a tools.exec_command({...})
    object: a call written inside a string or a comment is text, not a call."""
    out, i, n = [], 0, len(src)
    while i < n:
        if src.startswith('//', i) or src.startswith('/*', i):
            j = src.find('\n' if src[i + 1] == '/' else '*/', i + 2)
            j = n if j < 0 else j + (0 if src[i + 1] == '/' else 2)
            out.append(' ' * (j - i))
            i = j
        elif src[i] in '\'"`':
            j = i + 1
            while j < n and src[j] != src[i]:
                j += 2 if src[j] == '\\' else 1
            j = min(j + 1, n)
            in_call = re.search(r'tools\.exec_command\(\s*\{[^{}]*$', ''.join(out[-200:]))
            out.append(src[i:j] if in_call and src[i] == '"' else ' ' * (j - i))
            i = j
        else:
            out.append(src[i])
            i += 1
    return ''.join(out)


def codex_commands(payload, tally):
    """[(command, workdir|None)] for a shell call: a function_call to exec_command, or each
    tools.exec_command({cmd: "...", workdir: "..."}) in an exec script whose fields are JSON string literals; any
    other exec_command form counts as unknown. None when the payload is no shell call."""
    kind = payload.get('type')
    if kind == 'function_call' and payload.get('name') in ('exec_command', 'shell', 'local_shell'):
        try:
            a = json.loads(payload.get('arguments') or '{}')
        except ValueError:
            tally.unknown['unparsed_codex_call'] += 1
            return []
        cmd = a.get('cmd') or a.get('command')
        return [(' '.join(cmd) if isinstance(cmd, list) else cmd, a.get('workdir'))] if cmd else []
    if kind == 'custom_tool_call' and payload.get('name') == 'exec':
        src = js_code(payload.get('input') or '')
        if not JS_PLAIN.fullmatch(re.sub(r'(?:await\s+)?tools\.exec_command\(\s*\{[^{}]*\}\s*\)', 'CALL', src)):
            tally.unknown['unparsed_codex_call'] += src.count('tools.exec_command(')
            return []
        calls = CODEX_CALL.findall(src)
        tally.unknown['unparsed_codex_call'] += src.count('tools.exec_command(') - len(calls)
        cmds = []
        for body in calls:
            cmd, wd = re.search(CODEX_FIELD % 'cmd', body), re.search(CODEX_FIELD % 'workdir', body)
            if not CODEX_OBJECT.fullmatch(body) or not cmd or (re.search(r'\bworkdir\s*:', body) and not wd):
                tally.unknown['unparsed_codex_call'] += 1      # a field that is an expression, not a literal
                continue
            try:
                cmds.append((json.loads(cmd.group(1)), json.loads(wd.group(1)) if wd else None))
            except ValueError:              # a JavaScript escape JSON lacks, such as \\x41
                tally.unknown['unparsed_codex_call'] += 1
        return cmds
    return None


def scan_codex(path, tally, window, stats):
    cwd, session, caller = None, Path(path).stem, 'codex'
    with open(path, encoding='utf-8', errors='replace') as f:
        for n, line in enumerate(f, 1):
            try:
                rec = json.loads(line)
            except ValueError:
                stats['unparsed_records'] += 1
                continue
            p = rec.get('payload') or {}
            if rec.get('type') in ('session_meta', 'turn_context'):
                cwd = p.get('cwd') or cwd
                if rec.get('type') == 'session_meta':
                    session = p.get('id') or session
                    caller = {'codex_exec': 'codex-headless'}.get(p.get('originator'), 'codex')
                continue
            if rec.get('type') != 'response_item' or not rec.get('timestamp'):
                continue
            t = aware(rec['timestamp'])
            if not window[0] <= t < window[1]:
                continue
            if p.get('type') in ('function_call_output', 'custom_tool_call_output'):
                out = json.dumps(p.get('output'))
                codes = re.findall(r'exit_code\\?":\s*(-?\d+)|Exit code:\s*(-?\d+)', out)
                if len(codes) == 1:
                    tally.result(p.get('call_id'), int(codes[0][0] or codes[0][1]) != 0)
                continue
            if p.get('type') not in ('function_call', 'custom_tool_call') or p.get('call_id') in stats['seen']:
                continue
            cmds = codex_commands(p, tally)
            if cmds is None:
                continue
            stats['seen'].add(p.get('call_id'))
            stats['tool_calls'] += 1
            ctx = {'session': session, 'day': t.astimezone().date().isoformat(), 'caller': caller,
                   'cwd_class': tally.repo.cwd_class(cwd), 'where': f'{path}:{n}#{p.get("call_id")}'}
            keys = []
            for c, wd in cmds:
                keys += parse(c, tally, dict(ctx, cwd_class=tally.repo.cwd_class(wd or cwd)), wd or cwd, base_env())
            if keys:
                if len(cmds) > 1:           # one status for the batch proves no single command's run
                    deferred = sum(m == 'after-and' for _, m, _ in keys)
                    if deferred:
                        tally.unknown['conditional_invocation'] += deferred
                    keys = [(k, 'none', None) for k, m, _ in keys if m != 'after-and']
                tally.pending[p.get('call_id')] = keys


def files(root, window, errors):
    """JSONL files under root that may hold events in the window (modified at or after its start); every
    directory or file that cannot be read is appended to errors."""
    start = window[0].timestamp()
    for dirpath, _, names in os.walk(root, onerror=errors.append):
        for name in names:
            if name.endswith('.jsonl'):
                full = os.path.join(dirpath, name)
                try:
                    if os.path.getmtime(full) >= start:
                        yield full
                except OSError as e:
                    errors.append(e)


def report(tally, roots, stats, window):
    scripts = {k: dict(v, sessions=len(v['sessions']), days=len(v['days']), cwd=dict(v['cwd']), caller=dict(v['caller']),
                       subcommands=dict(v['subcommands']), flags=dict(v['flags']))
               for k, v in sorted(tally.scripts.items())}
    return {'repo': str(tally.repo.top), 'window': [w.isoformat() for w in window], 'roots': roots,
            'tool_calls': stats['tool_calls'], 'unparsed_records': stats['unparsed_records'],
            'unknown': dict(tally.unknown), 'scripts': scripts, 'unattributed': dict(tally.unattributed),
            'skills': dict(tally.skills), 'agents': dict(tally.agents), 'workflows': dict(tally.workflows),
            'docs_read': dict(tally.docs)}


def render(agg):
    lines = [f"{agg['repo']}  {agg['window'][0]} .. {agg['window'][1]}"]
    lines += [f"root {r['kind']} {r['path']}: {r['status']}, {r.get('files', 0)} files" for r in agg['roots']]
    lines.append(f"tool calls {agg['tool_calls']}, unparsed records {agg['unparsed_records']}, "
                 f"unknown {agg['unknown'] or 0}")
    lines.append('== scripts: invoke ok error result? sessions days | cwd | caller | reads')
    for k, s in sorted(agg['scripts'].items(), key=lambda kv: -kv[1]['invoke']):
        if not s['invoke']:
            continue
        lines.append(f"{s['invoke']:6d} {s['ok']:5d} {s['error']:5d} {s['result_unknown']:5d} {s['sessions']:4d} "
                     f"{s['days']:3d} {k} | {s['cwd']} | {s['caller']} | reads {s['reads']}")
        if s['subcommands']:
            lines.append(f"         subcommands {s['subcommands']}")
        if s['flags'] or s['unrecognised_args']:
            lines.append(f"         flags {s['flags']} unrecognised {s['unrecognised_args']}")
    lines.append('== read, never invoked')
    lines += [f"{s['reads']:6d} {k}" for k, s in sorted(agg['scripts'].items(), key=lambda kv: -kv[1]['reads'])
              if not s['invoke']]
    for title, key in (('unattributed (names a repository file, not resolved into a worktree)', 'unattributed'),
                       ('skill loads', 'skills'), ('agents', 'agents'), ('workflows', 'workflows'),
                       ('docs read', 'docs_read')):
        lines.append(f'== {title}')
        lines += [f'{v:6d} {k}' for k, v in sorted(agg[key].items(), key=lambda kv: -kv[1])]
    return '\n'.join(lines)


def main(argv=None):
    p = argparse.ArgumentParser(description=__doc__.split('\n\n')[0])
    p.add_argument('--run', required=True)
    p.add_argument('--claude-root', action='append', default=[])
    p.add_argument('--codex-root', action='append', default=[])
    p.add_argument('--out', required=True)
    args = p.parse_args(argv)
    try:
        if not args.claude_root and not args.codex_root:
            raise Refused('no transcript root: pass --claude-root and/or --codex-root')
        if os.path.exists(args.out):
            raise Refused(f'{args.out} exists')
        run = json.loads(Path(args.run, 'run.json').read_text(encoding='utf-8'))
        window = (aware(run['since']), aware(run['until']))
        repo = Repo(run['repo'], run['start_head'])
    except (Refused, OSError, KeyError, ValueError) as e:
        print(f'refused: {e}', file=sys.stderr)
        return 2
    tally = Tally(repo)
    stats = {'seen': set(), 'tool_calls': 0, 'unparsed_records': 0}
    roots, partial = [], False
    for kind, scan, paths in (('claude', scan_claude, args.claude_root), ('codex', scan_codex, args.codex_root)):
        for root in paths:
            entry = {'kind': kind, 'path': root}
            if not os.path.isdir(root):
                entry['status'], partial = 'missing', True
            else:
                errors = []
                found = list(files(root, window, errors))
                entry['status'], entry['files'] = 'scanned', len(found)
                if errors:
                    entry['status'], entry['unreadable'], partial = 'partly unreadable', len(errors), True
                for f in found:
                    try:
                        scan(f, tally, window, stats)
                    except OSError:
                        entry['status'], partial = 'partly unreadable', True
            roots.append(entry)
    tally.unresolved_results()
    agg = report(tally, roots, stats, window)
    create(args.out, json.dumps(dict(agg, locators=dict(tally.locators)), indent=1), mode=0o600)
    print(render(agg))
    return 1 if partial else 0


if __name__ == '__main__':
    sys.exit(main())
