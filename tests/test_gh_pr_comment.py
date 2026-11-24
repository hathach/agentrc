"""skills/hil/scripts/gh_pr_comment.py: the shared plumbing of a CI report posted on a PR by a later job (#471)."""
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

sys.path.insert(0, str(Path(__file__).resolve().parents[1] / 'skills' / 'hil' / 'scripts'))
import gh_pr_comment as G  # noqa: E402

sys.path.insert(0, str(Path(__file__).resolve().parent))
from helpers import guard_survivors  # noqa: E402

REPO = 'adafruit/tinyuf2'
SHA = 'a' * 40


def setUpModule():
    guard_survivors(__name__)


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
                           ([{'name': 'hil-run', 'conclusion': None}], None), ([{'name': 'other'}], None),
                           ([{'name': 'build', 'conclusion': 'success', 'run_attempt': 2},
                             {'name': 'hil-run', 'conclusion': 'success', 'run_attempt': 1},
                             {'name': 'report', 'conclusion': 'failure', 'run_attempt': 2}], 1)):
            with self.subTest(jobs=jobs):
                self.assertEqual(G.job_attempt(REPO, 11, 2, 'hil-run', api=api_of({path: jobs})), want)

    def test_two_jobs_of_the_name_are_unknown(self):
        """CodeRabbit on #574: the first of two same-named jobs answered for both."""
        jobs = [{'id': 5, 'name': 'hil-run', 'conclusion': 'skipped'}, {'id': 6, 'name': 'other'},
                {'id': 7, 'name': 'hil-run', 'conclusion': 'success', 'run_attempt': 1}]
        with self.assertRaisesRegex(G.ApiError, r'\b5\b.*\b7\b'):
            G.job_attempt(REPO, 11, 2, 'hil-run', api=api_of({'repos/': jobs}))


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
        # tinyusb's case: a newer run from the same fork on another branch is not this PR's
        self.assertEqual(self.fresh([run(100, 5, repo='me/tinyuf2'), run(120, 6, repo='me/tinyuf2', branch='other')],
                                    pr_head=fork)[0], (True, 'current'))

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


MARKER = '<!-- Sticky Pull Request Commenthil-report -->'


def comment(cid, body=f'old\n{MARKER}', login='github-actions', minimized=False):
    return {'databaseId': cid, 'author': None if login is None else {'login': login}, 'isMinimized': minimized,
            'body': body}


def post_api(comments, runs=None, viewer='github-actions[bot]', attempt_now=1, writes=None):
    """A fake `gh` for post: the GraphQL viewer and comment pages, fresh's answers, and the writes."""
    calls = []
    runs = [run(100, 5, [7])] if runs is None else runs

    def api(path, paginate_key=None, *, method=None, fields=None, payload=None):
        calls.append({'path': path, 'key': paginate_key, 'method': method, 'fields': fields, 'payload': payload})
        if path == 'graphql':
            if paginate_key:
                return comments() if callable(comments) else comments
            return {'data': {'viewer': {'login': viewer}}}
        if method:
            return writes if writes is not None else {'html_url': f'https://github.com/{REPO}/pull/7#c'}
        for prefix, value in ((f'repos/{REPO}/pulls/7', {'head': head()}),
                              (f'repos/{REPO}/actions/workflows/hil.yml/runs', runs),
                              (f'repos/{REPO}/actions/runs/100', {'run_attempt': attempt_now})):
            if path.startswith(prefix):
                return value
        raise AssertionError(f'unexpected {path}')
    api.calls = calls
    return api


def post(api, body='## HIL\n', header='hil-report', run_name=None):
    return G.post(REPO, pr=7, header=header, body=body, head_sha=SHA, run_id=100, attempt=1, workflow='hil.yml',
                  run_name=run_name, api=api)


def writes(api):
    return [c for c in api.calls if c['method']]


