"""Tests for repo-audit's scripts on temporary repositories and synthetic transcripts."""
import contextlib
import importlib.util
import io
import json
import ntpath
import os
import re
import subprocess
import sys
import textwrap
import unittest
from pathlib import Path, PurePosixPath
from unittest import mock
sys.path.insert(0, str(Path(__file__).resolve().parent))
from helpers import git_env, guard_survivors, tmpdir  # noqa: E402

SCRIPTS = Path(__file__).resolve().parents[1] / 'skills' / 'repo-audit' / 'scripts'


def load(name):
    spec = importlib.util.spec_from_file_location(f'repo_audit_{name}', SCRIPTS / f'{name}.py')
    mod = importlib.util.module_from_spec(spec)
    spec.loader.exec_module(mod)
    return mod


audit, usage, overlap = load('audit'), load('usage'), load('coverage_overlap')
SINCE, UNTIL = '2026-01-01T00:00:00+00:00', '2026-02-01T00:00:00+00:00'
TOOL = textwrap.dedent('''\
    import argparse
    p = argparse.ArgumentParser()
    sub = p.add_subparsers()
    s = sub.add_parser('send')
    s.add_argument('--lane')
    ''')


def setUpModule():
    guard_survivors(__name__)


def run(main, *argv):
    out, err = io.StringIO(), io.StringIO()
    with mock.patch.dict(os.environ, git_env(), clear=True), contextlib.redirect_stdout(out), \
            contextlib.redirect_stderr(err):
        code = main(list(argv))
    return code, out.getvalue(), err.getvalue()


class Repo:
    def __init__(self, case):
        self.path = tmpdir(case)
        self.git('init', '-q', '-b', 'main')

    def git(self, *args):
        return subprocess.run(['git', '-C', str(self.path), *args], check=True, capture_output=True, text=True,
                              env=git_env()).stdout.strip()

    def commit(self, files, msg='c'):
        for name, body in files.items():
            p = self.path / name
            if body is None:
                p.unlink()
                continue
            p.parent.mkdir(parents=True, exist_ok=True)
            if isinstance(body, bytes):
                p.write_bytes(body)
            else:
                p.write_text(body, newline='\n')      # LF on Windows too: sizes are counted in bytes
        self.git('add', '-A')
        self.git('commit', '-q', '-m', msg)
        return self.git('rev-parse', 'HEAD')


def start(repo, out, *extra, head='main', window=True):
    """audit.py start on repo into out; --base takes --base-kind bootstrap unless extra names a kind."""
    argv = ['start', '--repo', str(repo), '--head', head, '--out', str(out), *extra]
    if '--base' in extra and '--base-kind' not in extra:
        argv += ['--base-kind', 'bootstrap']
    if window:
        argv += ['--since', SINCE, '--until', UNTIL]
    return run(audit.main, *argv)


