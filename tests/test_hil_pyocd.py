"""skills/hil/scripts/pyocd/run_pyocd.py and user_script.py, moved from tinyusb's
test/hil/pyocd (c5099be4f) with the project directory now an explicit input.

The launcher cases run it against fake usb/libusb_package/pyocd packages whose find() applies
keyword filters before custom_match, as pyusb's does. They prove the plumbing only; that the
real pyusb and pyocd open nothing else is the strace evidence from the rig.
"""
import sys
import unittest

if not sys.platform.startswith('linux'):
    raise unittest.SkipTest('the launcher refuses other platforms')

import importlib.util
import json
import os
import subprocess
import tempfile
from pathlib import Path
from unittest import mock

PYOCD_DIR = Path(__file__).resolve().parents[1] / 'skills' / 'hil' / 'scripts' / 'pyocd'
LAUNCHER = PYOCD_DIR / 'run_pyocd.py'
USER_SCRIPT = PYOCD_DIR / 'user_script.py'
PROBE = [0x1fc9, 0x0090]
OTHER = [0x1366, 0x1024]

FAKES = {
    'usb/__init__.py': '',
    'usb/core.py': '''
import json, os
class Device:
    def __init__(self, vid, pid):
        self.idVendor, self.idProduct = vid, pid
def find(find_all=False, backend=None, custom_match=None, **kwargs):
    devs = [Device(*v) for v in json.loads(os.environ['FAKE_USB_DEVICES'])]
    found = [d for d in devs if all(getattr(d, k) == v for k, v in kwargs.items())
             and (custom_match is None or custom_match(d))]
    return found if find_all else (found[0] if found else None)
''',
    'pyocd/__init__.py': '',
    'pyocd/probe/__init__.py': '',
    'pyocd/probe/aggregator.py': "PROBE_CLASSES = {'cmsisdap': 1, 'jlink': 2, 'stlink': 3}\n",
    'pyocd/probe/pydapaccess/__init__.py': '',
    'pyocd/probe/pydapaccess/interface/__init__.py': '',
    'pyocd/__main__.py': '''
import json, os, sys
def main():
    from pyocd.probe.aggregator import PROBE_CLASSES
    from pyocd.probe.pydapaccess.interface import pyusb_backend, pyusb_v2_backend
    touched = []
    for mod in (pyusb_backend, pyusb_v2_backend):
        mod.usb_find(find_all=True, custom_match=lambda d: touched.append([d.idVendor, d.idProduct]))
    print(json.dumps({'touched': touched, 'classes': sorted(PROBE_CLASSES),
                      'backend': os.environ.get('PYOCD_USB_BACKEND'),
                      'project_dir': os.environ.get('PYOCD_PROJECT_DIR'), 'argv': sys.argv[1:]}))
    sys.exit(0)
''',
}
BACKEND = '''
import os
if os.environ.get('FAKE_BIND_ELSEWHERE'):
    usb_find = lambda *a, **k: []
else:
    try:
        from libusb_package import find as usb_find
    except ImportError:
        from usb.core import find as usb_find
'''
LIBUSB_PACKAGE = '''
def find(*args, **kwargs):
    import usb.core
    return usb.core.find(*args, **kwargs)
'''


