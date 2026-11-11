#!/usr/bin/env python3
"""Mechanics of debug-probe maintenance: which probes are attached, a probe's firmware update or recovery, and a
target chip's config word through its ISP bootloader. Judgment (which tool, when, what a value means) is in SKILL.md.

    probe.py probes [--version] [--json]               # serial, sysfs port, mode, wlink index (+ firmware vNN)
    probe.py fw-update --serial S --image F            # the probe at S, image known to tools.toml
    probe.py fw-recover --port 5-2.5 --model WCH-LinkE-CH32V305 --image F   # a probe stuck in IAP mode
    probe.py config --chip CH582 --show [--wait 30]    # the chip's config word, decoded
    probe.py config --chip CH582 --set CFG_RESET_EN=1  # change named fields only, write, read back

Every probe is selected by serial or USB port, never by index, and the backends (patched wlink-iap and wchisp,
built per SKILL.md) enforce the same selection. A probe's firmware version comes from `wlink-iap -V`, which never
attaches to the target. fw-update refuses an image whose sha256 tools.toml does not list for that model, a
downgrade without --allow-downgrade, the same version without --reinstall, and fails unless the same serial comes
back at the same port on the image's version. config needs exactly one WCH ISP device (4348:55e0 or 1a86:55e0) on
the bus, the same one after `wchisp info` and before the write; it changes only the fields CHIPS allows, refuses a
word whose fixed bits are off, and hands wchisp the UID and current value so the write aborts if either changed."""
import argparse
import hashlib
import json
import os
import re
import subprocess
import sys
import tempfile
import time
import tomllib
from pathlib import Path

sys.path.insert(0, str(Path(__file__).resolve().parents[2] / 'hil' / 'scripts'))
import usb_observe  # noqa: E402   bounded sysfs reads: a wedged device's `serial` cannot stall a scan

TOOLS = Path(__file__).resolve().parents[1] / 'tools.toml'
DEV_USB = '/dev/bus/usb'
MODES = {('1a86', '8010'): 'rv', ('1a86', '8012'): 'dap', ('4348', '55e0'): 'isp', ('1a86', '55e0'): 'isp'}
ANSI = re.compile(r'\x1b\[[0-9;]*m')


class Refused(Exception):
    pass


# chip as wchisp names it: the config word set-word may replace; fields this script may change as
# (high bit, low bit, values a written word may hold); bits every word must carry as (mask, value, why);
# bit combinations a written word must not hold as (mask, value, why);
# complement: byte 3 is the inverse of byte 2 (USER/nUSER), recomputed on write
CHIPS = {
    'CH582': {'register': 'USER_CFG', 'device_type': 0x16, 'chip_id': 0x82, 'complement': False, 'forbidden': [],
              'fields': {'CFG_RESET_EN': (3, 3, {0, 1}), 'CFG_DEBUG_EN': (4, 4, {0, 1}),
                         'CFG_ROM_READ': (7, 7, {1})},   # 0 read-protects the flash
              'fixed': [(0xF0000000, 0x40000000, 'VALID_SIG is not 0100b'), (0x40, 0x40, 'CFG_BOOT_EN is off')]},
    'CH32X035C8T6': {'register': 'RDPR_USER', 'device_type': 0x23, 'chip_id': 81, 'complement': True, 'forbidden': [],
                     'fields': {'RST_MODE': (20, 19, {0, 3})},
                     'fixed': [(0xFFFF, 0x5AA5, 'RDPR/nRDPR is not the unprotected A5/5A')]},
    'CH569': {'register': 'NV_INFO', 'device_type': 0x10, 'chip_id': 0x69, 'complement': False,
              'fields': {'RESET_EN': (4, 4, {0, 1}), 'DEBUG_EN': (5, 5, {0, 1}),
                         'CODE_READ_EN': (7, 7, {1})},   # 0 bars an external programmer from reading flash
              'fixed': [(0xF, 0x5, 'bits [3:0] are not 0101b'), (0x40, 0x40, 'BOOT_EN is off')],
              'forbidden': [(0x30, 0x30, 'DEBUG_EN with RESET_EN: debug then fails (CH569DS1 Table 2-2)')]},
}


def run(argv, timeout=120):
    try:
        p = subprocess.run(argv, capture_output=True, text=True, timeout=timeout)
    except FileNotFoundError:
        raise Refused(f'{argv[0]} is not on PATH; build it per SKILL.md') from None
    except subprocess.TimeoutExpired:
        raise Refused(f'{" ".join(argv)} did not finish in {timeout} s') from None
    return p.returncode, ANSI.sub('', p.stdout + p.stderr)