class Start(unittest.TestCase):
    def setUp(self):
        self.repo = Repo(self)
        self.base = self.repo.commit({'skills/a/SKILL.md': 'one\n', 'tests/test_a.py': 'def test_x():\n  pass\n',
                                      'old.txt': 'x\n' * 40})
        self.head = self.repo.commit({'skills/a/SKILL.md': 'one\ntwo\n', 'tests/test_a.py': 'def test_y():\n  pass\n'
                                      'def test_z():\n  pass\n', 'img.bin': b'\0\1\2', 'old.txt': None,
                                      'new.txt': 'x\n' * 40})
        self.out = tmpdir(self) / 'run'

    def start(self, *extra, **kw):
        return start(self.repo.path, self.out, *extra, **kw)

    def inventory(self):
        return json.loads((self.out / 'inventory.json').read_text(encoding='utf-8'))

    def test_inventory_counts_files_tests_prose_renames_and_binaries(self):
        code, out, _ = self.start('--base', self.base)
        self.assertEqual(code, 0)
        inv = self.inventory()
        self.assertEqual((inv['commits'], inv['files']), (1, [3, 4]))
        self.assertEqual(inv['tests_added_removed'], [[2, 1, 'tests/test_a.py']])
        self.assertEqual(inv['binary_changed'], ['img.bin'])
        self.assertNotIn('old.txt', json.dumps(inv['areas']))      # a rename counts under its new path
        run_json = json.loads((self.out / 'run.json').read_text())
        self.assertEqual((run_json['base'], run_json['start_head']), (self.base, self.head))
        self.assertEqual((run_json['identity'], run_json['baseKind']), ([self.base], 'bootstrap'))
        self.assertEqual(json.loads((self.out / 'carried.json').read_text()),
                         {'record': None, 'pr': None, 'decisions': [], 'open': [], 'policy': []})
        self.assertIn('1 commits', out)

    def test_growth_is_ranked_over_every_kind_and_summarised_per_kind(self):
        base = self.repo.commit({'keep.txt': 'k\n'})       # setUp's files and this one stay unchanged
        self.repo.commit({'src/a.c': 'c\n' * 50, 'doc.md': 'd\n' * 5, 'b.py': 'p\n', 'img.bin': None})
        code, out, _ = self.start('--base', base)
        self.assertEqual(code, 0)
        self.assertEqual(self.inventory()['growth'], [[100, 100, 'c', 'src/a.c'], [10, 10, 'prose', 'doc.md'],
                                                      [0, -3, 'other', 'img.bin'], [2, 2, 'python', 'b.py']])
        ranked = out.split('== largest byte changes per kind (bytes now, change)\n')[1].split('\n==')[0]
        self.assertEqual([line.split() for line in ranked.splitlines()],
                         [['--', 'c'], ['100', '+100', 'src/a.c'], ['--', 'python'], ['2', '+2', 'b.py'],
                          ['--', 'prose'], ['10', '+10', 'doc.md'], ['--', 'other'], ['0', '-3', 'img.bin']])

    def test_output_directory_and_window_are_checked(self):
        self.assertEqual(self.start('--base', self.base)[0], 0)
        code, _, err = self.start('--base', self.base)
        self.assertEqual((code, 'exists' in err), (2, True))
        code, _, err = start(self.repo.path, str(self.out) + '2', '--base', self.base, '--since', UNTIL, '--until',
                             SINCE, window=False)
        self.assertEqual((code, '--since' in err), (2, True))

    def test_the_window_defaults_to_the_base_commit_until_now(self):
        code, out, err = self.start('--base', self.base, window=False)
        self.assertEqual(code, 0, err)
        run_json = json.loads((self.out / 'run.json').read_text())
        committed = audit.aware(self.repo.git('log', '-1', '--format=%cI', self.base))
        self.assertEqual(audit.aware(run_json['since']), committed)
        self.assertLess(audit.aware(run_json['until']) - audit.aware(run_json['since']), audit.datetime.timedelta(1))
        self.assertIn(f"window: {run_json['since']} .. {run_json['until']}", out)

    def test_without_a_record_or_base_it_refuses(self):
        code, _, err = self.start()
        self.assertEqual(code, 2)
        self.assertIn(f'no {audit.RECORD}', err)
        self.assertIn('--base-kind bootstrap', err)
        self.assertFalse(self.out.exists())

    def test_a_base_takes_its_kind(self):
        code, _, err = run(audit.main, 'start', '--repo', str(self.repo.path), '--head', 'main', '--base', self.base,
                           '--out', str(self.out))
        self.assertEqual((code, 'go together' in err), (2, True))
        self.assertEqual(self.start('--base', self.base, '--base-kind', 'override')[0], 0)
        self.assertEqual(json.loads((self.out / 'run.json').read_text())['baseKind'], 'override')

    def test_a_base_over_a_record_is_an_override_that_still_carries_its_items(self):
        rec = dict({k: v for k, v in review(self.base, self.head).items() if k != 'range'}, schema=audit.SCHEMA,
                   identity=[self.base], base=self.base, baseKind='bootstrap', reviewedHead=self.head,
                   completed=SINCE, decisions=[{'id': 'd1', 'text': 'keep', 'status': 'new'},
                                               {'id': 'd0', 'text': 'old', 'status': 'resolved'}])
        self.assertEqual(audit.check_record(rec), [])
        self.repo.commit({audit.RECORD: json.dumps(rec)})
        code, _, err = self.start('--base', self.base)
        self.assertEqual(code, 2)
        self.assertIn(f'holds {audit.RECORD}: a base replacing it is --base-kind override', err)
        self.assertFalse(self.out.exists())
        self.assertEqual(self.start('--base', self.base, '--base-kind', 'override')[0], 0)
        carried = json.loads((self.out / 'carried.json').read_text())
        self.assertEqual(carried['record'], self.repo.git('rev-parse', f'HEAD:{audit.RECORD}'))
        self.assertEqual(([d['id'] for d in carried['decisions']], [o['id'] for o in carried['open']],
                          carried['policy'], carried['pr']), (['d1'], ['o1'], rec['policy'], rec['pr']))
        self.repo.commit({audit.RECORD: '{'})
        out2 = tmpdir(self) / 'run2'
        code, out, err = start(self.repo.path, out2, '--base', self.base, '--base-kind', 'override')
        self.assertEqual(code, 0, err)
        self.assertIn('is no valid record of this repository: nothing carried from it', out)
        carried = json.loads((out2 / 'carried.json').read_text())
        self.assertEqual((carried['record'], carried['decisions'], carried['open']),
                         (self.repo.git('rev-parse', f'HEAD:{audit.RECORD}'), [], []))

    def test_status_is_read_without_taking_the_index_lock(self):
        calls, git = [], audit.git

        def spy(repo, *args, **kw):
            calls.append(args)
            return git(repo, *args, **kw)
        with mock.patch.object(audit, 'git', spy):
            self.assertEqual(self.start('--base', self.base)[0], 0)
        status = [c for c in calls if 'status' in c]
        self.assertTrue(status)
        self.assertTrue(all(c[:2] == ('--no-optional-locks', 'status') for c in status), status)

    def test_the_default_run_dir_is_private_and_unique_within_a_second(self):
        state = tmpdir(self)
        argv = ('start', '--repo', str(self.repo.path), '--head', 'main', '--base', self.base, '--base-kind',
                'bootstrap', '--since', SINCE, '--until', UNTIL)
        with mock.patch.dict(os.environ, {'XDG_STATE_HOME': str(state)}), \
                mock.patch.object(audit.time, 'strftime', return_value='20260101T000000'):
            first, second = run(audit.main, *argv), run(audit.main, *argv)
            with mock.patch.object(audit.secrets, 'token_hex', return_value='abcdef'):
                self.assertEqual(run(audit.main, *argv)[0], 0)
                code, _, err = run(audit.main, *argv)
        self.assertEqual((code, 'exists' in err), (2, True))
        self.assertEqual((first[0], second[0]), (0, 0))
        made = [Path(re.search(r'run dir: (.*)', out).group(1)) for _, out, _ in (first, second)]
        self.assertNotEqual(made[0], made[1])
        key = audit.hashlib.sha256(self.base.encode()).hexdigest()[:12]      # the one root commit
        for d in made:
            self.assertEqual(d.parent, state / 'agentrc' / 'repo-audit' / key)
            self.assertRegex(d.name, r'^20260101T000000-[0-9a-f]{6}$')
            self.assertTrue((d / 'run.json').is_file())
            if os.name == 'posix':
                self.assertEqual(d.stat().st_mode & 0o777, 0o700)

    def test_a_window_compares_instants_not_text(self):
        code, _, _ = self.start('--base', self.base, '--since', '2026-01-01T10:00:00+07:00', '--until',
                                '2026-01-01T04:00:00Z', window=False)
        self.assertEqual(code, 0)

    def test_a_deleted_test_file_counts_its_removed_tests(self):
        self.repo.commit({'tests/test_a.py': None, 'tests/test é b.py': 'def test_q():\n  pass\n'})
        self.assertEqual(self.start('--base', self.head)[0], 0)
        self.assertEqual(sorted(self.inventory()['tests_added_removed']),
                         [[0, 2, 'tests/test_a.py'], [1, 0, 'tests/test é b.py']])

    def test_a_noprefix_config_does_not_change_the_paths(self):
        self.repo.git('config', 'diff.noprefix', 'true')
        self.assertEqual(self.start('--base', self.base)[0], 0)
        self.assertEqual(self.inventory()['tests_added_removed'], [[2, 1, 'tests/test_a.py']])

    def test_an_external_diff_does_not_change_the_counts(self):
        self.repo.git('config', 'diff.external', 'true')
        self.assertEqual(self.start('--base', self.base)[0], 0)
        self.assertEqual(self.inventory()['tests_added_removed'], [[2, 1, 'tests/test_a.py']])

    def test_a_textconv_driver_does_not_change_the_counts(self):
        self.repo.git('config', 'diff.hide.textconv', 'sed -e s/def/xx/')
        (self.repo.path / '.git' / 'info').mkdir(exist_ok=True)
        (self.repo.path / '.git' / 'info' / 'attributes').write_text('*.py diff=hide\n')
        self.assertEqual(self.start('--base', self.base)[0], 0)
        self.assertEqual(self.inventory()['tests_added_removed'], [[2, 1, 'tests/test_a.py']])

    @unittest.skipUnless(os.name == 'posix', 'a double quote cannot be in a Windows file name')
    def test_a_name_git_quotes_is_decoded(self):
        self.repo.commit({'tests/café "q".py': 'def test_q():\n  pass\n'})
        self.assertEqual(self.start('--base', self.head)[0], 0)
        self.assertEqual(self.inventory()['tests_added_removed'], [[1, 0, 'tests/café "q".py']])

    def test_bytes_and_lines_are_totalled_by_kind(self):
        r = self.repo
        base = r.commit({'src/a.c': 'x\n', 'doc.md': 'd\n', 'tests/t.md': 't\n', 'a.py': 'p\n'})
        r.commit({'src/a.c': 'x\ny\n', 'src/b.h': 'h\n', 'doc.md': None, 'tests/t.md': 't\nu\n'})
        self.assertEqual(self.start('--base', base)[0], 0)
        kinds = self.inventory()['kinds']
        before = {'c': 2, 'python': 44, 'prose': 10, 'other': 85}      # with setUp's files
        self.assertEqual({k: v['bytes'][0] for k, v in kinds.items()}, before)
        self.assertEqual({k: v['bytes'][1] - v['bytes'][0] for k, v in kinds.items()},
                         {'c': 4, 'python': 0, 'prose': -2, 'other': 2})
        self.assertEqual({k: v['lines'] for k, v in kinds.items()},
                         {'c': [2, 0], 'python': [0, 0], 'prose': [0, 1], 'other': [1, 0]})

    def test_submodule_pins_are_listed_with_both_commits(self):
        r = self.repo
        one, two = self.base, self.head
        r.git('update-index', '--add', '--cacheinfo', f'160000,{one},lib/dep')
        r.git('update-index', '--add', '--cacheinfo', f'160000,{one},lib/gone')
        r.git('commit', '-q', '-m', 'pin')
        pinned = r.git('rev-parse', 'HEAD')
        r.git('update-index', '--cacheinfo', f'160000,{two},lib/dep')
        r.git('update-index', '--force-remove', 'lib/gone')
        r.git('update-index', '--add', '--cacheinfo', f'160000,{one},lib/new')
        r.git('commit', '-q', '-m', 'bump')
        r.git('config', 'diff.ignoreSubmodules', 'all')      # the inventory agrees with itself all the same
        self.assertEqual(self.start('--base', pinned)[0], 0)
        inv = self.inventory()
        self.assertEqual(inv['gitlinks'], [['lib/dep', one, two], ['lib/gone', one, None], ['lib/new', None, one]])
        self.assertEqual(sorted(row[2] for row in inv['areas']), ['lib/dep', 'lib/new'])     # gone -> new: a rename
        self.assertTrue(inv['shortstat'].startswith('2 files changed'), inv['shortstat'])

    def test_unity_definitions_count_and_prototypes_do_not(self):
        self.repo.commit({'test/unit/test_dcd.c': 'void test_open(void);\nvoid test_open(void)\n{\n}\n'
                                                  'void test_close(void) { TEST_ASSERT(1); }\n'
                                                  'static void helper(void);\n'})
        self.assertEqual(self.start('--base', self.head)[0], 0)
        self.assertEqual(self.inventory()['tests_added_removed'], [[2, 0, 'test/unit/test_dcd.c']])

    def test_the_shortstat_matches_git_from_the_numstat(self):
        r = self.repo
        base = r.commit({'f.txt': 'a\nb\nc\nd\ne\nf\n', 'b.bin': b'\0\1'})
        r.git('mv', 'f.txt', 'g.txt')
        r.git('commit', '-q', '-m', 'rename')
        renamed = r.git('rev-parse', 'HEAD')
        (r.path / 'g.txt').chmod(0o755)         # else a later add -A stages the old mode back
        r.git('update-index', '--chmod=+x', 'g.txt')
        r.git('commit', '-q', '-m', 'mode')
        mode = r.git('rev-parse', 'HEAD')
        binary = r.commit({'b.bin': b'\0\3'})
        added = r.commit({'g.txt': 'a\nb\nc\nd\ne\nf\nx\n'})
        removed = r.commit({'g.txt': 'b\nc\nd\ne\nf\nx\n'})
        cases = [(base, base, ''), (base, renamed, '1 file changed, 0 insertions(+), 0 deletions(-)'),
                 (renamed, mode, '1 file changed, 0 insertions(+), 0 deletions(-)'),
                 (mode, binary, '1 file changed, 0 insertions(+), 0 deletions(-)'),
                 (binary, added, '1 file changed, 1 insertion(+)'), (added, removed, '1 file changed, 1 deletion(-)'),
                 (base, removed, '2 files changed, 1 insertion(+), 1 deletion(-)'),
                 (self.base, removed, '6 files changed, 10 insertions(+), 1 deletion(-)')]
        with mock.patch.dict(os.environ, git_env(), clear=True):
            for a, b, want in cases:
                self.assertEqual(audit.shortstat(audit.numstat(r.path, a, b)), want, (a, b))


def review(base, reviewed, **fields):
    """A review file finish accepts for the range base..reviewed, fields replacing its own."""
    r = {'range': f'{base}..{reviewed}', 'pr': {'repo': 'o/r', 'number': 7}, 'outcome': 'growth reviewed, one cut',
         'stages': {name: {'status': 'completed', 'ref': 'o/r#7'} for name in audit.STAGES},
         'coverage': [{'batch': 'all', 'paths': ['**']}],
         'claims': [{'claim': 'FIFO flush needs a wait', 'document': 'RM0090', 'revision': '19',
                     'section': '34.17.2', 'variants': ['STM32F4'], 'status': 'verified'}],
         'decisions': [{'id': 'd1', 'text': 'keep the retry', 'status': 'new'}],
         'open': [{'id': 'o1', 'text': 'split the parser', 'status': 'new', 'ref': 'o/r#8'}],
         'policy': [{'question': 'deprecate tu_x?', 'answer': 'keep it', 'date': '2026-11-08'}],
         'usage': {'totals': {'sessions': 3, 'toolCalls': 40}, 'hosts': 2, 'window': {'since': SINCE, 'until': UNTIL}},
         'validation': {'expected': [
             {'id': 'suite', 'check': 'BUILD.md suite', 'kind': 'software', 'source': 'BUILD.md', 'status': 'covered',
              'ref': 'CI run 1 on R'},
             {'id': 'hil', 'check': 'HIL battery', 'kind': 'hardware', 'source': 'HIL contract', 'status': 'excluded',
              'owner': 'no rig board for it this time'},
             {'id': 'size', 'check': 'code size', 'kind': 'software', 'source': 'build contract code-size section',
              'status': 'inapplicable', 'reason': 'no firmware changed'}]}}
    r.update(fields)
    return r


def open_pr(head, repo='o/r'):
    return {'state': 'open', 'merged': False, 'head': {'sha': head}, 'base': {'repo': {'full_name': repo}}}


def validation(end, *items):
    body = json.dumps({'end': end, 'items': [{'id': i, 'result': res, 'ref': 'CI run 1'} for i, res in items]})
    return f'Validated.\n\n```text\n{audit.VALIDATION}\n{body}\n```\n'


