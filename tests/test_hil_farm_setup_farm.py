import os
import unittest

if os.name == 'nt':
    raise unittest.SkipTest('the HIL farm setup runs on Debian: bash, fcntl, Linux paths')

import copy
import re
import tomllib
import hashlib
import io
import json
import shutil
import subprocess
import sys
import tarfile
import tempfile
from pathlib import Path
from unittest import mock

sys.path.insert(0, str(Path(__file__).resolve().parent))
from helpers import guard_survivors  # noqa: E402

sys.path.insert(0, str(Path(__file__).resolve().parent.parent / 'skills' / 'hil-farm-setup' / 'scripts'))
import farm  # noqa: E402

HOSTS = Path(__file__).resolve().parent / 'fixtures' / 'hil_farm_setup'


def sha(data):
    return hashlib.sha256(data).hexdigest()


def tar_bytes(members):
    """members: {name: (bytes, mode)} or {name: ('->', target)} for a symlink."""
    buf = io.BytesIO()
    with tarfile.open(fileobj=buf, mode='w:gz') as tf:
        for name, (data, mode) in members.items():
            ti = tarfile.TarInfo(name)
            if data == '->':
                ti.type, ti.linkname = tarfile.SYMTYPE, mode
                tf.addfile(ti)
            else:
                ti.size, ti.mode = len(data), mode
                tf.addfile(ti, io.BytesIO(data))
    return buf.getvalue()


def tool_script(version):
    return f'#!/bin/sh\necho "tool {version}"\n'.encode()


class FarmCase(unittest.TestCase):
    def setUp(self):
        self.addCleanup(os.umask, os.umask(0o022))  # root's under sudo; a 0775 dir would fail verify's path check
        self.tmp = Path(tempfile.mkdtemp())
        self.bin = self.tmp / 'usr/local/bin'
        self.bin.mkdir(parents=True)
        self.remote = {}  # url -> bytes
        patches = [
            mock.patch.object(farm, 'TOOLS_ROOT', self.tmp / 'opt/hil-farm/tools'),
            mock.patch.object(farm, 'RECEIPTS', self.tmp / 'var/lib/hil-farm'),
            mock.patch.object(farm, 'LINK_ROOTS', (str(self.bin) + '/',)),
            mock.patch.object(farm, 'FIRMWARE_ROOT', str(self.tmp / 'lib/firmware') + '/'),
            mock.patch.object(farm, 'HERE', self.tmp / 'checkout'),
            mock.patch.object(farm, 'fetch', self.fake_fetch),
            mock.patch.object(farm, 'TRUSTED_UIDS', {0, os.getuid()}),
        ]
        for p in patches:
            p.start()
            self.addCleanup(p.stop)
        (self.tmp / 'checkout').mkdir()
        self.addCleanup(shutil.rmtree, self.tmp)

    def inputs(self):
        d = self.tmp / 'inputs'
        d.mkdir(exist_ok=True)
        p = mock.patch.object(farm, 'INPUTS', str(d))
        p.start()
        self.addCleanup(p.stop)
        return d

    def fake_fetch(self, url, sha256, dest, form=None):
        data = self.remote[url]
        if sha(data) != sha256:
            raise farm.FarmError(f'sha256 mismatch for {url}')
        Path(dest).write_bytes(data)

    def firmware(self, blob):
        (self.tmp / 'checkout/fw.mem').write_bytes(blob)
        dest = self.tmp / 'lib/firmware/fw.mem'
        dest.parent.mkdir(parents=True)
        return {'kind': 'firmware', 'file': 'fw.mem', 'sha256': sha(blob), 'version': '1', 'dest': str(dest)}, dest

    def release(self, version, url='https://example.test/t.tgz', **extra):
        data = tar_bytes({'t/bin/tool': (tool_script(version), 0o755)})
        self.remote[url] = data
        t = {'kind': 'release', 'url': url, 'sha256': sha(data), 'version': version,
             'links': {str(self.bin / 'tool'): 't/bin/tool'},
             'check': [str(self.bin / 'tool')]}
        t.update(extra)
        return t


class TestValidate(unittest.TestCase):
    def test_repo_config_is_valid(self):
        cfg = farm.load()
        self.assertIn('openocd', cfg['tool'])

    def bad(self, **change):
        t = {'kind': 'release', 'url': 'https://x/y.tgz', 'sha256': 'a' * 64, 'version': '1.0',
             'links': {'/usr/local/bin/y': 'y'}, 'check': ['y']}
        t.update(change)
        t = {k: v for k, v in t.items() if v is not None}
        return farm.validate_tool('y', t)

    def test_good_tool(self):
        self.assertEqual(self.bad(), [])

    def test_rejects(self):
        cases = {
            'unknown field': dict(extra=1),
            'missing': dict(sha256=None),
            'sha256': dict(sha256='abc'),
            'https': dict(url='http://x/y.tgz'),
            'unsafe': dict(version='1 0'),
            'expect without check': dict(check=None, expect='1.0'),
            'outside': dict(links={'/etc/y': 'y'}),
            'escapes': dict(links={'/usr/local/bin/y': '../y'}),
            'unknown kind': dict(kind='curl'),
        }
        for want, change in cases.items():
            with self.subTest(want):
                errs = self.bad(**change)
                self.assertTrue(any(want in e for e in errs), errs)

    def test_git_rules(self):
        t = {'kind': 'git', 'recipe': 'copy', 'repo': 'https://x/r.git', 'commit': 'abc', 'version': '1',
             'files': ['a/x.sh', 'b/x.sh']}
        errs = farm.validate_tool('g', t)
        self.assertTrue(any('40-digit' in e for e in errs))
        self.assertTrue(any('basename' in e for e in errs))
        t.update(commit='a' * 40, recipe='cmake', files=['x'])
        self.assertTrue(any('recipe' in e for e in farm.validate_tool('g', t)))

    def test_unknown_top_level(self):
        with self.assertRaises(farm.FarmError):
            farm.validate({'tool': {}, 'extra': {}})



