"""Tests for pr-babysit's harvest.py: the settle rules on fixture artifacts, and one end-to-end read through a fake gh."""
import base64
import hashlib
import importlib.util
import io
import json
import os
import tempfile
import unittest
from contextlib import redirect_stdout
from pathlib import Path
from unittest import mock

SCRIPT = Path(__file__).resolve().parents[1] / 'skills' / 'pr-babysit' / 'scripts' / 'harvest.py'
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


class SettleRules(unittest.TestCase):
    def test_copilot(self):
        bot = 'copilot-pull-request-reviewer[bot]'
        cases = [
            ([review(bot, HEAD, '## Pull request overview')], [], ('reviewed', None)),
            ([review(bot, HEAD, 'no header at all')], [], ('reviewed', None)),
            ([review(bot, HEAD, harvest.COPILOT_LIMITED + ' more')], [], ('settled', 'limited')),
            ([review(bot, HEAD, harvest.COPILOT_FAILED + '.')], [], ('settled', 'failed')),
            ([review(bot, OLD, 'old')], [{'login': 'Copilot'}], ('queued', None)),
            ([review(bot, OLD, 'old')], [], ('absent', None)),
        ]
        for reviews, requested, want in cases:
            r = harvest.copilot(HEAD, reviews, requested)
            self.assertEqual((r['state'], r['kind']), want, (reviews, requested))
        self.assertIsNone(harvest.copilot(HEAD, [review(bot, OLD, 'old')], [])['sha'], 'an older review is evidence, never the sha')

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

    def test_digest_matches_the_one_pr_reply_reads_back(self):
        spec_ = importlib.util.spec_from_file_location('pr_reply', SCRIPT.parents[2] / 'pr-reply' / 'scripts' / 'reply.py')
        reply = importlib.util.module_from_spec(spec_)
        spec_.loader.exec_module(reply)
        for body in (None, '', 'fix this', '📝 nit\r\nline'):
            self.assertEqual(harvest.digest(body), reply.comment_digest(body), body)
            self.assertEqual(harvest.digest(body), hashlib.sha256((body or '').encode()).hexdigest()[:12])

    def test_codex(self):
        bot = 'chatgpt-codex-connector[bot]'
        sticky = lambda row: comment(bot, f'{harvest.CODEX_MARKER}\n| Review | Status | Commit |\n{row}\n| 🔒 Security Review | ✅ **Completed** | {OLD[:7]} |')
        self.assertEqual(harvest.codex(HEAD, [sticky(f'| 📝 Code Review | ✅ **Completed** | {HEAD[:7]} |')], [])['state'], 'reviewed')
        self.assertEqual(harvest.codex(HEAD, [sticky(f'| 📝 Code Review | ✅ **Completed** | {OLD[:7]} |')], [])['state'], 'absent',
                         'the Security Review row tracks the opening commit and never settles')
        self.assertEqual(harvest.codex(HEAD, [sticky(f'| 📝 Code Review | ⏳ In progress | {HEAD[:7]} |')], [])['state'], 'working')
        self.assertEqual(harvest.codex(HEAD, [sticky(f'| 📝 Code Review | 🤔 Pondering | {HEAD[:7]} |')], [])['state'], 'unknown')
        self.assertEqual(harvest.codex(HEAD, [], [review(bot, HEAD, f'**Reviewed commit:** {HEAD[:7]}')])['state'], 'reviewed')
        self.assertEqual(harvest.codex(HEAD, [], [])['state'], 'absent')


