"""skills/hil/scripts/hil_report_core.py is the report document core the HIL harnesses share:
reading with explicit outcomes, `_hil` metadata beside the native fields, publication (JSON
first, Markdown rendered from it, each replaced atomically), immutable attempt records, the
`hil` skill's verdict rules and retry-sequence aggregation, and the table utilities.

Three deliberately different bodies (tests/data/hil_report_core/models.py) model tinyusb's
matrix report, tinyuf2's run report and a farm check. Golden checks (golden.json, regenerated
by gen_golden.py from tinyusb 2c992c002 and tinyuf2 ce6db76): tinyusb's render_report bytes
reproduced through the core's matrix, and hil_ci.verdict() outcomes kept through
apply_run_rules. Sequence aggregation is checked against expected results, not against
tinyusb's summarize(). Real files and a real killed process; no hardware."""
import os
import unittest

if os.name == 'nt':
    raise unittest.SkipTest('the report writer lock tests use fcntl')

import fcntl
import importlib.util
import json
import shutil
import signal
import subprocess
import sys
import tempfile
from pathlib import Path
from unittest import mock

FIXTURES = Path(__file__).resolve().parent / 'data' / 'hil_report_core'


def _load(name, path):
    spec = importlib.util.spec_from_file_location(name, path)
    mod = importlib.util.module_from_spec(spec)
    spec.loader.exec_module(mod)
    return mod


models = _load('hil_report_core_models', FIXTURES / 'models.py')
core = models.core
TINYUSB, TINYUF2, FARM = models.TINYUSB, models.TINYUF2, models.FARM
INPUTS = json.loads((FIXTURES / 'inputs.json').read_text(encoding='utf-8'))
GOLDEN = json.loads((FIXTURES / 'golden.json').read_text(encoding='utf-8'))
ROSTER = {'path': 'test/hil/tinyusb.json', 'digest': 'abc123'}


def usb_body(rows, caveat='', scope=''):
    return {'rows': rows, 'scope': scope, 'caveat': caveat}


def usb_row(board, **cells):
    return {'board': board, 'cells': cells, 'duration': '1s'}


def uf2_body(rows, path='/runs/run-1/hil_report.json', **extra):
    return {'pass': all(r.get('pass') for r in rows) and bool(rows), 'caveat': '', 'results': rows,
            'reportPath': path, **extra}


class Dir(unittest.TestCase):
    def setUp(self):
        td = tempfile.TemporaryDirectory()
        self.addCleanup(td.cleanup)
        self.d = Path(td.name) / 'report'

    def meta(self, schema, attempt, termination='completed', selection=('b1',), **kw):
        return core.make_meta(schema, attempt, termination=termination, invocation=['hil_test.py', 'cfg.json'],
                              selection=list(selection), host='ci.lan', roster=kw.pop('roster', ROSTER), **kw)

    def run_attempt(self, schema, body, accumulate=False, termination='completed', selection=('b1',),
                    attempt_id=None, **kw):
        a = core.start_attempt(self.d, schema, accumulate, attempt_id)
        core.publish(self.d, schema, body, self.meta(schema, a, termination, selection, **kw))
        return a

    def files(self, schema=TINYUSB):
        j, m, _ = core.paths(self.d, schema)
        return (j.read_bytes() if j.exists() else None, m.read_bytes() if m.exists() else None)

    def strays(self):
        return sorted(p.name for p in self.d.rglob('*.tmp'))


# --- tables -------------------------------------------------------------------------------------
class Tables(unittest.TestCase):
    def test_marks_are_two_columns_wide(self):
        self.assertEqual(core.display_width('✅ ok'), 5)
        self.assertEqual(core.display_width('abc'), 3)
        self.assertEqual(core.pad('✅', 4), '✅  ')
        self.assertEqual(core.pad('✅', 5, center=True), ' ✅  ')

    def test_tinyusb_render_report_is_reproduced_byte_for_byte(self):
        """golden.json holds tinyusb 2c992c002's render_report() of each inputs.json document."""
        for name, doc in INPUTS['tinyusb_docs'].items():
            with self.subTest(name):
                self.assertEqual(models.tinyusb_render_report(doc), GOLDEN['tinyusb_render_report'][name])

    def test_a_row_of_the_wrong_length_or_a_non_string_cell_is_refused(self):
        with self.assertRaises(ValueError):
            core.table(['a', 'b'], [['x']])
        with self.assertRaises(TypeError):
            core.matrix('Board', ['t'], [('b', {'t': None})], {})
        with self.assertRaises(ValueError):
            core.matrix('Board', ['t'], [], {}, headers=['t', 'u'])

    def test_matrix_columns_follow_the_given_order_and_marks(self):
        md = core.matrix('Board', ['z', 'a'], [('b1', {'a': 'ok', 'z': 'x'})], {'ok': '✅'})
        self.assertEqual(md.splitlines()[0], '| Board | z | a  |')
        self.assertEqual(md.splitlines()[2], '| b1    | x | ✅ |')