class Hub:
    """GitHub, stubbed: pull requests, issue comments and permission bodies per repository (None: the read fails),
    and GITHUB's git side as bare repositories under root."""

    def __init__(self, case):
        self.root = tmpdir(case)
        self.pulls, self.comments, self.calls = {}, {}, []
        self.roles = {'alice': {'permission': 'write', 'role_name': 'maintain'},
                      'bob': {'permission': 'read', 'role_name': 'triage'}}

    def comment(self, slug, number, login, body, association='COLLABORATOR'):
        listed = self.comments.setdefault((slug, number), [])
        listed.append({'id': len(listed) + 1, 'user': {'login': login}, 'body': body, 'author_association': association,
                       'created_at': f'2026-11-0{len(listed) + 1}T00:00:00Z'})

    def gh(self, args):
        self.calls.append(args)
        path = args[-1]
        if m := re.fullmatch(r'repos/([^/]+/[^/]+)/pulls/(\d+)', path):
            pr = self.pulls.get((m[1], int(m[2])))
            return (0, json.dumps(pr), '') if pr else (1, '{"message":"Not Found"}', 'gh: Not Found (HTTP 404)')
        if m := re.fullmatch(r'repos/([^/]+/[^/]+)/issues/(\d+)/comments\?per_page=100', path):
            return 0, json.dumps([self.comments.get((m[1], int(m[2])), [])]), ''
        if m := re.fullmatch(r'repos/([^/]+/[^/]+)/collaborators/([^/]+)/permission', path):
            body = self.roles.get(m[2], {'permission': 'read', 'role_name': 'read'})
            if body == 403:         # what a caller without push access gets for anyone
                return 1, '{"status":"403"}', 'gh: Must have push access to view collaborator permission. (HTTP 403)'
            return (1, '', 'gh: Service Unavailable (HTTP 503)') if body is None else (0, json.dumps(body), '')
        raise AssertionError(f'unexpected gh {args}')

    def bare(self, slug):
        path = self.root / slug
        path = path.with_name(path.name + '.git')
        if not path.exists():
            subprocess.run(['git', 'init', '-q', '--bare', str(path)], check=True, env=git_env())
        return path


