"""lib/gh_pr_comment.py: the shared plumbing of a CI report posted on a PR by a later job (#471)."""
import io
import json
import os
import subprocess
import sys
import tempfile
import unittest
from contextlib import redirect_stderr, redirect_stdout
from pathlib import Path
from unittest import mock

sys.path.insert(0, str(Path(__file__).resolve().parents[1] / 'lib'))
import gh_pr_comment as G  # noqa: E402

REPO = 'adafruit/tinyuf2'
SHA = 'a' * 40


def api_of(answers):
    """A fake `gh`: each path prefix answers its value, or raises it when it is an exception."""
    calls = []

    def api(path, paginate_key=None):
        calls.append((path, paginate_key))
        for prefix, value in answers.items():
            if path.startswith(prefix):
                if isinstance(value, Exception):
                    raise value
                return value
        raise AssertionError(f'unexpected {path}')
    api.calls = calls
    return api


def same_repo_pr(n, repo=REPO):
    return {'number': n, 'base': {'repo': {'url': f'https://api.github.com/repos/{repo}'}}}


class PrOfRun(unittest.TestCase):
    def test_a_same_repo_pr_comes_from_pull_requests_whose_base_is_this_repo(self):
        api = api_of({})
        self.assertEqual(G.pr_of_run(REPO, head_sha=SHA, pull_requests=[same_repo_pr(7), same_repo_pr(9, 'x/y')],
                                     api=api), (7, ''))
        self.assertEqual(api.calls, [])

    def test_a_fork_pr_is_found_by_head_repo_branch_and_sha_with_the_branch_encoded(self):
        pulls = [{'number': 3, 'head': {'sha': SHA}, 'base': {'repo': {'full_name': REPO}}},
                 {'number': 4, 'head': {'sha': 'b' * 40}, 'base': {'repo': {'full_name': REPO}}},
                 {'number': 5, 'head': {'sha': SHA}, 'base': {'repo': {'full_name': 'other/repo'}}}]
        api = api_of({f'repos/{REPO}/pulls': pulls})
        self.assertEqual(G.pr_of_run(REPO, head_sha=SHA, head_branch='fix#1+a&b', head_repo='me/tinyuf2', api=api),
                         (3, ''))
        self.assertIn('head=me%3Afix%231%2Ba%26b', api.calls[0][0])

    def test_none_or_several_is_no_pr_and_an_api_error_is_unknown(self):
        self.assertEqual(G.pr_of_run(REPO, head_sha=SHA, api=api_of({}))[0], None)
        n, why = G.pr_of_run(REPO, head_sha=SHA, pull_requests=[same_repo_pr(1), same_repo_pr(2)], api=api_of({}))
        self.assertEqual((n, why), (None, 'PRs [1, 2] share this run'))
        with self.assertRaises(G.ApiError):
            G.pr_of_run(REPO, head_sha=SHA, head_branch='b', head_repo='me/r',
                        api=api_of({'repos/': G.ApiError('HTTP 502')}))


class JobAttempt(unittest.TestCase):
    def test_the_attempt_the_job_ran_in_or_none_when_it_did_not_run(self):
        path = f'repos/{REPO}/actions/runs/11/attempts/2/jobs'
        for jobs, want in (([{'name': 'hil-run', 'conclusion': 'success', 'run_attempt': 1}], 1),
                           ([{'name': 'hil-run', 'conclusion': 'failure'}], 2),
                           ([{'name': 'hil-run', 'conclusion': 'skipped', 'run_attempt': 2}], None),
                           ([{'name': 'hil-run', 'conclusion': None}], None), ([{'name': 'other'}], None)):
            with self.subTest(jobs=jobs):
                self.assertEqual(G.job_attempt(REPO, 11, 2, 'hil-run', api=api_of({path: jobs})), want)


def head(sha=SHA, ref='feature', repo=REPO):
    return {'sha': sha, 'ref': ref, 'repo': {'full_name': repo}}


def run(id, number, prs=(), title='HIL PR #7', branch='feature', repo=REPO):
    return {'id': id, 'run_number': number, 'display_title': title, 'head_branch': branch,
            'head_repository': {'full_name': repo}, 'pull_requests': [{'number': n} for n in prs]}


