#!/usr/bin/env python3
"""Golden flash/reset cases for tests/test_hil_flash.py, frozen from tinyusb's pre-move
test/hil/hil_flash.py at SOURCE. tinyusb.json and hfp.json beside this file are byte copies
of that commit's test/hil rosters.

Regenerate (reads the checkout, writes nothing there):
    python3 tests/data/hil_flash/gen_golden.py <tinyusb checkout at SOURCE>

The test replays every case on the provider through the same `record`, so both sides are
observed identically: every command, its kwargs and J-Link command-file contents, the files
left in the working directory, stdout/stderr, and which runner result came back.
"""
import contextlib
import io
import json
import os
import shlex
import subprocess
import sys
import tempfile
import types
from pathlib import Path

HERE = Path(__file__).resolve().parent
GOLDEN = HERE / 'golden.json'
SOURCE = 'c5099be4f'
ROSTERS = ('tinyusb.json', 'hfp.json')
FIRMWARE = '/hil-golden/cdc_msc.elf'
# No roster entry flashes with pyocd; this is tinyusb test_hil_pyocd.py's flasher with a relative
# --pack added.
PYOCD = {'name': 'pyocd', 'uid': 'GSA0CQEQ', 'vid_pid': '0x1fc9 0x0090',
         'args': '-t lpc55s28 --pack packs/lpc55s28.pack'}
ESP_CHIP = {'espressif_p4_function_ev': 'esp32p4', 'espressif_s3_devkitm': 'esp32s3'}
ESP_PORT = '/dev/serial/by-id/usb-golden_{uid}-if00'
ESP_FLASH_ARGS = ('--flash_mode dio --flash_freq 80m --flash_size 4MB\n0x0 bootloader/bootloader.bin\n'
                  '0x10000 cdc_msc.bin\n0x8000 partition_table/partition-table.bin\n')
NO_RESET = {'esptool', 'lm4flash'}
# scenario: (runner rcs in turn, seconds each call takes on the fake clock, timeout)
SCENARIOS = {'ok': ([0], 0, 37), 'crash': ([139, -6, 0], 10, 30)}
RESCUES = {'wedged': ('Error: Failed to connect multidrop rp2040.dap0', [0]),
           'wedged-failing': ('Error connecting DP: cannot read IDR', [1]),
           'other-failure': ('Error: flash write failed', [0])}
RESCUE_TIMEOUT = 13


def flashers():
    """(roster, board, role, flasher) for every roster entry, recovery entries resolved as
    tinyusb's recover_flasher does (the primary's uid)."""
    for roster in ROSTERS:
        for boards in json.loads((HERE / roster).read_text()).values():
            for b in boards:
                yield roster, b['name'], 'flasher', b['flasher']
                if 'flasher_recover' in b:
                    yield roster, b['name'], 'flasher_recover', {**b['flasher_recover'], 'uid': b['flasher'].get('uid')}
    yield 'synthetic', 'lpcxpresso55s28', 'flasher', PYOCD


def cases():
    for roster, board, role, flasher in flashers():
        base = {'roster': roster, 'board': board, 'role': role, 'flasher': flasher}
        ops = ['flash'] if flasher['name'].lower() in NO_RESET else ['flash', 'reset']
        for op in ops:
            for scenario, (rcs, took, timeout) in SCENARIOS.items():
                yield {**base, 'op': op, 'scenario': scenario, 'rcs': rcs, 'took': took, 'timeout': timeout}
        for scenario, (flash_out, rcs) in RESCUES.items():
            yield {**base, 'op': 'rescue', 'scenario': scenario, 'flash_out': flash_out, 'rcs': rcs,
                   'took': 0, 'timeout': RESCUE_TIMEOUT}


def key(case) -> tuple:
    return tuple(case[k] for k in ('roster', 'board', 'role', 'op', 'scenario'))


