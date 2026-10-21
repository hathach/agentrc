"""skills/hil/scripts/hil_load.py is the one path loader of the hil scripts: one module per resolved file per
process (a second load reuses it, under any name, a consumer's own registered load included), a second path
under one name refused, a failed run rolled back; and each script's FILES, its launch-time file closure, is the
literal transitive closure of what it loads or launches by path, refused at import naming any missing entry."""
import ast
import importlib.util
import os
import re
import shutil
import subprocess
import sys
import tempfile
import threading
import types
import unittest
from pathlib import Path, PurePosixPath
from unittest import mock
sys.path.insert(0, str(Path(__file__).resolve().parent))
from helpers import guard_survivors  # noqa: E402

ROOT = Path(__file__).resolve().parents[1]
SCRIPTS = ROOT / 'skills' / 'hil' / 'scripts'
LOADER = 'skills/hil/scripts/hil_load.py'

spec = importlib.util.spec_from_file_location('hil_load_under_test', SCRIPTS / 'hil_load.py')
hl = importlib.util.module_from_spec(spec)
spec.loader.exec_module(hl)

# the scripts a consumer loads, by ROOT-relative path, and their FILES read statically
MODULES = sorted(p.relative_to(ROOT).as_posix() for p in SCRIPTS.glob('*.py') if p.name != 'hil_load.py')


def files_of(rel):
    for node in ast.parse((ROOT / rel).read_text()).body:
        if isinstance(node, ast.Assign) and [ast.unparse(t) for t in node.targets] == ['FILES']:
            return ast.literal_eval(node.value)
    return None


def direct(rel):
    """What `rel` itself loads or launches by path: load_file('<rel>'), os.path.join(HERE, ...),
    Path(...).with_name('<file>'), and a shell script's "$(dirname "$0")/<file>"."""
    text, here = (ROOT / rel).read_text(), PurePosixPath(rel).parent
    if rel.endswith('.sh'):
        return {(here / f).as_posix() for f in re.findall(r'"\$\(dirname "\$0"\)/([\w.-]+)"', text)}
    out = set()
    for node in ast.walk(ast.parse(text)):
        if not isinstance(node, ast.Call) or not node.args:
            continue
        fn, args = ast.unparse(node.func), node.args
        if fn.endswith('load_file') and isinstance(args[0], ast.Constant):
            out.add(args[0].value)
        elif fn == 'os.path.join' and ast.unparse(args[0]) == 'HERE' and all(isinstance(a, ast.Constant) for a in args[1:]):
            out.add(os.path.normpath(here.joinpath(*(a.value for a in args[1:]))))
        elif fn.endswith('.with_name') and isinstance(args[0], ast.Constant):
            out.add((here / args[0].value).as_posix())
    out.discard(rel)
    return out


def closure(rel):
    seen, todo = set(), [rel]
    while todo:
        for f in direct(todo.pop()) - seen:
            seen.add(f)
            todo.append(f)
    seen.discard(rel)
    return seen


def stage(dest, rels):
    for rel in rels:
        (dest / rel).parent.mkdir(parents=True, exist_ok=True)
        shutil.copy2(ROOT / rel, dest / rel)


def run_py(code, *args):
    return subprocess.run([sys.executable, '-I', '-c', code, *map(str, args)], capture_output=True, text=True,
                          timeout=60)


# a consumer: hil_load registered as agentrc_hil_load (the scripts' bootstrap), and a path loader of its own that
# registers what it loads, as tinyusb's helper/agentrc.load does
CONSUMER = '''
import importlib.util, sys
def boot(dep):
    spec = importlib.util.spec_from_file_location('agentrc_hil_load', dep + '/skills/hil/scripts/hil_load.py')
    if spec.name not in sys.modules:
        mod = importlib.util.module_from_spec(spec)
        spec.loader.exec_module(mod)
        sys.modules.setdefault(spec.name, mod)
    return sys.modules[spec.name].load(spec.name, spec.origin)
def own(name, path):
    spec = importlib.util.spec_from_file_location(name, path)
    mod = importlib.util.module_from_spec(spec)
    sys.modules[name] = mod
    spec.loader.exec_module(mod)
    return mod
dep = sys.argv[1]
S = dep + '/skills/hil/scripts/'
'''


