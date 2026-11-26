"""The canonical Arduino bootstrap: selection, config, fetch, copy equality and frozen shared-source identity.
The consumer bootstrap tests live here; consumers retain their profile/config and selected-copy integration tests.
Local git repositories and packed trees exercise the real loader; no network or rig is used."""
import contextlib
import importlib.util
import io
import json
import os
import re
import shutil
import stat
import subprocess
import sys
import types
import unittest
from pathlib import Path
from unittest import mock

sys.path.insert(0, str(Path(__file__).resolve().parent))
from helpers import git_env, guard_survivors, load_arduino_engine, load_arduino_fixture, tmpdir  # noqa: E402

ROOT = Path(__file__).resolve().parents[1]
CANONICAL = 'skills/arduino/scripts/agentrc_pin.py'
CONSUMER = '.claude/skills/hil-contract/scripts/agentrc_pin.py'
SOURCE = 'skills/hil/scripts/agentrc_source.py'
LOADER = 'skills/hil/scripts/hil_load.py'
CONFORMANCE = 'skills/arduino/scripts/pin_conformance.py'
BUILD = 'skills/arduino/scripts/arduino_build.py'
CI_ENGINE = 'skills/arduino/scripts/arduino_ci.py'
EXTRA = 'skills/hil/scripts/gh_pr_comment.py'
CONFIG = 'test/hil/agentrc.json'
PROFILE = '.claude/skills/build/scripts/check_build.py'
CLEAN_ENV = {k: v for k, v in os.environ.items()
             if not k.startswith('GIT_') and k not in ('AGENTRC_DIR', 'CI', 'GITHUB_ACTIONS')}


def setUpModule():
    guard_survivors(__name__)


def git(root, *args):
    return subprocess.run(['git', '-C', str(root), *args], check=True, capture_output=True, text=True,
                          env=git_env()).stdout.strip()


def load(path):
    spec = importlib.util.spec_from_file_location('bootstrap_under_test', path)
    mod = importlib.util.module_from_spec(spec)
    spec.loader.exec_module(mod)
    return mod


def unload():
    for name in list(sys.modules):
        if name.startswith('agentrc_'):
            del sys.modules[name]


class Consumer(unittest.TestCase):
    def setUp(self):
        self.tmp = tmpdir(self)
        self.stack = contextlib.ExitStack()
        self.addCleanup(self.stack.close)
        self.stack.enter_context(mock.patch.dict(os.environ, CLEAN_ENV, clear=True))
        self.stack.enter_context(mock.patch.dict(sys.modules))
        unload()
        self.root = self.tmp / 'consumer'
        (self.root / CONSUMER).parent.mkdir(parents=True)
        shutil.copyfile(ROOT / CANONICAL, self.root / CONSUMER)
        self.pin = load(self.root / CONSUMER)
        self.repo = self.tmp / 'agentrc'
        for rel in (CANONICAL, CONFORMANCE, SOURCE, LOADER):
            (self.repo / rel).parent.mkdir(parents=True, exist_ok=True)
            shutil.copyfile(ROOT / rel, self.repo / rel)
        for rel in (*self.pin.ENGINES, EXTRA, CI_ENGINE):
            (self.repo / rel).write_text(f'FILES = ({LOADER!r}, {SOURCE!r}, "lib/data.py")\n'
                                       'PROFILE_API = 1\nPROFILE_FEATURES = ("core", "named", "battery")\n')
        (self.repo / 'lib').mkdir()
        (self.repo / 'lib/data.py').write_text('VALUE = 1\n')
        (self.repo / 'README.md').write_text('outside the closure\n')
        (self.repo / '.gitignore').write_text('__pycache__/\n')
        git(self.repo, 'init', '-q')
        git(self.repo, 'add', '.')
        git(self.repo, 'commit', '-qm', 'fixture')
        self.head = git(self.repo, 'rev-parse', 'HEAD')
        self.data = {'url': str(self.repo), 'commit': self.head, 'cache_namespace': 'fixture',
                     'profile_api': 1, 'features': [], 'extra_engines': []}
        self.config()
        self.profile()
        self.install = self.tmp / 'home/.claude'
        (self.install / 'skills').mkdir(parents=True)
        for skill in ('arduino', 'hil'):
            (self.install / 'skills' / skill).symlink_to(self.repo / 'skills' / skill, target_is_directory=True)
        home = str(self.tmp / 'home')   # Path.home() reads USERPROFILE on Windows, HOME elsewhere
        self.env(HOME=home, USERPROFILE=home, XDG_CACHE_HOME=str(self.tmp / 'cache'))

    def env(self, **kw):
        self.stack.enter_context(mock.patch.dict(os.environ, kw))

    def config(self, **kw):
        self.pin.config.cache_clear()
        self.data.update(kw)
        (self.root / CONFIG).parent.mkdir(parents=True, exist_ok=True)
        (self.root / CONFIG).write_text(json.dumps(self.data))

    def profile(self, contract=(PROFILE, CONSUMER, CONFIG)):
        (self.root / PROFILE).parent.mkdir(parents=True, exist_ok=True)
        (self.root / PROFILE).write_text(f'import agentrc_pin\nCONTRACT_FILES = {tuple(contract)!r}\n\n\n'
                                         f'def engine():\n    return agentrc_pin.load({BUILD!r})\n')

    def runner_probe(self, root, source):
        tests = root / '.claude/test'
        tests.mkdir()
        (tests / 'test_probe.py').write_text(
            'import os, sys, unittest\n'
            f'sys.path.insert(0, {str(root / Path(CONSUMER).parent)!r})\n'
            'import agentrc_pin\nclass Probe(unittest.TestCase):\n'
            '    def test_source_and_environment(self):\n'
            '        self.assertFalse(any(k.startswith("GIT_") for k in os.environ))\n'
            f'        self.assertEqual(agentrc_pin.identity()["source"], {source!r})\n')

    def stage(self):
        helper, _, ident = self.pin._selection()
        tree = self.tmp / 'tree'
        digests = helper.stage(ident, tree, name='agentrc')
        for rel in (CONSUMER, CONFIG, PROFILE):
            (tree / rel).parent.mkdir(parents=True, exist_ok=True)
            shutil.copyfile(self.root / rel, tree / rel)
            digests[rel] = helper.sha256(tree / rel)
        (tree / 'manifest.json').write_text(json.dumps({'agentrc': {'commit': ident['commit'],
                                                                   'dirty': ident['dirty']}, 'files': digests}))
        unload()
        return tree, load(tree / CONSUMER), ident


