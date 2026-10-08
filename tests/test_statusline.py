import importlib.util
import json
import os
import re
import subprocess
import sys
import tempfile
import time
import unittest
from unittest import mock
from pathlib import Path

ROOT = Path(__file__).resolve().parents[1] / 'statusline'
spec = importlib.util.spec_from_file_location('codex_usage', ROOT / 'statusline-codex-usage.py')
codex_usage = importlib.util.module_from_spec(spec)
spec.loader.exec_module(codex_usage)
spec = importlib.util.spec_from_file_location('statusline_prs', ROOT / 'statusline-prs.py')
prs = importlib.util.module_from_spec(spec)
spec.loader.exec_module(prs)
SID = '38bd3fd0-775b-4cdb-bc1f-32d8a7eb775a'
ANSI = re.compile(r'\x1b\[[0-9;]*m|\x1b\]8;;[^\a]*\a')


def link(number, repo='hathach/tinyusb', sid=SID):
    return {'type': 'pr-link', 'sessionId': sid, 'prNumber': number, 'prRepository': repo,
            'prUrl': f'https://github.com/{repo}/pull/{number}', 'timestamp': '2026-10-08T09:38:19.195Z'}


def plain(text):
    return ANSI.sub('', text)


def window(used, mins):
    return {'usedPercent': used, 'windowDurationMins': mins, 'resetsAt': 1}


class CodexWeeklyTest(unittest.TestCase):
    def test_the_codex_bucket_wins_and_either_slot_may_hold_the_week(self):
        result = {'rateLimitsByLimitId': {
            'other': {'primary': window(90, 10080)},
            'codex': {'primary': window(40, 10080), 'secondary': None}}}
        self.assertEqual(codex_usage.weekly(result)['usedPercent'], 40)
        result = {'rateLimitsByLimitId': {'codex': {'primary': window(5, 300), 'secondary': window(60, 10080)}}}
        self.assertEqual(codex_usage.weekly(result)['usedPercent'], 60)

    def test_legacy_snapshot_near_a_week_counts_but_another_limit_does_not(self):
        self.assertEqual(codex_usage.weekly({'rateLimits': {'primary': window(7, 10000)}})['usedPercent'], 7)
        self.assertIsNone(codex_usage.weekly({'rateLimits': {'limitId': 'gpt-x', 'primary': window(7, 10080)}}))
        self.assertIsNone(codex_usage.weekly({'rateLimits': {'primary': window(7, 300)}}))


class RenderTest(unittest.TestCase):
    def test_renders_from_input_alone_and_exits_zero(self):
        with tempfile.TemporaryDirectory() as home:
            payload = {'workspace': {'current_dir': f'{home}/code'}, 'model': {'display_name': 'Opus (1M context)'},
                       'effort': {'level': 'high'}, 'context_window': {'used_percentage': 12}}
            done = subprocess.run(['bash', str(ROOT / 'statusline.sh')], input=json.dumps(payload),
                                  capture_output=True, text=True,
                                  env={**os.environ, 'HOME': home, 'CLAUDE_CONFIG_DIR': home})
        self.assertEqual(done.returncode, 0, done.stderr)
        self.assertIn('~/code', done.stdout)
        self.assertIn('Opus', done.stdout)
        self.assertNotIn('1M context', done.stdout)
        self.assertIn('12%', done.stdout)
        self.assertIn('?', done.stdout, 'no usage cache, so the placeholders show')

    def test_pr_segment_renders_from_cache_while_a_slow_refresh_runs_detached(self):
        with tempfile.TemporaryDirectory() as home:
            home = Path(home)
            (home / '.claude').mkdir()
            os.symlink(ROOT / 'statusline-prs.py', home / '.claude' / 'statusline-prs.py')
            (home / 'bin').mkdir()
            (home / 'bin' / 'gh').write_text(  # holds the worker in gh until the test releases it
                '#!/bin/sh\ntouch "$HOME/entered"\nwhile [ ! -e "$HOME/go" ]; do sleep 0.05; done\n')
            (home / 'bin' / 'gh').chmod(0o755)
            transcript = home / 't.jsonl'
            transcript.write_text(json.dumps(link(7, 'o/r')) + '\n')
            (home / 'statusline-prs').mkdir()
            (home / 'statusline-prs' / f'{SID}.json').write_text(json.dumps({
                'prs': ['o/r#7'], 'attempt_at': 0,
                'status': {'o/r#7': {'state': 'OPEN', 'rollup': 'SUCCESS'}}}))
            payload = {'workspace': {'current_dir': str(home)}, 'session_id': SID, 'transcript_path': str(transcript)}
            try:
                # a render that waited for the held gh would outlast this timeout (GH_TIMEOUT_S is longer)
                done = subprocess.run(['bash', str(ROOT / 'statusline.sh')], input=json.dumps(payload),
                                      capture_output=True, text=True, timeout=prs.GH_TIMEOUT_S / 2,
                                      env={**os.environ, 'HOME': str(home), 'CLAUDE_CONFIG_DIR': str(home),
                                           'PATH': f"{home / 'bin'}:{os.environ['PATH']}"})
                self.assertEqual(done.returncode, 0, done.stderr)
                self.assertIn('#7✓', plain(done.stdout))
                deadline = time.monotonic() + 10
                while not (home / 'entered').exists() and time.monotonic() < deadline:
                    time.sleep(0.05)
                self.assertTrue((home / 'entered').exists(), 'the detached worker reached gh after render returned')
            finally:
                (home / 'go').touch()
                cache = home / 'statusline-prs' / f'{SID}.json'
                deadline = time.monotonic() + 10
                while 'retry_after' not in prs.load(cache) and time.monotonic() < deadline:
                    time.sleep(0.05)  # the worker publishes its failed poll before the directory goes


