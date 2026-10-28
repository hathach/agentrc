import errno
import importlib.util
import json
import os
import shutil
import signal
import stat
import subprocess
import sys
import tempfile
import threading
import time
import unittest
from pathlib import Path
from unittest import mock

HOOK = Path(__file__).resolve().parents[1] / 'hooks' / 'simplify-gate' / 'simplify_gate.py'
WRAPPER = HOOK.with_name('simplify-gate')
GATE = Path(__file__).resolve().parents[1] / 'skills' / 'simplify-gate' / 'scripts' / 'gate.py'
sys.path.insert(0, str(Path(__file__).resolve().parent))
from helpers import git_env, guard_survivors, tmpdir  # noqa: E402
spec = importlib.util.spec_from_file_location('simplify_gate', HOOK)
gate = importlib.util.module_from_spec(spec)
spec.loader.exec_module(gate)

REAL_RUN = subprocess.run  # check_output goes through run, so the fake must pass git through
FINDING = {'file': 'a.txt', 'line': 1, 'problem': 'dead branch', 'alternative': 'drop it'}


def sh(cwd, *args):
    return subprocess.run(args, cwd=cwd, check=True, capture_output=True, text=True).stdout


def rmtree(path):
    """shutil.rmtree that also removes git's read-only objects, which Windows refuses to delete."""
    def writable(func, p, _):
        os.chmod(p, stat.S_IWRITE)
        func(p)
    shutil.rmtree(path, **({'onexc': writable} if sys.version_info >= (3, 12) else {'onerror': writable}))


def make_repo(path):
    path.mkdir(parents=True, exist_ok=True)
    sh(path, 'git', 'init', '-q', '-b', 'main')
    sh(path, 'git', 'config', 'user.email', 't@t')
    sh(path, 'git', 'config', 'user.name', 't')
    (path / 'a.txt').write_text('a\n')
    (path / 'c.txt').write_text('c\n')
    sh(path, 'git', 'add', '-A')
    sh(path, 'git', 'commit', '-qm', 'init')
    (path / '.git' / 'simplify-gate').touch()
    return path


def setUpModule():
    guard_survivors(__name__)
    unittest.enterModuleContext(mock.patch.dict(os.environ, git_env(), clear=True))  # the hook's git too


class Session:
    """Drives handle() the way Claude Code would, with a fake Codex."""

    def __init__(self, root, directory):
        self.root, self.directory = root, directory
        self.results = []  # what each codex attempt writes to -o; an Exception raises instead
        self.calls = []
        self.prompts = []
        self.on_codex = None  # runs while the fake Codex is 'running'

    def fake_run(self, cmd, **kw):
        if Path(cmd[0]).stem.lower() != 'codex':  # the hook runs codex by its full path where it finds one
            return REAL_RUN(cmd, **kw)
        self.calls.append(cmd)
        self.prompts.append(kw['input'].decode('utf-8'))  # the bytes Codex reads
        if self.on_codex:
            self.on_codex()
        if not self.results:
            raise AssertionError('unexpected codex call')
        result = self.results.pop(0)
        if isinstance(result, Exception):
            raise result
        Path(cmd[cmd.index('-o') + 1]).write_text(result)
        return subprocess.CompletedProcess(cmd, 0)

    def event(self, name, **fields):
        payload = {'session_id': 's1', 'cwd': str(self.root), 'hook_event_name': name, **fields}
        # codex_exe would find none on Windows: no codex is on the test's PATH
        with mock.patch.object(gate, 'run_tree', self.fake_run), mock.patch.object(gate, 'codex_exe', lambda: 'codex'):
            return gate.handle(self.root, self.directory, payload)

    def prompt(self, text='next'):
        return self.event('UserPromptSubmit', prompt=text)

    def stop(self, message='done'):
        return self.event('Stop', last_assistant_message=message)

    def state(self):
        return json.loads((self.directory / 'state.json').read_text())

    def patch(self):
        return gate.patch_text(self.root, self.directory / 'blobs', gate.coalesce(self.state()['batches']))