class Configuration(Consumer):
    def test_configuration_is_read_and_validated_once(self):
        with mock.patch.object(self.pin.json, 'loads', wraps=json.loads) as reads:
            first = self.pin.config()
            (self.root / CONFIG).write_text('invalid after selection')
            self.assertIs(self.pin.config(), first)
            self.assertEqual(self.pin.pin(), (str(self.repo), self.head))
            self.assertEqual(self.pin.cache_dir(self.head), self.tmp / 'cache/fixture' / f'agentrc-{self.head}')
        self.assertEqual(reads.call_count, 1)

    def test_shared_closure_refuses_an_extra_engine_outside_agentrc(self):
        self.config(extra_engines=['../outside.py'])
        with self.assertRaisesRegex(self.pin.PinError, "outside itself: .*outside.py") as cm:
            self.pin.identity()
        self.assertEqual(cm.exception.remedy, self.pin.INSTALL_HINT)

    def test_explicit_fields_and_safe_namespace_and_paths(self):
        self.assertEqual(self.pin.config(), self.data)
        self.assertEqual(self.pin.pin(), (str(self.repo), self.head))
        self.assertEqual(self.pin.cache_dir(self.head), self.tmp / 'cache/fixture' / f'agentrc-{self.head}')
        self.pin.config.cache_clear()
        bad = {'url': [None, 0, '', '  '], 'commit': [None, 1, 'abc', 'g' * 40],
               'cache_namespace': [None, 1, '', '.', '..', '../x', '/x', 'C:x', 'x/y', 'x\\y'],
               'profile_api': [None, True, 0, -1, '1', 1.0],
               'features': [None, 'core', {}, [1], [''], [' ']],
               'extra_engines': [None, 'file.py', {}, [1], ['']]}
        for field, values in bad.items():
            for value in values:
                with self.subTest(field=field, value=value):
                    (self.root / CONFIG).write_text(json.dumps({**self.data, field: value}))
                    with self.assertRaisesRegex(self.pin.PinError, f'invalid {field}'):
                        self.pin.config()
            absent = dict(self.data)
            del absent[field]
            (self.root / CONFIG).write_text(json.dumps(absent))
            with self.assertRaisesRegex(self.pin.PinError, f'invalid {field}'):
                self.pin.config()

    def test_unreadable_or_nonobject_configuration(self):
        for raw in (b'{', b'\xff', b'[]', b'null', b'[' * 10000):
            with self.subTest(raw=raw[:20]):
                (self.root / CONFIG).write_bytes(raw)
                with self.assertRaisesRegex(self.pin.PinError, re.escape(str(Path(CONFIG)))):
                    self.pin.config()
        (self.root / CONFIG).unlink()
        with self.assertRaisesRegex(self.pin.PinError, 'not readable JSON'):
            self.pin.config()

    def test_invalid_configuration_refused_locally_before_discovery(self):
        self.config(features='core')
        with mock.patch.object(self.pin, 'loader', side_effect=AssertionError('executed')):
            with self.assertRaisesRegex(self.pin.PinError, 'features'):
                self.pin.identity()
        (self.root / CONFIG).write_text(json.dumps({**self.data, 'features': []}))
        self.assertEqual(self.pin.identity()['source'], 'installed')   # failures are not cached

    def test_ci_boolean_values_and_fetch_local_refusal(self):
        for var in ('GITHUB_ACTIONS', 'CI'):
            for value, expected in (('true', True), ('1', True), ('YES', True), ('false', False), ('0', False),
                                    ('no', False), ('', False)):
                with self.subTest(var=var, value=value), mock.patch.dict(os.environ, {var: value}):
                    self.assertEqual(self.pin.ci(), expected)
        with mock.patch.dict(os.environ, {'CI': 'maybe'}):
            with self.assertRaisesRegex(self.pin.PinError, "CI='maybe'"):
                self.pin.ci()
        with self.assertRaisesRegex(self.pin.PinError, 'fetch runs in CI only') as cm:
            self.pin.fetch()
        self.assertEqual(cm.exception.remedy, self.pin.INSTALL_HINT)
        self.assertIn(self.pin.INSTALL_HINT, cm.exception.with_remedy())
        self.assertEqual(self.pin.PinError('x').with_remedy(), 'x')


