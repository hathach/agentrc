import fcntl
import hashlib
import importlib.util
import json
import os
import shutil
import subprocess
import sys
import tempfile
import unittest
from pathlib import Path
from unittest import mock

HOOK = Path(__file__).resolve().parents[1] / 'hooks' / 'simplify_gate.py'
WRAPPER = HOOK.with_name('simplify-gate')
GATE = Path(__file__).resolve().parents[1] / 'skills' / 'simplify-gate' / 'scripts' / 'gate.py'
spec = importlib.util.spec_from_file_location('simplify_gate', HOOK)
gate = importlib.util.module_from_spec(spec)
spec.loader.exec_module(gate)

REAL_RUN = subprocess.run  # check_output goes through run, so the fake must pass git through
FINDING = {'file': 'a.txt', 'line': 1, 'problem': 'dead branch', 'alternative': 'drop it'}


def blob(content):
    return hashlib.sha1(b'blob %d\0' % len(content) + content).hexdigest()


def sh(cwd, *args):
    return subprocess.run(args, cwd=cwd, check=True, capture_output=True, text=True).stdout


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


class Session:
    """Drives handle() the way Claude Code would, with a fake Codex."""

    def __init__(self, root, directory):
        self.root, self.directory = root, directory
        self.results = []  # what each codex attempt writes to -o; an Exception raises instead
        self.calls = []

    def fake_run(self, cmd, **kw):
        if cmd[0] != 'codex':
            return REAL_RUN(cmd, **kw)
        self.calls.append(cmd)
        if not self.results:
            raise AssertionError('unexpected codex call')
        result = self.results.pop(0)
        if isinstance(result, Exception):
            raise result
        Path(cmd[cmd.index('-o') + 1]).write_text(result)
        return subprocess.CompletedProcess(cmd, 0)

    def event(self, name, **fields):
        payload = {'session_id': 's1', 'cwd': str(self.root), 'hook_event_name': name, **fields}
        with mock.patch.object(gate.subprocess, 'run', self.fake_run):
            return gate.handle(self.root, self.directory, payload)

    def window(self, edit, tool='Bash', tool_use_id='t', agent_id=None, tool_input=None, fail=False):
        extra = {'agent_id': agent_id} if agent_id else {}
        fields = dict(tool_name=tool, tool_use_id=tool_use_id, tool_input=tool_input or {}, **extra)
        self.event('PreToolUse', **fields)
        edit()
        return self.event('PostToolUseFailure' if fail else 'PostToolUse', **fields)

    def state(self):
        return json.loads((self.directory / 'state.json').read_text())

    def stop(self, message='done'):
        return self.event('Stop', last_assistant_message=message)


