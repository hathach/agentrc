#!/usr/bin/env python3
"""The farm's board registry (`skills/hil/boards.json`): how each shared-farm board is flashed, keyed by
the serial of the debug probe it is wired to. Each board has one probe, so its `flasher` and
`flasher_recover` are farm facts, not a project's; so is `dev_attached`, the devices wired to its USB
host port. A project roster names the board by `probe` and keeps its own `uid`, the serial its
firmware reports, which differs per firmware, and its tests. Stdlib only.

  {"boards": {"<probe serial>": {"name": <lock name>,
                                 "flasher": {name, args, vid_pid, verify, note},
                                 "flasher_recover": {name, args, vid_pid, verify, note},
                                 "dev_attached": [{vid_pid "vvvv:pppp", serial, is_cdc, is_msc,
                                                   block_size, block_count, msc_inquiry, note}]}}}

A device's `serial` is its iSerialNumber string as the host reads it, kept exactly ("\u0409" for a
drive answering with its language ID). A device with no usable serial recorded omits it: tinyusb's host
examples print "0" both for iSerialNumber 0 and for a failed read, so "0" is no device fact.

`name` is the board's lock name on every host, whatever a roster calls the board: unique, a
plain file name, and renamed only with a migration of the locks and records naming it.

resolve(entry) is a roster entry with its board's `flasher` (its `uid` the probe serial) and
`flasher_recover` filled in, and its registered `dev_attached` at the top level, a new dict. An entry
with `probe` that also carries a flasher is refused as a duplicate; one without `probe` (a bench board,
or a rig one project owns) keeps its own, unless its probe is a registered board's. Where the registry
has a board's `dev_attached`, a copy its roster still carries (skills/hil/SKILL.md, Board registry) is
compared with it and warned about on stderr, never refused; resolve(entry, roster=FILE) names the roster
file in that warning. resolve_roster(roster) resolves `boards` and `boards-skip`.
Both raise ValueError naming every problem. lock_name(entry, own) is an entry's board lock.

  hil_boards.py roster FILE         the resolved roster as JSON (exit 1 refused, 2 usage)
  hil_boards.py lock FILE BOARD     the board lock of FILE's entry named BOARD, boards-skip included
                                    (lock_name without `own`: a project lock field is not read)
"""

from __future__ import annotations

import copy
import importlib.util
import json
import os
import re
import sys

HERE = os.path.dirname(os.path.abspath(__file__))
REGISTRY = os.path.join(HERE, '..', 'boards.json')
FILES = ('skills/hil/scripts/hil_load.py', 'skills/hil/scripts/usb_observe.py', 'skills/hil/boards.json')
PHYSICAL = ('flasher', 'flasher_recover')
LOCK_NAME_RE = re.compile(r'[A-Za-z0-9][A-Za-z0-9._-]*\Z')   # also within hil_lock's own grammar
FLASHER_NAMES = ('jlink', 'stlink', 'openocd', 'lm4flash', 'esptool', 'pyocd', 'openocd_esp')
_FLASHER_KEYS = {'name', 'uid', 'args', 'vid_pid', 'verify', 'note'}
REGISTERED = (*PHYSICAL, 'dev_attached')   # what a registered board's resolved entry takes from here
_ENTRY_KEYS = {'name', *REGISTERED}
_DEV_KEYS = {'vid_pid', 'serial', 'is_cdc', 'is_msc', 'block_size', 'block_count', 'msc_inquiry', 'note'}
DEV_VID_PID_RE = re.compile(r'[0-9a-f]{4}:[0-9a-f]{4}\Z')


def _hil_load():
    """hil_load.py from beside this file, the process's one `agentrc_hil_load`."""
    spec = importlib.util.spec_from_file_location('agentrc_hil_load', os.path.join(HERE, 'hil_load.py'))
    if spec.name not in sys.modules:
        if not os.path.isfile(spec.origin):
            raise ImportError(f'{spec.origin} is missing')
        mod = importlib.util.module_from_spec(spec)
        spec.loader.exec_module(mod)
        sys.modules.setdefault(spec.name, mod)
    return sys.modules[spec.name].load(spec.name, spec.origin)


hil_load = _hil_load()
hil_load.require(__file__, FILES)
usb_observe = hil_load.load_file('skills/hil/scripts/usb_observe.py')