class TestInputHash(unittest.TestCase):
    def test_per_tool(self):
        a = {'kind': 'release', 'version': '1'}
        b = copy.deepcopy(a)
        self.assertEqual(farm.input_hash('x', a), farm.input_hash('x', b))
        b['version'] = '2'
        self.assertNotEqual(farm.input_hash('x', a), farm.input_hash('x', b))
        self.assertNotEqual(farm.input_hash('x', a), farm.input_hash('y', a))


class TestInstall(FarmCase):
    def test_a_plain_file_at_a_link_is_refused(self):
        (self.bin / 'tool').write_bytes(b'owner')
        with self.assertRaisesRegex(farm.FarmError, 'exists and is not a link'):
            farm.install_tool('t', self.release('1.0'), False)
        self.assertEqual((self.bin / 'tool').read_bytes(), b'owner')

    def test_install_verify_skip_bump(self):
        t1 = self.release('1.0')
        farm.install_tool('t', t1, False)
        self.assertEqual(farm.verify_tool('t', t1), [])
        dir1 = Path(farm.read_receipt('t')['dir'])
        self.assertEqual(Path(os.readlink(self.bin / 'tool')), dir1 / 't/bin/tool')
        with mock.patch.object(farm, 'build', side_effect=AssertionError('rebuilt')):
            farm.install_tool('t', t1, False)  # verify passes: skipped
        t2 = self.release('2.0')
        self.assertTrue(farm.verify_tool('t', t2))
        farm.install_tool('t', t2, False)
        dir2 = Path(farm.read_receipt('t')['dir'])
        self.assertNotEqual(dir1, dir2)
        self.assertEqual(Path(os.readlink(self.bin / 'tool')), dir2 / 't/bin/tool')
        self.assertTrue(dir1.exists(), 'old dirs are kept')

    def test_failed_check_keeps_old_links(self):
        t1 = self.release('1.0')
        farm.install_tool('t', t1, False)
        bad = self.release('2.0', url='https://example.test/b.tgz', expect='9.9')
        with self.assertRaises(farm.FarmError):
            farm.install_tool('t', bad, False)
        self.assertEqual(farm.verify_tool('t', t1), [])

    def test_hash_mismatch(self):
        t = self.release('1.0')
        t['sha256'] = 'b' * 64
        with self.assertRaises(farm.FarmError):
            farm.install_tool('t', t, False)
        self.assertFalse((self.bin / 'tool').exists())

    def test_tar_traversal(self):
        url = 'https://example.test/evil.tgz'
        self.remote[url] = tar_bytes({'../evil': (b'x', 0o644)})
        t = {'kind': 'release', 'url': url, 'sha256': sha(self.remote[url]), 'version': '1'}
        with self.assertRaises(farm.FarmError):
            farm.install_tool('t', t, False)
        self.assertFalse((self.tmp / 'opt/hil-farm/tools/t/evil').exists())

    def test_link_target_escaping_via_symlink(self):
        url = 'https://example.test/l.tgz'
        self.remote[url] = tar_bytes({'t/real': (b'x', 0o755), 't/esc': ('->', '../../../../../../etc/passwd')})
        t = {'kind': 'release', 'url': url, 'sha256': sha(self.remote[url]), 'version': '1',
             'links': {str(self.bin / 'esc'): 't/esc'}}
        with self.assertRaises(farm.FarmError):
            farm.install_tool('t', t, False)

    def test_glob_links_and_stale_removal(self):
        def rel(names, url):
            data = tar_bytes({f'g/bin/{n}': (tool_script('1'), 0o755) for n in names})
            self.remote[url] = data
            return {'kind': 'release', 'url': url, 'sha256': sha(data), 'version': str(len(names)),
                    'links': {str(self.bin) + '/': 'g/bin/*'}}
        farm.install_tool('g', rel(['a', 'b'], 'https://example.test/g1'), False)
        self.assertTrue((self.bin / 'b').is_symlink())
        farm.install_tool('g', rel(['a'], 'https://example.test/g2'), False)
        self.assertTrue((self.bin / 'a').is_symlink())
        self.assertFalse((self.bin / 'b').exists(), 'stale link removed')
        with self.assertRaises(farm.FarmError):
            farm.resolve_links({'links': {str(self.bin) + '/': 'none/*'}}, self.tmp)

    def test_refuses_foreign_paths(self):
        (self.bin / 'tool').write_text('real file')
        with self.assertRaises(farm.FarmError):
            farm.install_tool('t', self.release('1.0'), False)
        (self.bin / 'tool').unlink()
        farm.install_tool('t', self.release('1.0'), False)
        other = self.release('1.0', url='https://example.test/o.tgz')
        with self.assertRaises(farm.FarmError):
            farm.install_tool('other', other, False)

    def test_local_and_source(self):
        data = tar_bytes({'L/bin/tool': (tool_script('3.1'), 0o755)})
        (self.inputs() / 'l.tar').write_bytes(data)
        t = {'kind': 'local', 'file': 'l.tar', 'sha256': sha(data), 'version': '3.1',
             'links': {str(self.bin / 'tool'): 'L/bin/tool'}, 'check': [str(self.bin / 'tool')]}
        farm.install_tool('l', t, False)
        with mock.patch.object(farm, 'INPUTS', None):
            self.assertEqual(farm.verify_tool('l', t), [])   # verify and an unchanged update need no input
            farm.install_tool('l', t, False)
        if not shutil.which('cc'):
            self.skipTest('no cc')
        src = b'#include <stdio.h>\nint main(void){puts("hello 7");return 0;}\n'
        self.remote['https://example.test/h.c?h=v1'] = src
        s = {'kind': 'source', 'url': 'https://example.test/h.c?h=v1', 'sha256': sha(src),
             'version': '7', 'cflags': ['-O2'], 'links': {str(self.bin / 'hello'): 'hello'},
             'check': [str(self.bin / 'hello')]}
        farm.install_tool('hello', s, False)
        self.assertEqual(farm.verify_tool('hello', s), [])

    def test_firmware_written_only_when_changed(self):
        blob = b'\x01' * 64
        t, dest = self.firmware(blob)
        calls = []
        with mock.patch.object(farm, 'run', side_effect=lambda a, **k: calls.append(a) or ''):
            farm.install_tool('fw', t, False)
            self.assertEqual(dest.read_bytes(), blob)
            self.assertEqual(calls, [['update-initramfs', '-u', '-k', 'all']])
            farm.install_tool('fw', t, True)
            self.assertEqual(len(calls), 1, 'unchanged firmware is not rewritten')
        self.assertEqual(oct(dest.stat().st_mode & 0o777), '0o644')


