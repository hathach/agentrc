#!/usr/bin/env python3
"""Capture one USB bus with usbmon into a Wireshark pcapng.

usage: usbcap.py <bus|VID:PID|VID:|auto> [seconds] [outfile] [--snaplen N]

  <bus>      numeric USB bus (lsusb "Bus 00N" -> N); 0 or "auto" = every bus
  <VID:PID>  the bus of a plugged-in device, e.g. 1a86:55d4
  <VID:>     any device of that vendor, e.g. 1a86:

A selector that matches devices on more than one bus is refused with the
matches listed; pass the bus instead. An existing outfile is refused, never
overwritten. A capture with no URB in it fails and keeps the file: the bus
was idle, or the selector named the wrong one. Linux only (usbmon). Assumes
usbmon is loaded and /dev/usbmon* is readable by the wireshark group (see
SKILL.md), so tshark captures without sudo; running as root is refused.
"""
import argparse
import os
import re
import shutil
import subprocess
import sys

LSUSB = re.compile(r'^Bus (\d+) Device (\d+): ID ([0-9a-f]{4}):([0-9a-f]{4})\s*(.*)$', re.I)


def devices(lsusb):
    """[(bus, device, vid, pid, name)] from lsusb output."""
    found = []
    for line in lsusb.splitlines():
        m = LSUSB.match(line.strip())
        if m:
            found.append((int(m[1]), int(m[2]), m[3].lower(), m[4].lower(), m[5].strip()))
    return found


def resolve(target, lsusb):
    """The usbmon bus number for a selector; 0 means every bus. `lsusb` is
    called for its output only when a device has to be looked up."""
    if target == 'auto':
        return 0
    if target.isdigit():
        return int(target)
    m = re.fullmatch(r'([0-9a-f]{4}):([0-9a-f]{4})?', target, re.I)
    if not m:
        raise ValueError(f"bad target '{target}': expected a bus number, VID:PID, VID: or auto")
    vid, pid = m[1].lower(), (m[2] or '').lower()
    matches = [d for d in devices(lsusb()) if d[2] == vid and (not pid or d[3] == pid)]
    if not matches:
        raise ValueError(f"no device matching '{target}' (plugged in?)")
    if len({d[0] for d in matches}) > 1:
        raise ValueError(f"'{target}' matches devices on several buses; pass the bus number instead:\n" +
                         '\n'.join(f'  bus {b} device {d}: {v}:{p} {n}' for b, d, v, p, n in matches))
    return matches[0][0]


def main():
    parser = argparse.ArgumentParser(description=__doc__, formatter_class=argparse.RawDescriptionHelpFormatter)
    parser.add_argument('target')
    parser.add_argument('seconds', nargs='?', type=int, default=10)
    parser.add_argument('outfile', nargs='?', default=f'/tmp/usbcap-{os.getpid()}.pcapng')
    parser.add_argument('--snaplen', type=int, help='bytes per packet: 64 keeps the URB header only, 128 adds 64 bytes of payload')
    args = parser.parse_args()
    if not sys.platform.startswith('linux'):
        sys.exit(f'usbcap.py is Linux-only (usbmon); this is {sys.platform}')
    if os.geteuid() == 0:
        # Wireshark 4.4 dumpcap.c: relinquish_privs_except_capture(), then relinquish_all_capabilities()
        sys.exit('usbcap.py refuses to run as root: dumpcap drops all root capabilities before writing, so it '
                 'cannot reach the outfile through a directory only its owner may enter (a mode-0700 home) and '
                 'fails with "Permission denied". Run it without sudo, as a user in the wireshark group '
                 "(SKILL.md; after a fresh group add: sg wireshark -c '...')")
    if args.seconds <= 0:
        sys.exit('seconds must be positive')
    if args.snaplen is not None and args.snaplen <= 0:
        sys.exit('--snaplen must be positive')
    needed = ['tshark', 'capinfos'] + ([] if args.target == 'auto' or args.target.isdigit() else ['lsusb'])
    missing = [t for t in needed if not shutil.which(t)]
    if missing:
        sys.exit(f'{", ".join(missing)} not on PATH (SKILL.md, Setup)')
    try:
        os.close(os.open(args.outfile, os.O_CREAT | os.O_EXCL | os.O_WRONLY, 0o644))  # reserved: nothing else can claim it now
    except FileExistsError:
        sys.exit(f'{args.outfile} exists; a capture never overwrites, pick another name')

    try:
        bus = resolve(args.target, lambda: subprocess.run(['lsusb'], capture_output=True, text=True, check=True).stdout)
    except (ValueError, OSError, subprocess.CalledProcessError) as e:
        os.unlink(args.outfile)
        sys.exit(str(e) if isinstance(e, ValueError) else f'lsusb failed: {e}')

    print(f'capturing usbmon{bus} for {args.seconds}s -> {args.outfile}')
    cmd = ['tshark', '-i', f'usbmon{bus}', '-a', f'duration:{args.seconds}', '-w', args.outfile]
    if args.snaplen:
        cmd += ['-s', str(args.snaplen)]
    try:
        capture = subprocess.run(cmd, capture_output=True, text=True)
    except OSError as e:
        os.unlink(args.outfile)
        sys.exit(f'cannot run tshark: {e}')
    if capture.returncode != 0:
        if os.path.getsize(args.outfile) == 0:
            os.unlink(args.outfile)
            kept = ''
        else:
            kept = f'\npartial capture kept in {args.outfile}'
        sys.exit(f'tshark exited {capture.returncode}: {capture.stderr.strip()}\n'
                 f'(no /dev/usbmon{bus} access? see SKILL.md: wireshark group, or wrap in sg wireshark){kept}')
    try:
        count = subprocess.run(['capinfos', '-c', '-M', args.outfile], capture_output=True, text=True)
    except OSError as e:
        sys.exit(f'capture written to {args.outfile}, but cannot run capinfos to check it: {e}')
    if count.returncode != 0:
        sys.exit(f'capture written but unreadable: {count.stderr.strip()}')
    m = re.search(r'^Number of packets:\s*(\d+)\s*$', count.stdout, re.M)
    if not m:
        sys.exit(f'capture written but capinfos gave no packet count: {count.stdout.strip()!r}')
    if int(m[1]) == 0:
        sys.exit(f'no URB on usbmon{bus} in {args.seconds}s: the bus was idle, or it is the wrong bus '
                 f'({args.outfile} kept, empty)')
    print(f'saved {args.outfile}  ({m[1]} packets)')
    print(f'analyze: tshark -r {args.outfile}   |   tshark -r {args.outfile} -V')


if __name__ == '__main__':
    main()