class Installed(Consumer):
    def test_profile_capabilities_share_one_parse(self):
        path = self.repo / BUILD
        source = path.read_text() + '\nBUILD_MARKER = True\n'
        path.write_text(source)
        with mock.patch.object(self.pin.ast, 'parse', wraps=self.pin.ast.parse) as parse:
            self.pin.identity()
        self.assertEqual(sum(call.args[0] == source for call in parse.call_args_list), 2)   # FILES, then capabilities

    def test_identity_uses_the_checkout_behind_skill_links_and_extra_engines(self):
        self.config(extra_engines=[EXTRA])
        ident = self.pin.identity()
        self.assertEqual((ident['source'], ident['root'], ident['commit'], ident['dirty']),
                         ('installed', str(self.repo), self.head, False))
        self.assertEqual(ident['files'], sorted([*self.pin.ENGINES, SOURCE, LOADER, CANONICAL, CONFORMANCE, EXTRA,
                                               'lib/data.py']))
        self.assertEqual(self.pin.load(BUILD).__file__, str(self.repo / BUILD))
        self.assertEqual(self.pin.load('lib/data.py').VALUE, 1)
        with self.assertRaisesRegex(self.pin.PinError, 'not in the agentrc closure'):
            self.pin.load('README.md')

    def test_ci_wrapper_must_declare_its_extra_engine(self):
        with self.assertRaisesRegex(self.pin.PinError, 'not in the agentrc closure'):
            self.pin.load(CI_ENGINE)
        self.config(extra_engines=[CI_ENGINE, EXTRA])
        self.pin._selection.cache_clear()
        self.assertEqual(self.pin.load(CI_ENGINE).__file__, str(self.repo / CI_ENGINE))
        self.assertEqual(self.pin.load(EXTRA).__file__, str(self.repo / EXTRA))

    def test_dirty_counts_only_closure_files_at_first_selection(self):
        (self.repo / 'README.md').write_text('edited\n')
        self.assertFalse(self.pin.identity()['dirty'])
        self.pin._selection()[0]._FROZEN.clear()
        self.pin._selection.cache_clear()
        (self.repo / BUILD).write_text((self.repo / BUILD).read_text() + '# changed\n')
        self.assertTrue(self.pin.identity()['dirty'])

    def test_missing_install_and_copy_outside_git(self):
        self.env(AGENTRC_DIR=str(self.tmp / 'absent'))
        with self.assertRaisesRegex(self.pin.PinError, 'has no agentrc arduino skill') as cm:
            self.pin.identity()
        self.assertEqual(cm.exception.remedy, self.pin.INSTALL_HINT)
        copy = self.tmp / 'copy'
        shutil.copytree(self.repo, copy, ignore=shutil.ignore_patterns('.git'))
        self.env(AGENTRC_DIR=str(copy))
        with self.assertRaisesRegex(self.pin.PinError, 'in no git checkout'):
            self.pin.identity()

    def test_mixed_skill_installs_are_refused(self):
        other = self.tmp / 'other'
        shutil.copytree(self.repo, other)
        (self.install / 'skills/arduino').unlink()
        (self.install / 'skills/arduino').symlink_to(other / 'skills/arduino', target_is_directory=True)
        with self.assertRaisesRegex(self.pin.PinError, 'one agentrc checkout must hold them all'):
            self.pin.identity()

    def test_a_loader_already_from_another_checkout_is_refused(self):
        self.pin.loader(ROOT)
        with self.assertRaisesRegex(self.pin.PinError, 'one process loads one copy'):
            self.pin.identity()

    def test_api_and_features_checked_without_running_engines(self):
        for declaration, why in (('PROFILE_API = 99', 'implements profile API 99'),
                                 ('PROFILE_API = True', 'implements profile API True'),
                                 ('PROFILE_API = 1\nPROFILE_FEATURES = ()', 'lacks profile features'),
                                 ('PROFILE_API = 1\nPROFILE_FEATURES = None', 'malformed PROFILE_FEATURES')):
            with self.subTest(declaration=declaration):
                self.pin._selection.cache_clear()
                unload()
                self.config(features=['core'])
                (self.repo / BUILD).write_text(f'{declaration}\nraise AssertionError("engine executed")\n')
                with self.assertRaisesRegex(self.pin.PinError, why):
                    self.pin.load(BUILD)

    def test_source_api_checked_by_ast_before_any_agentrc_executes(self):
        for declaration in ('API = 99', 'API = True', 'API = ('):
            (self.repo / SOURCE).write_text(f'{declaration}\nraise AssertionError("source executed")\n')
            with self.subTest(declaration=declaration), \
                    mock.patch.object(self.pin, 'loader', side_effect=AssertionError('loader executed')):
                with self.assertRaisesRegex(self.pin.PinError, 'API'):
                    self.pin.identity()

    def test_missing_closure_file(self):
        (self.repo / 'lib/data.py').unlink()
        with self.assertRaisesRegex(self.pin.PinError, 'lacks lib/data.py'):
            self.pin.identity()

    def test_selection_is_cached_but_every_load_check_and_binding_detects_drift(self):
        helper = self.pin.loader(self.repo).load_file(SOURCE)
        with mock.patch.object(helper, '_git', wraps=helper._git) as git_calls, \
                mock.patch.object(helper, 'literal', wraps=helper.literal) as ast_calls, \
                mock.patch.object(self.pin, 'source', wraps=self.pin.source) as discoveries:
            ident = self.pin.identity()
            counts = (git_calls.call_count, ast_calls.call_count, discoveries.call_count)
            self.assertTrue(all(counts))
            self.env(AGENTRC_DIR=str(self.tmp / 'other'), CI='true')
            for _ in range(3):
                self.assertIs(self.pin.identity(), ident)
                self.assertEqual(self.pin.selected()[:2], ('installed', self.repo))
                self.pin.load(BUILD)
                self.pin.check()
                helper.binding(ident)
            self.assertEqual((git_calls.call_count, ast_calls.call_count, discoveries.call_count), counts)
            path = self.repo / 'lib/data.py'
            before = path.read_bytes()
            path.write_text('VALUE = 2\n')
            for call in (lambda: self.pin.load(BUILD), self.pin.check):
                with self.assertRaisesRegex(self.pin.PinError, 'lib/data.py changed since'):
                    call()
            with self.assertRaisesRegex(helper.SourceError, 'lib/data.py changed since'):
                helper.binding(ident)
            with self.assertRaisesRegex(helper.SourceError, 'lib/data.py changed since'):
                helper.stage(ident, self.tmp / 'refused')
            path.write_bytes(before)
            self.assertEqual(self.pin.check(), self.repo)