def usb_devices():
    """[{port, vid, pid, mode, serial, node}] of each WCH probe or ISP device; serial '' when absent or it did not
    answer, node its usbfs path or None when busnum/devnum did not read."""
    found = []
    for (vid, pid), mode in MODES.items():
        for dev in usb_observe.usb_devices((vid, pid)):
            serial = '' if mode == 'isp' else usb_observe.read_sysfs(os.path.join(dev['dir'], 'serial')) or ''
            node = (None if dev['busnum'] is None or dev['devnum'] is None
                    else os.path.join(DEV_USB, f'{dev["busnum"]:03d}', f'{dev["devnum"]:03d}'))
            found.append({'port': dev['busport'], 'vid': vid, 'pid': pid, 'mode': mode, 'serial': serial,
                          'node': node})
    return sorted(found, key=lambda d: d['port'])


def wlink_indexes():
    """serial -> `wlink -d` index; empty when wlink is absent (the index is informational only)."""
    try:
        _, out = run(['wlink', 'list'], timeout=30)
    except Refused:
        return {}
    return {m[2]: int(m[1]) for m in re.finditer(r'WCH-Link#(\d+).*?Serial (\w+)', out)}


def require_patched_wlink_iap():
    _, out = run(['wlink-iap'], timeout=10)
    if '-p <port>' not in out:
        raise Refused('wlink-iap is not the patched build (no -p); build it per SKILL.md')


def probe_version(port, serial=None):
    argv = ['wlink-iap', '-p', port, '-V'] + (['-s', serial] if serial else [])
    code, out = run(argv, timeout=30)
    m = re.search(r'model=(\S+) version=(\d+\.\d+) v=(\d+)', out)
    if code or not m:
        raise Refused(f'{" ".join(argv)} failed: {out.strip()}')
    return {'model': m[1], 'version': m[2], 'v': int(m[3])}


def cmd_probes(args):
    devices = usb_devices()
    if args.version:
        require_patched_wlink_iap()
    index = wlink_indexes() if any(d['mode'] != 'isp' for d in devices) else {}
    for d in devices:
        d['index'] = index.get(d['serial'])
        if args.version and d['mode'] in ('rv', 'dap'):
            d.update(probe_version(d['port'], d['serial'] or None))
    if args.json:
        print(json.dumps(devices, indent=2))
        return 0
    for d in devices:
        what = 'ISP bootloader (target chip) or probe in IAP mode' if d['mode'] == 'isp' else 'WCH-Link'
        ver = f' {d["model"]} v{d["version"]} (v{d["v"]})' if 'v' in d else ''
        idx = '' if d['index'] is None else f' wlink -d {d["index"]}'
        print(f'{d["port"]:10} {d["vid"]}:{d["pid"]} {d["mode"]:4} {d["serial"] or "(no serial)":14}{idx}{ver}  {what}')
    return 0


def firmware_entry(data, image, model):
    digest = hashlib.sha256(data).hexdigest()
    entries = tomllib.loads(TOOLS.read_text())['firmware']
    match = [e for e in entries if e['sha256'] == digest]
    if not match:
        raise Refused(f'{image} (sha256 {digest}) is not an image tools.toml lists; app-only images only')
    if match[0]['model'] != model:
        raise Refused(f'{image} is firmware for {match[0]["model"]}, the probe is {model}')
    return match[0]


def node_ready(dev):
    """Whether this process may open the device's usbfs node: udev can set its permissions after sysfs
    shows the device (an open right after re-enumeration failed LIBUSB_ERROR_ACCESS on ci.lan, 2026-11-11)."""
    return bool(dev['node']) and os.access(dev['node'], os.R_OK | os.W_OK)


def wait_back(port, serial, timeout):
    """Wait for the probe in RV mode at `port`, its usbfs node openable."""
    deadline = time.monotonic() + timeout
    what = f'RV-mode probe{"" if serial is None else " " + serial}'
    while True:
        back = [d for d in usb_devices()
                if d['port'] == port and d['mode'] == 'rv' and (serial is None or d['serial'] == serial)]
        if any(node_ready(d) for d in back):
            return
        if time.monotonic() >= deadline:
            if back:
                raise Refused(f'{what} back at {port} but its usbfs node is not accessible after {timeout} s')
            raise Refused(f'no {what} back at {port} within {timeout} s')
        time.sleep(0.2)


