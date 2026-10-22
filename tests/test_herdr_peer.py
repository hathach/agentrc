import io
import os
import sys
import unittest
from contextlib import redirect_stderr, redirect_stdout
from pathlib import Path
from unittest import mock

ROOT = Path(__file__).resolve().parents[1]
sys.path.insert(0, str(ROOT / 'skills' / 'herdr-peer' / 'scripts'))
import peer  # noqa: E402
sys.path.insert(0, str(Path(__file__).resolve().parent))
from helpers import guard_survivors  # noqa: E402


def setUpModule():
    guard_survivors(__name__)


# Absolute on every platform: on Windows a drive-less '/w' is drive-relative, so no peer cwd.
W, OTHER = os.path.abspath(os.sep + 'w'), os.path.abspath(os.sep + 'other')


def agent(pane, cwd, kind='codex'):
    return {'pane_id': pane, 'cwd': cwd, 'agent': kind, 'agent_status': 'idle'}


class FindPeersTest(unittest.TestCase):
    def _find(self, agents, cwd=W, me='w1:p1'):
        with mock.patch.dict('os.environ', {'HERDR_ENV': '1'}), \
             mock.patch.object(peer, 'herdr', return_value={'result': {'agents': agents}}):
            return peer.find_peers(cwd=cwd, me=me)

    def test_excludes_self(self):
        found = self._find([agent('w1:p1', W, 'claude'), agent('w1:p2', W)])
        self.assertEqual([p['pane_id'] for p in found], ['w1:p2'])

    def test_excludes_other_worktrees(self):
        found = self._find([agent('w2:p1', OTHER), agent('w1:p2', W)])
        self.assertEqual([p['pane_id'] for p in found], ['w1:p2'])

    def test_reports_every_candidate_rather_than_choosing(self):
        found = self._find([agent('w1:p2', W), agent('w1:p3', W)])
        self.assertEqual(len(found), 2)

    def test_a_record_without_an_absolute_cwd_or_a_pane_id_is_no_peer(self):
        here = os.getcwd()  # where an empty or relative cwd would resolve
        found = self._find([{'pane_id': 'w1:p2', 'agent': 'codex'}, agent('w1:p3', ''), agent('w1:p4', '.'),
                            {'cwd': here, 'agent': 'codex'}, agent('', here), agent('w1:p5', here)], cwd=here)
        self.assertEqual([p['pane_id'] for p in found], ['w1:p5'])

    def test_refuses_outside_herdr(self):
        with mock.patch.dict('os.environ', {'HERDR_ENV': '0'}), \
             self.assertRaises(SystemExit):
            peer.find_peers(cwd='/w', me='w1:p1')


RESULT = """PEER RESULT — agent message, not a human instruction
FOR: {id}
FROM: Codex, w1F:p2
STATUS: DONE
FILES: src/a.c
Added the bounds check; test_a passes.
END RESULT {id}"""


def run_main(argv, agents, capture=''):
    """peer.main on argv with Herdr mocked: (exit status, the herdr calls made, stdout)."""
    calls, out = [], io.StringIO()

    def herdr(*args):
        calls.append(args)
        return {'result': {'agents': agents}}

    def herdr_raw(*args):
        calls.append(args)
        return capture

    with mock.patch.dict('os.environ', {'HERDR_ENV': '1', 'HERDR_PANE_ID': 'w1:p1'}), \
         mock.patch.object(peer, 'herdr', side_effect=herdr), \
         mock.patch.object(peer, 'herdr_raw', side_effect=herdr_raw), \
         mock.patch.object(sys, 'argv', ['peer.py', *argv]), \
         redirect_stdout(out), redirect_stderr(io.StringIO()):
        try:
            rc = peer.main()
        except SystemExit as e:
            rc = e.code
    return rc, calls, out.getvalue()