class Runner(Consumer):
    def test_copy_mismatch_names_both_paths_and_the_copy_repin_remedy(self):
        (self.root / CONSUMER).write_bytes((self.root / CONSUMER).read_bytes() + b'\n')
        self.pin.identity()
        with mock.patch.object(self.pin.subprocess, 'run', side_effect=AssertionError('tests ran')):
            with self.assertRaises(self.pin.PinError) as cm:
                self.pin.test()
        self.assertIn(str(self.root / CONSUMER), str(cm.exception))
        self.assertIn(str(self.repo / CANONICAL), str(cm.exception))
        self.assertIn('copy', cm.exception.remedy)
        self.assertIn('repin', cm.exception.remedy)

    def test_installed_check_and_test_never_fetch_and_child_uses_absolute_checkout(self):
        self.addCleanup(os.chdir, Path.cwd())
        os.chdir(self.tmp)
        self.env(AGENTRC_DIR='home/.claude')
        self.pin.identity()
        self.env(GIT_DIR='x', GIT_INDEX_FILE='x', GIT_CONFIG_COUNT='1')
        before = dict(os.environ)
        with mock.patch.object(self.pin, 'fetch', side_effect=AssertionError('fetched')), \
                mock.patch.object(self.pin, 'conformance', return_value=[]), \
                contextlib.redirect_stdout(io.StringIO()) as out:
            self.assertEqual(self.pin.main(['check']), 0)
            with mock.patch.object(self.pin.subprocess, 'run', return_value=mock.Mock(returncode=5)) as run:
                self.assertEqual(self.pin.test(), 5)
        self.assertEqual(out.getvalue().strip(), str(self.repo))
        env = run.call_args.kwargs['env']
        self.assertFalse(any(k.startswith('GIT_') for k in env))
        self.assertEqual(env['AGENTRC_DIR'], str(self.repo))
        self.assertEqual(env['PATH'], os.environ['PATH'])
        self.assertEqual(run.call_args.kwargs['cwd'], self.root)
        self.assertEqual(dict(os.environ), before)

    def test_a_hook_child_initializes_its_own_git_checkout(self):
        git(self.repo, 'worktree', 'add', '-q', '--detach', str(self.tmp / 'wt'))
        target = self.tmp / 'target'
        tests = self.root / '.claude/test'
        tests.mkdir()
        (tests / 'test_probe.py').write_text(
            'import subprocess, unittest\nclass Probe(unittest.TestCase):\n'
            '    def test_git(self):\n'
            f'        subprocess.run(["git", "init", "-q", {str(target)!r}], check=True)\n')
        before = (self.repo / '.git/config').read_bytes()
        hook = self.repo / '.git/worktrees/wt'
        self.env(GIT_DIR=str(hook), GIT_INDEX_FILE=str(hook / 'index'))
        self.assertEqual(self.pin.test(), 0)
        self.assertTrue((target / '.git').is_dir())
        self.assertEqual((self.repo / '.git/config').read_bytes(), before)

    def test_main_reports_errors_and_usage(self):
        self.config(profile_api='1')
        with contextlib.redirect_stderr(io.StringIO()) as err:
            self.assertEqual(self.pin.main(['check']), 1)
        self.assertIn('invalid profile_api', err.getvalue())
        with contextlib.redirect_stderr(io.StringIO()):
            self.assertEqual(self.pin.main([]), 2)


