#!/usr/bin/env python3
"""Flash and reset commands per flasher backend, shared by the HIL harnesses (`hil` skill,
Flashers). Linux only, stdlib only.

Every operation takes a flasher dict (`name`, `uid`, `args`, optional `vid_pid` and
`verify`), never a board, roster, example or build root; it builds the backend's command and
runs it through `run` (default hil_process.run_cmd, the instance loaded here as
`hil_flash.hil_process`), called as `run(cmd, timeout=...)` or, for esptool,
`run(cmd, cwd=..., timeout=...)`, so a caller injects its own runner. Every operation takes
`timeout` explicitly. The result is the runner's CompletedProcess: rc 0 means the tool said
it succeeded, not that the target runs the image; rc 124 means it was killed on the bound.
"""

from __future__ import annotations

import contextlib
import importlib.util
import os
import re
import shlex
import shutil
import signal
import subprocess
import sys
import tempfile
import time
from pathlib import Path

HERE = os.path.dirname(os.path.abspath(__file__))
PYOCD_LAUNCHER = os.path.join(HERE, 'pyocd', 'run_pyocd.py')


def _load(name):
    spec = importlib.util.spec_from_file_location(f'agentrc_{name}', os.path.join(HERE, f'{name}.py'))
    mod = importlib.util.module_from_spec(spec)
    spec.loader.exec_module(mod)
    return mod


hil_process = _load('hil_process')

_VID_PID_WARNED: set = set()   # one warning per probe, not per command


def _runner(run):
    return hil_process.run_cmd if run is None else run


# -------------------------------------------------------------
# J-Link
# -------------------------------------------------------------
# libjlinkarm.so (V9.66 and V9.78) corrupts its heap on on-board J-Link probes (OB, LPC-Link 2,
# reflashed ST-Link) and dies by SIGSEGV or SIGABRT ("double free"), 3 of 810 runs on feather's OB.
# Every crash seen came right after reading the probe's S/N, before target access, so the whole
# script can rerun. rc is 128+N through the shell, -N when it execs directly.
JLINK_CRASH_RCS = {128 + signal.SIGSEGV, -signal.SIGSEGV, 128 + signal.SIGABRT, -signal.SIGABRT}


@contextlib.contextmanager
def _command_file(script: list):
    # A unique file per invocation in the current directory: a stale or concurrent file is
    # never the one run, and none outlives the invocation.
    fd, path = tempfile.mkstemp(prefix='hil_jlink_', suffix='.jlink', dir=os.getcwd())
    try:
        with os.fdopen(fd, 'w') as f:
            f.writelines(f'{s}\n' for s in script)
        yield path
    finally:
        Path(path).unlink(missing_ok=True)


def _run_jlink(flasher: dict, script: list, *, timeout, crash_retries: int,
               run) -> subprocess.CompletedProcess:
    # crash_retries reruns share one deadline, taken once the command file is written.
    # A rerun is no proof the target was untouched.
    run = _runner(run)
    with _command_file(script) as path:
        cmd = (f'JLinkExe -USB {flasher["uid"]} {flasher["args"]} -if swd -JTAGConf -1,-1 -speed auto '
               f'-NoGui 1 -ExitOnError 1 -CommandFile {shlex.quote(path)}')
        deadline = time.monotonic() + timeout
        ret = run(cmd, timeout=timeout)
        for _ in range(crash_retries):
            left = deadline - time.monotonic()
            if ret.returncode not in JLINK_CRASH_RCS or left <= 0:
                break
            print(f'JLinkExe -USB {flasher["uid"]} crashed (rc {ret.returncode}), rerunning', flush=True)
            ret = run(cmd, timeout=left)
    return ret


def flash_jlink(flasher: dict, firmware: str, *, timeout,
                crash_retries: int = 0, run=None) -> subprocess.CompletedProcess:
    script = ['halt', 'r', f'loadfile {firmware}', 'r', 'go', 'exit']
    return _run_jlink(flasher, script, timeout=timeout, crash_retries=crash_retries, run=run)


def reset_jlink(flasher: dict, *, timeout,
                crash_retries: int = 0, run=None) -> subprocess.CompletedProcess:
    script = ['halt', 'r', 'go', 'exit']
    return _run_jlink(flasher, script, timeout=timeout, crash_retries=crash_retries, run=run)