class FailClosed(unittest.TestCase):
    """What a first review found failing open: each case must end unknown or with the older, provable state."""

    def test_an_undated_check_run_cannot_be_ordered(self):
        queued = greptile_run('queued', at=None)
        queued['id'] = 10
        self.assertEqual(harvest.greptile(HEAD, [greptile_run('completed', 'success'), queued], [], [], EVENT)['state'], 'unknown')
        self.assertEqual(harvest.greptile(HEAD, [queued], [], [], EVENT)['state'], 'queued', 'a lone queued run needs no ordering')
        cr = {'id': 11, 'name': 'CodeRabbit', 'status': 'queued', 'conclusion': None, 'started_at': None}
        self.assertEqual(harvest.coderabbit(HEAD, [], [cr], [], EVENT)['state'], 'queued')

    def test_an_undated_copilot_review_among_several_on_the_head_cannot_be_ordered(self):
        bot = 'copilot-pull-request-reviewer[bot]'
        undated = review(bot, HEAD, harvest.COPILOT_LIMITED, at=None, id_=5)
        self.assertEqual(harvest.copilot(HEAD, [review(bot, HEAD, 'ok'), undated], [])['state'], 'unknown')
        self.assertEqual(harvest.copilot(HEAD, [undated], [])['kind'], 'limited', 'a lone review needs no ordering')

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
        cx = 'chatgpt-codex-connector[bot]'
        proven = review(cx, HEAD, f'**Reviewed commit:** `{HEAD[:7]}`', at=at)
        failed = comment(cx, f'{harvest.CODEX_MARKER}\n| Review | Status | Commit |\n|---|---|---|\n| 📝 Code Review | ❌ Failed | {HEAD[:7]} |', updated=at)
        self.assertEqual(harvest.codex(HEAD, [failed], [proven])['state'], 'unknown')
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
        cp = 'copilot-pull-request-reviewer[bot]'
        self.assertEqual(harvest.copilot(HEAD, [review(cp, HEAD, 'ok', at=at), review(cp, HEAD, harvest.COPILOT_FAILED, at=at, id_=2)], [])['state'], 'unknown')
        self.assertEqual(harvest.copilot(HEAD, [review(cp, HEAD, 'ok', at=at), review(cp, HEAD, 'fine too', at=at, id_=2)], [])['state'], 'reviewed')

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

    def test_codex_needs_its_proof_and_the_newest_artifact_wins(self):
        bot = 'chatgpt-codex-connector[bot]'
        self.assertEqual(harvest.codex(HEAD, [], [review(bot, HEAD, 'looks fine')])['state'], 'absent', 'a review without Reviewed commit proves nothing')
        proven = review(bot, HEAD, f'**Reviewed commit:** `{HEAD[:7]}`', at='2026-09-25T09:00:00Z')
        failed = comment(bot, f'{harvest.CODEX_MARKER}\n| Review | Status | Commit |\n|---|---|---|\n| 📝 Code Review | ❌ Failed | {HEAD[:7]} |',
                         updated='2026-09-25T10:00:00Z')
        got = harvest.codex(HEAD, [failed], [proven])
        self.assertEqual((got['state'], got['kind']), ('settled', 'failed'), 'the newer failed row beats the older review')

    def test_unfamiliar_artifacts_are_unknown(self):
        bot = 'chatgpt-codex-connector[bot]'
        table = lambda status, commit: comment(bot, f'{harvest.CODEX_MARKER}\n| Review | Status | Commit |\n| 📝 Code Review | {status} | {commit} |')
        self.assertEqual(harvest.codex(HEAD, [table('✅ **Completed**', '—')], [])['state'], 'unknown', 'a row with no readable commit')
        self.assertEqual(harvest.codex(HEAD, [table(f'✅ **Completed** (was {HEAD[:7]})', OLD[:7])], [])['state'], 'absent',
                         'a head SHA outside the Commit column names nothing')
        self.assertEqual(harvest.coderabbit(HEAD, [status('success', 'Something new')], [], [], EVENT)['state'], 'unknown')
        renamed = comment(bot, f'{harvest.CODEX_MARKER}\n| Review | Status text | Commit |\n| 📝 Code Review | ✅ **Completed** | {HEAD[:7]} |')
        self.assertEqual(harvest.codex(HEAD, [renamed], [])['state'], 'unknown', 'a renamed column is unread, not a crash')
        self.assertEqual(harvest.codex(HEAD, [table('❌ Pondering', HEAD[:7])], [])['state'], 'unknown')
        old = comment(bot, f'{harvest.CODEX_MARKER}\nsomething new', updated='2026-09-25T08:30:00Z')
        proven = review(bot, HEAD, f'**Reviewed commit:** {HEAD[:7]}', at='2026-09-25T09:00:00Z')
        self.assertEqual(harvest.codex(HEAD, [old], [proven])['state'], 'reviewed', 'a newer proving review outlives an unreadable summary')
        self.assertEqual(harvest.codex(HEAD, [comment(bot, f'{harvest.CODEX_MARKER}\nsomething new', updated='2026-09-25T09:30:00Z')], [proven])['state'], 'unknown')
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
    """A gh on PATH answering from a {joined argv: [answers...]} map; each call pops the first answer that remains."""

    def __init__(self, root, answers):
        self.file = root / 'answers.json'
        self.file.write_text(json.dumps(answers))
        bin_dir = root / 'bin'
        bin_dir.mkdir(exist_ok=True)
        (bin_dir / 'gh').write_text(f'''#!/usr/bin/env python3
import json, sys
f = {str(self.file)!r}
answers = json.load(open(f))
key = ' '.join(sys.argv[1:])
got = answers.get(key)
if not got:
    sys.stderr.write('no answer for ' + key); sys.exit(1)
out = got.pop(0) if len(got) > 1 else got[0]
json.dump(answers, open(f, 'w'))
print(json.dumps(out))
''')
        (bin_dir / 'gh').chmod(0o755)
        self.path = f"{bin_dir}{os.pathsep}{os.environ['PATH']}"