# --- reading ------------------------------------------------------------------------------------
class Reading(Dir):
    def read(self, data, schema=TINYUSB):
        self.d.mkdir(parents=True, exist_ok=True)
        p = self.d / 'r.json'
        p.write_bytes(data if isinstance(data, bytes) else json.dumps(data).encode())
        return core.read_document(p, schema)

    def published(self, schema=TINYUSB, body=None):
        a = core.Attempt('a1', None, None, None, None)
        return json.loads(core.serialise(schema, body or usb_body([usb_row('b1', t='pass')]), self.meta(schema, a)))

    def test_missing(self):
        self.assertEqual(core.read_document(self.d / 'nope.json', TINYUSB).outcome, 'missing')

    def test_corrupt_shapes(self):
        meta_bad = self.published()
        meta_bad['_hil']['termination'] = 'exploded'
        extra = self.published()
        extra['_hil']['surprise'] = 1
        for name, data in (('empty file', b''), ('not json', b'{"rows": ['), ('bad utf-8', b'\xff\xfe'),
                           ('array', [1]), ('meta not object', {'_hil': 3, **usb_body([])}),
                           ('bad termination', meta_bad), ('unknown meta field', extra),
                           ('body fails check', {'rows': 1, 'scope': '', 'caveat': ''})):
            with self.subTest(name):
                r = self.read(data)
                self.assertEqual(r.outcome, 'corrupt')
                self.assertIsNone(r.body)

    def test_an_explicitly_unknown_version_or_schema_is_refused_never_read_as_legacy(self):
        future = self.published()
        future['_hil']['version'] = 2
        other_body = self.published()
        other_body['_hil']['body_version'] = 9
        for name, data, schema in (('future core version', future, TINYUSB),
                                   ('future body version', other_body, TINYUSB),
                                   ('another body schema', self.published(), TINYUF2),
                                   ('legacy, no legacy reader', usb_body([]), FARM)):
            with self.subTest(name):
                r = self.read(data, schema)
                self.assertEqual(r.outcome, 'unsupported')
                self.assertIsNone(r.body)

    def test_valid_empty_and_valid(self):
        self.assertEqual(self.read(self.published(body=usb_body([]))).outcome, 'empty')
        r = self.read(self.published())
        self.assertEqual(r.outcome, 'valid')
        self.assertFalse(r.legacy)
        self.assertEqual(r.meta['attempt_id'], 'a1')
        self.assertNotIn('_hil', r.body)

    def test_a_legacy_report_is_read_only_by_the_selected_adapter(self):
        r = self.read(usb_body([usb_row('b1', t='pass')]))
        self.assertEqual((r.outcome, r.legacy), ('valid', True))
        # tinyuf2's adapter reads its own legacy reports; a tinyusb one fails its check
        self.assertEqual(self.read(usb_body([]), TINYUF2).outcome, 'corrupt')

    def test_project_extension_fields_survive_a_round_trip(self):
        row = {'board': 'metro_m7_1011', 'ran': True, 'pass': True, 'jlinkRetried': [1],
               'snapshot': {'preserved': {'nvs': '0' * 32}}, 'steps': [], 'cleanup': {'pass': True}}
        body = uf2_body([row], markErrors=['x'], survivors=[])
        self.run_attempt(TINYUF2, body, selection=['metro_m7_1011'])
        r = core.read_document(core.paths(self.d, TINYUF2)[0], TINYUF2)
        self.assertEqual(r.body, body)
        # hil_test.py:3107's recovery read keeps working on the published file
        self.assertEqual(json.loads(core.paths(self.d, TINYUF2)[0].read_text())['results'], [row])


# --- metadata -----------------------------------------------------------------------------------
class Metadata(Dir):
    def test_meta_sits_beside_the_native_fields(self):
        self.run_attempt(TINYUSB, usb_body([usb_row('b1', t='pass')], scope='-b b1'), attempt_id='a1')
        doc = json.loads(core.paths(self.d, TINYUSB)[0].read_text())
        self.assertEqual(set(doc), {'rows', 'scope', 'caveat', '_hil'})
        self.assertEqual(doc['scope'], '-b b1')
        self.assertEqual(doc['_hil'], {
            'version': 1, 'body_schema': 'tinyusb-hil-report', 'body_version': 1, 'attempt_id': 'a1',
            'previous_attempt': None, 'termination': 'completed', 'invocation': ['hil_test.py', 'cfg.json'],
            'selection': ['b1'], 'host': 'ci.lan', 'roster': ROSTER})

    def test_build_provenance_only_when_given(self):
        a = core.Attempt('a1', None, None, None, None)
        self.assertNotIn('build', self.meta(TINYUSB, a))
        self.assertEqual(self.meta(TINYUSB, a, build={'commit': 'c0ffee'})['build'], {'commit': 'c0ffee'})

    def test_bad_metadata_is_refused_when_made(self):
        a = core.Attempt('a1', None, None, None, None)
        for kw in ({'termination': 'done'}, {'selection': [1]}, {'roster': 'x'}):
            with self.subTest(kw), self.assertRaises((ValueError, TypeError)):
                self.meta(TINYUSB, a, **{'termination': 'completed', **kw})
        with self.assertRaises(ValueError):
            self.meta(TINYUSB, core.Attempt('../x', None, None, None, None))

    def test_serialise_validates_without_writing(self):
        a = core.Attempt('a1', None, None, None, None)
        text = core.serialise(FARM, {'rows': [], 'counts': {}}, self.meta(FARM, a))
        self.assertEqual(json.loads(text)['_hil']['body_schema'], 'farm-check')
        self.assertFalse(self.d.exists())
        with self.assertRaises(core.ReportError) as cm:
            core.serialise(FARM, {'rows': [], 'counts': {}, '_hil': {}}, self.meta(FARM, a))
        self.assertEqual(cm.exception.stage, 'validate')