def setUpModule():
    guard_survivors(__name__)


class Load(unittest.TestCase):
    """hil_load.load on plain files, in this process under names of its own."""

    def setUp(self):
        td = tempfile.TemporaryDirectory()
        self.addCleanup(td.cleanup)
        self.dir = Path(td.name)
        self.names = []
        self.addCleanup(lambda: [sys.modules.pop(n, None) for n in self.names])

    def name(self, suffix):
        n = f'hil_load_test_{suffix}'
        self.names.append(n)
        return n

    def write(self, rel, text):
        p = self.dir / rel
        p.parent.mkdir(parents=True, exist_ok=True)
        p.write_text(text)
        return p

    def counted(self, rel):
        # each run of the file appends to its own log
        return self.write(rel, f'with open({str(self.dir / (rel + ".runs"))!r}, "a") as f:\n    f.write("x")\n')

    def runs(self, rel):
        return len((self.dir / (rel + '.runs')).read_text())

    def test_the_same_path_is_one_module_run_once(self):
        p = self.counted('a.py')
        a = hl.load(self.name('a'), p)
        self.assertIs(hl.load(self.name('a'), p), a)
        self.assertIs(sys.modules[self.name('a')], a)
        self.assertEqual(self.runs('a.py'), 1)

    def test_another_spelling_of_the_path_resolves_to_the_same_module(self):
        p = self.counted('a.py')
        (self.dir / 'link').symlink_to(self.dir)
        a = hl.load(self.name('a'), p)
        self.assertIs(hl.load(self.name('a'), self.dir / 'link' / 'sub' / '..' / 'a.py'), a)
        self.assertEqual(self.runs('a.py'), 1)

    def test_a_module_registered_from_the_path_under_another_name_is_reused(self):
        for case in ('the same path', 'a differently named symlink'):
            with self.subTest(case=case):
                n = len(self.names)
                p = self.counted(f'real{n}.py')
                registered = p
                if case != 'the same path':
                    registered = self.dir / f'different_name{n}.py'
                    registered.symlink_to(p)
                own = importlib.util.spec_from_file_location(self.name(f'consumer{n}'), registered)
                mine = importlib.util.module_from_spec(own)
                sys.modules[own.name] = mine
                own.loader.exec_module(mine)
                self.assertIs(hl.load(self.name(f'a{n}'), p), mine)
                self.assertIs(sys.modules[self.name(f'a{n}')], mine)
                self.assertEqual(self.runs(f'real{n}.py'), 1)

    def test_a_second_path_under_one_name_is_refused(self):
        one, two = self.counted('one/a.py'), self.counted('two/a.py')
        a = hl.load(self.name('a'), one)
        with self.assertRaises(ImportError) as cm:
            hl.load(self.name('a'), two)
        self.assertIn(str(one.resolve()), str(cm.exception))
        self.assertIn(str(two.resolve()), str(cm.exception))
        self.assertIs(sys.modules[self.name('a')], a)
        self.assertFalse((self.dir / 'two/a.py.runs').exists())

    def test_a_name_held_by_a_module_without_a_file_is_refused(self):
        sys.modules[self.name('a')] = type(sys)('fileless')
        with self.assertRaisesRegex(ImportError, 'no file'):
            hl.load(self.name('a'), self.counted('a.py'))

    def ctx(self, **kw):
        """What a test file's body reaches as sys.modules['hil_load_test_ctx']."""
        sys.modules[self.name('ctx')] = ns = types.SimpleNamespace(hl=hl, **kw)
        return ns

    def test_a_failed_or_interrupted_run_is_rolled_back_with_every_alias_and_a_retry_runs_again(self):
        seen = 'import sys\nSEEN = __name__ in sys.modules\n'
        for case, exc, aliased in (('a failure', RuntimeError, False), ('an interrupt', KeyboardInterrupt, False),
                                   ('a secondary alias', RuntimeError, True)):
            with self.subTest(case=case):
                n = len(self.names)
                name, alias = self.name(f'bad{n}'), self.name(f'alias{n}')
                aliasing = f'sys.modules[{alias!r}] = sys.modules[__name__]\n' if aliased else ''
                p = self.write(f'bad{n}.py', f'{seen}{aliasing}raise {exc.__name__}("half run")\n')
                with self.assertRaisesRegex(exc, 'half run'):
                    hl.load(name, p)
                self.assertNotIn(name, sys.modules)
                self.assertNotIn(alias, sys.modules)
                p.write_text(f'{seen}DONE = True\n')
                m = hl.load(alias if aliased else name, p)
                self.assertTrue(m.SEEN, 'registered before it runs')
                self.assertTrue(m.DONE, 'the retry ran the replacement source')

    def test_a_circular_load_is_refused_not_served_partial(self):
        self.ctx()
        p = self.write('circ.py', f'import sys\nctx = sys.modules[{self.name("ctx")!r}]\n'
                                  f'try:\n    ctx.hl.load({self.name("alias")!r}, __file__)\n'
                                  f'except ImportError as e:\n    REFUSED = str(e)\nDONE = True\n')
        m = hl.load(self.name('circ'), p)
        self.assertTrue(m.DONE)
        self.assertIn('still being initialized', m.REFUSED)
        self.assertFalse(self.name('alias') in sys.modules)
        self.assertFalse(m.__spec__._initializing)

    def on_path(self):
        sys.path.insert(0, str(self.dir))
        self.addCleanup(sys.path.remove, str(self.dir))

    def thread(self, target):
        out = {}

        def run():
            try:
                out['value'] = target()
            except BaseException as e:     # noqa: BLE001 - reported to the test
                out['error'] = e
        t = threading.Thread(target=run, daemon=True)
        t.start()
        return t, out

    def test_an_ordinary_import_running_in_another_thread_is_waited_for(self):
        ctx = self.ctx(started=threading.Event(), go=threading.Event())
        mod = self.name('slow')
        p = self.write(f'{mod}.py', f'import sys\nctx = sys.modules[{self.name("ctx")!r}]\n'
                                    f'ctx.started.set()\nctx.go.wait(10)\nDONE = True\n')
        self.on_path()
        t, out = self.thread(lambda: importlib.import_module(mod))
        self.assertTrue(ctx.started.wait(10))
        timer = threading.Timer(0.3, ctx.go.set)
        timer.start()
        self.addCleanup(timer.cancel)
        m = hl.load(self.name('a'), p)
        self.assertTrue(m.DONE, 'a partially initialized module was returned')
        t.join(10)
        self.assertIs(out.get('value'), m)

    def test_two_paths_loaded_at_once_under_one_name_have_one_winner(self):
        ctx = self.ctx(barrier=threading.Barrier(2))
        paths = [self.write(f'{d}/a.py', 'import time\ntime.sleep(0.3)\nDONE = True\n') for d in ('one', 'two')]
        name = self.name('a')

        def racer(path):
            ctx.barrier.wait(10)
            return hl.load(name, path)
        runs = [self.thread(lambda p=p: racer(p)) for p in paths]
        for t, _ in runs:
            t.join(30)
            self.assertFalse(t.is_alive())
        outs = [out for _, out in runs]
        won = [out['value'] for out in outs if 'value' in out]
        lost = [out['error'] for out in outs if 'error' in out]
        self.assertEqual((len(won), len(lost)), (1, 1), outs)
        self.assertIsInstance(lost[0], ImportError)
        self.assertIn('is already loaded from', str(lost[0]))
        self.assertIs(sys.modules[name], won[0])
        self.assertTrue(won[0].DONE)

    def test_an_ordinary_import_that_fails_while_waited_for_is_not_published(self):
        ctx = self.ctx(started=threading.Event(), go=threading.Event())
        mod = self.name('failing')
        p = self.write(f'{mod}.py', f'import sys\nctx = sys.modules[{self.name("ctx")!r}]\n'
                                    f'ctx.started.set()\nctx.go.wait(10)\nraise RuntimeError("failed run")\n')
        self.on_path()
        t, out = self.thread(lambda: importlib.import_module(mod))
        self.assertTrue(ctx.started.wait(10))
        timer = threading.Timer(0.3, ctx.go.set)
        timer.start()
        self.addCleanup(timer.cancel)
        with self.assertRaisesRegex(ImportError, 'failed to initialize in another thread'):
            hl.load(self.name('alias'), p)
        t.join(10)
        self.assertIsInstance(out.get('error'), RuntimeError)
        self.assertFalse(self.name('alias') in sys.modules)
        self.assertFalse(mod in sys.modules)

    def test_an_import_that_fails_after_the_scan_found_it_is_not_published(self):
        """The failure lands between the alias scan's snapshot and the wait: by then the module is no
        longer initializing, and only its missing registration tells."""
        p = self.counted('late.py')
        spec = importlib.util.spec_from_file_location(self.name('late'), p)
        found = importlib.util.module_from_spec(spec)
        spec._initializing = True
        sys.modules[spec.name] = found
        real_origin = hl._origin

        def origin(module):
            if module is found:          # the ordinary import fails just after the scan reads it
                sys.modules.pop(spec.name, None)
                spec._initializing = False
            return real_origin(module)
        with mock.patch.object(hl, '_origin', origin), \
                self.assertRaisesRegex(ImportError, 'failed to initialize in another thread'):
            hl.load(self.name('alias'), p)
        self.assertFalse(self.name('alias') in sys.modules)

    def test_a_cross_thread_cycle_with_an_ordinary_import_ends(self):
        """Thread A loads a.py, which imports b; thread B imports b, which loads a.py. Each ends, with a
        complete module or an error, never a partial one or a hang."""
        ctx = self.ctx(a_running=threading.Event(), b_running=threading.Event())
        a, b, c = self.name('cyc_a'), self.name('cyc_b'), self.name('ctx')
        pa = self.write('cyc_a.py', f'import sys\nctx = sys.modules[{c!r}]\nctx.a_running.set()\n'
                                    f'ctx.b_running.wait(10)\nimport {b}\nDONE = True\n')
        self.write(f'{b}.py', f'import sys\nctx = sys.modules[{c!r}]\nctx.b_running.set()\nctx.a_running.wait(10)\n'
                              f'A_DONE = getattr(ctx.hl.load({a!r}, {str(pa)!r}), "DONE", False)\nDONE = True\n')
        self.on_path()
        ta, out_a = self.thread(lambda: hl.load(a, pa))
        tb, out_b = self.thread(lambda: importlib.import_module(b))
        for t in (ta, tb):
            t.join(30)
            self.assertFalse(t.is_alive(), 'the import cycle hung')
        for out in (out_a, out_b):
            if 'value' in out:
                self.assertTrue(out['value'].DONE)
        if 'value' in out_b:
            self.assertTrue(out_b['value'].A_DONE, 'b got a partially initialized a')
        self.assertTrue('value' in out_a or 'value' in out_b, (out_a, out_b))

    def test_a_missing_file_is_refused_naming_it(self):
        with self.assertRaises(ImportError) as cm:
            hl.load(self.name('none'), self.dir / 'none.py')
        self.assertIn(str((self.dir / 'none.py').resolve()), str(cm.exception))
        self.assertFalse(self.name('none') in sys.modules)

    def test_require_names_every_missing_file(self):
        with self.assertRaises(ImportError) as cm:
            hl.require('owner.py', (LOADER, 'skills/none/a.py', 'skills/none/b.py'))
        self.assertIn('owner.py', str(cm.exception))
        self.assertIn('skills/none/a.py, skills/none/b.py', str(cm.exception))
        self.assertNotIn(LOADER, str(cm.exception))
        hl.require('owner.py', (LOADER,))