class Fresh(unittest.TestCase):
    def fresh(self, runs, pr_head=None, attempt_now=1, attempt=1, run_id=100, run_name=None):
        api = api_of({f'repos/{REPO}/pulls/7': {'head': pr_head or head()},
                      f'repos/{REPO}/actions/workflows/hil.yml/runs': runs,
                      f'repos/{REPO}/actions/runs/{run_id}': {'run_attempt': attempt_now}})
        return G.fresh(REPO, pr=7, head_sha=SHA, run_id=run_id, attempt=attempt, workflow='hil.yml',
                       run_name=run_name, api=api), api

    def test_the_newest_run_by_number_and_its_live_attempt_is_current(self):
        (ok, why), api = self.fresh([run(90, 4, [7]), run(100, 5, [7])][::-1])
        self.assertEqual((ok, why), (True, 'current'))
        self.assertEqual(api.calls[1][1], 'workflow_runs', 'every page is read')

    def test_list_order_does_not_decide(self):
        (ok, why), _ = self.fresh([run(100, 5, [7]), run(120, 6, [7])])
        self.assertFalse(ok)
        self.assertIn('run 120 is newer', why)

    def test_a_moved_head_a_newer_attempt_or_no_run_is_stale(self):
        self.assertIn('head moved', self.fresh([run(100, 5, [7])], pr_head=head(sha='b' * 40))[0][1])
        self.assertIn('attempt 2 is newer', self.fresh([run(100, 5, [7])], attempt_now=2)[0][1])
        self.assertIn('no hil.yml run', self.fresh([run(100, 5, [8])])[0][1])

    def test_a_newer_run_listing_this_pr_among_others_supersedes_it(self):
        # tinyusb's rule: pull_requests trusted as listed
        self.assertIn('newer', self.fresh([run(100, 5, [7]), run(120, 6, [7, 8])])[0][1])

    def test_a_fork_run_unlisted_in_pull_requests_counts_by_branch_and_repo(self):
        fork = head(repo='me/tinyuf2')
        self.assertTrue(self.fresh([run(100, 5, repo='me/tinyuf2')], pr_head=fork)[0][0])
        self.assertFalse(self.fresh([run(100, 5, repo='other/tinyuf2')], pr_head=fork)[0][0])

    def test_by_run_name_another_pr_on_the_same_head_is_not_this_ones(self):
        runs = [run(100, 5, [7, 8], title='HIL PR #7'), run(120, 6, [7, 8], title='HIL PR #8')]
        self.assertTrue(self.fresh(runs, run_name='HIL PR #7')[0][0])
        self.assertFalse(self.fresh(runs)[0][0])

    def test_an_api_error_or_a_malformed_answer_is_unknown(self):
        api = api_of({f'repos/{REPO}/pulls/7': G.ApiError('HTTP 500')})
        with self.assertRaises(G.ApiError):
            G.fresh(REPO, pr=7, head_sha=SHA, run_id=1, attempt=1, workflow='hil.yml', api=api)
        for runs in ([{'id': 100, 'pull_requests': [{'number': 7}]}], [run(100, 'x', [7])]):
            with self.subTest(runs=runs), self.assertRaises(G.ApiError):
                self.fresh(runs)