def workflow(on):
    return {'content': base64.b64encode(on.encode()).decode()}


class EndToEnd(unittest.TestCase):
    def setUp(self):
        tmp = tempfile.TemporaryDirectory()
        self.addCleanup(tmp.cleanup)
        self.root = Path(tmp.name)
        harvest.pr_types.cache_clear()

    def answers(self, heads):
        R, N = 'o/r', 7
        view = lambda h: {'headRefOid': h, 'headRefName': 'fix', 'headRepository': {'name': 'r', 'nameWithOwner': 'o/r'}, 'reviewRequests': []}
        run = lambda id_, event, path, at, h=HEAD: {'id': id_, 'event': event, 'path': path, 'created_at': at, 'head_sha': h,
                                                    'head_branch': 'fix', 'head_repository': {'full_name': 'o/r'}}
        a = {'repo view --json nameWithOwner': [{'nameWithOwner': R}],
             f'pr view {N} --json headRefOid,headRefName,headRepository,reviewRequests': [view(h) for h in heads],
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
        with mock.patch.dict(os.environ, {'PATH': gh.path}), redirect_stdout(out):
            code = harvest.report(harvest.collect, list(argv) or ['--pr', '7', '--reviewers', 'coderabbit,copilot', '--auto-run', 'coderabbit'])
        return code, json.loads(out.getvalue().splitlines()[-1])

    def test_one_read(self):
        code, got = self.harvest(self.answers([HEAD]))
        self.assertEqual(code, 0, got)
        self.assertEqual((got['headSha'], got['headEventAt']), (HEAD, '2026-09-25T08:00:00Z'), 'the labeled run and the other branch date nothing')
        self.assertIn('run 2', got['headEventEvidence'])
        self.assertEqual([b['state'] for b in got['bots']], ['reviewed'])
        self.assertEqual([(c['kind'], c['commentId']) for c in got['comments']], [('review', 40), ('issue', 20), ('review-body', 30)],
                         'only the named reviewers, and no empty review body')
        self.assertEqual(got['comments'][0]['digest'], harvest.digest('fix this'))
        self.assertEqual(got['comments'][0]['body'], 'fix this')

    def test_a_moving_head_is_read_again_then_refused(self):
        code, got = self.harvest(self.answers([OLD, HEAD]))
        self.assertEqual((code, got['headSha']), (0, HEAD))
        code, got = self.harvest(self.answers([OLD, HEAD, 'c' * 40]))
        self.assertEqual(code, 2)
        self.assertIn('head moved', got['error'])

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
        code, got = self.harvest(answers, '--pr', '7', '--reviewers', 'coderabbit')
        self.assertEqual((code, got['headEventAt'], got['bots']), (0, None, []), got)
        self.assertIn('no reviewer auto-runs', got['headEventEvidence'])
        self.assertEqual(len(got['comments']), 3, 'the comments are still harvested')

    def test_usage(self):
        for argv in (['--pr', '7', '--reviewers', 'coderabbit', '--auto-run', 'greptile'], ['--pr', '7', '--reviewers', 'bard'], ['--pr', '7'],
                     ['--pr', '7', '--reviewers', 'code-scanning', '--auto-run', 'code-scanning']):
            code, got = self.harvest({}, *argv)
            self.assertEqual(code, 2, argv)


if __name__ == '__main__':
    unittest.main()
