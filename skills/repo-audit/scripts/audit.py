#!/usr/bin/env python3
"""Pin an audit's range, report the repository's growth over it, and keep the last audit's record.

  audit.py check --repo PATH --head REF
  audit.py start --repo PATH --head REF [--base REF --base-kind bootstrap|override] [--since TIME] [--until TIME]
                 [--out DIR]
  audit.py finish --run DIR --head REF --review FILE [--repo PATH]
  audit.py report --repo PATH --head E --results FILE

The record is `.claude/audit.json`, committed by the audit PR as its last commit P (parent R, the reviewed head).
check verifies it at --head: schema and identity (the root commits of --head); E, the last commit on head's
history that changed it, is the recorded PR's merge commit, read from the recorded base repository on GitHub; P,
that PR's head (fetched from its refs/pull/N/head when absent), has the single parent R, changes only the record
and has E's tree (so only a squash or rebase merge verifies); the PR base M descends from base and is in R and E; the
coverage globs match every path base..R changes;
and the newest validation report, a PR comment by an account with write permission holding a fenced block whose
first line is `repo-audit-validation: 1` and then {"end": E, "items": [{"id", "result": "pass"|"fail", "ref"}]}
(no other fields; ref non-empty), passes every expected item recorded as covered. A newer report whose author's
permission cannot be read leaves the record provisional; no older report stands in for it. A caller without push
access cannot read permissions (HTTP 403): then only the owner of a user's repository (author_association OWNER)
counts, never an organisation's (MEMBER), so check an organisation's record with push access. The comments are
listed twice; a second listing that lost or reordered the first's comments refuses (run it again). It prints
`complete`, `provisional` (the report is missing, malformed, names another end, fails or misses an item) or
`invalid` (a PR or ref GitHub does not have included; a repository that answers 404 too, as one the caller cannot
see does, refuses), each reason after it. Exit 0 complete, 1 otherwise, 2 refused.

report checks the record at E as check does, without the validation comment, and that FILE's {"end", "items":
[{"id", "result", "ref"}]} names E, the commit its results were taken on, and exactly the record's covered items;
it prints the comment body: the audit report rendered from the record and git, each record string on one line
after a fixed prefix, then the validation block. A body over GitHub's comment limit keeps the block and cuts the
report above it at a line, saying so. It refuses when the block alone does not fit, or does not read back as
printed, and posts nothing: `gh pr comment <pr.number> --repo <pr.repo> -F <body>` does. Exit 0 printed, 2 refused.

start takes the base from a complete record (its end E, kind `audit`) or from --base with its kind (`bootstrap` for
a first audit, refused when head holds a record; `override` to replace one); anything else refuses, offering the
record's own base as an override when it is an ancestor of head. Whatever the base, DIR/carried.json holds the blob
id of the record at head (null without one) and, from a schema-valid record of this repository, its pr, live
decisions, open items and policy. It writes DIR/inventory.json, DIR/carried.json, then DIR/run.json, and prints the
growth summary. DIR defaults to $XDG_STATE_HOME
(~/.local/state) agentrc/repo-audit/<identity key>/<time>-<random>, created exclusively. --since/--until bound the
transcript window usage.py reads (ISO 8601; default the base commit's committer time and now).

finish checks, in the run's repository at --repo (default the checkout start ran in), the lead's review FILE against
the record schema, the run's range and carried items and the privacy rules, and that the review's PR is open on its
repository with head R, and records as the PR base M the tip of its base branch, read from that repository, which
must descend from the run's base and be in R (the PR read again after it must still have head R on that branch);
then, after those reads, that the checkout is clean at --head R, R descends from the run's base and start head, and
R holds the record start read (this audit's own record commit, or another audit's record merged since, refuses). It
refuses a `.claude` or record path that is a symbolic link or an ignored record path, then writes the record through
a new file renamed over it and prints how to commit it. Exit 0 done, 2 refused (with every reason on stderr).
Shallow or grafted history, or a replace ref, is refused before any command; so is a run dir of another audit.py
version. A GitHub read (gh, git fetch or ls-remote) never prompts for credentials (on native Windows an ssh url
rewrite may still ask on the console until the time bound), so git takes them from a credential helper only (gh auth
setup-git sets one), never from a GIT_ASKPASS or core.askPass program; it refuses after TIMEOUT seconds, and ends
the helpers it started (git-remote-https, ssh) with it then and on an interrupt. A fetch reporting
progress runs on until FETCH_TIMEOUT; one that reports none for TIMEOUT seconds, on any transport, is refused.

FILE is one JSON object; a field the schema does not name is refused at any depth:
  range       "<base>..<R>" in full SHAs: the run's range, so a review written before R moved refuses
  pr          {repo: "owner/name" of the base repository, number}
  outcome     text
  stages      {coverage, phase1, claims, coreview, correctness, any other stage}: each {status: "completed", ref}
              or {status: "inapplicable", reason}; check accepts a record without correctness, written before it
  coverage    [{batch, paths: [glob]}], not empty; every path base..R changes (both sides of a rename, a deletion,
              a submodule pin) must match a glob: ** spans directories, * and ? stay within one
  claims      [{claim, document, revision, section, variants: [part], status: verified|refuted|pending}]
  decisions   [{id, text, status, by?, ref?}]
  open        [{id, text, status, by?, ref?}]: deferred work
  policy      [{question, answer, date: YYYY-MM-DD}]
  usage       {totals: {camelCaseName: number}, hosts: count, window: {since, until}}: totals across hosts only;
              hosts 0 with totals {} when no transcripts were read (not measured, never zero use)
  validation  {expected: [{id, check, kind: software|hardware, source (the selector or contract that produced
              it), status: covered + ref (its run on R) | inapplicable + reason | excluded + owner (the owner's
              words)}]}, with at least one software and one hardware item; required-pending refuses. The script
              checks presence and provenance only; whether the list is complete for the range is the record
              co-review's call.
The record holds these fields but range, and schema, identity (the root commits), base, baseKind, prBase M,
reviewedHead R and completed (finish's local time, ISO 8601). An item's status is new, carried, resolved or superseded
(+ by, another item's id; a chain of them ends in an item that is not superseded). Each id carried.json lists must
appear carried, resolved or superseded; only those may be carried. Strings that look private refuse: home or /tmp
paths, .jsonl, session URLs and ids, toolu_ ids, ChatGPT conversation links, bare UUIDs outside a CircleCI URL, and
shell command shapes ($(, 2>&1, /dev/null, sudo, a `$ ` prompt, a pipe into a filter, a $VAR/ path).
"""
import argparse
import codecs
import datetime
import hashlib
import json
import os
import re
import secrets
import signal
import subprocess
import sys
import tempfile
import time
from collections import Counter, defaultdict
from pathlib import Path, PurePosixPath
sys.path.insert(0, str(Path(__file__).resolve().parents[3] / 'lib'))
from gh_repo import REPO  # noqa: E402
import windows  # noqa: E402

RECORD = '.claude/audit.json'
SCHEMA = 1
VALIDATION = 'repo-audit-validation: 1'
COMMENT_LIMIT = 65536            # GitHub's comment body maximum, in characters
TIMEOUT = 300                    # seconds a GitHub read may take, or a fetch may receive nothing
FETCH_TIMEOUT = 3600             # seconds a fetch still receiving data may take
# an empty GIT_ASKPASS overrides core.askPass and SSH_ASKPASS for git; ssh reads SSH_ASKPASS itself, and before
# OpenSSH 8.4 ignores SSH_ASKPASS_REQUIRE: an empty one fails its exec, so with DISPLAY set it opens no dialog either
NO_PROMPT = {'GIT_TERMINAL_PROMPT': '0', 'GIT_ASKPASS': '', 'SSH_ASKPASS': '', 'SSH_ASKPASS_REQUIRE': 'never',
             'GCM_INTERACTIVE': 'never', 'GH_PROMPT_DISABLED': '1'}
# a fetch's --progress lines, local and remote: they show it receiving, but explain no failure
PROGRESS = re.compile(r'(?:remote: )?(?:[A-Z][a-z]+(?: [a-z]+)*: +\d|Total \d)')
GITHUB = 'https://github.com/'
STAGES = ('coverage', 'phase1', 'claims', 'coreview')
# finish requires the correctness co-review too; check still reads records written before it
FINISH_STAGES = STAGES + ('correctness',)
STAGE_FIELDS = {'completed': ('ref',), 'inapplicable': ('reason',)}
# what each expected validation status must carry; required-pending blocks finish
EXPECTED_FIELDS = {'covered': ('ref',), 'inapplicable': ('reason',), 'excluded': ('owner',), 'required-pending': ()}
WRITERS = {'admin', 'write'}       # the permission field: a custom role_name reports its base level there
LEVELS = WRITERS | {'read', 'none'}
TEST_DIRS = {'tests', 'test', 'fixtures'}
# Unity/Ceedling: a definition, not a prototype ending in `;`
TEST_DEF = re.compile(r'^\s*(?:async\s+)?def test_\w+|^\s*(?:test|it)\(|^\s*void\s+test_\w+\s*\((?!.*;\s*$)')
KINDS = ('c', 'python', 'prose', 'other')
TOP = 25
TOP_PER_KIND = 10