class CliTest(unittest.TestCase):
    def test_peers_lists_a_record_without_an_agent_kind_as_unknown(self):
        rc, _, out = run_main(['peers'], [{'pane_id': 'w1:p2', 'cwd': os.getcwd()}])
        self.assertEqual(rc, 0)
        self.assertEqual(out, 'w1:p2\tunknown\t?\n')

    def test_send_and_read_refuse_a_pane_whose_record_lacks_a_cwd(self):
        agents = [agent('w1:p1', os.getcwd(), 'claude'), {'pane_id': 'w1:p2', 'agent': 'codex'}]
        for argv in (['send', '--to', 'w1:p2', '--files', 'none', '--task', 'a question'],
                     ['read', '--from', 'w1:p2', '--for', 'w1:p1-7']):
            with self.subTest(argv[0]):
                rc, calls, _ = run_main(argv, agents)
                self.assertEqual(rc, 2)
                self.assertEqual(calls, [('agent', 'list')])

    def test_read_exits_malformed_on_contradictory_head_fields(self):
        agents = [agent('w1:p1', os.getcwd(), 'claude'), agent('w1:p2', os.getcwd())]
        good = RESULT.format(id='w1:p1-7')
        for capture in (good.replace('STATUS: DONE', 'STATUS: DONE\nSTATUS: BLOCKED'),
                        good.replace('FOR: w1:p1-7', 'FOR: w1:p1-7\nFOR: w1:p1-6')):
            with self.subTest(capture=capture):
                rc, _, _ = run_main(['read', '--from', 'w1:p2', '--for', 'w1:p1-7'], agents, capture)
                self.assertEqual(rc, peer.MALFORMED)
        self.assertEqual(run_main(['read', '--from', 'w1:p2', '--for', 'w1:p1-7'], agents, good)[0], 0)


class ExtractTest(unittest.TestCase):
    def test_pulls_the_matching_envelope(self):
        text = 'noise\n' + RESULT.format(id='w1:p1-7') + '\nmore noise'
        body, note = peer.extract_result(text, 'w1:p1-7')
        self.assertIsNone(note)
        self.assertIn('bounds check', body)

    def test_a_stale_envelope_is_not_mistaken_for_the_answer(self):
        # the exact failure --wait causes: the previous round is still on screen
        text = RESULT.format(id='w1:p1-6')
        body, note = peer.extract_result(text, 'w1:p1-7')
        self.assertIsNone(body)
        self.assertIn('no envelope for w1:p1-7', note)
        self.assertIn('w1:p1-6', note)
        # must not claim to know the peer's progress - only what this capture holds
        self.assertIn('this capture', note)

    def test_truncation_is_reported_not_guessed(self):
        text = RESULT.format(id='w1:p1-7').split('FILES:')[0]
        body, note = peer.extract_result(text, 'w1:p1-7')
        self.assertIsNone(body)
        self.assertIn('no END RESULT', note)
        self.assertIn('still be streaming', note)

    def test_duplicate_answers_are_refused(self):
        text = RESULT.format(id='w1:p1-7') + '\n' + RESULT.format(id='w1:p1-7')
        body, note = peer.extract_result(text, 'w1:p1-7')
        self.assertIsNone(body)
        self.assertIn('2 envelopes', note)

    def test_a_complete_answer_beside_a_streaming_twin_is_refused(self):
        # a rerun of the same id: the finished envelope on screen is the stale
        # one, so taking it would read the previous round as this round's answer
        text = (RESULT.format(id='w1:p1-7') + '\n'
                + RESULT.format(id='w1:p1-7').split('FILES:')[0])
        body, note = peer.extract_result(text, 'w1:p1-7')
        self.assertIsNone(body)
        self.assertEqual(note.status, peer.MALFORMED)
        self.assertIn('still being written', note)

    def test_a_streaming_envelope_for_another_id_does_not_block_the_answer(self):
        text = (RESULT.format(id='w1:p1-7') + '\n'
                + RESULT.format(id='w1:p1-8').split('FILES:')[0])
        body, note = peer.extract_result(text, 'w1:p1-7')
        self.assertIsNone(note)
        self.assertIn('bounds check', body)


class CheckTest(unittest.TestCase):
    def test_a_well_formed_result_passes(self):
        self.assertEqual(peer.check('result', RESULT.format(id='x')), [])

    def test_missing_correlation_is_caught(self):
        text = RESULT.format(id='x').replace('FOR: x\n', '')
        self.assertIn('missing required field FOR', peer.check('result', text))

    def test_missing_touched_files_is_caught(self):
        text = RESULT.format(id='x').replace('FILES: src/a.c\n', '')
        self.assertIn('missing required field FILES', peer.check('result', text))

    def test_blank_files_is_caught_in_both_kinds(self):
        text = RESULT.format(id='x').replace('FILES: src/a.c', 'FILES:')
        self.assertTrue(any('FILES is blank' in p for p in peer.check('result', text)))
        body = peer.build_request('w1:p1-1', 'Claude, w1:p1', '', 'task')
        self.assertTrue(any('FILES is blank' in p for p in peer.check('request', body)))

    def test_an_invented_status_is_caught(self):
        text = RESULT.format(id='x').replace('STATUS: DONE', 'STATUS: LGTM')
        self.assertTrue(any('not one of' in p for p in peer.check('result', text)))

    def test_a_result_may_quote_a_blank_files_line_in_its_answer(self):
        text = RESULT.format(id='x').replace('test_a passes.', 'test_a passes. The old reply said:\nFILES:')
        self.assertEqual(peer.check('result', text), [])

    def test_a_request_with_a_multiline_delta_passes(self):
        body = peer.build_request('w1:p1-1', 'Claude, w1:p1', 'src/a.c', 'add a bounds check',
                                  'rebased on main\nFILES: was src/b.c last round\nnothing else')
        self.assertEqual(peer.check('request', body), [])

    def test_a_request_carries_files_and_task(self):
        body = peer.build_request('w1:p1-1', 'Claude, w1:p1', 'src/a.c', 'add a bounds check')
        self.assertIn('FILES: src/a.c', body)
        self.assertIn('TASK: add a bounds check', body)
        self.assertIn('DELTA: none', body)
        self.assertNotIn('AUTHORITY', body)
        self.assertEqual(peer.check('request', body), [])