def flash(argv, data, port, serial, entry, timeout):
    """Flash a private copy of the bytes whose hash was checked, then require the probe back on the image's version."""
    with tempfile.TemporaryDirectory() as tmp:
        snapshot = Path(tmp) / 'firmware.bin'
        snapshot.write_bytes(data)
        code, out = run([*argv, '-f', str(snapshot)], timeout=300)
    print(out.rstrip())
    if code:
        raise Refused(f'{" ".join(argv)} -f <image> exited {code}')
    wait_back(port, serial, timeout)
    after = probe_version(port, serial)
    if (after['model'], after['v']) != (entry['model'], entry['v']):
        raise Refused(f'probe at {port} reports {after["model"]} v{after["v"]} after flashing a '
                      f'{entry["model"]} v{entry["v"]} image')
    return after


def cmd_fw_update(args):
    require_patched_wlink_iap()
    probes = [d for d in usb_devices() if d['serial'] == args.serial and d['mode'] in ('rv', 'dap')]
    if len(probes) != 1:
        raise Refused(f'{len(probes)} RV/DAP-mode probes with serial {args.serial}; need exactly one')
    port = probes[0]['port']
    before = probe_version(port, args.serial)
    data = Path(args.image).read_bytes()
    entry = firmware_entry(data, args.image, before['model'])
    if entry['v'] < before['v'] and not args.allow_downgrade:
        raise Refused(f'{args.image} is v{entry["v"]}, the probe runs v{before["v"]}: pass --allow-downgrade')
    if entry['v'] == before['v'] and not args.reinstall:
        raise Refused(f'the probe already runs v{entry["v"]}: pass --reinstall')
    after = flash(['wlink-iap', '-p', port, '-s', args.serial], data, port, args.serial, entry, args.timeout)
    print(f'{args.serial} at {port}: v{before["v"]} -> v{after["v"]}')
    return 0


def cmd_fw_recover(args):
    require_patched_wlink_iap()
    iap = [d for d in usb_devices() if d['port'] == args.port and (d['vid'], d['pid']) == ('4348', '55e0')]
    if not iap:
        raise Refused(f'no 4348:55e0 device at {args.port}')
    if not node_ready(iap[0]):
        raise Refused(f'4348:55e0 at {args.port}: its usbfs node {iap[0]["node"]} is not accessible')
    data = Path(args.image).read_bytes()
    entry = firmware_entry(data, args.image, args.model)
    after = flash(['wlink-iap', '-p', args.port, '-r'], data, args.port, None, entry, args.timeout)
    print(f'probe at {args.port}: unknown -> v{after["v"]}')
    return 0


def wait_one_isp_device(wait):
    deadline = time.monotonic() + wait
    while True:
        isp = [d for d in usb_devices() if d['mode'] == 'isp']
        if len(isp) > 1:
            raise Refused(f'{len(isp)} ISP devices on the bus ({", ".join(d["port"] for d in isp)}); leave one')
        if isp and node_ready(isp[0]):
            return isp[0]['port']
        if time.monotonic() >= deadline:
            if isp:
                raise Refused(f'ISP device at {isp[0]["port"]} but its usbfs node is not accessible after {wait} s')
            raise Refused(f'no WCH ISP device appeared within {wait} s; enter the bootloader per chips/wch.md')
        time.sleep(0.2)


def require_only_isp_device(port, when):
    """wchisp opens the first ISP device libusb lists, not a port: one appearing since the guard could be the one
    it read or writes."""
    isp = [d['port'] for d in usb_devices() if d['mode'] == 'isp']
    if isp != [port]:
        raise Refused(f'ISP devices {when}: {", ".join(isp) or "none"}, not only the one at {port}; leave one')


def chip_word(spec, chip):
    code, out = run(['wchisp', '-u', 'info'], timeout=30)
    m = re.search(r'Chip: (\S+)\[0x([0-9a-f]{2})([0-9a-f]{2})\]', out)
    uid = re.search(r'Chip UID: ([0-9A-F-]+)', out)
    word = re.search(rf'^{spec["register"]}: 0x([0-9A-F]{{8}})', out, re.M)
    if code or not (m and uid and word):
        raise Refused(f'wchisp info failed: {out.strip()}')
    if (m[1], int(m[2], 16), int(m[3], 16)) != (chip, spec['chip_id'], spec['device_type']):
        raise Refused(f'the ISP device is {m[1]}[0x{m[2]}{m[3]}], not {chip}')
    return uid[1], int(word[1], 16)


def decode(spec, word):
    return {name: word >> lo & (1 << hi - lo + 1) - 1 for name, (hi, lo, _) in spec['fields'].items()}