class TestRegressions(FarmCase):
    """Cases from the engine review: each one failed before its fix."""

    def test_forced_reinstall_never_breaks_the_live_tool(self):
        t = self.release('1.0')
        farm.install_tool('t', t, False)
        with mock.patch.object(farm, 'build', side_effect=farm.FarmError('build broke')):
            with self.assertRaises(farm.FarmError):
                farm.install_tool('t', t, True)
        self.assertEqual(farm.verify_tool('t', t), [])

    def test_failed_initramfs_is_retried(self):
        blob = b'\x02' * 64
        t, dest = self.firmware(blob)
        with mock.patch.object(farm, 'run', side_effect=farm.FarmError('initramfs broke')):
            with self.assertRaises(farm.FarmError):
                farm.install_tool('fw', t, False)
        calls = []
        with mock.patch.object(farm, 'run', side_effect=lambda a, **k: calls.append(a) or ''):
            farm.install_tool('fw', t, False)
        self.assertEqual(calls, [['update-initramfs', '-u', '-k', 'all']])

    def test_check_needs_exit_zero(self):
        script = self.tmp / 'bad'
        script.write_text('#!/bin/sh\necho 1.0\nexit 7\n')
        script.chmod(0o755)
        self.assertFalse(farm.run_check({'check': [str(script)], 'expect': '1.0'}))

    def test_pip_lock_checked_before_publishing(self):
        (self.tmp / 'checkout/x.lock').write_text('foo==1.0 --hash=sha256:' + 'a' * 64 + '\n')
        t = {'kind': 'pip', 'lock': 'x.lock', 'links': {str(self.bin / 'py'): 'bin/python3'}}

        def fake_build(name, t, dest, work):
            (dest / 'bin').mkdir()
            (dest / 'bin/python3').write_text('#!/bin/sh\nexit 1\n')
            (dest / 'bin/python3').chmod(0o755)
        with mock.patch.object(farm, 'build', side_effect=fake_build):
            with self.assertRaises(farm.FarmError):
                farm.install_tool('v', t, False)
        self.assertFalse((self.bin / 'py').exists())
        self.assertIsNone(farm.read_receipt('v'))

    def test_unmanaged_symlink_refused(self):
        (self.bin / 'tool').symlink_to('/bin/true')
        with self.assertRaises(farm.FarmError):
            farm.install_tool('t', self.release('1.0'), False)
        self.assertEqual(os.readlink(self.bin / 'tool'), '/bin/true')

    def test_a_writable_directory_on_a_link_or_target_path_is_flagged(self):
        t = self.release('1.0')
        farm.install_tool('t', t, False)
        target_dir = Path(os.path.realpath(self.bin / 'tool')).parent
        for d in (self.bin, target_dir):
            with self.subTest(d=d):
                d.chmod(0o777)
                self.assertTrue(any('writable by a non-root user' in p for p in farm.verify_tool('t', t)))
                d.chmod(0o755)
        self.assertEqual(farm.verify_tool('t', t), [])
        # a symlink met on the way counts with its own chain: a protected alias inside a writable dir does not hide it
        (self.tmp / 'open').mkdir(mode=0o755)
        (self.tmp / 'open' / 'alias').symlink_to(target_dir)
        os.chmod(self.tmp / 'open', 0o777)
        (self.bin / 'via').symlink_to(self.tmp / 'open' / 'alias' / 'tool')
        self.assertEqual(farm.writable_by_others(self.bin / 'via'), self.tmp / 'open')
        (self.bin / 'via').unlink()
        # `..` after a symlink climbs from where the link led, not from the link's own directory
        (self.tmp / 'open' / 'deep').mkdir()
        (self.tmp / 'open' / 'tool').write_text('')
        (self.tmp / 'tool').write_text('')   # where a lexical `..` would point: safe, and wrong
        (self.tmp / 'hop').symlink_to(self.tmp / 'open' / 'deep')
        (self.bin / 'dots').symlink_to(self.tmp / 'hop' / '..' / 'tool')
        self.assertEqual(farm.writable_by_others(self.bin / 'dots'), self.tmp / 'open')
        (self.bin / 'dots').unlink()
        (self.tmp / 'sticky').mkdir(mode=0o777)
        os.chmod(self.tmp / 'sticky', 0o1777)
        self.assertIsNone(farm.writable_by_others(self.tmp / 'sticky'))
        self.assertEqual(farm.verify_tool('t', t), [])

    def test_trusted_names_what_a_non_root_user_could_replace(self):
        (self.tmp / 'open').mkdir()
        os.chmod(self.tmp / 'open', 0o777)
        (self.tmp / 'open' / 'f').write_text('')
        cfg = {'tool': {}, 'apt': {'packages': []}}
        with mock.patch.object(farm, 'load', lambda: cfg), mock.patch('sys.stderr', io.StringIO()) as err:
            self.assertEqual(farm.main(['trusted', str(self.bin), str(self.tmp / 'open' / 'f'), str(self.tmp / 'gone')]), 1)
        self.assertNotIn(str(self.bin), err.getvalue())
        self.assertIn(f'{self.tmp / "open"} is writable by a non-root user', err.getvalue())
        self.assertIn('gone', err.getvalue())

    def test_no_check_runs_through_an_unsafe_or_changed_install(self):
        t = self.release('1.0')
        farm.install_tool('t', t, False)
        self.bin.chmod(0o777)
        self.addCleanup(self.bin.chmod, 0o755)
        with mock.patch.object(farm, 'run_check', side_effect=AssertionError('ran a check')):
            self.assertTrue(farm.verify_tool('t', t))

    def test_helper_tampering_detected(self):
        t = self.release('1.0')
        del t['check']
        farm.install_tool('t', t, False)
        target = Path(os.path.realpath(self.bin / 'tool'))
        target.chmod(0o644)
        self.assertTrue(farm.verify_tool('t', t), 'exec bit removed')
        target.chmod(0o755)
        self.assertEqual(farm.verify_tool('t', t), [])
        target.write_bytes(b'#!/bin/sh\necho evil\n')
        self.assertTrue(farm.verify_tool('t', t), 'content changed')

    def test_firmware_repair_keeps_retrying_initramfs(self):
        blob = b'\x03' * 64
        t, dest = self.firmware(blob)
        with mock.patch.object(farm, 'run', return_value=''):
            farm.install_tool('fw', t, False)
        dest.write_bytes(b'corrupt')
        with mock.patch.object(farm, 'run', side_effect=farm.FarmError('initramfs broke')):
            with self.assertRaises(farm.FarmError):
                farm.install_tool('fw', t, False)
        self.assertTrue(farm.verify_tool('fw', t), 'no receipt claims success')
        calls = []
        with mock.patch.object(farm, 'run', side_effect=lambda a, **k: calls.append(a) or ''):
            farm.install_tool('fw', t, False)
        self.assertEqual(calls, [['update-initramfs', '-u', '-k', 'all']])

    def test_every_attempt_gets_its_own_dir(self):
        t = self.release('1.0')
        farm.install_tool('t', t, False)
        first = farm.read_receipt('t')['dir']
        farm.install_tool('t', t, True)
        second = farm.read_receipt('t')['dir']
        self.assertNotEqual(first, second)
        self.assertTrue(Path(first).exists() and Path(second).exists())

    def test_copy_source_symlink_cannot_leave_the_checkout(self):
        outside = self.tmp / 'secret'
        outside.write_text('secret')

        def fake_commit(repo, commit, dest):
            os.makedirs(dest)
            os.symlink(outside, Path(dest) / 'helper.sh')
        t = {'kind': 'git', 'recipe': 'copy', 'repo': 'https://x/r.git', 'commit': 'a' * 40, 'version': '1',
             'files': ['helper.sh'], 'links': {str(self.bin / 'helper'): 'helper.sh'}}
        with mock.patch.object(farm, 'fetch_commit', side_effect=fake_commit):
            with self.assertRaises(farm.FarmError):
                farm.install_tool('h', t, False)
        self.assertFalse((self.bin / 'helper').exists())

    def test_local_file_symlink_cannot_leave_the_inputs(self):
        data = tar_bytes({'x': (b'x', 0o644)})
        (self.tmp / 'real.tar').write_bytes(data)
        (self.inputs() / 'l.tar').symlink_to(self.tmp / 'real.tar')
        t = {'kind': 'local', 'file': 'l.tar', 'sha256': sha(data), 'version': '1'}
        with self.assertRaisesRegex(farm.FarmError, 'resolves outside'):
            farm.install_tool('l', t, False)

    def test_local_input_is_checked_and_extracted_from_roots_copy(self):
        good = tar_bytes({'L/bin/tool': (tool_script('3.1'), 0o755)})
        src = self.inputs() / 'l.tar'
        src.write_bytes(good)
        t = {'kind': 'local', 'file': 'l.tar', 'sha256': sha(good), 'version': '3.1',
             'links': {str(self.bin / 'tool'): 'L/bin/tool'}, 'check': [str(self.bin / 'tool')]}
        check = farm.check_file
        def rewrite_after_check(path, sha256):   # the input's owner rewrites it in place once the hash passed
            check(path, sha256)
            src.write_bytes(tar_bytes({'L/bin/tool': (b'#!/bin/sh\necho evil\n', 0o755)}))
        with mock.patch.object(farm, 'check_file', rewrite_after_check):
            farm.install_tool('l', t, False)
        self.assertEqual((self.bin / 'tool').read_bytes(), tool_script('3.1'))
        self.assertEqual([p.name for p in (self.tmp / 'opt/hil-farm/tools/l').iterdir() if p.name.startswith('.')], [])

    def test_local_input_must_be_named_and_present(self):
        t = {'kind': 'local', 'file': 'l.tar', 'sha256': 'a' * 64, 'version': '1'}
        with mock.patch.object(farm, 'INPUTS', None), \
                self.assertRaisesRegex(farm.FarmError, 'needs l.tar: name the directory holding it with setup.sh --inputs'):
            farm.install_tool('l', t, False)
        d = self.inputs()
        with self.assertRaisesRegex(farm.FarmError, f'{d / "l.tar"} missing; prepare it'):
            farm.install_tool('l', t, False)
        (d / 'l.tar').write_bytes(b'other')
        with self.assertRaisesRegex(farm.FarmError, 'sha256 mismatch'):
            farm.install_tool('l', t, False)

    def test_path_validation(self):
        runner = {'kind': 'runner', 'url': 'https://x/r', 'sha256': 'a' * 64, 'version': '1',
                  'dir': '/home/ghrunner/r'}
        self.assertTrue(any('unknown fields' in e for e in farm.validate_tool('r', runner)), 'runner dir is fixed')
        git = {'kind': 'git', 'recipe': 'copy', 'repo': 'https://x/r.git', 'commit': 'a' * 40, 'version': '1',
               'files': ['../../outside']}
        self.assertTrue(farm.validate_tool('g', git))
        self.assertTrue(farm.validate_tool('p', {'kind': 'pip', 'lock': '/etc/x.lock'}))
        self.assertTrue(farm.validate_tool('l', {'kind': 'local', 'file': '../x.tar', 'sha256': 'a' * 64,
                                                 'version': '1'}))


