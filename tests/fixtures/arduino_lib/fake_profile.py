"""A fake Arduino library for tests/test_arduino_*.py: its checkout tree (library/), the build and HIL profiles a
library supplies to skills/arduino/scripts/*.py, as namespaces, and a roster of its rig boards.

library/ holds ci-arduino filter files: Basic/echo skips the host_ci platform (.skip.txt), Host/info and Only/bonly
build only on host_ci and dev_b (.test.only), Nowhere/nowhere on no board, Skip/askip on all but dev_a (.test.skip).
test/hil/sketches/probe is a HIL sketch outside examples/."""
import copy
import hashlib
import json
import shutil
import types
from pathlib import Path

HERE = Path(__file__).resolve().parent
NAME = 'Fake USB Library'
SELF = 'skills/arduino/scripts/arduino_build.py'

BOARDS = {
    'dev_a': {'fqbn': 'vend:arch:dev_a', 'family': 'fa'},
    'dev_b': {'fqbn': 'vend:arch:dev_b', 'family': 'fb'},
    'host_ci': {'fqbn': 'vend:arch:host_ci', 'family': 'fa'},
    'rig_jlink': {'fqbn': 'vend:arch:rig_jlink', 'family': 'fb', 'filters': 'dev_b', 'span': (0x4000, 0x80000)},
    'rig_ocd': {'fqbn': 'vend:arch:rig_ocd', 'family': 'fa', 'filters': 'dev_a', 'span': (0x10000000, 0x10200000)},
    'rig_host': {'fqbn': 'vend:arch:rig_host', 'family': 'fa', 'filters': 'host_ci', 'role': 'host',
                 'span': (0x10000000, 0x10200000)},
    'rig_esp': {'fqbn': 'esp:esp32:s3', 'family': 'fb', 'filters': 'dev_b', 'span': (0, 0x400000), 'chip': 'esp32s3',
                'fixture': 'test/hil/fixture.cpp'},
}
REPRESENTATIVES = {'fa': ['rig_ocd', 'host_ci'], 'fb': ['rig_jlink', 'dev_b']}
HIL_SKETCHES = {'device': ('examples/Basic/echo', 'examples/HID/inout', 'test/hil/sketches/probe'),
                'host': ('examples/Host/info',)}

ROSTER = {'boards': [
    {'name': 'rig_jlink', 'uid': 'JUID', 'flasher': {'name': 'jlink', 'uid': '123456', 'args': '-device X'},
     'debug': {'openocd': '-f interface/jlink.cfg -f target/x.cfg'}, 'arduino': {'tests': ['echo']}},
    {'name': 'rig_ocd', 'uid': 'OUID', 'flasher': {'name': 'openocd', 'uid': 'E66', 'vid_pid': '0x2e8a 0x000c',
                                                   'args': '-f interface/cmsis-dap.cfg -f target/rp2040.cfg',
                                                   'verify': True},
     'debug': {'openocd': '-f interface/cmsis-dap.cfg -f target/rp2040.cfg'}, 'arduino': {'tests': ['echo', 'inout']}},
    {'name': 'rig_host', 'uid': 'HUID', 'flasher': {'name': 'openocd', 'uid': 'H66', 'vid_pid': '0x2e8a 0x000c',
                                                    'args': '-f interface/cmsis-dap.cfg -f target/rp2350.cfg'},
     'debug': {'openocd': '-f interface/cmsis-dap.cfg -f target/rp2350.cfg'}, 'arduino': {'tests': ['info']}},
    {'name': 'rig_esp', 'uid': 'EUID', 'usb_port': '11-1.3', 'no_serial': True,
     'flasher': {'name': 'esptool', 'uid': '3ea6', 'args': '-b 1500000'},
     'debug': {'openocd': '-f board/esp32s3-builtin.cfg', 'tool': 'openocd-esp32', 'uid': '60:55:F9',
               'usb_port': '11-1.2'},
     'arduino': {'tests': ['echo']}},
]}


def library(dest):
    """A copy of library/ at dest: a fresh checkout root, resolved."""
    shutil.copytree(HERE / 'library', dest)
    return Path(dest).resolve()


def agentrc(dest):
    """A stand-in agentrc checkout holding the files a record binds, as agentrc_pin.identity() gives it."""
    files = [SELF, 'skills/hil/scripts/hil_lock.py']
    for f in files:
        (Path(dest) / f).parent.mkdir(parents=True, exist_ok=True)
        (Path(dest) / f).write_text(f'# agentrc {f}\n')
    root = Path(dest).resolve()
    return {'commit': 'a' * 40, 'root': root, 'files': files, 'dirty': False, 'source': 'installed',
            'sha256': {f: hashlib.sha256((root / f).read_bytes()).hexdigest() for f in files}}


def classify_source(path):
    if path == 'src/core_bundled.h':
        return 'uncovered', None, 'a core compiles its bundled copy of this header'
    if path.startswith('src/ports/'):
        port = path.split('/')[2]
        return ('family', port, f'port {port}') if port in REPRESENTATIVES else \
            ('uncovered', None, f'port {port} is built by no board')
    return None


def build_profile(root, agentrc_identity, **over):
    p = types.SimpleNamespace(
        __doc__='The fake library\'s build contract.', ROOT=root, LIBRARY_NAME=NAME, BOARDS=copy.deepcopy(BOARDS),
        REPRESENTATIVES=copy.deepcopy(REPRESENTATIVES), HIL_SKETCHES=dict(HIL_SKETCHES),
        SMOKE_BOARDS=('rig_ocd', 'rig_jlink', 'rig_esp'),
        CONTRACT_TESTS={'build': '.claude/test/test_build.py', 'hil': '.claude/test/test_hil.py'},
        NOTHING=('docs/', 'library.properties'), EXTRA_DEPENDENCIES=('Dep C',),
        CONTRACT_FILES=('contract/check.py', 'contract/pin.json'), classify_source=classify_source,
        vid_pid=lambda build_dir, board, roster_entry: '1234:5678', AGENTRC=agentrc_identity)
    vars(p).update(over)
    return p