class PrScanTest(unittest.TestCase):
    def scan(self, *lines):
        with tempfile.TemporaryDirectory() as d:
            path = Path(d, 't.jsonl')
            path.write_bytes(b''.join(line if isinstance(line, bytes) else (json.dumps(line) + '\n').encode()
                                      for line in lines))
            return prs.scan(path, SID)

    def test_first_link_order_survives_restamps_and_relinks(self):
        self.assertEqual(self.scan(link(4059), link(4066), link(4059), link(4068), link(4066), link(4164)),
                         ['hathach/tinyusb#4059', 'hathach/tinyusb#4066', 'hathach/tinyusb#4068',
                          'hathach/tinyusb#4164'])

    def test_whitespace_json_counts_and_torn_or_foreign_lines_do_not(self):
        spaced = json.dumps(link(1), indent=None, separators=(', ', ': ')).encode() + b'\n'
        self.assertEqual(self.scan(spaced, {'type': 'user', 'text': 'pr-link'}, link(2, sid='other'),
                                   link(3, repo='a/b c'), link(True), link(0),
                                   {**link(4), 'prUrl': 'https://gitlab.com/hathach/tinyusb/pull/4'},
                                   {**link(5), 'prUrl': 'https://github.com/hathach/tinyusb/pull/6'},
                                   json.dumps(link(7))[:-5].encode()),
                         ['hathach/tinyusb#1'])


