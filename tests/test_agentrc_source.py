"""skills/hil/scripts/agentrc_source.py: the identity of the agentrc a consumer loads (installed behind skill
symlinks, a pin checkout, a staged copy), its closure, drift, staging and the record comparison."""
import importlib.util
import json
import subprocess
import sys
import tempfile
import unittest
from pathlib import Path
sys.path.insert(0, str(Path(__file__).resolve().parent))
from helpers import git_env, guard_survivors  # noqa: E402  importing it drops a caller's GIT_* before any git runs

ROOT = Path(__file__).resolve().parents[1]
spec = importlib.util.spec_from_file_location('agentrc_source_under_test', ROOT / 'skills/hil/scripts/agentrc_source.py')
src = importlib.util.module_from_spec(spec)
spec.loader.exec_module(src)


def setUpModule():
    guard_survivors(__name__)


SCRIPT = 'skills/hil/scripts/tool.py'
OTHER = 'skills/rtt/scripts/other.py'


def git(root, *args):
    subprocess.run(['git', '-C', str(root), *args], check=True, capture_output=True, env=git_env())
    return subprocess.run(['git', '-C', str(root), 'rev-parse', 'HEAD'], capture_output=True, text=True,
                          env=git_env()).stdout.strip()


def checkout(at):
    """A git checkout shaped like agentrc: this file, hil_load, a script whose FILES name a lib/ module and a data
    file, an rtt script, and a README outside every closure; its HEAD."""
    files = {src.SELF: (ROOT / src.SELF).read_text(), src.LOADER: '# loader\n',
             SCRIPT: "FILES = ('lib/windows.py', 'skills/hil/boards.json')\n", 'lib/windows.py': '# win\n',
             'skills/hil/boards.json': '{}\n', OTHER: 'FILES = ()\n', 'README.md': 'readme\n'}
    for rel, text in files.items():
        (at / rel).parent.mkdir(parents=True, exist_ok=True)
        (at / rel).write_text(text)
    git(at, 'init', '-q')
    git(at, 'add', '.')
    return git(at, 'commit', '-qm', 'c')


class Base(unittest.TestCase):
    def setUp(self):
        d = tempfile.TemporaryDirectory()
        self.addCleanup(d.cleanup)
        self.tmp = Path(d.name).resolve()
        self.co = self.tmp / 'agentrc'
        self.co.mkdir()
        self.head = checkout(self.co)
        # an install: skill directories linked into the checkout, no lib/
        self.inst = self.tmp / 'claude'
        (self.inst / 'skills').mkdir(parents=True)
        for skill in ('hil', 'rtt'):
            (self.inst / 'skills' / skill).symlink_to(self.co / 'skills' / skill)
        src._FROZEN.clear()
        self.addCleanup(src._FROZEN.clear)


class Installed(Base):
    def test_identity_resolves_the_checkout_behind_the_install(self):
        ident = src.identity('installed', self.inst, [SCRIPT])
        self.assertEqual(ident['root'], str(self.co))
        self.assertEqual(ident['commit'], self.head)
        self.assertFalse(ident['dirty'])
        self.assertEqual(ident['files'], sorted([src.SELF, src.LOADER, SCRIPT, 'lib/windows.py',
                                                 'skills/hil/boards.json']))
        self.assertEqual(ident['sha256'][SCRIPT], src.sha256(self.co / SCRIPT))

    def test_dirty_counts_only_the_closure(self):
        (self.co / 'README.md').write_text('changed\n')
        self.assertFalse(src.identity('installed', self.inst, [SCRIPT])['dirty'])
        (self.co / 'lib/windows.py').write_text('# changed\n')
        self.assertTrue(src.identity('installed', self.inst, [SCRIPT])['dirty'])

    def test_a_mix_of_installs_is_refused(self):
        other = self.tmp / 'other'
        other.mkdir()
        checkout(other)
        (self.inst / 'skills/rtt').unlink()
        (self.inst / 'skills/rtt').symlink_to(other / 'skills/rtt')
        with self.assertRaisesRegex(src.SourceError, 'resolve outside'):
            src.identity('installed', self.inst, [SCRIPT, OTHER])

    def test_no_install(self):
        with self.assertRaisesRegex(src.SourceError, 'no agentrc is installed'):
            src.identity('installed', self.tmp / 'nothing', [SCRIPT])

    def test_a_missing_closure_file(self):
        (self.co / 'skills/hil/boards.json').unlink()
        with self.assertRaisesRegex(src.SourceError, 'lacks skills/hil/boards.json'):
            src.identity('installed', self.inst, [SCRIPT])

    def test_frozen_keeps_the_first_identity(self):
        first = src.frozen('installed', self.inst, [SCRIPT])
        (self.co / SCRIPT).write_text("FILES = ('lib/windows.py', 'skills/hil/boards.json')\n# edited\n")
        self.assertIs(src.frozen('installed', self.inst, [SCRIPT]), first)
        self.assertEqual(src.drift(first), [SCRIPT])