def hil_profile(build, **over):
    p = types.SimpleNamespace(
        __doc__='The fake library\'s HIL harness.', ROOT=build.ROOT, BUILD=build,
        TESTS={'echo': 'echo', 'inout': 'inout', 'probe': 'probe', 'info': 'info'},
        ROLE_TESTS={'device': ('echo', 'inout', 'probe'), 'host': ('info',)},
        RESTORE_TEST={'device': 'echo', 'host': 'info'},
        TEST_FUNCS={'echo': lambda *a: (True, 'echoed'), 'inout': lambda *a: (True, 'echoed'),
                    'probe': lambda *a: (True, 'probed')},
        REPORT_SCHEMA='fake-usb-hil', step_host=lambda *a: {'test': a[3], 'pass': True, 'detail': 'host'},
        REMOTE_BASE='~/hil-remote/fake-usb', REMOTE_ROSTER='test/hil/ci.json', HIL='hil/hil_test.py')
    vars(p).update(over)
    return p


def roster(path, **entries):
    """ROSTER written to path, each named entry updated by its dict (a None value drops that key)."""
    data = copy.deepcopy(ROSTER)
    for b in data['boards']:
        for k, v in entries.get(b['name'], {}).items():
            if v is None:
                b.pop(k, None)
            else:
                b[k] = v
    Path(path).parent.mkdir(parents=True, exist_ok=True)
    Path(path).write_text(json.dumps(data))
    return Path(path)


CORE_NAME = 'Fake Core Boards'
CORE_BOARDS = {
    'm0': {'fqbn': 'fake:core:m0', 'family': 'c21'},
    'm0_tusb': {'fqbn': 'fake:core:m0:usb=tusb', 'family': 'c21', 'filters': 'm0:usb=tusb'},
    'm4': {'fqbn': 'fake:core:m4', 'family': 'c51'},
    'rig_m4': {'fqbn': 'fake:core:m4:usb=arduino', 'family': 'c51', 'filters': 'm4', 'span': (0x4000, 0x80000)},
}
CORE_REPRESENTATIVES = {'c21': ['m0', 'm0_tusb'], 'c51': ['m4']}
CORE_HIL_SKETCHES = {'device': ('test/hil/core_cdc', 'test/hil/core_cdc_tusb')}
# a core's tree: Wire/scan and USB/scan share a basename, USB/echo builds only on tusb boards (exclude()),
# Wire/skipped skips everywhere, Wire/only builds on m4 only, SDU/extras/Boot sits outside examples/
CORE_SKETCHES = ('libraries/Wire/examples/scan', 'libraries/Wire/examples/skipped', 'libraries/Wire/examples/only',
                 'libraries/USB/examples/scan', 'libraries/USB/examples/echo', 'libraries/SDU/extras/Boot',
                 'test/hil/core_cdc', 'test/hil/core_cdc_tusb')


def core(dest):
    """A fake Arduino core checkout at dest, resolved: platform.txt, bundled libraries with their sketches."""
    root = Path(dest)
    (root / 'cores/fake').mkdir(parents=True)
    (root / 'cores/fake/main.cpp').write_text('int main() {}\n')
    (root / 'platform.txt').write_text(f'name={CORE_NAME}\nversion=1.0.0\n')
    for lib, props in (('Wire', 'name=Wire\n'), ('USB', 'name=Fake USB\ndepends=Wire, Dep U\n'), ('SDU', 'name=SDU\n')):
        (root / 'libraries' / lib).mkdir(parents=True)
        (root / 'libraries' / lib / 'library.properties').write_text(props)
    for sketch in CORE_SKETCHES:
        (root / sketch).mkdir(parents=True, exist_ok=True)
        (root / sketch / f'{Path(sketch).name}.ino').write_text('void setup() {}\nvoid loop() {}\n')
    (root / 'libraries/Wire/examples/skipped/.all.test.skip').write_text('')
    (root / 'libraries/Wire/examples/only/.m4.test.only').write_text('')
    return root.resolve()


def core_classify(path):
    if path.startswith('cores/'):
        return 'shared', None, 'core runtime'
    if path == 'platform.txt':
        return 'boards', ('m0', 'm0_tusb', 'm4'), 'every recipe'
    if path.startswith('libraries/'):
        lib = path.split('/')[1]
        return 'sketches', [s for s in CORE_SKETCHES if s.startswith(f'libraries/{lib}/')], f'bundled {lib}'
    return None


def core_profile(root, agentrc_identity, **over):
    return build_profile(root, agentrc_identity, **{
        'LIBRARY_NAME': CORE_NAME, 'PLATFORM': 'fake:core', 'SKETCH_ROOTS': ('libraries',),
        'BOARDS': copy.deepcopy(CORE_BOARDS), 'REPRESENTATIVES': copy.deepcopy(CORE_REPRESENTATIVES),
        'HIL_SKETCHES': dict(CORE_HIL_SKETCHES), 'SMOKE_BOARDS': ('rig_m4',), 'EXTRA_DEPENDENCIES': ('Dep C',),
        'classify_source': core_classify,
        'exclude': lambda board, ex: ex.startswith('libraries/USB/') and 'tusb' not in board and ex.endswith('echo'),
        'fqbn': lambda board, ex: 'fake:core:m4:usb=tusb' if ex == 'test/hil/core_cdc_tusb' else
        CORE_BOARDS[board]['fqbn'],
        **over})