class Record(unittest.TestCase):
    """An audit of base..R that lands as o/r#7: finish writes the record, P commits it on R, E squashes P on main."""

    def setUp(self):
        self.hub = Hub(self)
        self.repo = Repo(self)
        self.base = self.repo.commit({'a.txt': '1\n'})

    def audit(self, moved=False, merge='squash', unchecked_coverage=False, **fields):
        r = self.repo
        r.git('checkout', '-q', '-b', 'audit')
        self.reviewed = r.commit({'a.txt': '2\n'})
        self.run_dir = tmpdir(self) / 'run'
        code, _, err = start(r.path, self.run_dir, '--base', self.base, head='audit')
        self.assertEqual(code, 0, err)
        file = tmpdir(self) / 'review.json'
        file.write_text(json.dumps(review(self.base, self.reviewed, **fields)))
        self.hub.pulls[('o/r', 7)] = open_pr(self.reviewed)
        with mock.patch.object(audit, 'uncovered', return_value=[]) if unchecked_coverage else \
                contextlib.nullcontext(), mock.patch.object(audit, 'gh', self.hub.gh):
            code, out, err = run(audit.main, 'finish', '--run', str(self.run_dir), '--head', self.reviewed,
                                 '--review', str(file))
        self.assertEqual(code, 0, err)
        r.git('add', '--', audit.RECORD)
        r.git('commit', '-q', '--only', '-m', 'Record the audit', '--', audit.RECORD)
        self.pr_head = r.git('rev-parse', 'HEAD')
        r.git('push', '-q', str(self.hub.bare('o/r')), f'{self.pr_head}:refs/pull/7/head')
        r.git('checkout', '-q', 'main')
        if moved:
            r.commit({'b.txt': 'later\n'})
        if merge == 'squash':
            r.git('merge', '-q', '--squash', 'audit')
            r.git('commit', '-q', '-m', 'Audit (#7)')
        else:
            r.git('merge', '-q', '--no-ff', '-m', 'Merge pull request #7', 'audit')
        self.end = r.git('rev-parse', 'HEAD')
        self.hub.pulls[('o/r', 7)] = {'merged': True, 'merge_commit_sha': self.end, 'head': {'sha': self.pr_head}}
        self.hub.comment('o/r', 7, 'alice', validation(self.end, ('suite', 'pass')))

    def check(self, repo=None):
        with mock.patch.multiple(audit, gh=self.hub.gh, GITHUB=self.hub.root.as_posix() + '/'):
            return run(audit.main, 'check', '--repo', str(repo or self.repo.path), '--head', 'main')

    def assertStatus(self, status, reason='', repo=None):
        code, out, err = self.check(repo)
        self.assertEqual((code, out.split('\n')[0].split(':')[0]), (0 if status == 'complete' else 1, status),
                         out + err)
        self.assertIn(reason, out)
        return out

    def record(self):
        return json.loads((self.repo.path / audit.RECORD).read_text())

    def test_none(self):
        self.assertStatus('invalid', f'no {audit.RECORD}')

    def test_a_squash_merged_record_is_complete_and_the_next_start_takes_its_end(self):
        self.audit(decisions=[{'id': 'd1', 'text': 'keep', 'status': 'new'},
                              {'id': 'd0', 'text': 'old', 'status': 'resolved'}])
        out = self.assertStatus('complete')
        self.assertIn(f'end {self.end}, o/r#7', out)
        later = self.repo.commit({'c.txt': 'c\n'})
        nxt = tmpdir(self) / 'next'
        with mock.patch.object(audit, 'gh', self.hub.gh):
            code, _, err = start(self.repo.path, nxt)
        self.assertEqual(code, 0, err)
        run_json = json.loads((nxt / 'run.json').read_text())
        self.assertEqual((run_json['base'], run_json['baseKind'], run_json['start_head']), (self.end, 'audit', later))
        carried = json.loads((nxt / 'carried.json').read_text())
        self.assertEqual(([d['id'] for d in carried['decisions']], [o['id'] for o in carried['open']]),
                         (['d1'], ['o1']))      # a resolved item stays in history, not carried
        self.assertEqual(carried['policy'], review(self.base, self.base)['policy'])
        file = tmpdir(self) / 'review.json'         # the carried ids must each be settled by the next finish
        file.write_text(json.dumps(review(self.end, later, decisions=[], open=[])))
        code, _, err = run(audit.main, 'finish', '--run', str(nxt), '--head', later, '--review', str(file))
        self.assertEqual(code, 2)
        self.assertIn('carried item d1 is missing', err)
        self.assertIn('carried item o1 is missing', err)

    def test_a_pr_head_absent_here_is_fetched_from_the_recorded_repository(self):
        self.audit()
        clone = tmpdir(self) / 'clone'
        subprocess.run(['git', 'clone', '-q', '--no-local', '--single-branch', '--branch', 'main',
                        str(self.repo.path), str(clone)], check=True, env=git_env())

        def has(sha):
            return subprocess.run(['git', '-C', str(clone), 'cat-file', '-e', sha], capture_output=True,
                                  env=git_env()).returncode == 0
        self.assertFalse(has(self.pr_head))
        self.assertStatus('complete', repo=clone)
        self.assertTrue(has(self.pr_head))
        self.hub.pulls[('o/r', 7)]['head']['sha'] = '1' * 40     # the API names a head refs/pull/7/head is not
        self.assertStatus('invalid', f'refs/pull/7/head of o/r did not bring the head the API names, {"1" * 40}',
                          repo=clone)
        subprocess.run(['git', '-C', str(self.hub.bare('o/r')), 'update-ref', '-d', 'refs/pull/7/head'], check=True,
                       env=git_env())
        self.assertStatus('invalid', 'o/r has no refs/pull/7/head', repo=clone)

    def test_a_pr_github_does_not_have_reads_invalid_and_start_offers_the_override(self):
        self.audit()
        del self.hub.pulls[('o/r', 7)]
        self.assertStatus('invalid', 'o/r#7 does not exist: gh api repos/o/r/pulls/7: gh: Not Found (HTTP 404)')
        with mock.patch.object(audit, 'gh', self.hub.gh):
            code, _, err = start(self.repo.path, tmpdir(self) / 'run')
        self.assertEqual(code, 2)
        self.assertIn(f'pass --base {self.base} --base-kind override', err)
        with mock.patch.object(audit, 'gh', return_value=(1, '', 'gh: Service Unavailable (HTTP 503)')):
            code, _, err = run(audit.main, 'check', '--repo', str(self.repo.path), '--head', 'main')
        self.assertEqual((code, 'HTTP 503' in err), (2, True))      # a failed read stays a refusal

    def test_the_recorded_repository_is_asked_never_origin(self):
        self.audit()
        self.repo.git('remote', 'add', 'origin', 'https://github.com/fork/r.git')
        self.hub.pulls[('fork/r', 7)] = self.hub.pulls[('o/r', 7)]
        self.hub.pulls[('o/r', 7)] = {'merged': True, 'merge_commit_sha': self.base, 'head': {'sha': self.base}}
        self.assertStatus('invalid', f'o/r#7 merged as {self.base}')
        self.assertFalse([c for c in self.hub.calls if 'fork/r' in c[-1]])

    def test_an_unmerged_pr(self):
        self.audit()
        self.hub.pulls[('o/r', 7)]['merged'] = False
        self.assertStatus('invalid', 'o/r#7 is not merged')

    def test_a_later_commit_touching_the_record(self):
        self.audit()
        rec = self.record()
        self.repo.commit({audit.RECORD: json.dumps(dict(rec, outcome='edited by hand'))})      # record only
        self.assertStatus('invalid', 'but the record last changed in')
        self.repo.git('reset', '-q', '--hard', self.end)
        self.repo.commit({audit.RECORD: json.dumps(rec, indent=1), 'a.txt': '3\n'})            # with other code
        self.assertStatus('invalid', 'but the record last changed in')

    def test_main_moved_between_review_and_merge(self):
        self.audit(moved=True)
        self.assertStatus('invalid', 'another tree than PR head')

    def test_an_up_to_date_ordinary_merge_reads_invalid(self):
        self.audit(merge='merge')
        self.assertEqual(self.repo.git('rev-parse', 'main^{tree}'), self.repo.git('rev-parse', 'audit^{tree}'))
        self.assertStatus('invalid', f'but the record last changed in {self.pr_head}, by an ordinary merge: the '
                                     'record verifies only after a squash or rebase merge')

    def test_a_record_whose_coverage_misses_a_changed_path(self):
        self.audit(unchecked_coverage=True, coverage=[{'batch': 'docs', 'paths': ['docs/**']}])
        self.assertStatus('invalid', f'coverage: 1 paths changed in {self.base[:12]}..{self.reviewed[:12]} match no '
                                     'batch glob: a.txt')

    def test_the_pr_head_must_be_the_record_alone_on_the_reviewed_head(self):
        self.audit()
        self.repo.git('checkout', '-q', 'audit')
        after = self.repo.commit({'a.txt': '9\n'})
        self.repo.git('reset', '-q', '--hard', self.reviewed)
        record = self.repo.git('show', f'{self.pr_head}:{audit.RECORD}') + '\n'
        wider = self.repo.commit({'a.txt': '9\n', audit.RECORD: record})
        self.repo.git('checkout', '-q', 'main')
        for head, reason in ((after, 'not the reviewed head'), (wider, f'changes more than {audit.RECORD}')):
            with self.subTest(reason):
                self.hub.pulls[('o/r', 7)]['head']['sha'] = head
                self.assertStatus('invalid', reason)

    def test_validation_reports(self):
        self.audit()
        rec, comments = self.record(), self.hub.comments[('o/r', 7)]
        self.hub.roles.update(carol=None, dave={'permission': 'write', 'role_name': 'release-engineer'},
                              erin={'role_name': 'release-engineer'})
        item = {'id': 'suite', 'result': 'pass', 'ref': 'CI run 1'}

        def block(report):
            return f'```\n{audit.VALIDATION}\n{json.dumps(report)}\n```'
        cases = [([], 'no validation report on o/r#7 by an account with write access'),
                 ([('alice', validation(self.end, ('suite', 'fail')))], 'suite is fail'),
                 ([('alice', validation(self.end, ('other', 'pass')))], 'expected item suite (BUILD.md suite) is'),
                 ([('alice', validation(self.base, ('suite', 'pass')))], f'names end {self.base}, not'),
                 ([('bob', validation(self.end, ('suite', 'pass')))], 'by an account with write access'),
                 ([('alice', validation(self.end, ('suite', 'pass'))),
                   ('alice', validation(self.end, ('suite', 'fail')))], 'suite is fail'),      # the newest one wins
                 ([('alice', '```\nrepo-audit-validation: 1\n{not json\n```')], 'report: not an object'),
                 ([('alice', validation(self.end, ('suite', 'pass'))),
                   ('carol', validation(self.end, ('suite', 'fail')))],
                  'validation report 2 by carol: permission unknown: gh api'),
                 ([('alice', validation(self.end, ('suite', 'pass'))),
                   ('dave', validation(self.end, ('suite', 'fail')))], 'suite is fail'),     # custom role, write base
                 ([('alice', validation(self.end, ('suite', 'pass'))),
                   ('erin', validation(self.end, ('suite', 'fail')))], 'by erin: permission unknown'),
                 ([('alice', block({'end': self.end, 'items': [item], 'by': 'x'}))], 'report: unknown field by'),
                 ([('alice', block({'end': self.end, 'items': [dict(item, result='skipped')]}))],
                  "report.items[0].result: 'skipped' is not one of fail, pass"),
                 ([('alice', block({'end': self.end, 'items': [item, item]}))], 'report.items: id suite used 2 times')]
        for ref in ({}, {'ref': None}, {'ref': ''}, {'ref': {'run': 1}}):
            bad = {k: v for k, v in item.items() if k != 'ref'} | ref
            cases.append(([('alice', block({'end': self.end, 'items': [bad]}))], 'report.items[0]'))
        with mock.patch.object(audit, 'gh', self.hub.gh):
            for posted, reason in cases:
                with self.subTest(reason):
                    comments.clear()
                    for login, body in posted:
                        self.hub.comment('o/r', 7, login, body)
                    problems = audit.validation_problems(rec, self.end)
                    self.assertTrue(problems)
                    self.assertIn(reason, '\n'.join(problems))
        comments.clear()
        self.hub.roles['owner'] = 403          # the caller has no push access: the endpoint answers 403 for anyone
        for association, problems in (('OWNER', []), ('MEMBER', ['by owner: permission unknown'])):
            with self.subTest(association), mock.patch.object(audit, 'gh', self.hub.gh):
                comments.clear()
                self.hub.comment('o/r', 7, 'owner', validation(self.end, ('suite', 'pass')), association)
                got = audit.validation_problems(rec, self.end)
                self.assertEqual(len(got), len(problems), got)
                for want, have in zip(problems, got):
                    self.assertIn(want, have)
        comments.clear()
        self.hub.comment('o/r', 7, 'alice', validation(self.end, ('suite', 'pass')))
        self.hub.comment('o/r', 7, 'bob', validation(self.end, ('suite', 'fail')))      # no write access: ignored
        self.assertStatus('complete')
        self.hub.comment('o/r', 7, 'alice', validation(self.end, ('suite', 'fail')))
        self.assertStatus('provisional', 'validation report by alice: suite is fail')

    def test_comments_changed_between_two_listings_leave_the_record_provisional(self):
        self.audit()
        listings = []

        def gh(args):
            answer = self.hub.gh(args)
            if args[-1].endswith('/comments?per_page=100'):
                listings.append(args)
                if len(listings) == 1:      # a comment deleted during the first read
                    self.hub.comments[('o/r', 7)].pop(0)
            return answer
        with mock.patch.object(audit, 'gh', gh):
            self.assertEqual(audit.validation_problems(self.record(), self.end),
                             ['the comments on o/r#7 changed while they were read: check again'])
        self.assertEqual(len(listings), 2)

    def test_a_listing_that_is_not_pages_is_refused(self):
        with mock.patch.object(audit, 'gh', return_value=(0, '{"message": "x"}', '')):
            with self.assertRaisesRegex(audit.Refused, 'not a list of pages'):
                audit.api('repos/o/r/issues/7/comments', paginate=True)

    def test_a_malformed_record_or_another_identity(self):
        self.audit()
        rec = self.record()
        old_style = dict(rec, branchPoint=self.base, open=[{'id': 'o1', 'kind': 'deferred', 'text': 't',
                                                             'status': 'new'}])
        for body, reason in ((dict(rec, extra=1), 'record: unknown field extra'),
                             (dict(rec, usage=dict(rec['usage'], hostNames=['ci'])), 'usage: unknown field hostNames'),
                             (dict(rec, stages={}), 'stages: missing coverage'),
                             (dict(rec, range=f'{self.base}..{self.reviewed}'), 'record: unknown field range'),
                             (old_style, 'record: unknown field branchPoint'),
                             (old_style, 'open[0]: unknown field kind'),
                             (dict(rec, validation={'expected': [dict(rec['validation']['expected'][0], ref='')]}),
                              "validation.expected[0].ref: not a non-empty string")):
            with self.subTest(reason):
                self.assertIn(reason, '\n'.join(audit.check_record(body)))
        with mock.patch.dict(os.environ, git_env(), clear=True):
            for body, reason in (('{', 'is not JSON'),
                                 (json.dumps(dict(rec, identity=['0' * 40])), 'identity differs')):
                with self.subTest(reason):
                    self.repo.git('reset', '-q', '--hard', self.end)
                    head = self.repo.commit({audit.RECORD: body})
                    self.assertIn(reason, '\n'.join(audit.provenance(self.repo.path, head)[0]))
        self.repo.git('reset', '-q', '--hard', self.end)
        self.repo.commit({audit.RECORD: json.dumps(dict(rec, extra=1))})
        self.assertStatus('invalid', 'record: unknown field extra')

    def test_a_shallow_clone_or_a_replaced_commit_is_refused(self):
        self.audit()
        shallow = tmpdir(self) / 'shallow'
        subprocess.run(['git', 'clone', '-q', '--depth', '1', self.repo.path.as_uri(), str(shallow)], check=True,
                       env=git_env())
        code, _, err = self.check(shallow)
        self.assertEqual((code, 'shallow' in err), (2, True))
        self.repo.git('replace', self.end, self.base)
        code, _, err = self.check()
        self.assertEqual((code, 'refs/replace' in err), (2, True))
        with mock.patch.object(audit, 'gh', self.hub.gh):
            code, _, err = start(self.repo.path, tmpdir(self) / 'run', '--base', self.base, '--base-kind', 'override')
        self.assertEqual((code, 'refs/replace' in err), (2, True))

    def test_start_on_an_invalid_record_offers_its_base_as_an_override(self):
        self.audit()
        self.hub.pulls[('o/r', 7)]['merged'] = False
        with mock.patch.object(audit, 'gh', self.hub.gh):
            code, _, err = start(self.repo.path, tmpdir(self) / 'run')
        self.assertEqual(code, 2)
        self.assertIn('record is invalid', err)
        self.assertIn(f'pass --base {self.base} --base-kind override', err)
        self.repo.commit({audit.RECORD: json.dumps(dict(self.record(), base='1' * 40))})     # not an ancestor
        with mock.patch.object(audit, 'gh', self.hub.gh):
            code, _, err = start(self.repo.path, tmpdir(self) / 'run')
        self.assertEqual(code, 2)
        self.assertNotIn('--base-kind override', err)

    def report(self, items, head=None, tested=None):
        file = tmpdir(self) / 'results.json'
        file.write_text(json.dumps({'end': tested or head or self.end, 'items': items}))
        with mock.patch.multiple(audit, gh=self.hub.gh, GITHUB=self.hub.root.as_posix() + '/'):
            return run(audit.main, 'report', '--repo', str(self.repo.path), '--head', head or self.end, '--results',
                       str(file))

    def test_report_prints_the_comment_check_then_accepts(self):
        self.audit()
        self.hub.comments[('o/r', 7)].clear()
        items = [{'id': 'suite', 'result': 'pass', 'ref': 'CI run 9 on E'}]
        code, out, err = self.report(items)
        self.assertEqual(code, 0, err)
        self.assertEqual(audit.report_blocks(out), [{'end': self.end, 'items': items}])
        self.hub.comment('o/r', 7, 'alice', out)
        self.assertStatus('complete')

    def test_report_refuses(self):
        self.audit()
        item = {'id': 'suite', 'result': 'pass', 'ref': 'CI run 9'}
        for items, reason in (([dict(item, id='other')], "missing ['suite'], not expected ['other']"),
                              ([item, dict(item, id='other')], "missing [], not expected ['other']"),
                              ([dict(item, ref='/home/me/log')], 'looks private'),
                              ([dict(item, result='ok')], "report.items[0].result: 'ok' is not one of")):
            with self.subTest(reason):
                code, _, err = self.report(items)
                self.assertEqual(code, 2, err)
                self.assertIn(reason, err)
        for tested in (self.reviewed, self.pr_head):       # results taken before the merge are not E's
            with self.subTest(tested=tested):
                code, _, err = self.report([item], tested=tested)
                self.assertEqual(code, 2, err)
                self.assertIn(f'report.end: names {tested}, not --head {self.end}', err)
        later = self.repo.commit({'c.txt': 'c\n'})
        code, _, err = self.report([item], head=later)
        self.assertEqual(code, 2)
        self.assertIn(f'is not the merge commit that last changed the record, {self.end}', err)
        self.hub.pulls[('o/r', 7)]['merged'] = False
        code, _, err = self.report([item])
        self.assertEqual(code, 2)
        self.assertIn('o/r#7 is not merged', err)