class Artifacts(unittest.TestCase):
    def setUp(self):
        self.base = Path(self.enterContext(tempfile.TemporaryDirectory()))

    def test_only_a_regular_object_file_inside_the_base_within_the_size_is_read(self):
        (self.base / 'ok.json').write_text('{"a": 1}')
        (self.base / 'list.json').write_text('[1]')
        (self.base / 'big.json').write_text('{"a": "' + 'x' * 100 + '"}')
        (self.base / 'bad.json').write_text('{')
        outside = self.base.parent / f'{self.base.name}-outside.json'
        outside.write_text('{"a": 1}')
        self.addCleanup(outside.unlink)
        (self.base / 'link.json').symlink_to(outside)
        self.assertEqual(G.read_artifact_json(self.base, 'ok.json'), {'a': 1})
        for rel in ('list.json', 'bad.json', 'link.json', 'missing.json', f'../{outside.name}'):
            with self.subTest(rel=rel):
                self.assertIsNone(G.read_artifact_json(self.base, rel))
        self.assertIsNone(G.read_artifact_json(self.base, 'big.json', max_bytes=50))

    def test_literal_renders_nothing_inside_and_escapes_table_pipes(self):
        self.assertEqual(G.literal('a ``b`` c'), '```a ``b`` c```')
        self.assertEqual(G.literal('`x'), '`` `x ``')
        self.assertEqual(G.literal('a|b', table=True), '`a\\|b`')
        self.assertEqual(G.literal('one\ntwo   three'), '`one two three`')
        self.assertEqual(G.literal(''), '')
        self.assertEqual(len(G.literal('x' * 500)), 302)

    def test_mentions_are_broken_and_a_long_body_is_cut_at_a_line_end(self):
        self.assertEqual(G.neutralize_mentions('hi @hathach and @org/team, a@b'), 'hi @​hathach and '
                                                                                   '@​org/team, a@​b')
        text = ''.join(f'line {i} `code`\n' for i in range(100))
        out = G.cap(text, limit=200)
        self.assertLessEqual(len(out), 200)
        self.assertTrue(out.endswith("…truncated: see the run's artifacts.\n"))
        self.assertEqual(out.count('`') % 2, 0)
        self.assertEqual(G.cap('short\n', limit=200), 'short\n')


class Cli(unittest.TestCase):
    def main(self, argv, api):
        out, err = io.StringIO(), io.StringIO()
        with redirect_stdout(out), redirect_stderr(err):
            rc = G.main(argv, api=api)
        return rc, out.getvalue(), err.getvalue()

    def test_exit_codes(self):
        self.assertEqual(self.main(['pr', '--repo', REPO, '--head-sha', SHA, '--prs', json.dumps([same_repo_pr(7)])],
                                   api_of({}))[:2], (0, '7\n'))
        rc, out, err = self.main(['pr', '--repo', REPO, '--head-sha', SHA, '--head-branch', 'b', '--head-repo', 'me/r'],
                                 api_of({'repos/': G.ApiError('HTTP 502')}))
        self.assertEqual((rc, out), (2, ''))
        self.assertIn('unknown: HTTP 502', err)
        fresh = ['fresh', '--repo', REPO, '--pr', '7', '--run-id', '100', '--attempt', '1', '--head-sha', SHA,
                 '--workflow', 'hil.yml']
        answers = {f'repos/{REPO}/pulls/7': {'head': head()}, f'repos/{REPO}/actions/runs/100': {'run_attempt': 1}}
        self.assertEqual(self.main(fresh, api_of({**answers, f'repos/{REPO}/actions/workflows': [run(100, 5, [7])]}))
                         [:2], (0, 'current\n'))
        rc, out, _ = self.main(fresh, api_of({**answers, f'repos/{REPO}/actions/workflows': [run(120, 6, [7])]}))
        self.assertEqual(rc, 1)
        self.assertTrue(out.startswith('stale: '))
        self.assertEqual(self.main(['job-attempt', '--repo', REPO, '--run-id', '1', '--attempt', '1', '--job', 'j'],
                                   api_of({'repos/': [{'name': 'j', 'conclusion': 'skipped'}]}))[:2], (0, ''))


class Gh(unittest.TestCase):
    def test_paginated_lines_a_failure_and_a_timeout(self):
        ok = subprocess.CompletedProcess([], 0, '{"a": 1}\n{"a": 2}\n', '')
        with mock.patch.object(G.subprocess, 'run', return_value=ok) as r:
            self.assertEqual(G.gh('repos/x/y/pulls', 'items'), [{'a': 1}, {'a': 2}])
        self.assertIn('--paginate', r.call_args.args[0])
        with mock.patch.object(G.subprocess, 'run', return_value=subprocess.CompletedProcess([], 1, '', 'HTTP 404')), \
                self.assertRaisesRegex(G.ApiError, 'HTTP 404'):
            G.gh('x')
        with mock.patch.object(G.subprocess, 'run', side_effect=subprocess.TimeoutExpired('gh', 60)), \
                self.assertRaisesRegex(G.ApiError, 'no reply'):
            G.gh('x')
        with mock.patch.object(G.subprocess, 'run', side_effect=FileNotFoundError('gh')), \
                self.assertRaises(G.ApiError):
            G.gh('x')


if __name__ == '__main__':
    unittest.main()