# --- publication --------------------------------------------------------------------------------
class Publication(Dir):
    def setUp(self):
        super().setUp()
        self.first = self.run_attempt(TINYUSB, usb_body([usb_row('b1', t='pass')]), attempt_id='a1')
        self.before = self.files()

    def next_meta(self):
        return self.meta(TINYUSB, core.start_attempt(self.d, TINYUSB, False, 'a2'))

    def test_the_markdown_is_a_rendering_of_the_serialised_json(self):
        j, m = self.files()
        self.assertEqual(m.decode(), core.render_markdown(TINYUSB, json.loads(j)))
        # the native view, unchanged by the metadata beside it
        self.assertEqual(m.decode(), models.tinyusb_render_report(usb_body([usb_row('b1', t='pass')])) + '\n')

    def test_a_render_failure_leaves_the_prior_artifacts(self):
        broken = TINYUSB._replace(render=lambda b: 1 / 0)
        with self.assertRaises(core.ReportError) as cm:
            core.publish(self.d, broken, usb_body([]), self.next_meta())
        self.assertEqual(cm.exception.stage, 'render')
        self.assertEqual(self.files(), self.before)
        self.assertEqual(self.strays(), [])

    def test_a_serialise_failure_leaves_the_prior_artifacts(self):
        meta = self.next_meta()
        for body in (usb_body([{'board': 'b1', 'cells': {'t': {1, 2}}}]), usb_body([{'x': float('nan')}])):
            with self.subTest(body), self.assertRaises(core.ReportError) as cm:
                core.publish(self.d, TINYUSB, body, meta)
            self.assertEqual(cm.exception.stage, 'serialise')
        self.assertEqual(self.files(), self.before)

    def test_a_json_replace_failure_is_explicit_and_changes_nothing(self):
        meta = self.next_meta()
        real = os.replace

        def fail_json(src, dst, *a, **kw):
            if str(dst).endswith('.json'):
                raise PermissionError(13, 'denied')
            return real(src, dst, *a, **kw)

        with mock.patch.object(core.os, 'replace', fail_json), self.assertRaises(core.ReportError) as cm:
            core.publish(self.d, TINYUSB, usb_body([usb_row('b2', t='fail')]), meta)
        self.assertEqual((cm.exception.stage, cm.exception.published), ('replace', ()))
        self.assertEqual(self.files(), self.before)
        self.assertEqual(self.strays(), [])

    def test_a_markdown_replace_failure_names_the_published_json_and_leaves_no_stale_view(self):
        meta = self.next_meta()
        real = os.replace

        def fail_md(src, dst, *a, **kw):
            if str(dst).endswith('.md'):
                raise OSError(28, 'no space')
            return real(src, dst, *a, **kw)

        with mock.patch.object(core.os, 'replace', fail_md), self.assertRaises(core.ReportError) as cm:
            core.publish(self.d, TINYUSB, usb_body([usb_row('b2', t='fail')]), meta)
        self.assertEqual(cm.exception.stage, 'markdown')
        self.assertEqual(cm.exception.published, (str(core.paths(self.d, TINYUSB)[0]),))
        self.assertIn('absent', str(cm.exception))
        j, m = self.files()
        self.assertEqual(json.loads(j)['_hil']['attempt_id'], 'a2')
        self.assertIsNone(m)
        self.assertEqual(core.prepare_pair(self.d, TINYUSB).outcome, 'valid')
        self.assertEqual(self.files()[1].decode(), core.render_markdown(TINYUSB, json.loads(j)))

    def test_an_unwritable_dir_is_a_report_error(self):
        a = core.Attempt('a9', None, None, None, None)
        with self.assertRaises(core.ReportError):
            core.publish(Path('/proc/nonexistent/nope'), TINYUSB, usb_body([]), self.meta(TINYUSB, a))

    def test_one_writer_per_report_dir(self):
        meta = self.next_meta()
        lock = os.open(self.d / '.hil_report.lock', os.O_RDWR)
        self.addCleanup(os.close, lock)
        fcntl.flock(lock, fcntl.LOCK_EX)
        for call in (lambda: core.publish(self.d, TINYUSB, usb_body([]), meta),
                     lambda: core.start_attempt(self.d, TINYUSB, True),
                     lambda: core.prepare_pair(self.d, TINYUSB)):
            with self.assertRaises(core.ReportError) as cm:
                call()
            self.assertEqual(cm.exception.stage, 'writer')
        self.assertEqual(self.files(), self.before)

    def test_an_archived_attempt_is_sealed(self):
        """a1 fails, a2 accumulates a fail, a3 starts: a1's old metadata cannot publish a pass."""
        meta1 = self.meta(TINYUSB, self.first)
        core.publish(self.d, TINYUSB, usb_body([usb_row('b1', t='fail')]), meta1)
        self.run_attempt(TINYUSB, usb_body([usb_row('b1', t='fail')]), accumulate=True, attempt_id='a2')
        core.start_attempt(self.d, TINYUSB, True, 'a3')
        before = self.files()
        with self.assertRaises(core.ReportError) as cm:
            core.publish(self.d, TINYUSB, usb_body([usb_row('b1', t='pass')]), meta1)
        self.assertEqual(cm.exception.stage, 'sealed')
        self.assertEqual(self.files(), before)
        self.assertEqual([c.attempt_id for c in core.read_chain(self.d, TINYUSB)], ['a1', 'a2'])

    def test_an_attempt_cannot_update_once_its_successor_started(self):
        meta1 = self.meta(TINYUSB, self.first)
        a2 = core.start_attempt(self.d, TINYUSB, True, 'a2')
        with self.assertRaises(core.ReportError) as cm:
            core.publish(self.d, TINYUSB, usb_body([usb_row('b1', t='fail')]), meta1)
        self.assertEqual(cm.exception.stage, 'sealed')
        self.assertEqual(self.files(), self.before)
        core.publish(self.d, TINYUSB, usb_body([usb_row('b1', t='pass')]), self.meta(TINYUSB, a2))
        self.assertEqual([c.attempt_id for c in core.read_chain(self.d, TINYUSB)], ['a1', 'a2'])

    def test_a_predecessor_must_be_the_archived_current_report(self):
        core.start_attempt(self.d, TINYUSB, True, 'a2')
        for previous in ('a0', 'a2'):
            with self.subTest(previous), self.assertRaises(core.ReportError) as cm:
                core.publish(self.d, TINYUSB, usb_body([]),
                             self.meta(TINYUSB, core.Attempt('a3', previous, None, None, None)))
            self.assertEqual(cm.exception.stage, 'predecessor')
        self.assertEqual(self.files(), self.before)

    def test_a_render_failure_while_archiving_is_a_report_error(self):
        broken = TINYUSB._replace(render=lambda b: (_ for _ in ()).throw(RuntimeError('boom')))
        with self.assertRaises(core.ReportError) as cm:
            core.start_attempt(self.d, broken, True, 'a2')
        self.assertEqual((cm.exception.stage, cm.exception.published), ('render', ()))
        self.assertEqual(self.files(), self.before)
        self.assertFalse(core.paths(self.d, TINYUSB)[2].exists())

    def test_a_rejected_start_leaves_the_current_attempt_publishable(self):
        with self.assertRaises(core.ReportError) as cm:
            core.start_attempt(self.d, TINYUSB, True, 'a1')
        self.assertEqual(cm.exception.stage, 'attempt')
        self.assertFalse(core.paths(self.d, TINYUSB)[2].exists())
        core.publish(self.d, TINYUSB, usb_body([usb_row('b1', t='fail')]), self.meta(TINYUSB, self.first))
        self.assertEqual(json.loads(self.files()[0])['rows'][0]['cells'], {'t': 'fail'})

    def surrogate_report(self):
        """A lone surrogate survives JSON (escaped) but cannot be encoded as UTF-8 Markdown."""
        doc = json.loads(self.before[0])
        doc['rows'][0]['cells']['t'] = '\udcff'
        text = json.dumps(doc)
        self.assertIn('\\udcff', text)
        core.paths(self.d, TINYUSB)[0].write_text(text)
        return self.files()

    def test_an_unencodable_markdown_is_a_render_error_and_changes_nothing(self):
        with self.assertRaises(core.ReportError) as cm:
            core.publish(self.d, TINYUSB, usb_body([usb_row('b1', t='\udcff')]), self.meta(TINYUSB, self.first))
        self.assertEqual((cm.exception.stage, cm.exception.published), ('render', ()))
        self.assertEqual(self.files(), self.before)
        self.assertEqual(self.strays(), [])
        prior = self.surrogate_report()
        with self.assertRaises(core.ReportError) as cm:
            core.start_attempt(self.d, TINYUSB, True, 'a2')
        self.assertEqual((cm.exception.stage, cm.exception.published), ('render', ()))
        self.assertEqual(self.files(), prior)
        self.assertFalse(core.paths(self.d, TINYUSB)[2].exists())

    def test_prepare_pair_shows_an_unencodable_view_as_a_notice(self):
        self.surrogate_report()
        self.assertEqual(core.prepare_pair(self.d, TINYUSB).outcome, 'corrupt')
        md = self.files()[1].decode()
        self.assertTrue(md.startswith(core.NOTICE))
        self.assertIn('UnicodeEncodeError', md)

    def test_another_attempts_report_is_never_replaced_unarchived(self):
        a = core.Attempt('a3', None, None, None, None)
        with self.assertRaises(core.ReportError) as cm:
            core.publish(self.d, TINYUSB, usb_body([]), self.meta(TINYUSB, a))
        self.assertEqual(cm.exception.stage, 'predecessor')
        self.assertEqual(self.files(), self.before)