def _no_duplicate_keys(pairs):
    keys = [k for k, _ in pairs]
    dup = sorted({k for k in keys if keys.count(k) > 1})
    if dup:
        raise ValueError(f'duplicate key(s) {", ".join(dup)}')
    return dict(pairs)


def flasher_problem(flasher, *, has_uid=True) -> str:
    """Why `flasher` is not a well-formed flasher dict, or ''. A bench board's primary flasher
    names its probe by `uid`; a recovery flasher reaches the board through the primary's probe,
    and a registered flasher's probe is its registry key, so neither has one."""
    if not isinstance(flasher, dict):
        return f'not an object: {flasher!r}'
    extra = set(flasher) - (_FLASHER_KEYS if has_uid else _FLASHER_KEYS - {'uid'})
    if extra:
        return f'unknown key(s) {", ".join(sorted(extra))}'
    if flasher.get('name') not in FLASHER_NAMES:
        return f'name {flasher.get("name")!r} is none of {", ".join(FLASHER_NAMES)}'
    for key in ('uid', 'args', 'vid_pid', 'note'):
        if key in flasher and not (isinstance(flasher[key], str) and flasher[key].strip()):
            return f'{key} must be a non-empty string, got {flasher[key]!r}'
    if has_uid and 'uid' not in flasher:
        return 'no uid (the probe serial)'
    if 'vid_pid' in flasher and not usb_observe.valid_vid_pid(flasher['vid_pid']):
        return f'vid_pid must be "0xVVVV 0xPPPP" pairs, got {flasher["vid_pid"]!r}'
    if 'verify' in flasher and not isinstance(flasher['verify'], bool):
        return f'verify must be true or false, got {flasher["verify"]!r}'
    return ''


def _identity(device, legacy=False) -> tuple:
    """(vid_pid, serial or None) of a device; in a roster's copy, either vid_pid spelling and a "0" serial."""
    serial = device.get('serial')
    if legacy:
        return device['vid_pid'].lower().replace('_', ':'), None if serial == '0' else serial
    return device['vid_pid'], serial


def dev_attached_problem(devices) -> str:
    """Why `devices` is not a well-formed registry `dev_attached` list, or ''."""
    if not (isinstance(devices, list) and devices):
        return f'not a non-empty list: {devices!r}'
    seen = set()
    for i, d in enumerate(devices):
        if not isinstance(d, dict):
            return f'[{i}] not an object: {d!r}'
        if set(d) - _DEV_KEYS:
            return f'[{i}] unknown key(s) {", ".join(sorted(set(d) - _DEV_KEYS))}'
        if not (isinstance(d.get('vid_pid'), str) and DEV_VID_PID_RE.match(d['vid_pid'])):
            return f'[{i}] vid_pid must be lowercase "vvvv:pppp", got {d.get("vid_pid")!r}'
        if 'serial' in d and not (isinstance(d['serial'], str) and d['serial'].strip() and d['serial'] != '0'):
            return f'[{i}] serial must be a non-empty string other than "0", or absent, got {d["serial"]!r}'
        for key in ('is_cdc', 'is_msc'):
            if key in d and not isinstance(d[key], bool):
                return f'[{i}] {key} must be true or false, got {d[key]!r}'
        for key in ('block_size', 'block_count'):
            if key in d and not (type(d[key]) is int and d[key] > 0):
                return f'[{i}] {key} must be a positive integer, got {d[key]!r}'
        for key in ('msc_inquiry', 'note'):
            if key in d and not (isinstance(d[key], str) and d[key].strip()):
                return f'[{i}] {key} must be a non-empty string, got {d[key]!r}'
        if (ident := _identity(d)) in seen:
            return f'[{i}] {d["vid_pid"]} {d.get("serial")!r} is listed twice'
        seen.add(ident)
    return ''