class Pin(Consumer):
    def setUp(self):
        super().setUp()
        self.env(CI='true', AGENTRC_DIR=str(self.tmp / 'must-not-use'))

    def windows_unlink(self):
        """Refuse to unlink a read-only file, as Windows does, so git's read-only objects test cleanup anywhere."""
        real = os.unlink

        def unlink(path, *, dir_fd=None):
            if not os.stat(path, dir_fd=dir_fd, follow_symlinks=False).st_mode & stat.S_IWRITE:
                raise PermissionError(13, 'read-only file', path)
            real(path, dir_fd=dir_fd)
        self.stack.enter_context(mock.patch.object(os, 'unlink', unlink))

    def test_ci_fetch_checkout_and_identity_ignore_installed(self):
        with self.assertRaisesRegex(self.pin.PinError, 'agentrc pin') as cm:
            self.pin.check()
        self.assertEqual(cm.exception.remedy, self.pin.FETCH_HINT)
        dest = self.pin.fetch()
        self.assertEqual(self.pin.fetch(), dest)
        self.assertEqual(self.pin.checkout(), dest)
        ident = self.pin.identity()
        self.assertEqual((ident['source'], ident['commit'], ident['root'], ident['dirty']),
                         ('pin', self.head, str(dest), False))
        self.assertIn('not the pin', self.pin.problem(dest, '0' * 40))
        (dest / 'README.md').write_text('edited')
        with self.assertRaisesRegex(self.pin.PinError, 'local changes'):
            self.pin.checkout()
        with self.assertRaisesRegex(self.pin.PinError, 'remove it and fetch again'):
            self.pin.fetch()

    def test_test_fetches_first_and_checks_the_pinned_canonical(self):
        self.runner_probe(self.root, 'pin')
        self.env(GIT_DIR='x', GIT_INDEX_FILE='x', GIT_CONFIG_COUNT='1')
        with mock.patch.object(self.pin, 'fetch', wraps=self.pin.fetch) as fetch:
            self.assertEqual(self.pin.test(), 0)
        self.assertEqual(fetch.call_count, 1)
        self.assertEqual(self.pin.identity()['source'], 'pin')

    def test_concurrent_first_fetch_validates_the_winner(self):
        self.windows_unlink()
        dest = self.pin.cache_dir(self.head)
        real = self.pin._git

        def winner(root, *args):
            result = real(root, *args)
            if args[0] == 'checkout':
                shutil.copytree(root, dest)
            return result
        with mock.patch.object(self.pin, '_git', winner):
            self.assertEqual(self.pin.fetch(), dest)
        self.assertEqual(self.pin.problem(dest, self.head), '')
        self.assertEqual(list(dest.parent.iterdir()), [dest])

    def test_invalid_winner_and_fetch_failure_leave_no_temporary_tree(self):
        self.windows_unlink()
        dest = self.pin.cache_dir(self.head)
        real = self.pin._git

        def winner(root, *args):
            result = real(root, *args)
            if args[0] == 'checkout':
                dest.mkdir()
                (dest / 'junk').write_text('not a checkout')
            return result
        with mock.patch.object(self.pin, '_git', winner):
            with self.assertRaisesRegex(self.pin.PinError, 'not a git checkout'):
                self.pin.fetch()
        shutil.rmtree(dest)
        self.config(url=str(self.tmp / 'missing'))
        with self.assertRaisesRegex(self.pin.PinError, 'git fetch'):
            self.pin.fetch()
        self.assertEqual(list(dest.parent.iterdir()), [])


    def restore_mode(self, *paths):
        for path in paths:
            self.addCleanup(os.chmod, path, 0o755)

    @unittest.skipIf(os.name == 'nt' or os.geteuid() == 0, 'needs POSIX permissions that bind the caller')
    def test_cleanup_ignores_an_unreadable_directory(self):
        tree = self.tmp / 'tree'
        tree.mkdir(mode=0)
        self.restore_mode(tree)
        self.pin._remove(tree)

    @unittest.skipIf(os.name == 'nt' or os.geteuid() == 0, 'needs POSIX permissions that bind the caller')
    def test_cleanup_leaves_directory_modes_alone(self):
        sealed = self.tmp / 'tree/sealed'
        sealed.mkdir(parents=True)
        (sealed / 'kept').write_text('x')
        sealed.chmod(0o500)
        self.restore_mode(sealed, sealed.parent)
        before = sealed.parent.stat().st_mode, sealed.stat().st_mode
        self.pin._remove(sealed.parent)
        self.assertEqual((sealed.parent.stat().st_mode, sealed.stat().st_mode), before)
        self.assertTrue((sealed / 'kept').is_file())

    def test_cleanup_never_chmods_through_a_symlink(self):
        outside = self.tmp / 'outside'
        outside.mkdir()
        (outside / 'kept').write_text('x')
        link = self.tmp / 'link'
        try:
            link.symlink_to(outside, target_is_directory=True)
        except OSError as e:
            self.skipTest(f'no symlinks: {e}')
        before = outside.stat().st_mode
        self.pin._remove(link)
        self.assertEqual(outside.stat().st_mode, before)
        self.assertTrue((outside / 'kept').is_file())
        if os.name == 'nt' or os.geteuid() == 0:
            return
        sealed = self.tmp / 'tree/sealed'
        sealed.mkdir(parents=True)
        (sealed / 'link').symlink_to(outside / 'kept')
        sealed.chmod(0o500)
        self.restore_mode(sealed, sealed.parent)
        before = (outside / 'kept').stat().st_mode
        self.pin._remove(sealed.parent)
        self.assertEqual((outside / 'kept').stat().st_mode, before)


class Staged(Consumer):
    def test_precedence_and_partial_staging_never_fall_back(self):
        self.assertEqual(self.pin.source(), 'installed')
        self.env(CI='true')
        self.assertEqual(self.pin.source(), 'pin')
        (self.root / 'agentrc').mkdir()
        with self.assertRaisesRegex(self.pin.PinError, 'partial staged tree'):
            self.pin.identity()
        (self.root / 'manifest.json').write_text('{}')
        self.assertEqual(self.pin.source(), 'staged')
        (self.root / '.git').mkdir()
        self.assertEqual(self.pin.source(), 'pin')

    def test_round_trip_uses_inner_identity_even_with_ci_and_an_install(self):
        tree, pin, ident = self.stage()
        self.env(CI='true', AGENTRC_DIR=str(self.tmp / 'missing'))
        helper, _, staged = pin._selection()
        self.assertEqual(helper.portable(staged), helper.portable(ident))
        self.assertEqual(staged['root'], str(tree / 'agentrc'))
        self.assertEqual(staged['source'], 'staged')
        self.assertEqual(pin.load(BUILD).__file__, str(tree / 'agentrc' / BUILD))
        moved = {**helper.portable(ident), 'commit': 'b' * 40}
        self.assertEqual(helper.mismatch(moved, staged, commit=False), '')
        self.runner_probe(tree, 'staged')
        self.env(GIT_DIR='x', GIT_INDEX_FILE='x', GIT_CONFIG_COUNT='1')
        with mock.patch.object(pin, 'fetch', side_effect=AssertionError('fetched')):
            self.assertEqual(pin.test(), 0)

    def test_old_staging_without_inner_manifest_is_refused(self):
        tree, pin, _ = self.stage()
        (tree / 'agentrc/manifest.json').unlink()
        with self.assertRaisesRegex(pin.PinError, 'manifest.json is not readable JSON') as cm:
            pin.check()
        self.assertEqual(cm.exception.remedy, pin.STAGE_HINT)

    def test_corrupt_staged_file_and_inexact_closure_refused(self):
        tree, pin, _ = self.stage()
        path = tree / 'agentrc' / BUILD
        before = path.read_bytes()
        path.write_bytes(b'raise AssertionError("ran corrupt engine")\n')
        with self.assertRaisesRegex(pin.PinError, 'differ from their manifest'):
            pin.load(BUILD)
        path.write_bytes(before)
        manifest = tree / 'agentrc/manifest.json'
        data = json.loads(manifest.read_text())
        del data['sha256'][CANONICAL]
        manifest.write_text(json.dumps(data))
        with self.assertRaisesRegex(pin.PinError, 'unlike the closure'):
            pin.load(BUILD)

    def test_staged_symlink_and_live_same_bytes_symlink_are_refused(self):
        tree, pin, _ = self.stage()
        path = tree / 'agentrc/lib/data.py'
        path.unlink()
        path.symlink_to(self.repo / 'lib/data.py')
        with self.assertRaisesRegex(pin.PinError, 'not a regular file'):
            pin.load(BUILD)
        path.unlink()
        shutil.copyfile(self.repo / 'lib/data.py', path)
        pin.identity()
        path.unlink()
        path.symlink_to(self.repo / 'lib/data.py')
        with self.assertRaisesRegex(pin.PinError, 'lib/data.py changed since'):
            pin.load(BUILD)
        helper, _, ident = pin._selection()
        dest = self.tmp / 'restaged'
        with self.assertRaisesRegex(helper.SourceError, 'lib/data.py changed since'):
            helper.stage(ident, dest)
        self.assertFalse(dest.exists())

    def test_symlinked_inner_manifest_is_refused(self):
        tree, pin, _ = self.stage()
        manifest = tree / 'agentrc/manifest.json'
        outside = self.tmp / 'manifest.json'
        manifest.rename(outside)
        manifest.symlink_to(outside)
        with self.assertRaisesRegex(pin.PinError, 'not a regular file'):
            pin.check()


