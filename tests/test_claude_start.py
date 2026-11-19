"""bin/claude-start against a real git checkout, with hc, herdr-app and PATH replaced."""
import importlib.machinery
import importlib.util
import os
import subprocess
import sys
import tempfile
import unittest
from pathlib import Path
from unittest import mock

ROOT = Path(__file__).resolve().parents[1]
sys.path.insert(0, str(Path(__file__).resolve().parent))
from helpers import guard_survivors  # noqa: E402

_loader = importlib.machinery.SourceFileLoader('claude_start', str(ROOT / 'bin' / 'claude-start'))
_spec = importlib.util.spec_from_loader('claude_start', _loader)
cs = importlib.util.module_from_spec(_spec)
_loader.exec_module(cs)


def setUpModule():
    guard_survivors(__name__)


def ran(rc, body=None, kind=None, msg=''):
    return rc, body, kind, msg


class StartTest(unittest.TestCase):
    def setUp(self):
        tmp = tempfile.TemporaryDirectory()
        self.addCleanup(tmp.cleanup)
        repo = Path(tmp.name) / 'proj'
        repo.mkdir()
        subprocess.run(['git', 'init', '-q', str(repo)], check=True)
        self.root = os.path.realpath(repo)
        self.elsewhere = os.path.realpath(tmp.name)   # an absolute directory that is not the checkout
        self.spaces = [{'id': 'spA', 'name': 'proj', 'folder': self.root, 'machine': None, 'tabs': [{'id': 'tbA'}]},
                       {'id': 'spB', 'name': 'other', 'folder': '/elsewhere', 'machine': None, 'tabs': []}]
        self.calls = []
        self.run_answer = ran(0, {'address': 't9', 'created_ms': 99})
        self.gets = {'t9': self.terminal()}
        self.missing = set()
        for target, attr, value in [
            (cs.hc, 'run', self._hc),
            (cs, 'herdr_spaces', lambda: self.spaces),
            (cs.shutil, 'which', lambda p: None if p in self.missing else f'/bin/{p}'),
        ]:
            patcher = mock.patch.object(target, attr, value)
            patcher.start()
            self.addCleanup(patcher.stop)

    def terminal(self, app='claude-code', state='live', cwd=None):
        return {'address': 't9', 'state': state, 'status': {'app': app, 'state': 'idle'},
                'foreground': {'cwd': cwd or self.root, 'pid': 77}}

    def _hc(self, *args, timeout):
        self.calls.append(args)
        if args[0] == 'run':
            return self.run_answer
        if args[0] == 'get':
            found = self.gets.get(args[-1])
            return (0, found, None, '') if found else (3, None, 'not_found', 'no such terminal')
        return ran(0, {'ended': True})

    def main(self, *argv):
        with mock.patch('builtins.print') as out:
            code = cs.main([*argv])
        lines = [(str(c.args[0]), bool(c.kwargs.get('file'))) for c in out.call_args_list]
        return code, '\n'.join(t for t, err in lines if not err), '\n'.join(t for t, err in lines if err)

    def launched(self):
        return [c for c in self.calls if c[0] == 'run']

    def test_main_runs_claude_with_its_config_dir_unset_in_a_new_tab_of_the_checkout_s_space(self):
        code, out, _ = self.main(self.root)
        self.assertEqual(code, 0)
        self.assertEqual(self.launched(), [('run', '--json', '--bg', '--keep', '--no-parent', '--name', 'proj-main',
                                            '--cwd', self.root, '--label', 'herdr.space=spA', '--',
                                            '/bin/env', '-u', 'CLAUDE_CONFIG_DIR', '/bin/claude')])
        self.assertIn('a new tab', out)
        self.assertIn('verified', out)

    def test_ada_runs_its_wrapper_with_the_task_as_its_first_prompt(self):
        code, out, _ = self.main(self.root, 'ada', '--task', 'fix issue 12')
        self.assertEqual(code, 0)
        run = self.launched()[0]
        self.assertEqual(run[run.index('--') + 1:], ('/bin/claude-ada', 'fix issue 12'))
        self.assertIn('proj-ada', run)
        self.assertIn('with its first task', out)

    def test_space_tab_and_name_are_passed_on(self):
        code, _, _ = self.main(self.root, '--space', 'other', '--name', 'mine')
        self.assertEqual(code, 0)
        self.assertIn('herdr.space=spB', self.launched()[0])
        self.assertIn('mine', self.launched()[0])
        code, _, _ = self.main(self.root, '--tab', 'tbA', '--name', 'mine2')
        self.assertEqual(code, 0)
        self.assertIn('herdr.tab=tbA', self.launched()[1])

    def test_checks_refuse_before_anything_starts(self):
        sub = os.path.join(self.root, 'sub')
        os.mkdir(sub)
        nogit = tempfile.mkdtemp()
        self.addCleanup(os.rmdir, nogit)
        cases = [
            (['relative/path'], 'absolute or ~/'),
            ([self.root + '-nope'], 'no such directory'),
            ([sub], 'not its top level'),
            ([nogit], 'not a git checkout'),
            ([self.root, '--tab', 'tbX'], 'tab tbX is not in space'),
            ([self.root, '--space', 'nowhere'], 'matches none'),
            ([self.root, '--name', 'T7'], 'hc names are'),
            ([self.root, '--name', 't7'], 'never an address'),
            ([self.root, '--task=-p'], 'does not start with -'),
            ([self.root, '--task', '  '], '--task must be text'),
        ]
        for argv, said in cases:
            with self.subTest(argv=argv):
                code, _, err = self.main(*argv)
                self.assertEqual(code, 2)
                self.assertIn(said, err)
        self.assertEqual(self.launched(), [])

    def test_a_missing_launcher_or_a_live_name_refuses(self):
        self.missing = {'claude-ada'}
        code, _, err = self.main(self.root, 'ada')
        self.assertEqual((code, 'claude-ada is not on PATH' in err), (2, True))
        self.gets['proj-main'] = {'address': 't3', 'state': 'live'}
        code, _, err = self.main(self.root)
        self.assertEqual((code, 'the live terminal t3 has that name' in err), (2, True))
        self.assertEqual(self.launched(), [])

    def test_a_space_without_the_checkout_or_with_it_twice_refuses(self):
        self.spaces[1]['folder'] = self.root
        code, _, err = self.main(self.root)
        self.assertEqual(code, 2)
        self.assertIn("'proj' (spA); 'other' (spB)", err)
        self.spaces = [{'id': 'r', 'name': 'remote', 'folder': self.root, 'machine': 'box'}]
        code, _, err = self.main(self.root, '--space', 'r')
        self.assertIn('is on box', err)

    def test_hc_refusal_and_an_uncertain_start(self):
        self.run_answer = ran(4, None, 'name_taken', 'proj-main is taken')
        code, _, err = self.main(self.root)
        self.assertEqual(code, 1)
        self.assertIn('nothing started: name_taken', err)
        self.run_answer = ran(None, None, 'timeout', 'hc run gave no answer')
        code, _, err = self.main(self.root)
        self.assertEqual(code, 3)
        self.assertIn('may have started', err)
        self.run_answer = ran(0, {'address': 'box/t9'})
        code, _, err = self.main(self.root)
        self.assertEqual(code, 3)
        self.assertIn('a terminal elsewhere: box/t9', err)

    def test_verification_outcomes(self):
        no_foreground = {k: v for k, v in self.terminal().items() if k != 'foreground'}
        for terminal, code, said in [
            (self.terminal(app='shell'), 0, 'verification pending (not yet a Claude Code session)'),
            (self.terminal(state='finished'), 1, 'VERIFICATION FAILED: its terminal is finished'),
            (self.terminal(cwd=self.elsewhere), 1, f'VERIFICATION FAILED: it runs in {self.elsewhere}'),
            (None, 1, 'VERIFICATION FAILED: its terminal is gone'),
            (no_foreground, 0, 'verification pending (its working directory is not yet known)'),
            ({**no_foreground, 'foreground': {'cwd': None, 'pid': 77}}, 0,
             'verification pending (its working directory is not yet known)'),
        ]:
            with self.subTest(terminal=terminal, said=said):
                self.gets = {'t9': terminal} if terminal else {}
                got, out, _ = self.main(self.root)
                self.assertEqual(got, code)
                self.assertIn(said, out)

    def test_a_non_ascii_checkout_and_herdr_app_output_are_read_whatever_the_locale(self):
        # U+0141 leaves a 0x81 that cp1252 cannot decode, and ASCII cannot either
        repo = Path(self.elsewhere) / 'prŁj'
        subprocess.run(['git', 'init', '-q', str(repo)], check=True)
        root = os.path.realpath(repo)
        herdr = Path(self.elsewhere) / 'herdr'
        herdr.mkdir()
        (herdr / 'ls').write_text('import json, sys\nsys.stdout.buffer.write(json.dumps({"spaces": [{"folder": '
                                  f'{root!r}}}, {{"id": "spA", "folder": {self.root!r}}}]}}, ensure_ascii=False)'
                                  '.encode())\n', encoding='utf-8')
        code = ('import importlib.machinery, importlib.util, sys; '
                'l = importlib.machinery.SourceFileLoader("cs", sys.argv[1]); '
                'm = importlib.util.module_from_spec(importlib.util.spec_from_loader("cs", l)); l.exec_module(m); '
                'm.shutil.which = lambda p: sys.executable; '   # herdr-app ls runs ./ls under this interpreter
                'print(ascii((m.checkout_root(sys.argv[2]) == sys.argv[2], m.herdr_spaces()[0]["folder"], '
                'm.placement(m.checkout_root(sys.argv[3]), None, None, m.herdr_spaces()))))')
        env = {k: v for k, v in os.environ.items() if k not in ('PYTHONUTF8', 'PYTHONIOENCODING')}
        if os.name != 'nt':
            env.update(LC_ALL='C', PYTHONCOERCECLOCALE='0')   # Windows reads the ANSI code page instead
        # an ASCII checkout is still placed when a neighbouring space's folder cannot be encoded in this locale
        r = subprocess.run([sys.executable, '-X', 'utf8=0', '-c', code, str(ROOT / 'bin' / 'claude-start'), root,
                            self.root], capture_output=True, text=True, env=env, cwd=herdr)
        self.assertEqual((r.returncode, r.stdout.strip()), (0, ascii((True, root, 'spA'))), r.stderr)

    def test_an_hc_failure_while_verifying_is_unchecked(self):
        def broken(*args, timeout):
            self.calls.append(args)
            if args[0] == 'run':
                return self.run_answer
            if args[0] == 'get' and any(c[0] == 'run' for c in self.calls):
                return ran(1, None, 'internal', 'boom')
            return ran(3, None, 'not_found', '') if args[0] == 'get' else ran(0, {})
        with mock.patch.object(cs.hc, 'run', broken):
            code, out, _ = self.main(self.root)
        self.assertEqual(code, 3)
        self.assertIn('VERIFICATION UNCHECKED', out)


if __name__ == '__main__':
    unittest.main()