class PrRenderTest(unittest.TestCase):
    def render(self, ids, status=None):
        return plain(prs.render({'prs': ids, 'status': status or {}}))

    def status(self, **fields):
        return {'state': 'OPEN', 'rollup': 'SUCCESS', **fields}

    def test_newest_link_first_terminal_hidden_unknown_shown(self):
        ids = ['o/r#1', 'o/r#2', 'o/r#3']
        status = {'o/r#1': self.status(), 'o/r#2': self.status(state='MERGED')}
        self.assertEqual(self.render(ids, status), '#3? #1✓')

    def test_the_mark_is_the_check_rollup_alone(self):
        cases = {'✗': [dict(rollup='FAILURE'), dict(rollup='ERROR')],
                 '⏳': [dict(rollup='PENDING'), dict(rollup='EXPECTED')],
                 '✓': [dict(), dict(reviewDecision='CHANGES_REQUESTED'), dict(isDraft=True)],
                 '?': [dict(rollup=None), dict(rollup='SOMETHING_NEW')]}
        for mark, variants in cases.items():
            for fields in variants:
                with self.subTest(mark=mark, fields=fields):
                    self.assertEqual(self.render(['o/r#9'], {'o/r#9': self.status(**fields)}), '#9' + mark)

    def test_overflow_and_repo_qualifier(self):
        ids = [f'o/r#{n}' for n in range(1, 8)]
        self.assertEqual(self.render(ids), '#7? #6? #5? #4? #3? +2')
        self.assertEqual(self.render(['o/a#1', 'o/b#2']), 'b#2? a#1?')

    def test_each_label_links_to_its_pull_request(self):
        out = prs.render({'prs': ['o/a#1', 'p/b#2'], 'status': {}})
        self.assertIn('\x1b[38;5;208m\x1b]8;;https://github.com/p/b/pull/2\ab#2\x1b]8;;\a', out)
        self.assertIn('\x1b]8;;https://github.com/o/a/pull/1\aa#1\x1b]8;;\a', out)

    def test_older_open_pr_behind_many_terminal_ones_still_shows(self):
        ids = ['o/r#1'] + [f'o/r#{n}' for n in range(2, 30)]
        status = {pr: self.status(state='CLOSED') for pr in ids[1:]}
        self.assertEqual(self.render(ids, status), '#1?')

    def test_corrupt_or_missing_cache_renders_nothing_and_exits_zero(self):
        with tempfile.TemporaryDirectory() as d:
            Path(d, f'{SID}.json').write_text('{"prs": [1, "o/r#')
            for sid in (SID, 'missing'):
                done = subprocess.run([sys.executable, str(ROOT / 'statusline-prs.py'), 'render', d, sid, '/none'],
                                      capture_output=True, text=True)
                self.assertEqual((done.returncode, done.stdout.strip()), (0, ''), done.stderr)
            Path(d, 'bad-ids.json').write_text('{"prs": ["broken", "o/r#", "o/r#0", "o/r#1"]}')
            done = subprocess.run([sys.executable, str(ROOT / 'statusline-prs.py'), 'render', d, 'bad-ids', '/none'],
                                  capture_output=True, text=True)
            self.assertEqual((done.returncode, plain(done.stdout).strip()), (0, '#1?'), done.stderr)
            self.assertEqual(prs.render({'prs': 'x', 'status': []}), '')


STUB_GH = """#!/bin/sh
echo "$@" >> "$STUB_DIR/calls"
[ -n "$STUB_SLEEP" ] && sleep "$STUB_SLEEP"
cat "$STUB_DIR/answer"
exit "${STUB_RC:-0}"
"""


def pull(state='OPEN', rollup='SUCCESS'):
    nodes = [{'commit': {'statusCheckRollup': rollup and {'state': rollup}}}]
    return {'state': state, 'commits': {'nodes': nodes}}