def _check(boards: dict, path) -> list:
    problems, probes, locks = [], [], {}
    for probe, e in boards.items():
        if not probe.strip():
            problems.append('an empty probe serial')
        for other in probes:
            if usb_observe.jlink_serial_match(probe, other):
                problems.append(f'{probe}: the same probe as {other}')
        probes.append(probe)
        if not isinstance(e, dict):
            problems.append(f'{probe}: not an object')
            continue
        if set(e) - _ENTRY_KEYS:
            problems.append(f'{probe}: unknown key(s) {", ".join(sorted(set(e) - _ENTRY_KEYS))}')
        name = e.get('name')
        if not (isinstance(name, str) and LOCK_NAME_RE.match(name)):
            problems.append(f'{probe}: name {name!r} is not a plain file name (it is the board lock)')
        elif name.casefold() in locks:
            problems.append(f'{probe}: name {name} is also {locks[name.casefold()]}\'s')
        else:
            locks[name.casefold()] = probe
        if 'flasher' not in e:
            problems.append(f'{probe}: no flasher')
        for key in PHYSICAL:
            if key in e and (why := flasher_problem(e[key], has_uid=False)):
                problems.append(f'{probe}: {key}: {why}')
        if 'dev_attached' in e and (why := dev_attached_problem(e['dev_attached'])):
            problems.append(f'{probe}: dev_attached: {why}')
    return [f'{path}: {p}' for p in problems]


_cache: dict = {}


def registry(path=REGISTRY) -> dict:
    """{probe serial: entry} of the registry at `path`, validated; ValueError naming every problem."""
    path = os.path.realpath(path)
    if path not in _cache:
        try:
            with open(path, encoding='utf-8') as f:
                doc = json.load(f, object_pairs_hook=_no_duplicate_keys)
        except (OSError, ValueError) as e:
            raise ValueError(f'board registry {path}: {e}') from e
        boards = doc.get('boards') if isinstance(doc, dict) and set(doc) == {'boards'} else None
        if not isinstance(boards, dict):
            raise ValueError(f'board registry {path}: expected {{"boards": {{"<probe serial>": {{...}}}}}}')
        problems = _check(boards, path)
        if problems:
            raise ValueError('; '.join(problems))
        _cache[path] = boards
    return _cache[path]


def _registered(entry, path) -> tuple:
    """(the registry entry of a roster entry's `probe`, '') or (None, why)."""
    probe = entry['probe']
    reg = registry(path).get(probe) if isinstance(probe, str) else None
    return (reg, '') if reg else (None, f'{entry.get("name")}: probe {probe!r} is not in agentrc\'s board registry')


LEGACY_COPIES = (('dev_attached',), ('tests', 'dev_attached'), ('arduino', 'dev_attached'))


def _local_copies(entry):
    """[(path, value)] of the `dev_attached` copies a roster entry carries (LEGACY_COPIES)."""
    found = []
    for path in LEGACY_COPIES:
        obj = entry
        for key in path:
            if not isinstance(obj, dict) or key not in obj:
                break
            obj = obj[key]
        else:
            found.append(('.'.join(path), obj))
    return found


def _copy_problem(local, registered) -> str:
    """How a roster's own `dev_attached` differs from the registry's, or ''."""
    if not isinstance(local, list) or not all(isinstance(d, dict) for d in local):
        return 'not a list of objects'
    if not all(isinstance(d.get('vid_pid'), str) and isinstance(d.get('serial', ''), str) for d in local):
        return 'a device without a vid_pid string, or with a serial that is not one'
    by_id = {_identity(d): d for d in registered}
    ids = [_identity(d, legacy=True) for d in local]
    if len(set(ids)) != len(ids):
        return 'a device is listed twice'
    if set(ids) != set(by_id):
        def show(i):
            return sorted(f'{v} {s!r}' if s is not None else f'{v} (no serial)' for v, s in i)
        return f'devices {show(ids)}, the registry\'s {show(by_id)}'
    for i, d in zip(ids, local):
        differ = sorted(k for k in d if k not in ('vid_pid', 'serial') and (k not in by_id[i] or by_id[i][k] != d[k]))
        if differ:
            return f'{i[0]} {i[1]!r}: {", ".join(differ)} differ'
    return ''


_warned: set = set()