INTERRUPT = r'''
import importlib.util, os, signal, sys
spec = importlib.util.spec_from_file_location('models', sys.argv[1])
models = importlib.util.module_from_spec(spec)
spec.loader.exec_module(models)
core, d = models.core, sys.argv[2]
real = os.replace
def replace(src, dst):
    real(src, dst)
    if str(dst).endswith('.json'):
        os.kill(os.getpid(), signal.SIGKILL)   # between the two replacements
os.replace = replace
a = core.start_attempt(d, models.TINYUSB, True, 'a2')
meta = core.make_meta(models.TINYUSB, a, termination='completed', invocation=['x'], selection=['b1'],
                      host='ci.lan', roster={'path': 'r'})
core.publish(d, models.TINYUSB, {'rows': [{'board': 'b1', 'cells': {'t': 'fail'}, 'duration': '2s'}],
                                 'scope': '', 'caveat': ''}, meta)
'''


class InterruptedPublication(Dir):
    """A real process killed after the JSON replacement and before the Markdown's: the pair
    disagrees on disk, and prepare_pair -- what copy-back and CI upload preparation call
    before exposing it -- regenerates the view from the JSON."""

    def test_copy_back_and_upload_preparation_regenerate_the_view(self):
        self.run_attempt(TINYUSB, usb_body([usb_row('b1', t='pass')]), selection=['b1'], roster={'path': 'r'},
                         attempt_id='a1')
        old_md = self.files()[1]
        p = subprocess.run([sys.executable, '-I', '-c', INTERRUPT, str(FIXTURES / 'models.py'), str(self.d)],
                           capture_output=True, text=True, timeout=60)
        self.assertEqual(p.returncode, -signal.SIGKILL, p.stderr)
        j, m = self.files()
        self.assertEqual(json.loads(j)['_hil']['attempt_id'], 'a2')
        self.assertEqual(m, old_md)   # the stale view the interruption left
        fresh = core.render_markdown(TINYUSB, json.loads(j)).encode()
        # copy-back: the pair as transferred into the caller's dir
        dest = self.d.parent / 'copied'
        shutil.copytree(self.d, dest)
        for where in (dest, self.d):   # then CI upload preparation on the rig's own dir
            with self.subTest(str(where)):
                self.assertEqual(core.prepare_pair(where, TINYUSB).outcome, 'valid')
                self.assertEqual((where / 'hil_report.md').read_bytes(), fresh)


class PreparePair(Dir):
    def test_an_unreadable_json_gets_a_notice_never_the_stale_view(self):
        self.d.mkdir(parents=True)
        (self.d / 'hil_report.md').write_text('| Board | t |\n| b1 | ✅ |\n', encoding='utf-8')
        (self.d / 'hil_report.json').write_text('{"rows": [')
        self.assertEqual(core.prepare_pair(self.d, TINYUSB).outcome, 'corrupt')
        md = (self.d / 'hil_report.md').read_text(encoding='utf-8')
        self.assertTrue(md.startswith(core.NOTICE))
        self.assertIn('_Legacy text:', md)
        self.assertIn('| b1 | ✅ |', md)
        core.prepare_pair(self.d, TINYUSB)
        self.assertEqual((self.d / 'hil_report.md').read_text(encoding='utf-8'), md)   # idempotent

    def test_a_missing_or_unsupported_json_is_named(self):
        self.d.mkdir(parents=True)
        self.assertEqual(core.prepare_pair(self.d, TINYUSB).outcome, 'missing')
        self.assertIn('hil_report.json is missing', (self.d / 'hil_report.md').read_text())
        (self.d / 'farm_check.json').write_text(json.dumps(usb_body([])))
        self.assertEqual(core.prepare_pair(self.d, FARM).outcome, 'unsupported')

    def test_a_render_failure_is_a_notice(self):
        self.run_attempt(TINYUSB, usb_body([]))
        broken = TINYUSB._replace(render=lambda b: None)
        self.assertEqual(core.prepare_pair(self.d, broken).outcome, 'corrupt')
        self.assertIn('render failed', (self.d / 'hil_report.md').read_text())