class Post(unittest.TestCase):
    def test_no_comment_of_ours_creates_one_with_the_sticky_marker_after_the_body(self):
        api = post_api([comment(1, login='hathach'), comment(2, body='no marker'),
                        comment(3, body='<!-- Sticky Pull Request Commentcode-size -->')])
        self.assertEqual(post(api), (True, f'created https://github.com/{REPO}/pull/7#c'))
        (w,) = writes(api)
        self.assertEqual((w['path'], w['method']), (f'repos/{REPO}/issues/7/comments', 'POST'))
        self.assertEqual(w['payload'], {'body': f'## HIL\n\n{MARKER}'}, "marocchino's bodyWithHeader")

    def test_our_visible_marker_comment_is_updated_and_a_minimized_one_is_left(self):
        api = post_api([comment(5, minimized=True), comment(6, login='hathach'), comment(9)])
        self.assertTrue(post(api)[0])
        (w,) = writes(api)
        self.assertEqual((w['path'], w['method']), (f'repos/{REPO}/issues/comments/9', 'PATCH'))
        self.assertTrue(w['payload']['body'].endswith(f'\n{MARKER}'))

    def test_the_author_matches_with_or_without_the_bot_suffix(self):
        for viewer, login in (('github-actions[bot]', 'github-actions'), ('hathach', 'hathach'),
                              ('github-actions[bot]', 'github-actions[bot]')):
            with self.subTest(viewer=viewer, login=login):
                api = post_api([comment(9, login=login)], viewer=viewer)
                post(api)
                self.assertEqual(writes(api)[0]['method'], 'PATCH')
        api = post_api([comment(9, login=None)])
        post(api)
        self.assertEqual(writes(api)[0]['method'], 'POST', 'a deleted author is not us')

    def test_every_comment_page_is_read_through_one_paginated_query(self):
        api = post_api([comment(9)])
        post(api)
        q = [c for c in api.calls if c['path'] == 'graphql' and c['key']]
        self.assertEqual(len(q), 1)
        self.assertEqual(q[0]['key'], 'data.repository.pullRequest.comments.nodes')
        self.assertEqual({k: v for k, v in q[0]['fields'].items() if k != 'query'},
                         {'owner': 'adafruit', 'name': 'tinyuf2', 'number': 7})
        self.assertIn('$endCursor', q[0]['fields']['query'])
        self.assertIn('pageInfo { hasNextPage endCursor }', q[0]['fields']['query'])

    def test_two_visible_marker_comments_of_ours_are_ambiguous_and_nothing_is_written(self):
        api = post_api([comment(5), comment(9), comment(3, minimized=True)])
        with self.assertRaisesRegex(G.ApiError, r'\[5, 9\].*ambiguous'):
            post(api)
        self.assertEqual(writes(api), [])

    def test_a_stale_run_or_attempt_writes_nothing(self):
        for api, why in ((post_api([comment(9)], attempt_now=2), 'attempt 2 is newer'),
                         (post_api([], runs=[run(100, 5, [7]), run(120, 6, [7])]), 'run 120 is newer')):
            with self.subTest(why=why):
                ok, got = post(api)
                self.assertFalse(ok)
                self.assertIn(why, got)
                self.assertEqual(writes(api), [])

    def test_freshness_is_checked_after_the_lookup_right_before_the_write(self):
        api = post_api([comment(9)])
        post(api)
        order = [c['path'] if c['path'] != 'graphql' else ('comments' if c['key'] else 'viewer') for c in api.calls]
        self.assertEqual(order[:2], ['viewer', 'comments'])
        self.assertEqual(order[-2:], [f'repos/{REPO}/actions/runs/100', f'repos/{REPO}/issues/comments/9'])

    def test_a_long_body_is_capped_with_the_marker_kept(self):
        api = post_api([])
        post(api, body='line\n' * 20000)
        body = writes(api)[0]['payload']['body']
        self.assertLessEqual(len(body), G.MAX_COMMENT + len(MARKER) + 1)
        self.assertTrue(body.endswith(f"artifacts.\n\n{MARKER}"))

    def test_a_blank_body_or_a_marker_breaking_header_refuses_before_any_call(self):
        for body, header in (('  \n', 'hil-report'), ('x', ''), ('x', 'a-->b'), ('x', 'a\nb')):
            with self.subTest(body=body, header=header):
                api = post_api([])
                with self.assertRaises(ValueError):
                    post(api, body=body, header=header)
                self.assertEqual(api.calls, [])

    def test_a_malformed_or_failed_lookup_is_unknown(self):
        def failed():
            raise G.ApiError('HTTP 502')
        ours = {'author': {'login': 'github-actions'}, 'body': MARKER}
        for comments in (failed, [ours], [{**ours, 'databaseId': 'x'}]):
            with self.subTest(comments=comments):
                api = post_api(comments)
                with self.assertRaises(G.ApiError):
                    post(api)
                self.assertEqual(writes(api), [])


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

    def test_job_attempt_of_a_name_two_jobs_share_exits_unknown(self):
        jobs = [{'id': 5, 'name': 'j', 'conclusion': 'success'}, {'id': 7, 'name': 'j', 'conclusion': 'success'}]
        rc, out, err = self.main(['job-attempt', '--repo', REPO, '--run-id', '1', '--attempt', '1', '--job', 'j'],
                                 api_of({'repos/': jobs}))
        self.assertEqual((rc, out), (2, ''))
        self.assertTrue(err.startswith('unknown: '))

    def test_prs_that_are_not_a_json_list_exit_unknown_and_an_empty_list_looks_up_a_fork(self):
        """CodeRabbit on #574: malformed --prs read as no PR, so a same-repo run fell to the fork lookup."""
        pulls = [{'number': 3, 'head': {'sha': SHA}, 'base': {'repo': {'full_name': REPO}}}]
        argv = ['pr', '--repo', REPO, '--head-sha', SHA, '--head-branch', 'b', '--head-repo', 'me/r']
        for prs in ('{', '{}', '7', 'null'):
            with self.subTest(prs=prs):
                rc, out, err = self.main([*argv, '--prs', prs], api_of({}))
                self.assertEqual((rc, out), (2, ''))
                self.assertIn('--prs', err)
        self.assertEqual(self.main([*argv, '--prs', '[]'], api_of({f'repos/{REPO}/pulls': pulls}))[:2], (0, '3\n'))

    def test_a_fork_lookup_matching_several_prs_posts_nothing(self):
        pulls = [{'number': n, 'head': {'sha': SHA}, 'base': {'repo': {'full_name': REPO}}} for n in (3, 4)]
        rc, out, err = self.main(['pr', '--repo', REPO, '--head-sha', SHA, '--head-branch', 'b', '--head-repo',
                                  'me/r'], api_of({f'repos/{REPO}/pulls': pulls}))
        self.assertEqual((rc, out), (0, ''))
        self.assertIn('PRs [3, 4] share this run: no comment', err)

    def test_job_attempt_and_fresh_api_failures_exit_unknown(self):
        job = ['job-attempt', '--repo', REPO, '--run-id', '1', '--attempt', '1', '--job', 'hil-run']
        fresh = ['fresh', '--repo', REPO, '--pr', '7', '--run-id', '100', '--attempt', '1', '--head-sha', SHA,
                 '--workflow', 'hil.yml']
        broken = {f'repos/{REPO}/pulls/7': {'head': head()}, 'repos/': G.ApiError('HTTP 503')}
        for argv, answers in ((job, {'repos/': G.ApiError('HTTP 503')}), (fresh, broken),
                              (fresh, {f'repos/{REPO}/pulls/7': {'no head': 1}})):
            with self.subTest(argv=argv[0], answers=answers):
                rc, out, err = self.main(argv, api_of(answers))
                self.assertEqual((rc, out), (2, ''))
                self.assertTrue(err.startswith('unknown: '))


