"""Tests for pr-babysit's harvest.py: the settle rules on fixture artifacts, and one end-to-end read through a fake gh."""
import base64
import hashlib
import importlib.util
import io
import json
import sys
import tempfile
import unittest
from contextlib import redirect_stdout
from datetime import datetime, timedelta
from pathlib import Path
from unittest import mock

SCRIPT = Path(__file__).resolve().parents[1] / 'skills' / 'pr-babysit' / 'scripts' / 'harvest.py'
sys.path.insert(0, str(Path(__file__).resolve().parent))
from helpers import guard_survivors, run_gh_as  # noqa: E402
spec = importlib.util.spec_from_file_location('pr_babysit_harvest', SCRIPT)
harvest = importlib.util.module_from_spec(spec)
spec.loader.exec_module(harvest)

HEAD = 'a' * 40
OLD = 'b' * 40
EVENT = '2026-09-25T08:00:00Z'


def user(login):
    return {'login': login}


def review(login, commit, body='', at='2026-09-25T09:00:00Z', id_=1):
    return {'id': id_, 'user': user(login), 'commit_id': commit, 'body': body, 'submitted_at': at, 'html_url': f'u/{id_}'}


def comment(login, body, at='2026-09-25T09:00:00Z', updated=None, id_=2):
    return {'id': id_, 'user': user(login), 'body': body, 'created_at': at, 'updated_at': updated or at, 'html_url': f'u/{id_}'}


def status(state, desc, at='2026-09-25T09:00:00Z'):
    return {'context': 'CodeRabbit', 'state': state, 'description': desc, 'updated_at': at}


def greptile_run(status_, conclusion=None, summary='Greptile has reviewed the Pull Request.', at='2026-09-25T09:00:00Z'):
    return {'id': 9, 'name': 'Greptile Review', 'app': {'slug': 'greptile-apps'}, 'status': status_, 'conclusion': conclusion,
            'started_at': at, 'output': {'summary': summary}}


# Check run 118615785789 on hathach/agentrc PR 165, as GitHub reports it.
BELOW_TITLE = 'Base review: Confidence 4/5 \u2014 below your required 5/5'
BELOW_SUMMARY = ("Greptile reviewed this pull request successfully \u2014 this check reflects your team's confidence threshold, not a review "
                 'failure. The review scored 4/5, below the 5/5 this repository requires for the check to pass.')
CONCLUDED = '2026-10-20T12:06:54Z'


def below_run(title=BELOW_TITLE, summary=BELOW_SUMMARY, conclusion='failure', completed=CONCLUDED):
    return {**greptile_run('completed', conclusion, summary, at='2026-10-20T12:02:14Z'), 'id': 118615785789, 'completed_at': completed,
            'output': {'title': title, 'summary': summary, 'text': None}}


def minutes_after(at, n):
    return datetime.fromisoformat(at.replace('Z', '+00:00')) + timedelta(minutes=n)


def greptile_review(commit, at='2026-10-20T12:06:52Z', id_=50, body='<h3>Greptile Summary</h3>'):
    return {**review('greptile-apps[bot]', commit, body, at, id_), 'state': 'COMMENTED'}


def setUpModule():
    guard_survivors(__name__)