class PrRefreshTest(unittest.TestCase):
    def setUp(self):
        tmp = tempfile.TemporaryDirectory()
        self.addCleanup(tmp.cleanup)
        self.dir = Path(tmp.name)
        (self.dir / 'bin').mkdir()
        gh = self.dir / 'bin' / 'gh'
        gh.write_text(STUB_GH)
        gh.chmod(0o755)
        env = mock.patch.dict(os.environ, {'PATH': f"{self.dir / 'bin'}:{os.environ['PATH']}",
                                           'STUB_DIR': str(self.dir)})
        env.start()
        self.addCleanup(env.stop)
        self.cache_dir, self.transcript = self.dir / 'cache', self.dir / 't.jsonl'
        self.write_links(link(1, 'o/r'), link(2, 'o/r'), link(3, 'o/s'))

    def write_links(self, *entries):
        with open(self.transcript, 'a') as f:
            f.writelines(json.dumps(e) + '\n' for e in entries)

    def answer(self, data=None, errors=None, remaining=4000, reset='2026-10-08T12:00:00Z'):
        rate = {'remaining': remaining, 'resetAt': reset} if remaining is not None else None
        body = {'data': data and {'rateLimit': rate, **data}}
        if errors:
            body['errors'] = errors
        (self.dir / 'answer').write_text(json.dumps(body))

    @staticmethod
    def all(**pulls):
        """An answer naming every queried PR: o/r#1, o/r#2 as r0 and o/s#3 as r1."""
        p = {'p1': pull(), 'p2': pull(), 'p3': pull(), **pulls}
        return {'r0': {'p1': p['p1'], 'p2': p['p2']}, 'r1': {'p3': p['p3']}}

    def calls(self):
        path = self.dir / 'calls'
        return path.read_text().splitlines() if path.exists() else []

    def cache(self):
        return json.loads((self.cache_dir / f'{SID}.json').read_text())

    def refresh(self):
        prs.refresh(self.cache_dir, SID, str(self.transcript))

    def age(self, **fields):
        cache = self.cache()
        cache.update(attempt_at=0, **fields)
        (self.cache_dir / f'{SID}.json').write_text(json.dumps(cache))

    def test_one_request_for_every_repo_null_is_unverified_and_not_due_twice(self):
        self.answer(self.all(p2=pull('MERGED'), p3=None))
        self.refresh()
        self.assertEqual(len(self.calls()), 1)
        self.assertIn('r1: repository(owner: "o", name: "s")', self.calls()[0])
        cache = self.cache()
        self.assertEqual(cache['prs'], ['o/r#1', 'o/r#2', 'o/s#3'])
        self.assertEqual(sorted(cache['status']), ['o/r#1', 'o/r#2'])
        self.assertNotIn('retry_after', cache)
        self.assertEqual(plain(prs.render(cache)), 's#3? r#1✓')
        self.refresh()
        self.assertEqual(len(self.calls()), 1, 'a fresh attempt is not repeated')

    def test_a_pr_without_checks_is_unknown_and_the_others_still_update(self):
        self.answer(self.all(p1=pull(rollup=None), p2=pull(rollup='PENDING')))
        self.refresh()
        status = self.cache()['status']
        self.assertEqual((status['o/r#1']['rollup'], status['o/r#2']['rollup']), (None, 'PENDING'))
        self.assertNotIn('retry_after', self.cache())

    def test_graphql_errors_keep_last_good_with_its_age_and_back_off(self):
        self.answer(self.all(p3=pull(rollup='FAILURE')))
        self.refresh()
        good = self.cache()['status']
        self.age()
        self.answer(self.all(p1=None), errors=[{'type': 'NOT_FOUND'}])
        before = time.time()
        self.refresh()
        cache = self.cache()
        self.assertEqual(cache['status'], good, 'last-good kept')
        self.assertGreaterEqual(cache['retry_after'], before + prs.BACKOFF_S)
        self.age()
        self.refresh()
        self.assertEqual(len(self.calls()), 2, 'retry_after holds back the next poll')

    def test_failed_gh_and_malformed_fields_back_off(self):
        for setup in (lambda: os.environ.update(STUB_RC='1'),
                      lambda: self.answer(self.all(p1={'state': 'OPEN'})),
                      lambda: self.answer(self.all(p1={**pull(), 'commits': {'nodes': None}})),
                      lambda: self.answer({'r0': {'p1': pull(), 'p2': pull()}, 'r1': {}}),
                      lambda: self.answer(self.all(), remaining=None)):
            with self.subTest(setup=setup), mock.patch.dict(os.environ):
                self.answer(self.all())
                setup()
                if (self.cache_dir / f'{SID}.json').exists():
                    self.age(retry_after=0)
                self.refresh()
                self.assertGreater(self.cache()['retry_after'], time.time())

    def test_slow_gh_is_cut_off_and_backs_off(self):
        self.answer(self.all())
        with mock.patch.dict(os.environ, STUB_SLEEP='1'), mock.patch.object(prs, 'GH_TIMEOUT_S', 0.2):
            self.refresh()
        self.assertGreater(self.cache()['retry_after'], time.time())

    def test_low_points_wait_for_reset(self):
        self.answer(self.all(), remaining=50, reset='2099-01-01T00:00:00Z')
        self.refresh()
        self.assertEqual(self.cache()['retry_after'], 4070908800.0)
        self.assertIn('o/r#1', self.cache()['status'])

    def test_changed_transcript_is_rescanned(self):
        self.answer(self.all())
        self.refresh()
        self.write_links(link(4, 'o/r'))
        self.age()
        self.refresh()
        self.assertEqual(self.cache()['prs'], ['o/r#1', 'o/r#2', 'o/s#3', 'o/r#4'])
        self.assertIn('p4: pullRequest(number: 4)', self.calls()[-1])

    def test_missing_transcript_keeps_membership(self):
        self.answer(self.all())
        self.refresh()
        self.transcript.unlink()
        self.age()
        self.refresh()
        self.assertEqual(self.cache()['prs'], ['o/r#1', 'o/r#2', 'o/s#3'])
        self.assertEqual(len(self.calls()), 2)

    def spawned(self, transcript):
        with mock.patch.object(prs, 'spawn_refresh') as spawn, mock.patch('builtins.print'):
            prs.main(['statusline-prs.py', 'render', str(self.cache_dir), SID, str(transcript)])
        return spawn.called

    def test_render_starts_a_due_refresh_for_known_prs_even_without_a_transcript(self):
        self.answer(self.all())
        self.refresh()
        self.age()
        self.assertTrue(self.spawned(self.transcript))
        self.assertTrue(self.spawned(self.dir / 'gone'), 'known PRs keep polling')
        self.age(prs=[])
        self.assertFalse(self.spawned(self.dir / 'gone'), 'nothing to discover or poll')

    def test_merged_prs_leave_the_poll_and_closed_ones_stay(self):
        self.answer(self.all(p1=pull('MERGED'), p2=pull('CLOSED')))
        self.refresh()
        self.age()
        self.answer({'r0': {'p2': pull('OPEN')}, 'r1': {'p3': pull()}})
        self.refresh()
        self.assertNotIn('p1:', self.calls()[-1])
        status = self.cache()['status']
        self.assertEqual((status['o/r#1']['state'], status['o/r#2']['state']), ('MERGED', 'OPEN'),
                         'merged status kept, closed one seen reopened')
        self.answer(self.all(p1=pull('MERGED'), p2=pull('MERGED'), p3=pull('MERGED')))
        self.age()
        self.refresh()
        self.age()
        self.refresh()
        self.assertEqual(len(self.calls()), 3, 'an all-merged session makes no gh call')
        self.assertNotIn('retry_after', self.cache())

    def test_a_refresh_sweeps_only_this_helpers_idle_files(self):
        self.answer(self.all())
        self.cache_dir.mkdir()
        old, fresh = time.time() - prs.KEEP_S - 60, time.time() - 600
        files = {'idle-1.json': old, 'live-2.json': fresh, 'idle-1.json.4242.tmp': old, 'live-2.json.4243.tmp': fresh,
                 'idle-5.lock': old, 'live-6.lock': fresh, 'notes.txt': old, '.lock': old}
        for name, mtime in files.items():
            (self.cache_dir / name).write_text('x')
            os.utime(self.cache_dir / name, (mtime, mtime))
        (self.cache_dir / 'dir-3.json').mkdir()
        os.utime(self.cache_dir / 'dir-3.json', (old, old))
        os.symlink(self.dir / 'answer', self.cache_dir / 'link-4.json')
        os.utime(self.cache_dir / 'link-4.json', (old, old), follow_symlinks=False)
        self.refresh()
        self.assertEqual(sorted(os.listdir(self.cache_dir)),
                         ['.lock', f'{SID}.json', 'dir-3.json', 'live-2.json', 'live-2.json.4243.tmp', 'live-6.lock',
                          'notes.txt'])
        self.assertTrue((self.dir / 'answer').exists(), 'a swept symlink leaves its target')

    def test_an_unreadable_cache_dir_leaves_the_refresh_result_in_place(self):
        self.answer(self.all())
        with mock.patch.object(prs.os, 'scandir', side_effect=PermissionError):
            self.refresh()
        self.assertIn('o/r#1', self.cache()['status'])

    def test_a_clock_jump_cannot_admit_a_second_worker_mid_refresh(self):
        self.answer(self.all())
        poll = prs.fetch

        def jump_then_poll(polled):
            self.write_links(link(4, 'o/r'))
            with mock.patch.object(prs.time, 'time', return_value=time.time() + prs.REFRESH_S + 1):
                self.refresh()  # would publish the new link if the lock let it in
            return poll(polled)

        with mock.patch.object(prs, 'fetch', side_effect=jump_then_poll):
            self.refresh()
        self.assertEqual(self.cache()['prs'], ['o/r#1', 'o/r#2', 'o/s#3'])
        self.assertEqual(len(self.calls()), 1)

    def test_a_held_lock_makes_a_second_refresher_leave_without_writing(self):
        self.answer(self.all())
        self.cache_dir.mkdir()
        with open(self.cache_dir / '.lock', 'a') as held:
            prs.fcntl.flock(held, prs.fcntl.LOCK_EX)
            self.refresh()
        self.assertFalse((self.cache_dir / f'{SID}.json').exists())
        self.assertEqual(self.calls(), [])


if __name__ == '__main__':
    unittest.main()