SHA = re.compile(r'[0-9a-f]{40}')
ITEM_ID = re.compile(r'[A-Za-z0-9][\w.-]*')
STAGE_NAME = re.compile(r'[a-z][a-z0-9-]*')
TOTAL_NAME = re.compile(r'[a-z][A-Za-z0-9]*')
DATE = re.compile(r'\d{4}-\d{2}-\d{2}')
UUID = r'\b[0-9a-f]{8}-[0-9a-f]{4}-[0-9a-f]{4}-[0-9a-f]{4}-[0-9a-f]{12}\b'
# a session id is session_01 + base62, case-sensitive: register names like OTGSC_B_SESSION_VALID are not one; sudo,
# the filters and a $VAR are case-sensitive too, so prose ("Sudo rights", "base | HEAD", "$6/launch") passes
PRIVATE = re.compile(r'/home/|/Users/|~/|/tmp/|C:\\Users|\.jsonl|claude\.ai/code/session'
                     r'|(?-i:\bsession_01[0-9A-Za-z]{8})|toolu_|chatgpt\.com/(?:c|codex|share)/|' + UUID +
                     r'|\$\(|\d>&\d|/dev/null|(?-i:(?:^|\s)sudo\s)|(?:^|\n)\s*\$ '
                     r'|(?-i:\|\s*(?:grep|sed|awk|head|tail|xargs|tee|jq)\b|\$\{?[A-Za-z_]\w*\}?/)', re.I)
# its workflow and job ids are UUIDs; punctuation ends it, so a UUID glued on after it is still checked
CI_URL = re.compile(r'https://app\.circleci\.com/[^\s,;()\[\]<>"\']*', re.I)
CI_ID = re.compile(UUID, re.I)
FENCE = re.compile(r'^```[^\n]*\n(.*?)^```[ \t]*$', re.M | re.S)
LIVE = {'new', 'carried'}
SETTLED = {'carried', 'resolved', 'superseded'}
RUN_FORMAT = 3
LEFT = re.compile(r'\.claude/\.audit\.json\.[0-9a-f]{8}\.tmp')     # finish's new record, before its rename


class Refused(Exception):
    pass


class HttpError(Refused):
    """A GitHub read that got an HTTP error status back, status None when gh named none."""

    def __init__(self, message, status):
        super().__init__(message)
        self.status = status


def git(repo, *args, check=True, network=False):
    if network:
        fetching = args[0] == 'fetch'
        r = github_read(['git', '-C', str(repo), '-c', 'http.lowSpeedLimit=1', '-c', f'http.lowSpeedTime={TIMEOUT}',
                         *args], f'git {" ".join(args)}', FETCH_TIMEOUT if fetching else TIMEOUT,
                        TIMEOUT if fetching else None)
        if fetching:
            r.stderr = ''.join(line for line in r.stderr.splitlines(keepends=True) if not PROGRESS.match(line))
    else:
        r = subprocess.run(['git', '-C', str(repo), *args], capture_output=True, text=True, encoding='utf-8',
                           errors='replace')
    if check and r.returncode:
        raise Refused(f'git {" ".join(args)}: {failure(r.returncode, r.stderr) if network else r.stderr.strip()}')
    return r


def failure(code, stderr):
    """Why a GitHub read failed: its message, naming the way to credentials when it could not prompt for them, or
    its exit status when it wrote none, as when TMPDIR, which holds its output files, filled meanwhile."""
    why = stderr.strip()
    if not why:
        return f'exit {code} with no message (its output files are under {tempfile.gettempdir()}: is that full?)'
    if 'terminal prompts disabled' in why:
        why += ' (git takes credentials from a credential helper only, never an askpass: gh auth setup-git sets one)'
    return why


def gh(args):
    r = github_read(['gh', *args], f'gh {" ".join(args)}')
    return r.returncode, r.stdout, r.stderr


def github_read(argv, label, timeout=TIMEOUT, idle=None):
    """argv run with no prompt, no stdin and, off Windows, no controlling terminal, refused after timeout seconds or
    once it has written nothing for idle seconds. Its output goes through files: on Windows a timed-out run waits for
    every holder of its pipes, as a git-remote-https left behind."""
    with output_file(label) as out, output_file(label) as err:
        try:
            code = bounded(argv, out, err, {**os.environ, **NO_PROMPT}, timeout, idle)
        except Idle:
            raise Refused(f'{label}: nothing received for {idle} s') from None
        except subprocess.TimeoutExpired:
            raise Refused(f'{label}: not finished in {timeout} s') from None
        out.seek(0)
        err.seek(0)
        return subprocess.CompletedProcess(argv, code, out.read(), err.read())


def output_file(label):
    try:
        return tempfile.TemporaryFile('w+', encoding='utf-8', errors='replace')
    except OSError as e:
        raise Refused(f'{label}: no output file under {tempfile.gettempdir()}: {e.strerror or e}') from None


class Idle(subprocess.TimeoutExpired):
    pass


def bounded(argv, out, err, env, timeout, idle=None):
    """argv's exit status; TimeoutExpired after timeout seconds, Idle once out and err have not grown for idle
    seconds. Then or on an interrupt its whole tree is killed, off Windows as its own session's process group:
    killing only argv would leave the helpers it started running."""
    p = subprocess.Popen(argv, stdin=subprocess.DEVNULL, stdout=out, stderr=err, env=env, start_new_session=True)
    try:
        began = last = time.monotonic()
        size = 0
        while True:
            now = time.monotonic()
            if now - began >= timeout:
                raise subprocess.TimeoutExpired(argv, timeout)
            if idle and now - last >= idle:
                raise Idle(argv, idle)
            try:
                return p.wait(min(0.5, began + timeout - now))
            except subprocess.TimeoutExpired:
                pass
            grown = os.fstat(out.fileno()).st_size + os.fstat(err.fileno()).st_size
            if grown != size:
                size, last = grown, time.monotonic()
    except BaseException:
        if os.name == 'nt':
            windows.kill_tree(p, 10)
        else:
            try:
                os.killpg(p.pid, signal.SIGKILL)
            except OSError:
                pass
            p.wait()
        raise


def api(path, paginate=False):
    """One GitHub REST read through gh; a paginated listing comes back as one list."""
    if paginate:
        path += f'{"&" if "?" in path else "?"}per_page=100'
    rc, out, err = gh(['api', *(['--paginate', '--slurp'] if paginate else []), path])
    if rc:
        status = re.search(r'\(HTTP (\d{3})\)', err)
        raise HttpError(f'gh api {path}: {failure(rc, err or out)}', status and int(status[1]))
    try:
        data = json.loads(out)
    except ValueError:
        raise Refused(f'gh api {path}: not JSON: {out.strip()[:200]}') from None
    if not paginate:
        return data
    if not isinstance(data, list) or not all(isinstance(page, list) for page in data):
        raise Refused(f'gh api {path}: not a list of pages: {out.strip()[:200]}')
    return [x for page in data for x in page]


def fetch(repo, slug, ref, check=True):
    """Fetch ref from slug on GitHub into the object store only: no tracking ref, no FETCH_HEAD."""
    return git(repo, 'fetch', '-q', '--progress', '--no-tags', '--no-recurse-submodules', '--no-write-fetch-head',
               f'{GITHUB}{slug}.git', ref, check=check, network=True)


def commit(repo, ref):
    r = git(repo, 'rev-parse', '--verify', '--quiet', f'{ref}^{{commit}}', check=False)
    if r.returncode:
        raise Refused(f'{ref}: not a commit in {repo}')
    return r.stdout.strip()


def has_commit(repo, sha):
    return git(repo, 'cat-file', '-e', f'{sha}^{{commit}}', check=False).returncode == 0


def is_ancestor(repo, a, b):
    return git(repo, 'merge-base', '--is-ancestor', a, b, check=False).returncode == 0


def plain_history(repo):
    """Refuse history that is not the commits' own: identity and ancestry would read a truncated or rewritten graph."""
    if git(repo, 'rev-parse', '--is-shallow-repository').stdout.strip() == 'true':
        raise Refused(f'{repo} is a shallow clone: fetch its whole history (git fetch --unshallow)')
    if git(repo, 'for-each-ref', '--format=%(refname)', 'refs/replace/').stdout.strip():
        raise Refused(f'{repo} has refs/replace/* objects replacing commits')
    common = Path(git(repo, 'rev-parse', '--path-format=absolute', '--git-common-dir').stdout.strip())
    if (common / 'info' / 'grafts').exists():
        raise Refused(f'{repo} has grafted history ({common / "info" / "grafts"})')