def record(case, module, invoke, normalise_cmd) -> dict:
    """Run invoke(case, run, ctx) in a fresh working directory, `run` answering case['rcs'] in
    turn on a fake clock patched into module.time; return what was observed."""
    calls, results, rcs, now = [], [], iter(case['rcs']), [0.0]
    with tempfile.TemporaryDirectory() as td:
        cwd, fw_dir, project_dir = (Path(td) / d for d in ('cwd', 'fw', 'project'))
        for d in (cwd, fw_dir, project_dir):
            d.mkdir()
        (fw_dir / 'flash_args').write_text(ESP_FLASH_ARGS)
        (fw_dir / 'config.env').write_text(json.dumps({'IDF_TARGET': ESP_CHIP.get(case['board'], '')}))
        ctx = {'fw_dir': fw_dir, 'project_dir': project_dir}

        def run(cmd, **kw):
            script = None
            if isinstance(cmd, str) and '-CommandFile ' in cmd:
                script = Path(shlex.split(cmd.rsplit('-CommandFile ', 1)[1])[0]).read_text()
            elif isinstance(cmd, list) and '-CommandFile' in cmd:
                script = Path(cmd[cmd.index('-CommandFile') + 1]).read_text()
            calls.append({'cmd': normalise_cmd(cmd, ctx), 'kw': kw, 'script': script})
            now[0] += case['took']
            results.append(subprocess.CompletedProcess(cmd, next(rcs), f'out{len(results)}', None))
            return results[-1]

        module._VID_PID_WARNED.clear()
        out, err, old_cwd, old_time = io.StringIO(), io.StringIO(), os.getcwd(), module.time
        os.chdir(cwd)
        module.time = types.SimpleNamespace(monotonic=lambda: now[0])
        try:
            with contextlib.redirect_stdout(out), contextlib.redirect_stderr(err):
                ret = invoke(case, run, ctx)
        finally:
            module.time = old_time
            os.chdir(old_cwd)
        left = sorted(os.listdir(cwd))
        observed = json.loads(json.dumps({
            'calls': calls, 'left': left, 'stdout': out.getvalue(), 'stderr': err.getvalue(),
            'result': ret if isinstance(ret, bool) else next(i for i, r in enumerate(results) if r is ret)}))
        text = json.dumps(observed).replace(str(fw_dir), '{FW_DIR}')
    return json.loads(text)


def _tinyusb_invoke(hf):
    def invoke(case, run, ctx):
        board = {'name': case['board'], 'flasher': case['flasher']}
        name = case['flasher']['name'].lower()
        hf.hil_util.run_cmd = run
        hf.hil_util.get_serial_dev = lambda uid, *_: ESP_PORT.format(uid=uid)
        hf._pyocd_python = lambda: '/py'
        if case['op'] == 'rescue':
            return hf.rescue_openocd(board, case['flash_out'], timeout=case['timeout'])
        if case['op'] == 'reset':
            return hf.reset_primitive(name)(board, timeout=case['timeout'])
        fw = str(ctx['fw_dir'] / 'cdc_msc.bin') if name == 'esptool' else FIRMWARE
        return hf.flash_primitive(name)(board, fw, timeout=case['timeout'])
    return invoke


def _tinyusb_normalise(launcher):
    def normalise(cmd, ctx):
        if isinstance(cmd, list) and cmd[1] == launcher:
            return [cmd[0], '{LAUNCHER}', *cmd[2:]]
        return cmd
    return normalise


def main(argv):
    if len(argv) != 1:
        sys.exit(__doc__)
    hil_dir = Path(argv[0]).resolve() / 'test' / 'hil'
    head = subprocess.run(['git', '-C', str(hil_dir), 'rev-parse', '--short=9', 'HEAD'],
                          capture_output=True, text=True, check=True).stdout.strip()
    dirty = subprocess.run(['git', '-C', str(hil_dir), 'status', '--porcelain', '--', '.'],
                           capture_output=True, text=True, check=True).stdout
    if head != SOURCE or dirty:
        sys.exit(f'want a clean {SOURCE} checkout, got {head}{" (dirty)" if dirty else ""}')
    for roster in ROSTERS:
        if (hil_dir / roster).read_bytes() != (HERE / roster).read_bytes():
            sys.exit(f'{roster} differs from {hil_dir / roster}: copy it first')
    sys.dont_write_bytecode = True
    sys.path.insert(0, str(hil_dir))
    import hil_flash as hf
    invoke = _tinyusb_invoke(hf)
    normalise = _tinyusb_normalise(str(hf.hil_util.TINYUSB_ROOT / 'test/hil/pyocd/run_pyocd.py'))
    rows = [json.dumps({'case': key(c), 'observed': record(c, hf, invoke, normalise)}) for c in cases()]
    GOLDEN.write_text('{"source": %s,\n "cases": [\n%s\n]}\n' % (
        json.dumps(f'tinyusb {SOURCE} test/hil/hil_flash.py; regenerate with gen_golden.py'), ',\n'.join(rows)))
    print(f'{len(rows)} cases -> {GOLDEN}')


if __name__ == '__main__':
    main(sys.argv[1:])