# -------------------------------------------------------------
# ST-Link
# -------------------------------------------------------------
def flash_stlink(flasher: dict, firmware: str, *, timeout, run=None) -> subprocess.CompletedProcess:
    # --verify catches the partial/corrupt write that exits 0 and sends the test phase
    # off to exercise bad firmware. Opt-IN here ("verify": true), unlike flash_openocd's
    # opt-out: a default-on read-back silently changes every roster entry that lacks the
    # key, including boards on rigs this was never validated against.
    verify = ' --verify' if flasher.get('verify', False) else ''
    return _runner(run)(f'STM32_Programmer_CLI --connect port=swd sn={flasher["uid"]} --write {firmware}{verify} --go',
                        timeout=timeout)


def reset_stlink(flasher: dict, *, timeout, run=None) -> subprocess.CompletedProcess:
    return _runner(run)(f'STM32_Programmer_CLI --connect port=swd sn={flasher["uid"]} --rst --go',
                        timeout=timeout)


# -------------------------------------------------------------
# OpenOCD
# -------------------------------------------------------------
def openocd_cmd_base(flasher: dict) -> str:
    # Optional roster field vid_pid, openocd-verbatim (e.g. "0x1a86 0x8010"), pins probe
    # discovery to the probe's IDs so openocd never opens foreign usbfs nodes to read
    # strings -- a wedged node makes that open hang unkillably (the 2026-08-10 convoy).
    # BEFORE args, because the rescue cfgs run `init` internally and reject (or never see)
    # a config command that follows it.
    vid_pid = ''
    if 'vid_pid' in flasher:
        # Validated HERE too, not just in convoy_safe: openocd only warns ("incomplete
        # vid_pid configuration directive") and exits 0 on a malformed value, so the pin
        # silently does not apply and discovery goes back to opening every usbfs node --
        # the convoy this field exists to stop. The same key name carries a DIFFERENT
        # syntax under tinyusb's tests.dev_attached ('1a86_55d4'), so the typo is one copy away.
        if valid_vid_pid(flasher['vid_pid']):
            vid_pid = f'-c "adapter usb vid_pid {flasher["vid_pid"]}" '
        else:
            # stderr + once-per-probe, like the missing-pin branch below: a harness that
            # captures stdout per test shows it only when the test FAILS, so on a PASSING
            # run the operator would never learn the pin was silently dropped.
            uid = flasher.get('uid', '?')
            if uid not in _VID_PID_WARNED:
                _VID_PID_WARNED.add(uid)
                print(f'warning: {uid} has a malformed vid_pid {flasher["vid_pid"]!r} '
                      f'(want "0xVVVV 0xPPPP"); probe pin DROPPED, so discovery will open '
                      f'foreign usbfs nodes', file=sys.stderr, flush=True)
    elif not convoy_safe(flasher) and flasher.get('uid') not in _VID_PID_WARNED:
        # (unpinned yet convoy-safe is openocd over a CONVOY_SAFE_CFGS driver: no warning)
        # stderr, once per probe: a captured stdout would swallow this on a passing run and
        # the operator would never learn discovery still opens every usbfs node
        _VID_PID_WARNED.add(flasher.get('uid'))
        print(f'warning: openocd flasher {flasher.get("uid", "?")} has no vid_pid pin; '
              f'probe discovery will open every usbfs node (hangs on a wedged one)',
              file=sys.stderr, flush=True)
    return (f'openocd -c "tcl_port disabled" -c "gdb_port disabled" -c "telnet_port disabled" '
            f'-c "adapter serial {flasher["uid"]}" {vid_pid}{flasher["args"]}')


# `verify` is on by default, opted out per board with "verify": false. ch32v103r and
# ch582m opt out: a raw dump_image of their flash is not the image just written (~85% of
# bytes differ, the same at 0x0 and 0x08000000, shifting with halt vs reset halt, and
# abstract memory reads fail), so verify_image always fails there.
# ch32v203 and ch32v307 read back byte-exact and verify, and catch a one-byte negative
# control, over progbuf and abstract alike (ci.lan, 2026-09-21). Do NOT drop verify
# fleet-wide -- without it a partial or corrupt write exits 0 and the tests run bad firmware.
# `program` = init; reset init; write_image erase; verify; reset run. Its reset-init event is
# what some targets need (kx.cfg's `kinetis disable_wdog`; without it frdm_k64f's verify takes
# 21 s). stm32f0x/f4x/f7x/l4x.cfg raise `adapter speed` to 8000 there, which their J-Link
# recovery entries cannot run, so those entries empty the event in the roster instead.
def flash_openocd(flasher: dict, firmware: str, *, timeout, run=None) -> subprocess.CompletedProcess:
    verify = ' verify' if flasher.get('verify', True) else ''
    return _runner(run)(f'{openocd_cmd_base(flasher)} -c "program {firmware}{verify} reset exit"',
                        timeout=timeout)