class SettleRules(unittest.TestCase):
    def test_coderabbit_status_and_check_run(self):
        cases = [
            ([status('pending', 'Review queued')], [], ('queued', None)),
            ([status('pending', 'Review in progress')], [], ('working', None)),
            ([status('pending', '???')], [], ('queued', None)),
            ([status('success', 'Review completed')], [], ('reviewed', None)),
            ([status('success', 'Review skipped')], [], ('settled', 'skipped')),
            ([status('failure', 'boom')], [], ('settled', 'failed')),
            ([status('weird', 'x')], [], ('unknown', None)),
            ([status('pending', 'Review queued', '2026-09-25T09:00:00Z'), status('success', 'Review completed', '2026-09-25T09:05:00Z')], [], ('reviewed', None)),
            ([], [{'name': 'CodeRabbit', 'status': 'in_progress', 'conclusion': None, 'started_at': '2026-09-25T09:00:00Z'}], ('working', None)),
            ([], [{'name': 'CodeRabbit', 'status': 'completed', 'conclusion': 'neutral', 'started_at': '2026-09-25T09:00:00Z'}], ('unknown', None)),
            ([], [], ('absent', None)),
        ]
        for statuses, checks, want in cases:
            r = harvest.coderabbit(HEAD, statuses, checks, [], EVENT)
            self.assertEqual((r['state'], r['kind']), want, (statuses, checks))

    def test_coderabbit_pause_and_rate_limit(self):
        bot = 'coderabbitai[bot]'
        paused = comment(bot, 'x ' + harvest.CR_PAUSED, at='2026-09-25T07:00:00Z', updated='2026-09-25T09:30:00Z')
        self.assertEqual(harvest.coderabbit(HEAD, [status('success', 'Review completed')], [], [paused], EVENT)['kind'], 'paused')
        resumed = status('success', 'Review completed', '2026-09-25T10:00:00Z')
        self.assertEqual(harvest.coderabbit(HEAD, [resumed], [], [paused], EVENT)['state'], 'reviewed', 'a review after the pause wins')
        rate = comment(bot, harvest.CR_RATE + '\n> ## Review limit reached', at='2026-09-25T09:30:00Z')
        self.assertEqual(harvest.coderabbit(HEAD, [], [], [rate], EVENT)['kind'], 'limited')
        self.assertEqual(harvest.coderabbit(HEAD, [], [], [rate], None)['state'], 'unknown', 'no head event time: cannot date it')
        edited = comment(bot, harvest.CR_RATE + '\n> ## Review limit reached', at='2026-09-25T07:00:00Z', updated='2026-09-25T09:30:00Z')
        self.assertEqual(harvest.coderabbit(HEAD, [], [], [edited], EVENT)['state'], 'unknown', 'an edited sticky proves nothing')
        before = comment(bot, harvest.CR_RATE + '\n> ## Review limit reached', at='2026-09-25T07:00:00Z')
        self.assertEqual(harvest.coderabbit(HEAD, [], [], [before], EVENT)['state'], 'absent', 'a notice before the head event is about an older push')

    def test_greptile(self):
        self.assertEqual(harvest.greptile(HEAD, [greptile_run('completed', 'success')], [], [], EVENT)['state'], 'reviewed')
        self.assertEqual(harvest.greptile(HEAD, [greptile_run('queued')], [], [], EVENT)['state'], 'queued')
        self.assertEqual(harvest.greptile(HEAD, [greptile_run('in_progress')], [], [], EVENT)['state'], 'working')
        err = greptile_run('completed', 'neutral', 'Greptile encountered an error while reviewing this PR. Try again')
        self.assertEqual(harvest.greptile(HEAD, [err], [], [], EVENT)['kind'], 'failed')
        self.assertEqual(harvest.greptile(HEAD, [greptile_run('completed', 'success', 'Something new')], [], [], EVENT)['state'], 'unknown')
        ish = greptile_run('completed', 'neutral', 'Greptile encountered an error while reviewing this PR-ish new format')
        self.assertEqual(harvest.greptile(HEAD, [ish], [], [], EVENT)['state'], 'unknown', 'only the whole known prefix reads')
        self.assertEqual(harvest.greptile(HEAD, [], [], [], EVENT)['state'], 'absent')
        bot = 'greptile-apps[bot]'
        big = comment(bot, harvest.GREPTILE_STATUS + '\nToo many files changed for review', at='2026-09-25T09:30:00Z')
        self.assertEqual(harvest.greptile(HEAD, [greptile_run('completed', 'success')], [big], [], EVENT)['kind'], 'skipped', 'a newer refusal beats the run')
        late_run = greptile_run('completed', 'success', at='2026-09-25T10:00:00Z')
        self.assertEqual(harvest.greptile(HEAD, [late_run], [big], [], EVENT)['state'], 'reviewed', 'a newer run beats the refusal')
        self.assertEqual(harvest.greptile(HEAD, [], [big], [], None)['state'], 'unknown')
        paused = review(bot, OLD, 'Greptile has paused reviews on this repository — it used its 50 free open-source review credits')
        self.assertEqual(harvest.greptile(HEAD, [], [], [paused], EVENT)['state'], 'absent', 'a review on an older commit settles nothing')
        paused['commit_id'] = HEAD
        self.assertEqual(harvest.greptile(HEAD, [], [], [paused], EVENT)['kind'], 'limited')

    def read_below(self, runs, reviews, minutes=3):
        return harvest.greptile(HEAD, runs, [], reviews, EVENT, minutes_after(CONCLUDED, minutes))

    def test_a_confidence_result_below_the_threshold_is_a_review_once_its_review_on_the_head_is_posted(self):
        got = self.read_below([below_run()], [greptile_review(HEAD)])
        self.assertEqual((got['state'], got['sha'], got['kind']), ('reviewed', HEAD, None))
        self.assertEqual(got['evidence'], ['check run 118615785789 completed/failure at 2026-10-20T12:02:14Z', f'review 50 on {HEAD[:8]}'])
        self.assertIn(BELOW_TITLE, got['reason'])

    def test_either_arrival_order_settles_reviewed(self):
        # The check first: its result waits for the review, which then settles it.
        self.assertEqual(self.read_below([below_run()], [])['state'], 'working')
        self.assertEqual(self.read_below([below_run()], [greptile_review(HEAD, at='2026-10-20T12:08:00Z')])['state'], 'reviewed')
        # The review first, two seconds before the check concluded (PR 179): the running check is what is read.
        early = [greptile_review(HEAD, at='2026-10-20T12:06:52Z')]
        self.assertEqual(self.read_below([greptile_run('in_progress', at='2026-10-20T12:02:14Z')], early)['state'], 'working')
        self.assertEqual(self.read_below([below_run()], early)['state'], 'reviewed')

    def test_a_result_with_no_review_on_the_head_waits_then_is_settled_failed(self):
        for reviews, why in (([], 'no review'), ([greptile_review(OLD)], 'a review of an older head'),
                             ([{**greptile_review(HEAD), 'state': 'PENDING'}], 'an unsubmitted review'),
                             ([greptile_review(HEAD, at='2026-10-20T12:00:00Z', body=harvest.GREPTILE_PAUSED + ' on this repository')], 'a paused notice the run superseded')):
            with self.subTest(why):
                waiting = self.read_below([below_run()], reviews, minutes=9)
                self.assertEqual((waiting['state'], waiting['sha']), ('working', HEAD))
                self.assertIn('its review on the head not posted yet', waiting['reason'])
                late = self.read_below([below_run()], reviews, minutes=10)
                self.assertEqual((late['state'], late['kind'], late['sha']), ('settled', 'failed', HEAD))
                self.assertTrue(late['reason'].startswith('review not observed: '), late['reason'])

    def test_a_genuinely_failed_run_is_settled_failed_and_an_ambiguous_one_unknown(self):
        for conclusion in ('failure', 'cancelled', 'timed_out', 'action_required'):
            got = self.read_below([below_run('Review failed', 'Greptile could not finish this review.', conclusion)], [greptile_review(HEAD)])
            self.assertEqual((got['state'], got['kind'], got['reason']), ('settled', 'failed', f'check run {conclusion}'), conclusion)
        for run, why in ((below_run(summary='Greptile reviewed this pull request.'), 'a threshold title with another summary'),
                         (below_run(title='Base review'), 'the threshold summary under another title'),
                         (below_run(conclusion='cancelled'), 'the threshold result not concluding failure'),
                         (below_run(completed=None), 'no completion time to wait from')):
            with self.subTest(why):
                self.assertEqual(self.read_below([run], [])['state'], 'unknown')

    def test_the_wait_for_a_scored_review_is_the_workflows_review_cap(self):
        workflow_src = (SCRIPT.parents[3] / 'workflows' / 'pr-babysit.js').read_text(encoding='utf-8')
        self.assertIn(f'const REVIEW_CAP_MIN = {int(harvest.GREPTILE_REVIEW_WAIT.total_seconds() // 60)}\n', workflow_src)

    def test_digest_and_aliases_are_pr_replys_comment_digest(self):
        rabbit = 'x\n\n<!-- This is an auto-generated reply by CodeRabbit -->\n\n✅ Addressed in commit a3f8bc8'
        for body, login in ((None, 'a'), ('', 'a'), ('fix this', 'a'), ('📝 nit\r\nline', 'a'), (rabbit, 'coderabbitai[bot]')):
            got = harvest.entry('review', {'id': 1, 'user': {'login': login}, 'html_url': 'u'}, 'coderabbit', body, 't', 't')
            self.assertEqual([got['digest'], *got.get('aliases', [])], harvest.comment_digest.digests(body, login), body)
            if login == 'a':
                self.assertEqual(got['digest'], hashlib.sha256((body or '').encode()).hexdigest()[:12])
                self.assertNotIn('aliases', got)

    def test_only_coderabbits_walkthrough_issue_comment_loses_its_body(self):
        rabbit = 'coderabbitai[bot]'
        walk = harvest.CR_WALKTHROUGH + '\n<!-- walkthrough_start -->\n## Walkthrough\nsequence diagrams\n<!-- walkthrough_end -->'
        got = harvest.entry('issue', {'id': 5, 'user': {'login': rabbit}, 'html_url': 'u/5'}, 'coderabbit', walk, 't', 't')
        self.assertEqual((got['body'], got['bodyOmitted'], got['bodyLength']), (None, 'coderabbit-walkthrough', len(walk)))
        self.assertEqual([got['digest'], *got.get('aliases', [])], harvest.comment_digest.digests(walk, rabbit), 'the real body\'s digest')
        self.assertEqual((got['commentId'], got['url'], got['kind'], got['source']), (5, 'u/5', 'issue', 'coderabbit'))
        prompt = '\n<details><summary>🤖 Prompt for AI Agents</summary>\nIn @a.c around line 3, fix it.\n</details>'
        kept = [('review-body', rabbit, 'coderabbit', harvest.CR_WALKTHROUGH + '\n<summary>🧹 Nitpick comments (1)</summary>' + prompt),
                ('review', rabbit, 'coderabbit', harvest.CR_WALKTHROUGH + '\n_⚠️ Potential issue_' + prompt),
                ('issue', rabbit, 'coderabbit', harvest.CR_RATE + '\n> ## Review limit reached'),
                ('issue', 'greptile-apps[bot]', 'greptile', harvest.CR_WALKTHROUGH + '\n<!-- greptile_summary -->')]
        for kind, login, src, body in kept:
            got = harvest.entry(kind, {'id': 6, 'user': {'login': login}, 'html_url': 'u/6'}, src, body, 't', 't')
            self.assertEqual(got['body'], body, (kind, src))
            self.assertNotIn('bodyOmitted', got)


