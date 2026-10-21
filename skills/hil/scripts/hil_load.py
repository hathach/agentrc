#!/usr/bin/env python3
"""The one path loader of the `hil` skill's scripts, and of any agentrc file a consumer loads by
path: one module per resolved file per process, registered in sys.modules. Stdlib only.

load(name, path) returns the module of the file at `path`, registered as sys.modules[name]:

- `name` registered from the same resolved path: that module, never executed again;
- `name` registered from another path, or from no file: ImportError. A bare name never mixes
  two checkouts or pins in one process;
- the same resolved path registered under another name (a consumer's own path loader that
  registers what it loads, or another load() name): that module, registered as `name` too;
- otherwise the file runs as a new module, marked initializing and registered as `name` before
  it runs (pickle finds it); one whose run raises is removed under every name it got.

Each load holds Python's import lock for `name` while it decides who owns it and publishes it, so
loads of two paths under one name, or a load and an ordinary import, never both win.

A module still running is never returned: a load waits for another thread's run (its own or an
ordinary import's), and raises ImportError for a circular load, a cross-thread cycle, or a run
that failed while it waited.

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
# Python's own per-module import locks. Holding them, a load waits for and is waited on by ordinary
# imports, and a wait that would close a cycle across threads raises _DeadlockError instead of
# hanging. Private: a CPython without these names fails this import, but one that changes what
# they do would not; tests/test_hil_load.py checks the semantics relied on, run on CPython 3.13.5.
from importlib._bootstrap import _DeadlockError, _ModuleLockManager

ROOT = os.path.dirname(os.path.dirname(os.path.dirname(os.path.dirname(os.path.abspath(__file__)))))
_FILE_LOCK = 'hil_load file '   # + resolved path: one load of a file at a time, whatever its names


def _origin(module):
    f = getattr(module, '__file__', None)
    return os.path.realpath(f) if isinstance(f, str) else None


def _complete(module, name, key):
    """`module`, found as sys.modules[key], once no other thread runs it; ImportError while it is
    still running (a circular load, or a cycle with another thread's import) or once a run that
    failed has removed it: never a partial or failed module."""
    spec = getattr(module, '__spec__', None)
    if getattr(spec, '_initializing', False):
        try:
            with _ModuleLockManager(spec.name):
                pass
        except _DeadlockError:
            pass
        if getattr(spec, '_initializing', False):
            raise ImportError(f'{name}: {module.__file__} is still being initialized', name=name)
        if sys.modules.get(key) is not module:
            raise ImportError(f'{name}: {module.__file__} failed to initialize in another thread', name=name)
    return module


def load(name: str, path) -> object:
    real = os.path.realpath(path)
    try:
        # the file's lock, then the name's: who owns `name` is decided and published under its lock
        with _ModuleLockManager(_FILE_LOCK + real), _ModuleLockManager(name):
            return _load_locked(name, real)
    except _DeadlockError as e:
        raise ImportError(f'{name}: loading {real} deadlocked with an import in another thread ({e})',
                          name=name, path=real) from e


def _load_locked(name, real):
    mod = sys.modules.get(name)
    if mod is not None:
        if _origin(mod) != real:
            raise ImportError(f'{name} is already loaded from {_origin(mod) or "no file"}, '
                              f'not {real}: one process loads one copy of it', name=name, path=real)
        return _complete(mod, name, name)
    for key, other in list(sys.modules.items()):
        if _origin(other) == real:
            sys.modules[name] = _complete(other, name, key)
            return other
    if not os.path.isfile(real):
        raise ImportError(f'{real} is missing', name=name, path=real)
    spec = importlib.util.spec_from_file_location(name, real)
    mod = importlib.util.module_from_spec(spec)
    spec._initializing = True          # as importlib marks a running module, before it is registered
    sys.modules[name] = mod
    try:
        spec.loader.exec_module(mod)
    except BaseException:
        for key, value in list(sys.modules.items()):
            if value is mod:           # its own name and any alias registered while it ran
                del sys.modules[key]
        raise
    finally:
        spec._initializing = False
    return mod


def load_file(rel: str) -> object:
    return load('agentrc_' + os.path.splitext(os.path.basename(rel))[0], os.path.join(ROOT, rel))


def require(owner: str, files) -> None:
    missing = [f for f in files if not os.path.isfile(os.path.join(ROOT, f))]
    if missing:
        raise ImportError(f'{owner} needs {", ".join(missing)}, missing under {ROOT}', path=owner)