class PostCli(unittest.TestCase):
    def setUp(self):
        self.body = Path(self.enterContext(tempfile.TemporaryDirectory())) / 'comment.md'
        self.body.write_text('## HIL\n', encoding='utf-8')
        self.argv = ['post', '--repo', REPO, '--pr', '7', '--header', 'hil-report', '--body-file', str(self.body),
                     '--run-id', '100', '--attempt', '1', '--head-sha', SHA, '--workflow', 'hil.yml']

    def main(self, argv, api):
        out, err = io.StringIO(), io.StringIO()
        with redirect_stdout(out), redirect_stderr(err):
            rc = G.main(argv, api=api)
        return rc, out.getvalue(), err.getvalue()

    def test_posted_stale_ambiguous_and_unreadable_exit_codes(self):
        rc, out, _ = self.main(self.argv, post_api([comment(9)]))
        self.assertEqual((rc, out), (0, f'updated https://github.com/{REPO}/pull/7#c\n'))
        rc, out, _ = self.main(self.argv, post_api([], attempt_now=2))
        self.assertEqual(rc, 1)
        self.assertTrue(out.startswith('stale: '))
        rc, out, err = self.main(self.argv, post_api([comment(5), comment(9)]))
        self.assertEqual((rc, out), (2, ''))
        self.assertIn('unknown: ', err)
        self.body.write_text('')
        rc, out, err = self.main(self.argv, post_api([]))
        self.assertEqual((rc, out), (2, ''))
        self.assertIn('not posted: the body is blank', err)
        self.body.unlink()
        rc, _, err = self.main(self.argv, post_api([]))
        self.assertEqual(rc, 2)
        self.assertIn('not posted: ', err)

    def test_run_name_matches_by_display_title(self):
        runs = [run(100, 5, [7, 8], title='HIL PR #7'), run(120, 6, [7, 8], title='HIL PR #8')]
        self.assertEqual(self.main([*self.argv, '--run-name', 'HIL PR #7'], post_api([], runs=runs))[0], 0)
        self.assertEqual(self.main(self.argv, post_api([], runs=runs))[0], 1)


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

    def test_a_write_sends_its_payload_on_stdin_and_graphql_fields_are_typed(self):
        ok = subprocess.CompletedProcess([], 0, '{"id": 1}', '')
        with mock.patch.object(G.subprocess, 'run', return_value=ok) as r:
            self.assertEqual(G.gh('repos/x/y/issues/7/comments', method='POST', payload={'body': 'x' * 70000}),
                             {'id': 1})
        argv = r.call_args.args[0]
        self.assertEqual(argv[-4:], ['POST', '--input', '-', 'repos/x/y/issues/7/comments'])
        self.assertEqual(json.loads(r.call_args.kwargs['input']), {'body': 'x' * 70000})
        self.assertFalse(any(len(a) > 1000 for a in argv), 'the body is not on the command line')
        with mock.patch.object(G.subprocess, 'run', return_value=ok) as r:
            G.gh('graphql', 'data.n', fields={'query': 'q', 'name': 'r', 'number': 7})
        argv = r.call_args.args[0]
        self.assertIn(['-f', 'name=r'], [argv[i:i + 2] for i in range(len(argv))])
        self.assertIn(['-F', 'number=7'], [argv[i:i + 2] for i in range(len(argv))])
        self.assertIsNone(r.call_args.kwargs['input'])


if __name__ == '__main__':
    unittest.main()
