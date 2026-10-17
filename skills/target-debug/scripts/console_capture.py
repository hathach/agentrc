#!/usr/bin/env python3
"""Capture a serial console into a new file, the bytes as received, with the time of every
chunk and the open recorded as the baseline (the target-debug skill's Log capture).

  console_capture.py --port /dev/serial/by-id/<port> --baud 115200 --seconds 20 \
                     --lines leave|low --out <new file> [--mark REGEX ...]

The port is named by /dev/serial/by-id or by-path; /dev/ttyACM<N> and /dev/ttyUSB<N> are
refused, since those numbers drift within one boot. The script configures its own fd: raw mode
and the baud rate with TCSANOW (TCSAFLUSH waits for another client's output to drain and
discards unread input), and puts the port's original settings back at the end; a restore that
fails fails the run.

--lines is the control-line policy, with no default: `leave` keeps DTR and RTS as the open set
them, `low` clears both once the port is configured. Neither keeps them low through the open: Linux
raises DTR and RTS when a serial port opens, and on Espressif boards a line change can reset
the chip (espressif.md, Console). A port without modem control lines (a pty) refuses `low`.
Closing the port drops both lines again when the restored settings carry HUPCL.

A reset at the open is measured, never assumed: each --mark pattern (a Python regex over the
bytes read as Latin-1, e.g. 'rst:0x[0-9a-f]+' for an Espressif ROM banner) is reported with the
time after the open of the chunk where each match starts. An empty capture reports no baseline:
nothing anchors it. Other processes holding the device open are listed as a possible
conflict: a second reader takes bytes from this one.

Output: the bytes in --out, one JSON line per chunk ({"t": s after the open, "offset", "len"})
in <out>.times, and a JSON summary on stdout. The capture ends at --seconds, at a signal, or
when the device goes away or hangs up (a reset that re-enumerates a USB console); its settings
then often cannot be restored, which fails the run like any other restore failure (a hung-up
USB-serial port keeps its termios: the USB-serial core does not reset them at hangup).

Exit: 0 captured (an empty capture included); 2 bad usage; 3 the port could not be opened or
configured, a read failed, its settings could not be restored, or an output file failed to close.
"""
import argparse
import bisect
import datetime
import errno
import itertools
import json
import math
import os
import re
import select
import signal
import struct
import sys
import time

try:
    import fcntl
    import termios
    import tty
except ImportError:  # Windows: main() refuses
    termios = None

DRIFTING = re.compile(r'/dev/tty(ACM|USB)[0-9]+')
MAX_MARKS = 20


class Stop(Exception):
    pass


def baud_constant(rate):
    return getattr(termios, f'B{rate}', None)


def modem_lines(fd):
    """{'dtr', 'rts'}, or None on a port without modem control lines."""
    try:
        v = struct.unpack('i', fcntl.ioctl(fd, termios.TIOCMGET, struct.pack('i', 0)))[0]
    except OSError:
        return None
    return {'dtr': bool(v & termios.TIOCM_DTR), 'rts': bool(v & termios.TIOCM_RTS)}


def other_openers(device):
    """[{'pid', 'comm'}] of other processes with the device open, as /proc shows them."""
    found = []
    for pid in filter(str.isdigit, os.listdir('/proc')):
        if int(pid) == os.getpid():
            continue
        try:
            fds = os.listdir(f'/proc/{pid}/fd')
        except OSError:
            continue
        for fd in fds:
            try:
                if os.readlink(f'/proc/{pid}/fd/{fd}') == device:
                    with open(f'/proc/{pid}/comm') as f:
                        found.append({'pid': int(pid), 'comm': f.read().strip()})
                    break
            except OSError:
                continue
    return found


def marks(data, chunks, patterns):
    if not chunks:
        return []
    text = data.decode('latin-1')
    starts = [c['offset'] for c in chunks]
    out = []
    for pat in patterns:
        for m in itertools.islice(pat.finditer(text), MAX_MARKS):
            i = max(0, bisect.bisect_right(starts, m.start()) - 1)
            out.append({'pattern': pat.pattern, 'offset': m.start(), 't': chunks[i]['t'], 'text': m.group(0)})
    return out