class TestRunnerInstall(FarmCase):
    def setUp(self):
        super().setUp()
        self.dirs = tuple(str(self.tmp / f'opt/actions-runner/{n}') for n in farm.RUNNERS)
        p = mock.patch.object(farm, 'RUNNER_DIRS', self.dirs)
        p.start()
        self.addCleanup(p.stop)
        data = tar_bytes({'bin/Runner.Listener': (b'listener', 0o755), 'config.sh': (b'#!/bin/sh\n', 0o755)})
        self.remote['https://example.test/runner.tgz'] = data
        self.t = {'kind': 'runner', 'url': 'https://example.test/runner.tgz', 'sha256': sha(data), 'version': '2'}

    def test_the_same_pin_goes_into_every_runner_dir(self):
        self.assertEqual(len(self.dirs), 2)
        farm.install_tool('actions-runner', self.t, False)
        for d in self.dirs:
            self.assertEqual(Path(d, 'bin/Runner.Listener').read_bytes(), b'listener')
        self.assertEqual(farm.verify_tool('actions-runner', self.t), [])
        farm.install_tool('actions-runner', self.t, False)   # verified: a no-op, not an update

    def test_a_missing_runner_dir_is_named_and_filled_without_touching_the_other(self):
        farm.install_tool('actions-runner', self.t, False)
        shutil.rmtree(self.dirs[1])
        Path(self.dirs[0], '.runner').write_text('registered')
        self.assertEqual(farm.verify_tool('actions-runner', self.t), [f'{self.dirs[1]} holds no runner'])
        farm.install_tool('actions-runner', self.t, False)
        self.assertTrue(Path(self.dirs[1], 'bin/Runner.Listener').is_file())
        self.assertEqual(Path(self.dirs[0], '.runner').read_text(), 'registered')

    def test_never_over_an_installed_runner_of_another_pin(self):
        farm.install_tool('actions-runner', self.t, False)
        shutil.rmtree(self.dirs[1])
        bumped = dict(self.t, version='3')
        with self.assertRaisesRegex(farm.FarmError, 'runner updates are not supported'):
            farm.install_tool('actions-runner', bumped, False)
        self.assertFalse(os.path.exists(self.dirs[1]))
        farm.install_tool('actions-runner', self.t, False)
        with self.assertRaisesRegex(farm.FarmError, 'runner updates are not supported'):
            farm.install_tool('actions-runner', bumped, False)