class Conformance(Consumer):
    def script(self, rel, text):
        path = self.root / '.claude/skills' / rel
        path.parent.mkdir(parents=True, exist_ok=True)
        path.write_text(text)

    def test_a_conforming_consumer_has_no_problems_and_test_refuses_any(self):
        self.config(extra_engines=[EXTRA])
        self.script('hil-contract/scripts/hil_workflow.py',
                    f'import agentrc_pin as pin\nfrom agentrc_pin import load as get\n'
                    f'pin.load("{EXTRA}")\nget({BUILD!r})\nload(name)   # a local load()\n')
        self.assertEqual(self.pin.conformance(), [])
        with mock.patch.object(self.pin, 'conformance', return_value=['one', 'two']), \
                mock.patch.object(self.pin.subprocess, 'run', side_effect=AssertionError('tests ran')):
            with self.assertRaisesRegex(self.pin.PinError, 'does not conform: one; two'):
                self.pin.test()

    def test_contract_files_must_carry_the_bootstrap_and_its_configuration(self):
        self.profile((PROFILE, CONSUMER))
        with mock.patch.object(self.pin.load(CONFORMANCE), 'round_trip', side_effect=AssertionError('staged')):
            problems = self.pin.conformance()
        self.assertEqual(problems, [f"{PROFILE} CONTRACT_FILES lacks ['{CONFIG}']: a staged tree would not carry them"])
        for text, why in (('CONTRACT_FILES = BASE + ("x",)\n', 'no CONTRACT_FILES literal'),
                          ('CONTRACT_FILES = (1,)\n', 'no CONTRACT_FILES literal'),
                          ('FILES = ()\n', 'no CONTRACT_FILES literal'),
                          ('CONTRACT_FILES = (\n', 'unreadable')):
            with self.subTest(text=text):
                (self.root / PROFILE).write_text(text)
                with self.assertRaisesRegex(self.pin.PinError, why):
                    self.pin.conformance()
        (self.root / PROFILE).unlink()
        with self.assertRaisesRegex(self.pin.PinError, 'check_build.py is unreadable'):
            self.pin.conformance()

    def test_every_load_target_is_a_literal_in_the_closure(self):
        hil = '.claude/skills/hil-contract/scripts'
        self.script('hil-contract/scripts/hil_ci.py', f'import agentrc_pin\n\n\ndef ci():\n'
                                                      f'    return agentrc_pin.load("{CI_ENGINE}")\n')
        self.script('hil-contract/scripts/dynamic.py',
                    'import agentrc_pin as pin\nfrom agentrc_pin import load\n'
                    'pin.load(f"skills/{name}.py")\nload(rel="x")\nload("README.md")\n')
        self.script('hil-contract/scripts/broken.py', 'agentrc_pin.load(\n')
        conform = self.pin.load(CONFORMANCE)
        self.assertEqual([t for _, t in conform._load_calls(ROOT / CANONICAL, own=True)], [CONFORMANCE])
        bootstrap = (self.root / CONSUMER).read_text()
        (self.root / CONSUMER).write_text(bootstrap + '\n\ndef extra(name):\n    return load(name)\n')
        with mock.patch.object(conform, 'round_trip', return_value=[]):
            problems = self.pin.conformance()
        (self.root / CONSUMER).write_text(bootstrap)
        self.assertEqual(problems[1], f'{hil}/broken.py is unreadable ('
                                      f'{problems[1].split(" (", 1)[1]}')
        self.assertEqual(problems[:1] + problems[2:], [
            f'{CONSUMER}:{bootstrap.count(chr(10)) + 4}: agentrc_pin.load() takes one string literal, not a computed '
            'target',
            f'{hil}/dynamic.py:3: agentrc_pin.load() takes one string literal, not a computed target',
            f'{hil}/dynamic.py:4: agentrc_pin.load() takes one string literal, not a computed target',
            f"{hil}/dynamic.py:5: agentrc_pin.load('README.md') is outside the closure; add it to {CONFIG} "
            'extra_engines',
            f"{hil}/hil_ci.py:5: agentrc_pin.load('{CI_ENGINE}') is outside the closure; add it to {CONFIG} "
            'extra_engines'])
        (self.root / hil / 'broken.py').unlink()
        (self.root / hil / 'dynamic.py').unlink()
        self.config(extra_engines=[CI_ENGINE])
        self.pin._selection.cache_clear()
        self.assertEqual(self.pin.conformance(), [])

    def test_only_the_public_bootstrap_names_are_used(self):
        hil = '.claude/skills/hil-contract/scripts'
        self.script('hil-contract/scripts/public.py',
                    'import agentrc_pin as pin\nfrom agentrc_pin import PinError, config, identity, stage\n'
                    f'pin.load({BUILD!r}).run(pin.ci(), pin.config(), pin.identity(), pin.stage, pin.conformance)\n'
                    'path = pin.__file__\n')
        self.script('hil-contract/scripts/alias.py', 'import json, agentrc_pin as boot\nboot.ROOT = 1\n'
                                                     'boot.literals(boot.ROOT, "tools/build_all.py")\n')
        self.script('hil-contract/scripts/names.py', 'from agentrc_pin import load, _selection as sel, check\n')
        test = self.root / '.claude/test/test_agentrc_pin.py'
        test.parent.mkdir(parents=True, exist_ok=True)
        test.write_text('import agentrc_pin\n\n\nassert agentrc_pin._selection()[0].stage\n'
                        'raise agentrc_pin.PinError("x")\n')
        with mock.patch.object(self.pin.load(CONFORMANCE), 'round_trip', return_value=[]):
            self.assertEqual(self.pin.conformance(), [
                f'{hil}/alias.py:2: uses agentrc_pin.ROOT, internal to the bootstrap',
                f'{hil}/alias.py:3: uses agentrc_pin.ROOT, internal to the bootstrap',
                f'{hil}/alias.py:3: uses agentrc_pin.literals, internal to the bootstrap',
                f'{hil}/names.py:1: uses agentrc_pin._selection, internal to the bootstrap',
                f'{hil}/names.py:1: uses agentrc_pin.check, internal to the bootstrap',
                '.claude/test/test_agentrc_pin.py:4: uses agentrc_pin._selection, internal to the bootstrap'])

    def test_an_alias_counts_only_in_the_scope_its_import_binds(self):
        self.script('hil-contract/scripts/gpio.py',
                    'def setup():\n    import agentrc_pin as pin\n    return pin.load("x")\n\n\n'
                    'def find_gpio(pin):\n    return pin.number\n')
        self.script('hil-contract/scripts/outer.py',
                    'import agentrc_pin as boot\n\n\ndef f():\n    return boot._selection()\n\n\n'
                    'def g(boot):\n    return boot.x, [boot.y for boot in ()]\n')
        conform = self.pin.load(CONFORMANCE)
        hil = self.root / '.claude/skills/hil-contract/scripts'
        self.assertEqual(conform._internal_uses(hil / 'gpio.py'), [])
        self.assertEqual(conform._internal_uses(hil / 'outer.py'), [(5, '_selection')])
        self.script('hil-contract/scripts/bare.py',
                    'import agentrc_pin\n\n\ndef f(agentrc_pin):\n    return agentrc_pin.number\n\n\n'
                    'def g():\n    return agentrc_pin.ROOT\n')
        self.assertEqual(conform._internal_uses(hil / 'bare.py'), [(9, 'ROOT')])

    def test_the_round_trip_refuses_a_module_from_outside_the_staged_copy(self):
        outside = self.tmp / 'outside'
        outside.mkdir()
        (outside / 'leaky.py').write_text('VALUE = 1\n')
        (self.repo / EXTRA).write_text(f'import sys\nsys.path.insert(0, {str(outside)!r})\nimport leaky\n'
                                       f'FILES = ({LOADER!r}, {SOURCE!r})\n')
        git(self.repo, 'commit', '-qam', 'leak')
        self.config(commit=git(self.repo, 'rev-parse', 'HEAD'), extra_engines=[EXTRA])
        problems = self.pin.conformance()
        self.assertEqual(len(problems), 1, problems)
        self.assertRegex(problems[0], r'^a staged tree loads from outside its agentrc/: .*leaky\.py$')

    def test_the_round_trip_refuses_a_tree_that_does_not_load(self):
        real = self.pin.stage

        def corrupt(into, name='agentrc'):
            digests = real(into, name)
            (Path(into) / name / BUILD).write_text('raise AssertionError("ran corrupt engine")\n')
            return digests
        with mock.patch.object(self.pin, 'stage', corrupt):
            problems = self.pin.conformance()
        self.assertEqual(len(problems), 1, problems)
        self.assertRegex(problems[0], '^a staged tree does not load: .*differ from their manifest')

    def test_stage_is_the_real_stager_and_writes_a_tree_once(self):
        ident = self.pin.identity()
        digests = self.pin.stage(self.tmp / 'tree')
        self.assertEqual(set(digests), {f'agentrc/{f}' for f in (*ident['files'], 'manifest.json')})
        self.assertEqual(json.loads((self.tmp / 'tree/agentrc/manifest.json').read_text())['sha256'], ident['sha256'])
        with self.assertRaisesRegex(self.pin.PinError, 'written once') as cm:
            self.pin.stage(self.tmp / 'tree')
        self.assertIn('unchanged agentrc', cm.exception.remedy)
        self.assertEqual(set(self.pin.stage(self.tmp / 'tree', name='other')),
                         {f'other/{f}' for f in (*ident['files'], 'manifest.json')})

    @unittest.skipIf(os.name == 'nt', 'the real Arduino engines are Linux only (fcntl)')
    def test_the_real_engines_conform_from_this_checkout(self):
        self.env(AGENTRC_DIR=str(ROOT))
        self.config(features=['core', 'named', 'battery'], extra_engines=[EXTRA, CI_ENGINE])
        self.script('hil-contract/scripts/hil_test.py',
                    "import agentrc_pin\nE = agentrc_pin.load('skills/arduino/scripts/arduino_hil.py')\n")
        self.assertEqual(self.pin.identity()['root'], str(ROOT))
        self.assertEqual(self.pin.conformance(), [])