def check_word(spec, word, which):
    for mask, value, why in spec['fixed']:
        if word & mask != value:
            raise Refused(f'{which} {spec["register"]} 0x{word:08X}: {why}')
    if spec['complement'] and word >> 24 != (word >> 16 & 0xff) ^ 0xff:
        raise Refused(f'{which} {spec["register"]} 0x{word:08X}: byte 3 is not the inverse of byte 2')


def apply_fields(spec, word, assignments):
    for item in assignments.split(','):
        name, _, value = item.partition('=')
        if name not in spec['fields'] or not value:
            raise Refused(f'{item!r}: settable fields are {", ".join(spec["fields"])}')
        hi, lo, _ = spec['fields'][name]
        width = hi - lo + 1
        v = int(value, 0)
        if not 0 <= v < 1 << width:
            raise Refused(f'{name} is {width} bit(s); {v} does not fit')
        word = word & ~((1 << width) - 1 << lo) | v << lo
    if spec['complement']:
        user = word >> 16 & 0xff
        word = word & 0xffffff | (user ^ 0xff) << 24
    for name, got in decode(spec, word).items():
        if got not in spec['fields'][name][2]:
            allowed = ', '.join(map(str, sorted(spec['fields'][name][2])))
            raise Refused(f'{name}={got} is not allowed here (allowed: {allowed})')
    for mask, value, why in spec['forbidden']:
        if word & mask == value:
            raise Refused(f'new {spec["register"]} 0x{word:08X}: {why}')
    check_word(spec, word, 'new')
    return word


def cmd_config(args):
    spec = CHIPS[args.chip]
    _, out = run(['wchisp', 'config', '--help'], timeout=10)
    if 'set-word' not in out:
        raise Refused('wchisp is not the patched build (no config set-word); build it per SKILL.md')
    port = wait_one_isp_device(args.wait)
    uid, word = chip_word(spec, args.chip)
    require_only_isp_device(port, 'after wchisp info')
    check_word(spec, word, 'current')
    print(f'{args.chip} UID {uid} {spec["register"]} 0x{word:08X} {decode(spec, word)}')
    if args.show:
        return 0
    new = apply_fields(spec, word, args.set)
    if new == word:
        print('nothing to change')
        return 0
    require_only_isp_device(port, 'before set-word')
    code, out = run(['wchisp', '-u', 'config', 'set-word', '--register', spec['register'], '--expect-uid', uid,
                     '--expect', f'{word:08X}', '--value', f'{new:08X}'], timeout=30)
    print(out.rstrip())
    if code:
        raise Refused(f'wchisp config set-word exited {code}')
    print(f'{spec["register"]} 0x{word:08X} -> 0x{new:08X} {decode(spec, new)}; power-cycle out of the bootloader, '
          'check the pin, and --show again to confirm it persisted')
    return 0


def main():
    p = argparse.ArgumentParser(description=__doc__, formatter_class=argparse.RawDescriptionHelpFormatter)
    sub = p.add_subparsers(dest='cmd', required=True)
    s = sub.add_parser('probes')
    s.add_argument('--version', action='store_true', help='read each probe\'s firmware (wlink-iap -V)')
    s.add_argument('--json', action='store_true')
    s = sub.add_parser('fw-update')
    s.add_argument('--serial', required=True)
    s.add_argument('--image', required=True)
    s.add_argument('--allow-downgrade', action='store_true')
    s.add_argument('--reinstall', action='store_true')
    s.add_argument('--timeout', type=int, default=30, help='seconds for the probe to come back')
    s = sub.add_parser('fw-recover')
    s.add_argument('--port', required=True, help='sysfs port of the 4348:55e0 device, e.g. 5-2.5')
    s.add_argument('--model', required=True, help='probe model as -V prints it, e.g. WCH-LinkE-CH32V305')
    s.add_argument('--image', required=True)
    s.add_argument('--timeout', type=int, default=30)
    s = sub.add_parser('config')
    s.add_argument('--chip', required=True, choices=sorted(CHIPS))
    g = s.add_mutually_exclusive_group(required=True)
    g.add_argument('--show', action='store_true')
    g.add_argument('--set', metavar='FIELD=V[,FIELD=V]')
    s.add_argument('--wait', type=int, default=0, help='seconds to wait for the chip to enter its bootloader')
    args = p.parse_args()
    handler = {'probes': cmd_probes, 'fw-update': cmd_fw_update, 'fw-recover': cmd_fw_recover,
               'config': cmd_config}[args.cmd]
    try:
        return handler(args)
    except Refused as e:
        print(f'probe.py: {e}', file=sys.stderr)
        return 1


if __name__ == '__main__':
    sys.exit(main())