class GateTest(unittest.TestCase):
    def setUp(self):
        self.tmp = tempfile.TemporaryDirectory()
        base = Path(self.tmp.name)
        self.root = make_repo(base / 'repo')
        self.s = Session(self.root, base / 'state')

    def tearDown(self):
        self.tmp.cleanup()

    def write(self, name, text):
        return lambda: (self.root / name).write_text(text)

    # --- scope -----------------------------------------------------------------

    def test_status_turn_skips_codex(self):
        self.assertEqual(self.s.stop(), {})
        self.assertEqual(self.s.calls, [])

    def test_pre_existing_dirt_is_excluded(self):
        (self.root / 'c.txt').write_text('dirty before the session\n')
        (self.root / 'stray.txt').write_text('untracked before the session\n')
        self.s.window(self.write('a.txt', 'a2\n'))
        self.assertEqual(sorted(self.s.state()['changes']), ['a.txt'])

    def test_edit_new_delete_then_stage_and_commit_are_one_change_set(self):
        def edits():
            (self.root / 'a.txt').write_text('a2\n')
            (self.root / 'n.txt').write_text('new\n')
            (self.root / 'c.txt').unlink()
        self.s.window(edits)
        before = gate.digest(self.s.state()['changes'])
        self.s.window(lambda: (sh(self.root, 'git', 'add', '-A'), sh(self.root, 'git', 'commit', '-qm', 'x')), tool_use_id='commit')
        state = self.s.state()
        self.assertEqual(gate.digest(state['changes']), before, 'committing is not a change')
        self.assertEqual(sorted(state['changes']), ['a.txt', 'c.txt', 'n.txt'])
        self.assertIsNone(state['changes']['c.txt'][1])
        self.assertIsNone(state['changes']['n.txt'][0])
        patch = gate.patch_text(self.root, self.s.directory / 'blobs', state['changes'])
        self.assertIn('-a\n+a2\n', patch)
        self.assertIn('+new\n', patch)
        self.assertIn('-c\n', patch)

    def test_reverting_a_file_drops_it_from_scope(self):
        self.s.window(self.write('a.txt', 'a2\n'))
        self.s.window(self.write('a.txt', 'a\n'), tool_use_id='t2')
        self.assertEqual(self.s.state()['changes'], {})

    def test_worker_and_overlapping_windows_accumulate_without_errors(self):
        # a subagent's Edit and the main Bash overlap: Pre(A) Pre(B) edit-x Post(A) edit-y Post(B)
        a = dict(tool_name='Bash', tool_use_id='A', tool_input={})
        b = dict(tool_name='Edit', tool_use_id='B', tool_input={'file_path': str(self.root / 'y.txt')}, agent_id='w1')
        self.s.event('PreToolUse', **a)
        self.s.event('PreToolUse', **b)
        (self.root / 'a.txt').write_text('x\n')
        self.s.event('PostToolUse', **a)
        (self.root / 'y.txt').write_text('y\n')
        self.s.event('PostToolUse', **b)
        state = self.s.state()
        self.assertEqual(sorted(state['changes']), ['a.txt', 'y.txt'])
        self.assertEqual(state['errors'], [])
        self.assertEqual(state['pending'], {})

    def test_an_earlier_window_closing_later_keeps_the_earliest_baseline(self):
        # Bash opens at a, writes a1; an Edit then writes a2 and closes first
        bash = dict(tool_name='Bash', tool_use_id='A', tool_input={})
        edit = dict(tool_name='Edit', tool_use_id='B', tool_input={'file_path': str(self.root / 'a.txt')}, agent_id='w1')
        self.s.event('PreToolUse', **bash)
        (self.root / 'a.txt').write_text('a1\n')
        self.s.event('PreToolUse', **edit)
        (self.root / 'a.txt').write_text('a2\n')
        self.s.event('PostToolUse', **edit)
        self.s.event('PostToolUse', **bash)
        patch = gate.patch_text(self.root, self.s.directory / 'blobs', self.s.state()['changes'])
        self.assertIn('-a\n+a2\n', patch, 'the baseline is what the first window saw')
        # and the reverse completion order gives the same answer
        (self.root / 'a.txt').write_text('a\n')
        self.s.event('PreToolUse', **{**bash, 'tool_use_id': 'C'})
        (self.root / 'a.txt').write_text('a1\n')
        self.s.event('PreToolUse', **{**edit, 'tool_use_id': 'D'})
        (self.root / 'a.txt').write_text('a3\n')
        self.s.event('PostToolUse', **{**bash, 'tool_use_id': 'C'})
        self.s.event('PostToolUse', **{**edit, 'tool_use_id': 'D'})
        patch = gate.patch_text(self.root, self.s.directory / 'blobs', self.s.state()['changes'])
        self.assertIn('-a\n+a3\n', patch)

    def test_an_edit_reverted_by_an_inner_window_that_closes_first_leaves_no_change(self):
        bash = dict(tool_name='Bash', tool_use_id='A', tool_input={})
        edit = dict(tool_name='Edit', tool_use_id='B', tool_input={'file_path': str(self.root / 'a.txt')}, agent_id='w1')
        self.s.event('PreToolUse', **bash)
        (self.root / 'a.txt').write_text('b\n')
        self.s.event('PreToolUse', **edit)
        (self.root / 'a.txt').write_text('a\n')  # the editor restores the original
        self.s.event('PostToolUse', **edit)
        self.s.event('PostToolUse', **bash)
        self.assertEqual(self.s.state()['changes'], {})

    def test_an_editor_target_is_captured_even_when_gitignored(self):
        (self.root / '.gitignore').write_text('out/\n')
        sh(self.root, 'git', 'add', '.gitignore')
        sh(self.root, 'git', 'commit', '-qm', 'ignore')
        (self.root / 'out').mkdir()
        target = self.root / 'out' / 'gen.txt'
        self.s.window(lambda: target.write_text('g\n'), tool='Write', tool_input={'file_path': str(target)})
        self.assertEqual(sorted(self.s.state()['changes']), ['out/gen.txt'])
        # a Bash window still ignores build output
        self.s.window(lambda: (self.root / 'out' / 'obj.o').write_bytes(b'\0'), tool_use_id='t2')
        self.assertEqual(sorted(self.s.state()['changes']), ['out/gen.txt'])

    def test_an_editor_window_counts_only_its_target(self):
        # a peer sharing the checkout edits c.txt while this session's Edit runs
        def edits():
            (self.root / 'a.txt').write_text('mine\n')
            (self.root / 'c.txt').write_text('peer\n')
        self.s.window(edits, tool='Edit', tool_input={'file_path': str(self.root / 'a.txt')})
        state = self.s.state()
        self.assertEqual(sorted(state['changes']), ['a.txt'])
        self.assertEqual(state['by_time'], [])

    def test_bash_attributed_files_are_named_as_ambiguous(self):
        self.s.window(self.write('a.txt', 'a2\n'))
        self.s.window(self.write('c.txt', 'c2\n'), tool='Edit', tool_use_id='t2',
                      tool_input={'file_path': str(self.root / 'c.txt')})
        self.s.results = [json.dumps({'findings': []})]
        reply = self.s.stop()['systemMessage']
        self.assertIn('Attributed by time window', reply)
        self.assertIn('a.txt', reply.split('Attributed')[1])
        self.assertNotIn('c.txt', reply.split('Attributed')[1])

    def test_a_pending_editor_window_on_an_ignored_file_is_not_a_deletion(self):
        (self.root / '.gitignore').write_text('out/\n')
        sh(self.root, 'git', 'add', '.gitignore')
        sh(self.root, 'git', 'commit', '-qm', 'ignore')
        (self.root / 'out').mkdir()
        target = self.root / 'out' / 'gen.txt'
        target.write_text('old\n')
        fields = dict(tool_name='Write', tool_use_id='slow', tool_input={'file_path': str(target)}, agent_id='w1')
        self.s.event('PreToolUse', **fields)
        target.write_text('new\n')
        self.s.results = [json.dumps({'findings': []})]
        self.s.stop()
        state = self.s.state()
        self.assertIsNotNone(state['changes']['out/gen.txt'][1], 'the file exists; it was edited, not deleted')
        patch = gate.patch_text(self.root, self.s.directory / 'blobs', state['changes'])
        self.assertIn('-old\n+new\n', patch)

    def test_the_lock_is_free_while_codex_runs(self):
        self.s.window(self.write('a.txt', 'a2\n'))
        fields = dict(tool_name='Bash', tool_use_id='bg', tool_input={}, agent_id='w1')
        self.s.event('PreToolUse', **fields)
        seen = {}
        def codex_side(cmd, **kw):
            if cmd[0] == 'codex':
                with (self.s.directory / 'lock').open('w') as lock:
                    fcntl.flock(lock, fcntl.LOCK_EX | fcntl.LOCK_NB)  # raises if Stop still holds it
                # a background worker's PostToolUse lands during the review
                (self.root / 'b.txt').write_text('b\n')
                seen['reply'] = gate.handle(self.root, self.s.directory, {
                    'session_id': 's1', 'cwd': str(self.root), 'hook_event_name': 'PostToolUse', **fields})
            return self.s.fake_run(cmd, **kw)
        self.s.results = [json.dumps({'findings': []})]
        with mock.patch.object(gate.subprocess, 'run', codex_side):
            reply = gate.handle(self.root, self.s.directory, {
                'session_id': 's1', 'cwd': str(self.root), 'hook_event_name': 'Stop', 'last_assistant_message': ''})
        self.assertIn('no findings', reply['systemMessage'])
        state = self.s.state()
        self.assertEqual(state['pending'], {})
        self.assertEqual(sorted(state['changes']), ['a.txt', 'b.txt'], 'the worker\'s window survived the review')

    def test_edits_in_a_task_worktree_are_tracked(self):
        wt = self.root / '.worktrees' / 'task'
        self.s.window(lambda: sh(self.root, 'git', 'worktree', 'add', '-q', str(wt), '-b', 'task'))
        self.assertEqual(self.s.state()['changes'], {}, 'creating the worktree changes nothing')
        self.s.window(lambda: (wt / 'a.txt').write_text('in worktree\n'), tool_use_id='t2')
        self.s.window(lambda: (wt / 'n.txt').write_text('n\n'), tool='Write', tool_use_id='t3',
                      tool_input={'file_path': str(wt / 'n.txt')})
        state = self.s.state()
        self.assertEqual(sorted(state['changes']), ['.worktrees/task/a.txt', '.worktrees/task/n.txt'])
        patch = gate.patch_text(self.root, self.s.directory / 'blobs', state['changes'])
        self.assertIn('-a\n+in worktree\n', patch)
        self.s.window(lambda: (sh(wt, 'git', 'add', '-A'), sh(wt, 'git', 'commit', '-qm', 'wt')), tool_use_id='t4')
        self.assertEqual(sorted(self.s.state()['changes']), ['.worktrees/task/a.txt', '.worktrees/task/n.txt'],
                         'committing in the worktree is not a change')

    def test_a_submodule_pointer_change_renders_without_reading_a_blob(self):
        sub = make_repo(Path(self.tmp.name) / 'sub')
        self.s.window(lambda: sh(self.root, 'git', '-c', 'protocol.file.allow=always', 'submodule', 'add', '-q', str(sub), 'sub'))
        state = self.s.state()
        self.assertIn('sub', state['changes'])
        self.assertEqual(state['errors'], [])
        patch = gate.patch_text(self.root, self.s.directory / 'blobs', state['changes'])
        self.assertIn('[submodule at commit', patch)
        self.s.results = [json.dumps({'findings': []})]
        self.assertIn('no findings', self.s.stop()['systemMessage'])

    def test_an_editor_outside_the_repository_is_not_a_window(self):
        outside = Path(self.tmp.name) / 'notes.md'
        def edits():
            outside.write_text('n\n')
            (self.root / 'c.txt').write_text('peer\n')
        self.s.window(edits, tool='Write', tool_input={'file_path': str(outside)})
        state = self.s.state()
        self.assertEqual(state['changes'], {})
        self.assertEqual(state['pending'], {})
        self.assertEqual(state['errors'], [])

    def test_a_missing_final_newline_keeps_diff_lines_apart(self):
        (self.root / 'a.txt').write_text('old')
        sh(self.root, 'git', 'commit', '-qam', 'no newline')
        self.s.window(lambda: (self.root / 'a.txt').write_text('new'))
        patch = gate.patch_text(self.root, self.s.directory / 'blobs', self.s.state()['changes'])
        self.assertIn('-old\n', patch)
        self.assertIn('+new\n', patch)
        self.assertNotIn('-old+new', patch)
        self.assertIn('No newline at end of file', patch)

    def test_crlf_conversion_is_not_a_change(self):
        (self.root / '.gitattributes').write_text('* text eol=crlf\n')
        sh(self.root, 'git', 'add', '.gitattributes')
        sh(self.root, 'git', 'commit', '-qm', 'crlf')
        (self.root / 'a.txt').write_bytes(b'a\r\n')  # the worktree form; the index holds LF
        self.s.window(lambda: sh(self.root, 'git', 'add', 'a.txt'))
        self.assertEqual(self.s.state()['changes'], {}, 'staging a file that only differs in eol')
        self.s.window(lambda: (self.root / 'a.txt').write_bytes(b'a\r\nb\r\n'), tool_use_id='t2')
        patch = gate.patch_text(self.root, self.s.directory / 'blobs', self.s.state()['changes'])
        self.assertIn('+b\n', patch, 'rendered as stored')
        self.assertNotIn('-a', patch)

    def test_awkward_path_names_are_recorded(self):
        names = ['line\nbreak.txt', '"quoted".txt', 'tab\tbed.txt']
        self.s.window(lambda: [(self.root / n).write_text('x\n') for n in names])
        state = self.s.state()
        self.assertEqual(sorted(state['changes']), sorted(names))
        self.assertEqual(state['errors'], [])

    def test_a_clean_filter_renders_each_file_as_stored(self):
        sh(self.root, 'git', 'config', 'filter.lower.clean', 'tr A-Z a-z')
        (self.root / '.gitattributes').write_text('*.txt filter=lower\n')
        sh(self.root, 'git', 'add', '.gitattributes')
        sh(self.root, 'git', 'commit', '-qm', 'filter')
        def edits():
            (self.root / 'x.txt').write_text('HELLO\n')
            (self.root / 'y.txt').write_text('hello\n')
        self.s.window(edits)
        patch = gate.patch_text(self.root, self.s.directory / 'blobs', self.s.state()['changes'])
        self.assertEqual(patch.count('+hello\n'), 2)
        self.assertNotIn('HELLO', patch)

    def test_rendering_does_not_depend_on_current_attributes(self):
        self.s.window(self.write('a.txt', 'new\n'))
        sh(self.root, 'git', 'config', 'filter.up.smudge', 'tr a-z A-Z')
        (self.root / '.gitattributes').write_text('a.txt filter=up\n')
        patch = gate.patch_text(self.root, self.s.directory / 'blobs', self.s.state()['changes'])
        self.assertIn('-a\n+new\n', patch)

    def test_a_gc_between_edit_and_review_does_not_lose_the_original(self):
        (self.root / 'a.txt').write_text('uncommitted before the session\n')
        self.s.window(self.write('a.txt', 'first\n'))
        self.s.window(self.write('a.txt', 'second\n'), tool_use_id='t2')
        sh(self.root, 'git', 'gc', '-q', '--prune=now')
        patch = gate.patch_text(self.root, self.s.directory / 'blobs', self.s.state()['changes'])
        self.assertIn('-uncommitted before the session\n+second\n', patch)

    def test_a_gc_inside_the_window_does_not_lose_the_pre_snapshot(self):
        (self.root / 'a.txt').write_text('uncommitted before the session\n')
        def edit_and_gc():
            (self.root / 'a.txt').write_text('edited\n')
            sh(self.root, 'git', 'gc', '-q', '--prune=now')
        self.s.window(edit_and_gc)
        self.assertEqual(self.s.state()['errors'], [])
        patch = gate.patch_text(self.root, self.s.directory / 'blobs', self.s.state()['changes'])
        self.assertIn('-uncommitted before the session\n+edited\n', patch)

    def test_a_gc_inside_the_window_does_not_lose_a_dropped_index_blob(self):
        (self.root / 'a.txt').write_text('staged, never committed\n')
        sh(self.root, 'git', 'add', 'a.txt')
        sh(self.root, 'git', 'update-index', '--refresh')
        def rm_and_gc():
            sh(self.root, 'git', 'rm', '-qf', 'a.txt')
            sh(self.root, 'git', 'gc', '-q', '--prune=now')
        self.s.window(rm_and_gc)
        self.assertEqual(self.s.state()['errors'], [])
        patch = gate.patch_text(self.root, self.s.directory / 'blobs', self.s.state()['changes'])
        self.assertIn('-staged, never committed\n', patch)

    def test_a_gc_does_not_lose_a_dropped_index_blob(self):
        (self.root / 'a.txt').write_text('staged, never committed\n')
        sh(self.root, 'git', 'add', 'a.txt')
        self.s.window(lambda: sh(self.root, 'git', 'rm', '-qf', 'a.txt'))
        sh(self.root, 'git', 'gc', '-q', '--prune=now')
        patch = gate.patch_text(self.root, self.s.directory / 'blobs', self.s.state()['changes'])
        self.assertIn('-staged, never committed\n', patch)

    def test_a_gc_does_not_lose_a_worktree_baseline_blob(self):
        (self.root / 'a.txt').write_text('only on a dangling commit\n')
        sh(self.root, 'git', 'commit', '-qam', 'dangling')
        dangling = sh(self.root, 'git', 'rev-parse', 'HEAD').strip()
        sh(self.root, 'git', 'reset', '-q', '--hard', 'HEAD~1')
        wt = self.root / '.worktrees' / 'task'
        def work():
            sh(self.root, 'git', 'worktree', 'add', '-q', '--detach', str(wt), dangling)
            (wt / 'a.txt').unlink()
        self.s.window(work)
        self.s.window(lambda: sh(self.root, 'git', 'worktree', 'remove', '--force', str(wt)), tool_use_id='t2')
        sh(self.root, 'git', 'reflog', 'expire', '--expire=now', '--all')
        sh(self.root, 'git', 'gc', '-q', '--prune=now')
        patch = gate.patch_text(self.root, self.s.directory / 'blobs', self.s.state()['changes'])
        self.assertIn('-only on a dangling commit\n', patch)

    def test_a_command_backgrounded_after_launch_keeps_its_window_open(self):
        fields = dict(tool_name='Bash', tool_use_id='bg', tool_input={'command': 'make'})
        self.s.event('PreToolUse', **fields)
        self.s.event('PostToolUse', tool_response={'backgroundTaskId': 'b1'}, **fields)  # ctrl-b
        self.assertIn('bg', self.s.state()['pending'])

    def test_a_background_command_keeps_its_window_open_until_stop(self):
        fields = dict(tool_name='Bash', tool_use_id='bg', tool_input={'command': 'make', 'run_in_background': True})
        self.s.event('PreToolUse', **fields)
        self.s.event('PostToolUse', **fields)  # fires at launch
        self.assertIn('bg', self.s.state()['pending'])
        (self.root / 'gen.txt').write_text('generated later\n')
        self.s.results = [json.dumps({'findings': []})]
        reply = self.s.stop()['systemMessage']
        self.assertIn('no findings', reply)
        self.assertIn('1 tool window(s) still open', reply)
        self.assertEqual(sorted(self.s.state()['changes']), ['gen.txt'])

    def test_state_from_an_older_gate_version_is_reported_not_a_crash(self):
        self.s.directory.mkdir(parents=True)
        (self.s.directory / 'state.json').write_text(json.dumps({
            'changes': {}, 'pending': {'old': {'a.txt': ['100644', 'x']}}, 'rounds': 0,
            'reviewed': None, 'findings': [], 'errors': []}))
        reply = self.s.stop()
        self.assertIn('older gate version', reply['systemMessage'])
        state = self.s.state()
        self.assertEqual(state['pending'], {})
        self.assertEqual(state['by_time'], [])
        self.s.event('UserPromptSubmit', prompt='next')
        self.assertEqual(self.s.stop(), {})

    def test_a_worktree_of_a_bare_repository_is_tracked(self):
        bare = Path(self.tmp.name) / 'bare.git'
        sh(self.root, 'git', 'clone', '-q', '--bare', str(self.root), str(bare))
        root = Path(self.tmp.name) / 'linked'
        sh(bare, 'git', 'worktree', 'add', '-q', str(root), 'main')
        (bare / 'simplify-gate').touch()
        s = Session(root, Path(self.tmp.name) / 'linked-state')
        s.window(lambda: (root / 'a.txt').write_text('a2\n'))
        state = s.state()
        self.assertEqual(state['errors'], [])
        self.assertEqual(sorted(state['changes']), ['a.txt'])

    def test_failed_tool_calls_still_close_their_window(self):
        self.s.window(self.write('a.txt', 'half\n'), fail=True)
        self.assertEqual(sorted(self.s.state()['changes']), ['a.txt'])
        self.assertEqual(self.s.state()['pending'], {})

    def test_symlinks_and_binaries_are_described_not_traversed(self):
        outside = Path(self.tmp.name) / 'outside'
        outside.mkdir()
        (outside / 'secret').write_text('nope\n')
        def edits():
            os.symlink(outside, self.root / 'deps')
            (self.root / 'blob.bin').write_bytes(b'\0\1\2')
        self.s.window(edits)
        patch = gate.patch_text(self.root, self.s.directory / 'blobs', self.s.state()['changes'])
        self.assertIn(f'+{outside}', patch)
        self.assertNotIn('nope', patch)
        self.assertIn('[binary blob', patch)

    def test_a_submodule_is_left_out_of_scope(self):
        sub = make_repo(Path(self.tmp.name) / 'sub')
        sh(self.root, 'git', '-c', 'protocol.file.allow=always', 'submodule', 'add', '-q', str(sub), 'sub')
        sh(self.root, 'git', 'commit', '-qm', 'sub')
        def edits():
            (self.root / 'sub' / 'a.txt').write_text('vendored change\n')
            (self.root / 'a.txt').write_text('mine\n')
        self.s.window(edits)
        self.assertEqual(sorted(self.s.state()['changes']), ['a.txt'])

    def test_an_open_window_is_reviewed_as_far_as_it_got(self):
        fields = dict(tool_name='Edit', tool_use_id='slow', tool_input={'file_path': str(self.root / 'a.txt')}, agent_id='w1')
        self.s.event('PreToolUse', **fields)
        (self.root / 'a.txt').write_text('half\n')
        self.s.results = [json.dumps({'findings': []})]
        reply = self.s.stop()
        self.assertIn('no findings', reply['systemMessage'])
        self.assertEqual(sorted(self.s.state()['changes']), ['a.txt'])
        (self.root / 'a.txt').write_text('whole\n')
        self.s.event('PostToolUse', **fields)
        state = self.s.state()
        self.assertEqual(state['errors'], [])
        self.assertEqual(state['pending'], {})
        patch = gate.patch_text(self.root, self.s.directory / 'blobs', state['changes'])
        self.assertIn('-a\n+whole\n', patch, 'the original is still the pre-snapshot')
        self.s.results = [json.dumps({'findings': []})]
        self.s.stop()
        self.assertEqual(len(self.s.calls), 2, 'the completed window is new work')

    def test_missing_pre_snapshot_is_reported_not_swallowed(self):
        self.s.event('PostToolUse', tool_name='Edit', tool_use_id='ghost', tool_input={})
        reply = self.s.stop()
        self.assertIn('no pre-tool snapshot', reply['systemMessage'])
        self.assertEqual(self.s.calls, [])

    # --- rounds ----------------------------------------------------------------

    def test_findings_block_then_second_round_is_final(self):
        self.s.window(self.write('a.txt', 'a2\n'))
        self.s.results = [json.dumps({'findings': [FINDING]})]
        reply = self.s.stop()
        self.assertEqual(reply['decision'], 'block')
        self.assertIn('round 1 of 2', reply['reason'])
        self.assertIn('a.txt:1: dead branch Alternative: drop it', reply['reason'])
        self.assertEqual(len(self.s.calls), 1)
        prompt = self.s.calls[0]
        self.assertIn('--sandbox', prompt)
        self.assertIn('read-only', prompt)

        self.s.window(self.write('a.txt', 'a3\n'), tool_use_id='t2')
        self.s.results = [json.dumps({'findings': [FINDING]})]
        reply = self.s.stop('applied part of it')
        self.assertEqual(reply['decision'], 'block')
        self.assertIn('round 2, final', reply['reason'])

        self.s.window(self.write('a.txt', 'a4\n'), tool_use_id='t3')
        reply = self.s.stop('rejected the rest because')
        self.assertIn('round limit reached', reply['systemMessage'])
        self.assertEqual(len(self.s.calls), 2)

    def test_previous_findings_and_claude_reply_reach_the_follow_up(self):
        self.s.window(self.write('a.txt', 'a2\n'))
        self.s.results = [json.dumps({'findings': [FINDING]})]
        self.s.stop()
        self.s.window(self.write('a.txt', 'a3\n'), tool_use_id='t2')
        captured = {}
        def spy(cmd, **kw):
            if cmd[0] == 'codex':
                captured['prompt'] = kw['input']
            return self.s.fake_run(cmd, **kw)
        self.s.results = [json.dumps({'findings': []})]
        with mock.patch.object(gate.subprocess, 'run', spy):
            reply = gate.handle(self.root, self.s.directory, {
                'session_id': 's1', 'cwd': str(self.root), 'hook_event_name': 'Stop',
                'last_assistant_message': 'kept the branch because callers rely on it'})
        self.assertIn('dead branch', captured['prompt'])
        self.assertIn('callers rely on it', captured['prompt'])
        self.assertIn('no findings', reply['systemMessage'])

    def test_a_rejection_from_an_earlier_turn_still_reaches_the_reviewer(self):
        self.s.window(self.write('a.txt', 'a2\n'))
        self.s.results = [json.dumps({'findings': [FINDING]})]
        self.s.stop()
        self.assertEqual(self.s.stop('rejected: callers rely on that branch'), {})
        self.s.event('UserPromptSubmit', prompt='next')
        self.s.window(self.write('b.txt', 'b\n'), tool_use_id='t2')
        captured = {}
        def spy(cmd, **kw):
            if cmd[0] == 'codex':
                captured['prompt'] = kw['input']
            return self.s.fake_run(cmd, **kw)
        self.s.results = [json.dumps({'findings': []})]
        with mock.patch.object(gate.subprocess, 'run', spy):
            gate.handle(self.root, self.s.directory, {
                'session_id': 's1', 'cwd': str(self.root), 'hook_event_name': 'Stop',
                'last_assistant_message': 'added b'})
        self.assertIn('callers rely on that branch', captured['prompt'])
        self.assertIn('added b', captured['prompt'])
        self.assertIsNone(self.s.state()['answer'], 'a new verdict starts a new answer')

    def test_unchanged_changes_skip_codex(self):
        self.s.window(self.write('a.txt', 'a2\n'))
        self.s.results = [json.dumps({'findings': []})]
        self.assertIn('no findings', self.s.stop()['systemMessage'])
        self.assertEqual(self.s.stop(), {})
        self.assertEqual(len(self.s.calls), 1)

    def test_rejection_without_edits_does_not_rerun(self):
        self.s.window(self.write('a.txt', 'a2\n'))
        self.s.results = [json.dumps({'findings': [FINDING]})]
        self.assertEqual(self.s.stop()['decision'], 'block')
        self.assertEqual(self.s.stop('I reject it because'), {})
        self.assertEqual(len(self.s.calls), 1)

    def test_the_fed_back_challenge_is_not_a_new_turn(self):
        self.s.window(self.write('a.txt', 'a2\n'))
        self.s.results = [json.dumps({'findings': [FINDING]})]
        reply = self.s.stop()
        self.assertIn('round 1 of 2', reply['reason'])
        self.s.event('UserPromptSubmit', prompt='Stop hook feedback:\n' + reply['reason'])
        self.assertEqual(self.s.state()['rounds'], 1, 'the block fed back as a prompt keeps the budget')
        self.s.window(self.write('a.txt', 'a3\n'), tool_use_id='t2')
        self.s.results = [json.dumps({'findings': [FINDING]})]
        self.assertIn('round 2, final', self.s.stop()['reason'])

    def test_the_reported_round_is_the_one_reviewed(self):
        self.s.window(self.write('a.txt', 'a2\n'))
        def reset_during_codex(cmd, **kw):
            if cmd[0] == 'codex':
                gate.handle(self.root, self.s.directory, {
                    'session_id': 's1', 'cwd': str(self.root), 'hook_event_name': 'UserPromptSubmit', 'prompt': 'next'})
            return self.s.fake_run(cmd, **kw)
        self.s.results = [json.dumps({'findings': [FINDING]})]
        with mock.patch.object(gate.subprocess, 'run', reset_during_codex):
            reply = gate.handle(self.root, self.s.directory, {
                'session_id': 's1', 'cwd': str(self.root), 'hook_event_name': 'Stop', 'last_assistant_message': ''})
        self.assertIn('round 1 of 2', reply['reason'])

    def test_a_new_user_turn_resets_the_round_budget_only(self):
        self.s.window(self.write('a.txt', 'a2\n'))
        self.s.results = [json.dumps({'findings': [FINDING]}), json.dumps({'findings': [FINDING]})]
        self.s.stop()
        self.s.window(self.write('a.txt', 'a3\n'), tool_use_id='t2')
        self.s.stop()
        self.s.event('UserPromptSubmit', prompt='next')
        self.assertEqual(self.s.state()['rounds'], 0)
        self.assertEqual(sorted(self.s.state()['changes']), ['a.txt'], 'session scope persists across turns')
        self.assertEqual(self.s.stop(), {}, 'nothing new since the last review')
        self.s.window(self.write('b.txt', 'b\n'), tool_use_id='t3')
        self.s.results = [json.dumps({'findings': []})]
        self.s.stop()
        self.assertEqual(len(self.s.calls), 3)

    def test_outside_change_after_the_session_edit_is_named(self):
        self.s.window(self.write('a.txt', 'a2\n'))
        (self.root / 'a.txt').write_text('someone else\n')
        self.s.results = [json.dumps({'findings': []})]
        reply = self.s.stop()
        self.assertIn('Changed after the session', reply['systemMessage'])
        self.assertIn('a.txt', reply['systemMessage'])

    # --- infrastructure failures -----------------------------------------------

    def test_two_failures_then_continue_with_a_notice(self):
        self.s.window(self.write('a.txt', 'a2\n'))
        self.s.results = [subprocess.TimeoutExpired('codex', 1), OSError('no codex')]
        reply = self.s.stop()
        self.assertNotIn('decision', reply)
        self.assertIn('incomplete', reply['systemMessage'])
        self.assertIn('failed twice', reply['systemMessage'])
        self.assertEqual(len(self.s.calls), 2)
        self.assertIn('incomplete', self.s.stop()['systemMessage'], 'the error stays visible')
        self.assertEqual(len(self.s.calls), 2)
        self.s.event('UserPromptSubmit', prompt='next')
        self.assertEqual(self.s.state()['errors'], [])
        self.s.results = [json.dumps({'findings': []})]
        self.assertIn('no findings', self.s.stop()['systemMessage'], 'the unreviewed patch is retried')

    def test_a_failure_after_the_patch_is_marked_reviewed_is_not_silent(self):
        self.s.window(self.write('a.txt', 'a2\n'))
        with mock.patch.object(gate, 'patch_text', side_effect=OSError('blob store gone')):
            reply = self.s.stop()
        self.assertIn('blob store gone', reply['systemMessage'])
        self.assertEqual(self.s.calls, [])
        self.s.event('UserPromptSubmit', prompt='next')
        self.s.results = [json.dumps({'findings': []})]
        self.assertIn('no findings', self.s.stop()['systemMessage'])

    def test_a_pending_edit_reverted_during_review_drops_out(self):
        fields = dict(tool_name='Edit', tool_use_id='slow', tool_input={'file_path': str(self.root / 'a.txt')}, agent_id='w1')
        self.s.event('PreToolUse', **fields)
        (self.root / 'a.txt').write_text('half\n')
        def revert_during_codex(cmd, **kw):
            if cmd[0] == 'codex':  # the worker finishes, restoring the file, while Codex reviews "half"
                (self.root / 'a.txt').write_text('a\n')
                gate.handle(self.root, self.s.directory, {
                    'session_id': 's1', 'cwd': str(self.root), 'hook_event_name': 'PostToolUse', **fields})
            return self.s.fake_run(cmd, **kw)
        self.s.results = [json.dumps({'findings': [FINDING]})]
        with mock.patch.object(gate.subprocess, 'run', revert_during_codex):
            reply = gate.handle(self.root, self.s.directory, {
                'session_id': 's1', 'cwd': str(self.root), 'hook_event_name': 'Stop', 'last_assistant_message': ''})
        self.assertEqual(reply['decision'], 'block')
        self.assertEqual(self.s.state()['changes'], {})
        self.assertEqual(self.s.stop(), {})

    def test_an_unchanged_bash_window_keeps_the_session_version(self):
        self.s.window(self.write('a.txt', 'mine\n'))
        (self.root / 'a.txt').write_text('peer\n')  # outside any window
        self.s.window(lambda: None, tool_use_id='ro')  # a read-only command
        self.assertEqual(self.s.state()['changes']['a.txt'][1], ['100644', blob(b'mine\n')])
        self.s.results = [json.dumps({'findings': []})]
        self.assertIn('Changed after the session', self.s.stop()['systemMessage'])

    def test_a_previewed_window_rebases_only_what_it_previewed(self):
        self.s.window(self.write('a.txt', 'mine\n'), tool='Edit', tool_input={'file_path': str(self.root / 'a.txt')})
        (self.root / 'a.txt').write_text('peer\n')  # outside any window
        fields = dict(tool_name='Bash', tool_use_id='ro', tool_input={}, agent_id='w1')
        self.s.event('PreToolUse', **fields)
        self.s.results = [json.dumps({'findings': []})]
        self.s.stop()
        self.s.event('PostToolUse', **fields)
        state = self.s.state()
        self.assertEqual(state['changes']['a.txt'][1], ['100644', blob(b'mine\n')])
        self.assertEqual(state['by_time'], [])

    def test_a_worktree_removed_during_review_keeps_the_previewed_edit(self):
        wt = self.root / '.worktrees' / 'task'
        sh(self.root, 'git', 'worktree', 'add', '-q', str(wt), '-b', 'task')
        fields = dict(tool_name='Bash', tool_use_id='bg', tool_input={}, agent_id='w1')
        self.s.event('PreToolUse', **fields)
        (wt / 'a.txt').write_text('mine\n')
        def remove_during_codex(cmd, **kw):
            if cmd[0] == 'codex':
                sh(self.root, 'git', 'worktree', 'remove', '--force', str(wt))
                gate.handle(self.root, self.s.directory, {
                    'session_id': 's1', 'cwd': str(self.root), 'hook_event_name': 'PostToolUse', **fields})
            return self.s.fake_run(cmd, **kw)
        self.s.results = [json.dumps({'findings': []})]
        with mock.patch.object(gate.subprocess, 'run', remove_during_codex):
            gate.handle(self.root, self.s.directory, {
                'session_id': 's1', 'cwd': str(self.root), 'hook_event_name': 'Stop', 'last_assistant_message': ''})
        state = self.s.state()
        self.assertEqual(state['changes']['.worktrees/task/a.txt'][1], ['100644', blob(b'mine\n')])

    def test_a_pending_bash_window_at_stop_is_attributed_by_time(self):
        fields = dict(tool_name='Bash', tool_use_id='bg', tool_input={}, agent_id='w1')
        self.s.event('PreToolUse', **fields)
        (self.root / 'a.txt').write_text('a2\n')
        self.s.results = [json.dumps({'findings': []})]
        self.assertIn('Attributed by time window', self.s.stop()['systemMessage'])

    def test_an_ignored_editor_target_does_not_leak_into_an_overlapping_bash_window(self):
        (self.root / '.gitignore').write_text('gen.txt\n')
        sh(self.root, 'git', 'add', '.gitignore')
        sh(self.root, 'git', 'commit', '-qm', 'ignore')
        (self.root / 'gen.txt').write_text('old\n')
        bash = dict(tool_name='Bash', tool_use_id='A', tool_input={}, agent_id='w1')
        edit = dict(tool_name='Write', tool_use_id='B', tool_input={'file_path': str(self.root / 'gen.txt')}, agent_id='w2')
        self.s.event('PreToolUse', **bash)
        self.s.event('PreToolUse', **edit)
        (self.root / 'gen.txt').write_text('new\n')
        self.s.results = [json.dumps({'findings': []})]
        self.s.stop()
        patch = gate.patch_text(self.root, self.s.directory / 'blobs', self.s.state()['changes'])
        self.assertIn('-old\n+new\n', patch)
        self.assertEqual(self.s.state()['by_time'], [])

    def test_removing_a_task_worktree_keeps_its_recorded_edits(self):
        wt = self.root / '.worktrees' / 'task'
        sh(self.root, 'git', 'worktree', 'add', '-q', str(wt), '-b', 'task')
        self.s.window(lambda: (wt / 'a.txt').write_text('edited\n'))
        recorded = self.s.state()['changes']
        self.s.window(lambda: sh(self.root, 'git', 'worktree', 'remove', '--force', str(wt)), tool_use_id='t2')
        self.assertEqual(self.s.state()['changes'], recorded, 'the edit stays as recorded; nothing became a deletion')

    def test_a_deleted_but_unpruned_worktree_is_skipped(self):
        wt = self.root / '.worktrees' / 'task'
        sh(self.root, 'git', 'worktree', 'add', '-q', str(wt), '-b', 'task')
        shutil.rmtree(wt)
        self.assertIn('prunable', sh(self.root, 'git', 'worktree', 'list', '--porcelain'))
        self.s.window(lambda: (self.root / 'a.txt').write_text('edited\n'))
        state = self.s.state()
        self.assertEqual(state['errors'], [])
        self.assertEqual(sorted(state['changes']), ['a.txt'])

    def test_a_worktree_added_edited_and_committed_in_one_window(self):
        wt = self.root / '.worktrees' / 'task'
        def work():
            sh(self.root, 'git', 'worktree', 'add', '-q', str(wt), '-b', 'task')
            (wt / 'a.txt').write_text('mine\n')
            (wt / 'c.txt').unlink()
            sh(wt, 'git', 'add', '-A')
            sh(wt, 'git', 'commit', '-qm', 'in the window')
        self.s.window(work)
        state = self.s.state()
        self.assertEqual(sorted(state['changes']), ['.worktrees/task/a.txt', '.worktrees/task/c.txt'])
        patch = gate.patch_text(self.root, self.s.directory / 'blobs', state['changes'])
        self.assertIn('-a\n+mine\n', patch, 'an edit, not a creation')
        self.assertIn('-c\n', patch, 'the deletion is kept')

    def test_invalid_or_out_of_scope_results_count_as_failures(self):
        self.s.window(self.write('a.txt', 'a2\n'))
        self.s.results = ['not json', json.dumps({'findings': [{**FINDING, 'file': 'c.txt'}]})]
        reply = self.s.stop()
        self.assertIn('incomplete', reply['systemMessage'])
        self.assertIn('outside review scope', reply['systemMessage'])

    def test_marker_overrides_drive_the_codex_command(self):
        self.s.window(self.write('a.txt', 'a2\n'))
        self.s.results = [json.dumps({'findings': []})]
        self.s.stop()
        cmd = self.s.calls[0]
        self.assertEqual(cmd[cmd.index('-m') + 1], gate.MODEL)
        self.assertIn(f'model_reasoning_effort="{gate.EFFORT}"', cmd)
        (self.root / '.git' / 'simplify-gate').write_text('# repo override\nmodel = spark\neffort=low\n')
        self.s.window(self.write('a.txt', 'a3\n'), tool_use_id='t2')
        self.s.results = [json.dumps({'findings': []})]
        self.s.stop()
        cmd = self.s.calls[1]
        self.assertEqual(cmd[cmd.index('-m') + 1], 'spark')
        self.assertIn('model_reasoning_effort="low"', cmd)

    def test_a_bad_marker_is_reported_not_defaulted(self):
        (self.root / '.git' / 'simplify-gate').write_text('effort=extreme\n')
        self.s.window(self.write('a.txt', 'a2\n'))
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