def identity(repo, rev):
    """The root commits of rev: the same in every clone and worktree of the repository, unlike a path or a remote."""
    return sorted(git(repo, 'rev-list', '--max-parents=0', rev).stdout.split())


def is_test_path(path):
    p = PurePosixPath(path)
    return bool(TEST_DIRS & set(p.parts[:-1])) or p.name.startswith('test_') or '.test.' in p.name


def area(path):
    parts = PurePosixPath(path).parts
    return '/'.join(parts[:2])


def kind(path):
    if path.endswith(('.c', '.h')):
        return 'c'
    if path.endswith('.py'):
        return 'python'
    if path.endswith('.md') and not is_test_path(path):
        return 'prose'
    return 'other'


def tree(repo, rev):
    """{path: bytes} of every tracked blob at rev."""
    out = git(repo, 'ls-tree', '-r', '-l', '-z', rev).stdout
    files = {}
    for rec in out.split('\0'):
        if not rec:
            continue
        meta, path = rec.split('\t', 1)
        otype, size = meta.split()[1], meta.split()[3]
        if otype == 'blob':
            files[path] = int(size)
    return files


def numstat(repo, base, end):
    """[(added|None, deleted|None, path)] with None for a binary file; a rename counts under its new path."""
    out = git(repo, 'diff', '--numstat', '-z', '-M', '--ignore-submodules=none', base, end).stdout.split('\0')
    rows, i = [], 0
    while i < len(out):
        rec = out[i]
        if not rec:
            i += 1
            continue
        a, d, path = rec.split('\t', 2)
        if not path:            # rename: the old and new paths follow as their own fields
            path = out[i + 2]
            i += 2
        rows.append((None if a == '-' else int(a), None if d == '-' else int(d), path))
        i += 1
    return rows


def gitlinks(repo, base, end):
    """[[path, old, new]] for each submodule pin the range changes, None on the side where the path is no gitlink."""
    out = git(repo, 'diff', '--raw', '-z', '--no-abbrev', '--no-renames', '--ignore-submodules=none', base,
              end).stdout.split('\0')
    rows = []
    for meta, path in zip(out[0::2], out[1::2]):
        old_mode, new_mode, old, new = meta.lstrip(':').split()[:4]
        if '160000' in (old_mode, new_mode):
            rows.append([path, old if old_mode == '160000' else None, new if new_mode == '160000' else None])
    return sorted(rows)


def shortstat(rows):
    """git diff --shortstat's line from numstat rows: a binary file counts as changed, its lines do not."""
    if not rows:
        return ''
    ins = sum(a for a, _, _ in rows if a is not None)
    dels = sum(d for _, d, _ in rows if d is not None)
    parts = [f'{len(rows)} file{"s" * (len(rows) != 1)} changed']
    if ins or not dels:
        parts.append(f'{ins} insertion{"s" * (ins != 1)}(+)')
    if dels or not ins:
        parts.append(f'{dels} deletion{"s" * (dels != 1)}(-)')
    return ', '.join(parts)


def diff_path(text):
    """A path from a ---/+++ header: None for /dev/null, git's C-style quoting undone."""
    text = text.rstrip('\t')         # git ends a header naming a path with a space in a tab
    if text == '/dev/null':
        return None
    if text.startswith('"'):
        text = codecs.escape_decode(text[1:-1].encode())[0].decode()
    return text[2:]


def count_test_defs(repo, base, end):
    """{test file: [added, removed]} counting test function heads in the diff of test paths."""
    out = git(repo, '-c', 'core.quotePath=false', 'diff', '-U0', '--no-color', '--no-ext-diff', '--no-textconv',
              '-M', '--ignore-submodules=none', '--submodule=short', '--src-prefix=a/', '--dst-prefix=b/', base,
              end).stdout
    counts, old, path = defaultdict(lambda: [0, 0]), None, None
    for line in out.splitlines():
        if line.startswith('--- '):
            old = diff_path(line[4:])
        elif line.startswith('+++ '):
            path = diff_path(line[4:]) or old       # a deleted file's removals count under its old path
        elif path and is_test_path(path) and line[:1] in '+-' and TEST_DEF.match(line[1:]):
            counts[path][line[0] == '-'] += 1
    return counts


def big_commits(repo, base, end):
    out = git(repo, 'log', '--first-parent', '-m', '--format=%x00%h %s', '--shortstat', f'{base}..{end}').stdout
    rows = []
    for rec in out.split('\0')[1:]:
        head, _, stat = rec.partition('\n')
        ins = re.search(r'(\d+) insertion', stat)
        dels = re.search(r'(\d+) deletion', stat)
        rows.append((int(ins.group(1)) if ins else 0, int(dels.group(1)) if dels else 0, head.strip()))
    return sorted(rows, reverse=True)


def inventory(repo, base, end):
    before, after = tree(repo, base), tree(repo, end)
    stat = numstat(repo, base, end)
    areas = defaultdict(lambda: [0, 0])
    kinds = {k: {'bytes': [0, 0], 'lines': [0, 0]} for k in KINDS}
    binary, test_lines = [], [0, 0]
    for a, d, path in stat:
        if a is None:
            binary.append(path)
            continue
        for lines in (areas[area(path)], kinds[kind(path)]['lines']):
            lines[0] += a
            lines[1] += d
        if is_test_path(path):
            test_lines[0] += a
            test_lines[1] += d
    sizes, growth = Counter(), []
    for path in set(before) | set(after):
        old, new, k = before.get(path, 0), after.get(path, 0), kind(path)
        sizes[area(path)] += new - old
        kinds[k]['bytes'][0] += old
        kinds[k]['bytes'][1] += new
        if new != old:
            growth.append([new, new - old, k, path])
    tests = count_test_defs(repo, base, end)
    return {
        'base': base, 'end': end,
        'commits': int(git(repo, 'rev-list', '--count', f'{base}..{end}').stdout),
        'shortstat': shortstat(stat),
        'files': [len(before), len(after)],
        'bytes': [sum(before.values()), sum(after.values())],
        'test_bytes': [sum(s for p, s in before.items() if is_test_path(p)),
                       sum(s for p, s in after.items() if is_test_path(p))],
        'test_lines': test_lines,
        'kinds': kinds,
        'areas': sorted(([a, d, k] for k, (a, d) in areas.items()), reverse=True),
        'area_bytes': sorted(([v, k] for k, v in sizes.items() if v), reverse=True),
        'binary_changed': sorted(binary),
        'gitlinks': gitlinks(repo, base, end),
        'largest': sorted(([s, p] for p, s in after.items()), reverse=True)[:TOP],
        'growth': sorted(growth, key=lambda r: (-abs(r[1]), r[3])),
        'tests_added_removed': sorted(([a, r, p] for p, (a, r) in tests.items()), reverse=True),
        'largest_commits': big_commits(repo, base, end)[:TOP],
    }


def summary(inv):
    t = inv['tests_added_removed']
    k = inv['kinds']
    lines = [
        f"{inv['base'][:12]}..{inv['end'][:12]}: {inv['commits']} commits, {inv['shortstat']}",
        f"files {inv['files'][0]} -> {inv['files'][1]}, bytes {inv['bytes'][0]} -> {inv['bytes'][1]}, "
        f"test bytes {inv['test_bytes'][0]} -> {inv['test_bytes'][1]}",
        f"test functions +{sum(r[0] for r in t)} -{sum(r[1] for r in t)} (heads matched by pattern)",
        '== by kind (bytes before -> after, lines added/deleted; prose = non-test markdown)',
        *(f"{n:7s} {k[n]['bytes'][0]:10d} -> {k[n]['bytes'][1]:10d} {'+%d' % k[n]['lines'][0]:>8} "
          f"{'-%d' % k[n]['lines'][1]:>8}" for n in KINDS),
        '== lines added/deleted by area', *(f'{a:7d} {d:7d} {n}' for a, d, n in inv['areas'][:TOP]),
        '== byte change by area', *(f'{v:+9d} {n}' for v, n in inv['area_bytes'][:TOP]),
        '== largest files', *(f'{s:9d} {p}' for s, p in inv['largest']),
        '== largest byte changes per kind (bytes now, change)',
        *(row for n in KINDS for row in per_kind(inv['growth'], n)),
        '== test functions added, removed', *(f'{a:5d} {r:5d} {p}' for a, r, p in t[:TOP]),
        '== largest commits (insertions, deletions)', *(f'{a:6d} {d:6d} {s}' for a, d, s in inv['largest_commits']),
    ]
    if inv['gitlinks']:
        lines += ['== submodule pins changed (a dependency delta, not audited here)',
                  *(f'{p} {(o or "-")[:12]} -> {(n or "-")[:12]}' for p, o, n in inv['gitlinks'])]
    if inv['binary_changed']:
        lines.append(f"binary files changed: {len(inv['binary_changed'])}")
    return '\n'.join(lines)