# --- attempts -----------------------------------------------------------------------------------
class Attempts(Dir):
    def test_a_fresh_attempt_archives_the_previous_one_and_does_not_link_it(self):
        self.run_attempt(TINYUSB, usb_body([usb_row('b1', t='fail')]), attempt_id='a1')
        old = self.files()[0]
        a = self.run_attempt(TINYUSB, usb_body([usb_row('b1', t='pass')]), attempt_id='a2')
        self.assertEqual((a.previous_attempt, a.archived, a.prior), (None, 'a1', None))
        root = core.paths(self.d, TINYUSB)[2]
        self.assertEqual((root / 'a1' / 'hil_report.json').read_bytes(), old)
        self.assertEqual([c.attempt_id for c in core.read_chain(self.d, TINYUSB)], ['a2'])

    def test_an_accumulated_attempt_links_its_predecessor(self):
        self.run_attempt(TINYUSB, usb_body([usb_row('b1', t='fail')]), attempt_id='a1')
        a = core.start_attempt(self.d, TINYUSB, True, 'a2')
        self.assertEqual((a.previous_attempt, a.prior.outcome), ('a1', 'valid'))
        self.assertEqual(a.prior.body['rows'][0]['cells'], {'t': 'fail'})
        core.publish(self.d, TINYUSB, usb_body([usb_row('b1', t='pass')]), self.meta(TINYUSB, a))
        chain = core.read_chain(self.d, TINYUSB)
        self.assertEqual([(c.attempt_id, c.previous_attempt, c.outcome) for c in chain],
                         [('a1', None, 'valid'), ('a2', 'a1', 'valid')])

    def test_repeated_writes_inside_one_attempt_are_not_retries(self):
        a = core.start_attempt(self.d, TINYUSB, False, 'a1')
        for cell in ('fail', 'pass'):
            core.publish(self.d, TINYUSB, usb_body([usb_row('b1', t=cell)]), self.meta(TINYUSB, a))
        self.assertFalse(core.paths(self.d, TINYUSB)[2].exists())
        self.assertEqual([c.attempt_id for c in core.read_chain(self.d, TINYUSB)], ['a1'])

    def test_an_attempt_record_is_never_rewritten_and_an_id_never_reused(self):
        self.run_attempt(TINYUSB, usb_body([usb_row('b1', t='fail')]), attempt_id='a1')
        self.run_attempt(TINYUSB, usb_body([]), attempt_id='a2')
        with self.assertRaises(core.ReportError) as cm:
            core.start_attempt(self.d, TINYUSB, False, 'a1')
        self.assertEqual(cm.exception.stage, 'attempt')
        with self.assertRaises(core.ReportError):
            core.start_attempt(self.d, TINYUSB, False, 'a2')
        # a different report claiming an archived id is refused, the record kept
        a1 = (core.paths(self.d, TINYUSB)[2] / 'a1' / 'hil_report.json').read_bytes()
        doc = json.loads(self.files()[0])
        doc['_hil']['attempt_id'] = 'a1'
        core.paths(self.d, TINYUSB)[0].write_text(json.dumps(doc))
        with self.assertRaises(core.ReportError) as cm:
            core.start_attempt(self.d, TINYUSB, True, 'a3')
        self.assertEqual(cm.exception.stage, 'archive')
        self.assertEqual((core.paths(self.d, TINYUSB)[2] / 'a1' / 'hil_report.json').read_bytes(), a1)

    def test_an_unsupported_report_is_neither_replaced_nor_accumulated_onto(self):
        self.d.mkdir(parents=True)
        future = {**usb_body([]), '_hil': {'version': 99}}
        core.paths(self.d, TINYUSB)[0].write_text(json.dumps(future))
        for accumulate in (False, True):
            with self.assertRaises(core.ReportError) as cm:
                core.start_attempt(self.d, TINYUSB, accumulate)
            self.assertEqual(cm.exception.stage, 'predecessor')
        self.assertEqual(json.loads(core.paths(self.d, TINYUSB)[0].read_text()), future)

    def test_a_legacy_report_is_archived_by_its_digest(self):
        self.d.mkdir(parents=True)
        core.paths(self.d, TINYUSB)[0].write_text(json.dumps(usb_body([usb_row('b1', t='pass')])))
        a = core.start_attempt(self.d, TINYUSB, True, 'a2')
        self.assertTrue(a.previous_attempt.startswith('legacy-'))
        core.publish(self.d, TINYUSB, usb_body([usb_row('b1', t='pass')]), self.meta(TINYUSB, a))
        chain = core.read_chain(self.d, TINYUSB)
        self.assertEqual([(c.attempt_id, c.outcome) for c in chain], [(a.previous_attempt, 'valid'), ('a2', 'valid')])

    def test_retained_markdown_without_json_becomes_labelled_legacy_text(self):
        """tinyusb hil_report.py:375: the table survives only as text. It is archived, carried
        into the recovery document labelled, and never read as rows or a verdict."""
        self.d.mkdir(parents=True)
        prior = '| Board | t |\n| b9 | ✅ |\n'
        core.paths(self.d, TINYUSB)[1].write_text(prior, encoding='utf-8')
        a = core.start_attempt(self.d, TINYUSB, True, 'a2')
        self.assertEqual((a.legacy_text, a.prior.outcome), (prior, 'missing'))
        self.assertTrue(a.previous_attempt.startswith('legacy-text-'))
        stuck = usb_body([usb_row('b1', **{'pool-timeout': 'fail'})], caveat='**HIL run aborted.**\n')
        core.publish(self.d, TINYUSB, stuck, self.meta(TINYUSB, a, 'timeout', legacy_text=a.legacy_text))
        j, m = self.files()
        self.assertIn('_Legacy text:', m.decode())
        self.assertIn('| b9 | ✅ |', m.decode())
        self.assertEqual(m.decode(), core.render_markdown(TINYUSB, json.loads(j)))
        self.assertEqual([r['board'] for r in json.loads(j)['rows']], ['b1'])
        chain = core.read_chain(self.d, TINYUSB)
        self.assertEqual(chain[0].outcome, 'missing')   # the text is no attempt record

    def test_a_corrupt_report_is_archived_and_accumulation_links_it(self):
        self.d.mkdir(parents=True)
        core.paths(self.d, TINYUSB)[0].write_text('{"rows": [')
        core.paths(self.d, TINYUSB)[1].write_text('| old |\n')
        a = core.start_attempt(self.d, TINYUSB, True, 'a2')
        self.assertEqual((a.prior.outcome, a.legacy_text), ('corrupt', '| old |\n'))
        self.assertTrue(a.previous_attempt.startswith('corrupt-'))

    def test_inventory_lists_the_pair_and_every_attempt(self):
        self.run_attempt(TINYUSB, usb_body([usb_row('b1', t='fail')]), attempt_id='a1')
        self.run_attempt(TINYUSB, usb_body([usb_row('b1', t='pass')]), accumulate=True, attempt_id='a2')
        inv = core.inventory(self.d, TINYUSB)
        self.assertEqual(set(inv['current']), {'hil_report.json', 'hil_report.md'})
        self.assertEqual([a['attempt_id'] for a in inv['attempts']], ['a1'])
        self.assertEqual(set(inv['attempts'][0]['files']), {'hil_report.json', 'hil_report.md'})
        self.assertEqual(len(inv['attempts'][0]['files']['hil_report.json']['sha256']), 64)