class Finish(unittest.TestCase):
    def setUp(self):
        self.repo = Repo(self)
        self.root = self.repo.commit({'a.txt': '0\n'})
        self.base = self.repo.commit({'a.txt': '1\n'})
        self.repo.git('checkout', '-q', '-b', 'audit')
        self.head = self.repo.commit({'a.txt': '2\n'})
        self.out = tmpdir(self) / 'run'
        start(self.repo.path, self.out, '--base', self.base, head=self.head)
        self.hub = Hub(self)
        patch = mock.patch.object(audit, 'gh', self.hub.gh)
        patch.start()
        self.addCleanup(patch.stop)

    def finish(self, review_json, head=None, *extra):
        """finish on head; the review's PR is open on o/r with that head unless the test set it."""
        file = tmpdir(self) / 'review.json'
        file.write_text(json.dumps(review_json))
        head = head or self.head
        self.hub.pulls.setdefault(('o/r', 7), open_pr(self.repo.git('rev-parse', head)))
        return run(audit.main, 'finish', '--run', str(self.out), '--head', head, '--review', str(file), *extra)

    def refused(self, reason, head=None, **fields):
        code, _, err = self.finish(review(self.base, self.head, **fields), head)
        self.assertEqual(code, 2, err)
        self.assertIn(reason, err)
        self.assertFalse((self.repo.path / audit.RECORD).exists())
        return err

    def test_finish_writes_the_record_from_the_run_and_the_review(self):
        code, out, err = self.finish(review(self.base, self.head))
        self.assertEqual(code, 0, err)
        self.assertIn(f'git add -- {audit.RECORD} && git commit --only -- {audit.RECORD}', out)
        rec = json.loads((self.repo.path / audit.RECORD).read_text())
        self.assertEqual({k: rec[k] for k in ('schema', 'identity', 'base', 'baseKind', 'reviewedHead')},
                         {'schema': 1, 'identity': [self.root], 'base': self.base, 'baseKind': 'bootstrap',
                          'reviewedHead': self.head})
        self.assertEqual({k: v for k, v in rec.items() if k not in audit.RECORD_FIELDS},
                         {k: v for k, v in review(self.base, self.head).items() if k != 'range'})
        self.assertEqual(sorted(p.name for p in self.out.iterdir()), ['carried.json', 'inventory.json', 'run.json'])
        self.assertEqual(self.finish(review(self.base, self.head))[0], 0)       # a rewrite after a review round
        self.assertEqual([p.name for p in (self.repo.path / '.claude').iterdir()], ['audit.json'])
        self.assertNotIn(b'\r\n', (self.repo.path / audit.RECORD).read_bytes())

    def test_the_pr_must_be_open_on_its_repository_with_head_r(self):
        for pr, reason in ((None, 'pr: o/r#7 cannot be read: gh api repos/o/r/pulls/7: gh: Not Found (HTTP 404)'),
                           (dict(open_pr(self.head), state='closed'), "pr: o/r#7 is 'closed', not open"),
                           (open_pr(self.base), f'pr: o/r#7 has head {self.base}, not --head {self.head}: push R'),
                           (open_pr(self.head, 'fork/r'), 'pr: o/r#7 targets fork/r, not o/r')):
            with self.subTest(reason):
                self.hub.pulls[('o/r', 7)] = pr
                self.refused(reason)
        self.hub.pulls[('o/r', 7)] = open_pr(self.head, 'O/R')       # GitHub's own case of the name
        self.assertEqual(self.finish(review(self.base, self.head))[0], 0)

    def test_a_record_that_landed_at_r_since_start_is_not_overwritten(self):
        other = json.dumps({'decisions': 'another audit merged this'}) + '\n'
        head = self.repo.commit({audit.RECORD: other})
        code, _, err = self.finish(review(self.base, head), head)
        self.assertEqual(code, 2, err)
        self.assertIn(f'{audit.RECORD} at --head {head[:12]} is not the one start read at {self.head[:12]}', err)
        self.assertEqual((self.repo.path / audit.RECORD).read_text(), other)

    def test_a_record_path_through_a_symbolic_link_is_refused(self):
        victim = tmpdir(self) / 'victim.txt'
        victim.write_text('ORIGINAL', newline='\n')
        (self.repo.path / '.claude').mkdir()
        (self.repo.path / audit.RECORD).symlink_to(victim)          # untracked: the dirty check passes it
        code, _, err = self.finish(review(self.base, self.head))
        self.assertEqual(code, 2, err)
        self.assertIn(f'{audit.RECORD} is a symbolic link', err)
        self.assertEqual(victim.read_text(), 'ORIGINAL')
        (self.repo.path / audit.RECORD).unlink()
        (self.repo.path / '.claude').rmdir()
        elsewhere = tmpdir(self)
        (self.repo.path / '.claude').symlink_to(elsewhere, target_is_directory=True)
        head = self.repo.commit({})                 # a tracked link: the checkout is clean
        self.out = tmpdir(self) / 'run2'
        self.assertEqual(start(self.repo.path, self.out, '--base', self.base, head=head)[0], 0)
        code, _, err = self.finish(review(self.base, head), head)
        self.assertEqual(code, 2, err)
        self.assertIn('.claude is a symbolic link', err)
        self.assertEqual(list(elsewhere.iterdir()), [])

    def test_an_ignored_record_path_is_refused(self):
        (self.repo.path / '.git' / 'info').mkdir(exist_ok=True)
        (self.repo.path / '.git' / 'info' / 'exclude').write_text('.claude/\n')
        self.refused(f'{audit.RECORD} is ignored by git')

    def test_run_files_cut_short_or_edited_are_refused(self):
        data = json.loads((self.out / 'run.json').read_text())
        carried = (self.out / 'carried.json').read_text()
        for name, body, reason in (('run.json', '{', 'run.json is not JSON'),
                                   ('run.json', json.dumps(dict(data, format=1)), 'a run of an older audit.py'),
                                   ('run.json', json.dumps({k: v for k, v in data.items() if k != 'repo'}),
                                    'run.json lacks repo: start again'),
                                   ('carried.json', None, 'has no carried.json: start did not complete'),
                                   ('carried.json', '[]', 'carried.json lacks an object'),
                                   ('carried.json', json.dumps({'record': None, 'decisions': {}, 'open': []}),
                                    'decisions and open are not lists of items')):
            with self.subTest(reason):
                (self.out / 'run.json').write_text(json.dumps(data))
                (self.out / 'carried.json').write_text(carried)
                if body is None:
                    (self.out / name).unlink()
                else:
                    (self.out / name).write_text(body)
                self.assertNotIn('Traceback', self.refused(reason))

    def test_finish_runs_in_another_checkout_of_r_with_repo(self):
        other = tmpdir(self) / 'wt'
        self.repo.git('worktree', 'add', '-q', '--detach', str(other), self.head)
        code, _, err = self.finish(review(self.base, self.head), None, '--repo', str(other))
        self.assertEqual(code, 0, err)
        self.assertTrue((other / audit.RECORD).is_file())
        self.assertFalse((self.repo.path / audit.RECORD).exists())
        data = json.loads((self.out / 'run.json').read_text())
        (self.out / 'run.json').write_text(json.dumps(dict(data, repo=str(tmpdir(self) / 'gone'))))
        self.refused('the checkout start ran in, is gone: pass --repo')

    def test_stages_and_the_range(self):
        stages = review(self.base, self.head)['stages']
        self.refused('stages: missing coreview', stages={k: v for k, v in stages.items() if k != 'coreview'})
        self.refused('stages.claims: missing reason', stages=dict(stages, claims={'status': 'inapplicable'}))
        self.refused('stages.claims: unknown field range',
                     stages=dict(stages, claims=dict(stages['claims'], range=f'{self.base}..{self.head}')))
        self.refused(f"range: '{self.base}..{self.base}' is not this run's range {self.base}..{self.head}",
                     range=f'{self.base}..{self.base}')

    def test_the_reviewed_head_must_be_the_clean_checked_out_descendant(self):
        self.repo.git('checkout', '-q', '-b', 'side', self.root)
        side = self.repo.commit({'s.txt': 's\n'})
        err = self.refused('does not descend from the run\'s base', head=side)
        self.assertIn("does not descend from the run's start_head", err)
        self.repo.git('checkout', '-q', 'audit')
        self.refused(f'HEAD of {self.repo.path} is not --head {self.base[:12]}', head=self.base)
        (self.repo.path / 'stray.txt').write_text('x')
        self.refused('1 uncommitted paths (first: stray.txt)')

    def test_another_repository_is_refused(self):
        other = Repo(self)
        other.commit({'x': 'y'})
        data = json.loads((self.out / 'run.json').read_text())
        (self.out / 'run.json').write_text(json.dumps(dict(data, repo=str(other.path))))
        code, _, err = self.finish(review(self.base, self.head), head='HEAD')
        self.assertEqual(code, 2)
        self.assertIn('not the repository', err)

    def test_carried_items_are_settled(self):
        (self.out / 'carried.json').write_text(json.dumps({'record': None,
            'decisions': [{'id': 'd1', 'text': 'keep', 'status': 'new'}],
            'open': [{'id': 'o9', 'text': 'x', 'status': 'carried'}], 'policy': []}))
        self.refused('carried item o9 is missing')
        self.refused("decisions[0].by: 'zz' is no other item's id",
                     decisions=[{'id': 'd1', 'text': 'keep', 'status': 'superseded', 'by': 'zz'}],
                     open=[{'id': 'o9', 'text': 'x', 'status': 'resolved'}])
        self.refused('carried item d1 must be carried, resolved or superseded',
                     open=[{'id': 'o9', 'text': 'x', 'status': 'resolved'}])
        code, _, err = self.finish(review(self.base, self.head, decisions=[
            {'id': 'd1', 'text': 'keep', 'status': 'superseded', 'by': 'd2'},
            {'id': 'd2', 'text': 'keep, simpler', 'status': 'new'}],
            open=[{'id': 'o9', 'text': 'x', 'status': 'carried'}]))
        self.assertEqual(code, 0, err)

    def test_expected_validation(self):
        expected = review(self.base, self.head)['validation']['expected']
        software, hardware = expected[0], expected[1]
        cases = [('validation.expected: empty', []),
                 ('validation.expected: no software item', [hardware]),
                 ('validation.expected: no hardware item', [software]),
                 ('validation.expected[0]: missing source', [{k: v for k, v in software.items() if k != 'source'},
                                                             hardware]),
                 ("validation.expected[0].kind: 'firmware' is not one of", [dict(software, kind='firmware'), hardware]),
                 ('validation.expected[0]: missing ref', [{k: v for k, v in software.items() if k != 'ref'}, hardware]),
                 ('validation.expected[1]: missing owner', [software, {k: v for k, v in hardware.items()
                                                                        if k != 'owner'}]),
                 ('hil is required-pending', [software, dict({k: v for k, v in hardware.items() if k != 'owner'},
                                                             status='required-pending')]),
                 ("validation.expected[1].status: 'deferred' is not one of",       # deferring never covers it
                  [software, dict({k: v for k, v in hardware.items() if k != 'owner'}, status='deferred')])]
        for reason, items in cases:
            with self.subTest(reason):
                self.refused(reason, validation={'expected': items})
        agentrc = dict({k: v for k, v in hardware.items() if k != 'owner'}, status='inapplicable',
                       reason='no firmware in agentrc')
        code, _, err = self.finish(review(self.base, self.head, validation={'expected': [software, agentrc]}))
        self.assertEqual(code, 0, err)

    def test_private_looking_strings_and_host_names(self):
        err = self.refused('looks private', outcome='see /home/me/x.jsonl', policy=[
            {'question': 'session_01AbCdEfGhJkMnPq', 'answer': 'toolu_01x', 'date': '2026-11-08'}],
            claims=[{'claim': 'https://chatgpt.com/c/abc', 'document': 'd', 'revision': '1', 'section': '1',
                     'variants': ['x'], 'status': 'pending'}],
            coverage=[{'batch': '123e4567-e89b-12d3-a456-426614174000', 'paths': ['**']}])
        for where in ('outcome', 'policy[0].question', 'policy[0].answer', 'claims[0].claim', 'coverage[0].batch'):
            self.assertIn(f'{where}: looks private', err)
        shell = ['cd x && python3 "$RUN/a.py"', 'n=$(ls)', 'make 2>&1', 'x >/dev/null', 'run sudo make',
                 '$ make all', 'log | grep x']
        self.assertEqual([bool(audit.private(c)) for c in shell], [True] * len(shell))
        usb = ['OTGSC_B_SESSION_VALID stays', 'session_id', 'USBC_INTUSB_SESSION_REQ', 'tud_session_end()',
               'https://app.circleci.com/pipelines/github/o/r/1/workflows/123e4567-e89b-12d3-a456-426614174000/jobs/2',
               'a || b && c', 'rate 5 MB/s', 'software|hardware']
        self.assertEqual(audit.private(usb), [])
        usage_ = review(self.base, self.head)['usage']
        self.refused("usage.hosts: not a host count: ['htpc', 'ci']", usage=dict(usage_, hosts=['htpc', 'ci']))
        self.refused("usage.totals: 'ci.lan': a total", usage=dict(usage_, totals={'ci.lan': 3}))
        self.refused('usage: missing window', usage={'totals': {'sessions': 1}, 'hosts': 1})

    def test_usage_with_no_transcripts_is_recorded_unmeasured(self):
        usage_ = review(self.base, self.head)['usage']
        self.refused('usage.totals: totals from no host', usage=dict(usage_, hosts=0))
        self.refused('usage.totals: empty: give the totals', usage=dict(usage_, totals={}))
        self.refused('usage.hosts: not a host count: -1', usage=dict(usage_, hosts=-1, totals={}))
        code, _, err = self.finish(review(self.base, self.head, usage=dict(usage_, hosts=0, totals={})))
        self.assertEqual(code, 0, err)

    def test_malformed_nested_values_are_listed_not_raised(self):
        (self.out / 'carried.json').write_text(json.dumps({'record': None, 'decisions': [], 'open': [{'id': 'o1'}],
                                                           'policy': []}))
        usage_ = review(self.base, self.head)['usage']
        expected = review(self.base, self.head)['validation']['expected']
        err = self.refused('decisions[0].status: {', decisions=[{'id': 'd1', 'text': 't', 'status': {'x': 1}}],
                           open=[{'id': 'o1', 'text': 't', 'status': ['carried']}],
                           claims=[dict(review(self.base, self.head)['claims'][0], status={'s': 1})],
                           usage=dict(usage_, window={'until': 5}),
                           validation={'expected': [dict(expected[0], status=['covered']), *expected[1:]]})
        for reason in ('open[0].status: [', 'carried item o1 must be', 'claims[0].status: {',
                       'usage.window: missing since', "validation.expected[0].status: ['covered']"):
            self.assertIn(reason, err)
        self.assertNotIn('Traceback', err)
        self.refused("decisions[0].by: ['d2'] is no other", decisions=[
            {'id': 'd1', 'text': 't', 'status': 'superseded', 'by': ['d2']},
            {'id': 'd2', 'text': 't', 'status': 'new'}])

    def test_the_coverage_ledger_must_match_every_changed_path(self):
        r = self.repo
        (r.path / 'lib' / 'dep').mkdir(parents=True)      # an unpopulated submodule, not a deletion
        r.commit({'gone.txt': 'g\n', 'old/name.txt': 'same\n' * 20, 'src/deep/a.c': 'a\n'})
        r.git('update-index', '--add', '--cacheinfo', f'160000,{self.base},lib/dep')
        r.git('commit', '-q', '-m', 'pin')
        base = r.git('rev-parse', 'HEAD')
        for name, body in (('gone.txt', None), ('old/name.txt', None), ('new/name.txt', 'same\n' * 20),
                           ('src/deep/a.c', 'b\n')):
            if body is None:
                (r.path / name).unlink()
            else:
                (r.path / name).parent.mkdir(parents=True, exist_ok=True)
                (r.path / name).write_text(body, newline='\n')
        r.git('add', '-A')
        r.git('update-index', '--cacheinfo', f'160000,{self.head},lib/dep')
        r.git('commit', '-q', '-m', 'move, delete, bump')
        end = r.git('rev-parse', 'HEAD')
        self.assertIn('R100\x00old/name.txt\x00new/name.txt', r.git('diff', '--name-status', '-z', '-M', base, end))
        self.out = tmpdir(self) / 'run2'
        start(r.path, self.out, '--base', base, head=end)
        for globs, missed in ((['docs/**'], 'gone.txt, lib/dep, new/name.txt, old/name.txt, src/deep/a.c'),
                              (['*', 'lib/*', 'new/*', 'old/*', 'src/*'], 'src/deep/a.c'),
                              (['src/**', 'lib/dep', '*/name.txt'], 'gone.txt')):
            with self.subTest(globs):
                code, _, err = self.finish(review(base, end, coverage=[{'batch': 'b', 'paths': globs}]), head=end)
                self.assertEqual(code, 2, err)
                n = len(missed.split(', '))
                self.assertIn(f'coverage: {n} paths changed in {base[:12]}..{end[:12]} match no batch glob: {missed}',
                              err)
        code, _, err = self.finish(review(base, end, coverage=[{'batch': 'b', 'paths': ['**']}]), head=end)
        self.assertEqual(code, 0, err)


