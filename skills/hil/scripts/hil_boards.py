#!/usr/bin/env python3
"""The farm's board registry (`skills/hil/boards.json`): how each rig board is flashed, keyed by
the serial of the debug probe it is wired to. Each board has one probe, so its `flasher` and
`flasher_recover` are farm facts, not a project's. A project roster names the board by
`probe` and keeps its own `uid`, the serial its firmware reports, which differs per firmware.
Stdlib only.

  {"boards": {"<probe serial>": {"name": <label>,
                                 "flasher": {name, args, vid_pid, verify, note},
                                 "flasher_recover": {name, args, vid_pid, verify, note}}}}

`name` is a label: rosters keep their own.

resolve(entry) is a roster entry with its board's `flasher` (its `uid` the probe serial) and
`flasher_recover` filled in, a new dict. An entry with `probe` that also carries either is
refused as a duplicate; one without `probe` (a bench board on a dev PC) keeps its own, unless
its probe is a registered board's. resolve_roster(roster) resolves `boards` and `boards-skip`.
Both raise ValueError naming every problem.

  hil_boards.py roster FILE    the resolved roster as JSON (exit 1 refused, 2 usage)
"""

from __future__ import annotations

import copy
import importlib.util
import json
import os
import sys

HERE = os.path.dirname(os.path.abspath(__file__))
REGISTRY = os.path.join(HERE, '..', 'boards.json')
FILES = ('skills/hil/scripts/hil_load.py', 'skills/hil/scripts/usb_observe.py', 'skills/hil/boards.json')
PHYSICAL = ('flasher', 'flasher_recover')
FLASHER_NAMES = ('jlink', 'stlink', 'openocd', 'lm4flash', 'esptool', 'pyocd', 'openocd_esp')
_FLASHER_KEYS = {'name', 'uid', 'args', 'vid_pid', 'verify', 'note'}
_ENTRY_KEYS = {'name', *PHYSICAL}


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


def _check(boards: dict, path) -> list:
    problems, probes = [], []
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
        if not (isinstance(e.get('name'), str) and e['name'].strip()):
            problems.append(f'{probe}: no name')
        if 'flasher' not in e:
            problems.append(f'{probe}: no flasher')
        for key in PHYSICAL:
            if key in e and (why := flasher_problem(e[key], has_uid=False)):
                problems.append(f'{probe}: {key}: {why}')
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


def _resolve(entry, path) -> tuple:
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
        reg = registry(path).get(probe) if isinstance(probe, str) else None
        if reg is None:
            return None, f'{label}: probe {probe!r} is not in agentrc\'s board registry'
        if inline:
            return None, (f'{label}: {" and ".join(inline)} of probe {probe} come from agentrc\'s board registry '
                          f'({reg["name"]}); drop them from the roster')
        out = copy.deepcopy({**entry, **{k: reg[k] for k in PHYSICAL if k in reg}})
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


def resolve(entry, path=REGISTRY) -> dict:
    out, why = _resolve(entry, path)
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
    if len(argv) != 2 or argv[0] != 'roster':
        print(__doc__.rsplit('\n\n', 1)[-1].rstrip(), file=sys.stderr)
        return 2
    try:
        with open(argv[1], encoding='utf-8-sig') as f:
            roster = json.load(f)
        print(json.dumps(resolve_roster(roster), indent=2))
    except (OSError, ValueError) as e:
        print(f'hil_boards: {argv[1]}: {e}', file=sys.stderr)
        return 1
    return 0


if __name__ == '__main__':
    sys.exit(main())
