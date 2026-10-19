#!/usr/bin/env python3
"""The `openocd_esp` flasher (`hil` skill, Flashers): an Espressif chip's built-in
USB-Serial/JTAG (USJ) driven by openocd-esp32. Linux only, stdlib only.

A flasher is {"name": "openocd_esp", "uid": "<USJ serial>", "args": "-f board/<chip>-builtin.cfg"},
or args "-f interface/esp_usb_jtag.cfg -f target/<chip>.cfg"; any other args, a `vid_pid` or
an openocd that is not the verified openocd-esp32 build is refused before anything runs.

Selection: the USJ's bus-port is resolved from sysfs by its serial, exactly one match, and
passed as `adapter usb location` before `adapter serial`, both before init. esp_usb_jtag_init
opens through jtag_libusb_open, which skips a device outside the location before libusb_open
but compares the serial only after opening (libusb_helper.c:272-286), so the serial alone
would open every 303a:1001 ahead of the board's in libusb order. Not convoy-safe even pinned:
esp_usb_jtag_revive_device (esp_usb_jtag.c:313-350, on a transfer error) finds the device
again through jtag_libusb_find_device, which opens every 303a:1001 to read its serial and
ignores the location (libusb_helper.c:500-540). openocd-esp32 source citations are at tag
v0.12.0-esp32-20260703.
"""

from __future__ import annotations

import glob
import importlib.util
import os
import re
import shlex
import shutil
import subprocess

HERE = os.path.dirname(os.path.abspath(__file__))


def _load(name, path):
    spec = importlib.util.spec_from_file_location(f'agentrc_{name}', path)
    mod = importlib.util.module_from_spec(spec)
    spec.loader.exec_module(mod)
    return mod


usb_observe = _load('usb_observe', os.path.join(HERE, 'usb_observe.py'))

# The build whose program_esp, flash read_bank, esp_usb_jtag and shutdown paths were read; another
# build is refused until its source is checked again.
VERIFIED_OPENOCD = 'v0.12.0-esp32-20260703'
OPENOCD_ENV = 'HIL_OPENOCD_ESP'
USJ_VID_PID = ('303a', '1001')       # interface/esp_usb_jtag.cfg's `adapter usb vid_pid`
# adapter.c:324-331 warns about a location of 16 characters and truncates a longer one
USB_LOCATION_MAX = 15

_UID_RE = re.compile(r'[A-Za-z0-9_.:+-]+')
_CHIP = r'esp32[a-z0-9]*'
_BUSPORT_RE = re.compile(r'[0-9]+-[0-9]+(\.[0-9]+)*')


def admit(flasher: dict) -> tuple:
    """(chip, cfg files) of an `openocd_esp` flasher, or ValueError: `args` names one board cfg
    or the USJ interface and a target cfg, nothing that selects an adapter, runs a command or
    initialises early."""
    if (flasher.get('name') or '') != 'openocd_esp':
        raise ValueError(f'not an openocd_esp flasher: {flasher.get("name")!r}')
    uid = flasher.get('uid')
    if not isinstance(uid, str) or not _UID_RE.fullmatch(uid):
        raise ValueError(f'openocd_esp uid {uid!r}: want the USJ serial (letters, digits and _ . : + -)')
    if 'vid_pid' in flasher:
        raise ValueError('openocd_esp takes no vid_pid: interface/esp_usb_jtag.cfg selects 303a:1001 and the '
                         'bus-port pins the device')
    args = shlex.split(flasher.get('args') or '')
    m = len(args) == 2 and args[0] == '-f' and re.fullmatch(rf'board/({_CHIP})-builtin\.cfg', args[1])
    if m:
        return m[1], [args[1]]
    m = (len(args) == 4 and args[:3] == ['-f', 'interface/esp_usb_jtag.cfg', '-f']
         and re.fullmatch(rf'target/({_CHIP})\.cfg', args[3]))
    if m:
        return m[1], [args[1], args[3]]
    raise ValueError(f'openocd_esp args {flasher.get("args")!r}: want "-f board/<chip>-builtin.cfg" or '
                     f'"-f interface/esp_usb_jtag.cfg -f target/<chip>.cfg" and nothing else')


def find_openocd(openocd: str | None = None) -> dict:
    """{exe, banner, scripts} of the verified openocd-esp32: `openocd`, else $HIL_OPENOCD_ESP,
    else `openocd` on PATH. Its scripts are the install's own share/openocd/scripts, passed
    with -s, so another install's cfgs are never sourced. RuntimeError otherwise."""
    name = openocd or os.environ.get(OPENOCD_ENV) or 'openocd'
    exe = shutil.which(name)
    if not exe:
        raise RuntimeError(f'{name} not found: pass openocd= or set {OPENOCD_ENV}')
    try:
        r = subprocess.run([exe, '--version'], capture_output=True, text=True, timeout=10, stdin=subprocess.DEVNULL)
    except (OSError, subprocess.TimeoutExpired) as e:
        raise RuntimeError(f'{exe} --version: {e}') from None
    lines = (r.stdout + r.stderr).strip().splitlines()
    banner = lines[0] if lines else ''
    if VERIFIED_OPENOCD not in banner.split():
        raise RuntimeError(f'{exe}: banner {banner!r} is not openocd-esp32 {VERIFIED_OPENOCD}, the build this '
                           f'backend was checked against')
    scripts = os.path.normpath(os.path.join(os.path.dirname(os.path.realpath(exe)), '..', 'share', 'openocd',
                                            'scripts'))
    try:
        with open(os.path.join(scripts, 'target', 'esp_common.cfg')) as f:
            common = f.read()
    except OSError as e:
        raise RuntimeError(f'{exe}: no openocd-esp32 scripts beside it ({e})') from None
    if 'proc program_esp ' not in common:
        raise RuntimeError(f'{scripts}/target/esp_common.cfg defines no program_esp')
    return {'exe': exe, 'banner': banner, 'scripts': scripts}