def per_kind(growth, name):
    rows = [r for r in growth if r[2] == name][:TOP_PER_KIND]
    return [f'-- {name}', *(f'{s:9d} {g:+9d} {p}' for s, g, _, p in rows)] if rows else []


def create(path, text, mode=0o666, newline=None):
    """Write text to a new file, mode masked by the umask; FileExistsError when path exists, and no file left when
    the write fails."""
    flags = os.O_WRONLY | os.O_CREAT | os.O_EXCL | getattr(os, 'O_BINARY', 0)    # newlines translated once, by Python
    fd = os.open(path, flags, mode)
    try:
        with os.fdopen(fd, 'w', encoding='utf-8', newline=newline) as f:
            f.write(text)
    except BaseException:
        Path(path).unlink(missing_ok=True)
        raise


def aware(text):
    """An ISO 8601 time, a naive one taken as local; ValueError when text is not one."""
    t = datetime.datetime.fromisoformat(text.replace('Z', '+00:00'))
    return t if t.tzinfo else t.astimezone()


def when(text):
    try:
        return aware(text)
    except ValueError:
        raise Refused(f'{text}: not an ISO 8601 time') from None


class Shape:
    """Collects every way a JSON value departs from the record schema, each named by its path."""

    def __init__(self):
        self.problems = []

    def bad(self, where, what):
        self.problems.append(f'{where}: {what}')

    def obj(self, v, where, required, optional=()):
        if not isinstance(v, dict):
            self.bad(where, 'not an object')
            return False
        for k in required:
            if k not in v:
                self.bad(where, f'missing {k}')
        for k in sorted(set(v) - set(required) - set(optional)):
            self.bad(where, f'unknown field {k}')
        return True

    def text(self, v, where, pattern=None, what='a non-empty string'):
        if not isinstance(v, str) or not v.strip() or (pattern and not pattern.fullmatch(v)):
            self.bad(where, f'not {what}: {v!r}'[:200])
            return False
        return True

    def texts(self, v, where):
        if not isinstance(v, list) or not v:
            self.bad(where, 'not a non-empty list of strings')
            return
        for i, x in enumerate(v):
            self.text(x, f'{where}[{i}]')

    def enum(self, v, where, allowed):
        if not isinstance(v, str) or v not in allowed:
            self.bad(where, f'{v!r} is not one of {", ".join(sorted(allowed))}')
            return False
        return True

    def items(self, v, where):
        """(path, item) of each object in the list v."""
        if not isinstance(v, list):
            self.bad(where, 'not a list')
            return []
        out = []
        for i, x in enumerate(v):
            if isinstance(x, dict):
                out.append((f'{where}[{i}]', x))
            else:
                self.bad(f'{where}[{i}]', 'not an object')
        return out

    def time(self, v, where):
        try:
            if not isinstance(v, str):
                raise ValueError
            aware(v)
        except ValueError:
            self.bad(where, f'not an ISO 8601 time: {v!r}'[:200])
            return False
        return True


def unique(s, where, ids):
    for i, n in Counter(ids).items():
        if n > 1:
            s.bad(where, f'id {i} used {n} times')


def by_status(s, v, where, table, fields=()):
    """v's status picks, from table, the text fields it must carry besides fields; False for an unknown status."""
    if not s.enum(v.get('status'), f'{where}.status', set(table)):
        return False
    if s.obj(v, where, ('status', *fields, *table[v['status']])):
        for k in table[v['status']]:
            s.text(v.get(k), f'{where}.{k}')
    return True


def supersession_cycle(start, by):
    seen, i = set(), start
    while i in by and i not in seen:
        seen.add(i)
        i = by[i]
    return i in seen


def check_review(s, r, rng=None, carried=None, stages=STAGES):
    """The lead's review fields, each checked when present (the caller reports a missing one). rng is the run's
    exact base..R; carried the previous record's live item ids (None: not checked)."""
    if 'range' in r and s.text(r['range'], 'range') and rng and r['range'] != rng:
        s.bad('range', f'{r["range"]!r} is not this run\'s range {rng}: the review was written for another head')
    if 'pr' in r and s.obj(r['pr'], 'pr', ('repo', 'number')):
        s.text(r['pr'].get('repo'), 'pr.repo', REPO, 'owner/name')
        n = r['pr'].get('number')
        if type(n) is not int or n < 1:
            s.bad('pr.number', f'not a positive integer: {n!r}')
    if 'outcome' in r:
        s.text(r['outcome'], 'outcome')
    if 'stages' in r and s.obj(r['stages'], 'stages', stages, r['stages'] if isinstance(r['stages'], dict) else ()):
        for name, st in r['stages'].items():
            w = f'stages.{name}'
            if not STAGE_NAME.fullmatch(name):
                s.bad(w, 'not a stage name')
            if isinstance(st, dict):
                by_status(s, st, w, STAGE_FIELDS)
            else:
                s.bad(w, 'not an object')
    if 'coverage' in r:
        if r['coverage'] == []:
            s.bad('coverage', 'empty: every changed path belongs to a batch')
        for w, b in s.items(r['coverage'], 'coverage'):
            if s.obj(b, w, ('batch', 'paths')):
                s.text(b.get('batch'), f'{w}.batch')
                s.texts(b.get('paths'), f'{w}.paths')
    for w, c in s.items(r.get('claims', []), 'claims'):
        if s.obj(c, w, ('claim', 'document', 'revision', 'section', 'variants', 'status')):
            for k in ('claim', 'document', 'revision', 'section'):
                s.text(c.get(k), f'{w}.{k}')
            s.texts(c.get('variants'), f'{w}.variants')
            s.enum(c.get('status'), f'{w}.status', {'verified', 'refuted', 'pending'})
    items = []
    for field in ('decisions', 'open'):
        for w, it in s.items(r.get(field, []), field):
            if not s.obj(it, w, ('id', 'text', 'status'), ('by', 'ref')):
                continue
            s.text(it.get('id'), f'{w}.id', ITEM_ID, 'an id')
            s.text(it.get('text'), f'{w}.text')
            if 'ref' in it:
                s.text(it['ref'], f'{w}.ref')
            if not s.enum(it.get('status'), f'{w}.status', LIVE | SETTLED):
                it = dict(it, status=None)
            items.append((w, it))
    ids = [it['id'] for _, it in items if isinstance(it.get('id'), str)]
    unique(s, 'decisions/open', ids)
    by = {it['id']: it['by'] for _, it in items
          if it.get('status') == 'superseded' and isinstance(it.get('id'), str) and isinstance(it.get('by'), str)}
    for w, it in items:
        superseded = it.get('status') == 'superseded'
        if superseded != ('by' in it):
            s.bad(w, 'superseded needs by, and only superseded takes it')
        elif superseded and (not isinstance(it['by'], str) or it['by'] not in ids or it['by'] == it.get('id')):
            s.bad(f'{w}.by', f'{it["by"]!r} is no other item\'s id')
        elif superseded and isinstance(it.get('id'), str) and supersession_cycle(it['id'], by):
            s.bad(f'{w}.by', f'{it["id"]} is superseded in a cycle: its chain must end in an item that is not '
                             'superseded, or every item of it drops out of later audits')
        if carried is None or not isinstance(it.get('id'), str):
            continue
        if it['id'] in carried and it.get('status') not in SETTLED:
            s.bad(f'{w}.status', f'carried item {it["id"]} must be carried, resolved or superseded')
        elif it['id'] not in carried and it.get('status') == 'carried':
            s.bad(f'{w}.status', f'{it["id"]} was not carried from the previous record')
    for i in sorted(set(carried or ()) - set(ids)):
        s.bad('decisions/open', f'carried item {i} is missing: mark it carried, resolved or superseded')
    for w, p in s.items(r.get('policy', []), 'policy'):
        if s.obj(p, w, ('question', 'answer', 'date')):
            s.text(p.get('question'), f'{w}.question')
            s.text(p.get('answer'), f'{w}.answer')
            s.text(p.get('date'), f'{w}.date', DATE, 'a YYYY-MM-DD date')
    # totals across hosts with their count: a host name has no field to go in
    if 'usage' in r and s.obj(r['usage'], 'usage', ('totals', 'hosts', 'window')):
        u = r['usage']
        totals, hosts = u.get('totals'), u.get('hosts')
        if type(hosts) is not int or hosts < 0:
            s.bad('usage.hosts', f'not a host count: {hosts!r}'[:200])
            hosts = None
        if not isinstance(totals, dict):
            s.bad('usage.totals', 'not an object of totals across hosts')
        else:
            for k, v in totals.items():
                if not TOTAL_NAME.fullmatch(k) or type(v) not in (int, float) or v < 0:
                    s.bad('usage.totals', f'{k!r}: a total is a camelCase name and a non-negative number')
            if hosts == 0 and totals:
                s.bad('usage.totals', 'totals from no host: hosts 0 means no transcripts were read')
            elif hosts and not totals:
                s.bad('usage.totals', 'empty: give the totals across hosts, or hosts 0 when no transcripts were read')
        win = u.get('window')
        if s.obj(win, 'usage.window', ('since', 'until')) and s.time(win.get('since'), 'usage.window.since') \
                and s.time(win.get('until'), 'usage.window.until') and aware(win['since']) >= aware(win['until']):
            s.bad('usage.window', 'since is not before until')
    # which checks the range needs is the repository's contracts' call, reviewed with the record; here only that
    # each kind is present and every item names the selector or contract that produced it
    if 'validation' in r and s.obj(r['validation'], 'validation', ('expected',)):
        kinds = set()
        expected = r['validation'].get('expected')
        if expected == []:
            s.bad('validation.expected', 'empty: list what the build and HIL contracts select for the range')
        found = s.items(expected, 'validation.expected')
        for w, e in found:
            if s.enum(e.get('kind'), f'{w}.kind', {'software', 'hardware'}):
                kinds.add(e['kind'])
            if not by_status(s, e, w, EXPECTED_FIELDS, ('id', 'check', 'kind', 'source')):
                continue
            s.text(e.get('id'), f'{w}.id', ITEM_ID, 'an id')
            s.text(e.get('check'), f'{w}.check')
            s.text(e.get('source'), f'{w}.source')
            if e['status'] == 'required-pending':
                s.bad(w, f'{e.get("id")} is required-pending: run it, or record the owner\'s exclusion')
        unique(s, 'validation.expected', [e['id'] for _, e in found if isinstance(e.get('id'), str)])
        if expected:
            for k in sorted({'software', 'hardware'} - kinds):
                s.bad('validation.expected', f'no {k} item: list it, inapplicable with a reason if nothing applies')
    return s