def keep_host_globals(case):
    """Restore what use_host sets once case ends."""
    p = mock.patch.multiple(farm, **{g: getattr(farm, g) for g in
                                     ('RUNNERS', 'RUNNER_DIRS', 'RUNNER_UNITS')})
    p.start()
    case.addCleanup(p.stop)


class TestRunnersMatchCommon(unittest.TestCase):
    def test_farm_py_and_common_sh_name_the_same_runners(self):
        here = Path(__file__).resolve().parent.parent / 'skills' / 'hil-farm-setup' / 'scripts'
        for host in sorted(HOSTS.glob('*.toml')):
            with self.subTest(host=host.name):
                out = subprocess.run(['bash', '-c', '. "$1"; for n in "${RUNNERS[@]}"; do runner_conf "$n"; '
                                      'echo "$n $RUNNER_DIR $RUNNER_UNIT"; done', '_', str(here / 'common.sh')],
                                     capture_output=True, text=True, check=True,
                                     env={**os.environ, 'HIL_FARM_HOST': str(host)}).stdout
                keep_host_globals(self)
                cfg = farm.load()
                farm.use_host(cfg, farm.load_host(cfg, host))
                want = [' '.join(r) for r in zip(farm.RUNNERS, farm.RUNNER_DIRS, farm.RUNNER_UNITS)]
                self.assertEqual(out.splitlines(), want)