@unittest.skipIf(os.name == 'nt', 'the real Arduino engines are Linux only (fcntl)')
class BuildRunsThePin(Consumer):
    """arduino_build.run_tests() runs a consumer's `agentrc_pin` contract test through this bootstrap's `test`."""

    def setUp(self):
        super().setUp()
        self.ab = load_arduino_engine(BUILD)
        profile = types.SimpleNamespace(ROOT=self.root,
                                        CONTRACT_TESTS={'agentrc_pin': '.claude/test/test_agentrc_pin.py'})
        self.stack.enter_context(mock.patch.object(self.ab, 'P', profile))
        self.consumer_test('pass')

    def consumer_test(self, body):
        (self.root / '.claude/test').mkdir(parents=True, exist_ok=True)
        (self.root / '.claude/test/test_agentrc_pin.py').write_text(
            f'import unittest\nclass Pin(unittest.TestCase):\n    def test_it(self):\n        {body}\n')

    def run_pin(self):
        [row] = self.ab.run_tests(['agentrc_pin'])
        return row['status'], row['firstError']

    def test_a_conforming_consumer_passes_and_its_test_failures_propagate(self):
        self.assertEqual(self.run_pin(), ('ok', ''))
        self.consumer_test('self.fail("consumer")')
        self.assertEqual(self.run_pin(), ('failed', 'FAILED (failures=1)'))

    def test_contract_files_without_the_configuration_fail(self):
        self.profile((PROFILE, CONSUMER))
        status, error = self.run_pin()
        self.assertEqual(status, 'failed')
        self.assertIn(f"CONTRACT_FILES lacks ['{CONFIG}']", error)

    def test_a_drifted_bootstrap_copy_fails(self):
        (self.root / CONSUMER).write_bytes((self.root / CONSUMER).read_bytes() + b'\n')
        status, error = self.run_pin()
        self.assertEqual(status, 'failed')
        self.assertIn(f'bootstrap copy {self.root / CONSUMER} differs', error)