def main(argv=None):
    p = argparse.ArgumentParser(description=__doc__, formatter_class=argparse.RawDescriptionHelpFormatter)
    p.add_argument('--port', required=True, help='/dev/serial/by-id/... or /dev/serial/by-path/...')
    p.add_argument('--baud', required=True, type=int, help='baud rate, no default')
    p.add_argument('--seconds', required=True, type=float, help='capture length in seconds')
    p.add_argument('--lines', required=True, choices=['leave', 'low'],
                   help='DTR/RTS after the open: leave as opened, or clear both')
    p.add_argument('--out', required=True, help='new file for the bytes; <out>.times gets the chunk times')
    p.add_argument('--mark', action='append', default=[], metavar='REGEX',
                   help='report where this pattern occurs, with its time after the open (repeatable)')
    a = p.parse_args(argv)
    if termios is None:
        p.error(f'POSIX-only (termios); this is {sys.platform}')
    if DRIFTING.fullmatch(a.port):
        p.error(f'--port {a.port}: ttyACM/ttyUSB numbers drift; name it by /dev/serial/by-id or by-path')
    if not os.path.exists(a.port):
        p.error(f'--port {a.port}: no such device')
    speed = baud_constant(a.baud)
    if speed is None:
        p.error(f'--baud {a.baud}: not a rate this platform\'s termios offers')
    if not (math.isfinite(a.seconds) and a.seconds > 0):
        p.error('--seconds must be a finite positive number')
    try:
        patterns = [re.compile(m) for m in a.mark]
    except re.error as e:
        p.error(f'--mark: {e}')
    times_path = a.out + '.times'
    files = []
    for path in (a.out, times_path):
        try:
            files.append(open(path, 'xb'))
        except OSError as e:
            for f in files:
                f.close()
                os.unlink(f.name)
            p.error(f'{path}: exists; a capture never overwrites' if isinstance(e, FileExistsError) else f'{path}: {e}')
    out, times = files
    device = os.path.realpath(a.port)
    result = {'port': a.port, 'device': device, 'baud': a.baud, 'seconds': a.seconds, 'out': a.out, 'times': times_path}
    openers = other_openers(device)
    if openers:
        result['otherOpeners'] = openers

    def on_signal(signum, _frame):
        raise Stop(f'signal {signum}')

    for sig in (signal.SIGINT, signal.SIGTERM):
        signal.signal(sig, on_signal)
    fd, saved, errors = None, None, []
    chunks, size, ended = [], 0, 'time'
    try:
        fd = os.open(a.port, os.O_RDONLY | os.O_NOCTTY | os.O_NONBLOCK)
        t_open = time.monotonic()
        result['openedAt'] = datetime.datetime.now(datetime.timezone.utc).isoformat(timespec='milliseconds')
        saved = termios.tcgetattr(fd)
        tty.setraw(fd, termios.TCSANOW)
        attrs = termios.tcgetattr(fd)
        attrs[4] = attrs[5] = speed
        termios.tcsetattr(fd, termios.TCSANOW, attrs)
        if a.lines == 'low':
            # after the baud: leaving B0 re-raises DTR, and RTS on ftdi (ftdi_sio.c, cdc-acm.c set_termios)
            try:
                fcntl.ioctl(fd, termios.TIOCMBIC, struct.pack('i', termios.TIOCM_DTR | termios.TIOCM_RTS))
            except OSError as e:
                raise OSError(e.errno, f'cannot clear DTR/RTS on this port ({e.strerror}); --lines low refused')
        result['lines'] = {'policy': a.lines, 'afterSetup': modem_lines(fd)}
        deadline = t_open + a.seconds
        while (left := deadline - time.monotonic()) > 0:
            ready, _, _ = select.select([fd], [], [], left)
            if not ready:
                continue
            try:
                chunk = os.read(fd, 4096)
            except BlockingIOError:
                continue
            except OSError as e:
                if e.errno not in (errno.EIO, errno.ENODEV, errno.ENXIO):
                    raise
                chunk = b''
            if not chunk:
                ended = 'device gone'
                break
            rec = {'t': round(time.monotonic() - t_open, 4), 'offset': size, 'len': len(chunk)}
            out.write(chunk)
            out.flush()
            times.write((json.dumps(rec) + '\n').encode())
            times.flush()
            chunks.append(rec)
            size += len(chunk)
    except Stop as e:
        ended = str(e)
    except (OSError, termios.error) as e:
        errors.append(f'{a.port}: {getattr(e, "strerror", None) or e}')
    finally:
        for sig in (signal.SIGINT, signal.SIGTERM):
            signal.signal(sig, signal.SIG_IGN)
        if fd is not None:
            if saved is not None:
                try:
                    termios.tcsetattr(fd, termios.TCSANOW, saved)
                    result['terminalRestored'] = True
                except termios.error as e:
                    result['terminalRestored'] = False
                    errors.append(f'restoring the port settings: {e}')
            os.close(fd)
        for f in (out, times):
            try:
                f.close()
            except OSError as e:
                errors.append(f'closing {f.name}: {e}')
    result.update(ended=ended, bytes=size, chunks=len(chunks),
                  firstByteAt=chunks[0]['t'] if chunks else None,
                  baseline='the open' if chunks else 'none: nothing was received')
    if patterns:
        try:
            with open(a.out, 'rb') as f:
                result['marks'] = marks(f.read(), chunks, patterns)
        except OSError as e:
            errors.append(f'reading {a.out} back for --mark: {e}')
    if errors:
        result['error'] = '; '.join(errors)
    print(json.dumps(result))
    return 3 if errors else 0


if __name__ == '__main__':
    sys.exit(main())
