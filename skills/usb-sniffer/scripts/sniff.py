#!/usr/bin/env python3
"""Mechanics of the ataradov usb-sniffer: a bounded capture at the right link
speed, and the DUT's wire address out of a capture. Judgment (what to tap,
what a capture shows) is in SKILL.md.

    sniff.py capture OUT.pcapng --port 3-2.7 --limit 3000000     # speed from sysfs
    sniff.py capture OUT.pcapng --speed hs --seconds 15 --fold    # explicit speed, time bound
    sniff.py addr CAP.pcapng                                      # SET_ADDRESS values seen on the wire

capture refuses to run unbounded, refuses --speed and --port together, refuses
a SuperSpeed port (the sniffer is USB 2.0 only) and refuses while another
usb_sniffer process runs. addr refuses a capture with no SET_ADDRESS and lists
every address when there are several: the caller picks the DUT."""
import argparse
import os
import shutil
import subprocess
import sys
import tempfile
from pathlib import Path

SYSFS = Path('/sys/bus/usb/devices')
SPEEDS = {'1.5': 'ls', '12': 'fs', '480': 'hs'}


class Refused(Exception):
    pass


def port_speed(port, sysfs=SYSFS):
    """ls/fs/hs of the device at a sysfs port such as 3-2.7."""
    f = sysfs / port / 'speed'
    if not f.exists():
        raise Refused(f'no device at port {port} ({f} missing); see `lsusb -t`')
    speed = f.read_text().strip()
    if speed not in SPEEDS:
        raise Refused(f'port {port} runs at {speed} Mb/s; the sniffer captures USB 2.0 (ls/fs/hs) only')
    return SPEEDS[speed]


def running():
    """pids of usb_sniffer processes already capturing."""
    r = subprocess.run(['pgrep', '-x', 'usb_sniffer'], capture_output=True, text=True)
    if r.returncode not in (0, 1):  # 1 is "no match"; anything else is pgrep itself failing
        raise Refused(f'pgrep failed ({r.returncode}): {r.stderr.strip()}')
    return r.stdout.split()


def capture(a):
    if bool(a.speed) == bool(a.port):
        raise Refused('give exactly one of --speed and --port')
    if a.limit is None and a.seconds is None:
        raise Refused('bound the capture with --limit N (packets) or --seconds S; HS fills a disk at ~20 MB/s')
    if a.limit is not None and a.limit <= 0:
        raise Refused(f'--limit must be positive, got {a.limit} (the tool counts down from it; 0 or less never stops)')
    if a.seconds is not None and not 0 < a.seconds < float('inf'):
        raise Refused(f'--seconds must be positive and finite, got {a.seconds}')
    if a.out.exists():
        raise Refused(f'{a.out} exists; a capture never overwrites, so a tool killed early cannot pass off the old file')
    if shutil.which('usb_sniffer') is None:
        raise Refused('usb_sniffer not on PATH (SKILL.md, Setup)')
    pids = running()
    if pids:
        raise Refused(f'usb_sniffer already running (pid {" ".join(pids)}); a leftover capture, kill it first')
    speed = a.speed or port_speed(a.port)
    cmd = ['usb_sniffer', '--capture', '--fifo', str(a.out), '--speed', speed]
    if a.fold:
        cmd.append('--fold')
    if a.limit:
        cmd += ['--limit', str(a.limit)]
    if a.trigger:
        cmd += ['--trigger', a.trigger]
    print('+', ' '.join(cmd), f'(timeout {a.seconds}s)' if a.seconds else '', flush=True)
    try:
        r = subprocess.run(cmd, timeout=a.seconds)
        if r.returncode != 0:
            raise Refused(f'usb_sniffer exited {r.returncode}')
    except subprocess.TimeoutExpired:
        # The tool closes its file on no signal, so the time bound leaves it cut mid-packet
        # and every reader refuses it; editcap keeps the whole packets.
        fd, whole = tempfile.mkstemp(dir=a.out.parent, prefix=a.out.name + '.', suffix='.whole')
        os.close(fd)
        fixed = subprocess.run(['editcap', str(a.out), whole], capture_output=True, text=True)
        if fixed.returncode != 0:
            os.unlink(whole)
            raise Refused(f'{a.out} is cut short and editcap could not repair it: {fixed.stderr.strip()}')
        os.replace(whole, a.out)
    if not a.out.exists() or a.out.stat().st_size == 0:
        raise Refused(f'{a.out} is empty: no capture (wrong --speed gives only Syslog records; no device on the tap gives nothing)')
    count = subprocess.run(['capinfos', '-c', '-M', str(a.out)], capture_output=True, text=True)
    if count.returncode != 0:
        raise Refused(f'{a.out} is not a readable capture: {count.stderr.strip()}')
    packets = next((l.split()[-1] for l in count.stdout.splitlines() if 'Number of packets' in l), '?')
    print(f'{a.out}: {packets} packets, {a.out.stat().st_size // 1024} KiB, speed {speed}')