def reset_openocd(flasher: dict, *, timeout, run=None) -> subprocess.CompletedProcess:
    # timeout: a post-hang recovery bounds this; an unbounded reset there would outlive the
    # caller's outer kill and orphan openocd on the probe, which is the stray the recovery
    # exists to avoid.
    return _runner(run)(f'{openocd_cmd_base(flasher)} -c "init; reset run; exit"',
                        timeout=timeout)


# openocd interface cfgs whose discovery filters by VID/PID before opening; see convoy_safe
CONVOY_SAFE_CFGS = ('interface/jlink.cfg', 'interface/stlink.cfg', 'interface/ti-icdi.cfg')


# OpenOCD's messages for "the target's debug port did not answer". The probe is fine when
# these appear ("CMSIS-DAP: Interface ready" is still logged); the chip's debug clock is
# gone, which no probe-driven reset fixes -- the CMSIS-DAP probe has no nRESET line. Which
# message appears depends on DAP topology, not the board, so both are accepted for both
# chips; RESCUE_CFG below picks the rescue.
DAP_WEDGED = ('Failed to connect multidrop', 'Error connecting DP: cannot read IDR')

# How each RP target reaches its Rescue DP, keyed by the target cfg named in flasher args:
# (cfg substitution, pre args, post args). rp2040.cfg drives the Rescue DP behind a RESCUE
# flag and init/shutdowns itself; rp2350-rescue.cfg never shuts down, so it needs an
# explicit one or it sits in the server loop until the timeout.
RESCUE_CFG = {
    'target/rp2040.cfg': ('target/rp2040.cfg', '-c "set RESCUE 1" ', ''),
    'target/rp2350.cfg': ('target/rp2350-rescue.cfg', '', ' -c "shutdown"'),
}


def rescue_openocd(flasher: dict, flash_out: str = '', *, timeout, run=None) -> bool:
    """Power-on-reset a wedged RP2040/RP2350 through its Rescue DP, the one debug port not
    gated by the system clock (RP2040 datasheet 2.3.4.2): CDBGPWRUPREQ hard-resets the
    chip and the bootrom halts it ready to be flashed. Without it the board needs a
    physical replug -- the probe carries no reset line.

    No-op (False) unless this is an openocd RP flasher AND the flash output shows the wedge,
    so a flash that failed for any other reason still just retries. True when the rescue
    command exited 0, which is not proof the chip came back; the caller retries the flash
    afterwards either way."""
    if flasher['name'].lower() != 'openocd' or not any(m in flash_out for m in DAP_WEDGED):
        return False
    for cfg, (rescue_cfg, pre, post) in RESCUE_CFG.items():
        if cfg in flasher['args']:
            args = flasher['args'].replace(cfg, rescue_cfg)
            return _runner(run)(f'{openocd_cmd_base({**flasher, "args": pre + args})}{post}',
                                timeout=timeout).returncode == 0
    return False


# openocd's own syntax: one or more "0xVVVV 0xPPPP" pairs. Validated rather than merely
# tested for truthiness -- `vid_pid` is a hand-edited roster field whose NAME is also used,
# with a different syntax, by tinyusb's tests.dev_attached, and convoy_safe reads a non-empty
# value as PROOF the flasher can deliver a recovery past a poisoned node. A typo there
# silently promised a recovery that openocd would reject at startup.
_VID_PID_RE = re.compile(r'^0x[0-9a-fA-F]{4}(\s+0x[0-9a-fA-F]{4})+$')


def valid_vid_pid(value) -> bool:
    return isinstance(value, str) and bool(_VID_PID_RE.match(value.strip()))