def claude(path, records):
    path.parent.mkdir(parents=True, exist_ok=True)
    path.write_text(''.join(json.dumps(r) + '\n' for r in records))


def use(call_id, name, inp, ts='2026-01-10T00:00:00Z', cwd=None, session='s1', kind='assistant'):
    return {'type': kind, 'timestamp': ts, 'cwd': cwd, 'sessionId': session,
            'message': {'content': [{'type': 'tool_use', 'id': call_id, 'name': name, 'input': inp}]}}


def result(call_id, error=False, ts='2026-01-10T00:00:01Z'):
    return {'type': 'user', 'timestamp': ts,
            'message': {'content': [{'type': 'tool_result', 'tool_use_id': call_id, 'is_error': error}]}}


class Usage(unittest.TestCase):
    def setUp(self):
        self.repo = Repo(self)
        head = self.repo.commit({'skills/tool/scripts/tool.py': TOOL, 'skills/tool/SKILL.md': 'x\n',
                                 'agents/helper.md': 'x\n', 'workflows/flow.js': 'x\n'})
        self.run_dir = tmpdir(self) / 'run'
        start(self.repo.path, self.run_dir, '--base', head, head=head)
        self.root = tmpdir(self)
        # a command is parsed as bash would: a Windows path in it takes forward slashes
        self.script = PurePosixPath(self.repo.path.as_posix(), 'skills/tool/scripts/tool.py')
        self.out = tmpdir(self) / 'usage.json'

    def usage(self, *extra):
        return run(usage.main, '--run', str(self.run_dir), '--claude-root', str(self.root), '--out', str(self.out),
                   *extra)

    def test_invocations_are_parsed_not_grepped(self):
        s = self.script
        claude(self.root / 'p' / 's1.jsonl', [
            use('a', 'Bash', {'command': f'python3 -X utf8 {s} send --lane l1 SECRETVALUE --bogus 1'}),
            result('a'),
            {**use('a', 'Bash', {'command': f'python3 {s} send'}), 'type': 'api-request-blob'},   # a repeat
            use('b', 'Bash', {'command': f'S={s}; sudo -E timeout 5 python3 "$S" send && echo "python3 {s} send"'}),
            result('b', error=True),
            use('c', 'Bash', {'command': f'cat <<EOF\npython3 {s} send\nEOF\nsed -n 1p {s}'}),
            use('d', 'Bash', {'command': f'python3 {s} send', 'description': 'x'}, ts='2025-12-31T23:59:59Z'),
            use('e', 'Bash', {'command': f'cd {s.parent} && ./tool.py'}),
            use('f', 'Bash', {'command': f"S={s}; python3 '$S' send"}),          # single quotes: no expansion
            use('g', 'Bash', {'command': f'env S={s} python3 "$S" send'}),       # $S expands before env sets it
            use('h', 'Bash', {'command': f'python3 {s} send; true'}), result('h'),
            use('i', 'Bash', {'command': f'ssh host "python3 {s} send"'}),
            use('j', 'Bash', {'command': f'python3 "{s}"suffix send'}),          # bash runs tool.pysuffix
            use('k', 'Bash', {'command': f'true || python3 {s} send'}),          # may never run
            use('l', 'Bash', {'command': f'X=$(pwd)/{s.name}; python3 "$X" send 2>&1 >/dev/null'}),
            use('m', 'Bash', {'command': f'false && python3 {s} send'}), result('m', error=True),
            use('n', 'Bash', {'command': f'python3 {s} send &'}), result('n'),
            use('o', 'Bash', {'command': f"R={self.repo.path.as_posix()}; "
                                         "python3 '$R'\"/skills/tool/scripts/tool.py\" send"}),
            use('p', 'Bash', {'command': f'true || R={s}; python3 "$R" send'}),    # the skipped assignment
            use('q', 'Bash', {'command': f'true || (python3 {s} send)'}),
            use('r', 'Bash', {'command': f'if true; then python3 {s} send; fi'}),
            use('s', 'Bash', {'command': f'exit 0; python3 {s} send'}),
            use('t', 'Bash', {'command': f'R={s} & python3 "$R" send'}),           # R is set in a subshell
            use('u', 'Bash', {'command': f'echo "$(python3 {s} send)"'}),
            use('v', 'Bash', {'command': f'R={s} && true & python3 "$R" send'}),
            use('w', 'Bash', {'command': f'cd {s.parent} & cd / ; ./tool.py send'}),
        ])
        code, out, _ = self.usage()
        self.assertEqual(code, 0)
        agg = json.loads(self.out.read_text())
        t = agg['scripts']['skills/tool/scripts/tool.py']
        # a: alone, ok; b: && follows, its failure proves nothing; h: ; true follows; n: backgrounded
        self.assertEqual((t['invoke'], t['ok'], t['error'], t['result_unknown']), (4, 1, 0, 3))
        self.assertEqual((t['subcommands'], t['flags'], t['unrecognised_args']), ({'send': 4}, {'--lane': 1}, 1))
        self.assertEqual(t['reads'], 1)
        # conditional: e (cd && ./tool.py, no result), k, p, q (after ||), m (after &&, the call failed), r (if)
        self.assertEqual(agg['unknown'], {'unresolved_variable': 5, 'conditional_invocation': 7,
                                          'command_substitution': 1, 'unknown_cwd': 1})
        self.assertEqual(agg['unattributed'], {'skills/tool/scripts/tool.py': 1})        # the ssh one
        self.assertNotIn('SECRETVALUE', out + self.out.read_text())
        self.assertNotIn('bogus', out)

    def test_exec_runs_its_command_and_nothing_after_it(self):
        s = self.script
        claude(self.root / 'p' / 's1.jsonl', [
            use('a', 'Bash', {'command': f'exec python3 {s} send'}), result('a'),
            use('b', 'Bash', {'command': f'exec python3 {s} send; python3 {s} send'}),
            use('c', 'Bash', {'command': f'exec 2>/dev/null; python3 {s} send'}),
        ])
        self.usage()
        agg = json.loads(self.out.read_text())
        t = agg['scripts']['skills/tool/scripts/tool.py']
        self.assertEqual((t['invoke'], t['ok'], t['result_unknown']), (2, 1, 1))
        self.assertEqual(agg['unknown'], {'conditional_invocation': 2})

    def test_a_prefix_option_s_operand_is_not_the_command(self):
        s = self.script
        claude(self.root / 'p' / 's1.jsonl', [
            use('a', 'Bash', {'command': f"exec -a {s} python3 -c 'print(1)'"}), result('a'),
            use('b', 'Bash', {'command': f'exec -a foo python3 {s} send'}), result('b'),
            use('c', 'Bash', {'command': f'sudo -u root python3 {s} send'}), result('c'),
            use('d', 'Bash', {'command': f'sudo -Eu root python3 {s} send'}), result('d'),
            use('e', 'Bash', {'command': f'env -u X python3 {s} send'}), result('e'),
            use('f', 'Bash', {'command': f'timeout -s KILL 5 python3 {s} send'}), result('f'),
            use('g', 'Bash', {'command': f'stdbuf -o L nice -n 5 python3 {s} send'}), result('g'),
            use('h', 'Bash', {'command': f'sudo --user root timeout --signal=KILL 15s {s} send'}), result('h'),
        ])
        self.usage()
        agg = json.loads(self.out.read_text())
        t = agg['scripts']['skills/tool/scripts/tool.py']
        self.assertEqual((t['invoke'], t['ok'], t['subcommands']), (7, 7, {'send': 7}))
        self.assertEqual(agg['unknown'], {})

    def test_a_prefix_option_that_changes_what_runs_is_not_followed(self):
        s, rel, repo = self.script, 'skills/tool/scripts/tool.py', self.repo.path.as_posix()
        split = "'python3 -c print(1)'"
        commands = [f'env -S {split} {s} send', f'env -iS {split} {s} send', f'env --split-string {split} {s} send',
                    f'env --split-string={split} {s} send', f'env -C / python3 {rel} send',
                    f'env -C/ python3 {rel} send', f'env --chdir / python3 {rel} send',
                    f'env --chdir=/ python3 {rel} send', f'sudo -D / python3 {rel} send',
                    f'sudo -ED/ python3 {rel} send', f'sudo --chdir=/ python3 {rel} send',
                    f'sudo -R /mnt python3 {s} send', f'sudo --chroot /mnt python3 {s} send',
                    f'sudo -h host python3 {s} send', f'sudo --host=host python3 {s} send']
        records = [r for i, c in enumerate(commands) for r in (use(f'x{i}', 'Bash', {'command': c}, cwd=repo),
                                                               result(f'x{i}'))]
        claude(self.root / 'p' / 's1.jsonl', records + [
            use('a', 'Bash', {'command': f'sudo -Eu root env -u X python3 {rel} send'}, cwd=repo), result('a'),
        ])
        self.usage()
        agg = json.loads(self.out.read_text())
        t = agg['scripts']['skills/tool/scripts/tool.py']
        self.assertEqual((t['invoke'], t['ok'], t['subcommands']), (1, 1, {'send': 1}))
        self.assertEqual(agg['unknown'], {'unparsed_prefix': len(commands)})

    def test_installed_links_attribute_and_foreign_paths_do_not(self):
        installed = tmpdir(self) / 'claude' / 'skills'
        installed.mkdir(parents=True)
        (installed / 'tool').symlink_to(self.repo.path / 'skills' / 'tool')
        elsewhere = tmpdir(self) / 'other' / 'scripts' / 'tool.py'
        elsewhere.parent.mkdir(parents=True)
        elsewhere.write_text(TOOL)
        claude(self.root / 'p' / 's1.jsonl', [
            use('a', 'Bash', {'command': f'python3 {installed.as_posix()}/tool/scripts/tool.py send'}),
            use('b', 'Bash', {'command': f'python3 {elsewhere.as_posix()} send'}),
            use('c', 'Bash', {'command': 'python3 /gone/worktree/skills/tool/scripts/tool.py send'}),
            use('d', 'Bash', {'command': 'python3 "$UNSET/tool.py" send'}),
        ])
        self.usage()
        agg = json.loads(self.out.read_text())
        self.assertEqual(agg['scripts']['skills/tool/scripts/tool.py']['invoke'], 1)
        self.assertEqual(agg['unattributed'], {'skills/tool/scripts/tool.py': 1})
        self.assertEqual(agg['unknown'], {'unresolved_variable': 1})

    def test_a_worktree_nested_in_the_main_checkout_attributes(self):
        nested = self.repo.path / '.worktrees' / 'b'
        self.repo.git('worktree', 'add', '-q', str(nested), '-b', 'b')
        claude(self.root / 'p' / 's1.jsonl', [
            use('a', 'Bash', {'command': f'python3 {nested.as_posix()}/skills/tool/scripts/tool.py send'})])
        self.usage()
        agg = json.loads(self.out.read_text())
        self.assertEqual(agg['scripts']['skills/tool/scripts/tool.py']['invoke'], 1)
        self.assertEqual(agg['unattributed'], {})

    def test_callers_names_and_doc_reads(self):
        claude(self.root / 'p' / 's1' / 'subagents' / 'workflows' / 'wf_1' / 'agent-1.jsonl', [
            use('a', 'Bash', {'command': f'python3 {self.script} send'})])
        claude(self.root / 'p' / 's2.jsonl', [
            {**use('h', 'Bash', {'command': f'python3 {self.script} send'}), 'entrypoint': 'sdk-cli'}])
        claude(self.root / 'p' / 's1' / 'subagents' / 'agent-2.jsonl', [
            use('b', 'Skill', {'skill': 'tool'}), use('c', 'Skill', {'skill': 'private-thing'}),
            use('d', 'Agent', {'subagent_type': 'helper'}), use('e', 'Agent', {'subagent_type': 'mine'}),
            use('f', 'Workflow', {'scriptPath': '/x/flow-wf_ab12.js'}),
            use('g', 'Read', {'file_path': str(self.repo.path / 'skills/tool/SKILL.md')})])
        _, out, _ = self.usage()
        agg = json.loads(self.out.read_text())
        self.assertEqual(agg['scripts']['skills/tool/scripts/tool.py']['caller'], {'workflow': 1, 'headless': 1})
        self.assertEqual(agg['skills'], {'tool': 1, 'other': 1})
        self.assertEqual(agg['agents'], {'helper': 1, 'other': 1})
        self.assertEqual(agg['workflows'], {'flow': 1})
        self.assertEqual(agg['docs_read'], {'skills/tool/SKILL.md': 1})
        self.assertNotIn('private-thing', out)

    def test_the_claude_layout_is_catalogued_once_through_its_agents_alias(self):
        self.repo.commit({'.claude/skills/build/SKILL.md': 'x\n', '.claude/skills/tool/SKILL.md': 'x\n',
                          '.claude/agents/runner.md': 'x\n', '.claude/workflows/ship.js': 'x\n'})
        (self.repo.path / '.agents').symlink_to('.claude')
        self.repo.commit({})
        head = self.repo.git('rev-parse', 'HEAD')
        self.assertEqual(self.repo.git('ls-tree', '--name-only', head, '.agents'), '.agents')     # one link entry
        self.run_dir = tmpdir(self) / 'run2'
        start(self.repo.path, self.run_dir, '--base', head, head=head)
        alias = self.repo.path / '.agents' / 'skills' / 'build' / 'SKILL.md'
        claude(self.root / 'p' / 's1.jsonl', [
            use('a', 'Skill', {'skill': 'build'}), use('b', 'Skill', {'skill': 'tool'}),
            use('c', 'Agent', {'subagent_type': 'runner'}), use('d', 'Workflow', {'scriptPath': '/x/ship-wf_1.js'}),
            use('e', 'Read', {'file_path': str(alias)})])
        self.usage()
        agg = json.loads(self.out.read_text())
        self.assertEqual((agg['skills'], agg['agents'], agg['workflows']),
                         ({'build': 1, 'tool': 1}, {'runner': 1}, {'ship': 1}))
        self.assertEqual(agg['docs_read'], {'.claude/skills/build/SKILL.md': 1})

    def test_codex_exec_and_function_calls(self):
        root = tmpdir(self)
        rec = lambda payload: {'type': 'response_item', 'timestamp': '2026-01-10T00:00:00Z', 'payload': payload}  # noqa: E731
        claude(root / '2026' / 'r.jsonl', [
            {'type': 'session_meta', 'payload': {'cwd': str(self.repo.path)}},
            rec({'type': 'custom_tool_call', 'name': 'exec', 'call_id': 'x',
                 'input': f'text(await tools.exec_command({{cmd:"python3 {self.script} send",max:1}}));'}),
            rec({'type': 'custom_tool_call_output', 'call_id': 'x', 'output': [{'text': '{"exit_code":0}'}]}),
            rec({'type': 'function_call', 'name': 'exec_command', 'call_id': 'y',
                 'arguments': json.dumps({'cmd': 'python3 skills/tool/scripts/tool.py send'})}),
            rec({'type': 'function_call', 'name': 'exec_command', 'call_id': 'z',     # another checkout
                 'arguments': json.dumps({'cmd': 'python3 skills/tool/scripts/tool.py send', 'workdir': '/else'})}),
            rec({'type': 'custom_tool_call', 'name': 'exec', 'call_id': 'w', 'input':
                 f'text({{cmd:"python3 {self.script} send"}}); tools.exec_command({{cmd:`python3 {self.script}`}});'
                 f'text(\'tools.exec_command({{cmd:"python3 {self.script} send"}})\');'
                 f'tools.exec_command({{cmd:"python3 {self.script}" + x}});'
                 f'tools.exec_command({{cmd:"python3 {self.script}", workdir: other}});'
                 f'tools.exec_command({{cmd:"python3 {self.script} send"}}); tools.exec_command({{cmd:"true"}});'}),
            rec({'type': 'custom_tool_call_output', 'call_id': 'w', 'output': [{'text': '{"exit_code":0}'}]}),
            rec({'type': 'custom_tool_call', 'name': 'exec', 'call_id': 'w', 'input': 'tools.exec_command(x);'}),
            rec({'type': 'custom_tool_call', 'name': 'exec', 'call_id': 'u', 'input':
                 f'text(\'a; tools.exec_command({{cmd:"python3 {self.script} send"}})\');\n'
                 f'// await tools.exec_command({{cmd:"python3 {self.script} send"}});\n'
                 f'await tools.exec_command({{cmd:"printf \\x41"}});'}),
            rec({'type': 'custom_tool_call', 'name': 'exec', 'call_id': 't', 'input':
                 f'if (false) await tools.exec_command({{cmd:"python3 {self.script} send"}});'}),
            rec({'type': 'custom_tool_call', 'name': 'exec', 'call_id': 's', 'input':
                 f'throw new Error("stop"); await tools.exec_command({{cmd:"python3 {self.script} send"}});'}),
            rec({'type': 'function_call', 'name': 'exec_command', 'call_id': 'v',
                 'arguments': json.dumps({'cmd': f'python3 {self.script}', 'workdir': '/else'})})])
        code, _, _ = run(usage.main, '--run', str(self.run_dir), '--codex-root', str(root), '--out', str(self.out))
        agg = json.loads(self.out.read_text())
        t = agg['scripts']['skills/tool/scripts/tool.py']
        # x ran alone and succeeded; y and v carry no result; w holds a statement that is no plain call, so none of
        # its calls count
        self.assertEqual((code, t['invoke'], t['ok'], t['result_unknown']), (0, 3, 1, 2))
        self.assertEqual(t['cwd'], {'repo': 2, 'other': 1})
        # w's five calls, \\x41, the calls after if and throw; the repeated call id w is skipped
        self.assertEqual(agg['unknown'], {'unparsed_codex_call': 8})

    def test_roots_and_output_are_checked(self):
        code, _, err = run(usage.main, '--run', str(self.run_dir), '--out', str(self.out))
        self.assertEqual((code, 'no transcript root' in err), (2, True))
        code, out, _ = self.usage('--claude-root', str(self.root / 'missing'))
        self.assertEqual(code, 1)
        self.assertIn('missing', out)
        if os.name == 'posix':          # Windows keeps only a read-only bit
            self.assertEqual(oct(self.out.stat().st_mode & 0o777), '0o600')
        self.assertEqual(self.usage()[0], 2)
        if os.name == 'posix' and os.geteuid() != 0:
            locked = self.root / 'locked'
            locked.mkdir()
            locked.chmod(0)
            self.addCleanup(locked.chmod, 0o700)
            self.out.unlink()
            code, out, _ = self.usage()
            self.assertEqual(code, 1)
            self.assertIn('partly unreadable', out)