class ActivationTest(unittest.TestCase):
    def setUp(self):
        self.tmp = tempfile.TemporaryDirectory()
        self.base = Path(self.tmp.name)
        self.root = make_repo(self.base / 'repo')

    def tearDown(self):
        self.tmp.cleanup()

    def ours(self, data):
        return {event: [h for g in data.get('hooks', {}).get(event, []) for h in g['hooks'] if gate.ours(h)]
                for event in gate.EVENTS}

    def test_install_replaces_older_entries_keeps_other_hooks_and_is_idempotent(self):
        settings = self.base / 'settings.json'
        old = f'python3 {HOOK} --repository /some/repo'
        settings.write_text(json.dumps({'model': 'x', 'hooks': {
            'SessionStart': [{'matcher': '*', 'hooks': [{'type': 'command', 'command': 'other'}]}],
            'PreToolUse': [{'matcher': 'Bash', 'hooks': [{'type': 'command', 'command': old}]}],
            'Stop': [{'hooks': [{'type': 'command', 'command': 'other-stop'},
                                {'type': 'command', 'command': old}]}]}}))
        gate.install(settings)
        gate.install(settings)
        data = json.loads(settings.read_text())
        self.assertEqual(data['model'], 'x')
        self.assertEqual(data['hooks']['SessionStart'][0]['hooks'][0]['command'], 'other')
        self.assertEqual([h['command'] for g in data['hooks']['Stop'] for h in g['hooks']], ['other-stop', str(WRAPPER)])
        self.assertNotIn(old, settings.read_text())
        for event, hooks in self.ours(data).items():
            self.assertEqual(len(hooks), 1, event)
            self.assertEqual(hooks[0]['timeout'], 650 if event == 'Stop' else 30)
        for event in ('PreToolUse', 'PostToolUse', 'PostToolUseFailure'):
            group = [g for g in data['hooks'][event] if gate.ours(g['hooks'][0])][0]
            self.assertEqual(group['matcher'], 'Bash|Edit|Write|NotebookEdit')
        self.assertNotIn('matcher', data['hooks']['UserPromptSubmit'][0])
        backup = json.loads((self.base / 'settings.json.before-simplify-gate').read_text())
        self.assertIn(old, json.dumps(backup))

        gate.remove(settings)
        data = json.loads(settings.read_text())
        self.assertEqual(sum(len(v) for v in self.ours(data).values()), 0)
        self.assertEqual(data['hooks']['Stop'][0]['hooks'][0]['command'], 'other-stop')
        self.assertNotIn('PreToolUse', data['hooks'])
        self.assertEqual(data['hooks']['SessionStart'][0]['hooks'][0]['command'], 'other')

    def test_the_hook_command_is_shell_quoted(self):
        settings = self.base / 'settings.json'
        with mock.patch.object(gate, 'WRAPPER', Path('/opt/my agentrc/hooks/simplify-gate')):
            gate.install(settings)
            data = json.loads(settings.read_text())
            command = data['hooks']['Stop'][0]['hooks'][0]['command']
            self.assertEqual(command, "'/opt/my agentrc/hooks/simplify-gate'")
            gate.remove(settings)
        self.assertNotIn('hooks', json.loads(settings.read_text()))

    def run_hook(self, cwd, payload, project=None):
        env = {**os.environ, 'XDG_CACHE_HOME': str(self.base / 'cache'), 'CLAUDE_PROJECT_DIR': str(project or cwd)}
        return subprocess.run([str(WRAPPER)], input=json.dumps({'session_id': 's', 'cwd': str(cwd), **payload}),
                              capture_output=True, text=True, env=env, check=True)

    def test_wrapper_runs_the_gate_only_where_the_marker_exists(self):
        pre = {'hook_event_name': 'PreToolUse', 'tool_name': 'Bash', 'tool_use_id': 't', 'tool_input': {}}
        other = make_repo(self.base / 'other')
        (other / '.git' / 'simplify-gate').unlink()
        self.assertEqual(self.run_hook(other, pre).stdout, '')
        self.assertEqual(self.run_hook(self.base, pre).stdout, '', 'not a checkout at all')
        self.assertFalse((self.base / 'cache').exists())
        sh(self.root, 'git', 'worktree', 'add', '-q', str(self.base / 'wt'), '-b', 'wt')
        self.run_hook(self.base / 'wt', pre)
        self.assertTrue(list((self.base / 'cache' / 'agentrc' / 'simplify-gate').iterdir()), 'worktrees share the marker')

    def test_the_project_dir_names_the_checkout_not_the_shell_cwd(self):
        pre = {'hook_event_name': 'PreToolUse', 'tool_name': 'Bash', 'tool_use_id': 't', 'tool_input': {}}
        self.run_hook(self.root, pre)
        other = make_repo(self.base / 'other')
        (other / '.git' / 'simplify-gate').unlink()
        self.run_hook(other, {**pre, 'hook_event_name': 'PostToolUse'}, project=self.root)
        state = json.loads(next((self.base / 'cache' / 'agentrc' / 'simplify-gate').glob('*/state.json')).read_text())
        self.assertEqual(state['pending'], {})
        self.assertEqual(state['errors'], [])

    def test_stop_reply_is_json_on_stdout(self):
        pre = {'hook_event_name': 'PreToolUse', 'tool_name': 'Bash', 'tool_use_id': 't', 'tool_input': {}}
        self.run_hook(self.root, pre)
        self.run_hook(self.root, {**pre, 'hook_event_name': 'PostToolUse'})
        out = self.run_hook(self.root, {'hook_event_name': 'PostToolUse', 'tool_name': 'Bash',
                                        'tool_use_id': 'ghost', 'tool_input': {}}).stdout
        self.assertEqual(out, '')
        out = self.run_hook(self.root, {'hook_event_name': 'Stop', 'last_assistant_message': ''}).stdout
        self.assertIn('no pre-tool snapshot', json.loads(out)['systemMessage'])


class SkillTest(unittest.TestCase):
    def setUp(self):
        self.tmp = tempfile.TemporaryDirectory()
        self.base = Path(self.tmp.name)
        self.root = make_repo(self.base / 'repo')
        self.marker = self.root / '.git' / 'simplify-gate'
        self.marker.unlink()

    def tearDown(self):
        self.tmp.cleanup()

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


if __name__ == '__main__':
    unittest.main()