GLOB = {'**/': '(?:.*/)?', '**': '.*', '*': '[^/]*', '?': '[^/]'}


def glob_re(pattern):
    """A coverage glob as a regex: ** spans directories, * and ? stay within one."""
    return re.compile(''.join(GLOB.get(t) or re.escape(t) for t in re.split(r'(\*\*/|\*\*|\*|\?)', pattern)), re.S)


def changed_paths(repo, base, end):
    """Every path base..end touches: both sides of a rename or copy, a deletion by its old path, gitlinks too."""
    out = git(repo, 'diff', '--name-status', '-z', '-M', '--no-ext-diff', '--ignore-submodules=none', base,
              end).stdout.split('\0')
    paths, i = set(), 0
    while i < len(out) - 1:
        n = 2 if out[i][:1] in 'RC' else 1
        paths.update(out[i + 1:i + 1 + n])
        i += 1 + n
    return sorted(paths)


def uncovered(repo, base, end, coverage):
    """[reason] when a path base..end changes matches no glob of a schema-valid coverage ledger."""
    globs = [glob_re(g) for b in coverage for g in b['paths']]
    missed = [c for c in changed_paths(repo, base, end) if not any(g.fullmatch(c) for g in globs)]
    return [f'coverage: {len(missed)} paths changed in {base[:12]}..{end[:12]} match no batch glob: '
            f'{", ".join(missed)}'] if missed else []


def private(value, where='', found=None):
    """[(path, matched text)] for every key or string that looks private."""
    found = [] if found is None else found
    if isinstance(value, dict):
        for k, v in value.items():
            private(k, f'{where}.{k} (key)', found)
            private(v, f'{where}.{k}', found)
    elif isinstance(value, list):
        for i, v in enumerate(value):
            private(v, f'{where}[{i}]', found)
    elif isinstance(value, str) and (m := PRIVATE.search(CI_URL.sub(lambda u: CI_ID.sub('id', u[0]), value))):
        found.append((where.lstrip('.'), m.group()))
    return found


RECORD_FIELDS = ('schema', 'identity', 'base', 'baseKind', 'prBase', 'reviewedHead', 'completed')
RECORDED = ('pr', 'outcome', 'stages', 'coverage', 'claims', 'decisions', 'open', 'policy', 'usage', 'validation')
REVIEW_FIELDS = ('range', *RECORDED)


def check_record(rec):
    s = Shape()
    if not s.obj(rec, 'record', RECORD_FIELDS + RECORDED):
        return s.problems
    if rec.get('schema') != SCHEMA:
        s.bad('schema', f'{rec.get("schema")!r} is not {SCHEMA}')
    ident = rec.get('identity')
    if not isinstance(ident, list) or not ident or not all(isinstance(x, str) and SHA.fullmatch(x) for x in ident):
        s.bad('identity', 'not a list of root commits')
    for k in ('base', 'prBase', 'reviewedHead'):
        s.text(rec.get(k), k, SHA, 'a commit SHA')
    s.enum(rec.get('baseKind'), 'baseKind', {'audit', 'bootstrap', 'override'})
    s.time(rec.get('completed'), 'completed')
    return check_review(s, rec).problems


def report_blocks(body):
    """The JSON of each fenced block in a comment whose first line is VALIDATION (None where it does not parse)."""
    blocks = []
    for m in FENCE.finditer(body.replace('\r\n', '\n')):
        first, _, rest = m.group(1).partition('\n')
        if first.strip() == VALIDATION:
            try:
                blocks.append(json.loads(rest))
            except ValueError:
                blocks.append(None)
    return blocks


def check_report(s, report):
    """A validation report's shape, the same for the results file report reads and the comment it prints."""
    if not s.obj(report, 'report', ('end', 'items')):
        return
    s.text(report.get('end'), 'report.end', SHA, 'a commit SHA')
    found = s.items(report.get('items'), 'report.items')
    for w, i in found:
        if s.obj(i, w, ('id', 'result', 'ref')):
            s.text(i.get('id'), f'{w}.id', ITEM_ID, 'an id')
            s.enum(i.get('result'), f'{w}.result', {'pass', 'fail'})
            s.text(i.get('ref'), f'{w}.ref')
    unique(s, 'report.items', [i['id'] for _, i in found if isinstance(i.get('id'), str)])


def validation_problems(rec, end):
    """Why the record's validation is not complete: the newest report by a writer must name end and pass every
    expected item recorded as covered."""
    pr = rec['pr']
    url = f'repos/{pr["repo"]}/issues/{pr["number"]}/comments'
    # pages are numbered: a comment deleted during a read shifts another across a page boundary, out of the listing;
    # one appended between the two listings shifts none
    first = [c.get('id') for c in api(url, paginate=True)]
    listed = api(url, paginate=True)
    if first != [c.get('id') for c in listed][:len(first)]:
        raise Refused(f'the comments on {pr["repo"]}#{pr["number"]} changed while they were read: run it again')
    comments = sorted(listed, key=lambda c: (c.get('created_at') or '', c.get('id') or 0))
    writers = {}
    for c in reversed(comments):
        blocks = report_blocks(c.get('body') or '')
        if not blocks:
            continue
        login = (c.get('user') or {}).get('login') or ''
        if login not in writers:
            try:
                perm = api(f'repos/{pr["repo"]}/collaborators/{login}/permission')
                level = perm.get('permission') if isinstance(perm, dict) else None
                if level not in LEVELS:
                    raise Refused(f'no permission level in {str(perm)[:200]}')
            except Refused as e:
                # a caller without push access gets 403 for anyone; the repository's owner needs no lookup
                if isinstance(e, HttpError) and e.status == 403 and c.get('author_association') == 'OWNER':
                    level = 'admin'
                else:
                    # this report may be the newest valid one: an older one must not stand in for it
                    return [f'validation report {c.get("html_url") or c.get("id")} by {login}: permission unknown: '
                            f'{e}']
            writers[login] = level in WRITERS
        if writers[login]:
            return report_problems(blocks[-1], rec, end, login)
    return [f'no validation report on {pr["repo"]}#{pr["number"]} by an account with write access']


def report_problems(report, rec, end, login):
    where = f'validation report by {login}'
    s = Shape()
    check_report(s, report)
    if s.problems:
        return [f'{where}: {p}' for p in s.problems]
    problems = [f'{where} names end {report["end"]}, not {end}'] if report['end'] != end else []
    results = {i['id']: i['result'] for i in report['items']}
    problems += [f'{where}: {i} is {res}' for i, res in sorted(results.items()) if res != 'pass']
    problems += [f'{where}: expected item {e["id"]} ({e["check"]}) is missing' for e in rec['validation']['expected']
                 if e['status'] == 'covered' and e['id'] not in results]
    return problems