class TestHost(unittest.TestCase):
    """The repository's farm.toml under each host fixture; FarmCase's patched link roots would fail its validation."""
    def setUp(self):
        self.tmp = Path(tempfile.mkdtemp())
        self.addCleanup(shutil.rmtree, self.tmp)
        keep_host_globals(self)

    def host(self, name):
        cfg = farm.load()
        return cfg, farm.use_host(cfg, farm.load_host(cfg, HOSTS / f'{name}.toml'))

    def test_the_installed_host_file_unless_one_is_named(self):
        with mock.patch.dict(os.environ, {'HIL_FARM_HOST': ''}):
            self.assertEqual(farm.host_path(), farm.INSTALLED_HOST)
        with mock.patch.dict(os.environ, {'HIL_FARM_HOST': str(self.tmp / 'h.toml')}):
            self.assertEqual(farm.host_path(), self.tmp / 'h.toml')
            with self.assertRaisesRegex(farm.FarmError, f'no host file {self.tmp / "h.toml"}'):
                farm.load_host(farm.load())
            (self.tmp / 'h.toml').write_text('profile = \n')
            with self.assertRaisesRegex(farm.FarmError, 'h.toml: '):
                farm.load_host(farm.load())

    def test_a_dedicated_host_changes_nothing(self):
        cfg, _ = self.host('dedicated')
        self.assertEqual(cfg, farm.load())
        self.assertEqual(farm.RUNNERS, ('tinyusb', 'tinyuf2'))

    def test_a_shared_host_narrows_runners_tools_and_apt(self):
        cfg, h = self.host('shared')
        self.assertEqual((h['shared'], h['iar']), (True, True))
        self.assertEqual(farm.RUNNER_DIRS, ('/opt/actions-runner/tinyusb',))
        self.assertEqual(farm.RUNNER_UNITS, ('hil-runner-tinyusb',))
        self.assertEqual(set(farm.load()['tool']) - set(cfg['tool']), {'renesas-fw'})

    def test_only_a_dedicated_host_gets_the_policy_packages(self):
        policy = set(farm.load()['apt']['policy'])
        for name, want in (('dedicated', policy), ('shared', set())):
            env = {'HIL_FARM_HOST': str(HOSTS / f'{name}.toml')}
            with self.subTest(host=name), mock.patch.dict(os.environ, env), \
                    mock.patch('sys.stdout', io.StringIO()) as out:
                self.assertEqual(farm.main(['apt']), 0)
                pkgs = out.getvalue().split()
                self.assertIn('nftables', pkgs)
                self.assertIn('build-essential', pkgs)
                self.assertEqual(policy & set(pkgs), want)

    def test_runner_path_appends_the_host_dirs(self):
        with mock.patch.dict(os.environ, {'HIL_FARM_HOST': str(HOSTS / 'shared.toml')}), \
                mock.patch('sys.stdout', io.StringIO()) as out:
            self.assertEqual(farm.main(['runner-path']), 0)
        self.assertEqual(out.getvalue(), farm.load()['farm']['path'] + ':/opt/vendor/arm/bin\n')

    def test_host_sh_quotes_every_value(self):
        _, h = self.host('shared')
        h = {**h, 'runner': {'tinyusb': {'name': 'a.b', 'labels': ['x', 'y-z']}}}
        out = subprocess.run(['bash', '-c', f'set -u; {farm.host_sh(h)}\n'
                              'echo "$ADMIN|$SHARED_HOST|$IAR_CHECKS|${RUNNERS[*]}|'
                              '${RUNNER_REG_NAMES[tinyusb]}|${RUNNER_REG_LABELS[tinyusb]}"'],
                             capture_output=True, text=True, check=True).stdout
        self.assertEqual(out, 'owner|1|1|tinyusb|a.b|x,y-z\n')
        _, h = self.host('dedicated')
        out = subprocess.run(['bash', '-c', f'set -u; {farm.host_sh(h)}\necho "$SHARED_HOST $IAR_CHECKS ${{RUNNERS[*]}}"'],
                             capture_output=True, text=True, check=True).stdout
        self.assertEqual(out, '0 0 tinyusb tinyuf2\n')   # the runner tables' order

    def test_host_validation(self):
        cfg = farm.load()
        base = tomllib.loads((HOSTS / 'shared.toml').read_text())
        for change, msg in ((lambda h: h.pop('admin'), r"missing \['admin'\]"),
                            (lambda h: h.update(profile='Has Space'), 'profile must'),
                            (lambda h: h.update(admin='-x'), 'admin must'),
                            (lambda h: h.update(shared='yes'), r"wrong types for \['shared'\]"),
                            (lambda h: h.update(profile=1), r"wrong types for \['profile'\]"),
                            (lambda h: h.update(skip='renesas-fw'), r"wrong types for \['skip'\]"),
                            (lambda h: h['runner']['tinyusb'].update(labels='lab'), 'runner.tinyusb: name and labels'),
                            (lambda h: h['runner']['tinyusb'].update(name=1), 'runner.tinyusb: name and labels'),
                            (lambda h: h['runner'].update(tinyusb='x'), 'runner.tinyusb: name and labels'),
                            (lambda h: h.update(runner={}), 'runner must hold a table'),
                            (lambda h: h['runner'].update(x={'name': 'x', 'labels': ['x']}), 'runner must hold a table'),
                            (lambda h: h.update(iar='yes'), r"wrong types for \['iar'\]"),
                            (lambda h: h['runner']['tinyusb'].update(name='a b'), 'runner.tinyusb: name and labels'),
                            (lambda h: h['runner']['tinyusb'].update(labels=[]), 'runner.tinyusb: name and labels'),
                            (lambda h: h['runner']['tinyusb'].update(labels=['a,b']), 'runner.tinyusb'),
                            (lambda h: h['runner']['tinyusb'].update(other=1), 'runner.tinyusb'),
                            (lambda h: h.update(skip=['nope']), 'skip names unknown'),
                            (lambda h: h.update(runner_path=['rel/bin']), 'runner_path'),
                            (lambda h: h.update(runner_path=['/a:/b']), 'runner_path'),
                            (lambda h: h.update(legacy_units=['x.service']), 'unknown fields'),
                            (lambda h: h.update(runners=['tinyusb']), 'unknown fields'),
                            (lambda h: h.update(checks=['iar']), 'unknown fields'),
                            (lambda h: h.update(adopt={'/usr/local/bin/openocd': 'a' * 64}), 'unknown fields'),
                            (lambda h: h.update(other=1), 'unknown fields')):
            with self.subTest(msg=msg):
                h = copy.deepcopy(base)
                change(h)
                errs = farm.validate_host(h, cfg)
                self.assertTrue(any(re.search(msg, e) for e in errs), errs)
        self.assertEqual(farm.validate_host(base, cfg), [])

    def test_validate_names_a_bad_host_file(self):
        bad = self.tmp / 'bad.toml'
        bad.write_text((HOSTS / 'dedicated.toml').read_text().replace('"lab"', '"Lab"', 1))
        with mock.patch('sys.stderr', io.StringIO()) as err, mock.patch('sys.stdout', io.StringIO()):
            self.assertEqual(farm.main(['validate', '--host', str(bad)]), 1)
            self.assertEqual(farm.main(['validate', '--host', str(HOSTS / 'dedicated.toml')]), 0)
        self.assertIn('profile must be a lowercase name', err.getvalue())

    def test_every_farm_host_file_validates(self):
        hosts = sorted((Path(farm.HERE).parent / 'hosts').glob('*.toml'))
        self.assertTrue(hosts)
        cfg = farm.load()
        for path in hosts:
            with self.subTest(host=path.name):
                farm.load_host(cfg, path)
        # the README points at them as the examples: together they show every field
        self.assertEqual(set().union(*(tomllib.loads(p.read_text()) for p in hosts)), farm.HOST_FIELDS)


