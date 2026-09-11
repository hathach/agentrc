import fcntl
import importlib.util
import json
import os
import subprocess
import sys
import tempfile
import unittest
from pathlib import Path
from unittest import mock

HOOK = Path(__file__).resolve().parents[1] / 'hooks' / 'simplify-gate' / 'simplify_gate.py'
WRAPPER = HOOK.with_name('simplify-gate')
GATE = Path(__file__).resolve().parents[1] / 'skills' / 'simplify-gate' / 'scripts' / 'gate.py'
spec = importlib.util.spec_from_file_location('simplify_gate', HOOK)
gate = importlib.util.module_from_spec(spec)
spec.loader.exec_module(gate)

REAL_RUN = subprocess.run  # check_output goes through run, so the fake must pass git through
FINDING = {'file': 'a.txt', 'line': 1, 'problem': 'dead branch', 'alternative': 'drop it'}


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
        self.prompts = []
        self.on_codex = None  # runs while the fake Codex is 'running'

    def fake_run(self, cmd, **kw):
        if cmd[0] != 'codex':
            return REAL_RUN(cmd, **kw)
        self.calls.append(cmd)
        self.prompts.append(kw['input'])
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
        with mock.patch.object(gate.subprocess, 'run', self.fake_run):
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
        self.tmp = tempfile.TemporaryDirectory()
        base = Path(self.tmp.name)
        self.root = make_repo(base / 'repo')
        self.s = Session(self.root, base / 'state')
        self.s.prompt('first')

    def tearDown(self):
        self.tmp.cleanup()

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
        outside = Path(self.tmp.name) / 'outside'
        outside.mkdir()
        (outside / 'secret').write_text('nope\n')
        os.symlink(outside, self.root / 'deps')
        (self.root / 'blob.bin').write_bytes(b'\0\1\2')
        self.review()
        patch = self.s.prompts[0]
        self.assertIn(f'+{outside}', patch)
        self.assertNotIn('nope', patch)
        self.assertIn('[binary blob', patch)

    def test_a_submodule_is_left_out_of_scope_but_its_pointer_renders(self):
        sub = make_repo(Path(self.tmp.name) / 'sub')
        sh(self.root, 'git', '-c', 'protocol.file.allow=always', 'submodule', 'add', '-q', str(sub), 'sub')
        sh(self.root, 'git', 'commit', '-qm', 'sub')
        self.new_turn()
        (self.root / 'sub' / 'a.txt').write_text('vendored change\n')
        self.write('a.txt', 'mine\n')
        self.review()
        self.assertNotIn('vendored', self.s.prompts[0])
        self.assertIn('+mine\n', self.s.prompts[0])
        self.new_turn()
        sh(self.root / 'sub', 'git', 'commit', '-qam', 'bump')
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
        self.assertIn('Path: ".worktrees/task/a.txt"', patch)
        self.assertIn('-a\n+in worktree\n', patch, 'an edit, not a creation')
        self.assertIn('-c\n', patch, 'the deletion is kept')

    def test_a_worktree_removed_after_its_edit_keeps_the_edit(self):
        wt = self.root / '.worktrees' / 'task'
        sh(self.root, 'git', 'worktree', 'add', '-q', str(wt), '-b', 'task')
        (wt / 'a.txt').write_text('edited\n')
        self.review()
        self.assertIn('-a\n+edited\n', self.s.prompts[0])
        sh(self.root, 'git', 'worktree', 'remove', '--force', str(wt))
        self.assertEqual(self.s.stop(), {}, 'removing the worktree is not a deletion of its files')

    def test_a_worktree_of_a_bare_repository_is_tracked(self):
        bare = Path(self.tmp.name) / 'bare.git'
        sh(self.root, 'git', 'clone', '-q', '--bare', str(self.root), str(bare))
        root = Path(self.tmp.name) / 'linked'
        sh(bare, 'git', 'worktree', 'add', '-q', str(root), 'main')
        (bare / 'simplify-gate').touch()
        s = Session(root, Path(self.tmp.name) / 'linked-state')
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
        self.assertIn("may include a peer's edits", reply['reason'])
        self.assertIn('nor had a coworker write for you', reply['reason'], 'commissioned edits are ours to defend')
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
                fcntl.flock(lock, fcntl.LOCK_EX | fcntl.LOCK_NB)  # raises if Stop still holds it
        self.s.on_codex = take_state_lock
        self.assertIn('no findings', self.review()['systemMessage'])

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
        def snapshot_failing_after_codex(root, blobs):
            if self.s.calls:
                raise ValueError('index is unmerged')
            return real_snapshot(root, blobs)
        self.s.on_codex = lambda: self.write('b.txt', 'written during the review\n')
        with mock.patch.object(gate, 'snapshot', snapshot_failing_after_codex):
            reply = self.review()
        self.s.on_codex = None
        self.assertIn('could not be captured: index is unmerged', reply['systemMessage'])
        self.s.prompt()
        self.review()
        self.assertIn('written during the review', self.s.prompts[1])

    def test_a_stop_before_any_prompt_takes_a_baseline_and_says_so(self):
        s = Session(self.root, Path(self.tmp.name) / 'fresh')
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


class ActivationTest(unittest.TestCase):
    def setUp(self):
        self.tmp = tempfile.TemporaryDirectory()
        self.base = Path(self.tmp.name)
        self.root = make_repo(self.base / 'repo')

    def tearDown(self):
        self.tmp.cleanup()

    def run_hook(self, cwd, payload, project=None, **extra):
        env = {**os.environ, 'XDG_CACHE_HOME': str(self.base / 'cache'), 'CLAUDE_PROJECT_DIR': str(project or cwd)}
        env.pop('COWORK_TURN', None)  # the suite itself may run inside a cowork turn
        env.update(extra)
        return subprocess.run([str(WRAPPER)], input=json.dumps({'session_id': 's', 'cwd': str(cwd), **payload}),
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

    def test_the_project_dir_names_the_checkout_not_the_shell_cwd(self):
        prompt = {'hook_event_name': 'UserPromptSubmit', 'prompt': 'hi'}
        self.run_hook(self.root, prompt)
        other = make_repo(self.base / 'other')
        (other / '.git' / 'simplify-gate').unlink()
        self.run_hook(other, {'hook_event_name': 'Stop', 'last_assistant_message': ''}, project=self.root)
        state = json.loads(next((self.base / 'cache' / 'agentrc' / 'simplify-gate').glob('*/state.json')).read_text())
        self.assertEqual(state['errors'], [])
        self.assertIsNotNone(state['cursor'])

    def test_stop_reply_is_json_on_stdout(self):
        out = self.run_hook(self.root, {'hook_event_name': 'Stop', 'last_assistant_message': ''}).stdout
        self.assertIn('baseline taken', json.loads(out)['systemMessage'])
        out = self.run_hook(self.root, {'hook_event_name': 'Stop', 'last_assistant_message': ''}).stdout
        self.assertEqual(out, '')


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