def end_problems(repo, rec, end):
    """Why E is not the recorded PR's merge of P = R + the record."""
    pr = rec['pr']
    where = f'{pr["repo"]}#{pr["number"]}'
    try:
        data = api(f'repos/{pr["repo"]}/pulls/{pr["number"]}')
    except HttpError as e:
        if e.status != 404:
            raise
        try:
            api(f'repos/{pr["repo"]}')
        except HttpError as r:
            if r.status != 404:
                raise
            raise Refused(f'{pr["repo"]} cannot be read ({r}): GitHub answers 404 for a repository the token cannot '
                          'see too') from None
        return [f'{where} does not exist: {e}']
    if not data.get('merged'):
        return [f'{where} is not merged']
    problems, merged = [], data.get('merge_commit_sha')
    if merged != end:
        problems.append(f'{where} merged as {merged}, but the record last changed in {end}')
        if SHA.fullmatch(str(merged)) and len(git(repo, 'rev-list', '--parents', '-n1', merged,
                                                  check=False).stdout.split()) > 2:
            problems[-1] += ', by an ordinary merge: the record verifies only after a squash or rebase merge'
    p = (data.get('head') or {}).get('sha') or ''
    if not SHA.fullmatch(p):
        return problems + [f'{where} has no head SHA']
    if not has_commit(repo, p):
        ref = f'refs/pull/{pr["number"]}/head'
        fetched = fetch(repo, pr['repo'], ref, check=False)
        # exit 2: no such ref, whatever language the fetch's message came in
        if fetched.returncode and git(repo, 'ls-remote', '--exit-code', f'{GITHUB}{pr["repo"]}.git', ref,
                                      check=False, network=True).returncode == 2:
            return problems + [f'{pr["repo"]} has no {ref}']
        if fetched.returncode:
            raise Refused(f'git fetch {ref}: {failure(fetched.returncode, fetched.stderr)}')
        if not has_commit(repo, p):
            return problems + [f'{ref} of {pr["repo"]} did not bring the head the API names, {p}']
    parents = git(repo, 'rev-list', '--parents', '-n1', p).stdout.split()[1:]
    if parents != [rec['reviewedHead']]:
        problems.append(f'PR head {p[:12]} has parents {[x[:12] for x in parents]}, not the reviewed head '
                        f'{rec["reviewedHead"][:12]} alone')
        return problems
    if changed_paths(repo, rec['reviewedHead'], p) != [RECORD]:
        problems.append(f'PR head {p[:12]} changes more than {RECORD} over the reviewed head')
    if git(repo, 'rev-parse', f'{p}^{{tree}}').stdout != git(repo, 'rev-parse', f'{end}^{{tree}}').stdout:
        problems.append(f'merge commit {end[:12]} has another tree than PR head {p[:12]}: the base branch moved or '
                        'the merge changed it')
    m = rec['prBase']
    if not is_ancestor(repo, rec['base'], m):
        problems.append(f'the PR base {m[:12]} does not descend from the base {rec["base"][:12]}')
    for name, later in (('reviewed head', rec['reviewedHead']), ('merge commit', end)):
        if not is_ancestor(repo, m, later):
            problems.append(f'the PR base {m[:12]} is not in the {name} {later[:12]}')
    return problems


def provenance(repo, head):
    """(problems, record, end): why the record at head is not the one its merged audit PR committed, [] if it is."""
    shown = git(repo, 'cat-file', 'blob', f'{head}:{RECORD}', check=False)
    if shown.returncode:
        return [f'no {RECORD} at {head[:12]}: pass --base with --base-kind bootstrap'], None, None
    try:
        rec = json.loads(shown.stdout)
    except ValueError as e:
        return [f'{RECORD} is not JSON: {e}'], None, None
    problems = check_record(rec)
    if problems:
        return problems, rec, None
    if rec['identity'] != identity(repo, head):
        return ['the record names another repository (identity differs)'], rec, None
    end = git(repo, 'rev-list', '-n1', head, '--', RECORD).stdout.strip()
    return end_problems(repo, rec, end) or uncovered(repo, rec['base'], rec['reviewedHead'], rec['coverage']), \
        rec, end


def verify(repo, head):
    """(status, reasons, record, end): status complete, provisional or invalid."""
    problems, rec, end = provenance(repo, head)
    if problems:
        return 'invalid', problems, rec, end
    problems = validation_problems(rec, end)
    return ('provisional' if problems else 'complete'), problems, rec, end


def state_dir():
    return Path(os.environ.get('XDG_STATE_HOME') or Path.home() / '.local' / 'state') / 'agentrc' / 'repo-audit'


def toplevel(path):
    repo = Path(git(path, 'rev-parse', '--show-toplevel').stdout.strip())
    plain_history(repo)
    return repo


def read_json(path):
    try:
        return json.loads(Path(path).read_text(encoding='utf-8'))
    except ValueError as e:
        raise Refused(f'{path} is not JSON: {e}') from None


def check(args):
    repo = toplevel(args.repo)
    status, reasons, rec, end = verify(repo, commit(repo, args.head))
    print(f'{status}: end {end}, {rec["pr"]["repo"]}#{rec["pr"]["number"]}' if status == 'complete' else status)
    for r in reasons:
        print(f'- {r}')
    return 0 if status == 'complete' else 1


def report(args):
    repo = toplevel(args.repo)
    head = commit(repo, args.head)
    problems, rec, end = provenance(repo, head)
    if problems:
        raise Refused('the record at --head does not verify:\n' + '\n'.join(f'- {p}' for p in problems))
    if end != head:
        raise Refused(f'--head {head[:12]} is not the merge commit that last changed the record, {end}')
    results = read_json(args.results)
    s = Shape()
    check_report(s, results)
    if not s.problems and results['end'] != end:
        s.bad('report.end', f'names {results["end"]}, not --head {end}: results for another commit')
    if not s.problems:
        covered = {e['id'] for e in rec['validation']['expected'] if e['status'] == 'covered'}
        got = {i['id'] for i in results['items']}
        if got != covered:
            s.bad('report.items', f'ids must be the record\'s covered items exactly: missing {sorted(covered - got)}, '
                                  f'not expected {sorted(got - covered)}')
    problems = s.problems + [f'{w}: looks private ({m!r})' for w, m in private(results)]
    if problems:
        raise Refused('\n'.join(problems))
    text = render(repo, rec, end, results['items'])
    block = f'\n```text\n{VALIDATION}\n{json.dumps({"end": end, "items": results["items"]}, indent=1)}\n```\n'
    if len(text) + len(block) > COMMENT_LIMIT:
        # the record is fixed once merged: only the report can give way, never the block check reads
        note = f'\n*The report is cut here to fit GitHub\'s comment limit; the record at `{end[:12]}` holds the ' \
            'rest.*\n'
        room = COMMENT_LIMIT - len(block) - len(note)
        if room < 0:
            raise Refused(f'the validation block is {len(block)} characters, over GitHub\'s {COMMENT_LIMIT} with the '
                          'report\'s note: shorten the results\' refs')
        text = text[:text.rfind('\n', 0, room) + 1] + note
    body = text + block
    if report_blocks(body) != [{'end': end, 'items': results['items']}]:
        raise Refused('the rendered report hides or adds a validation block: a record string escaped its line')
    print(body, end='')
    return 0


def one_line(v):
    """A record string or git path on one line: printed after a fixed prefix, it cannot open a fence or a heading."""
    return ' '.join(str(v).split())


def change_rows(inv):
    k, tl = inv['kinds'], inv['test_lines']
    added, deleted = (sum(k[n]['lines'][i] for n in KINDS) for i in (0, 1))
    rows = [('tests', tl, inv['test_bytes']),
            ('everything else', [added - tl[0], deleted - tl[1]],
             [inv['bytes'][i] - inv['test_bytes'][i] for i in (0, 1)]),
            *((f'by kind: {n}', k[n]['lines'], k[n]['bytes']) for n in KINDS)]
    t = inv['tests_added_removed']
    return ['| | lines added | lines deleted | bytes before | bytes after |', '|---|--:|--:|--:|--:|',
            *(f'| {name} | {a} | {d} | {b0} | {b1} |' for name, (a, d), (b0, b1) in rows), '',
            f"commits {inv['commits']}; test functions +{sum(r[0] for r in t)} -{sum(r[1] for r in t)}; "
            f"{len(inv['binary_changed'])} binary files changed"]


def pin_lines(inv):
    return ['', 'Submodule pins changed (a dependency delta, not audited here):',
            *(f'- `{one_line(p)}` {(o or "-")[:12]} -> {(n or "-")[:12]}' for p, o, n in inv['gitlinks'])] \
        if inv['gitlinks'] else []