def convoy_safe(flasher: dict) -> bool:
    """Can this flasher DELIVER a recovery while a usbfs node on the rig is poisoned?

    A post-HUNG reflash only helps if the flasher reaches its probe without opening the
    wedged node. Following shapes qualify:

    * openocd pinned with the roster's `vid_pid` -- the match is made from the cached
      descriptor and the loop `continue`s BEFORE libusb_open, so a foreign node is never
      opened. On 2026-08-12 it was the only flasher that still reached its probe.
    * esptool -- delivery is `-p <ttyACM>`, a named port; it never enumerates usbfs.
    * stlink (STM32_Programmer_CLI v2.23) -- its libSTLinkUSBDriver.so lists devices through
      libusb's lock-free sysfs walk (descriptors, busnum, devnum, speed, uevent) and opens
      only ST-Link (0483) nodes. strace on ci.lan 2026-10-07; with a testusb D-state on the
      wedged DUT's node, `--rst --go` returned 0 in 1.7 s and the holder reaped.
    * openocd over interface/jlink.cfg -- libjaylink opens SEGGER devices only (below).
    * openocd over interface/stlink.cfg or interface/ti-icdi.cfg -- discovery is VID/PID
      filtered by the cfg's own `adapter usb vid_pid` (below).
    * pyocd with one `vid_pid` pair -- run_pyocd.py makes pyusb drop every other device
      before pyocd's matcher opens it, and leaves only the CMSIS-DAP probe plugin.

    Everything else enumerates by OPENING nodes, would block in D state on the poisoned
    one, survive SIGKILL and become a second stray. JLinkExe cannot be pinned: selection
    is serial-only (-USB/-SelectEmuBySN, no VID/PID filter in J-Link Commander V9.66), and
    it reads the serial from sysfs: strace on ci.lan (2026-09-21) shows it opening only its
    own probe's usbfs node but reading `product`, `serial` and `bNumInterfaces` -- served
    under the device lock -- of every USB device on the host. So those boards can only
    become convoy-safe by moving to openocd (or behind usb_recover.sh's shield).

    Verified against openocd 0ce743125 (the rig's build), because the INVERSE is what
    bites: cmsis_dap_usb_bulk.c:107 skips on `id_filter && !id_match`, and `id_filter` is
    only `vids[0] || pids[0]` -- so without the pin nothing is skipped and every device on
    the bus is opened, which the code itself expects to mostly fail. Enumeration cannot
    block: libusb reads the `descriptors` sysfs attribute, and descriptors_read (v6.12.101
    drivers/usb/core/sysfs.c) is a memcpy from udev->rawdescriptors under no lock.

    The pin gates the BULK backend, which is the one that runs: `auto` tries usb_bulk ->
    hid -> tcp (cmsis_dap.c:62) and stops at the first that opens, so a CMSIS-DAP v2 probe
    never reaches the rest. It does NOT cover the HID fallback that a v1 probe or a failed
    bulk open takes -- cmsis_dap_usb_hid.c:91 calls hid_enumerate(0x0, 0x0), pin ignored,
    and filters afterwards, while hidapi's hidraw backend reads `manufacturer` and
    `product` for every HID device it lists (linux/hid.c:744), both usb_string_attr and so
    served under the device lock. A wedged DUT running hid_generic_inout,
    hid_boot_interface or hid_composite_freertos is a HID device and would stall that walk
    -- interruptibly, so it hangs rather than joining the D-state convoy and run_cmd's
    timeout ends it, but "never opens a foreign node" is true of the bulk path, not of
    every path openocd can take.
    """
    name = (flasher.get('name') or '').lower()
    if name in ('esptool', 'stlink'):
        return True
    if name == 'pyocd':
        return pyocd_vid_pid(flasher) is not None
    # EXACT, not startswith: rescue_openocd and a harness's flash dispatch both require the
    # exact name, so an 'openocd_wch'-style entry would pass this gate, reserve the
    # Rescue-DP legs, and then find no recovery path at all -- paying for a path that cannot
    # fire, which is the precise cost this gate exists to avoid.
    if name != 'openocd':
        return False
    if valid_vid_pid(flasher.get('vid_pid')):
        return True
    # openocd over the JLINK driver is safe WITHOUT a pin, and cannot use one: jlink.c
    # never reads adapter_usb_get_vids/pids (selection is adapter serial / usb address /
    # usb location), but libjaylink's discovery returns early unless idVendor == 0x1366 and
    # the PID is in its table, and only THEN calls libusb_open (discovery_usb.c). So it
    # never opens a foreign node. Verified against openocd 0ce743125 and libjaylink 0.4.0,
    # and by strace on ci.lan (2026-09-21): only SEGGER usbfs nodes opened, and the only
    # locking sysfs attribute read is the selected probe's bConfigurationValue.
    # stlink_usb.c:3403 and ti_icdi_usb.c:675 discover through jtag_libusb_open with the
    # cfg's vid_pid list, which skips every non-matching descriptor before libusb_open
    # (libusb_helper.c:170-176); both cfgs always set that list. Verified against
    # openocd 0ce743125.
    return any(cfg in (flasher.get('args') or '') for cfg in CONVOY_SAFE_CFGS)


