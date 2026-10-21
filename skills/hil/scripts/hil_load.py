#!/usr/bin/env python3
"""The one path loader of the `hil` skill's scripts, and of any agentrc file a consumer loads by
path: one module per resolved file per process, registered in sys.modules. Stdlib only.

load(name, path) returns the module of the file at `path`, registered as sys.modules[name]:

- `name` registered from the same resolved path: that module, never executed again;
- `name` registered from another path, or from no file: ImportError. A bare name never mixes
  two checkouts or pins in one process;
- the same resolved path registered under another name (a consumer's own path loader that
  registers what it loads, or another load() name): that module, registered as `name` too;
- otherwise the file runs as a new module registered as `name` before it runs (pickle and a
  circular load find it); one whose run raises is removed again.

load_file(rel) is load('agentrc_<stem>', ROOT/rel), the names the scripts load each other
under. Each script exports FILES, the launch-time closure of the ROOT-relative files it loads
or launches by path, transitively (this one included, never itself; () for none), a literal
that ast.literal_eval reads where the script cannot be imported yet; one that has any passes
it to require() at import, which refuses naming each missing one before anything loads.

A script reaches this module through its bootstrap, which registers this file as
`agentrc_hil_load` and refuses one registered there from another path. A consumer that loads
agentrc files by path does the same:

    spec = importlib.util.spec_from_file_location('agentrc_hil_load', DEP / 'skills/hil/scripts/hil_load.py')
    if spec.name not in sys.modules:
        mod = importlib.util.module_from_spec(spec)
        spec.loader.exec_module(mod)
        sys.modules.setdefault(spec.name, mod)
    hil_load = sys.modules[spec.name].load(spec.name, spec.origin)
    module = hil_load.load('<consumer name>', DEP / rel)

and requires and stages `rel` plus its FILES. Loaded either way round, the consumer's name and
`agentrc_<stem>` are one module.
"""

from __future__ import annotations

import importlib.util
import os
import sys
import threading

ROOT = os.path.dirname(os.path.dirname(os.path.dirname(os.path.dirname(os.path.abspath(__file__)))))

# a nested load (a module loading its siblings as it runs) comes from the same thread
_lock = threading.RLock()


def _origin(module):
    f = getattr(module, '__file__', None)
    return os.path.realpath(f) if f else None


def load(name: str, path) -> object:
    real = os.path.realpath(path)
    with _lock:
        mod = sys.modules.get(name)
        if mod is not None:
            if _origin(mod) != real:
                raise ImportError(f'{name} is already loaded from {_origin(mod) or "no file"}, '
                                  f'not {real}: one process loads one copy of it', name=name, path=real)
            return mod
        base = os.path.basename(real)
        for other in list(sys.modules.values()):
            f = getattr(other, '__file__', None)
            if f and os.path.basename(f) == base and os.path.realpath(f) == real:
                sys.modules[name] = other
                return other
        if not os.path.isfile(real):
            raise ImportError(f'{real} is missing', name=name, path=real)
        spec = importlib.util.spec_from_file_location(name, real)
        mod = importlib.util.module_from_spec(spec)
        sys.modules[name] = mod
        try:
            spec.loader.exec_module(mod)
        except BaseException:
            if sys.modules.get(name) is mod:
                del sys.modules[name]
            raise
        return mod


def load_file(rel: str) -> object:
    return load('agentrc_' + os.path.splitext(os.path.basename(rel))[0], os.path.join(ROOT, rel))


def require(owner: str, files) -> None:
    missing = [f for f in files if not os.path.isfile(os.path.join(ROOT, f))]
    if missing:
        raise ImportError(f'{owner} needs {", ".join(missing)}, missing under {ROOT}', path=owner)