def item_line(i):
    by = f' by `{i["by"]}`' if i.get('by') else ''
    ref = f' ({one_line(i["ref"])})' if i.get('ref') else ''
    return f'- `{i["id"]}` {i["status"]}{by}: {one_line(i["text"])}{ref}'


def render(repo, rec, end, items):
    """The audit report above the validation block, from the record at E, git and the results on E."""
    base, m, r = rec['base'], rec['prBase'], rec['reviewedHead']
    failed = [i['id'] for i in items if i['result'] != 'pass']
    status = f'**validation FAILED on `{end[:12]}`: {", ".join(failed)}**' if failed else \
        f'validation on `{end[:12]}`: all {len(items)} covered checks pass'
    growth, own = inventory(repo, base, m), inventory(repo, m, r)
    out = [f'## Repository audit {rec["pr"]["repo"]}#{rec["pr"]["number"]}', '',
           f'- {status}',
           f'- reviewed `{base[:12]}..{r[:12]}` (base: {rec["baseKind"]}), merged as `{end[:12]}`, record prepared '
           f'{rec["completed"]}',
           f'- **Outcome:** {one_line(rec["outcome"])}', '',
           f'### This audit\'s changes `{m[:12]}..{r[:12]}`', '', *change_rows(own), *pin_lines(own), '',
           f'### Growth since the base `{base[:12]}..{m[:12]}`', '', *change_rows(growth), *pin_lines(growth),
           '', '### Decisions', '', *([*map(item_line, rec['decisions'])] or ['none']), '',
           '### Deferred work', '',
           *([*map(item_line, sorted(rec['open'], key=lambda o: o['status'] not in LIVE))] or ['none']), '',
           f'### Validation on `{end[:12]}`', '']
    results = {i['id']: i for i in items}
    for e in rec['validation']['expected']:
        head = f'- `{e["id"]}` {e["kind"]}: {one_line(e["check"])} ({one_line(e["source"])}): '
        if e['status'] == 'covered':
            res = results[e['id']]
            out.append(head + f'{res["result"]} ({one_line(res["ref"])}; on R: {one_line(e["ref"])})')
        else:
            out.append(head + f'{e["status"]}: {one_line(e.get("reason") or e.get("owner"))}')
    claims = Counter(c['status'] for c in rec['claims'])
    usage = rec['usage']
    totals = ', '.join(f'{k} {v}' for k, v in usage['totals'].items())
    out += ['', '### Evidence', '',
            *(f'- {name}: ' + (f'completed ({one_line(st["ref"])})' if st['status'] == 'completed' else
                               f'inapplicable: {one_line(st["reason"])}') for name, st in rec['stages'].items()),
            '- claim records: ' + (', '.join(f'{claims[k]} {k}' for k in ('verified', 'refuted', 'pending')) if claims
                            else 'none'),
            *(f'  - pending: {one_line(c["claim"])} ({one_line(c["document"])} {one_line(c["revision"])} '
              f'{one_line(c["section"])})' for c in rec['claims'] if c['status'] == 'pending'),
            '- usage: ' + (f'{totals} over {usage["hosts"]} host{"s" * (usage["hosts"] != 1)}, '
                           f'{one_line(usage["window"]["since"])} .. {one_line(usage["window"]["until"])}; zero '
                           'recorded use is not proof of no use'
                           if usage['hosts'] else 'not measured')]
    return '\n'.join(out) + '\n'


def record_blob(repo, rev):
    """The object id of the record at rev, None without one."""
    return git(repo, 'rev-parse', '--verify', '--quiet', f'{rev}:{RECORD}', check=False).stdout.strip() or None


def carry(repo, head):
    """carried.json: the record at head, for finish to find unchanged at R, and from a schema-valid record of this
    repository its live items and policy, whichever base the run takes."""
    blob = record_blob(repo, head)
    carried = {'record': blob, 'pr': None, 'decisions': [], 'open': [], 'policy': []}
    try:
        rec = json.loads(git(repo, 'cat-file', 'blob', blob).stdout) if blob else None
    except (Refused, ValueError):
        rec = None
    if rec is not None and not check_record(rec) and rec['identity'] == identity(repo, head):
        carried.update(pr=rec['pr'], decisions=[d for d in rec['decisions'] if d['status'] in LIVE],
                       open=[o for o in rec['open'] if o['status'] in LIVE], policy=rec['policy'])
    return carried


def start(args):
    repo = toplevel(args.repo)
    head = commit(repo, args.head)
    if bool(args.base) != bool(args.base_kind):
        raise Refused('--base and --base-kind go together')
    carried = carry(repo, head)
    if args.base:
        if args.base_kind == 'bootstrap' and carried['record']:
            raise Refused(f'{head[:12]} holds {RECORD}: a base replacing it is --base-kind override')
        base, base_kind = commit(repo, args.base), args.base_kind
    else:
        status, reasons, rec, end = verify(repo, head)
        if status != 'complete':
            prior = rec.get('base') if isinstance(rec, dict) else None
            hint = f'\npass --base {prior} --base-kind override' \
                if SHA.fullmatch(str(prior)) and is_ancestor(repo, prior, head) else ''
            raise Refused(f'the last audit record is {status}:\n' + '\n'.join(f'- {r}' for r in reasons) + hint)
        base, base_kind = end, 'audit'
    if not is_ancestor(repo, base, head):
        raise Refused(f'base {base[:12]} is not an ancestor of head {head[:12]}')
    since = when(args.since) if args.since else \
        aware(git(repo, 'log', '-1', '--no-show-signature', '--format=%cI', base).stdout.strip())
    until = when(args.until) if args.until else datetime.datetime.now().astimezone()
    if since >= until:
        raise Refused('--since must be before --until')
    since, until = since.isoformat(), until.isoformat()
    roots = identity(repo, head)
    if args.out:
        out = Path(args.out)
    else:
        key = hashlib.sha256('\n'.join(roots).encode()).hexdigest()[:12]
        out = state_dir() / key / f'{time.strftime("%Y%m%dT%H%M%S")}-{secrets.token_hex(3)}'
    out.mkdir(parents=True, exist_ok=False, mode=0o700)
    status = git(repo, '--no-optional-locks', 'status', '--porcelain').stdout.splitlines()
    tool = Path(__file__).resolve().parent
    tool_rev = git(tool, 'rev-parse', 'HEAD', check=False).stdout.strip() or None
    run = {'format': RUN_FORMAT, 'repo': str(repo), 'identity': roots, 'base': base, 'baseKind': base_kind,
           'start_head': head, 'since': since, 'until': until, 'tool_revision': tool_rev,
           'dirty': sum(not s.startswith('??') for s in status), 'untracked': sum(s.startswith('??') for s in status)}
    inv = inventory(repo, base, head)
    create(out / 'inventory.json', json.dumps(inv, indent=1) + '\n')
    create(out / 'carried.json', json.dumps(carried, indent=1) + '\n')
    # last: a run.json means start wrote the others; unsynced, a crash can still lose them (finish refuses a lost
    # carried.json)
    create(out / 'run.json', json.dumps(run, indent=1) + '\n')
    print(f'run dir: {out}')
    print(f'window: {since} .. {until}')
    if carried['record'] and carried['pr'] is None:
        print(f'note: {RECORD} at head is no valid record of this repository: nothing carried from it')
    if run['dirty'] or run['untracked']:
        print(f"note: {run['dirty']} modified and {run['untracked']} untracked paths are not in this inventory")
    print(summary(inv))
    return 0


def is_sha(v):
    return isinstance(v, str) and bool(SHA.fullmatch(v))


RUN_FIELDS = {'repo': lambda v: isinstance(v, str) and bool(v),
              'identity': lambda v: isinstance(v, list) and bool(v) and all(map(is_sha, v)),
              'base': is_sha, 'baseKind': lambda v: v in ('audit', 'bootstrap', 'override'), 'start_head': is_sha}
CARRIED_FIELDS = {'record': lambda v: v is None or isinstance(v, str), 'decisions': lambda v: True,
                  'open': lambda v: True}


def require(data, path, fields):
    """Refuse a run file start did not write whole, or that was edited since: fields maps each key to its test."""
    if not isinstance(data, dict):
        raise Refused(f'{path} lacks an object: start again')
    bad = [k for k, ok in fields.items() if k not in data or not ok(data[k])]
    if bad:
        raise Refused(f'{path} lacks a valid {", ".join(bad)}: start again')
    return data