class Pin(Base):
    def test_clean_checkout_at_the_pin(self):
        ident = src.identity('pin', self.co, [SCRIPT], self.head)
        self.assertEqual((ident['commit'], ident['dirty'], ident['source']), (self.head, False, 'pin'))

    def test_moved_or_modified_pin_refused(self):
        with self.assertRaisesRegex(src.SourceError, 'not the pin'):
            src.identity('pin', self.co, [SCRIPT], '0' * 40)
        (self.co / 'README.md').write_text('changed\n')
        with self.assertRaisesRegex(src.SourceError, 'local changes'):
            src.identity('pin', self.co, [SCRIPT], self.head)


class Staged(Base):
    def stage(self, name='tree'):
        ident = src.identity('installed', self.inst, [SCRIPT])
        tree = self.tmp / name
        tree.mkdir()
        digests = src.stage(ident, tree)
        return ident, tree / src.STAGED, digests

    def test_round_trip_is_the_same_portable_identity(self):
        ident, staged, digests = self.stage()
        got = src.identity('staged', staged, [SCRIPT])
        self.assertEqual((got['source'], got['root']), ('staged', str(staged)))
        self.assertEqual(src.portable(got), src.portable(ident))
        self.assertEqual(src.mismatch(src.portable(ident), got), '')
        self.assertEqual(set(digests), {f'{src.STAGED}/{f}' for f in ident['files']} | {f'{src.STAGED}/manifest.json'})
        self.assertFalse(any((staged / f).is_symlink() for f in ident['files']))

    def test_stage_refuses_a_file_edited_since_load(self):
        ident = src.identity('installed', self.inst, [SCRIPT])
        (self.co / 'lib/windows.py').write_text('# newer\n')
        tree = self.tmp / 'tree'
        tree.mkdir()
        with self.assertRaisesRegex(src.SourceError, 'lib/windows.py changed since'):
            src.stage(ident, tree)

    def test_stage_writes_once(self):
        ident, staged, _ = self.stage()
        with self.assertRaisesRegex(src.SourceError, 'written once'):
            src.stage(ident, staged.parent)

    def test_every_gap_refused(self):
        def manifest(edit, name):
            ident, staged, _ = self.stage(name)
            m = json.loads((staged / 'manifest.json').read_text())
            raw = edit(m, staged)
            (staged / 'manifest.json').write_text(raw if isinstance(raw, str) else json.dumps(m))
            return staged
        cases = {
            'not readable JSON': lambda m, s: '{',
            'names no commit': lambda m, s: m.pop('dirty'),
            'unlike the closure': lambda m, s: m['sha256'].__delitem__(src.LOADER),
            'not a regular file': lambda m, s: (s / 'lib/windows.py').unlink(),
            'differ from their manifest': lambda m, s: (s / 'lib/windows.py').write_text('# tampered\n'),
        }
        for n, (why, edit) in enumerate(cases.items()):
            with self.subTest(why):
                staged = manifest(edit, f'tree{n}')
                with self.assertRaisesRegex(src.SourceError, why):
                    src.identity('staged', staged, [SCRIPT])

    def test_a_symlinked_directory_refused(self):
        _, staged, _ = self.stage()
        outside = self.tmp / 'outside'
        (staged / 'lib').rename(outside)
        (staged / 'lib').symlink_to(outside)
        with self.assertRaisesRegex(src.SourceError, 'plain directory'):
            src.identity('staged', staged, [SCRIPT])

    def test_corrupt_staged_python_is_a_source_error(self):
        _, staged, _ = self.stage()
        (staged / SCRIPT).write_text('FILES = (\n')
        with self.assertRaisesRegex(src.SourceError, 'differ from their manifest'):
            src.identity('staged', staged, [SCRIPT])

    def test_files_outside_the_root_refused(self):
        (self.co / SCRIPT).write_text("FILES = ('../outside.txt',)\n")
        (self.tmp / 'outside.txt').write_text('x\n')
        with self.assertRaisesRegex(src.SourceError, 'outside itself'):
            src.closure(self.co, [SCRIPT])
        for bad in ('/abs.txt', 'C:/outside.txt', 'D:outside.txt', 'a\\\\b.txt'):
            with self.subTest(bad):
                (self.co / SCRIPT).write_text(f'FILES = ({bad!r},)\n')
                with self.assertRaisesRegex(src.SourceError, 'outside itself'):
                    src.closure(self.co, [SCRIPT])
        (self.co / SCRIPT).write_text('FILES = (\n')
        with self.assertRaisesRegex(src.SourceError, 'unreadable'):
            src.closure(self.co, [SCRIPT])

    def test_a_symlink_or_unsafe_path_refused(self):
        _, staged, _ = self.stage()
        (staged / 'lib/windows.py').unlink()
        (staged / 'lib/windows.py').symlink_to(self.co / 'lib/windows.py')
        with self.assertRaisesRegex(src.SourceError, 'not a regular file'):
            src.identity('staged', staged, [SCRIPT])
        m = json.loads((staged / 'manifest.json').read_text())
        m['sha256']['../escape'] = '0'
        (staged / 'manifest.json').write_text(json.dumps(m))
        with self.assertRaisesRegex(src.SourceError, 'names no commit'):
            src.identity('staged', staged, [SCRIPT])


