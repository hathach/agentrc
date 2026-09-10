import sys
import unittest
from pathlib import Path
from unittest import mock

import yaml

ROOT = Path(__file__).resolve().parents[1]
sys.path.insert(0, str(ROOT / 'skills' / 'herdr-peer' / 'scripts'))
SKILL = ROOT / 'skills' / 'herdr-peer' / 'SKILL.md'
import peer  # noqa: E402


def agent(pane, cwd, kind='codex'):
    return {'pane_id': pane, 'cwd': cwd, 'agent': kind, 'agent_status': 'idle'}


class FindPeersTest(unittest.TestCase):
    def _find(self, agents, cwd='/w', me='w1:p1'):
        with mock.patch.dict('os.environ', {'HERDR_ENV': '1'}), \
             mock.patch.object(peer, 'herdr', return_value={'result': {'agents': agents}}):
            return peer.find_peers(cwd=cwd, me=me)

    def test_excludes_self(self):
        found = self._find([agent('w1:p1', '/w', 'claude'), agent('w1:p2', '/w')])
        self.assertEqual([p['pane_id'] for p in found], ['w1:p2'])

    def test_excludes_other_worktrees(self):
        found = self._find([agent('w2:p1', '/other'), agent('w1:p2', '/w')])
        self.assertEqual([p['pane_id'] for p in found], ['w1:p2'])

    def test_reports_every_candidate_rather_than_choosing(self):
        found = self._find([agent('w1:p2', '/w'), agent('w1:p3', '/w')])
        self.assertEqual(len(found), 2)

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


class Frontmatter(unittest.TestCase):
    """A bare `word: ` inside an unquoted description is a YAML mapping, not
    prose, and the whole block stops parsing. Cheap to write, silent to hit."""

    def head(self):
        return SKILL.read_text().split('---')[1]

    def test_parses_as_yaml(self):
        yaml.safe_load(self.head())

    def test_declares_name_and_description(self):
        fm = yaml.safe_load(self.head())
        self.assertEqual(fm['name'], 'herdr-peer')
        self.assertTrue(fm['description'].strip())


if __name__ == '__main__':
    unittest.main()