def pr_problems(pr, head):
    """(problems, base branch): why the review's PR is not open on its base repository with head R."""
    where = f'pr: {pr["repo"]}#{pr["number"]}'
    try:
        data = api(f'repos/{pr["repo"]}/pulls/{pr["number"]}')
    except Refused as e:
        return [f'{where} cannot be read: {e}'], None
    if not isinstance(data, dict):
        return [f'{where}: not a pull request: {str(data)[:200]}'], None
    problems = []
    target = str(((data.get('base') or {}).get('repo') or {}).get('full_name'))
    if target.lower() != pr['repo'].lower():
        problems.append(f'{where} targets {target}, not {pr["repo"]}')
    if data.get('state') != 'open':
        problems.append(f'{where} is {data.get("state")!r}, not open')
    got = (data.get('head') or {}).get('sha')
    if got != head:
        problems.append(f'{where} has head {got}, not --head {head}: push R to it first')
    branch = (data.get('base') or {}).get('ref')
    if not isinstance(branch, str) or not branch:
        problems.append(f'{where} has no usable base branch: {branch!r}')
    return problems, branch


def branch_tip(repo, slug, branch):
    """The commit slug's branch names on GitHub now, fetched here when absent; never a tracking ref or FETCH_HEAD."""
    ref = f'refs/heads/{branch}'
    listed = git(repo, 'ls-remote', '--exit-code', f'{GITHUB}{slug}.git', ref, check=False, network=True)
    tips = [line.split('\t')[0] for line in listed.stdout.splitlines() if line.endswith(f'\t{ref}')]
    if listed.returncode or len(tips) != 1 or not SHA.fullmatch(tips[0]):
        if listed.returncode in (0, 2):
            why = listed.stderr.strip() or listed.stdout.strip()[:200]
        else:
            why = failure(listed.returncode, listed.stderr)
        raise Refused(f'{slug} {ref} cannot be read: {why}')
    if not has_commit(repo, tips[0]):
        fetch(repo, slug, ref)
        if not has_commit(repo, tips[0]):
            raise Refused(f'{slug} {ref} moved past {tips[0]} while it was fetched: run it again')
    return tips[0]


def checkout_problems(repo, run, record, reviewed):
    """Why the checkout cannot take the record commit on the reviewed head."""
    problems = []
    if git(repo, 'rev-parse', 'HEAD').stdout.strip() != reviewed:
        problems.append(f'HEAD of {repo} is not --head {reviewed[:12]}: the record commit goes on the reviewed head')
    dirty = [s[3:] for s in git(repo, '--no-optional-locks', 'status', '--porcelain', '--untracked-files=all')
             .stdout.splitlines() if s[3:] != RECORD]
    left = [p for p in dirty if LEFT.fullmatch(p)]
    if left:
        problems.append(f'{", ".join(left)}: left by an interrupted finish; delete it')
    dirty = [p for p in dirty if p not in left]
    if dirty:
        problems.append(f'{repo} has {len(dirty)} uncommitted paths (first: {dirty[0]}): the record commit holds the '
                        'record alone')
    for name in ('base', 'start_head'):
        if not is_ancestor(repo, run[name], reviewed):
            problems.append(f'--head {reviewed[:12]} does not descend from the run\'s {name} {run[name][:12]}')
    if record_blob(repo, reviewed) != record:
        problems.append(f'{RECORD} at --head {reviewed[:12]} is not the one start read at {run["start_head"][:12]}: '
                        'this audit\'s own record commit P is on it (drop P, then finish on R) or another audit\'s '
                        'record landed since (start again from it)')
    for link in ('.claude', RECORD):
        if (repo / link).is_symlink():
            problems.append(f'{link} is a symbolic link: the record goes only into a regular file in .claude/')
    if git(repo, 'check-ignore', '-q', '--', RECORD, check=False).returncode == 0:
        problems.append(f'{RECORD} is ignored by git: its commit would fail')
    return problems


def finish(args):
    out = Path(args.run)
    run = read_json(out / 'run.json')
    if not isinstance(run, dict) or run.get('format') != RUN_FORMAT:
        raise Refused(f'{out} is a run of an older audit.py: start again')
    require(run, out / 'run.json', RUN_FIELDS)
    if not (out / 'carried.json').is_file():
        raise Refused(f'{out} has no carried.json: start did not complete; start again')
    prior = require(read_json(out / 'carried.json'), out / 'carried.json', CARRIED_FIELDS)
    try:
        carried = {i['id'] for i in prior['decisions'] + prior['open']}
    except (TypeError, KeyError):
        raise Refused(f'{out / "carried.json"}: decisions and open are not lists of items: start again') from None
    if args.repo:
        repo = toplevel(args.repo)
    else:
        repo = Path(run['repo'])
        if not repo.is_dir():
            raise Refused(f'{repo}, the checkout start ran in, is gone: pass --repo with a checkout of --head')
        plain_history(repo)
    reviewed = commit(repo, args.head)
    if identity(repo, reviewed) != run['identity']:
        raise Refused(f'{repo} is not the repository this run audited')
    review = read_json(args.review)
    s = Shape()
    if s.obj(review, 'review', REVIEW_FIELDS):
        check_review(s, review, f'{run["base"]}..{reviewed}', carried, FINISH_STAGES)
    problems = s.problems + [f'{w}: looks private ({m!r})' for w, m in private(review)]
    pr_base = None
    if not s.problems:
        pr, branch = pr_problems(review['pr'], reviewed)
        problems += uncovered(repo, run['base'], reviewed, review['coverage']) + pr
        if not pr:
            try:
                pr_base = branch_tip(repo, review['pr']['repo'], branch)
            except Refused as e:
                problems.append(str(e))
        if pr_base:
            # a retarget or push while the tip was read would record a branch the PR no longer targets
            again, now = pr_problems(review['pr'], reviewed)
            problems += again
            if now and now != branch:      # no branch read: again says why
                problems.append(f'pr: {review["pr"]["repo"]}#{review["pr"]["number"]} moved from base branch {branch} '
                                f'to {now} while finish read it: run it again')
            if not is_ancestor(repo, pr_base, reviewed):
                problems.append(f'the base branch {branch} at {pr_base[:12]} is not in --head {reviewed[:12]}: bring '
                                'R up to date with it')
            if not is_ancestor(repo, run['base'], pr_base):
                problems.append(f'the base branch {branch} at {pr_base[:12]} does not descend from the run\'s base '
                                f'{run["base"][:12]}')
    # the checkout last, after the GitHub read: another agent may commit, check out or edit meanwhile
    problems += checkout_problems(repo, run, prior['record'], reviewed)
    if problems:
        raise Refused('\n'.join(problems))
    record = {'schema': SCHEMA, 'identity': run['identity'], 'base': run['base'], 'baseKind': run['baseKind'],
              'prBase': pr_base, 'reviewedHead': reviewed,
              'completed': datetime.datetime.now().astimezone().isoformat(timespec='seconds'),
              **{k: review[k] for k in RECORDED}}
    new = repo / '.claude' / f'.audit.json.{secrets.token_hex(4)}.tmp'
    try:
        (repo / '.claude').mkdir(exist_ok=True)
        create(new, json.dumps(record, indent=2, ensure_ascii=False) + '\n', newline='\n')
        try:
            os.replace(new, repo / RECORD)       # a link raced in at RECORD is replaced, never written through
        finally:
            new.unlink(missing_ok=True)
    except OSError as e:
        raise Refused(f'{RECORD} was not written: {e}') from None
    print(f'wrote {RECORD}; read it, then commit it alone as the PR\'s last commit:')
    print(f'  git add -- {RECORD} && git commit --only -m "Record the audit" -- {RECORD}')
    print('nothing may follow it: the PR merges with that commit as its head, its parent the reviewed head')
    return 0


def main(argv=None):
    p = argparse.ArgumentParser(description=__doc__.split('\n\n')[0])
    sub = p.add_subparsers(dest='cmd', required=True)
    c = sub.add_parser('check')
    c.add_argument('--repo', required=True)
    c.add_argument('--head', required=True)
    s = sub.add_parser('start')
    s.add_argument('--repo', required=True)
    s.add_argument('--head', required=True)
    s.add_argument('--base')
    s.add_argument('--base-kind', choices=('bootstrap', 'override'))
    s.add_argument('--since')
    s.add_argument('--until')
    s.add_argument('--out')
    f = sub.add_parser('finish')
    f.add_argument('--run', required=True)
    f.add_argument('--head', required=True)
    f.add_argument('--review', required=True)
    f.add_argument('--repo')
    r = sub.add_parser('report')
    r.add_argument('--repo', required=True)
    r.add_argument('--head', required=True)
    r.add_argument('--results', required=True)
    args = p.parse_args(argv)
    if hasattr(sys.stdout, 'reconfigure'):
        # the record is UTF-8 whatever the console code page; no \r\n added past the comment limit on Windows
        sys.stdout.reconfigure(encoding='utf-8', newline='\n')
    try:
        return {'check': check, 'start': start, 'finish': finish, 'report': report}[args.cmd](args)
    except (Refused, FileExistsError, FileNotFoundError) as e:
        print(f'refused: {e}', file=sys.stderr)
        return 2


if __name__ == '__main__':
    sys.exit(main())