class GateTest(unittest.TestCase):
    def setUp(self):
        self.base = tmpdir(self)  # as git reports it: snapshot() keeps worktrees under root
        self.root = make_repo(self.base / 'repo')
        self.s = Session(self.root, self.base / 'state')
        self.s.prompt('first')

    def write(self, name, text):
        (self.root / name).write_text(text)

    def new_turn(self):
        """Close the current turn, reviewing whatever the setup wrote, and open
        the next; the recorded prompts start over."""
        self.review()
        self.s.calls.clear()
        self.s.prompts.clear()
        self.s.prompt()

    def review(self, *results, message='done'):
        """Stop with the given fake verdicts queued; the empty verdict by default."""
        self.s.results = [json.dumps({'findings': list(r)}) for r in results or [[]]]
        return self.s.stop(message)

    # --- scope -----------------------------------------------------------------

    def test_status_turn_skips_codex(self):
        self.assertEqual(self.s.stop(), {})
        self.assertEqual(self.s.calls, [])

    def test_pre_existing_dirt_is_excluded(self):
        self.write('c.txt', 'dirty before the turn\n')
        (self.root / 'stray.txt').write_text('untracked before the turn\n')
        self.new_turn()
        self.write('a.txt', 'a2\n')
        self.review()
        self.assertIn('Path: "a.txt"', self.s.prompts[0])
        self.assertEqual(self.s.prompts[0].count('Path: '), 1)
        self.assertNotIn('stray.txt', self.s.prompts[0])
        self.assertNotIn('dirty before', self.s.prompts[0])

    def test_edit_new_delete_then_stage_and_commit_are_one_change_set(self):
        self.write('a.txt', 'a2\n')
        (self.root / 'n.txt').write_text('new\n')
        (self.root / 'c.txt').unlink()
        sh(self.root, 'git', 'add', '-A')
        sh(self.root, 'git', 'commit', '-qm', 'x')
        self.review()
        patch = self.s.prompts[0]
        self.assertIn('-a\n+a2\n', patch)
        self.assertIn('+new\n', patch)
        self.assertIn('-c\n', patch)
        self.assertEqual(self.s.state()['batches'], [], 'a verdict retires the batch')

    def test_a_reverted_edit_is_not_a_change(self):
        self.write('a.txt', 'a2\n')
        self.write('a.txt', 'a\n')
        self.assertEqual(self.s.stop(), {})
        self.assertEqual(self.s.calls, [])

    def test_a_gitignored_file_is_out_of_scope(self):
        (self.root / '.gitignore').write_text('out/\n')
        sh(self.root, 'git', 'add', '.gitignore')
        sh(self.root, 'git', 'commit', '-qm', 'ignore')
        self.new_turn()
        (self.root / 'out').mkdir()
        (self.root / 'out' / 'obj.o').write_bytes(b'\0')
        self.assertEqual(self.s.stop(), {})

    def test_symlinks_and_binaries_are_described_not_traversed(self):
        outside = self.base / 'outside'
        outside.mkdir()
        (outside / 'secret').write_text('nope\n')
        os.symlink(outside, self.root / 'deps')
        (self.root / 'blob.bin').write_bytes(b'\0\1\2')
        self.review()
        patch = self.s.prompts[0]
        self.assertIn(f'+{outside.as_posix()}', patch)
        self.assertNotIn('nope', patch)
        self.assertIn('[binary blob', patch)
        self.s.prompt()
        sh(self.root, 'git', 'add', 'deps')
        self.assertEqual(self.s.stop(), {}, 'the link is snapshotted as git stores it')

    def test_modes_are_the_ones_git_records(self):
        (self.root / 'run.sh').write_text('old\n')
        os.chmod(self.root / 'run.sh', 0o755)
        sh(self.root, 'git', 'add', 'run.sh')
        sh(self.root, 'git', 'update-index', '--chmod=+x', 'run.sh')  # git for Windows reads no exec bit
        sh(self.root, 'git', 'commit', '-qm', 'script')
        self.new_turn()
        (self.root / 'run.sh').write_text('new\n')
        (self.root / 'x.bat').write_text('@echo off\n')  # executable to os.stat on Windows, 100644 to git
        self.review()
        self.assertIn('Path: "run.sh"; mode 100755 -> 100755', self.s.prompts[0])
        self.assertIn('Path: "x.bat"; mode None -> 100644', self.s.prompts[0])
        self.s.prompt()
        sh(self.root, 'git', 'add', '-A')
        self.assertEqual(self.s.stop(), {}, 'staging changes no mode')

    @unittest.skipUnless(os.name == 'nt', 'POSIX reads the mode from the file system, as before')
    def test_a_link_checked_out_as_a_file_keeps_its_mode_and_filters(self):
        sh(self.root, 'git', 'config', 'core.symlinks', 'false')
        sh(self.root, 'git', 'config', 'filter.up.clean', 'tr a-z A-Z')  # git filters the file standing in for a link
        (self.root / '.gitattributes').write_text('link filter=up\n')
        sha = subprocess.run(['git', 'hash-object', '-w', '--stdin'], cwd=self.root, input=b'C.TXT', check=True,
                             capture_output=True).stdout.decode().strip()
        sh(self.root, 'git', 'update-index', '--add', '--cacheinfo', f'120000,{sha},link')
        (self.root / 'link').write_bytes(b'c.txt')  # as git checks the link out
        sh(self.root, 'git', 'add', '.gitattributes')
        sh(self.root, 'git', 'commit', '-qm', 'link')
        self.new_turn()
        (self.root / 'link').write_bytes(b'a.txt')
        self.review()
        self.assertIn('Path: "link"; mode 120000 -> 120000', self.s.prompts[0])
        self.assertIn('+A.TXT', self.s.prompts[0])
        self.s.prompt()
        sh(self.root, 'git', 'add', 'link')
        self.assertEqual(sh(self.root, 'git', 'ls-files', '-s', 'link').split()[0], '120000')
        self.assertEqual(self.s.stop(), {}, 'staging changes neither mode nor blob')

    def test_a_file_standing_in_for_a_link_takes_the_mode_git_gives_it(self):
        # the Windows rule, run here: a plain file keeps the link's 120000 only while core.symlinks is false
        sha = subprocess.run(['git', 'hash-object', '-w', '--stdin'], cwd=self.root, input=b'c.txt', check=True,
                             capture_output=True).stdout.decode().strip()
        (self.root / 'link').write_bytes(b'a.txt')
        blobs = self.base / 'cache' / 'blobs'
        blobs.mkdir(parents=True)
        for symlinks, want in (('true', '100644'), ('false', '120000')):
            sh(self.root, 'git', 'config', 'core.symlinks', symlinks)
            sh(self.root, 'git', 'update-index', '--add', '--cacheinfo', f'120000,{sha},link')
            with mock.patch.object(gate.os, 'name', 'nt'):
                got = gate.snapshot_tree(self.root, blobs)[0]['link'][0]
            sh(self.root, 'git', 'add', 'link')
            self.assertEqual((got, sh(self.root, 'git', 'ls-files', '-s', 'link').split()[0]), (want, want), symlinks)

    def test_non_ascii_text_reaches_codex_and_comes_back(self):
        text = 'caf\u00e9 \u2714 \u0151'  # outside the Windows ANSI code pages
        (self.root / 'a.txt').write_bytes(f'{text}\n'.encode())
        seen = self.base / 'stdin'
        code = ('import sys; open(sys.argv[1], "wb").write(sys.stdin.buffer.read()); '
                'open(sys.argv[2], "w", encoding="utf-8").write(sys.argv[3])')
        result = json.dumps({'findings': [{**FINDING, 'problem': text}]}, ensure_ascii=False)

        def child_codex(cmd, **kw):  # a real child, so the prompt crosses a pipe as the hook encodes it
            if Path(cmd[0]).stem.lower() != 'codex':
                return REAL_RUN(cmd, **kw)
            return REAL_RUN([sys.executable, '-c', code, str(seen), cmd[cmd.index('-o') + 1], result], **kw)
        self.s.fake_run = child_codex
        reply = self.s.stop(f'wrote {text}')
        self.assertIn(f'a.txt:1: {text}', reply['reason'])
        prompt = seen.read_bytes().decode('utf-8')
        self.assertIn(f'+{text}\n', prompt)
        self.assertIn(f'wrote {text}', prompt)

    def test_a_submodule_is_left_out_of_scope_but_its_pointer_renders(self):
        sub = make_repo(self.base / 'sub')
        sh(self.root, 'git', '-c', 'protocol.file.allow=always', 'submodule', 'add', '-q', str(sub), 'sub')
        sh(self.root, 'git', 'commit', '-qm', 'sub')
        self.new_turn()
        (self.root / 'sub' / 'a.txt').write_text('vendored change\n')
        self.write('a.txt', 'mine\n')
        self.review()
        self.assertNotIn('vendored', self.s.prompts[0])
        self.assertIn('+mine\n', self.s.prompts[0])
        self.new_turn()
        sh(self.root / 'sub', 'git', '-c', 'user.name=t', '-c', 'user.email=t@t', 'commit', '-qam', 'bump')  # the clone has no user.*, nor may HOME
        sh(self.root, 'git', 'add', 'sub')
        self.review()
        self.assertIn('[submodule at commit', self.s.prompts[-1])

    def test_edits_in_a_task_worktree_are_tracked(self):
        wt = self.root / '.worktrees' / 'task'
        sh(self.root, 'git', 'worktree', 'add', '-q', str(wt), '-b', 'task')
        (wt / 'a.txt').write_text('in worktree\n')
        (wt / 'c.txt').unlink()
        sh(wt, 'git', 'add', '-A')
        sh(wt, 'git', 'commit', '-qm', 'wt')
        self.review()
        patch = self.s.prompts[0]
        self.assertIn('Path: ".worktrees/task/a.txt"', patch, 'worktree prefixes are posix paths')
        self.assertIn('-a\n+in worktree\n', patch, 'an edit, not a creation')
        self.assertIn('-c\n', patch, 'the deletion is kept')

    def test_a_worktree_outside_the_checkout_is_another_sessions(self):
        wt = self.base / 'sibling'
        sh(self.root, 'git', 'worktree', 'add', '-q', str(wt), '-b', 'sibling')
        (wt / 'a.txt').write_text('elsewhere\n')
        self.assertEqual(self.s.stop(), {})

    def test_edits_in_a_sibling_worktree_are_tracked(self):
        wt = self.base / 'repo--task'
        sh(self.root, 'git', 'worktree', 'add', '-q', str(wt), '-b', 'task')
        self.new_turn()
        (wt / 'a.txt').write_text('in sibling\n')
        (wt / 'c.txt').unlink()
        sh(wt, 'git', 'add', '-A')
        sh(wt, 'git', 'commit', '-qm', 'wt')
        name = '../repo--task/a.txt'
        reply = self.review([{**FINDING, 'file': name}])
        patch = self.s.prompts[0]
        self.assertIn(f'Path: "{name}"', patch)
        self.assertIn('-a\n+in sibling\n', patch, 'an edit, not a creation')
        self.assertIn('Path: "../repo--task/c.txt"', patch, 'the deletion is kept')
        self.assertIn(f'{name}:1: dead branch', reply['reason'], 'a finding on a sibling path is in scope')

    def test_a_sibling_of_another_repository_created_mid_turn_reads_its_own_store(self):
        other = self.base / 'other'
        other.mkdir()
        sh(other, 'git', 'init', '-q', '-b', 'main')
        (other / 'o.txt').write_text('foreign original\n')
        (other / 'gone.txt').write_text('foreign deleted\n')
        sh(other, 'git', 'add', '-A')
        sh(other, 'git', '-c', 'user.name=t', '-c', 'user.email=t@t', 'commit', '-qm', 'init')
        wt = self.base / 'repo--other'
        sh(other, 'git', 'worktree', 'add', '-q', str(wt), '-b', 'task')
        (wt / 'o.txt').write_text('foreign edited\n')
        (wt / 'gone.txt').unlink()
        self.s.results = [OSError('no codex'), OSError('no codex')]
        self.s.stop()  # the batch stays queued with both sides cached
        self.assertEqual([e[:18] for e in self.s.state()['errors']], ['codex failed twice'],
                         'blobs come from the sibling, not the root repository')
        blobs = self.s.directory / 'blobs'
        blobs.rename(blobs.with_name('aside'))
        blobs.mkdir()
        self.assertIn('-foreign original\n+foreign edited\n', self.s.patch(), 'uncached, read from the sibling')
        blobs.rmdir()
        blobs.with_name('aside').rename(blobs)
        rmtree(other)
        rmtree(wt)
        sh(self.root, 'git', 'gc', '-q', '--prune=now')
        patch = self.s.patch()
        self.assertIn('-foreign original\n+foreign edited\n', patch, 'the baseline is the commit it was created at')
        self.assertIn('-foreign deleted\n', patch)

    def test_only_sibling_worktrees_are_in_scope(self):
        (self.base / 'repo--plain').mkdir()
        (self.base / 'repo--plain' / 'x.txt').write_text('a plain directory\n')
        neighbour = self.base / 'neighbour'
        sh(self.root, 'git', 'worktree', 'add', '-q', str(neighbour), '-b', 'neighbour')
        os.symlink(neighbour, self.base / 'repo--link')
        self.new_turn()
        (neighbour / 'a.txt').write_text('elsewhere\n')
        (self.base / 'repo--plain' / 'x.txt').write_text('still plain\n')
        self.assertEqual(self.s.stop(), {})
        self.assertEqual(self.s.state()['cursor']['trees'], [''])

    def test_a_pruned_siblings_leftover_is_skipped(self):
        wt = self.base / 'repo--gone'
        sh(self.root, 'git', 'worktree', 'add', '-q', str(wt), '-b', 'gone')
        shutil.rmtree(self.root / '.git' / 'worktrees' / 'repo--gone')
        self.new_turn()
        (wt / 'a.txt').write_text('no longer a worktree\n')
        self.assertEqual(self.s.stop(), {})
        self.assertEqual(self.s.state()['errors'], [])

    @unittest.skipIf(os.name == 'nt' or os.geteuid() == 0, 'POSIX permissions, which root ignores')
    def test_a_sibling_whose_git_dir_cannot_be_read_is_a_failed_snapshot(self):
        wt = self.base / 'repo--task'
        sh(self.root, 'git', 'worktree', 'add', '-q', str(wt), '-b', 'task')
        self.new_turn()
        (wt / 'a.txt').write_text('edited\n')
        gitdir = self.root / '.git' / 'worktrees' / 'repo--task'
        gitdir.chmod(0)
        try:
            reply = self.s.stop()
        finally:
            gitdir.chmod(0o755)
        self.assertIn('challenge incomplete', reply['systemMessage'])
        self.assertEqual(self.s.calls, [])

    def test_a_sibling_with_a_git_file_that_is_no_linked_worktree_is_a_failed_snapshot(self):
        self.new_turn()
        sh(self.base, 'git', 'init', '-q', '--separate-git-dir', str(self.base / 'sep.git'), str(self.base / 'repo--sep'))
        self.write('a.txt', 'a2\n')
        reply = self.s.stop()
        self.assertIn('challenge incomplete', reply['systemMessage'])
        self.assertIn('a primary checkout', reply['systemMessage'])

    def test_an_unchanged_clean_tree_reuses_its_entries_and_any_change_reads_it_again(self):
        (self.root / '.git' / 'info' / 'exclude').write_text('.worktrees/\n')  # else root lists it untracked
        sh(self.root, 'git', 'worktree', 'add', '-q', str(self.root / '.worktrees' / 'task'), '-b', 'task')
        sh(self.root, 'git', 'worktree', 'add', '-q', str(self.base / 'repo--side'), '-b', 'side')
        self.write('s.txt', 'staged, never committed\n')
        sh(self.root, 'git', 'add', 's.txt')
        blobs = self.base / 'cache' / 'blobs'
        blobs.mkdir(parents=True)
        real, read = gate.git, []
        def git(tree, *args):
            if args[:2] == ('ls-files', '--stage'):
                read.append(Path(tree).name)
            return real(tree, *args)
        def snapshot(previous):
            read.clear()
            with mock.patch.object(gate, 'git', git):
                snap = gate.snapshot(self.root, blobs, previous)
            self.assertEqual(snap, gate.snapshot(self.root, blobs), 'the same as a full snapshot')
            return snap, sorted(read)
        index = self.root / '.git' / 'index'
        sh(self.root, 'git', 'config', 'core.trustctime', 'false')  # so restoring the times makes a.txt clean
        st = (self.root / 'a.txt').stat()
        stat = (st.st_atime_ns, st.st_mtime_ns)
        prev, _ = snapshot(None)
        for change, reread in (
                (lambda: None, []),
                (lambda: sh(self.root, 'git', 'commit', '-qm', 'staged'), ['repo']),  # HEAD
                (lambda: (shutil.copy2(index, index.with_name('copy')), os.replace(index.with_name('copy'), index)),
                 ['repo']),  # the index file, same content
                (lambda: self.write('a.txt', 'b\n'), ['repo']),
                (lambda: (self.write('a.txt', 'a\n'), os.utime(self.root / 'a.txt', ns=stat[:2])),
                 ['repo']),  # clean again, index untouched, but it was not clean last time
                (lambda: (self.base / 'repo--side' / 'new.txt').write_text('untracked\n'), ['repo--side']),
                (lambda: None, ['repo--side']),
                (lambda: sh(self.root, 'git', 'worktree', 'remove', '--force', str(self.base / 'repo--side')), [])):
            change()
            prev, got = snapshot(prev)
            self.assertEqual(got, reread)
        self.assertIn('s.txt', prev['files'])

    def test_a_plugin_that_cannot_load_is_a_failed_snapshot(self):
        self.write('a.txt', 'a2\n')
        with mock.patch.object(gate, 'PLUGIN', self.base / 'missing.py'):
            reply = self.s.stop()
        self.assertIn('cannot load the worktree-children plugin', reply['systemMessage'])
        self.assertEqual(self.s.calls, [])

    def test_a_worktree_removed_after_its_edit_keeps_the_edit(self):
        wt = self.root / '.worktrees' / 'task'
        sh(self.root, 'git', 'worktree', 'add', '-q', str(wt), '-b', 'task')
        (wt / 'a.txt').write_text('edited\n')
        self.review()
        self.assertIn('-a\n+edited\n', self.s.prompts[0])
        sh(self.root, 'git', 'worktree', 'remove', '--force', str(wt))
        self.assertEqual(self.s.stop(), {}, 'removing the worktree is not a deletion of its files')

    def test_a_worktree_of_a_bare_repository_is_tracked(self):
        bare = self.base / 'bare.git'
        sh(self.root, 'git', 'clone', '-q', '--bare', str(self.root), str(bare))
        root = self.base / 'linked'
        sh(bare, 'git', 'worktree', 'add', '-q', str(root), 'main')
        (bare / 'simplify-gate').touch()
        s = Session(root, self.base / 'linked-state')
        s.prompt()
        (root / 'a.txt').write_text('a2\n')
        s.results = [json.dumps({'findings': []})]
        self.assertIn('no findings', s.stop()['systemMessage'])
        self.assertEqual(s.state()['errors'], [])

    def test_crlf_conversion_is_not_a_change(self):
        (self.root / '.gitattributes').write_text('* text eol=crlf\n')
        sh(self.root, 'git', 'add', '.gitattributes')
        sh(self.root, 'git', 'commit', '-qm', 'crlf')
        (self.root / 'a.txt').write_bytes(b'a\r\n')  # the worktree form; the index holds LF
        self.new_turn()
        sh(self.root, 'git', 'add', 'a.txt')
        self.assertEqual(self.s.stop(), {}, 'staging a file that only differs in eol')
        (self.root / 'a.txt').write_bytes(b'a\r\nb\r\n')
        self.review()
        self.assertIn('+b\n', self.s.prompts[0], 'rendered as stored')
        self.assertNotIn('-a', self.s.prompts[0])

    @unittest.skipIf(os.name == 'nt', 'newline, quote and tab are not allowed in Windows file names')
    def test_awkward_path_names_are_recorded(self):
        names = ['line\nbreak.txt', '"quoted".txt', 'tab\tbed.txt']
        for n in names:
            (self.root / n).write_text('x\n')
        self.review()
        for n in names:
            self.assertIn(json.dumps(n), self.s.prompts[0])
        self.assertEqual(self.s.state()['errors'], [])

    def test_a_clean_filter_renders_each_file_as_stored(self):
        sh(self.root, 'git', 'config', 'filter.lower.clean', 'tr A-Z a-z')
        (self.root / '.gitattributes').write_text('*.txt filter=lower\n')
        sh(self.root, 'git', 'add', '.gitattributes')
        sh(self.root, 'git', 'commit', '-qm', 'filter')
        self.new_turn()
        (self.root / 'x.txt').write_text('HELLO\n')
        (self.root / 'y.txt').write_text('hello\n')
        self.review()
        self.assertEqual(self.s.prompts[0].count('+hello\n'), 2)
        self.assertNotIn('HELLO', self.s.prompts[0])

    def test_a_missing_final_newline_keeps_diff_lines_apart(self):
        (self.root / 'a.txt').write_text('old')
        sh(self.root, 'git', 'commit', '-qam', 'no newline')
        self.new_turn()
        (self.root / 'a.txt').write_text('new')
        self.review()
        patch = self.s.prompts[0]
        self.assertIn('-old\n', patch)
        self.assertIn('+new\n', patch)
        self.assertNotIn('-old+new', patch)
        self.assertIn('No newline at end of file', patch)

    def test_a_gc_between_prompt_and_stop_does_not_lose_the_baseline(self):
        (self.root / 'a.txt').write_text('uncommitted before the turn\n')
        (self.root / 's.txt').write_text('staged, never committed\n')
        sh(self.root, 'git', 'add', 's.txt')
        self.new_turn()
        self.write('a.txt', 'edited\n')
        sh(self.root, 'git', 'rm', '-qf', 's.txt')
        sh(self.root, 'git', 'gc', '-q', '--prune=now')
        self.review()
        self.assertEqual(self.s.state()['errors'], [])
        self.assertIn('-uncommitted before the turn\n+edited\n', self.s.prompts[0])
        self.assertIn('-staged, never committed\n', self.s.prompts[0])

    def test_a_gc_does_not_lose_a_worktree_baseline_blob(self):
        (self.root / 'a.txt').write_text('only on a dangling commit\n')
        sh(self.root, 'git', 'commit', '-qam', 'dangling')
        dangling = sh(self.root, 'git', 'rev-parse', 'HEAD').strip()
        sh(self.root, 'git', 'reset', '-q', '--hard', 'HEAD~1')
        self.new_turn()
        wt = self.root / '.worktrees' / 'task'
        sh(self.root, 'git', 'worktree', 'add', '-q', '--detach', str(wt), dangling)
        (wt / 'a.txt').unlink()
        self.s.results = [OSError('no codex'), OSError('no codex')]
        self.s.stop()  # the batch stays queued with its blobs cached
        sh(self.root, 'git', 'worktree', 'remove', '--force', str(wt))
        sh(self.root, 'git', 'reflog', 'expire', '--expire=now', '--all')
        sh(self.root, 'git', 'gc', '-q', '--prune=now')
        self.assertIn('-only on a dangling commit\n', self.s.patch())

    # --- turns and batches -----------------------------------------------------

    def test_a_peer_edit_during_the_turn_is_in_scope_and_named(self):
        self.write('a.txt', 'mine\n')
        self.write('c.txt', 'peer wrote this\n')
        reply = self.review([FINDING])
        self.assertIn('peer wrote this', self.s.prompts[0])
        self.assertIn('a coworker acting for you', reply['reason'], 'commissioned edits are ours to defend')
        self.assertIn('a peer sharing the checkout may have made some', self.s.prompts[0])

    def test_an_interrupted_turn_keeps_its_edits_for_the_next_review(self):
        self.write('a.txt', 'edited, then the user interrupted\n')
        self.s.prompt()  # no Stop in between
        self.write('b.txt', 'b\n')
        self.review()
        patch = self.s.prompts[0]
        self.assertIn('+edited, then the user interrupted\n', patch)
        self.assertIn('+b\n', patch)

    def test_corrections_after_a_block_survive_an_interruption(self):
        self.write('a.txt', 'a2\n')
        self.review([FINDING])
        self.write('a.txt', 'corrected\n')  # then the user interrupts before Stop
        self.s.prompt()
        self.write('b.txt', 'b\n')
        self.review()
        self.assertIn('-a2\n+corrected\n', self.s.prompts[-1])

    def test_batches_of_a_reviewer_that_died_are_reviewed_again(self):
        self.write('a.txt', 'a2\n')
        def die():
            raise KeyboardInterrupt  # the hook process is killed mid-review
        self.s.on_codex = die
        with self.assertRaises(KeyboardInterrupt):
            self.review()
        self.s.on_codex = None
        self.review()
        self.assertIn('-a\n+a2\n', self.s.prompts[-1])

    def test_a_stop_while_a_review_runs_keeps_its_edits_queued(self):
        self.write('a.txt', 'a2\n')
        def prompt_edit_stop():
            self.s.prompt()
            self.write('b.txt', 'b\n')
            self.assertIn('still running', self.s.stop()['systemMessage'])
        self.s.on_codex = prompt_edit_stop
        outer = self.review([FINDING])
        self.s.on_codex = None
        self.assertIn('round 1 of 2', outer['reason'], 'the round reported is the one reviewed')
        self.assertEqual(len(self.s.calls), 1, 'one review at a time')
        self.s.prompt()
        self.review()
        self.assertIn('+b\n', self.s.prompts[-1], 'the queued edit is reviewed at the next Stop')
        self.assertNotIn('a2', self.s.prompts[-1].split('Changes seen')[-1], 'the reviewed edit is not sent again')

    def test_edits_between_turns_are_not_this_turns_scope(self):
        self.s.stop()
        self.write('a.txt', 'between turns\n')
        self.s.prompt()
        self.assertEqual(self.s.stop(), {})

    def test_unreviewed_edits_carry_over_to_the_next_turn(self):
        self.write('a.txt', 'a2\n')
        self.review([FINDING])
        self.write('a.txt', 'a3\n')
        self.review([FINDING])  # round 2, final
        self.write('a.txt', 'a4\n')
        self.assertIn('stay queued', self.s.stop()['systemMessage'])
        self.new_turn()
        self.write('b.txt', 'b\n')
        self.review()
        patch = self.s.prompts[-1]
        self.assertIn('-a3\n+a4\n', patch, 'the edit after the round limit is reviewed now')
        self.assertIn('+b\n', patch)
        self.assertEqual(self.s.state()['batches'], [])

    def test_a_failed_review_keeps_its_batches_for_the_next_turn(self):
        self.write('a.txt', 'a2\n')
        self.s.results = [subprocess.TimeoutExpired('codex', 1), OSError('no codex')]
        reply = self.s.stop()
        self.assertNotIn('decision', reply)
        self.assertIn('failed twice', reply['systemMessage'])
        self.assertIn('incomplete', self.s.stop()['systemMessage'], 'the error stays visible this turn')
        self.assertEqual(len(self.s.calls), 2)
        self.new_turn()
        self.assertIn('no findings', self.review()['systemMessage'])
        self.assertIn('-a\n+a2\n', self.s.prompts[-1], 'the unreviewed batch was retried')

    def test_an_oversized_prompt_is_skipped_without_calling_codex_or_retrying_later(self):
        self.write('a.txt', 'a2\n')
        with mock.patch.object(gate, 'PROMPT_LIMIT', 100):
            reply = self.s.stop()
        self.assertNotIn('decision', reply)
        self.assertRegex(reply['systemMessage'], r'challenge skipped: prompt has \d+ characters, over the 100')
        self.assertEqual(self.s.calls, [])
        self.assertEqual(self.s.state()['batches'], [])
        self.new_turn()
        self.write('a.txt', 'a3\n')
        self.assertIn('no findings', self.review()['systemMessage'])
        self.assertNotIn('-a\n+a2\n', self.s.prompts[-1], 'the skipped batch is not replayed')

    def test_a_skipped_challenge_still_reports_a_failed_snapshot(self):
        self.write('a.txt', 'a2\n')
        real_snapshot, taken = gate.snapshot, []
        def second_fails(root, blobs, previous=None):
            taken.append(1)
            if len(taken) > 1:
                raise ValueError('index is unmerged')
            return real_snapshot(root, blobs, previous)
        with mock.patch.object(gate, 'PROMPT_LIMIT', 100), mock.patch.object(gate, 'snapshot', second_fails):
            message = self.s.stop()['systemMessage']
        self.assertIn('could not be captured: index is unmerged', message)
        self.assertIn('challenge skipped', message)

    def test_a_failure_rendering_the_patch_is_not_silent(self):
        self.write('a.txt', 'a2\n')
        with mock.patch.object(gate, 'patch_text', side_effect=OSError('blob store gone')):
            reply = self.s.stop()
        self.assertIn('blob store gone', reply['systemMessage'])
        self.assertEqual(self.s.calls, [])
        self.new_turn()
        self.assertIn('no findings', self.review()['systemMessage'])

    def test_discontinuous_batches_still_show_start_and_end(self):
        self.write('a.txt', 'mine\n')
        self.s.results = [OSError('no codex'), OSError('no codex')]
        self.s.stop()
        self.write('a.txt', 'peer between turns\n')
        self.new_turn()
        self.write('a.txt', 'mine again\n')
        self.review()
        self.assertIn('-a\n+mine again\n', self.s.prompts[-1])

    # --- rounds ----------------------------------------------------------------

    def test_findings_block_then_second_round_is_final(self):
        self.write('a.txt', 'a2\n')
        reply = self.review([FINDING])
        self.assertEqual(reply['decision'], 'block')
        self.assertIn('round 1 of 2', reply['reason'])
        self.assertIn('a.txt:1: dead branch Alternative: drop it', reply['reason'])
        cmd = self.s.calls[0]
        self.assertIn('--sandbox', cmd)
        self.assertIn('read-only', cmd)
        self.write('a.txt', 'a3\n')
        reply = self.review([FINDING])
        self.assertIn('round 2, final', reply['reason'])
        self.write('a.txt', 'a4\n')
        self.assertIn('round limit reached', self.s.stop()['systemMessage'])
        self.assertEqual(len(self.s.calls), 2)

    def test_previous_findings_and_the_reply_reach_the_follow_up(self):
        self.write('a.txt', 'a2\n')
        self.review([FINDING])
        self.write('a.txt', 'a3\n')
        reply = self.review(message='kept the branch because callers rely on it')
        self.assertIn('dead branch', self.s.prompts[1])
        self.assertIn('callers rely on it', self.s.prompts[1])
        self.assertIn('no findings', reply['systemMessage'])

    def test_a_rejection_from_an_earlier_turn_still_reaches_the_reviewer(self):
        self.write('a.txt', 'a2\n')
        self.review([FINDING])
        self.assertEqual(self.s.stop('rejected: callers rely on that branch'), {})
        self.new_turn()
        self.write('b.txt', 'b\n')
        self.review(message='added b')
        self.assertIn('callers rely on that branch', self.s.prompts[-1])
        self.assertIn('added b', self.s.prompts[-1])
        self.assertEqual(self.s.state()['replies'], [], 'a new verdict starts new replies')

    def test_the_fed_back_challenge_is_not_a_new_turn(self):
        self.write('a.txt', 'a2\n')
        reply = self.review([FINDING])
        self.s.prompt('Stop hook feedback:\n' + reply['reason'])
        self.assertEqual(self.s.state()['rounds'], 1, 'the block fed back as a prompt keeps the budget')
        self.write('a.txt', 'a3\n')
        self.assertIn('round 2, final', self.review([FINDING])['reason'])

    def test_a_rejection_without_edits_does_not_rerun(self):
        self.write('a.txt', 'a2\n')
        self.assertEqual(self.review([FINDING])['decision'], 'block')
        self.assertEqual(self.s.stop('I reject it because'), {})
        self.assertEqual(len(self.s.calls), 1)

    def test_a_new_user_turn_resets_the_round_budget(self):
        self.write('a.txt', 'a2\n')
        self.review([FINDING])
        self.write('a.txt', 'a3\n')
        self.review([FINDING])
        self.new_turn()
        self.assertEqual(self.s.state()['rounds'], 0)
        self.assertEqual(self.s.stop(), {}, 'nothing new since the last review')
        self.write('b.txt', 'b\n')
        self.review()
        self.assertEqual(len(self.s.calls), 1, 'one review this turn')

    def test_the_lock_is_free_while_codex_runs(self):
        self.write('a.txt', 'a2\n')
        def take_state_lock():
            with (self.s.directory / 'lock').open('w') as lock:
                gate.lock_exclusive(lock, block=False)  # raises if Stop still holds it
        self.s.on_codex = take_state_lock
        self.assertIn('no findings', self.review()['systemMessage'])

    def test_the_state_is_written_while_another_process_reads_it(self):
        reader = open(self.s.directory / 'state.json', 'rb')  # as prune reads it, before it locks
        self.addCleanup(reader.close)
        threading.Timer(0.3, reader.close).start()
        with gate.Locked(self.s.directory) as state:
            state['notes'] = ['written']
        self.assertEqual(self.s.state()['notes'], ['written'])
        self.assertFalse((self.s.directory / 'state.tmp').exists())

    def test_codex_is_found_on_path_alone(self):
        if os.name != 'nt':
            self.assertEqual(gate.codex_exe(), 'codex', 'POSIX execs it by name, as before')
            return
        base = self.base
        for name in ('cwd/codex.exe', 'cwd/bin/codex.exe', 'cwd/rooted/codex.exe', 'first/codex.cmd',
                     'second/codex.exe', 'second/codex.cmd'):
            (base / name).parent.mkdir(parents=True, exist_ok=True)
            (base / name).write_text('never run')
        first, second = str(base / 'first'), str(base / 'second')
        rooted = os.path.splitdrive(base / 'cwd' / 'rooted')[1]  # no drive: the cwd's drive decides
        cwd = os.getcwd()
        os.chdir(base / 'cwd')
        try:
            with mock.patch.dict(os.environ, {'PATH': os.pathsep.join(['', '.', 'bin', rooted, first, second])}):
                os.environ.pop('NoDefaultCurrentDirectoryInExePath', None)
                self.assertEqual(gate.codex_exe(), os.path.join(first, 'codex.cmd'), 'PATH order, never the cwd or a relative entry')
        finally:
            os.chdir(cwd)
        with mock.patch.dict(os.environ, {'PATH': second}):
            self.assertEqual(gate.codex_exe(), os.path.join(second, 'codex.exe'), '.exe before .cmd')
        with mock.patch.dict(os.environ, {'PATH': str(base / 'none')}), self.assertRaises(FileNotFoundError):
            gate.codex_exe()

    @unittest.skipUnless(os.name == 'nt', 'POSIX kills the direct child alone, as it always has')
    def test_a_timed_out_codex_is_killed_with_its_children(self):
        late = self.base / 'late'
        child = f'import time; time.sleep(2); open({str(late)!r}, "w").close()'
        parent = f'import subprocess, sys, time; subprocess.Popen([sys.executable, "-c", {child!r}]); time.sleep(30)'
        with self.assertRaises(subprocess.TimeoutExpired):
            gate.run_tree([sys.executable, '-c', parent], input=b'', timeout=1,
                          stdout=subprocess.DEVNULL, stderr=subprocess.DEVNULL)
        time.sleep(2.5)
        self.assertFalse(late.exists(), 'a grandchild, as node under codex.cmd, outlived the timeout')

    def test_an_interrupted_codex_is_killed_with_its_children(self):
        # the Windows path, run here with taskkill stood in for: any failure, not only a timeout, kills the tree
        killed = []

        def taskkill(args, **kw):
            killed.append(args[1:3])
            os.kill(int(args[-1]), signal.SIGTERM)
        with mock.patch.object(gate.os, 'name', 'nt'), mock.patch.dict(os.environ, {'SystemRoot': 'C:\\Windows'}), \
                mock.patch.object(subprocess, 'run', taskkill), \
                mock.patch.object(subprocess.Popen, 'communicate', side_effect=KeyboardInterrupt), \
                self.assertRaises(KeyboardInterrupt):
            gate.run_tree([sys.executable, '-c', 'import time; time.sleep(30)'], input=b'', timeout=30)
        self.assertEqual(killed, [['/T', '/F']])

    @unittest.skipUnless(os.name == 'nt', 'msvcrt locks are Windows only')
    def test_a_lock_failure_other_than_contention_is_not_waited_out(self):
        failure = OSError(errno.EBADF, 'bad file descriptor')
        with (self.s.directory / 'lock').open('w') as lock, \
                mock.patch.object(gate.msvcrt, 'locking', side_effect=[failure, AssertionError('retried')]), \
                self.assertRaises(OSError) as raised:
            gate.lock_exclusive(lock)
        self.assertEqual(raised.exception.errno, errno.EBADF)

    @unittest.skipUnless(os.name == 'nt', 'msvcrt locks are Windows only')
    def test_a_lock_is_released_before_its_handle_closes(self):
        with mock.patch.object(gate.msvcrt, 'locking', wraps=gate.msvcrt.locking) as locking:
            with gate.Locked(self.s.directory):
                pass
        self.assertEqual([c.args[1] for c in locking.call_args_list], [gate.msvcrt.LK_NBLCK, gate.msvcrt.LK_UNLCK])

    def test_edits_made_during_a_clean_review_are_reviewed_next(self):
        self.write('a.txt', 'a2\n')
        self.s.on_codex = lambda: self.write('b.txt', 'written during the review\n')
        self.assertIn('no findings', self.review()['systemMessage'])
        self.s.on_codex = None
        self.assertEqual(len(self.s.state()['batches']), 1, 'the review-time edit waits for the next review')
        self.s.prompt()
        self.review()
        self.assertIn('written during the review', self.s.prompts[1])
        self.assertNotIn('a2', self.s.prompts[1], 'the reviewed edit is not sent again')

    def test_a_review_ending_after_a_new_turn_began_does_not_close_that_turn(self):
        self.write('a.txt', 'a2\n')
        self.s.on_codex = self.s.prompt
        self.review()
        self.s.on_codex = None
        self.write('b.txt', 'b\n')
        self.s.prompt()  # the new turn is interrupted before its Stop
        self.review()
        self.assertIn('+b\n', self.s.prompts[-1], 'the interrupted turn\'s edit is still in scope')

    def test_edits_made_during_a_review_survive_a_prompt_before_it_ends(self):
        self.write('a.txt', 'a2\n')
        def edit_then_prompt():
            self.write('b.txt', 'written during the review\n')
            self.s.prompt()
        self.s.on_codex = edit_then_prompt
        self.review()
        self.s.on_codex = None
        self.review()
        self.assertIn('written during the review', self.s.prompts[1])

    def test_edits_missed_by_a_failed_snapshot_are_reported_and_queued_at_the_next_prompt(self):
        self.write('a.txt', 'a2\n')
        real_snapshot = gate.snapshot
        def snapshot_failing_after_codex(root, blobs, previous=None):
            if self.s.calls:
                raise ValueError('index is unmerged')
            return real_snapshot(root, blobs, previous)
        self.s.on_codex = lambda: self.write('b.txt', 'written during the review\n')
        with mock.patch.object(gate, 'snapshot', snapshot_failing_after_codex):
            reply = self.review()
        self.s.on_codex = None
        self.assertIn('could not be captured: index is unmerged', reply['systemMessage'])
        self.s.prompt()
        self.review()
        self.assertIn('written during the review', self.s.prompts[1])

    def test_a_stop_before_any_prompt_takes_a_baseline_and_says_so(self):
        s = Session(self.root, self.base / 'fresh')
        self.write('a.txt', 'before the gate\n')
        self.assertIn('baseline taken', s.stop()['systemMessage'])
        s.prompt()
        self.assertEqual(s.stop(), {})

    # --- infrastructure --------------------------------------------------------

    def test_invalid_or_out_of_scope_results_count_as_failures(self):
        self.write('a.txt', 'a2\n')
        self.s.results = ['not json', json.dumps({'findings': [{**FINDING, 'file': 'c.txt'}]})]
        reply = self.s.stop()
        self.assertIn('incomplete', reply['systemMessage'])
        self.assertIn('outside review scope', reply['systemMessage'])

    def test_marker_overrides_drive_the_codex_command(self):
        self.write('a.txt', 'a2\n')
        self.review()
        cmd = self.s.calls[0]
        self.assertEqual(cmd[cmd.index('-m') + 1], gate.MODEL)
        self.assertIn(f'model_reasoning_effort="{gate.EFFORT}"', cmd)
        # The challenger runs without memories, plugins or the skills index; AGENTS.md is not switched off.
        self.assertEqual(cmd[cmd.index('--disable'):cmd.index('--output-schema')],
                         ['--disable', 'memories', '--disable', 'plugins', '-c', 'skills.include_instructions=false'])
        (self.root / '.git' / 'simplify-gate').write_text('# repo override\nmodel = spark\neffort=low\n')
        self.write('a.txt', 'a3\n')
        self.review()
        cmd = self.s.calls[1]
        self.assertEqual(cmd[cmd.index('-m') + 1], 'spark')
        self.assertIn('model_reasoning_effort="low"', cmd)

    def test_a_bad_marker_is_reported_not_defaulted(self):
        (self.root / '.git' / 'simplify-gate').write_text('effort=extreme\n')
        self.write('a.txt', 'a2\n')
        reply = self.s.stop()
        self.assertIn('effort must be one of', reply['systemMessage'])
        self.assertEqual(self.s.calls, [])
        for bad in ('model\n', 'rounds=3\n', 'model=\n'):
            (self.root / '.git' / 'simplify-gate').write_text(bad)
            with self.assertRaises(ValueError):
                gate.read_marker(self.root / '.git' / 'simplify-gate')

    def test_validate_result_shape(self):
        ok = {'findings': [FINDING]}
        self.assertEqual(gate.validate_result(ok, {'a.txt'}), [FINDING])
        for bad in ({'findings': 'x'}, {'findings': [{**FINDING, 'extra': 1}]},
                    {'findings': [{**FINDING, 'line': 0}]}, {'findings': [{**FINDING, 'alternative': ' '}]}, []):
            with self.assertRaises(ValueError):
                gate.validate_result(bad, {'a.txt'})

    def test_a_block_reply_carries_the_notes(self):
        (self.s.directory / 'state.json').write_text(json.dumps({'pending': {}}))
        self.s.prompt()
        self.write('a.txt', 'a2\n')
        self.assertIn('older gate version', self.review([FINDING])['reason'])

    def test_state_from_an_older_gate_version_is_discarded_with_a_notice(self):
        (self.s.directory / 'state.json').write_text(json.dumps({
            'changes': {}, 'pending': {'old': {'a.txt': ['100644', 'x']}}, 'rounds': 0,
            'reviewed': None, 'findings': [], 'errors': []}))
        reply = self.s.stop()
        self.assertIn('older gate version', reply['systemMessage'])
        self.assertIn('baseline taken', reply['systemMessage'])
        self.assertEqual(set(self.s.state()), set(gate.new_state()))
        self.new_turn()
        self.assertEqual(self.s.stop(), {})