# -------------------------------------------------------------
# esptool, lm4flash
# -------------------------------------------------------------
def flash_esptool(flasher: dict, *, chip: str, port: str, flash_args: str, cwd: str, timeout,
                  run=None) -> subprocess.CompletedProcess:
    """`chip` is the build's IDF_TARGET, `flash_args` its flash_args file on one line, `cwd`
    the build directory the file's relative paths name, `port` the probe's serial device."""
    command = (f'esptool --chip {chip} -p {port} {flasher["args"]} '
               f'--before=default_reset --after=hard_reset write_flash {flash_args}')
    return _runner(run)(command, cwd=cwd, timeout=timeout)


# no reset_esptool: esptool's reset would be `--after hard_reset`, which is not wired here


def flash_lm4flash(flasher: dict, firmware: str, *, timeout, run=None) -> subprocess.CompletedProcess:
    # TI Tiva-C / Stellaris ICDI: lightweight lm4flash, resets and runs after write
    return _runner(run)(f'lm4flash -s {flasher["uid"]} {flasher["args"]} {firmware}',
                        timeout=timeout)


# no reset_lm4flash: lm4flash has no reset-only mode; it resets+runs on flash


# -------------------------------------------------------------
# pyOCD
# -------------------------------------------------------------
def pyocd_vid_pid(flasher: dict) -> list | None:
    """[vid, pid] a pyocd entry filters discovery to, or None when it lacks a uid or exactly one
    valid `vid_pid` pair: unfiltered, pyocd opens every CMSIS-DAP-class device (run_pyocd.py)."""
    vid_pid = flasher.get('vid_pid')
    if not (flasher.get('uid') and valid_vid_pid(vid_pid) and len(vid_pid.split()) == 2):
        return None
    return vid_pid.split()


def _pyocd_python() -> str:
    # the launcher must patch the pyocd it runs, so it runs under pyocd's own interpreter
    exe = shutil.which('pyocd')
    if exe is None:
        raise RuntimeError('pyocd is not on PATH')
    with open(exe, 'rb') as f:
        line = f.readline().decode(errors='replace').strip()
    py = line[2:] if line.startswith('#!') else ''
    if not (os.path.isabs(py) and os.path.basename(py).startswith('python') and os.access(py, os.X_OK)):
        raise RuntimeError(f'{exe}: want an absolute python shebang, got {line!r}')
    return py


def _pyocd_argv(flasher: dict, verb: str, *extra: str, project_dir: str) -> list:
    vid_pid = pyocd_vid_pid(flasher)
    if vid_pid is None:
        raise ValueError(f'pyocd flasher {flasher.get("uid")!r} needs a uid and one "vid_pid" pair')
    # -W: with no matching probe pyocd otherwise waits for one to be plugged in
    return [_pyocd_python(), PYOCD_LAUNCHER, '--project-dir', str(project_dir), *vid_pid, verb, '-W',
            '-u', f'cmsisdap:{flasher["uid"]}', *shlex.split(flasher.get('args', '')), *extra]


def flash_pyocd(flasher: dict, firmware: str, *, project_dir: str, timeout,
                run=None) -> subprocess.CompletedProcess:
    """`project_dir` is where pyocd runs and resolves relative roster args (run_pyocd.py)."""
    return _runner(run)(_pyocd_argv(flasher, 'flash', os.path.abspath(firmware), project_dir=project_dir),
                        timeout=timeout)


def reset_pyocd(flasher: dict, *, project_dir: str, timeout, run=None) -> subprocess.CompletedProcess:
    return _runner(run)(_pyocd_argv(flasher, 'reset', project_dir=project_dir), timeout=timeout)