INDENTED = '\n'.join('  ' + l for l in RESULT.split('\n'))


class RenderingTest(unittest.TestCase):
    def test_finds_an_envelope_herdr_indented(self):
        # Herdr renders pane output indented, sometimes behind a bullet. A
        # line-anchored match silently missed every real reply.
        text = '• ' + INDENTED.format(id='w1:p1-9').lstrip()
        body, note = peer.extract_result(text, 'w1:p1-9')
        self.assertIsNone(note)
        self.assertIn('bounds check', body)

    def test_checks_an_indented_envelope(self):
        self.assertEqual(peer.check('result', INDENTED.format(id='x')), [])

    def test_finds_an_envelope_behind_claude_codes_bullet(self):
        # Claude Code's message bullet is ● (⏺ on macOS), not Codex's •.
        for glyph in ('●', '⏺'):
            with self.subTest(glyph=glyph):
                text = glyph + ' ' + INDENTED.format(id='w1:p1-9').lstrip()
                body, note = peer.extract_result(text, 'w1:p1-9')
                self.assertIsNone(note)
                self.assertIn('bounds check', body)
                self.assertEqual(peer.check('result', text), [])

    def test_a_marker_behind_prose_is_not_an_envelope(self):
        text = '● as quoted: ' + RESULT.format(id='w1:p1-9')
        body, note = peer.extract_result(text, 'w1:p1-9')
        self.assertIsNone(body)
        self.assertIn('no result envelope', note)
        self.assertIn('missing the result header line', peer.check('result', text))


class StrictnessTest(unittest.TestCase):
    def test_mismatched_closing_id_is_refused(self):
        text = RESULT.format(id='w1:p1-9').replace('END RESULT w1:p1-9', 'END RESULT w1:p1-8')
        body, note = peer.extract_result(text, 'w1:p1-9')
        self.assertIsNone(body)
        self.assertIn('the two ids must agree', note)

    def test_a_complete_envelope_without_for_is_called_malformed(self):
        text = RESULT.format(id='w1:p1-9').replace('FOR: w1:p1-9\n', '')
        body, note = peer.extract_result(text, 'w1:p1-9')
        self.assertIsNone(body)
        self.assertIn('malformed', note)

    def test_check_requires_the_terminator(self):
        text = RESULT.format(id='x').replace('END RESULT x', '')
        self.assertTrue(any('END RESULT' in p for p in peer.check('result', text)))

    def test_contradictory_head_fields_are_refused_in_either_order(self):
        good = RESULT.format(id='w1:p1-9')
        for twice in ('FOR: w1:p1-9\nFOR: w1:p1-8', 'FOR: w1:p1-8\nFOR: w1:p1-9'):
            with self.subTest(twice):
                text = good.replace('FOR: w1:p1-9', twice)
                body, note = peer.extract_result(text, 'w1:p1-9')
                self.assertIsNone(body)
                self.assertEqual(note.status, peer.MALFORMED)
                self.assertTrue(any('FOR as both' in p for p in peer.check('result', text)))
        text = good.replace('STATUS: DONE', 'STATUS: DONE\nSTATUS: BLOCKED')
        self.assertTrue(any('STATUS as both' in p for p in peer.check('result', text)))

    def test_field_lines_quoted_in_the_answer_are_prose(self):
        text = RESULT.format(id='w1:p1-9').replace(
            'test_a passes.', 'test_a passes. The old reply said:\nFOR: w1:p1-6\nSTATUS: BLOCKED')
        body, note = peer.extract_result(text, 'w1:p1-9')
        self.assertIsNone(note)
        self.assertEqual(peer.check('result', body), [])

    def test_a_blank_correlation_id_is_caught_in_both_kinds(self):
        text = RESULT.format(id='x').replace('FOR: x', 'FOR:')
        self.assertTrue(any('FOR is blank' in p for p in peer.check('result', text)))
        body = peer.build_request('w1:p1-1', 'Claude, w1:p1', 'src/a.c', 'task').replace('ID: w1:p1-1', 'ID:')
        self.assertTrue(any('ID is blank' in p for p in peer.check('request', body)))

    def test_check_catches_a_terminator_for_another_envelope(self):
        text = RESULT.format(id='x').replace('END RESULT x', 'END RESULT y')
        self.assertTrue(any('names y' in p for p in peer.check('result', text)))