DAY = 86400


class PruneTest(unittest.TestCase):
    def setUp(self):
        self.base = tmpdir(self)  # as git reports it
        self.cache = self.base / 'cache'
        self.store = self.cache / gate.STORE

    def blob(self, text):
        content = text.encode()
        return gate.git_blob_id(content, 40), content

    def session(self, name, age_days=0, blobs=(), cursor=(), files=(), **state):
        """A session as an earlier gate left it: plain blob copies, `cursor`
        referencing the given blob ids, everything last touched `age_days` ago."""
        directory = self.cache / name
        (directory / 'blobs').mkdir(parents=True)
        for sha, content in blobs:
            (directory / 'blobs' / sha).write_bytes(content)
        entries = {f'f{i}': ['100644', sha] for i, sha in enumerate(cursor)}
        (directory / 'state.json').write_text(json.dumps({
            **gate.new_state(), 'cursor': {'files': entries, 'trees': ['']}, **state}))
        (directory / 'lock').touch()
        for name in files:
            (directory / name).write_bytes(b'log\n')  # untranslated: sizes are asserted
        self.age(directory, age_days)
        return directory

    def age(self, directory, days):
        when = time.time() - days * DAY
        for path in [*directory.iterdir(), directory]:
            os.utime(path, (when, when))

    def prune(self, *args, **kw):
        return gate.prune(self.cache, *args, **kw)

    def test_new_writes_share_one_store_copy(self):
        root = make_repo(self.base / 'repo')
        (root / 'big.txt').write_text('untracked\n' * 1000)
        sha = self.blob('untracked\n' * 1000)[0]
        for name in ('s1', 's2'):
            Session(root, self.cache / name).prompt()
        shared = self.store / sha
        self.assertTrue(shared.exists())
        self.assertTrue((self.cache / 's1' / 'blobs' / sha).samefile(self.cache / 's2' / 'blobs' / sha))
        self.assertEqual(shared.stat().st_nlink, 3)
        self.assertEqual([p.name for p in self.store.iterdir()], [sha], 'no partial write is left behind')
        self.assertIn(gate.STORE, os.listdir(self.cache))
        shared.unlink()  # pruned between two events: the next session stores it again
        Session(root, self.cache / 's3').prompt()
        self.assertTrue((self.cache / 's3' / 'blobs' / sha).samefile(shared))
        with self.assertRaises(ValueError):
            gate.publish(self.store / ('0' * 40), self.base / 'target', b'not that blob')

    def test_bytes_just_hashed_are_stored_without_a_cat_file_unless_a_filter_changed_them(self):
        root = make_repo(self.base / 'repo')
        sha, content = self.blob('stored as hashed\n')
        blobs = self.cache / 's1' / 'blobs'
        blobs.mkdir(parents=True)
        with mock.patch.object(gate, 'git', wraps=gate.git) as git:
            gate.keep_blob(root, blobs, ['100644', sha], content)
        self.assertEqual(git.call_count, 0)
        self.assertEqual((blobs / sha).read_bytes(), content)
        stored = subprocess.run(['git', 'hash-object', '-w', '--stdin'], cwd=root, input=b'as git keeps it\n',
                                check=True, capture_output=True).stdout.decode().strip()
        other = self.cache / 's2' / 'blobs'
        other.mkdir(parents=True)
        with mock.patch.object(gate, 'git', wraps=gate.git) as git:
            gate.keep_blob(root, other, ['100644', stored], b'AS THE WORKTREE HAS IT\n')
        git.assert_called_once_with(root, 'cat-file', 'blob', stored)
        self.assertEqual((other / stored).read_bytes(), b'as git keeps it\n')

    def test_a_damaged_store_copy_is_not_handed_to_a_new_session(self):
        root = make_repo(self.base / 'repo')
        (root / 'big.txt').write_text('untracked\n')
        sha = self.blob('untracked\n')[0]
        self.store.mkdir(parents=True)
        (self.store / sha).write_bytes(b'damaged\n')
        s = Session(root, self.cache / 's1')
        s.prompt()
        self.assertEqual((s.directory / 'blobs' / sha).read_text(), 'untracked\n')
        self.assertEqual((self.store / sha).read_bytes(), b'damaged\n', 'left for prune to report')

    def test_a_concurrent_prune_cannot_take_a_store_copy_being_published(self):
        root = make_repo(self.base / 'repo')
        (root / 'big.txt').write_text('untracked\n')
        sha = self.blob('untracked\n')[0]
        real_link, pruning = os.link, []
        def link_after_a_prune(*args, **kw):
            # A prune pass lands before every link the hook makes, in each window publication has.
            if not pruning:
                pruning.append(1)
                self.prune(apply=True)
                pruning.clear()
            return real_link(*args, **kw)
        s = Session(root, self.cache / 's1')
        with mock.patch.object(gate.os, 'link', link_after_a_prune):
            s.prompt()
            Session(root, self.cache / 's2').prompt()
        for name in ('s1', 's2'):
            self.assertEqual((self.cache / name / 'blobs' / sha).read_text(), 'untracked\n')
        self.assertTrue((self.cache / 's2' / 'blobs' / sha).samefile(self.store / sha))

    def test_a_corrupt_store_copy_is_never_linked_over_a_good_one(self):
        sha, content = self.blob('good')
        a = self.session('a', blobs=[(sha, content)], cursor=[sha])
        self.store.mkdir()
        (self.store / sha).write_bytes(b'bad')
        report = self.prune(apply=True)
        self.assertIn(f'corrupt blobs (left in place): 1\n  {self.store / sha}', report)
        self.assertEqual((a / 'blobs' / sha).read_bytes(), content)
        self.assertEqual((a / 'blobs' / sha).stat().st_nlink, 1)
        self.assertEqual((self.store / sha).read_bytes(), b'bad')

    def test_symlinks_are_reported_never_followed(self):
        outside = self.base / 'outside'
        outside.mkdir()
        victim = outside / ('1' * 40)
        victim.write_text('unrelated\n')
        (outside / 'log').write_text('unrelated log\n')
        sha, content = self.blob('kept')
        linked_blobs = self.session('linked-blobs', age_days=40)
        shutil.rmtree(linked_blobs / 'blobs')
        (linked_blobs / 'blobs').symlink_to(outside)
        linked_log = self.session('linked-log', age_days=40, files=['codex-1-0.log'])
        (linked_log / 'codex-2-0.log').symlink_to(outside / 'log')
        linked_blob = self.session('linked-blob', age_days=40, blobs=[(sha, content)])
        (linked_blob / 'blobs' / ('2' * 40)).symlink_to(victim)
        real = self.session('real', age_days=40, files=['codex-1-0.log'])
        (self.cache / 'linked-session').symlink_to(real)
        for directory in (linked_blobs, linked_log, linked_blob):
            self.age(directory, 40)
        report = self.prune(apply=True)
        self.assertIn('symlinked or not a directory (left untouched): 4', report)
        self.assertEqual(victim.read_text(), 'unrelated\n')
        self.assertEqual((outside / 'log').read_text(), 'unrelated log\n')
        self.assertTrue((linked_log / 'codex-1-0.log').exists(), 'a session holding a symlink is left whole')
        self.assertTrue((linked_blob / 'blobs' / sha).exists())
        self.assertTrue((self.cache / 'linked-session').is_symlink())
        self.assertFalse((real / 'codex-1-0.log').exists(), 'the real session is pruned once, by its own name')

    def test_a_symlinked_lock_file_is_refused_without_o_nofollow(self):
        directory = self.session('linked-lock')
        (directory / 'lock').unlink()
        (directory / 'lock').symlink_to(self.base / 'elsewhere')
        with mock.patch.object(gate.os, 'O_NOFOLLOW', 0, create=True), self.assertRaises(OSError) as raised:
            with gate.held(directory / 'lock', True):
                pass
        self.assertEqual(raised.exception.errno, errno.ELOOP)
        self.assertFalse((self.base / 'elsewhere').exists(), 'nothing created through the link')

    def test_a_symlinked_store_is_reported_and_left_alone(self):
        outside = self.base / 'outside'
        outside.mkdir()
        sha, content = self.blob('shared')
        (outside / sha).write_bytes(b'not the blob')
        (outside / ('3' * 40)).write_text('unrelated\n')
        self.cache.mkdir()
        self.store.symlink_to(outside)
        a = self.session('a', blobs=[(sha, content)], cursor=[sha])
        report = self.prune(apply=True)
        self.assertIn(f'symlinked or not a directory (left untouched): 1\n  {self.store}', report)
        self.assertEqual(sorted(os.listdir(outside)), sorted([sha, '3' * 40]))
        self.assertEqual((a / 'blobs' / sha).read_bytes(), content)
        self.store.unlink()
        self.store.mkdir()
        (self.store / ('4' * 40)).symlink_to(outside / ('3' * 40))
        report = self.prune(apply=True)
        self.assertIn(str(self.store / ('4' * 40)), report)
        self.assertTrue((self.store / ('4' * 40)).is_symlink())
        self.assertTrue((outside / ('3' * 40)).exists())

    def test_relinking_duplicates_frees_their_bytes_and_keeps_content(self):
        sha, content = self.blob('x' * 5000)
        a = self.session('a', blobs=[(sha, content)], cursor=[sha])
        b = self.session('b', blobs=[(sha, content)], cursor=[sha], batches=[{'p': [None, ['100644', sha]]}])
        report = self.prune()
        self.assertIn('would free', report)
        self.assertRegex(report, r'relinked duplicates +4\.9 KB  \(1 file')
        self.assertFalse(self.store.exists(), 'a dry run changes nothing')
        self.assertEqual((a / 'blobs' / sha).stat().st_nlink, 1)
        report = self.prune(apply=True)
        self.assertRegex(report, r'relinked duplicates +4\.9 KB')
        for directory in (a, b):
            self.assertTrue((directory / 'blobs' / sha).samefile(self.store / sha))
            self.assertEqual((directory / 'blobs' / sha).read_bytes(), content)
        self.assertEqual((self.store / sha).stat().st_nlink, 3)
        self.assertRegex(self.prune(apply=True), r'total +0 B', 'a second pass has nothing left')

    def test_a_corrupt_copy_is_reported_not_linked(self):
        sha, _ = self.blob('real')
        a = self.session('a', blobs=[(sha, b'tampered')], cursor=[sha])
        report = self.prune(apply=True)
        self.assertIn(f'corrupt blobs (left in place): 1\n  {a / "blobs" / sha}', report)
        self.assertEqual((a / 'blobs' / sha).read_bytes(), b'tampered')
        self.assertFalse((self.store / sha).exists())

    def test_settled_idle_diagnostics_and_unreferenced_blobs_expire_only_with_apply(self):
        kept, dropped = self.blob('kept'), self.blob('dropped')
        logs = ('codex-1-0.log', 'result-1-0.json', 'schema.json')
        old = self.session('old', age_days=40, blobs=[kept, dropped], cursor=[kept[0]], files=logs, open_turn=True)
        (old / 'review.lock').touch()
        self.age(old, 40)
        recent = self.session('recent', age_days=5, blobs=[self.blob('recent')], files=logs)
        report = self.prune()
        self.assertRegex(report, r'diagnostics +12 B  \(3 file')
        self.assertRegex(report, r'unreferenced blobs +7 B  \(1 file')
        self.assertTrue(all((old / name).exists() for name in logs))
        self.assertIn(f'interrupted turn, idle over 30 days (snapshot kept): 1\n  {old}: 40 days', report)
        self.prune(apply=True)
        self.assertEqual(sorted(os.listdir(old)), ['blobs', 'lock', 'review.lock', 'state.json'])
        self.assertEqual(os.listdir(old / 'blobs'), [kept[0]])
        self.assertTrue((self.store / kept[0]).samefile(old / 'blobs' / kept[0]))
        self.assertTrue(all((recent / name).exists() for name in logs), 'a recent session keeps its logs')
        self.assertEqual(len(os.listdir(recent / 'blobs')), 1, 'and its blobs')

    def test_a_closed_idle_turn_loses_its_snapshot_and_the_blobs_only_it_held(self):
        only, shared = self.blob('only this cursor'), self.blob('shared')
        old = self.session('old', age_days=40, blobs=[only, shared], cursor=[only[0], shared[0]],
                           notes=['a note still to deliver'], rounds=2)
        live = self.session('live', age_days=1, cursor=[shared[0]])
        self.store.mkdir()
        os.link(old / 'blobs' / shared[0], self.store / shared[0])
        os.link(self.store / shared[0], live / 'blobs' / shared[0])
        self.age(old, 40)
        self.age(live, 1)
        before = {d: (d / 'state.json').read_text() for d in (old, live)}
        lock = (old / 'lock').stat().st_ino
        report = self.prune()
        self.assertEqual({d: (d / 'state.json').read_text() for d in (old, live)}, before, 'a dry run changes nothing')
        self.assertEqual(sorted(os.listdir(old / 'blobs')), sorted([only[0], shared[0]]))
        self.prune(apply=True)
        state = json.loads((old / 'state.json').read_text())
        self.assertEqual(state, {**json.loads(before[old]), 'cursor': None}, 'notes and counters kept')
        freed = len(before[old]) - (old / 'state.json').stat().st_size
        self.assertRegex(report, rf'compacted states +{freed} B  \(1 file')
        self.assertRegex(report, r'unreferenced blobs +16 B  \(2 file')
        self.assertEqual(os.listdir(old / 'blobs'), [])
        self.assertEqual((old / 'lock').stat().st_ino, lock, 'the lock file stays the one waiters hold')
        self.assertEqual((live / 'state.json').read_text(), before[live], 'a young session is untouched')
        self.assertEqual(os.listdir(self.store), [shared[0]], 'a blob another session links stays')
        self.assertTrue((self.store / shared[0]).samefile(live / 'blobs' / shared[0]))
        self.assertRegex(self.prune(apply=True), r'total +0 B')

    def test_compaction_keeps_an_interrupted_turn_and_a_closed_one_takes_a_new_baseline(self):
        root = make_repo(self.base / 'repo')
        (root / 'big.txt').write_text('untracked\n')
        sha = self.blob('untracked\n')[0]
        closed = Session(root, self.cache / 'closed')
        closed.prompt()
        (root / 'a.txt').write_text('closed turn\n')
        closed.results = [json.dumps({'findings': []})]
        closed.stop()
        interrupted = Session(root, self.cache / 'interrupted')
        interrupted.prompt()
        (root / 'c.txt').write_text('interrupted edit\n')
        for s in (closed, interrupted):
            self.age(s.directory, 40)
        locks = {s: (s.directory / 'lock').stat().st_ino for s in (closed, interrupted)}
        kept = interrupted.state()
        report = self.prune(apply=True)
        self.assertIn(f'interrupted turn, idle over 30 days (snapshot kept): 1\n  {interrupted.directory}', report)
        self.assertEqual(interrupted.state(), kept)
        self.assertIsNone(closed.state()['cursor'])
        self.assertEqual(os.listdir(closed.directory / 'blobs'), [])
        self.assertTrue((interrupted.directory / 'blobs' / sha).samefile(self.store / sha))
        self.assertEqual({s: (s.directory / 'lock').stat().st_ino for s in locks}, locks)
        interrupted.prompt()
        interrupted.results = [json.dumps({'findings': []})]
        interrupted.stop()
        self.assertIn('+interrupted edit\n', interrupted.prompts[-1])
        self.assertIn('baseline taken', closed.stop()['systemMessage'])
        closed.prompt()
        (root / 'b.txt').write_text('b\n')
        closed.results = [json.dumps({'findings': []})]
        closed.stop()
        self.assertIn('+b\n', closed.prompts[-1])
        self.assertNotIn('interrupted edit', closed.prompts[-1])

    def test_a_busy_session_is_skipped(self):
        sha, content = self.blob('busy')
        for lock in ('lock', 'review.lock'):
            directory = self.session(lock, age_days=40, blobs=[(sha, content)], files=['codex-1-0.log'])
            with open(directory / lock, 'a') as held:
                gate.lock_exclusive(held)
                report = self.prune(apply=True)
            self.assertIn(f'busy (skipped): 1\n  {directory}', report)
            self.assertTrue((directory / 'codex-1-0.log').exists())
            self.assertEqual((directory / 'blobs' / sha).stat().st_nlink, 1)
            shutil.rmtree(directory)

    def test_an_unresolved_idle_session_is_reported_and_kept(self):
        sha, content = self.blob('queued')
        directory = self.session('stuck', age_days=100, blobs=[(sha, content)], files=['codex-1-0.log'],
                                 batches=[{'p': [None, ['100644', sha]]}], errors=['codex failed twice'])
        report = self.prune(apply=True)
        self.assertIn(f'unresolved, inactive over 90 days (kept): 1\n  {directory}: 100 days; 1 batch(es)', report)
        self.assertTrue((directory / 'codex-1-0.log').exists())
        self.assertTrue((directory / 'blobs' / sha).exists())
        self.assertNotIn('unresolved', self.prune(report_days=200))

    def test_unrecognized_sessions_are_left_untouched(self):
        sha, content = self.blob('odd')
        garbled = self.session('garbled', age_days=40, blobs=[(sha, content)], files=['codex-1-0.log'])
        (garbled / 'state.json').write_text('not json')
        older = self.session('older', age_days=40, blobs=[(sha, content)], files=['codex-1-0.log'])
        (older / 'state.json').write_text(json.dumps({'pending': {}}))
        missing = self.session('missing', age_days=40, blobs=[(sha, content)])
        (missing / 'state.json').unlink()
        before = {d: sorted(os.listdir(d)) for d in (garbled, older, missing)}
        report = self.prune(apply=True)
        self.assertIn('unrecognized (left untouched): 3', report)
        for directory, names in before.items():
            self.assertEqual(sorted(os.listdir(directory)), names)
            self.assertEqual((directory / 'blobs' / sha).stat().st_nlink, 1)
        self.assertFalse(self.store.exists())

    def test_a_store_copy_goes_only_with_its_last_link(self):
        linked, orphan = self.blob('linked'), self.blob('orphan')
        directory = self.session('s', blobs=[linked], cursor=[linked[0]])
        self.store.mkdir()
        os.link(directory / 'blobs' / linked[0], self.store / linked[0])
        (self.store / orphan[0]).write_bytes(orphan[1])
        (self.store / (gate.TEMP + 'stale')).write_bytes(b'partial')
        os.utime(self.store / (gate.TEMP + 'stale'), (0, 0))
        report = self.prune()
        self.assertRegex(report, r'unused store blobs +13 B  \(2 file')
        self.assertTrue((self.store / orphan[0]).exists())
        self.prune(apply=True)
        self.assertEqual(os.listdir(self.store), [linked[0]])
        self.assertTrue((self.store / linked[0]).samefile(directory / 'blobs' / linked[0]))

    def test_a_dry_run_counts_a_store_copy_freed_by_expired_blobs(self):
        sha, content = self.blob('only old')
        directory = self.session('s', age_days=40, blobs=[(sha, content)])
        self.store.mkdir()
        os.link(directory / 'blobs' / sha, self.store / sha)
        report = self.prune()
        self.assertRegex(report, r'unreferenced blobs +0 B  \(1 file')
        self.assertRegex(report, r'unused store blobs +8 B  \(1 file')
        self.prune(apply=True)
        self.assertEqual(os.listdir(self.store), [])