@unittest.skipIf(os.name == 'nt', 'the hil scripts need fcntl')
class Files(unittest.TestCase):
    """Each script's FILES: a literal, its whole by-path closure, enough to import and run from a tree holding
    only it and FILES, and refused at import naming whichever entry is missing."""

    def test_every_by_path_load_goes_through_one_identical_bootstrap(self):
        boots = set()
        for rel in MODULES:
            tree = ast.parse(text := (ROOT / rel).read_text())
            calls = [n for n in ast.walk(tree) if isinstance(n, ast.Call)
                     and ast.unparse(n.func).endswith('spec_from_file_location')]
            fns = [n for n in tree.body if isinstance(n, ast.FunctionDef) and n.name == '_hil_load']
            inside = [n for f in fns for n in ast.walk(f) if n in calls]
            self.assertEqual(len(calls), len(inside), f'{rel} loads a file by path outside _hil_load')
            boots |= {ast.get_source_segment(text, f) for f in fns}
        self.assertEqual(len(boots), 1, 'the _hil_load bootstraps differ')

    def test_files_is_the_literal_by_path_closure(self):
        seen = 0
        for rel in MODULES:
            files = files_of(rel)
            self.assertIsNotNone(files, f'{rel} exports no FILES literal')
            seen += 1
            self.assertEqual(len(files), len(set(files)), rel)
            self.assertEqual(set(files), closure(rel), rel)
            for f in files:
                self.assertTrue((ROOT / f).is_file(), f'{rel}: FILES names {f}, which does not exist')
        self.assertEqual(seen, len(MODULES))
        self.assertGreater(seen, 12, 'the FILES walk went vacuous')
        self.assertIn('skills/usb-kernel-recover/scripts/root_port_cycle.py', files_of('skills/hil/scripts/hil_reserve.py'))
        self.assertIn('skills/target-debug/scripts/esp_session.py', files_of('skills/hil/scripts/hil_openocd_esp.py'))

    def test_a_tree_of_the_module_and_its_files_imports_and_runs_lazy_loads(self):
        for rel in MODULES:
            files = files_of(rel)
            if not files:
                continue
            with self.subTest(rel), tempfile.TemporaryDirectory() as td:
                stage(Path(td), (rel, *files))
                r = run_py(CONSUMER + 'm = boot(dep).load("consumer_m", dep + "/" + sys.argv[2])\n'
                           'getattr(m, "_process", lambda: None)()\n'
                           'print(sorted(f for f in (getattr(v, "__file__", None) for v in list(sys.modules.values()))'
                           ' if f and f.startswith(dep)))', td, rel)
                self.assertEqual(r.returncode, 0, r.stderr)
                loaded = {os.path.relpath(f, td) for f in ast.literal_eval(r.stdout)}
                self.assertLessEqual(loaded, {rel, *files})

    def test_each_missing_entry_is_refused_at_import_naming_it(self):
        cases = 0
        for rel in MODULES:
            for missing in files_of(rel) or ():
                with self.subTest(rel=rel, missing=missing), tempfile.TemporaryDirectory() as td:
                    stage(Path(td), [f for f in (rel, *files_of(rel)) if f != missing])
                    r = run_py('import importlib.util, sys\n'
                               'spec = importlib.util.spec_from_file_location("m", sys.argv[1])\n'
                               'mod = importlib.util.module_from_spec(spec)\n'
                               'try:\n    spec.loader.exec_module(mod)\n'
                               'except ImportError as e:\n    print(e)\n    sys.exit(3)\n'
                               'print(sorted(n for n in sys.modules if n.startswith("agentrc_")))',
                               Path(td) / rel)
                    self.assertEqual(r.returncode, 3, r.stdout + r.stderr)
                    self.assertIn(missing, r.stdout)
                    cases += 1
        self.assertGreater(cases, 20)

    def test_the_report_notice_loads_alone(self):
        with tempfile.TemporaryDirectory() as td:
            shutil.copy2(SCRIPTS / 'hil_report_notice.py', td)
            r = run_py('import importlib.util, sys\n'
                       'spec = importlib.util.spec_from_file_location("n", sys.argv[1])\n'
                       'mod = importlib.util.module_from_spec(spec)\n'
                       'spec.loader.exec_module(mod)\n'
                       'print(mod.write_unavailable.__name__, sorted(n for n in sys.modules if n.startswith("agentrc_")))',
                       Path(td) / 'hil_report_notice.py')
        self.assertEqual(r.returncode, 0, r.stderr)
        self.assertEqual(r.stdout.split(), ['write_unavailable', '[]'])

    def test_each_cli_runs_standalone_from_a_tree_of_its_files(self):
        for rel, argv, rc, out in (('skills/hil/scripts/hil_lock.py', ['--help'], 0, 'usage'),
                                   ('skills/hil/scripts/hil_reserve.py', ['--help'], 0, 'usage'),
                                   ('skills/hil/scripts/hil_openocd_esp.py', ['--help'], 0, 'usage'),
                                   ('skills/hil/scripts/hil_process.py', [], 1, 'usage')):
            with self.subTest(rel), tempfile.TemporaryDirectory() as td:
                stage(Path(td), (rel, *files_of(rel)))
                for flags in ([], ['-I']):
                    r = subprocess.run([sys.executable, *flags, str(Path(td) / rel), *argv], capture_output=True,
                                       text=True, timeout=60)
                    self.assertEqual(r.returncode, rc, r.stderr)
                    self.assertIn(out, r.stdout + r.stderr)
                    self.assertNotIn('Error', r.stderr)