class FailClosed(unittest.TestCase):
    """What a first review found failing open: each case must end unknown or with the older, provable state."""

    def test_an_undated_check_run_cannot_be_ordered(self):
        queued = greptile_run('queued', at=None)
        queued['id'] = 10
        self.assertEqual(harvest.greptile(HEAD, [greptile_run('completed', 'success'), queued], [], [], EVENT)['state'], 'unknown')
        self.assertEqual(harvest.greptile(HEAD, [queued], [], [], EVENT)['state'], 'queued', 'a lone queued run needs no ordering')
        cr = {'id': 11, 'name': 'CodeRabbit', 'status': 'queued', 'conclusion': None, 'started_at': None}
        self.assertEqual(harvest.coderabbit(HEAD, [], [cr], [], EVENT)['state'], 'queued')

    def test_coderabbit_reads_the_newer_of_its_status_and_check_run(self):
        queued = status('pending', 'Review queued', '2026-09-25T09:00:00Z')
        done = {'id': 11, 'name': 'CodeRabbit', 'status': 'completed', 'conclusion': 'success',
                'started_at': '2026-09-25T09:50:00Z', 'completed_at': '2026-09-25T10:00:00Z'}
        paused = comment('coderabbitai[bot]', harvest.CR_PAUSED, at='2026-09-25T09:30:00Z')
        self.assertEqual(harvest.coderabbit(HEAD, [queued], [done], [paused], EVENT)['state'], 'reviewed', 'a newer run beats the pause and the older status')
        self.assertEqual(harvest.coderabbit(HEAD, [status('success', 'Review completed', '2026-09-25T10:30:00Z')], [done], [], EVENT)['state'], 'reviewed')
        undated = {**done, 'started_at': None, 'completed_at': None, 'status': 'queued'}
        self.assertEqual(harvest.coderabbit(HEAD, [queued], [undated], [], EVENT)['state'], 'unknown', 'a status and an undated run')

    def test_disagreeing_artifacts_at_one_timestamp_are_unknown(self):
        at = '2026-09-25T09:00:00Z'
        run_ = {'id': 11, 'name': 'CodeRabbit', 'status': 'in_progress', 'conclusion': None, 'started_at': at}
        self.assertEqual(harvest.coderabbit(HEAD, [status('success', 'Review completed', at)], [run_], [], EVENT)['state'], 'unknown')
        done = {**run_, 'status': 'completed', 'conclusion': 'success', 'completed_at': at}
        self.assertEqual(harvest.coderabbit(HEAD, [status('success', 'Review completed', at)], [done], [], EVENT)['state'], 'reviewed', 'agreeing artifacts need no order')
        big = comment('greptile-apps[bot]', harvest.GREPTILE_STATUS + '\nToo many files changed for review', at=at)
        self.assertEqual(harvest.greptile(HEAD, [greptile_run('completed', 'success', at=at)], [big], [], EVENT)['state'], 'unknown')
        for pair in ([status('pending', 'Review queued', at), status('success', 'Review completed', at)],
                     [status('success', 'Review completed', at), status('pending', 'Review queued', at)]):
            self.assertEqual(harvest.coderabbit(HEAD, pair, [], [], EVENT)['state'], 'unknown', 'statuses tied in either order')
        working = {**greptile_run('in_progress', at=at), 'id': 10}
        for runs in ([greptile_run('completed', 'success', at=at), working], [working, greptile_run('completed', 'success', at=at)]):
            self.assertEqual(harvest.greptile(HEAD, runs, [], [], EVENT)['state'], 'unknown', 'runs tied in either order')
        self.assertEqual(harvest.greptile(HEAD, [greptile_run('completed', 'success', at=at), {**greptile_run('completed', 'success', at=at), 'id': 10}], [], [], EVENT)['state'],
                         'reviewed', 'runs that read the same need no order')
        failed = comment('greptile-apps[bot]', harvest.GREPTILE_STATUS + '\n' + harvest.GREPTILE_ERROR, at='2026-09-25T09:30:00Z', id_=3)
        big_late = {**big, 'created_at': '2026-09-25T09:30:00Z', 'updated_at': '2026-09-25T09:30:00Z'}
        for issue in ([failed, big_late], [big_late, failed]):
            self.assertEqual(harvest.greptile(HEAD, [], issue, [], EVENT)['state'], 'unknown', 'refusals tied in either order')
        undated = {**status('success', 'Review completed'), 'updated_at': None}
        self.assertEqual(harvest.coderabbit(HEAD, [undated], [], [], EVENT)['state'], 'unknown', 'a lone undated status')
        self.assertEqual(harvest.coderabbit(HEAD, [undated, status('success', 'Review completed')], [], [], EVENT)['state'], 'unknown')
        bot = 'coderabbitai[bot]'
        chatter = comment(bot, 'No rate limit was reached; see coderabbit.ai docs', at='2026-09-25T09:30:00Z', id_=8)
        self.assertEqual(harvest.coderabbit(HEAD, [], [], [chatter], EVENT)['state'], 'absent', 'only the marked notice is a refusal')
        old_pause = comment(bot, harvest.CR_PAUSED, at='2026-09-25T08:30:00Z', id_=9)
        new_pause = comment(bot, harvest.CR_PAUSED, at='2026-09-25T10:30:00Z', id_=10)
        done = status('success', 'Review completed', '2026-09-25T10:00:00Z')
        for issue in ([old_pause, new_pause], [new_pause, old_pause]):
            self.assertEqual(harvest.coderabbit(HEAD, [done], [], issue, EVENT)['kind'], 'paused', 'the newest pause decides, in either order')
        mixed = greptile_run('completed', 'success', 'Greptile encountered an error while reviewing this PR. Try again')
        self.assertEqual(harvest.greptile(HEAD, [mixed], [], [], EVENT)['state'], 'unknown', 'an error summary under success is conflicting')

    def test_an_edited_rate_notice_is_unknown_in_any_order(self):
        bot = 'coderabbitai[bot]'
        plain = comment(bot, harvest.CR_RATE + '\n> ## Review limit reached', at='2026-09-25T09:30:00Z', id_=3)
        edited = comment(bot, harvest.CR_RATE + '\n> ## Review limit reached', at='2026-09-25T09:40:00Z', updated='2026-09-25T09:45:00Z', id_=4)
        for issue in ([plain, edited], [edited, plain]):
            self.assertEqual(harvest.coderabbit(HEAD, [], [], issue, EVENT)['state'], 'unknown', issue)
        stale = comment(bot, harvest.CR_RATE + '\n> ## Review limit reached', at='2026-09-25T06:00:00Z', updated='2026-09-25T07:00:00Z', id_=5)
        self.assertEqual(harvest.coderabbit(HEAD, [], [], [stale], EVENT)['state'], 'absent', 'edited before the head event: an older push')
        superseded = comment(bot, harvest.CR_RATE + '\n> ## Review limit reached', at='2026-09-25T08:30:00Z', updated='2026-09-25T09:00:00Z', id_=6)
        done = status('success', 'Review completed', '2026-09-25T10:00:00Z')
        self.assertEqual(harvest.coderabbit(HEAD, [done], [], [superseded], EVENT)['state'], 'reviewed', 'older than a status on the head')
        at_once = status('success', 'Review completed', '2026-09-25T09:00:00Z')
        tie = comment(bot, harvest.CR_RATE + '\n> ## Review limit reached', at='2026-09-25T09:00:00Z', id_=7)
        self.assertEqual(harvest.coderabbit(HEAD, [at_once], [], [tie], EVENT)['state'], 'unknown', 'one timestamp orders nothing')
        self.assertEqual(harvest.coderabbit(HEAD, [at_once], [], [comment(bot, harvest.CR_PAUSED, at='2026-09-25T09:00:00Z')], EVENT)['state'], 'unknown')

    def test_a_rate_limit_status_dates_the_notice_it_edits(self):
        """#82: CodeRabbit edited its walkthrough into a rate-limit notice a second after its "Review rate limited" status."""
        bot = 'coderabbitai[bot]'
        body = harvest.CR_WALKTHROUGH + '\n' + harvest.CR_RATE + '\n> ## Review limit reached\n> **Next included review available in 51 minutes.**'
        notice = comment(bot, body, at='2026-09-25T08:54:33Z', updated='2026-09-25T09:04:19Z', id_=3)
        limited = status('success', 'Review rate limited', '2026-09-25T09:04:18Z')
        r = harvest.coderabbit(HEAD, [limited], [], [notice], EVENT)
        self.assertEqual((r['state'], r['kind'], r['sha'], r['retryAt']), ('settled', 'limited', HEAD, '2026-09-25T09:55:19Z'))
        self.assertIn('comment 3', r['evidence'])
        tie = comment(bot, body, at='2026-09-25T08:54:33Z', updated='2026-09-25T09:04:18Z', id_=3)
        self.assertEqual(harvest.coderabbit(HEAD, [limited], [], [tie], EVENT)['retryAt'], '2026-09-25T09:55:18Z', 'a notice at the status\'s second agrees with it')
        alone = harvest.coderabbit(HEAD, [limited], [], [], EVENT)
        self.assertEqual((alone['kind'], alone['retryAt']), ('limited', None), 'the status alone: limited, no reset time')
        self.assertIn('no reset time read', alone['reason'])
        older = comment(bot, body, at='2026-09-25T08:00:00Z', updated='2026-09-25T08:10:00Z', id_=4)
        self.assertEqual(harvest.coderabbit(HEAD, [limited], [], [older], EVENT)['retryAt'], '2026-09-25T09:01:00Z',
                         'a notice after the head event with nothing between it and the status is its notice (#138)')
        self.assertEqual(harvest.coderabbit(HEAD, [], [], [notice], EVENT)['state'], 'unknown', 'without the status the edit still cannot be dated')
        later = status('success', 'Review completed', '2026-09-25T10:00:00Z')
        self.assertEqual(harvest.coderabbit(HEAD, [limited, later], [], [notice], EVENT)['state'], 'reviewed', 'a newer review supersedes the limit')
        run_ = {'id': 11, 'name': 'CodeRabbit', 'status': 'in_progress', 'conclusion': None, 'started_at': '2026-09-25T09:30:00Z'}
        self.assertEqual(harvest.coderabbit(HEAD, [limited], [run_], [notice], EVENT)['state'], 'working', 'a newer run supersedes it too')
        pause = comment(bot, harvest.CR_PAUSED, at='2026-09-25T09:10:00Z', id_=5)
        self.assertEqual(harvest.coderabbit(HEAD, [limited], [], [notice, pause], EVENT)['kind'], 'paused')
        soon = comment(bot, body.replace('51 minutes', '1 minute'), at='2026-09-25T08:54:33Z', updated='2026-09-25T09:04:19Z', id_=6)
        for issue in ([notice, soon], [soon, notice]):
            r = harvest.coderabbit(HEAD, [limited], [], issue, EVENT)
            self.assertEqual((r['kind'], r['retryAt']), ('limited', None), 'tied notices with different countdowns give no reset time')
            self.assertIn('read differently', r['reason'])
        posted = [comment(bot, harvest.CR_RATE + f'\n> **Next included review available in {n}.**', at='2026-09-25T09:30:00Z', id_=i)
                  for i, n in ((7, '1 minute'), (8, '51 minutes'))]
        for issue in (posted, posted[::-1]):
            r = harvest.coderabbit(HEAD, [], [], issue, EVENT)
            self.assertEqual((r['kind'], r['retryAt']), ('limited', None), 'so do tied unedited ones')

    def test_a_rate_limit_status_pairs_its_notice_either_side_unless_something_came_between(self):
        """#138: the notice was edited a second before its "Review rate limited" status, and no reset was read."""
        bot = 'coderabbitai[bot]'
        body = harvest.CR_WALKTHROUGH + '\n' + harvest.CR_RATE + '\n> ## Review limit reached\n> **Next included review available in 40 minutes.**'
        limited = status('success', 'Review rate limited', '2026-09-25T12:08:35Z')
        notice = lambda updated, text=body: comment(bot, text, at='2026-09-25T07:00:00Z', updated=updated, id_=3)
        for updated, reset in (('2026-09-25T12:08:30Z', '2026-09-25T12:48:30Z'), ('2026-09-25T12:08:34Z', '2026-09-25T12:48:34Z'),
                               ('2026-09-25T12:08:35Z', '2026-09-25T12:48:35Z'), ('2026-09-25T12:08:36Z', '2026-09-25T12:48:36Z')):
            with self.subTest(updated):
                r = harvest.coderabbit(HEAD, [limited], [], [notice(updated)], EVENT)
                self.assertEqual((r['kind'], r['retryAt']), ('limited', reset))
        self.assertIsNone(harvest.coderabbit(HEAD, [limited], [], [notice('2026-09-25T07:59:59Z')], EVENT)['retryAt'], 'about an older push')
        run_ = {'id': 11, 'name': 'CodeRabbit', 'status': 'completed', 'conclusion': 'success',
                'started_at': '2026-09-25T12:08:31Z', 'completed_at': '2026-09-25T12:08:32Z'}
        self.assertIsNone(harvest.coderabbit(HEAD, [limited], [run_], [notice('2026-09-25T12:08:30Z')], EVENT)['retryAt'], 'a run came between')
        working = status('pending', 'Review in progress', '2026-09-25T12:08:32Z')
        self.assertIsNone(harvest.coderabbit(HEAD, [working, limited], [], [notice('2026-09-25T12:08:30Z')], EVENT)['retryAt'],
                          'another status came between')
        self.assertEqual(harvest.coderabbit(HEAD, [working, limited], [], [notice('2026-09-25T12:08:34Z')], EVENT)['retryAt'], '2026-09-25T12:48:34Z')
        self.assertIsNone(harvest.coderabbit(HEAD, [limited], [], [notice('2026-09-25T12:08:30Z')], None)['retryAt'],
                          'nothing dates the head: only a notice at or after the status is its')
        bare = notice('2026-09-25T12:08:34Z', harvest.CR_WALKTHROUGH + '\n' + harvest.CR_RATE + '\n> ## Review limit reached')
        first, again = (harvest.coderabbit(HEAD, [limited], [], [bare], EVENT) for _ in range(2))
        self.assertEqual((first['retryAt'], again['retryAt']), ('2026-09-25T13:08:34Z', '2026-09-25T13:08:34Z'),
                         'no countdown: an hour after the notice, the same on every harvest')
        self.assertIn('reset estimated an hour after the notice', first['reason'])
        posted = comment(bot, harvest.CR_RATE + '\n> ## Review limit reached', at='2026-09-25T09:30:00Z')
        self.assertEqual(harvest.coderabbit(HEAD, [], [], [posted], EVENT)['retryAt'], '2026-09-25T10:30:00Z', 'an unedited one alike')
        unread = harvest.coderabbit(HEAD, [limited], [], [notice('2026-09-25T12:08:34Z', body.replace('40 minutes', '3 days'))], EVENT)
        self.assertEqual((unread['kind'], unread['retryAt']), ('limited', None), 'a countdown that does not read is not estimated')
        self.assertIn('its countdown did not read', unread['reason'])

    def test_a_rate_limit_countdown_reads_or_is_none(self):
        def at(countdown, updated='2026-09-25T09:00:00Z'):
            return harvest.retry_at({'body': f'> **Next included review available in {countdown}.**', 'updated_at': updated})
        self.assertEqual(at('51 minutes'), '2026-09-25T09:51:00Z')
        self.assertEqual(at('1 minute'), '2026-09-25T09:01:00Z')
        self.assertEqual(at('1 hour and 3 minutes'), '2026-09-25T10:03:00Z')
        self.assertEqual(at('2 hours, 5 minutes and 30 seconds'), '2026-09-25T11:05:30Z')
        self.assertEqual(at('0 minutes'), '2026-09-25T09:00:00Z')
        for bad in ('a few minutes', '51 minutes or so', '5 minutes and 3 minutes', '3 days', ''):
            self.assertIsNone(at(bad), bad)
        self.assertIsNone(harvest.retry_at({'body': 'Review limit reached', 'updated_at': '2026-09-25T09:00:00Z'}))
        self.assertIsNone(harvest.retry_at(None))
        plain = comment('coderabbitai[bot]', harvest.CR_RATE + '\n> **Next included review available in 10 minutes.**', at='2026-09-25T09:30:00Z')
        r = harvest.coderabbit(HEAD, [], [], [plain], EVENT)
        self.assertEqual((r['kind'], r['retryAt']), ('limited', '2026-09-25T09:40:00Z'), 'an unedited notice counts from its posting')

    def test_copilot_reads_its_newest_submitted_review_on_the_head(self):
        cop = 'copilot-pull-request-reviewer[bot]'
        def v(body, commit=HEAD, at='2026-09-25T09:00:00Z', state='COMMENTED', id_=1):
            return review(cop, commit, body, at, id_) | {'state': state}
        overview = '<!-- ccr-overview-v2 -->\n### Approval recommended'
        excluded = harvest.COPILOT_NO_FILES + ' Check if the **Files changed** are included in default exclusions.'
        cases = [
            ([v(harvest.COPILOT_QUOTA)], ('settled', 'limited', HEAD)),
            ([v(excluded)], ('settled', 'skipped', HEAD)),
            ([v(overview)], ('reviewed', None, HEAD)),
            ([v('## Pull request overview\nx')], ('reviewed', None, HEAD)),
            ([v('Copilot was unable to review this pull request because it is on fire.')], ('unknown', None, None)),
            ([v('Something new')], ('unknown', None, None)),
            ([v(overview, commit=OLD)], ('absent', None, None)),
            ([v(overview, state='PENDING')], ('absent', None, None)),
            ([v(overview, state='DISMISSED')], ('absent', None, None)),
            ([review('coderabbitai[bot]', HEAD, overview)], ('absent', None, None)),
            ([], ('absent', None, None)),
            ([v(overview, at='2026-09-25T09:00:00Z', id_=1), v(harvest.COPILOT_QUOTA, at='2026-09-25T10:00:00Z', id_=2)], ('settled', 'limited', HEAD)),
            ([v(overview, id_=1), v(harvest.COPILOT_QUOTA, id_=2)], ('unknown', None, None)),
            ([v(overview, id_=1), v(harvest.COPILOT_QUOTA, at=None, id_=2)], ('unknown', None, None)),
        ]
        for reviews, want in cases:
            r = harvest.copilot(HEAD, reviews)
            self.assertEqual((r['state'], r['kind'], r['sha']), want, reviews)

    def test_unfamiliar_artifacts_are_unknown(self):
        self.assertEqual(harvest.coderabbit(HEAD, [status('success', 'Something new')], [], [], EVENT)['state'], 'unknown')
        undated = greptile_run('completed', 'success', at=None)
        refusal = comment('greptile-apps[bot]', harvest.GREPTILE_STATUS + '\nToo many files changed for review', at='2026-09-25T09:30:00Z')
        self.assertEqual(harvest.greptile(HEAD, [undated], [refusal], [], EVENT)['state'], 'unknown')

    def test_a_timeline_event_about_another_commit_dates_nothing(self):
        timeline = [{'event': 'reopened', 'created_at': '2026-09-25T09:00:00Z', 'commit_id': OLD}]
        view = {'headRefName': 'fix', 'headRepository': {'name': 'r', 'nameWithOwner': 'o/r'}, 'headRepositoryOwner': {'login': 'o'}}
        with mock.patch.object(harvest, 'gh_json', return_value=[{'workflow_runs': []}]), mock.patch.object(harvest, 'pages', return_value=timeline):
            self.assertEqual(harvest.head_event('o/r', 7, view, HEAD)[0], None)
            timeline[0]['commit_id'] = None
            self.assertEqual(harvest.head_event('o/r', 7, view, HEAD)[0], '2026-09-25T09:00:00Z', 'GitHub leaves a reopen\'s commit_id null')