def check_cfgs(chip: str, cfgs: list, scripts: str) -> None:
    """The route's cfgs exist in the verified scripts, the chip has a USJ board cfg there, and
    that board cfg only sources the USJ interface and the chip's target (no selector, no init)."""
    board = os.path.join(scripts, 'board', f'{chip}-builtin.cfg')
    try:
        with open(board) as f:
            lines = [ln.strip() for ln in f if ln.strip() and not ln.lstrip().startswith('#')]
    except OSError:
        raise ValueError(f'{chip}: openocd-esp32 has no board/{chip}-builtin.cfg, so no built-in USB-JTAG') from None
    if lines != ['source [find interface/esp_usb_jtag.cfg]', f'source [find target/{chip}.cfg]']:
        raise ValueError(f'{board}: not the plain USJ board cfg (sources more than the interface and target)')
    for cfg in cfgs:
        if not os.path.isfile(os.path.join(scripts, cfg)):
            raise ValueError(f'{cfg} is not in {scripts}')


def _attr(d, name):
    # idVendor, idProduct, busnum and devnum are lock-free sysfs_emit reads of cached fields
    with open(os.path.join(d, name)) as f:
        return f.read().strip()


def _usj_walk(serial: str) -> list:
    found, unknown = [], []
    for d in sorted(glob.glob(os.path.join(usb_observe.SYS_USB_DEVICES, '*-*'))):
        busport = os.path.basename(d)
        if ':' in busport:
            continue                  # an interface, not a device
        try:
            if (_attr(d, 'idVendor'), _attr(d, 'idProduct')) != USJ_VID_PID:
                continue
        except OSError:
            continue                  # gone before it was identified as a USJ
        try:
            bus, dev = int(_attr(d, 'busnum')), int(_attr(d, 'devnum'))
        except (OSError, ValueError):
            raise RuntimeError(f'USJ at {busport} disappeared during the walk') from None
        # udev's record first: `serial` is served under the device lock (usb_observe.read_sysfs)
        sn = usb_observe.udev_serial(bus, dev)
        if sn is None:
            sn = usb_observe.read_sysfs(os.path.join(d, 'serial'))
        if sn is None:
            unknown.append(busport)
        elif sn == serial:            # openocd compares exactly (libusb_helper.c string_descriptor_equal)
            found.append((busport, bus, dev))
    if unknown:
        raise RuntimeError(f'USJ serial {serial}: cannot rule out {", ".join(unknown)} (serial unreadable)')
    if not found:
        raise RuntimeError(f'USJ serial {serial}: no 303a:1001 with that serial')
    if len(found) > 1:
        raise RuntimeError(f'USJ serial {serial}: ambiguous, at {", ".join(f[0] for f in found)}')
    return found


def usj_location(serial: str) -> str:
    """The bus-port of the one USJ with `serial`, for `adapter usb location`; RuntimeError when
    it is missing, ambiguous, unidentifiable or changes between two walks. No serial-only
    fallback."""
    first = _usj_walk(serial)
    if _usj_walk(serial) != first:
        raise RuntimeError(f'USJ serial {serial}: moved or re-enumerated during resolution ({first[0][0]})')
    busport = first[0][0]
    if not _BUSPORT_RE.fullmatch(busport) or len(busport) > USB_LOCATION_MAX:
        raise RuntimeError(f'USJ serial {serial}: bus-port {busport!r} is not a location openocd can match')
    return busport


def server_argv(tool: dict, cfgs: list, location: str, serial: str, *, tcl_port: int, token: str,
                log: str) -> list:
    """The one place the configuration order is decided: ports and the session token, then the
    route with `adapter usb location` and `adapter serial` after the interface cfg (inside a
    board cfg, or before the target cfg) and `noinit`, so init runs only when asked over Tcl.
    Each cfg is named by its path in the verified scripts, never relative to a cwd."""
    selectors = ['-c', f'adapter usb location {location}', '-c', f'adapter serial {serial}']
    files = [os.path.join(tool['scripts'], cfg) for cfg in cfgs]
    route = ['-f', files[0], *selectors] + [x for f in files[1:] for x in ('-f', f)]
    return [tool['exe'], '-s', tool['scripts'], '-c', 'gdb port disabled', '-c', 'telnet port disabled',
            '-c', f'tcl port {tcl_port}', '-c', f'set ESPS_SESSION {token}', *route, '-c', 'noinit', '-l', log]