# --- verdicts -----------------------------------------------------------------------------------
def row(board, verdict, detail='', evidence=()):
    return {'board': board, 'verdict': verdict, 'detail': detail, 'evidence': list(evidence)}


class RunRules(unittest.TestCase):
    def rules(self, requested, rows, termination='completed', caveat=''):
        return core.apply_run_rules(TINYUSB, requested, rows, termination, caveat)

    def verdicts(self, run):
        return {r['board']: r['verdict'] for r in run['rows']}

    def test_the_vocabulary_is_validated(self):
        for rows in ([row('b1', 'flaky')], [dict(row('b1', 'pass'), extra=1)], [row('b9', 'pass')],
                     [row('b1', 'pass'), row('b1', 'fail')], [row('b1', 'pass', evidence=[1])]):
            with self.subTest(rows), self.assertRaises(ValueError):
                self.rules(['b1'], rows)
        with self.assertRaises(ValueError):
            self.rules(['b1'], [], termination='done')

    def test_blocker_precedence_and_a_missing_row(self):
        run = self.rules(['b1', 'b2', 'b3'], [row('b1', 'pass'), row('b2', 'fail')])
        self.assertEqual(self.verdicts(run), {'b1': 'pass', 'b2': 'fail', 'b3': 'blocker'})
        self.assertEqual(run['verdict'], 'blocker')
        self.assertEqual(self.rules(['b1', 'b2'], [row('b1', 'pass'), row('b2', 'not-run')])['verdict'], 'not-run')
        self.assertEqual(self.rules(['b1', 'b2'], [row('b1', 'fail'), row('b2', 'not-run')])['verdict'], 'fail')

    def test_an_abort_or_timeout_makes_every_requested_board_a_blocker(self):
        for t in ('aborted', 'timeout'):
            run = self.rules(['b1', 'b2'], [row('b1', 'pass'), row('b2', 'not-run')], t, '**HIL run aborted.**\n')
            self.assertEqual(set(self.verdicts(run).values()), {'blocker'})

    def test_no_selection_leaves_boards_not_run_unless_blocked(self):
        run = self.rules(['b1', 'b2', 'b3'], [row('b1', 'pass'), row('b2', 'blocker')], 'no-boards',
                         '**HIL run selected no boards.**\n')
        self.assertEqual(self.verdicts(run), {'b1': 'not-run', 'b2': 'blocker', 'b3': 'not-run'})
        self.assertEqual(self.verdicts(self.rules(['b1'], [], 'no-boards')), {'b1': 'not-run'})
        self.assertEqual(self.rules([], [], 'no-boards')['verdict'], 'not-run')

    def test_a_non_abort_caveat_denies_every_pass(self):
        run = self.rules(['b1', 'b2'], [row('b1', 'pass'), row('b2', 'fail')], caveat='partial roster\n')
        self.assertEqual(self.verdicts(run), {'b1': 'blocker', 'b2': 'fail'})

    def test_empty_coverage_never_passes(self):
        self.assertEqual(self.rules([], [])['verdict'], 'not-run')

    def test_tinyuf2_ci_verdicts_are_kept(self):
        """golden.json: tinyuf2 ce6db76 hil_ci.verdict() of each inputs.json case."""
        for case in INPUTS['tinyuf2_verdicts']:
            with self.subTest(case['name']):
                rows = models.tinyuf2_rows(case['code'], case['report'], ['B'])
                run = core.apply_run_rules(TINYUF2, ['B'], rows, 'completed', (case['report'] or {}).get('caveat', ''))
                self.assertEqual([run['rows'][0]['verdict'], run['rows'][0]['detail']],
                                 GOLDEN['tinyuf2_verdict'][case['name']])