class ActivationTest(unittest.TestCase):
    def setUp(self):
        self.tmp = tempfile.TemporaryDirectory()
        self.base = Path(self.tmp.name)
        self.root = make_repo(self.base / 'repo')

    def tearDown(self):
        self.tmp.cleanup()

    def run_hook(self, cwd, payload, project=None, **extra):
        stub = self.base / 'bin'  # a codex that fails, ahead of any real one
        stub.mkdir(exist_ok=True)
        if os.name == 'nt':
            (stub / 'codex.bat').write_text('@exit /b 1\n')
        else:
            (stub / 'codex').write_text('#!/bin/sh\nexit 1\n')
            (stub / 'codex').chmod(0o755)
        env = {**os.environ, 'XDG_CACHE_HOME': str(self.base / 'cache'), 'CLAUDE_PROJECT_DIR': str(project or cwd),
               'PATH': f'{stub}{os.pathsep}{os.environ["PATH"]}'}
        env.pop('COWORK_TURN', None)  # the suite itself may run inside a cowork turn
        env.update(extra)
        # Windows cannot exec a shebang script; Claude Code runs hook commands through bash there.
        wrapper = [shutil.which('sh'), WRAPPER.as_posix()] if os.name == 'nt' else [str(WRAPPER)]
        return subprocess.run(wrapper, input=json.dumps({'session_id': 's', 'cwd': str(cwd), **payload}),
                              capture_output=True, text=True, env=env, check=True)

    def test_wrapper_runs_the_gate_only_where_the_marker_exists(self):
        prompt = {'hook_event_name': 'UserPromptSubmit', 'prompt': 'hi'}
        other = make_repo(self.base / 'other')
        (other / '.git' / 'simplify-gate').unlink()
        self.assertEqual(self.run_hook(other, prompt).stdout, '')
        self.assertEqual(self.run_hook(self.base, prompt).stdout, '', 'not a checkout at all')
        self.assertFalse((self.base / 'cache').exists())
        sh(self.root, 'git', 'worktree', 'add', '-q', str(self.base / 'wt'), '-b', 'wt')
        self.run_hook(self.base / 'wt', prompt)
        self.assertTrue(list((self.base / 'cache' / 'agentrc' / 'simplify-gate').iterdir()), 'worktrees share the marker')

    def test_a_cowork_turn_is_left_alone(self):
        out = self.run_hook(self.root, {'hook_event_name': 'UserPromptSubmit', 'prompt': 'hi'}, COWORK_TURN='codex-1').stdout
        self.assertEqual(out, '')
        self.assertFalse((self.base / 'cache').exists())

    def test_a_chief_session_is_left_alone(self):
        prompt = {'hook_event_name': 'UserPromptSubmit', 'prompt': 'hi'}
        headers = {
            'chief': ['{"type":"agent-setting","agentSetting":"chief","sessionId":"s"}'],
            'titled': ['{"type":"ai-title","aiTitle":"t"}', '{"type":"agent-setting","agentSetting":"chief"}'],
            'writer': ['{"type":"agent-setting","agentSetting":"code-writer"}'],
            'late': ['{"type":"user","message":{}}', '{"type":"agent-setting","agentSetting":"chief"}'],
            'garbled': ['not json'],
            'array': ['[]', '{"type":"agent-setting","agentSetting":"chief"}'],
        }
        for name, lines in headers.items():
            path = self.base / f'{name}.jsonl'
            path.write_text('\n'.join(lines) + '\n')
            self.run_hook(self.root, {**prompt, 'transcript_path': str(path)})
            ran = (self.base / 'cache').exists()
            self.assertEqual(ran, name not in ('chief', 'titled'), name)
            shutil.rmtree(self.base / 'cache', ignore_errors=True)
        self.run_hook(self.root, {**prompt, 'transcript_path': str(self.base / 'missing.jsonl')})
        self.assertTrue((self.base / 'cache').exists(), 'an unreadable transcript gets no exemption')

    def test_the_project_dir_names_the_checkout_not_the_shell_cwd(self):
        prompt = {'hook_event_name': 'UserPromptSubmit', 'prompt': 'hi'}
        self.run_hook(self.root, prompt)
        other = make_repo(self.base / 'other')
        (other / '.git' / 'simplify-gate').unlink()
        self.run_hook(other, {'hook_event_name': 'Stop', 'last_assistant_message': ''}, project=self.root)
        state = json.loads(next((self.base / 'cache' / 'agentrc' / 'simplify-gate').glob('*/state.json')).read_text())
        self.assertEqual(state['errors'], [])
        self.assertIsNotNone(state['cursor'])

    def test_a_utf8_payload_is_read_as_utf8(self):
        prompt = {'session_id': 's', 'cwd': str(self.root), 'hook_event_name': 'UserPromptSubmit',
                  'prompt': 'caf\u00e9 \u2714 \u0151'}
        env = {**os.environ, 'XDG_CACHE_HOME': str(self.base / 'cache'), 'CLAUDE_PROJECT_DIR': str(self.root)}
        env.pop('COWORK_TURN', None)
        env.pop('PYTHONUTF8', None)
        run = subprocess.run([sys.executable, str(HOOK)], input=json.dumps(prompt, ensure_ascii=False).encode(),
                             capture_output=True, env=env)
        self.assertEqual(run.returncode, 0, run.stderr.decode(errors='replace'))
        self.assertTrue((self.base / 'cache').exists())

    def test_stop_reply_is_json_on_stdout(self):
        out = self.run_hook(self.root, {'hook_event_name': 'Stop', 'last_assistant_message': ''}).stdout
        self.assertIn('baseline taken', json.loads(out)['systemMessage'])
        out = self.run_hook(self.root, {'hook_event_name': 'Stop', 'last_assistant_message': ''}).stdout
        self.assertEqual(out, '')