class FakeGh:
    """A gh answering from a {joined argv: [answers...]} map; each call pops the first answer that remains."""

    def __init__(self, root, answers):
        self.file = root / 'answers.json'
        self.file.write_text(json.dumps(answers))
        self.stub = root / 'gh.py'
        self.stub.write_text(f'''import json, sys
f = {str(self.file)!r}
answers = json.load(open(f))
key = ' '.join(sys.argv[1:])
got = answers.get(key)
if not got:
    sys.stderr.write('no answer for ' + key); sys.exit(1)
out = got.pop(0) if len(got) > 1 else got[0]
json.dump(answers, open(f, 'w'))
if isinstance(out, dict) and 'stderr' in out and len(out) == 1:
    sys.stderr.write(out['stderr']); sys.exit(1)
print(json.dumps(out))
''')


def workflow(on):
    return {'content': base64.b64encode(on.encode()).decode()}


class WithoutPyYaml(unittest.TestCase):
    def test_only_reading_a_workflow_needs_pyyaml_and_without_it_none_is_read(self):
        """sonar.py and coderabbit_trigger.py import this file for its gh helpers, under a python3 that may lack PyYAML."""
        with mock.patch.dict(sys.modules, {'yaml': None}):
            bare = importlib.util.module_from_spec(spec)
            spec.loader.exec_module(bare)
            with mock.patch.object(bare, 'gh_json', side_effect=AssertionError('a workflow was fetched')):
                self.assertIsNone(bare.pr_types('o/r', '.github/workflows/build.yml', HEAD))


