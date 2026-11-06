"""Tests for repo-audit's scripts on temporary repositories and synthetic transcripts."""
import contextlib
import importlib.util
import io
import json
import ntpath
import os
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

    def marker(self, name, rev, message=None):
        target = self.git('rev-parse', rev)
        self.git('tag', '-a', name, rev, '-m', message or f'{audit.MARKER}\nprevious: {"0" * 40}\nend: {target}\n')


class Start(unittest.TestCase):
    def setUp(self):
        self.repo = Repo(self)
        self.base = self.repo.commit({'skills/a/SKILL.md': 'one\n', 'tests/test_a.py': 'def test_x():\n  pass\n',
                                      'old.txt': 'x\n' * 40})
        self.head = self.repo.commit({'skills/a/SKILL.md': 'one\ntwo\n', 'tests/test_a.py': 'def test_y():\n  pass\n'
                                      'def test_z():\n  pass\n', 'img.bin': b'\0\1\2', 'old.txt': None,
                                      'new.txt': 'x\n' * 40})
        self.out = tmpdir(self) / 'run'

    def start(self, *extra):
        return run(audit.main, 'start', '--repo', str(self.repo.path), '--head', 'main', '--since', SINCE,
                   '--until', UNTIL, '--out', str(self.out), *extra)

    def test_inventory_counts_files_tests_prose_renames_and_binaries(self):
        code, out, _ = self.start('--base', self.base)
        self.assertEqual(code, 0)
        inv = json.loads((self.out / 'inventory.json').read_text())
        self.assertEqual((inv['commits'], inv['files']), (1, [3, 4]))
        self.assertEqual(inv['tests_added_removed'], [[2, 1, 'tests/test_a.py']])
        self.assertEqual(inv['prose_growth'], [[8, 4, 'skills/a/SKILL.md']])
        self.assertEqual(inv['binary_changed'], ['img.bin'])
        self.assertNotIn('old.txt', json.dumps(inv['areas']))      # a rename counts under its new path
        run_json = json.loads((self.out / 'run.json').read_text())
        self.assertEqual((run_json['base'], run_json['start_head']), (self.base, self.head))
        self.assertEqual(run_json['identity'], [self.base])
        self.assertIn('1 commits', out)

    def test_output_directory_and_window_are_checked(self):
        self.assertEqual(self.start('--base', self.base)[0], 0)
        code, _, err = self.start('--base', self.base)
        self.assertEqual(code, 2)
        self.assertIn('exists', err)
        code, _, err = run(audit.main, 'start', '--repo', str(self.repo.path), '--head', 'main', '--base', self.base,
                           '--since', UNTIL, '--until', SINCE, '--out', str(self.out) + '2')
        self.assertEqual((code, '--since' in err), (2, True))

    def test_without_base_or_marker_it_refuses(self):
        self.repo.git('tag', 'audit/lightweight', self.base)
        self.repo.marker('audit/v10', self.base, f'repo-audit-marker: 10\nprevious: {"0" * 40}\nend: {self.base}\n')
        self.repo.marker('audit/other-end', self.head, f'{audit.MARKER}\nprevious: {"0" * 40}\nend: {self.base}\n')
        code, _, err = self.start()
        self.assertEqual(code, 2)
        for name in ('audit/lightweight', 'audit/v10', 'audit/other-end'):     # malformed: reported, never used
            self.assertIn(name, err)
        self.assertIn('pass --base', err)

    def test_a_window_compares_instants_not_text(self):
        code, _, _ = run(audit.main, 'start', '--repo', str(self.repo.path), '--head', 'main', '--base', self.base,
                         '--since', '2026-01-01T10:00:00+07:00', '--until', '2026-01-01T04:00:00Z', '--out',
                         str(self.out))
        self.assertEqual(code, 0)

    def test_a_deleted_test_file_counts_its_removed_tests(self):
        self.repo.commit({'tests/test_a.py': None, 'tests/test é b.py': 'def test_q():\n  pass\n'})
        self.assertEqual(self.start('--base', self.head)[0], 0)
        inv = json.loads((self.out / 'inventory.json').read_text(encoding='utf-8'))
        self.assertEqual(sorted(inv['tests_added_removed']), [[0, 2, 'tests/test_a.py'], [1, 0, 'tests/test é b.py']])

    @unittest.skipUnless(os.name == 'posix', 'a double quote cannot be in a Windows file name')
    def test_a_name_git_quotes_is_decoded(self):
        self.repo.commit({'tests/café "q".py': 'def test_q():\n  pass\n'})
        self.assertEqual(self.start('--base', self.head)[0], 0)
        inv = json.loads((self.out / 'inventory.json').read_text(encoding='utf-8'))
        self.assertEqual(inv['tests_added_removed'], [[1, 0, 'tests/café "q".py']])

    def test_the_latest_marker_by_ancestry_is_the_base(self):
        self.repo.marker('audit/z-older', self.base)
        mid = self.repo.git('rev-parse', 'HEAD~0')
        self.repo.marker('audit/a-newer', mid)
        self.repo.commit({'more.txt': 'm\n'})
        self.assertEqual(self.start()[0], 0)
        run_json = json.loads((self.out / 'run.json').read_text())
        self.assertEqual((run_json['base'], run_json['marker']), (mid, 'audit/a-newer'))

    def test_markers_on_diverged_branches_are_ambiguous(self):
        self.repo.git('checkout', '-q', '-b', 'side', self.base)
        side = self.repo.commit({'side.txt': 's\n'})
        self.repo.git('checkout', '-q', 'main')
        self.repo.marker('audit/one', self.head)
        self.repo.marker('audit/two', side)
        self.repo.git('merge', '-q', '--no-edit', 'side')
        code, _, err = self.start()
        self.assertEqual(code, 2)
        self.assertIn('no single latest marker', err)

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