class TestInstallHost(unittest.TestCase):
    """host-install against a temp /etc/hil-farm and /opt/hil-farm, with the guard recording what it asks."""
    def setUp(self):
        self.tmp = Path(tempfile.mkdtemp())
        self.addCleanup(shutil.rmtree, self.tmp)
        self.installed = self.tmp / 'etc' / 'host.toml'
        self.home = self.tmp / 'opt'
        self.home.mkdir()
        for k, v in (('INSTALLED_HOST', self.installed), ('FARM_HOME', self.home),
                     ('OLD_MARKER', self.home / 'profile')):
            p = mock.patch.object(farm, k, v)
            p.start()
            self.addCleanup(p.stop)
        self.guarded = []
        p = mock.patch.object(farm, 'guard', side_effect=lambda units=None: self.guarded.append(units))
        p.start()
        self.addCleanup(p.stop)

    def write(self, name, text):
        f = self.tmp / name
        f.write_text(text)
        return f

    def install(self, src):
        return farm.install_host(farm.load(), src)

    def test_first_install_writes_the_file_root_readable(self):
        self.assertTrue(self.install(HOSTS / 'dedicated.toml'))
        self.assertEqual(self.installed.read_bytes(), (HOSTS / 'dedicated.toml').read_bytes())
        self.assertEqual(self.installed.stat().st_mode & 0o777, 0o644)
        self.assertFalse(self.installed.with_name('host.toml.prev').exists())
        self.assertEqual(self.guarded, [['hil-runner-tinyuf2', 'hil-runner-tinyusb']])
        self.assertFalse(self.install(HOSTS / 'dedicated.toml'))   # identical: nothing to do
        self.assertEqual(len(self.guarded), 1)

    def test_a_setting_change_keeps_the_previous_file(self):
        self.install(HOSTS / 'shared.toml')
        text = (HOSTS / 'shared.toml').read_text()
        new = self.write('new.toml', text.replace('iar = true', 'iar = false'))
        self.assertTrue(self.install(new))
        self.assertEqual(self.installed.read_text(), new.read_text())
        self.assertEqual(self.installed.with_name('host.toml.prev').read_text(), text)
        self.assertEqual(self.guarded[-1], ['hil-runner-tinyusb'])

    def test_an_identity_change_refuses(self):
        self.install(HOSTS / 'dedicated.toml')
        text = (HOSTS / 'dedicated.toml').read_text()
        for old, new, field in (('profile = "lab"', 'profile = "lab2"', 'profile'),
                                ('admin = "admin"', 'admin = "other"', 'admin'),
                                ('admin = "admin"', 'admin = "admin"\nshared = true', 'shared'),
                                ('labels = ["tinyuf2"]', 'labels = ["tinyuf2", "x"]', 'runner'),
                                ('name = "lab-tinyuf2"', 'name = "renamed"', 'runner')):
            with self.subTest(field=field):
                f = self.write('new.toml', text.replace(old, new))
                with self.assertRaisesRegex(farm.FarmError, f'changes {field} of this host'):
                    self.install(f)
                self.assertEqual(self.installed.read_text(), text)
        self.assertEqual(len(self.guarded), 1)

    def test_an_explicit_default_is_no_identity_change(self):
        self.install(HOSTS / 'dedicated.toml')
        f = self.write('new.toml', (HOSTS / 'dedicated.toml').read_text().replace('admin = "admin"',
                                                                                  'admin = "admin"\nshared = false'))
        self.assertTrue(self.install(f))

    def test_guard_refusal_changes_nothing(self):
        farm.guard.side_effect = farm.FarmError('hil-runner-tinyusb is active')
        with self.assertRaisesRegex(farm.FarmError, 'is active'):
            self.install(HOSTS / 'dedicated.toml')
        self.assertFalse(self.installed.exists())

    def test_an_install_from_before_host_files_keeps_its_profile(self):
        (self.home / 'profile').write_text('lab\n')
        with self.assertRaisesRegex(farm.FarmError, 'installed as profile lab; .*shared.toml is for shared'):
            self.install(HOSTS / 'shared.toml')
        self.assertTrue(self.install(HOSTS / 'dedicated.toml'))
        self.assertFalse((self.home / 'profile').exists())

    def test_an_install_without_marker_needs_confirmation(self):
        (self.home / 'farm.toml').write_text('')
        with self.assertRaisesRegex(farm.FarmError, 'no host file and no profile marker'):
            self.install(HOSTS / 'dedicated.toml')
        self.assertFalse(self.installed.exists())
        self.assertEqual(self.guarded, [])

    def test_a_bad_installed_file_refuses(self):
        self.installed.parent.mkdir()
        self.installed.write_text('profile = "lab"\n')
        with self.assertRaisesRegex(farm.FarmError, 'missing'):
            self.install(HOSTS / 'dedicated.toml')

    def test_needs_root(self):
        with mock.patch.object(farm.os, 'geteuid', return_value=1000), mock.patch('sys.stderr', io.StringIO()) as err:
            self.assertEqual(farm.main(['host-install', str(HOSTS / 'dedicated.toml')]), 1)
        self.assertIn('run as root', err.getvalue())