class ShellAbs(unittest.TestCase):
    def test_a_root_relative_path_is_absolute_under_windows_path_rules_too(self):
        with mock.patch('os.path.isabs', ntpath.isabs):      # Python 3.13+ on Windows: '/x' is not absolute
            self.assertEqual([usage.shell_abs(p) for p in ('/gone/x', 'C:/x', 'C:\\x', 'x/y', 'C:x')],
                             [True, True, True, False, False])


class Overlap(unittest.TestCase):
    def test_overlap_counts_per_module_and_unmeasured_tests(self):
        if importlib.util.find_spec('coverage') is None:
            self.fail('coverage is a test dependency (BUILD.md)')
        work = tmpdir(self)
        (work / 'lib.py').write_text('def f(x):\n    return x + 1\n')
        (work / 'tests').mkdir()
        (work / 'tests' / 'test_m.py').write_text(textwrap.dedent('''\
            import subprocess, sys
            from lib import f
            def test_one():
                assert f(1) == 2
            def test_two():
                assert f(2) == 3
            def test_child():
                subprocess.run([sys.executable, '-c', 'pass'], check=True)
            '''))
        (work / 'rc').write_text(f'[run]\ndynamic_context = test_function\nbranch = True\n'
                                 f'data_file = {work}/.coverage\n')
        env = {**git_env(), 'PYTHONPATH': str(work)}
        subprocess.run([sys.executable, '-m', 'coverage', 'run', f'--rcfile={work}/rc', '-m', 'pytest', '-q',
                        '-p', 'no:cacheprovider', 'tests'], cwd=work, env=env, check=True, capture_output=True)
        (work / 'tests.txt').write_text('tests/test_m.py::test_one\ntests/test_m.py::test_two\n'
                                        'tests/test_m.py::test_child\n')
        code, out, _ = run(overlap.main, '--repo', str(work), '--data', str(work / '.coverage'), '--tests',
                           str(work / 'tests.txt'), '--out', str(work / 'o.json'))
        self.assertEqual(code, 0)
        mod = json.loads((work / 'o.json').read_text())['modules']['test_m']
        # same lines, different outputs: both listed for reading, neither called redundant
        self.assertEqual(mod['no_unique_unit'], ['test_m.test_one', 'test_m.test_two'])
        self.assertEqual(mod['unmeasured'], ['tests/test_m.py::test_child'])
        self.assertIn('provenance unknown', out)
        code, _, err = run(overlap.main, '--repo', str(work), '--data', str(work / '.coverage'), '--out',
                           str(work / 'o.json'))
        self.assertEqual((code, 'exists' in err), (2, True))
        code, _, err = run(overlap.main, '--repo', str(work), '--data', str(work / 'none'), '--out',
                           str(work / 'p.json'))
        self.assertEqual((code, 'no such' in err), (2, True))


if __name__ == '__main__':
    unittest.main()
