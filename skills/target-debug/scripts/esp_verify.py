#!/usr/bin/env python3
"""Verify every image in an ESP-IDF flasher_args.json, without programming flash.

  esp_verify.py --manifest <build>/flasher_args.json --chip esp32p4 \
      --port /dev/serial/by-id/<port> --before default-reset --after hard-reset [--timeout S]

ESP_VERIFY_ESPTOOL is a command line (e.g. '/path/to/python -m esptool'); otherwise
use esptool on PATH. The command/output contract is esptool 5.5.0 from ESP-IDF 6.1.
Reset policies are required, never taken from the manifest. No retry is made. esptool is
killed after --timeout seconds (default 300), a tool failure that leaves the chip state unknown.
Port ownership is the caller's board lock, not checked here: esptool's pyserial
exclusive=True uses advisory flock on POSIX and cannot exclude other readers
(esptool/loader.py:453-465; serial/serialposix.py:382-389). Stop console readers.

One JSON object on stdout. Coverage is only the manifest's file ranges, whose
SHA256 hashes describe the original files. Comparison uses esptool's header
patching with the manifest flash settings and 0xff padding to four bytes;
comparison_size records that padding. Images are snapshotted before invoking
the tool. Encrypted images and unsupported flashing options are refused.
esptool registers the --after reset only once it has connected, uploaded the stub and set the
baud (__init__.py:552 connects, :623 stub, :639 baud, the teardown at :660-680 resets): a
failure before then, a wrong chip included, can leave the chip in the ROM loader, whether --before
or the caller put it there, and a printed reset can still fail. A failed run says so
(`chipState`) unless --before and --after are both no-reset. With --before no-reset, DTR stays
asserted from the open and a UART bridge's hard reset writes only RTS, so a two-transistor
auto-reset circuit never pulls EN: reset the chip afterwards (espressif.md, "ESP32-S3", the Metro).
Exit: 0 all images match; 1 mismatch; 2 usage/refusal; 3 tool failure or incomplete
per-image evidence.
"""
import argparse
import hashlib
import json
import os
from pathlib import Path
import re
import shlex
import shutil
import subprocess
import sys
import tempfile


# esptool 5.5.0 __init__.py:295-349, 421-430, 1140-1158.
# Refuse detect: cmds.py:2230-2235 guesses 4MB if size detection fails.
FLASH_VALUES = {
    'flash_mode': {'keep', 'qio', 'qout', 'dio', 'dout'},
    'flash_freq': {'keep', '80m', '64m', '60m', '48m', '40m', '32m', '30m',
                   '26m', '24m', '20m', '16m', '15m', '12m'},
    'flash_size': {'keep', '256KB', '512KB', '1MB', '2MB', '2MB-c1', '4MB',
                   '4MB-c1', '8MB', '16MB', '32MB', '64MB', '128MB'},
}
BEFORE = ('default-reset', 'usb-reset', 'no-reset', 'no-reset-no-sync')
AFTER = ('hard-reset', 'soft-reset', 'no-reset', 'no-reset-stub', 'watchdog-reset')
PORT = re.compile(r'/dev/serial/(by-id|by-path)/[^/\x00-\x20]+')
OFFSET = re.compile(r'(0[xX][0-9a-fA-F]+|[0-9]+)')
HEADER = re.compile(r"Verifying (0x[0-9a-f]+) \(([0-9]+)\) bytes at "
                    r"(0x[0-9a-f]+) in flash against '(.+)'\.\.\.")
VERDICTS = {'Verification successful (digest matched).': 'match',
            'Verification failed (digest mismatch).': 'mismatch'}
MISMATCH_ERROR = 'ERROR: A fatal error occurred: Verification failed.'


class JsonParser(argparse.ArgumentParser):
    def error(self, message):
        raise ValueError(message)


def unique_object(pairs):
    obj = {}
    for key, value in pairs:
        if key in obj:
            raise ValueError(f'duplicate manifest key: {key}')
        obj[key] = value
    return obj


