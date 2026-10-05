"""Tests for pr-babysit's commits.py against a real temp repository."""
import importlib.util
import json
import os
import subprocess
import sys
import tempfile
import unittest
from pathlib import Path
from unittest import mock

SCRIPT = Path(__file__).resolve().parents[1] / 'skills' / 'pr-babysit' / 'scripts' / 'commits.py'
spec = importlib.util.spec_from_file_location('pr_babysit_commits', SCRIPT)
commits = importlib.util.module_from_spec(spec)
spec.loader.exec_module(commits)

ENV = {**os.environ, 'GIT_AUTHOR_NAME': 't', 'GIT_AUTHOR_EMAIL': 't@t', 'GIT_COMMITTER_NAME': 't',
       'GIT_COMMITTER_EMAIL': 't@t'}


class CommitsTest(unittest.TestCase):
    def setUp(self):
        tmp = tempfile.TemporaryDirectory()
        self.addCleanup(tmp.cleanup)
        self.repo = Path(tmp.name)
        self.git('init', '-q')
        self.write('a.c', 'a\n')
        self.base = self.commit('base', 'a.c')

    def git(self, *argv):
        return subprocess.run(['git', *argv], cwd=self.repo, env=ENV, check=True,
                              capture_output=True, text=True).stdout

    def write(self, path, text):
        (self.repo / path).parent.mkdir(parents=True, exist_ok=True)
        (self.repo / path).write_text(text)

    def commit(self, message, *paths):
        self.git('add', '-A', '--', *paths)
        self.git('commit', '-q', '-m', message)
        return self.git('rev-parse', 'HEAD').strip()

    def hook(self, body):
        hook = self.repo / '.git' / 'hooks' / 'pre-commit'
        hook.write_text(f'#!/bin/sh\n{body}\n')
        hook.chmod(0o755)

    def run_script(self, *argv, cwd=None, stdin=''):
        done = subprocess.run([sys.executable, str(SCRIPT), *argv], cwd=cwd or self.repo, env=ENV,
                              capture_output=True, text=True, input=stdin)
        return done.returncode, json.loads(done.stdout.splitlines()[-1])

    def test_head_reads_the_commit_and_the_scope_unquoted(self):
        odd = ['src/space name.c', 'src/quote"name.c', '-x.c']
        for p in odd:
            self.write(p, 'x\n')
        self.write('a.c', 'changed\n')
        self.write('left.c', 'left\n')
        sha = self.commit('fix: the thing\n\nWhy it matters.', *odd)
        code, out = self.run_script('head', '--parent', self.base, *odd)
        self.assertEqual(code, 0)
        self.assertEqual((out['sha'], out['parent'], out['scope'], out['refusal']), (sha, self.base, odd, ''),
                         'a change outside the scope is not left behind')
        self.assertEqual(len(out['entries']), 3)
        blob = self.git('rev-parse', f'{sha}:src/quote"name.c').strip()
        self.assertIn(f'100644 blob {blob}\tsrc/quote"name.c', out['entries'])
        rest = {k: v for k, v in out.items() if k != 'seal'}
        self.assertEqual(out['seal'], sys.modules['facts'].sealed(rest)['seal'], 'the line is sealed over what it says')

    def test_the_seal_leaves_null_members_out_and_ignores_key_order(self):
        sealed = sys.modules['facts'].sealed
        self.assertEqual(sealed({'a': 1, 'b': None, 'c': [{'d': None, 'e': 'é'}]})['seal'], sealed({'c': [{'e': 'é'}], 'a': 1})['seal'])
        self.assertNotEqual(sealed({'a': 'x' * 40})['seal'], sealed({'a': 'x' * 35})['seal'])

    def test_head_reports_a_deletion_by_its_absence(self):
        self.git('rm', '-q', 'a.c')
        self.git('commit', '-q', '-m', 'drop')
        code, out = self.run_script('head', '--parent', self.base, 'a.c')
        self.assertEqual((code, out['entries'], out['refusal']), (0, [], ''))

    def test_head_refuses_a_commit_it_cannot_publish(self):
        self.assertEqual(self.refusal('head', '--parent', self.base, 'a.c'), 'nothing was committed: HEAD is still the parent')
        self.write('a.c', 'one\n')
        self.write('b.c', 'b\n')
        one = self.commit('one', 'a.c', 'b.c')
        self.assertIn(f'sits on {self.base[:7]}, not 1111111', self.refusal('head', '--parent', '1' * 40, 'a.c', 'b.c'))
        self.assertEqual(self.refusal('head', '--parent', self.base, 'a.c'), 'the commit carries unowned path(s): b.c')
        self.write('c.c', 'c\n')
        self.assertEqual(self.refusal('head', '--parent', self.base, 'a.c', 'b.c', 'c.c'), 'the commit left owned change(s) behind: ?? c.c')
        self.git('commit', '-q', '--amend', '--allow-empty-message', '-m', '')
        self.assertEqual(self.refusal('head', '--parent', self.base, 'a.c', 'b.c'), 'the commit has no message')
        # A merge whose first parent is right still brings history nothing audited.
        self.git('checkout', '-q', '-b', 'side', self.base)
        self.write('s.c', 's\n')
        side = self.commit('side', 's.c')
        self.git('checkout', '-q', '--detach', one)
        self.git('merge', '-q', '--no-ff', '--no-edit', side)
        self.assertIn('has 2 parents', self.refusal('head', '--parent', one, 's.c'))

    def test_head_moving_while_read_is_an_error(self):
        real = commits.git
        heads = iter([self.base + '\n', '2' * 40 + '\n'])
        with mock.patch.object(commits, 'git', side_effect=lambda *a: next(heads) if a == ('rev-parse', 'HEAD') else real(*a)):
            cwd = os.getcwd()
            os.chdir(self.repo)
            try:
                with self.assertRaisesRegex(commits.Unusable, 'HEAD moved'):
                    commits.head(self.base, ['a.c'])
            finally:
                os.chdir(cwd)

    def test_chain_lists_each_commit_oldest_first(self):
        self.write('b.c', 'b\n')
        one = self.commit('one', 'b.c')
        self.write('c\nd.c', 'c\n')
        self.write('b.c', 'bb\n')
        two = self.commit('two', 'c\nd.c', 'b.c')
        code, out = self.run_script('chain', self.base, two, '--published', self.base, '--base', self.base)
        self.assertEqual(code, 0)
        self.assertEqual({k: v for k, v in out.items() if k != 'seal'}, {
            'from': self.base, 'to': two, 'published': self.base, 'base': self.base, 'commits': [one, two], 'refusal': '',
            'paths': ['b.c', 'c\nd.c'], 'unpublished': ['b.c', 'c\nd.c']}, 'each path once, a name holding a newline whole')

    def test_unpublished_holds_only_what_the_commits_after_the_published_head_touch(self):
        self.write('b.c', 'b\n')
        one = self.commit('one', 'b.c')
        self.write('c.c', 'c\n')
        two = self.commit('two', 'c.c')
        self.write('b.c', 'bb\n')
        three = self.commit('three', 'b.c')
        for published, want in ((self.base, ['b.c', 'c.c']), (one, ['c.c', 'b.c']), (two, ['b.c']), (three, []), ('f' * 40, ['b.c', 'c.c'])):
            code, out = self.run_script('chain', self.base, three, '--published', published, '--base', self.base)
            self.assertEqual(code, 0)
            self.assertEqual(out['unpublished'], want, f'{published[:7]}: a published path edited again later is unpublished again; a foreign head publishes nothing')

    def refusal(self, *argv):
        code, out = self.run_script(*argv)
        self.assertEqual(code, 0)
        return out['refusal']

    def chain_refusal(self, start, end, base=None):
        return self.refusal('chain', start, end, '--published', start, '--base', base or self.base)

    def test_a_chain_that_cannot_be_adopted_is_refused(self):
        self.write('b.c', 'b\n')
        one = self.commit('one', 'b.c')
        self.assertIn('no commits in', self.chain_refusal(one, self.base))
        self.git('commit', '-q', '--allow-empty', '-m', 'nothing')
        self.assertIn('touches no path', self.chain_refusal(self.base, self.git('rev-parse', 'HEAD').strip()))
        # A side commit on `one` merged back with --no-ff: the merge is no single-parent link.
        self.git('checkout', '-q', '-b', 'side', one)
        self.write('s.c', 's\n')
        side = self.commit('side', 's.c')
        self.git('checkout', '-q', '--detach', one)
        self.git('merge', '-q', '--no-ff', '--no-edit', side)
        merge = self.git('rev-parse', 'HEAD').strip()
        self.assertIn(f"{merge[:7]} merges {side[:7]}, which is not on the PR's base {self.base[:7]}", self.chain_refusal(one, merge))
        # From a commit beside the chain: its first commit does not sit on it.
        self.git('checkout', '-q', '--detach', one)
        self.write('t.c', 't\n')
        self.assertIn(f'sits on {one[:7]}, not {side[:7]}', self.chain_refusal(side, self.commit('two', 't.c')))

    def base_merge(self, base_files=None, pr_files=None, resolve=None, message='Merge the base', start=None):
        """A PR commit and base's own commit beside it on start, and the PR merging base, after resolve() edits the tree: (one, tip, merge)."""
        self.git('checkout', '-q', '-b', 'main', start or self.base)
        for path, text in (base_files or {'m.c': 'base\n'}).items():
            self.write(path, text)
        tip = self.commit('base moved')
        self.git('checkout', '-q', '-b', 'pr', start or self.base)
        for path, text in (pr_files or {'b.c': 'pr\n'}).items():
            self.write(path, text)
        one = self.commit('one')
        subprocess.run(['git', 'merge', '-q', '--no-ff', '--no-commit', tip], cwd=self.repo, env=ENV, capture_output=True)
        if resolve:
            resolve()
        return one, tip, self.commit(message)

    def chain_of(self, end, published, base):
        code, out = self.run_script('chain', self.base, end, '--published', published, '--base', base)
        self.assertEqual(code, 0, out)
        return out

    def test_a_merge_from_the_base_is_one_link_owning_only_what_it_leaves_unlike_both_parents(self):
        one, tip, merge = self.base_merge()
        self.write('c.c', 'c\n')
        two = self.commit('two', 'c.c')
        out = self.chain_of(two, one, tip)
        self.assertEqual((out['commits'], out['paths'], out['unpublished'], out['refusal']), ([one, merge, two], ['b.c', 'c.c'], ['c.c'], ''),
                         "the base's own commit is no link, and what it alone changed is not the merge's")
        self.git('checkout', '-q', 'main')
        self.write('n.c', 'n\n')
        later = self.commit('base moved on', 'n.c')
        self.assertEqual(self.chain_of(two, one, later)['refusal'], '', 'a base that moved on still holds the merged commit')
        self.assertEqual(self.chain_of(merge, tip, tip)['unpublished'], ['b.c'], 'a PR head on the merged side is no chain commit, so it publishes nothing')
        self.assertEqual(self.refusal('chain', merge, two, '--published', merge, '--base', 'e' * 40), '', 'a chain without a merge never reads the base')

    def test_a_merge_owns_each_path_whose_entry_differs_from_both_parents(self):
        self.write('l.c', ''.join(f'{n}\n' for n in range(1, 8)))
        lines = self.commit('lines', 'l.c')
        conflict = {'base_files': {'a.c': 'base\n'}, 'pr_files': {'a.c': 'pr\n'}}
        for name, case, owned in (
                ('a novel resolution', {**conflict, 'resolve': lambda: self.write('a.c', 'resolved\n')}, ['a.c']),
                ("the base's side of a conflict", {**conflict, 'resolve': lambda: self.write('a.c', 'base\n')}, []),
                ("the PR's side of a conflict", {**conflict, 'resolve': lambda: self.write('a.c', 'pr\n')}, []),
                ('a clean merge of both sides of one file',
                 {'start': lines, 'base_files': {'l.c': '1b\n2\n3\n4\n5\n6\n7\n'}, 'pr_files': {'l.c': '1\n2\n3\n4\n5\n6\n7p\n'}}, ['l.c']),
                ('a new mode', {'resolve': lambda: (self.repo / 'a.c').chmod(0o755)}, ['a.c']),
                ('a deletion of a file both parents hold', {'resolve': lambda: self.git('rm', '-q', 'a.c')}, ['a.c'])):
            with self.subTest(name):
                self.git('checkout', '-q', '--detach', self.base)
                for branch in ('main', 'pr'):
                    subprocess.run(['git', 'branch', '-q', '-D', branch], cwd=self.repo, env=ENV, capture_output=True)
                one, tip, merge = self.base_merge(**case)
                self.assertEqual(self.chain_of(merge, one, tip)['unpublished'], owned)

    def test_a_merge_that_changes_no_path_is_still_a_link(self):
        self.git('checkout', '-q', '-b', 'main')
        self.write('m.c', 'm\n')
        tip = self.commit('base moved', 'm.c')
        self.git('checkout', '-q', '-b', 'pr', self.base)
        self.git('merge', '-q', '--no-ff', '-s', 'ours', '-m', 'Merge the base, keeping ours', tip)
        merge = self.git('rev-parse', 'HEAD').strip()
        out = self.chain_of(merge, self.base, tip)
        self.assertEqual((out['commits'], out['paths'], out['refusal']), ([merge], [], ''))

    def test_a_merge_the_base_cannot_vouch_for_is_refused(self):
        one, tip, merge = self.base_merge(message='Merge the base\n\nClaude-Session: https://claude.ai/code/session_x')
        self.assertIn('commit message carries attribution', self.chain_refusal(self.base, merge, tip))
        self.git('checkout', '-q', '-b', 'other', self.base)
        self.write('o.c', 'o\n')
        other = self.commit('other', 'o.c')
        self.git('checkout', '-q', '--detach', one)
        self.git('merge', '-q', '--no-ff', '--no-edit', tip, other)
        self.assertIn('has 3 parents', self.chain_refusal(self.base, self.git('rev-parse', 'HEAD').strip(), tip))
        self.assertIn(f'not on the PR\'s base {self.base[:7]}', self.chain_refusal(self.base, merge, self.base),
                      'the merged commit is newer than the base given')

    def test_a_merge_link_needs_the_base_in_the_checkout_and_reports_git_failing(self):
        one, tip, merge = self.base_merge()
        code, out = self.run_script('chain', self.base, merge, '--published', one, '--base', 'e' * 40)
        self.assertEqual(code, 2)
        self.assertEqual(out['error'], f"the PR's base {'e' * 40} is not in the checkout: fetch it")
        cwd = os.getcwd()
        os.chdir(self.repo)
        try:
            with self.assertRaisesRegex(commits.Unusable, 'merge-base --is-ancestor 1{40} .*: fatal: Not a valid commit name 1{40}'):
                commits.on_base('1' * 40, tip)
        finally:
            os.chdir(cwd)

    def test_a_message_crediting_an_agent_is_refused(self):
        for line, said in (
                ('Co-Authored-By: Claude Sonnet 5 <noreply@anthropic.com>', 'Co-Authored-By: Claude Sonnet 5'),
                # A human co-author is still not this run's to claim.
                ('co-authored-by: Ha Thach <thach@tinyusb.org>', 'co-authored-by: Ha Thach'),
                ('Claude-Session: https://claude.ai/code/session_01BtCFFdnNVDJfQU8Y1umjeQ', 'Claude-Session:'),
                ('Codex-Session-Id: 0192', 'Codex-Session-Id: 0192'),
                ('Session-URL: https://example', 'Session-URL:'),
                ('🤖 Generated with [Claude Code](https://claude.com/claude-code)', '🤖 Generated with [Claude Code]'),
                ('Generated by Codex', 'Generated by Codex'),
                # \b is ASCII, as in the JavaScript it was ported from.
                ('Generated by Codex助手', 'Generated by Codex助手'),
                ('Generated with ChatGPT', 'Generated with ChatGPT'),
                ('Authored-by: Claude', 'Authored-by: Claude'),
                ('Written by an AI agent', 'Written by an AI agent'),
                ('https://claude.ai/code/session_01BtCFFdnNVDJfQU8Y1umjeQ', 'https://claude.ai/code/session_01BtCFFdnNVDJfQU8Y1umjeQ'),
                # A session link anywhere on a line, in any of its forms, a bare prefix included.
                ('See https://claude.ai/code/session_01Bt for the run.', 'See https://claude.ai/code/session_01Bt for the run.'),
                ('Run: [log](https://claude.ai/code/session_x)', 'Run: [log](https://claude.ai/code/session_x)'),
                ('mentions claude.ai/code/session_', 'mentions claude.ai/code/session_'),
                ('Thread https://chatgpt.com/c/abc', 'Thread https://chatgpt.com/c/abc'),
                ('Task https://chatgpt.com/codex/tasks/t', 'Task https://chatgpt.com/codex/tasks/t'),
                ('Shared https://chatgpt.com/share/abc', 'Shared https://chatgpt.com/share/abc')):
            self.git('reset', '-q', '--hard', self.base)
            self.write('b.c', line)
            sha = self.commit(f'Fix the finding\n\n{line}\n', 'b.c')
            self.assertIn(f'commit message carries attribution: {said}', self.chain_refusal(self.base, sha), line)
            self.assertIn(f'commit message carries attribution: {said}', self.refusal('head', '--parent', self.base, 'b.c'), line)

    def test_a_crlf_session_link_is_attribution_too(self):
        self.write('b.c', 'b\n')
        self.git('add', 'b.c')
        self.git('commit', '-q', '--cleanup=verbatim', '-m', 'Fix it\r\n\r\nhttps://claude.ai/code/session_abc\r\nMore.\r\n')
        sha = self.git('rev-parse', 'HEAD').strip()
        self.assertIn('carries attribution', self.chain_refusal(self.base, sha))
        self.assertIn('carries attribution', self.refusal('head', '--parent', self.base, 'b.c'))

    def test_the_first_offending_line_is_named_and_unicode_lookalikes_are_not_recognized(self):
        facts = sys.modules['facts']
        self.assertEqual(facts.attribution_in('Fix\n\nGenerated by Codex\nCo-Authored-By: x\n'), 'Generated by Codex')
        for text in ('Fix\n\nſession-url: x', 'Fix\n\nClaude-Seſsion: x', 'claude.aı/code/session_abc', 'Generated by GPTimer'):
            self.assertIsNone(facts.attribution_in(text), text)

    def test_a_message_that_talks_about_attribution_is_not_attribution(self):
        for message in (
                'Fix handling of claude.ai/code/ URLs in the reply script\n',
                'Session: reject expired tokens\n',
                'Fix the finding\n\nSigned-off-by: Ha Thach <thach@tinyusb.org>\n',
                'Refuse a commit generated with a session trailer\n\nThe audit reads the message back.\n',
                'Drop the Generated-with footer from PR bodies\n',
                'Generated by GPTimer\n\nWritten by an aide, generated by an aircraft simulator.\n'):
            self.git('reset', '-q', '--hard', self.base)
            self.write('b.c', message)
            sha = self.commit(message, 'b.c')
            self.assertEqual(self.chain_refusal(self.base, sha), '', message)

    def test_a_name_that_is_not_utf8_is_an_error_not_a_lookalike(self):
        self.write('bad\ufffd.c', 'owned\n')
        (self.repo / b'bad\xff.c'.decode('utf-8', 'surrogateescape')).write_text('stray\n')
        self.commit('both', '.')
        code, out = self.run_script('head', '--parent', self.base, 'bad\ufffd.c')
        self.assertEqual(code, 2)
        self.assertIn('not UTF-8', out['error'])

    def test_commit_takes_exactly_its_paths_with_the_message_on_stdin(self):
        for f in ('-x.c', 'space name.c', 'other.c'):
            self.write(f, 'new\n')
        self.git('add', '--', 'other.c')
        code, out = self.run_script('commit', '-x.c', 'space name.c', stdin='Fix the thing\n\nWhy.\n')
        self.assertEqual((code, out['committed']), (0, True), out)
        self.assertEqual(sorted(self.git('show', '--name-only', '--format=', 'HEAD').split('\n')[:-1]), ['-x.c', 'space name.c'])
        self.assertEqual(self.git('log', '-1', '--format=%B'), 'Fix the thing\n\nWhy.\n\n')
        self.assertIn('A  other.c', self.git('status', '--porcelain'), 'what was staged beside it stays staged, uncommitted')

    def test_commit_refused_by_a_hook_is_not_committed(self):
        self.hook('echo "trailing whitespace fixed in a.c" >&2\nexit 1')
        self.write('a.c', 'changed\n')
        code, out = self.run_script('commit', 'a.c', stdin='Fix\n')
        self.assertEqual((code, out['committed']), (0, False))
        self.assertIn('trailing whitespace', out['detail'])
        self.assertEqual(self.git('rev-parse', 'HEAD').strip(), self.base)

    def test_commit_reads_a_path_named_like_pathspec_magic_literally(self):
        self.write(':(top)*', 'odd\n')
        self.write('a.c', 'changed\n')
        code, out = self.run_script('commit', ':(top)*', stdin='Fix\n')
        self.assertEqual((code, out['committed']), (0, True), out)
        self.assertEqual(self.git('show', '--name-only', '--format=', 'HEAD').split('\n')[:-1], [':(top)*'])
        self.assertIn(' M a.c', self.git('status', '--porcelain'))
        code, seen = self.run_script('head', '--parent', self.base, ':(top)*')
        self.assertEqual((code, seen['refusal'], len(seen['entries'])), (0, '', 1), 'the audit reads back the same one file')

    def test_a_hook_that_moves_head_and_fails_is_no_clean_refusal(self):
        self.hook('git commit -q --allow-empty -m nested --no-verify\nexit 1')
        self.write('a.c', 'changed\n')
        code, out = self.run_script('commit', 'a.c', stdin='Fix\n')
        self.assertEqual(code, 2)
        self.assertIn('HEAD moved', out['error'])

    def test_a_blank_message_commits_and_stages_nothing(self):
        self.write('a.c', 'changed\n')
        code, out = self.run_script('commit', 'a.c', stdin=' \n\n')
        self.assertEqual((code, out['committed']), (0, False))
        self.assertIn('blank', out['detail'])
        self.assertEqual(self.git('status', '--porcelain'), ' M a.c\n', 'nothing was staged')

    def test_errors(self):
        chain = ['chain', self.base, self.base, '--published', self.base]
        for argv, want in ((['chain', 'HEAD~1', 'HEAD', '--published', 'HEAD~1', '--base', self.base], 'not a full SHA'),
                           ([*chain[:4], 'not-a-sha', '--base', self.base], 'not a full SHA'), (['chain', self.base], 'usage'), (['chain', self.base, self.base], 'usage'),
                           (chain, 'usage'), ([*chain, '--base', 'HEAD'], 'not a full SHA'),
                           (['head'], 'usage'), (['head', 'a.c'], 'usage'), (['head', '--parent', 'HEAD', 'a.c'], 'not a full SHA'),
                           (['frob'], 'usage'), (['commit'], 'usage'),
                           (['chain', self.base, 'f' * 40, '--published', self.base, '--base', self.base], 'git rev-list')):
            code, out = self.run_script(*argv)
            self.assertEqual(code, 2, argv)
            self.assertIn(want, out['error'], argv)


if __name__ == '__main__':
    unittest.main()
