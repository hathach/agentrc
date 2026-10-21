"""skills/hil/scripts/hil_report_notice.py is the report's unavailable notice a consumer writes
when hil_report_core itself failed to load: stdlib only and importable alone, under the core's
writer lock (never waited for), replacing the Markdown atomically with any earlier text kept as
labelled legacy text, an earlier notice never nested. Real files and flocks; no hardware."""
import os
import unittest

if os.name == 'nt':
    raise unittest.SkipTest('the report writer lock tests use fcntl')

import fcntl
import importlib.util
import shutil
import subprocess
import sys
import tempfile
from pathlib import Path

SCRIPTS = Path(__file__).resolve().parents[1] / 'skills' / 'hil' / 'scripts'


def _load(name, path):
    spec = importlib.util.spec_from_file_location(name, path)
    mod = importlib.util.module_from_spec(spec)
    spec.loader.exec_module(mod)
    return mod


notice = _load('hil_report_notice', SCRIPTS / 'hil_report_notice.py')
core = _load('agentrc_hil_report_core', SCRIPTS / 'hil_report_core.py')
SCHEMA = core.BodySchema('t', 1, 'hil_report', 'firmware-validation', check=lambda b: None,
                         empty=lambda b: False, render=lambda b: 'view\n')
LEGACY = '\n_Legacy text: '


class WriteUnavailable(unittest.TestCase):
    def setUp(self):
        td = tempfile.TemporaryDirectory()
        self.addCleanup(td.cleanup)
        self.d = Path(td.name) / 'run-1'
        self.md = self.d / 'hil_report.md'

    def files(self):
        return sorted(p.name for p in self.d.iterdir())

    def test_a_fresh_dir_gets_the_notice_alone(self):
        self.assertEqual(notice.write_unavailable(self.d, 'hil_report', 'core\n  failed'), self.md)
        self.assertEqual(self.md.read_text(), '**HIL report unavailable: core failed.**\n')
        self.assertEqual(self.files(), ['.hil_report.lock', 'hil_report.md'])

    def test_earlier_markdown_is_kept_as_labelled_legacy_text(self):
        self.d.mkdir()
        self.md.write_text('| Board | t |\n| b1 | ✅ |\n', encoding='utf-8')
        notice.write_unavailable(self.d, 'hil_report', 'not checked')
        text = self.md.read_text(encoding='utf-8')
        self.assertTrue(text.startswith(f'{notice.NOTICE}: not checked.**\n{LEGACY}'))
        self.assertTrue(text.endswith('\n\n| Board | t |\n| b1 | ✅ |\n'))

    def test_an_earlier_notice_is_not_nested(self):
        self.d.mkdir()
        self.md.write_text('| b1 | ✅ |\n', encoding='utf-8')
        notice.write_unavailable(self.d, 'hil_report', 'first')
        legacy = self.md.read_text(encoding='utf-8').split('\n', 1)[1]
        notice.write_unavailable(self.d, 'hil_report', 'second')
        text = self.md.read_text(encoding='utf-8')
        self.assertEqual(text, f'{notice.NOTICE}: second.**\n' + legacy)
        self.assertEqual(text.count(notice.NOTICE), 1)
        self.assertEqual(text.count(LEGACY), 1)

    def test_a_held_lock_writes_nothing_and_is_reported(self):
        self.d.mkdir()
        self.md.write_text('| b1 | ✅ |\n', encoding='utf-8')
        fd = os.open(notice.lock_path(self.d, 'hil_report'), os.O_RDWR | os.O_CREAT)
        self.addCleanup(os.close, fd)
        fcntl.flock(fd, fcntl.LOCK_EX)
        with self.assertRaises(notice.WriterHeld) as cm:
            notice.write_unavailable(self.d, 'hil_report', 'not checked')
        self.assertNotIsInstance(cm.exception, OSError)   # never mistaken for an unwritable dir
        self.assertEqual(cm.exception.lock, self.d / '.hil_report.lock')
        self.assertEqual(self.md.read_text(encoding='utf-8'), '| b1 | ✅ |\n')
        self.assertEqual(self.files(), ['.hil_report.lock', 'hil_report.md'])

    def test_the_core_and_the_notice_share_one_lock(self):
        fd = notice._acquire(self.d, 'hil_report')
        try:
            with self.assertRaises(core.ReportError) as cm:
                core.prepare_pair(self.d, SCHEMA)
            self.assertEqual(cm.exception.stage, 'writer')
        finally:
            notice._release(fd)
        with core._writer(self.d, SCHEMA), self.assertRaises(notice.WriterHeld):
            notice.write_unavailable(self.d, 'hil_report', 'x')

    def test_the_core_prepares_over_this_notice_without_nesting_it(self):
        self.d.mkdir()
        self.md.write_text('| b1 | ✅ |\n', encoding='utf-8')
        notice.write_unavailable(self.d, 'hil_report', 'core failed')
        legacy = self.md.read_text(encoding='utf-8').split('\n', 1)[1]
        self.assertEqual(core.prepare_pair(self.d, SCHEMA).outcome, 'missing')
        text = self.md.read_text(encoding='utf-8')
        self.assertTrue(text.startswith(f'{core.NOTICE}: hil_report.json is missing.**'))
        self.assertTrue(text.endswith(legacy))
        self.assertEqual(text.count(core.NOTICE), 1)

    def test_an_unwritable_dir_is_an_oserror(self):
        with self.assertRaises(OSError):
            notice.write_unavailable(Path('/proc/nonexistent/nope'), 'hil_report', 'x')
        if os.geteuid() != 0:
            self.d.mkdir()
            self.md.write_text('old\n')
            os.chmod(self.d, 0o500)
            self.addCleanup(os.chmod, self.d, 0o700)
            with self.assertRaises(OSError) as cm:
                notice.write_unavailable(self.d, 'hil_report', 'x')
            self.assertNotIsInstance(cm.exception, notice.WriterHeld)
            self.assertEqual(self.md.read_text(), 'old\n')

    def test_it_imports_alone(self):
        with tempfile.TemporaryDirectory() as td:
            shutil.copy(SCRIPTS / 'hil_report_notice.py', td)
            run = Path(td) / 'run'
            p = subprocess.run([sys.executable, '-I', '-c',
                                'import sys; sys.path.insert(0, sys.argv[1]); import hil_report_notice as n; '
                                'print(n.write_unavailable(sys.argv[2], "hil_report", "core failed"))',
                                td, str(run)],
                               capture_output=True, text=True, timeout=60)
            self.assertEqual(p.returncode, 0, p.stderr)
            self.assertEqual((run / 'hil_report.md').read_text(), '**HIL report unavailable: core failed.**\n')
            self.assertEqual(sorted(p.name for p in Path(td).iterdir() if p.name != '__pycache__'),
                             ['hil_report_notice.py', 'run'])


if __name__ == '__main__':
    unittest.main()