def load_manifest(path, chip):
    manifest = json.loads(path.read_text(), object_pairs_hook=unique_object)
    if not isinstance(manifest, dict):
        raise ValueError('manifest must be an object')
    extra = manifest.get('extra_esptool_args')
    if not isinstance(extra, dict) or extra.get('chip') != chip:
        raise ValueError('manifest chip disagrees with --chip or is missing')
    if type(extra.get('stub')) is not bool or set(extra) - {'chip', 'stub', 'before', 'after'}:
        raise ValueError('missing stub policy or unsupported extra_esptool_args')
    settings = manifest.get('flash_settings')
    if not isinstance(settings, dict) or set(settings) != set(FLASH_VALUES):
        raise ValueError('flash_settings must specify flash_mode, flash_size and flash_freq')
    for key, value in settings.items():
        if not isinstance(value, str) or value not in FLASH_VALUES[key]:
            raise ValueError(f'unsupported {key}: {value!r}')
    args = manifest.get('write_flash_args')
    if not isinstance(args, list) or len(args) != 6 or not all(isinstance(a, str) for a in args):
        raise ValueError('write_flash_args must contain the three flash settings only')
    parsed = {}
    for flag, value in zip(args[::2], args[1::2]):
        key = flag.removeprefix('--').replace('-', '_')
        if not flag.startswith('--') or key not in FLASH_VALUES or key in parsed:
            raise ValueError(f'unsupported or duplicate write_flash_args option: {flag}')
        parsed[key] = value
    if parsed != settings:
        raise ValueError('write_flash_args disagrees with flash_settings')
    files = manifest.get('flash_files')
    if not isinstance(files, dict) or not files:
        raise ValueError('flash_files must be a nonempty object')
    for value in manifest.values():
        if isinstance(value, dict) and 'encrypted' in value and value['encrypted'] not in (False, 'false'):
            raise ValueError('encrypted image verification is unsupported')
    images = []
    payloads = {}
    for offset, filename in files.items():
        if not OFFSET.fullmatch(offset):
            raise ValueError(f'malformed offset: {offset!r}')
        address = int(offset, 16 if offset.lower().startswith('0x') else 10)
        if not isinstance(filename, str) or not filename or any(ord(c) < 32 for c in filename):
            raise ValueError('image file must be a nonempty path without control characters')
        image_path = (path.parent / filename).resolve()
        if not image_path.is_file():
            raise ValueError(f'missing image file: {image_path}')
        data = image_path.read_bytes()
        if not data:
            raise ValueError(f'empty image file: {image_path}')
        size = len(data)
        # cmds.py:2664-2667 pads to four bytes before header patching and comparison.
        comparison_size = (size + 3) & ~3
        if address + comparison_size > 2**32:
            raise ValueError(f'image range exceeds 32-bit address space: {offset}')
        images.append({'offset': address, 'size': size, 'file': str(image_path),
                       'sha256': hashlib.sha256(data).hexdigest(), 'comparison_size': comparison_size})
        payloads[address] = data
    images.sort(key=lambda i: i['offset'])
    for first, second in zip(images, images[1:]):
        if first['offset'] + first['comparison_size'] > second['offset']:
            raise ValueError('overlapping image ranges (including comparison padding)')
    return images, payloads, settings, extra['stub']


def parse_verdicts(output, images, sources):
    # cmds.py:2671-2686 gives a range/source header then a digest verdict for each image.
    expected = {i['offset']: i for i in images}
    pending = None
    seen = set()
    for line in output.splitlines():
        line = line.strip()
        if line.startswith('Verifying '):
            match = HEADER.fullmatch(line)
            if not match or pending is not None:
                raise ValueError('malformed image header or missing per-image verdict')
            hex_size, size, offset, source = match.groups()
            offset, size = int(offset, 16), int(size)
            if (offset not in expected or offset in seen or int(hex_size, 16) != size
                    or size != expected[offset]['comparison_size'] or source != sources[offset]):
                raise ValueError('unexpected or duplicate image range/source in tool output')
            pending = offset
        elif line in VERDICTS:
            if pending is None:
                raise ValueError('per-image verdict without an image header')
            expected[pending]['verdict'] = VERDICTS[line]
            seen.add(pending)
            pending = None
        elif line.startswith('Verification '):
            raise ValueError(f'unrecognized per-image verdict: {line}')
    if pending is not None or len(seen) != len(images):
        raise ValueError('tool output does not account for every image')