class EndToEnd(unittest.TestCase):
    def setUp(self):
        tmp = tempfile.TemporaryDirectory()
        self.addCleanup(tmp.cleanup)
        self.root = Path(tmp.name)
        harvest.pr_types.cache_clear()

    def answers(self, heads):
        R, N = 'o/r', 7
        view = lambda h: {'headRefOid': h, 'headRefName': 'fix', 'headRepository': {'name': 'r', 'nameWithOwner': 'o/r'}}
        run = lambda id_, event, path, at, h=HEAD: {'id': id_, 'event': event, 'path': path, 'created_at': at, 'head_sha': h,
                                                    'head_branch': 'fix', 'head_repository': {'full_name': 'o/r'}}
        a = {'repo view --json nameWithOwner': [{'nameWithOwner': R}],
             f'pr view {N} --json headRefOid,headRefName,headRepository': [view(h) for h in heads],
             f'pr view {N} --json headRefOid': [{'headRefOid': h} for h in heads[1:] + heads[-1:]]}
        for h in set(heads):
            a[f'api --paginate --slurp repos/{R}/actions/runs?head_sha={h}&per_page=100'] = [[{'workflow_runs': [
                run(1, 'pull_request', '.github/workflows/label.yml', '2026-09-25T09:00:00Z', h),
                run(2, 'pull_request', '.github/workflows/build.yml', '2026-09-25T08:00:00Z', h),
                run(3, 'pull_request', '.github/workflows/other-branch.yml', '2026-09-25T09:30:00Z', h) | {'head_branch': 'main'}]}]]
            a[f'api repos/{R}/contents/.github/workflows/label.yml?ref={h}'] = [workflow('on:\n  pull_request:\n    types: [labeled]\n')]
            a[f'api repos/{R}/contents/.github/workflows/build.yml?ref={h}'] = [workflow('on: [push, pull_request]\n')]
            a[f'api --paginate --slurp repos/{R}/commits/{h}/statuses?per_page=100'] = [[[status('success', 'Review completed')]]]
            a[f'api --paginate --slurp repos/{R}/commits/{h}/check-runs?per_page=100'] = [[{'check_runs': []}]]
        a[f'api --paginate --slurp repos/{R}/issues/{N}/timeline?per_page=100'] = [[[]]]
        a[f'api --paginate --slurp repos/{R}/issues/{N}/comments?per_page=100'] = [[[comment('coderabbitai[bot]', 'walkthrough', id_=20), comment('hathach', 'mine', id_=21)]]]
        a[f'api --paginate --slurp repos/{R}/pulls/{N}/reviews?per_page=100'] = [[[review('coderabbitai[bot]', HEAD, '**Actionable comments posted: 1**', id_=30),
                                                                     review('coderabbitai[bot]', HEAD, '', id_=31)]]]
        a[f'api --paginate --slurp repos/{R}/pulls/{N}/comments?per_page=100'] = [[[{'id': 40, 'user': user('coderabbitai[bot]'), 'body': 'fix this', 'path': 'a.c',
                                                                       'line': 3, 'commit_id': HEAD, 'created_at': EVENT, 'updated_at': EVENT,
                                                                       'in_reply_to_id': None, 'html_url': 'u/40'}]]]
        return a

    def harvest(self, answers, *argv):
        gh = FakeGh(self.root, answers)
        out = io.StringIO()
        with run_gh_as(gh.stub), redirect_stdout(out):
            code = harvest.report(harvest.collect, list(argv) or ['--pr', '7', '--reviewers', 'coderabbit,copilot', '--auto-run', 'coderabbit'], limits=True)
        return code, json.loads(out.getvalue().splitlines()[-1])

    def test_one_read(self):
        code, got = self.harvest(self.answers([HEAD]))
        self.assertEqual(code, 0, got)
        self.assertEqual((got['headSha'], got['headEventAt']), (HEAD, '2026-09-25T08:00:00Z'), 'the labeled run and the other branch date nothing')
        self.assertIn('run 2', got['headEventEvidence'])
        self.assertEqual([(b['bot'], b['state']) for b in got['bots']], [('coderabbit', 'reviewed'), ('copilot', 'absent')], 'one record per reviewer')
        self.assertEqual([(c['kind'], c['commentId']) for c in got['comments']], [('review', 40), ('issue', 20), ('review-body', 30)],
                         'only the named reviewers, and no empty review body')
        self.assertEqual(got['comments'][0]['digest'], harvest.digest('fix this'))
        self.assertEqual(got['comments'][0]['body'], 'fix this')

    def test_without_pyyaml_no_pull_request_run_dates_the_head_and_the_evidence_says_why(self):
        answers = self.answers([HEAD])
        answers[f'api --paginate --slurp repos/o/r/commits/{HEAD}/statuses?per_page=100'] = [[[]]]
        answers['api --paginate --slurp repos/o/r/issues/7/comments?per_page=100'][0][0].append(
            comment('coderabbitai[bot]', harvest.CR_RATE + '\n> ## Review limit reached', at='2026-09-25T09:30:00Z', id_=22))
        code, got = self.harvest(answers)
        self.assertEqual((code, got['headEventAt'], got['bots'][0]['state']), (0, '2026-09-25T08:00:00Z', 'settled'), 'with PyYAML')
        harvest.pr_types.cache_clear()
        with mock.patch.object(harvest, 'yaml', None):
            code, got = self.harvest(answers)
        self.assertEqual((code, got['headEventAt'], got['bots'][0]['state']), (0, None, 'unknown'), 'the notice is not settled on a guess')
        self.assertTrue(got['headEventEvidence'].endswith('; PyYAML is not installed, so no pull_request run was read'), got['headEventEvidence'])

    def test_the_walkthrough_is_harvested_without_its_body(self):
        answers = self.answers([HEAD])
        walk = harvest.CR_WALKTHROUGH + '\n## Walkthrough'
        answers['api --paginate --slurp repos/o/r/issues/7/comments?per_page=100'][0][0].append(comment('coderabbitai[bot]', walk, id_=22))
        code, got = self.harvest(answers)
        self.assertEqual(code, 0, got)
        by_id = {c['commentId']: c for c in got['comments']}
        self.assertEqual((by_id[22]['body'], by_id[22]['bodyOmitted'], by_id[22]['digest']), (None, 'coderabbit-walkthrough', harvest.digest(walk)))
        self.assertEqual(by_id[20]['body'], 'walkthrough', 'a CodeRabbit issue comment without the marker stays verbatim')

    def test_greptiles_confidence_result_is_read_with_its_review_on_the_head(self):
        argv = ['--pr', '7', '--reviewers', 'coderabbit,greptile', '--auto-run', 'coderabbit,greptile']
        for reviews, state in (([greptile_review(HEAD)], 'reviewed'), ([], 'settled')):
            with self.subTest(state):
                answers = self.answers([HEAD])
                answers[f'api --paginate --slurp repos/o/r/commits/{HEAD}/check-runs?per_page=100'] = [[{'check_runs': [below_run()]}]]
                answers['api --paginate --slurp repos/o/r/pulls/7/reviews?per_page=100'][0][0].extend(reviews)
                code, got = self.harvest(answers, *argv)
                self.assertEqual(code, 0, got)
                self.assertEqual([(b['bot'], b['state']) for b in got['bots']], [('coderabbit', 'reviewed'), ('greptile', state)],
                                 'the result concluded long before this read: without a review it is not observed')

    def test_copilot_is_harvested_and_read_from_its_review(self):
        answers = self.answers([HEAD])
        answers['api --paginate --slurp repos/o/r/pulls/7/reviews?per_page=100'][0][0].append(
            review('copilot-pull-request-reviewer[bot]', HEAD, '## Pull request overview', id_=32) | {'state': 'COMMENTED'})
        answers['api --paginate --slurp repos/o/r/pulls/7/comments?per_page=100'][0][0].append(
            {'id': 41, 'user': user('Copilot'), 'body': 'nit', 'path': 'a.c', 'line': 5, 'commit_id': HEAD, 'created_at': EVENT, 'updated_at': EVENT,
             'in_reply_to_id': None, 'html_url': 'u/41'})
        code, got = self.harvest(answers)
        self.assertEqual(code, 0, got)
        self.assertEqual([(c['kind'], c['commentId']) for c in got['comments'] if c['source'] == 'copilot'], [('review', 41), ('review-body', 32)])
        self.assertEqual([(b['bot'], b['state'], b['evidence']) for b in got['bots']],
                         [('coderabbit', 'reviewed', got['bots'][0]['evidence']), ('copilot', 'reviewed', [f'review 32 on {HEAD[:8]}'])])

    def test_a_copilot_refusal_has_no_body_and_reads_settled(self):
        answers = self.answers([HEAD])
        answers['api --paginate --slurp repos/o/r/pulls/7/reviews?per_page=100'][0][0].append(
            review('copilot-pull-request-reviewer[bot]', HEAD, harvest.COPILOT_QUOTA, id_=33) | {'state': 'COMMENTED'})
        code, got = self.harvest(answers)
        self.assertEqual(code, 0, got)
        mine = [c for c in got['comments'] if c['source'] == 'copilot']
        self.assertEqual([(c['commentId'], c['body'], c['bodyOmitted'], c['digest']) for c in mine],
                         [(33, None, 'copilot-refusal', harvest.digest(harvest.COPILOT_QUOTA))])
        self.assertEqual([(b['state'], b['kind'], b['evidence']) for b in got['bots'] if b['bot'] == 'copilot'],
                         [('settled', 'limited', [f'review 33 on {HEAD[:8]}'])])

    def test_each_comment_endpoint_is_read_once(self):
        answers = self.answers([HEAD])
        quota = review('copilot-pull-request-reviewer[bot]', HEAD, harvest.COPILOT_QUOTA, id_=33) | {'state': 'COMMENTED'}
        # A second read would get the second page set, with no Copilot review in it.
        answers['api --paginate --slurp repos/o/r/pulls/7/reviews?per_page=100'] = [[[quota]], [[]]]
        answers['api --paginate --slurp repos/o/r/issues/7/comments?per_page=100'].append([[]])
        code, got = self.harvest(answers)
        self.assertEqual(code, 0, got)
        self.assertEqual([b['kind'] for b in got['bots'] if b['bot'] == 'copilot'], ['limited'])
        self.assertEqual(sorted(c['commentId'] for c in got['comments']), [20, 33, 40])

    def test_a_moving_head_is_read_again_then_refused(self):
        code, got = self.harvest(self.answers([OLD, HEAD]))
        self.assertEqual((code, got['headSha']), (0, HEAD))
        code, got = self.harvest(self.answers([OLD, HEAD, 'c' * 40]))
        self.assertEqual(code, 2)
        self.assertIn('head moved', got['error'])

    def test_a_rate_limit_is_reported_with_its_reset(self):
        limited = {'stderr': 'HTTP 403: API rate limit exceeded for user ID 249515.'}
        for name, resources, want in (
                ('primary', {'core': {'remaining': 0, 'reset': 1790000000}, 'graphql': {'remaining': 0, 'reset': 1790000060},
                             'search': {'remaining': 30, 'reset': 1790009999}}, ('2026-09-21T14:14:20Z', False)),
                ('secondary', {'core': {'remaining': 5000, 'reset': 1790000000}}, (None, True))):
            with self.subTest(name):
                answers = self.answers([HEAD])
                answers['pr view 7 --json headRefOid,headRefName,headRepository'] = [limited]
                answers['api rate_limit'] = [{'resources': resources}]
                code, got = self.harvest(answers)
                self.assertEqual(code, 2)
                self.assertIn('API rate limit exceeded', got['error'])
                self.assertEqual((got['rateLimit']['resetAt'], got['rateLimit']['secondary']), want)
                self.assertRegex(got['rateLimit']['observedAt'], r'^\d{4}-\d\d-\d\dT\d\d:\d\d:\d\dZ$')
        answers = self.answers([HEAD])
        answers['pr view 7 --json headRefOid,headRefName,headRepository'] = [{'stderr': 'HTTP 404: Not Found'}]
        code, got = self.harvest(answers)
        self.assertEqual((code, 'rateLimit' in got), (2, False), 'another failure keeps its plain error line')

    def test_a_rate_limit_on_a_workflow_read_is_reported(self):
        """#90: an unreadable workflow dates nothing, but a rate limit on its read stops the harvest."""
        answers = self.answers([HEAD])
        answers[f'api repos/o/r/contents/.github/workflows/label.yml?ref={HEAD}'] = [{'stderr': 'HTTP 403: You have exceeded a secondary rate limit.'}]
        answers['api rate_limit'] = [{'resources': {'core': {'remaining': 5000}}}]
        for later in ('statuses', 'check-runs'):
            del answers[f'api --paginate --slurp repos/o/r/commits/{HEAD}/{later}?per_page=100']
        code, got = self.harvest(answers)
        self.assertEqual(code, 2, got)
        self.assertIn('secondary rate limit', got['error'])
        self.assertEqual((got['rateLimit']['resetAt'], got['rateLimit']['secondary']), (None, True))

    def test_a_malformed_answer_is_an_error(self):
        answers = self.answers([HEAD])
        answers['api --paginate --slurp repos/o/r/pulls/7/reviews?per_page=100'] = [[[{'id': 30, 'user': None}]]]
        code, got = self.harvest(answers)
        self.assertEqual(code, 2)
        self.assertIn('shape harvest.py does not read', got['error'])

    def test_nothing_auto_running_reads_no_head_event_or_settle_artifacts(self):
        answers = self.answers([HEAD])
        for k in [k for k in answers if '/actions/runs' in k or '/timeline' in k or '/contents/' in k or '/statuses' in k or '/check-runs' in k]:
            del answers[k]
        code, got = self.harvest(answers, '--pr', '7', '--reviewers', 'coderabbit,code-scanning')
        self.assertEqual((code, got['headEventAt'], [(b['bot'], b['state']) for b in got['bots']]),
                         (0, None, [('coderabbit', 'harvested'), ('code-scanning', 'harvested')]), got)
        self.assertIn('3 comment(s) harvested', got['bots'][0]['reason'])
        self.assertIn('no reviewer auto-runs', got['headEventEvidence'])
        self.assertEqual(len(got['comments']), 3, 'the comments are still harvested')

    def test_a_greptile_outside_autorun_whose_only_output_on_the_head_is_a_pause_reads_settled_limited(self):
        """#193: PR #189's shape, Greptile requested but not auto-run, its one review on the head a pause notice."""
        paused = greptile_review(HEAD, id_=51, body=harvest.GREPTILE_PAUSED + ' -- it used its 100 free open-source review credits.')
        inline = {'id': 42, 'user': user('greptile-apps[bot]'), 'body': 'nit', 'path': 'a.c', 'line': 5, 'commit_id': HEAD,
                  'created_at': EVENT, 'updated_at': EVENT, 'in_reply_to_id': None, 'html_url': 'u/42'}
        for name, reviews, comments, want in (
                ('pause only', [paused], [], ('settled', 'limited')),
                ('pause on an older head', [paused | {'commit_id': OLD}], [], ('harvested', None)),
                ('pause and a review', [paused, greptile_review(HEAD, id_=52)], [], ('harvested', None)),
                ('pause and an inline comment', [paused], [inline], ('harvested', None)),
                ('another author', [paused | {'user': user('hathach')}], [], ('harvested', None))):
            with self.subTest(name):
                answers = self.answers([HEAD])
                answers['api --paginate --slurp repos/o/r/pulls/7/reviews?per_page=100'][0][0].extend(reviews)
                answers['api --paginate --slurp repos/o/r/pulls/7/comments?per_page=100'][0][0].extend(comments)
                code, got = self.harvest(answers, '--pr', '7', '--reviewers', 'coderabbit,copilot,greptile', '--auto-run', 'coderabbit')
                self.assertEqual(code, 0, got)
                g = next(b for b in got['bots'] if b['bot'] == 'greptile')
                self.assertEqual((g['state'], g['kind']), want, g)
                if want[0] == 'settled':
                    self.assertEqual((g['sha'], g['evidence']), (HEAD, [f'review 51 on {HEAD[:8]}']))
                    self.assertIn(51, [c['commentId'] for c in got['comments']], 'the notice is still harvested')

    def test_a_greptile_pause_reads_without_any_auto_run_artifact(self):
        answers = self.answers([HEAD])
        for k in [k for k in answers if '/actions/runs' in k or '/timeline' in k or '/contents/' in k or '/statuses' in k or '/check-runs' in k]:
            del answers[k]
        answers['api --paginate --slurp repos/o/r/pulls/7/reviews?per_page=100'] = [
            [[greptile_review(HEAD, id_=51, body=harvest.GREPTILE_PAUSED)]], [[]]]
        code, got = self.harvest(answers, '--pr', '7', '--reviewers', 'greptile')
        self.assertEqual((code, [(b['state'], b['kind']) for b in got['bots']]), (0, [('settled', 'limited')]), got)
        self.assertEqual([c['commentId'] for c in got['comments']], [51], 'the reviews are read once')

    def test_usage(self):
        for argv in (['--pr', '7', '--reviewers', 'coderabbit', '--auto-run', 'greptile'], ['--pr', '7', '--reviewers', 'bard'], ['--pr', '7'],
                     ['--pr', '7', '--reviewers', 'code-scanning', '--auto-run', 'code-scanning'],
                     ['--pr', '7', '--reviewers', 'copilot', '--auto-run', 'copilot'], ['--pr', '7', '--reviewers', 'codex']):
            code, got = self.harvest({}, *argv)
            self.assertEqual(code, 2, argv)


if __name__ == '__main__':
    unittest.main()