@unittest.skipIf(os.name == 'nt', 'the hil scripts need fcntl')
class Consumers(unittest.TestCase):
    """A consumer's loads and the scripts' own loads of one file share one module, in either order."""

    def run_consumer(self, body, *args):
        r = run_py(CONSUMER + body, ROOT, *args)
        self.assertEqual(r.returncode, 0, r.stderr)
        return r.stdout.split()

    def test_a_consumer_loading_first_through_its_own_registered_loader_is_reused(self):
        out = self.run_consumer('uo = own("usb_observe", S + "usb_observe.py")\n'
                                'hu = own("tinyusb_hil_usbtest", S + "hil_usbtest.py")\n'
                                'print(hu.usb_observe is uo, sys.modules["agentrc_usb_observe"] is uo)')
        self.assertEqual(out, ['True', 'True'])

    def test_the_scripts_loading_first_are_reused_by_a_consumer_through_hil_load(self):
        out = self.run_consumer(
            'hl = boot(dep)\n'
            'oe = hl.load("tinyusb_hil_openocd_esp", S + "hil_openocd_esp.py")\n'
            'hr = hl.load_file("skills/hil/scripts/hil_reserve.py")\n'
            'uo = hl.load("usb_observe", S + "usb_observe.py")\n'
            'hu = hl.load("tinyusb_hil_usbtest", S + "hil_usbtest.py")\n'
            'hf = hl.load("tinyusb_hil_flash_provider", S + "hil_flash.py")\n'
            'print(oe.usb_observe is uo is hu.usb_observe is hr.uo, oe.hil_flash is hf,\n'
            '      hf.hil_process is hr.hil_lock._process() is oe.hil_process, oe.hil_load is hl)')
        self.assertEqual(out, ['True'] * 4)

    def test_a_second_pin_in_one_process_is_refused(self):
        with tempfile.TemporaryDirectory() as td:
            rel = 'skills/hil/scripts/hil_usbtest.py'
            stage(Path(td), (rel, *files_of(rel)))
            r = run_py(CONSUMER + 'hl = boot(dep)\n'
                       'hl.load("tinyusb_hil_usbtest", S + "hil_usbtest.py")\n'
                       'hl.load("usb_observe", S + "usb_observe.py")\n'
                       'other = sys.argv[2] + "/skills/hil/scripts/"\n'
                       'for load, name, path in ((own, "other_usbtest", other + "hil_usbtest.py"),\n'
                       '                         (hl.load, "usb_observe", other + "usb_observe.py")):\n'
                       '    try:\n'
                       '        load(name, path)\n'
                       '    except ImportError as e:\n'
                       '        print(e)\n'
                       '    else:\n'
                       '        print("loaded", name)',
                       ROOT, td)
        self.assertEqual(r.returncode, 0, r.stderr)
        lines = r.stdout.splitlines()
        self.assertEqual(len(lines), 2, r.stdout)
        self.assertIn('agentrc_hil_load is already loaded from', lines[0])
        self.assertIn('usb_observe is already loaded from', lines[1])
        self.assertTrue(all(str(Path(td).resolve()) in line for line in lines), lines)


if __name__ == '__main__':
    unittest.main()
