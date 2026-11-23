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
import subprocess
import sys
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
BUILD = 'skills/arduino/scripts/arduino_build.py'
CI_ENGINE = 'skills/arduino/scripts/arduino_ci.py'
EXTRA = 'skills/hil/scripts/gh_pr_comment.py'
CONFIG = 'test/hil/agentrc.json'
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
        for rel in (CANONICAL, SOURCE, LOADER):
            (self.repo / rel).parent.mkdir(parents=True, exist_ok=True)
            shutil.copyfile(ROOT / rel, self.repo / rel)
        for rel in (*self.pin.ENGINES, EXTRA, CI_ENGINE):
            (self.repo / rel).write_text(f'FILES = ({LOADER!r}, {SOURCE!r}, {CANONICAL!r}, "lib/data.py")\n'
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
        self.install = self.tmp / 'home/.claude'
        (self.install / 'skills').mkdir(parents=True)
        for skill in ('arduino', 'hil'):
            (self.install / 'skills' / skill).symlink_to(self.repo / 'skills' / skill, target_is_directory=True)
        self.env(HOME=str(self.tmp / 'home'), XDG_CACHE_HOME=str(self.tmp / 'cache'))

    def env(self, **kw):
        self.stack.enter_context(mock.patch.dict(os.environ, kw))

    def config(self, **kw):
        self.data.update(kw)
        (self.root / CONFIG).parent.mkdir(parents=True, exist_ok=True)
        (self.root / CONFIG).write_text(json.dumps(self.data))

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
        for rel in (CONSUMER, CONFIG):
            (tree / rel).parent.mkdir(parents=True, exist_ok=True)
            shutil.copyfile(self.root / rel, tree / rel)
            digests[rel] = helper.sha256(tree / rel)
        (tree / 'manifest.json').write_text(json.dumps({'agentrc': {'commit': ident['commit'],
                                                                   'dirty': ident['dirty']}, 'files': digests}))
        unload()
        return tree, load(tree / CONSUMER), ident


class Configuration(Consumer):
    def test_explicit_fields_and_safe_namespace_and_paths(self):
        self.assertEqual(self.pin.config(), self.data)
        self.assertEqual(self.pin.pin(), (str(self.repo), self.head))
        self.assertEqual(self.pin.cache_dir(self.head), self.tmp / 'cache/fixture' / f'agentrc-{self.head}')
        bad = {'url': [None, 0, '', '  '], 'commit': [None, 1, 'abc', 'g' * 40],
               'cache_namespace': [None, 1, '', '.', '..', '../x', '/x', 'C:x', 'x/y', 'x\\y'],
               'profile_api': [None, True, 0, -1, '1', 1.0],
               'features': [None, 'core', {}, [1], [''], [' ']],
               'extra_engines': [None, 'file.py', {}, [1], [''], ['.'], ['..'], ['../file.py'],
                                 ['/file.py'], ['a/../file.py'], ['C:/x'], ['D:x'], ['a\\b'], ['a//b'], ['a\0b']]}
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
                with self.assertRaisesRegex(self.pin.PinError, re.escape(CONFIG)):
                    self.pin.config()
        (self.root / CONFIG).unlink()
        with self.assertRaisesRegex(self.pin.PinError, 'not readable JSON'):
            self.pin.config()

    def test_invalid_configuration_refused_locally_before_discovery(self):
        self.config(features='core')
        with mock.patch.object(self.pin, 'loader', side_effect=AssertionError('executed')):
            with self.assertRaisesRegex(self.pin.PinError, 'features'):
                self.pin.identity()
        self.config(features=[])
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
    def test_identity_uses_the_checkout_behind_skill_links_and_extra_engines(self):
        self.config(extra_engines=[EXTRA])
        ident = self.pin.identity()
        self.assertEqual((ident['source'], ident['root'], ident['commit'], ident['dirty']),
                         ('installed', str(self.repo), self.head, False))
        self.assertEqual(ident['files'], sorted([*self.pin.ENGINES, SOURCE, LOADER, CANONICAL, EXTRA, 'lib/data.py']))
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
            for call in (lambda: self.pin.load(BUILD), self.pin.check, self.pin.test):
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

    def test_symlinked_inner_manifest_is_refused(self):
        tree, pin, _ = self.stage()
        manifest = tree / 'agentrc/manifest.json'
        outside = self.tmp / 'manifest.json'
        manifest.rename(outside)
        manifest.symlink_to(outside)
        with self.assertRaisesRegex(pin.PinError, 'not a regular file'):
            pin.check()


@unittest.skipIf(os.name == 'nt', 'the real Arduino engines are Linux only (fcntl)')
class RealPack(unittest.TestCase):
    def test_new_and_origin_bootstraps_load_the_real_packed_closure(self):
        tmp = tmpdir(self)
        R = load_arduino_engine('skills/arduino/scripts/arduino_remote.py')
        fake = load_arduino_fixture()
        fixture = fake.clean_checkout(tmp, git, '/build/\n')
        fixture.BP.CONTRACT_FILES = (*fixture.BP.CONTRACT_FILES, CONSUMER, CONFIG)
        for name in ('canonical', 'tua', 'samd'):
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
                ident = R.B.agentrc_source.identity('installed', ROOT, [BUILD,
                    'skills/arduino/scripts/arduino_hil.py', 'skills/arduino/scripts/arduino_remote.py', *extra])
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
                expected = dict(ident['sha256'])
                if name == 'samd':
                    expected.pop(EXTRA)
                self.assertEqual(result['sha256'], expected)
                self.assertTrue(all(Path(f).is_relative_to(tree / 'agentrc') for f in result['engines']))


if __name__ == '__main__':
    unittest.main()