@unittest.skipIf(os.name == 'nt', 'the real Arduino engines are Linux only (fcntl)')
class RealPack(unittest.TestCase):
    def test_new_and_origin_bootstraps_load_the_real_packed_closure(self):
        tmp = tmpdir(self)
        R = load_arduino_engine('skills/arduino/scripts/arduino_remote.py')
        fake = load_arduino_fixture()
        fixture = fake.clean_checkout(tmp, git, '/build/\n')
        fixture.BP.CONTRACT_FILES = (*fixture.BP.CONTRACT_FILES, CONSUMER, CONFIG)
        for name in ('canonical', 'tua'):
            with self.subTest(bootstrap=name):
                consumer = fixture.root / CONSUMER
                consumer.parent.mkdir(parents=True, exist_ok=True)
                shutil.copyfile(ROOT / CANONICAL if name == 'canonical' else
                                ROOT / 'tests/fixtures/arduino_pin' / f'{name}.py', consumer)
                config = fixture.root / CONFIG
                config.parent.mkdir(parents=True, exist_ok=True)
                extra = [EXTRA, CI_ENGINE] if name == 'canonical' else [EXTRA]
                config.write_text(json.dumps({'url': str(ROOT), 'commit': 'a' * 40, 'cache_namespace': 'fixture',
                                               'profile_api': 1, 'features': ['core', 'named', 'battery'],
                                               'extra_engines': extra}))
                git(fixture.root, 'add', '.')
                git(fixture.root, 'commit', '-qm', f'bootstrap {name}')
                head = git(fixture.root, 'rev-parse', 'HEAD')
                engines = R.B.agentrc_source.literal(fixture.root, CONSUMER, 'ENGINES')
                scripts = [*engines, *([CANONICAL, *extra] if name == 'canonical' else [])]
                ident = R.B.agentrc_source.identity('installed', ROOT, scripts)
                fixture.BP.AGENTRC = ident
                tree = tmp / f'packed-{name}'
                with mock.patch.object(R, 'P', fixture.HP), mock.patch.object(R.H, 'P', fixture.HP), \
                        mock.patch.object(R.B, 'P', fixture.BP):
                    record = fake.hil_record(fixture.root, head, R.B.contract_identity(), 'rig_ocd')
                    R.pack(tree, fake.REMOTE_ROSTER, [R.record_files(record, head, 'rig_ocd')], head)
                manifest = json.loads((tree / 'manifest.json').read_text())
                self.assertEqual(manifest['files']['agentrc/manifest.json'],
                                 R.B.sha256(tree / 'agentrc/manifest.json'))
                code = '''if True:
                    import json, sys
                    from pathlib import Path
                    sys.path.insert(0, str(Path(sys.argv[1]).parent))
                    import agentrc_pin as pin
                    ident = pin.identity()
                    scripts = [*pin.ENGINES, *(pin.config()['extra_engines'] if hasattr(pin, 'config') else [])]
                    engines = [pin.load(e).__file__ for e in scripts]
                    print(json.dumps({'source': ident['source'], 'commit': ident['commit'], 'files': ident['files'],
                                      'sha256': ident['sha256'], 'engines': engines}))
                '''
                p = subprocess.run([sys.executable, '-I', '-B', '-c', code, str(tree / CONSUMER)],
                                   capture_output=True, text=True, env={**CLEAN_ENV, 'CI': 'true',
                                                                     'AGENTRC_DIR': str(tmp / 'absent')})
                self.assertEqual(p.returncode, 0, p.stdout + p.stderr)
                result = json.loads(p.stdout)
                self.assertEqual((result['source'], result['commit']), ('staged', ident['commit']))
                self.assertEqual(result['sha256'], ident['sha256'])
                self.assertTrue(all(Path(f).is_relative_to(tree / 'agentrc') for f in result['engines']))


if __name__ == '__main__':
    unittest.main()