class SkillTest(unittest.TestCase):
    def setUp(self):
        self.base = tmpdir(self)  # as git reports it
        self.root = make_repo(self.base / 'repo')
        self.marker = self.root / '.git' / 'simplify-gate'
        self.marker.unlink()

    def gate(self, *args, cwd=None):
        return subprocess.run([sys.executable, str(GATE), *args], cwd=cwd or self.root, capture_output=True, text=True)

    def test_bare_and_help_print_usage(self):
        for args in ((), ('help',)):
            run = self.gate(*args)
            self.assertEqual(run.returncode, 0)
            self.assertIn('usage: gate.py', run.stdout)
            self.assertNotIn('simplify-gate:', run.stdout)

    def test_status_on_off_and_overrides(self):
        out = self.gate('status').stdout
        self.assertIn('simplify-gate: off', out)
        self.assertIn(f'model:  {gate.MODEL} (default)', out)
        self.assertIn(f'effort: {gate.EFFORT} (default)', out)
        out = self.gate('on', '--effort', 'low').stdout
        self.assertIn(f'simplify-gate: on  ({self.marker})', out)
        self.assertIn('effort: low (repo)', out)
        self.assertIn(f'model:  {gate.MODEL} (default)', out)
        self.assertEqual(self.marker.read_text(), 'effort=low\n')
        out = self.gate('on', '--model', 'spark').stdout
        self.assertIn('model:  spark (repo)', out)
        self.assertIn('effort: low (repo)', out, 'on keeps the existing override')
        sh(self.root, 'git', 'worktree', 'add', '-q', str(self.base / 'wt'), '-b', 'wt')
        self.assertIn('simplify-gate: on', self.gate('status', cwd=self.base / 'wt').stdout)
        out = self.gate('off', cwd=self.base / 'wt').stdout
        self.assertIn('simplify-gate: off', out)
        self.assertFalse(self.marker.exists())
        self.assertIn(f'model:  {gate.MODEL} (default)', out)

    def test_refusals(self):
        self.assertNotEqual(self.gate('on', '--effort', 'extreme').returncode, 0)
        self.assertNotEqual(self.gate('off', '--model', 'x').returncode, 0)
        run = self.gate('status', cwd=self.base)
        self.assertNotEqual(run.returncode, 0)
        self.assertIn('not a git checkout', run.stderr)
        self.marker.write_text('effort=extreme\n')
        run = self.gate('status')
        self.assertNotEqual(run.returncode, 0)
        self.assertIn('effort must be one of', run.stderr)
        self.assertNotEqual(self.gate('status', '--apply').returncode, 0)

    def test_prune_runs_on_the_users_cache_outside_any_checkout(self):
        env = {**os.environ, 'XDG_CACHE_HOME': str(self.base / 'cache')}
        run = subprocess.run([sys.executable, str(GATE), 'prune', '--days', '7'], cwd=self.base,
                             capture_output=True, text=True, env=env)
        self.assertEqual(run.returncode, 0, run.stderr)
        self.assertIn(f'simplify-gate prune: {self.base / "cache" / "agentrc" / "simplify-gate"}', run.stdout)
        self.assertIn('idle over 7 days', run.stdout)
        self.assertIn('would free', run.stdout)


if __name__ == '__main__':
    unittest.main()