class Finish(unittest.TestCase):
    def setUp(self):
        self.repo = Repo(self)
        self.base = self.repo.commit({'a.txt': '1\n'})
        self.head = self.repo.commit({'a.txt': '2\n'})
        self.out = tmpdir(self) / 'run'
        run(audit.main, 'start', '--repo', str(self.repo.path), '--base', self.base, '--head', self.head,
            '--since', SINCE, '--until', UNTIL, '--out', str(self.out))

    def test_finish_writes_the_aggregate_and_a_tag_message_draft(self):
        end = self.repo.commit({'b.txt': '3\n'})
        code, out, _ = run(audit.main, 'finish', '--run', str(self.out), '--end', end)
        self.assertEqual(code, 0)
        msg = (self.out / 'tag-message.txt').read_text()
        self.assertTrue(msg.startswith(audit.MARKER))
        self.assertIn(f'previous: {self.base}', msg)
        self.assertIn(f'end: {end}', msg)
        self.assertEqual(json.loads((self.out / 'finish-inventory.json').read_text())['commits'], 2)
        self.assertEqual(self.repo.git('tag'), '')       # no tag is created

    def test_an_end_before_the_start_head_is_refused(self):
        code, _, err = run(audit.main, 'finish', '--run', str(self.out), '--end', self.base)
        self.assertEqual(code, 2)
        self.assertIn('start_head', err)

    def test_another_repository_is_refused(self):
        other = Repo(self)
        other.commit({'x': 'y'})
        data = json.loads((self.out / 'run.json').read_text())
        (self.out / 'run.json').write_text(json.dumps(dict(data, repo=str(other.path))))
        code, _, err = run(audit.main, 'finish', '--run', str(self.out), '--end', 'HEAD')
        self.assertEqual(code, 2)
        self.assertIn('not the repository', err)


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
        run(audit.main, 'start', '--repo', str(self.repo.path), '--base', head, '--head', head, '--since', SINCE,
            '--until', UNTIL, '--out', str(self.run_dir))
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