class Records(Base):
    def test_mismatch(self):
        ident = src.identity('installed', self.inst, [SCRIPT])
        self.assertEqual(src.mismatch(None, ident), 'it binds no agentrc identity')
        other = src.portable(ident)
        other['sha256'] = {**other['sha256'], SCRIPT: '0' * 64}
        self.assertIn(f'files differ: {SCRIPT}', src.mismatch(other, ident))

    def test_binding_require_and_mismatch_refuse_drift_since_load(self):
        ident = src.identity('installed', self.inst, [SCRIPT])
        self.assertEqual(src.binding(ident), src.portable(ident))
        self.assertEqual(src.require(ident, SCRIPT), self.co / SCRIPT)
        with self.assertRaisesRegex(src.SourceError, 'not in the agentrc closure'):
            src.require(ident, OTHER)
        (self.co / 'lib/windows.py').write_text('# newer\n')   # a dependency, not the file asked for
        why = 'agentrc lib/windows.py changed since this process loaded it'
        for call in (lambda: src.binding(ident), lambda: src.require(ident, SCRIPT)):
            with self.assertRaisesRegex(src.SourceError, why):
                call()
        self.assertEqual(src.mismatch(src.portable(ident), ident), why)


class Real(unittest.TestCase):
    def test_this_checkout_closes_a_consumer_closure(self):
        files = src.closure(ROOT, ['skills/hil/scripts/hil_boards.py', 'skills/hil/scripts/hil_flash.py',
                                   'skills/code-size/scripts/code_size.py',
                                   'skills/rtt/scripts/rtt.py'])
        for f in ('lib/windows.py', 'skills/hil/boards.json', src.SELF, src.LOADER):
            self.assertIn(f, files)
        self.assertEqual(src.literal(ROOT, src.SELF, 'API'), src.API)
        self.assertEqual(src.literal(ROOT, 'skills/code-size/scripts/code_size.py', 'PROFILE_API'), 1)


if __name__ == '__main__':
    unittest.main()