def set_addresses(tshark_fields):
    """{address: [frame numbers]} from 'frame<TAB>device' lines of the SET_ADDRESS filter (Wireshark
    dissects SET_ADDRESS's wValue as usb.device_address)."""
    seen = {}
    for line in tshark_fields.splitlines():
        frame, _, value = line.partition('\t')
        if value.strip():
            seen.setdefault(int(value, 0), []).append(int(frame))
    return seen


def addr_filter(address):
    """Display filter for one device's traffic; `contains "9."` would also match 19., 29., ..."""
    return f'usbll.src matches "^{address}[.]" || usbll.dst matches "^{address}[.]"'


def addr(a):
    r = subprocess.run(['tshark', '-r', str(a.cap), '-Y', 'usb.setup.bRequest == 5 && usb.bmRequestType == 0x00',
                        '-T', 'fields', '-e', 'frame.number', '-e', 'usb.device_address'],
                       capture_output=True, text=True)
    if r.returncode != 0:
        raise Refused(f'tshark failed: {r.stderr.strip()}')
    seen = set_addresses(r.stdout)
    if not seen:
        raise Refused('no SET_ADDRESS in the capture: it started after enumeration, or the DUT is not on the tap')
    for address, frames in sorted(seen.items()):
        print(f'{address}\tframes {", ".join(map(str, frames))}\tfilter: {addr_filter(address)}')
    if len(seen) > 1:
        print(f'{len(seen)} devices enumerated on the tap; pick the DUT by frame order', file=sys.stderr)


def main():
    p = argparse.ArgumentParser(description=__doc__, formatter_class=argparse.RawDescriptionHelpFormatter)
    sub = p.add_subparsers(dest='cmd', required=True)
    c = sub.add_parser('capture', help='bounded capture into a pcapng')
    c.add_argument('out', type=Path)
    c.add_argument('--speed', choices=['ls', 'fs', 'hs'], help='link speed of the tapped segment')
    c.add_argument('--port', help='sysfs port of the tapped device (3-2.7): its speed picks --speed')
    c.add_argument('--limit', type=int, help='stop after N packets (the tool exits by itself)')
    c.add_argument('--seconds', type=float, help='kill the tool after S seconds')
    c.add_argument('--fold', action='store_true', help='fold empty frames')
    c.add_argument('--trigger', choices=['low', 'high', 'falling', 'rising'], help='arm on the external trigger pin')
    d = sub.add_parser('addr', help='SET_ADDRESS values seen in a capture')
    d.add_argument('cap', type=Path)
    a = p.parse_args()
    try:
        (capture if a.cmd == 'capture' else addr)(a)
        return 0
    except Refused as e:
        print(f'sniff: {e}', file=sys.stderr)
        return 1


if __name__ == '__main__':
    sys.exit(main())