class TestGuard(unittest.TestCase):
    def guard_with(self, state, pgrep_rc):
        """state: one systemctl is-active answer for every unit, or {unit: answer}."""
        def fake(argv, **kw):
            if argv[0] == 'systemctl':
                return mock.Mock(stdout=(state if isinstance(state, str) else state[argv[2]]) + '\n', returncode=0)
            return mock.Mock(returncode=pgrep_rc)
        with mock.patch.object(farm.subprocess, 'run', side_effect=fake) as run:
            farm.guard()
        return run

    def test_every_runner_unit_is_asked(self):
        asked = [c.args[0][2] for c in self.guard_with('inactive', 1).call_args_list if c.args[0][0] == 'systemctl']
        self.assertEqual(asked, ['hil-runner-tinyusb', 'hil-runner-tinyuf2'])

    def test_one_running_unit_refuses(self):
        for running in farm.RUNNER_UNITS:
            states = {u: 'active' if u == running else 'inactive' for u in farm.RUNNER_UNITS}
            with self.subTest(running=running), self.assertRaisesRegex(farm.FarmError, f'{running} is active'):
                self.guard_with(states, 1)

    def test_idle(self):
        self.guard_with('inactive', 1)
        self.guard_with('failed', 1)

    def test_refuses(self):
        for state, rc in [('active', 1), ('activating', 1), ('deactivating', 1), ('', 1),
                          ('inactive', 0), ('inactive', 2)]:
            with self.subTest(state=state, rc=rc), self.assertRaises(farm.FarmError):
                self.guard_with(state, rc)

    def test_inspection_error(self):
        with mock.patch.object(farm.subprocess, 'run', side_effect=FileNotFoundError('systemctl')):
            with self.assertRaises(farm.FarmError):
                farm.guard()


class TestSelect(unittest.TestCase):
    cfg = {'tool': {'a': {'kind': 'release'}, 'r': {'kind': 'runner'}}}

    def test_update_skips_runner(self):
        self.assertEqual(list(farm.select(self.cfg, [], True)), ['a'])
        self.assertEqual(list(farm.select(self.cfg, [], False)), ['a', 'r'])

    def test_naming_runner_in_update_fails(self):
        with self.assertRaises(farm.FarmError):
            farm.select(self.cfg, ['r'], True)
        with self.assertRaises(farm.FarmError):
            farm.select(self.cfg, ['nope'], False)


class TestReceipt(FarmCase):
    def test_receipt_records_input(self):
        t = self.release('1.0')
        farm.install_tool('t', t, False)
        rec = json.loads((farm.RECEIPTS / 't.json').read_text())
        self.assertEqual(rec['input'], farm.input_hash('t', t))
        self.assertEqual(rec['version'], '1.0')


def setUpModule():
    guard_survivors(__name__)


if __name__ == '__main__':
    unittest.main()