class Sequences(Dir):
    """Expected results per fixture sequence, built through real files and read_chain."""

    def attempt(self, cells, accumulate, aid, termination='completed', caveat='', boards=('b1',), **kw):
        rows = [usb_row(b, **c) for b, c in zip(boards, cells)]
        self.run_attempt(TINYUSB, usb_body(rows, caveat), accumulate, termination, boards, aid, **kw)

    def verdict(self, requested=('b1',), retry=(), final=None):
        chain = []
        for c in core.read_chain(self.d, TINYUSB):
            run = models.tinyusb_run(c.reading, list(requested)) if c.outcome in core.READABLE else None
            chain.append(c._replace(run=run, retry=c.attempt_id in retry,
                                    final=frozenset((final or {}).get(c.attempt_id, ()))))
        return core.sequence_verdict(TINYUSB, list(requested), chain)

    def board(self, result, b='b1'):
        r = next(x for x in result['sequence']['rows'] if x['board'] == b)
        return r['verdict'], r['flaky']

    def test_an_eligible_fail_then_pass_is_a_flaky_pass_citing_both(self):
        self.attempt([{'t': 'fail'}], False, 'a1')
        self.attempt([{'t': 'pass'}], True, 'a2')
        res = self.verdict(retry={'a2'})
        self.assertEqual(self.board(res), ('pass', True))
        r = res['sequence']['rows'][0]
        self.assertEqual(r['attempts'], ['a1', 'a2'])
        self.assertTrue(any(e.startswith('a1:') for e in r['evidence']))
        self.assertTrue(any(e.startswith('a2:') for e in r['evidence']))
        # the current view is the last attempt alone, kept apart
        self.assertEqual(res['current']['verdict'], 'pass')
        self.assertEqual(res['sequence']['attempts'], ['a1', 'a2'])

    def test_accumulation_alone_is_not_a_retry(self):
        self.attempt([{'t': 'fail'}], False, 'a1')
        self.attempt([{'t': 'pass'}], True, 'a2')
        res = self.verdict()
        self.assertEqual(self.board(res), ('fail', False))
        self.assertEqual(res['current']['verdict'], 'pass')   # distinguishable from the sequence

    def test_a_locked_peer_makes_the_retry_ineligible(self):
        """The project withholds eligibility when its predecessor had a locked board."""
        self.attempt([{'t': 'fail'}, {'board-locked': 'held'}], False, 'a1', boards=('b1', 'b2'))
        self.attempt([{'t': 'pass'}, {'t': 'pass'}], True, 'a2', boards=('b1', 'b2'))
        res = self.verdict(('b1', 'b2'))
        self.assertEqual(self.board(res, 'b1'), ('fail', False))
        self.assertEqual(self.board(res, 'b2'), ('pass', False))

    def test_an_earlier_abort_survives_retry_aggregation(self):
        self.attempt([{'t': 'pass'}], False, 'a1', 'aborted', '**HIL run aborted: worker raised.**\n')
        self.attempt([{'t': 'pass'}], True, 'a2')
        res = self.verdict(retry={'a2'})
        self.assertEqual(self.board(res), ('blocker', False))
        self.assertEqual(res['current']['verdict'], 'pass')

    def test_a_non_abort_caveat_before_the_retry_makes_it_ineligible(self):
        self.attempt([{'t': 'fail'}, {'t': 'pass'}], False, 'a1', caveat='partial\n', boards=('b1', 'b2'))
        self.attempt([{'t': 'pass'}, {'t': 'pass'}], True, 'a2', boards=('b1', 'b2'))
        res = self.verdict(('b1', 'b2'), retry={'a2'})
        self.assertEqual(self.board(res, 'b1'), ('fail', False))
        self.assertEqual(self.board(res, 'b2'), ('blocker', False))   # its caveat-denied pass stands

    def test_a_final_failure_is_not_retried_into_a_pass(self):
        self.attempt([{'t': 'fail'}], False, 'a1')
        self.attempt([{'t': 'pass'}], True, 'a2')
        self.assertEqual(self.board(self.verdict(retry={'a2'}, final={'a1': {'b1'}})), ('fail', False))

    def test_an_unretryable_fail_is_not_laundered_by_a_later_fail(self):
        for name, caveat, final in (('caveat', 'partial roster\n', None), ('final', '', {'a1': {'b1'}})):
            with self.subTest(name):
                shutil.rmtree(self.d, ignore_errors=True)
                self.attempt([{'t': 'fail'}], False, 'a1', caveat=caveat)
                self.attempt([{'t': 'fail'}], True, 'a2')
                self.attempt([{'t': 'pass'}], True, 'a3')
                res = self.verdict(retry={'a3'}, final=final)
                self.assertEqual(self.board(res), ('fail', False))
                r = res['sequence']['rows'][0]
                self.assertTrue(any(e.startswith('a1:') for e in r['evidence']), r)
                self.assertTrue(any(e.startswith('a2:') for e in r['evidence']), r)

    def test_a_fail_is_retried_once(self):
        self.attempt([{'t': 'fail'}], False, 'a1')
        self.attempt([{'t': 'fail'}], True, 'a2')
        self.attempt([{'t': 'pass'}], True, 'a3')
        self.assertEqual(self.board(self.verdict(retry={'a2', 'a3'})), ('fail', False))

    def test_a_missing_predecessor_yields_no_pass(self):
        self.attempt([{'t': 'pass'}], False, 'a1')
        self.attempt([{'t': 'pass'}], True, 'a2')
        shutil.rmtree(core.paths(self.d, TINYUSB)[2] / 'a1')
        res = self.verdict(retry={'a2'})
        self.assertEqual(self.board(res), ('blocker', False))
        self.assertEqual(res['sequence']['broken'][0]['outcome'], 'missing')

    def test_a_corrupt_or_mismatched_predecessor_yields_no_pass(self):
        for damage, outcome in (('corrupt', 'corrupt'), ('renamed', 'mismatched'), ('roster', 'mismatched'),
                                ('farm', 'unsupported')):
            with self.subTest(damage):
                shutil.rmtree(self.d, ignore_errors=True)
                self.attempt([{'t': 'fail'}], False, 'a1')
                self.attempt([{'t': 'pass'}], True, 'a2')
                rec = core.paths(self.d, TINYUSB)[2] / 'a1' / 'hil_report.json'
                doc = json.loads(rec.read_text())
                if damage == 'corrupt':
                    rec.write_text('{')
                elif damage == 'renamed':
                    doc['_hil']['attempt_id'] = 'a0'
                elif damage == 'roster':
                    doc['_hil']['roster'] = {'path': 'other.json'}
                else:
                    doc['_hil']['body_schema'] = 'farm-check'
                if damage != 'corrupt':
                    rec.write_text(json.dumps(doc))
                res = self.verdict(retry={'a2'})
                self.assertEqual(self.board(res), ('blocker', False))
                self.assertEqual(res['sequence']['broken'][0]['outcome'], outcome)

    def test_an_unrelated_fresh_run_starts_a_new_sequence(self):
        self.attempt([{'t': 'fail'}], False, 'a1')
        self.attempt([{'t': 'pass'}], False, 'a2')
        res = self.verdict()
        self.assertEqual(self.board(res), ('pass', False))
        self.assertEqual(res['sequence']['attempts'], ['a2'])

    def test_no_board_or_empty_coverage_never_passes(self):
        self.attempt([], False, 'a1', 'no-boards', '**HIL run selected no boards.**\n', boards=())
        self.assertEqual(self.verdict(())['sequence']['verdict'], 'not-run')
        self.assertEqual(self.board(self.verdict()), ('not-run', False))   # requested, nothing selected
        self.assertEqual(core.sequence_verdict(TINYUSB, ['b1'], [])['sequence']['verdict'], 'not-run')