def main(argv=None):
    result = {'coverage': 'manifest ranges only', 'ownership': "caller's board lock; not checked"}
    try:
        parser = JsonParser(description=__doc__, formatter_class=argparse.RawDescriptionHelpFormatter)
        parser.add_argument('--manifest', required=True, type=Path)
        parser.add_argument('--chip', required=True)
        parser.add_argument('--port', required=True)
        parser.add_argument('--before', required=True, choices=BEFORE)
        parser.add_argument('--after', required=True, choices=AFTER)
        parser.add_argument('--timeout', type=float, default=300.0, help='seconds before esptool is killed')
        args = parser.parse_args(argv)
        if not 0 < args.timeout < float('inf'):
            raise ValueError('--timeout must be a finite positive number of seconds')
        if not re.fullmatch(r'esp(?:32[a-z0-9]*|8266)', args.chip):
            raise ValueError('--chip must name an explicit ESP chip, not auto')
        if not PORT.fullmatch(args.port) or args.port.rsplit('/', 1)[-1] in ('.', '..'):
            raise ValueError('--port must use /dev/serial/by-id or by-path; ttyACM/ttyUSB numbers drift')
        images, payloads, settings, stub = load_manifest(args.manifest.resolve(), args.chip)
        command = shlex.split(os.environ.get('ESP_VERIFY_ESPTOOL', 'esptool'))
        if not command or not shutil.which(command[0]):
            raise ValueError('esptool not found: set ESP_VERIFY_ESPTOOL or put esptool on PATH')
        result.update(manifest=str(args.manifest.resolve()), chip=args.chip, port=args.port,
                      before=args.before, after=args.after, stub=stub, flash_settings=settings, ranges=images)
    except (ValueError, OSError) as exc:
        result.update(status='refused', error=str(exc))
        print(json.dumps(result))
        return 2

    try:
        with tempfile.TemporaryDirectory(prefix='esp-verify-') as directory:
            sources = {}
            for i, image in enumerate(images):
                snapshot = Path(directory) / f'{i}.bin'
                snapshot.write_bytes(payloads[image['offset']])
                sources[image['offset']] = str(snapshot)
            # __init__.py:474-478, 531-558: bound connection and port-open attempts separately.
            command += ['--chip', args.chip, '--port', args.port, '--before', args.before,
                        '--after', args.after, '--connect-attempts', '1']
            if not stub:
                command.append('--no-stub')
            command.append('verify-flash')
            # cmds.py:1657-1659 and 2664-2667 use the same header patcher for write and verify.
            for key, value in settings.items():
                command += ['--' + key.replace('_', '-'), value]
            for image in images:
                command += [hex(image['offset']), sources[image['offset']]]
            result['command'] = command
            env = {**os.environ, 'ESPTOOL_OPEN_PORT_ATTEMPTS': '1', 'NO_COLOR': '1'}
            try:
                proc = subprocess.run(command, capture_output=True, text=True, errors='replace', env=env,
                                      timeout=args.timeout)
            except subprocess.TimeoutExpired as exc:
                # its output can come back as bytes after a timeout even with text=True
                text = lambda b: b.decode(errors='replace') if isinstance(b, bytes) else (b or '')
                result.update(tool_exit=None, stdout=text(exc.stdout), stderr=text(exc.stderr))
                raise ValueError(f'esptool did not finish within {args.timeout:g} s and was killed') from exc
            result.update(tool_exit=proc.returncode, stdout=proc.stdout, stderr=proc.stderr)
            output = proc.stdout + '\n' + proc.stderr
            # loader.py:984-995: unknown chip warning or explicit chip disagreement is a refusal.
            if 'Wrong chip argument?' in output or "This chip doesn't appear to be an " in output:
                result.update(status='refused', error='connected chip disagrees with --chip or is unrecognized')
                code = 2
            else:
                parse_verdicts(proc.stdout, images, sources)
                mismatch = any(i['verdict'] == 'mismatch' for i in images)
                errors = [line.strip() for line in output.splitlines() if line.strip().startswith('ERROR:')]
                # cmds.py:2699-2700; __init__.py:1372-1374: a completed mismatch exits 2.
                if mismatch and proc.returncode == 2 and errors == [MISMATCH_ERROR]:
                    result['status'], code = 'mismatch', 1
                elif not mismatch and proc.returncode == 0 and not errors:
                    result['status'], code = 'match', 0
                else:
                    raise ValueError('tool failure or exit status inconsistent with per-image verdicts')
    except (ValueError, OSError) as exc:
        result.update(status='failed', error=str(exc))
        code = 3
    stays = args.before.startswith('no-reset') and args.after.startswith('no-reset')
    if code in (2, 3) and 'tool_exit' in result and not stays:
        result['chipState'] = 'unknown, possibly in the ROM loader; reset the chip to return to the app'
    print(json.dumps(result))
    return code


if __name__ == '__main__':
    sys.exit(main())