class Launcher(unittest.TestCase):
    def setUp(self):
        td = tempfile.TemporaryDirectory()
        self.addCleanup(td.cleanup)
        self.root = Path(td.name).resolve()
        files = {**FAKES, 'pyocd/probe/pydapaccess/interface/pyusb_backend.py': BACKEND,
                 'pyocd/probe/pydapaccess/interface/pyusb_v2_backend.py': BACKEND}
        for rel, body in files.items():
            (self.root / 'fakes' / rel).parent.mkdir(parents=True, exist_ok=True)
            (self.root / 'fakes' / rel).write_text(body)
        # an interpreter may have the real libusb_package; the shadow makes it absent
        for where, body in (('libusb', LIBUSB_PACKAGE), ('nolibusb', "raise ImportError('absent')\n")):
            (self.root / where / 'libusb_package').mkdir(parents=True)
            (self.root / where / 'libusb_package' / '__init__.py').write_text(body)
        self.project = self.root / 'project'
        self.project.mkdir()
        self.elsewhere = self.root / 'elsewhere'
        self.elsewhere.mkdir()

    def run_launcher(self, *args, libusb_package=True, cwd=None, **env):
        path = [str(self.root / 'fakes'), str(self.root / ('libusb' if libusb_package else 'nolibusb'))]
        env = {**os.environ, 'PYTHONPATH': os.pathsep.join(path), 'PYOCD_USB_BACKEND': 'hidapiusb',
               'FAKE_USB_DEVICES': json.dumps([OTHER, PROBE, [0x0483, 0x3752]]), **env}
        return subprocess.run([sys.executable, str(LAUNCHER), *args], env=env, cwd=cwd or self.elsewhere,
                              capture_output=True, text=True, timeout=30)

    def test_only_the_filtered_probe_reaches_pyocds_matcher(self):
        for libusb_package in (True, False):
            with self.subTest(libusb_package=libusb_package):
                r = self.run_launcher('--project-dir', str(self.project), '0x1fc9', '0x0090', 'flash',
                                      '-u', 'cmsisdap:X', '--script', 'roster.py', 'fw.elf',
                                      libusb_package=libusb_package)
                self.assertEqual(r.returncode, 0, r.stderr)
                self.assertEqual(json.loads(r.stdout), {
                    'touched': [PROBE, PROBE],            # once per backend, nothing else
                    'classes': ['cmsisdap'],
                    'backend': 'pyusb',                   # hidapi would bypass the filter
                    'project_dir': str(self.project),     # the caller's, not the cwd
                    # argparse keeps the last --script
                    'argv': ['flash', '-u', 'cmsisdap:X', '--script', 'roster.py', 'fw.elf',
                             '--no-config', '--script', str(USER_SCRIPT)]})

    def test_a_relative_project_dir_resolves_from_the_cwd_and_relative_args_pass_through(self):
        r = self.run_launcher('--project-dir', '../project', '0x1fc9', '0x0090', 'flash',
                              '--pack', 'packs/lpc55.pack', 'fw.elf', cwd=self.elsewhere)
        self.assertEqual(r.returncode, 0, r.stderr)
        out = json.loads(r.stdout)
        self.assertEqual(out['project_dir'], str(self.project))
        # pyocd resolves them from the project dir it chdirs into, not the launcher's cwd
        self.assertEqual(out['argv'][:4], ['flash', '--pack', 'packs/lpc55.pack', 'fw.elf'])

    def test_a_missing_project_dir_argument_is_refused(self):
        for args in (('0x1fc9', '0x0090', 'flash'), ('--project-dir',)):
            r = self.run_launcher(*args)
            self.assertEqual((r.returncode, r.stdout), (2, ''), args)

    def test_a_backend_that_did_not_bind_the_filtered_find_stops_before_discovery(self):
        r = self.run_launcher('--project-dir', str(self.project), '0x1fc9', '0x0090', 'flash', 'fw.elf',
                              FAKE_BIND_ELSEWHERE='1')
        self.assertEqual((r.returncode, r.stdout), (2, ''))
        self.assertIn('is not the filtered find', r.stderr)

    def test_a_malformed_vid_pid_stops_before_pyocd(self):
        for args in (('flash', 'fw.elf'), ('0x1fc9',), ()):
            r = self.run_launcher('--project-dir', str(self.project), *args)
            self.assertEqual((r.returncode, r.stdout), (2, ''), args)

    def test_a_caller_asking_for_another_vid_is_refused(self):
        spec = importlib.util.spec_from_file_location('run_pyocd', LAUNCHER)
        run_pyocd = importlib.util.module_from_spec(spec)
        spec.loader.exec_module(run_pyocd)
        find = run_pyocd._filter_vid_pid(lambda **kw: kw, 0x1fc9, 0x0090)
        self.assertEqual(find(find_all=True), {'find_all': True, 'idVendor': 0x1fc9, 'idProduct': 0x0090})
        with self.assertRaises(RuntimeError):
            find(idVendor=0x1366)


class UserScript(unittest.TestCase):
    """set_reset_catch as pyocd calls it: the script's globals hold `target`, the delegate's
    falsy return keeps pyocd's own reset catch."""
    def load(self):
        lpc5500 = mock.MagicMock()
        lpc5500.DM_AP = 2
        lpc5500.CortexM_LPC5500 = type('CortexM_LPC5500', (), {})
        target = mock.MagicMock()
        target.aps = {0: 'ahb', 2: 'dm'}
        ns = {'target': target}
        with mock.patch.dict(sys.modules, {'pyocd': mock.MagicMock(), 'pyocd.target': mock.MagicMock(),
                                           'pyocd.target.family': mock.MagicMock(),
                                           'pyocd.target.family.target_lpc5500': lpc5500}):
            exec(compile(USER_SCRIPT.read_text(), str(USER_SCRIPT), 'exec'), ns)
        return ns, target, lpc5500.CortexM_LPC5500

    def test_an_lpc55_core_is_unlocked_through_the_debug_mailbox_first(self):
        ns, target, lpc55 = self.load()
        self.assertFalse(ns['set_reset_catch'](lpc55(), 'sw'))
        target.unlock.assert_called_once_with('dm')

    def test_any_other_core_is_left_to_pyocd(self):
        ns, target, _ = self.load()
        self.assertFalse(ns['set_reset_catch'](object(), 'sw'))
        target.unlock.assert_not_called()


if __name__ == '__main__':
    unittest.main()