# --- the farm check's namespace -----------------------------------------------------------------
class FarmNamespace(Dir):
    def farm_body(self):
        return {'rows': [{'name': 'b1', 'probe': 'jlink 1', 'status': 'ok'}], 'counts': {'ok': 1}}

    def test_a_farm_check_cannot_overwrite_or_join_a_harness_sequence(self):
        self.run_attempt(TINYUSB, usb_body([usb_row('b1', t='pass')]), attempt_id='a1')
        harness = self.files()
        self.run_attempt(FARM, self.farm_body(), attempt_id='f1')
        self.run_attempt(FARM, self.farm_body(), accumulate=True, attempt_id='f2')
        self.assertEqual(self.files(), harness)
        self.assertTrue((self.d / 'farm_check.json').is_file() and (self.d / 'farm_check.md').is_file())
        self.assertEqual(sorted(p.name for p in (self.d / 'farm_check.attempts').iterdir()), ['f1'])
        self.assertFalse(core.paths(self.d, TINYUSB)[2].exists())
        self.assertEqual([c.attempt_id for c in core.read_chain(self.d, TINYUSB)], ['a1'])
        # a farm schema pointed at the harness pair refuses it rather than replace it
        misplaced = FARM._replace(stem='hil_report')
        with self.assertRaises(core.ReportError):
            core.start_attempt(self.d, misplaced, False)
        with self.assertRaises(core.ReportError):
            core.publish(self.d, misplaced, self.farm_body(),
                         self.meta(misplaced, core.Attempt('f3', None, None, None, None)))
        self.assertEqual(self.files(), harness)

    def test_a_farm_document_never_yields_a_validation_verdict(self):
        with self.assertRaises(ValueError):
            core.apply_run_rules(FARM, ['b1'], [row('b1', 'pass')], 'completed', '')
        with self.assertRaises(ValueError):
            core.sequence_verdict(FARM, ['b1'], [])

    def test_stdout_only_by_default(self):
        a = core.Attempt(core.new_attempt_id(), None, None, None, None)
        text = core.serialise(FARM, self.farm_body(), self.meta(FARM, a))
        self.assertEqual(json.loads(text)['rows'][0]['status'], 'ok')
        self.assertFalse(self.d.exists())


class TinyUF2RunnerOutcome(Dir):
    """hil_ci.verdict() reads the runner's exit (4: cleanup failed; None: a holder kept in
    process), which the report file must carry natively. Rows here pass and carry no
    heldAfter, so the persisted outcome alone decides."""

    ROW = {'board': 'B', 'ran': True, 'pass': True, 'steps': [{'step': 'install', 'pass': True}],
           'cleanup': {'pass': True, 'state': 'blinky copied'}}

    def test_the_exit_survives_the_file_round_trip(self):
        for native, verdict in (({'exitCode': 0, 'heldInProcess': False}, 'pass'),
                                ({'exitCode': 4, 'heldInProcess': False}, 'blocker'),
                                ({'exitCode': 0, 'heldInProcess': True}, 'blocker'),
                                ({}, 'blocker')):   # a legacy report never recorded its exit
            with self.subTest(native):
                shutil.rmtree(self.d, ignore_errors=True)
                self.run_attempt(TINYUF2, uf2_body([dict(self.ROW)], **native), selection=['B'])
                r = core.read_document(core.paths(self.d, TINYUF2)[0], TINYUF2)
                code = models.tinyuf2_exit(r.body)
                run = core.apply_run_rules(TINYUF2, ['B'], models.tinyuf2_rows(code, r.body, ['B']),
                                           r.meta['termination'], r.body['caveat'])
                self.assertEqual(run['rows'][0]['verdict'], verdict)
                self.assertEqual(r.meta['termination'], 'completed')


class TinyUF2Markdown(Dir):
    def test_the_run_report_renders_its_steps(self):
        rows = [{'board': 'metro_m7_1011', 'ran': True, 'pass': False,
                 'steps': [{'step': 'install', 'pass': True}, {'step': 'copy', 'pass': False, 'detail': 'drive vanished'}],
                 'cleanup': {'pass': True, 'state': 'blinky copied'}},
                {'board': 'imxrt1064_evk', 'ran': False, 'pass': False, 'locked': True, 'steps': []}]
        self.run_attempt(TINYUF2, uf2_body(rows, error='one board failed'), selection=['metro_m7_1011', 'imxrt1064_evk'])
        self.assertEqual(self.files(TINYUF2)[1].decode(), (FIXTURES / 'tinyuf2_run.md').read_text(encoding='utf-8'))


if __name__ == '__main__':
    unittest.main()