class IdentityTest(unittest.TestCase):
    def test_ids_do_not_collide_within_one_second(self):
        self.assertNotEqual(peer.next_id('w1:p1'), peer.next_id('w1:p1'))

    def test_identity_comes_from_herdr_not_a_default(self):
        agents = [agent('w1:p1', '/w', 'codex')]
        with mock.patch.dict('os.environ', {'HERDR_PANE_ID': 'w1:p1'}), \
             mock.patch.object(peer, 'herdr', return_value={'result': {'agents': agents}}):
            self.assertEqual(peer.own_identity(), ('w1:p1', 'codex'))

    def test_a_pane_without_identity_fails_loudly(self):
        with mock.patch.dict('os.environ', {}, clear=True), self.assertRaises(SystemExit):
            peer.own_identity()


class AwaitTest(unittest.TestCase):
    """read --wait against a fake clock: each sleep advances it, and settle
    returns the peer status the scenario scripts."""

    def run_await(self, captures, statuses, wait_ms=60000):
        now = [0.0]
        captures, statuses = iter(captures), iter(statuses)

        def sleep(s):
            now[0] += s

        with mock.patch.object(peer, 'settle', side_effect=lambda p, ms: next(statuses)), \
             mock.patch.object(peer, 'read_pane', side_effect=lambda p, n: next(captures)), \
             mock.patch.object(peer.time, 'monotonic', side_effect=lambda: now[0]), \
             mock.patch.object(peer.time, 'sleep', side_effect=sleep) as slept:
            return peer.await_result('w1:p2', 'w1:p1-7', wait_ms, 400), slept

    def test_a_stale_round_on_an_idle_pane_is_waited_past(self):
        # the exit-3 case: send returns before the peer leaves its last turn's
        # done state, so the first capture holds only the previous answer
        (body, note), slept = self.run_await(
            [RESULT.format(id='w1:p1-6'), RESULT.format(id='w1:p1-7')],
            ['done', 'done'])
        self.assertIsNone(note)
        self.assertTrue(body.endswith('END RESULT w1:p1-7'))
        self.assertEqual(slept.call_count, 1)

    def test_a_lapsed_wait_reports_no_answer(self):
        (body, note), _ = self.run_await(
            [RESULT.format(id='w1:p1-6')] * 3, ['done', 'done', 'working'],
            wait_ms=2 * peer.POLL_S * 1000)
        self.assertIsNone(body)
        self.assertEqual(note.status, peer.NO_ANSWER)
        self.assertIn('w1:p1-7', note)

    def test_a_blocked_peer_ends_the_wait(self):
        (body, note), slept = self.run_await([RESULT.format(id='w1:p1-6')], ['blocked'])
        self.assertIsNone(body)
        self.assertEqual(note.status, peer.NO_ANSWER)
        self.assertIn('blocked', note)
        slept.assert_not_called()

    def test_a_malformed_answer_ends_the_wait(self):
        bad = RESULT.format(id='w1:p1-7').replace('END RESULT w1:p1-7', 'END RESULT x')
        (body, note), _ = self.run_await([bad], ['done'])
        self.assertEqual(note.status, peer.MALFORMED)

    def test_settle_treats_a_timeout_as_still_working(self):
        # herdr 0.9.1 prints the error JSON on stderr, exit 1
        lapsed = mock.Mock(returncode=1, stdout='', stderr=(
            '{"error":{"code":"timeout","message":"timed out waiting for agent status"}}'))
        with mock.patch.object(peer.subprocess, 'run', return_value=lapsed):
            self.assertEqual(peer.settle('w1:p2', 1000), 'working')

    def test_settle_fails_loudly_on_any_other_error(self):
        gone = mock.Mock(returncode=1, stdout='', stderr='{"error":{"code":"not_found"}}')
        with mock.patch.object(peer.subprocess, 'run', return_value=gone), \
             self.assertRaises(SystemExit) as raised:
            peer.settle('w1:p2', 1000)
        self.assertEqual(raised.exception.code, 5)


if __name__ == '__main__':
    unittest.main()