def _resolve(entry, path, roster=None) -> tuple:
    name = entry.get('name') if isinstance(entry, dict) else None
    label = name or '<unnamed>'
    if not isinstance(entry, dict):
        return None, f'{label}: not an object'
    uid = entry.get('uid')
    if not (isinstance(uid, str) and uid.strip()):
        return None, f'{label}: no uid (the serial its firmware reports)'
    inline = [k for k in PHYSICAL if k in entry]
    if 'probe' in entry:
        probe = entry['probe']
        reg, why = _registered(entry, path)
        if why:
            return None, why
        if inline:
            return None, (f'{label}: {" and ".join(inline)} of probe {probe} come from agentrc\'s board registry '
                          f'({reg["name"]}); drop them from the roster')
        if 'dev_attached' in reg:
            for where, local in _local_copies(entry):
                why = _copy_problem(local, reg['dev_attached'])
                kept = ('the registry\'s replaces it' if where == 'dev_attached'
                        else 'consumers reading it still get the roster\'s')
                note = (f'{label}: {where} differs from agentrc\'s board registry ({reg["name"]}): {why}; {kept}'
                        if why else f'{label}: {where} duplicates agentrc\'s board registry ({reg["name"]})')
                if roster is not None:
                    note = f'roster {roster}: {note}'
                if (path, note) not in _warned:
                    _warned.add((path, note))
                    print(f'hil_boards: {note}; drop it from the roster', file=sys.stderr)
        out = copy.deepcopy({**entry, **{k: reg[k] for k in REGISTERED if k in reg}})
        out['flasher']['uid'] = probe
        return out, ''
    if 'flasher' not in entry:
        return None, f'{label}: board {uid} names neither a registered probe nor a flasher of its own'
    for key in inline:
        if why := flasher_problem(entry[key], has_uid=key == 'flasher'):
            return None, f'{label}: {key}: {why}'
    probe = entry['flasher']['uid']
    for key, reg in registry(path).items():
        if usb_observe.jlink_serial_match(probe, key):
            return None, f'{label}: probe {probe} is registered ({reg["name"]}): name it as "probe": "{key}" instead'
    return copy.deepcopy(entry), ''


def lock_name(entry, own=None, path=REGISTRY) -> str:
    """The board lock of a roster entry: its registered board's `name`, else `own` (a project's
    lock field) or the entry's `name`. A registered board whose `own` differs is refused."""
    if 'probe' in entry:
        reg, why = _registered(entry, path)
        if why:
            raise ValueError(why)
        if own is not None and own != reg['name']:
            raise ValueError(f'{entry.get("name")}: lock {own!r} is not its registered board\'s, {reg["name"]}')
        return reg['name']
    return entry['name'] if own is None else own


def resolve(entry, path=REGISTRY, *, roster=None) -> dict:
    out, why = _resolve(entry, path, roster)
    if why:
        raise ValueError(why)
    return out


def resolve_roster(roster, path=REGISTRY) -> dict:
    if not isinstance(roster, dict) or not isinstance(roster.get('boards'), list):
        raise ValueError('a roster is {"boards": [...]}')
    out, problems = copy.deepcopy(roster), []
    for key in ('boards', 'boards-skip'):
        if key in roster:
            if not isinstance(roster[key], list):
                problems.append(f'{key} is not a list')
                continue
            results = [_resolve(e, path) for e in roster[key]]
            problems += [why for _, why in results if why]
            out[key] = [e for e, _ in results]
    if problems:
        raise ValueError('; '.join(problems))
    return out


def main(argv=None) -> int:
    argv = sys.argv[1:] if argv is None else argv
    if not (argv[:1] == ['roster'] and len(argv) == 2 or argv[:1] == ['lock'] and len(argv) == 3):
        print(__doc__.rsplit('\n\n', 1)[-1].rstrip(), file=sys.stderr)
        return 2
    try:
        with open(argv[1], encoding='utf-8-sig') as f:
            roster = json.load(f)
        if argv[0] == 'roster':
            print(json.dumps(resolve_roster(roster), indent=2))
            return 0
        found = [e for key in ('boards', 'boards-skip') for e in roster.get(key, []) if e.get('name') == argv[2]]
        if len(found) != 1:
            raise ValueError(f'{len(found)} entries named {argv[2]!r}')
        print(lock_name(found[0]))
    except (OSError, ValueError, AttributeError) as e:
        print(f'hil_boards: {argv[1]}: {e}', file=sys.stderr)
        return 1
    return 0


if __name__ == '__main__':
    sys.exit(main())
