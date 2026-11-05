#!/usr/bin/env python3
"""The Arduino build mechanics a library's or a core's build contract runs (agentrc `arduino` skill): selection,
arduino-cli compiles against the checkout, dependency and header provenance, image span checks and the HIL build
record. Stdlib only.

A library's `check_build.py` is its profile: it defines the names below and calls run(profile) from its own entry
point, loading this file from the pinned agentrc (its `agentrc_pin.py`). One profile per process (bind()).

  ROOT                  the library checkout (Path)
  LIBRARY_NAME          library.properties `name=`: how arduino-cli's used_libraries names it
  BOARDS                {board: {fqbn, family, [filters, span, role, chip, fixture]}}; a rig board has `span`
  REPRESENTATIVES       {family: [boards]} a change to that family's code builds
  HIL_SKETCHES          {role: (sketch dirs, the first the restoration image)}
  SMOKE_BOARDS          boards whose restoration sketch a build-contract change builds (one per image kind)
  CONTRACT_TESTS        {name: test script}; a change to `.claude/test/test_<name>.py` runs it
  NOTHING               path prefixes that build nothing (docs, agent and repo metadata)
  EXTRA_DEPENDENCIES    libraries the examples use beyond library.properties `depends=` (CI's install list)
  CONTRACT_FILES        library files a HIL record binds by sha256 (the wrapper scripts, the agentrc pin)
  classify_source(path) (effect, detail, reason) for the library's own source, or None for the common rules
  vid_pid(build_dir, board, roster_entry)  the VID:PID a device sketch enumerates with, or None (unresolved)
  record_check(board, row, build_dir, roster_entry)  optional: (extra sketch fields or None, error or '')
  AGENTRC               {commit, root, files, sha256, dirty, source}: the agentrc this process loaded (the library's
                        agentrc_pin.py): its checkout's HEAD, the closure (agentrc-relative) and each file's sha256 as
                        loaded, whether the closure differs from HEAD, and installed, pin or staged

A core's profile (an Arduino platform checkout, e.g. adafruit:samd) adds these, all optional for a library:

  PLATFORM              'vendor:arch': core mode. Sketches compile through an isolated sketchbook whose
                        hardware/<vendor>/<arch> is ROOT; LIBRARY_NAME is the platform's name (platform.txt `name=`)
  SKETCH_ROOTS          dirs whose *.ino sketch dirs are the examples (a library: ('examples',))
  exclude(board, example)  True drops a sketch the core's CI never builds on that board (beside the
                        .all.test.skip / .<filters>.test.skip / .<filters>.test.only markers a core's build_all.py reads)
  fqbn(board, example)  the FQBN a sketch builds with on `board` (default the board's): one rig board, two menus

PROFILE_API is the profile interface the three engines implement; a library's bootstrap refuses any other unloaded.

The JSON contract (stdout's last line, exits 0 pass, 1 failed, 2 usage or a missing prerequisite, 3 uncovered) is
the library's build SKILL.md.
"""

import argparse
import concurrent.futures
import fcntl
import functools
import hashlib
import importlib.util
import json
import os
import re
import shutil
import struct
import subprocess
import sys
from pathlib import Path

HERE = os.path.dirname(os.path.abspath(__file__))
FILES = ('skills/hil/boards.json', 'skills/hil/scripts/hil_boards.py', 'skills/hil/scripts/hil_load.py',
         'skills/hil/scripts/usb_observe.py')
SELF = 'skills/arduino/scripts/arduino_build.py'
SCHEMA = 2
PROFILE_API = 1   # bump on a change a library profile must follow, arduino_hil's and arduino_remote's included
COMPILE_TIMEOUT = 900
BUILD_SCRIPTS = '.claude/skills/build/scripts/'
HIL_CONTROL = ('.claude/skills/hil-contract/scripts/', 'test/hil/')
SOURCE_DIRS = ('src/', 'examples/', 'test/hil/')   # an untracked file here would be missing from a record's patch
IMAGE_KIND = {'openocd': 'elf', 'jlink': 'hex', 'esptool': 'esptool'}   # what the HIL harness flashes per flasher
BOOT_APP0 = 'tools/partitions/boot_app0.bin'   # the ESP core's otadata image its flash_args name, under the platform

P = None   # the bound profile


def _hil_load():
    """hil_load.py from this agentrc, the process's one `agentrc_hil_load`."""
    spec = importlib.util.spec_from_file_location('agentrc_hil_load', os.path.join(HERE, '..', '..', 'hil',
                                                                                    'scripts', 'hil_load.py'))
    if spec.name not in sys.modules:
        mod = importlib.util.module_from_spec(spec)
        spec.loader.exec_module(mod)
        sys.modules.setdefault(spec.name, mod)
    return sys.modules[spec.name].load(spec.name, os.path.realpath(spec.origin))


hil_load = _hil_load()
hil_load.require(__file__, FILES)
hil_boards = hil_load.load_file('skills/hil/scripts/hil_boards.py')


def bind(profile):
    """Bind the library profile this process builds for; a second, different one is refused."""
    global P
    if P is not None and P is not profile:
        raise RuntimeError(f'arduino_build is bound to another profile ({P.ROOT}), not {profile.ROOT}\'s')
    P = profile
    return sys.modules[__name__]


def role(board):
    return P.BOARDS[board].get('role', 'device')


def hil_sketches(board):
    return P.HIL_SKETCHES[role(board)]


def fixtures():
    return {b['fixture'] for b in P.BOARDS.values() if 'fixture' in b}


def fail(msg, **extra):
    print(json.dumps({'version': SCHEMA, 'pass': False, 'error': msg, **extra}))
    sys.exit(2)


def git(*args, check=True):
    p = subprocess.run(['git', '-C', str(P.ROOT), *args], capture_output=True, text=True)
    if check and p.returncode:
        fail(f'git {" ".join(args)}: {p.stderr.strip()}')
    return p.stdout


def sha256(path):
    h = hashlib.sha256()
    with open(path, 'rb') as f:
        for chunk in iter(lambda: f.read(1 << 20), b''):
            h.update(chunk)
    return h.hexdigest()


def rel(path):
    return Path(path).resolve().relative_to(P.ROOT).as_posix()


# ---- examples and ci-arduino's filters

def core_mode():
    return bool(getattr(P, 'PLATFORM', None))


def sketch_roots():
    return tuple(getattr(P, 'SKETCH_ROOTS', ('examples',)))


def is_example(path):
    return path.startswith(tuple(r + '/' for r in sketch_roots()))


@functools.lru_cache(maxsize=None)
def _examples(root, roots):
    return sorted({Path(p.parent).resolve().relative_to(root).as_posix() for r in roots
                   for p in (root / r).rglob('*.ino')})


def examples():
    """Every sketch dir (holding an .ino) under the sketch roots, repo-relative, sorted."""
    return _examples(P.ROOT, sketch_roots())


@functools.lru_cache(maxsize=None)
def _eligible(root, platform, example):
    folder = root / example
    if (folder / f'.{platform}.test.skip').exists():
        return False
    skip_txt = folder / '.skip.txt'
    if skip_txt.exists() and any(line.strip() == platform for line in skip_txt.read_text().splitlines()):
        return False
    if any(folder.glob('.*.test.only')):
        return (folder / f'.{platform}.test.only').exists()
    return True


@functools.lru_cache(maxsize=None)
def _eligible_core(root, platform, example):
    """A core's tools/build_all.py markers (adafruit SAMD and nRF52): .all.test.skip, .<platform>.test.skip, and
    .<platform>.test.only when any .*.test.only is present."""
    folder = root / example
    if (folder / '.all.test.skip').exists() or (folder / f'.{platform}.test.skip').exists():
        return False
    if any(folder.glob('.*.test.only')):
        return (folder / f'.{platform}.test.only').exists()
    return True


def eligible(board, example):
    """ci-arduino build_platform.py test_examples_in_folder (a core: its build_all.py markers and exclude()), for the
    board's filter platform. A HIL sketch outside the sketch roots is eligible on the rig boards whose role runs it,
    and nowhere else."""
    if not is_example(example):
        return 'span' in P.BOARDS[board] and example in hil_sketches(board)
    platform = P.BOARDS[board].get('filters', board)
    if not core_mode():
        return _eligible(P.ROOT, platform, example)
    exclude = getattr(P, 'exclude', None)
    return _eligible_core(P.ROOT, platform, example) and not (exclude and exclude(board, example))


def fqbn(board, example):
    hook = getattr(P, 'fqbn', None)
    return hook(board, example) if hook else P.BOARDS[board]['fqbn']


# ---- selection

def buildable(example):
    return any(eligible(b, example) for b in P.BOARDS)


def unbuildable_examples():
    """Example dirs no board of the profile may build: a library's contract tests pin the list, since no build here
    compiles them."""
    return [e for e in examples() if not buildable(e)]


def hil_sketch_of(path):
    """The HIL sketch dir outside examples/ holding `path`, or None."""
    for sketches in P.HIL_SKETCHES.values():
        for s in sketches:
            if not is_example(s) and path.startswith(s + '/'):
                return s
    return None


def example_of(path):
    """The example sketch dir holding `path`, or None."""
    return next((ex for ex in examples() if path.startswith(ex + '/')), None)


def classify(path):
    """(effect, detail, reason) for one changed path, first match wins: in a core an example sketch's own files, the
    profile's source rules, then the common ones. effect: family | shared | boards | sketches | example | hil_sketch |
    tests | fixture | hil | nothing | uncovered; `boards` builds every eligible sketch on the boards its detail names,
    `sketches` the sketch dirs its detail names on every representative."""
    if core_mode() and example_of(path):
        return 'example', example_of(path), 'example sketch'
    own = P.classify_source(path)
    if own:
        return own
    if path == 'library.properties':
        return 'shared', None, 'name= and depends= decide which libraries every build may use'
    if path.startswith('src/'):
        return 'shared', None, 'shared library code'
    if is_example(path):
        ex = example_of(path)
        if ex:
            return 'example', ex, 'example sketch'
        if path.endswith(('.md', '.txt', '.js', '.json', '.py', '.html')):
            return 'nothing', None, 'example documentation or host-side helper'
        return 'uncovered', None, 'under examples/ but in no sketch dir'
    if path.startswith(BUILD_SCRIPTS):
        return 'tests', 'build', 'the build contract; its tests and smoke builds verify it'
    if path in fixtures():
        return 'fixture', path, 'a HIL fixture compiled into every sketch of its rig boards'
    sketch = hil_sketch_of(path)
    if sketch:
        return 'hil_sketch', sketch, 'a HIL sketch: built on its rig boards, the battery on every roster board'
    if path.startswith(HIL_CONTROL):
        return 'hil', None, 'the HIL harness or rosters: its tests, and the battery on every roster board'
    if path.startswith('.claude/test/'):
        name = Path(path).stem.removeprefix('test_')
        return ('tests', name, 'a contract test') if name in P.CONTRACT_TESTS else ('nothing', None, 'test helper')
    if path.endswith('.md') or path.startswith(P.NOTHING):
        return 'nothing', None, 'docs, agent or repo metadata'
    return 'uncovered', None, 'no rule classifies this path'


def select(paths, roster=None, only_examples=None):
    builds, tests, out_paths, uncovered = {}, set(), [], []
    hil_picked, hil_full = set(), False

    def add(board, exs):
        builds.setdefault(board, set()).update(e for e in exs if eligible(board, e) and
                                               (only_examples is None or e in only_examples))

    for path in paths:
        effect, detail, reason = classify(path)
        out_paths.append({'path': path, 'rule': effect, 'reason': reason})
        if effect == 'uncovered':
            uncovered.append({'path': path, 'reason': reason})
        elif effect in ('family', 'shared'):
            families = [detail] if effect == 'family' else list(P.REPRESENTATIVES)
            boards = [b for f in families for b in P.REPRESENTATIVES[f]]
            for b in boards:
                add(b, examples())
            if roster:
                hil_picked.update(b for b in roster if P.BOARDS[b]['family'] in families)
        elif effect == 'boards':
            for b in detail:
                add(b, examples())
            if roster:
                families = {P.BOARDS[b]['family'] for b in detail}
                hil_picked.update(b for b in roster if P.BOARDS[b]['family'] in families)
        elif effect == 'sketches':
            unbuilt = [s for s in detail if not any(eligible(b, s) for f in P.REPRESENTATIVES
                                                    for b in P.REPRESENTATIVES[f])]
            uncovered += [{'path': path, 'reason': f'{s} is eligible on no representative'} for s in unbuilt]
            for b in {b for f in P.REPRESENTATIVES for b in P.REPRESENTATIVES[f]}:
                add(b, detail)
        elif effect == 'example':
            if not buildable(detail):
                uncovered.append({'path': path, 'reason': 'sketch eligible on no board this contract builds'})
            for b in P.BOARDS:
                add(b, [detail])
            if roster:
                hil_picked.update(b for b in roster if detail in hil_sketches(b))
        elif effect == 'tests':
            tests.add(detail)
            if detail == 'build':
                for b in P.SMOKE_BOARDS:
                    add(b, hil_sketches(b)[:1])
        elif effect == 'fixture':
            boards = [b for b in P.BOARDS if P.BOARDS[b].get('fixture') == detail]
            for b in boards:
                add(b, hil_sketches(b))
            if roster:
                hil_picked.update(b for b in roster if b in boards)
        elif effect == 'hil_sketch':
            for b in P.BOARDS:
                add(b, [detail])
            tests.update(t for t in ('hil', 'hil_remote') if t in P.CONTRACT_TESTS)
            hil_full = True
        elif effect == 'hil':
            tests.update(t for t in ('hil', 'hil_remote') if t in P.CONTRACT_TESTS)
            hil_full = True
    result = {
        'builds': {b: sorted(e) for b, e in sorted(builds.items()) if e},
        'tests': sorted(tests), 'paths': out_paths, 'uncovered': uncovered,
    }
    if roster is not None:
        result['hil'] = {'boards': sorted(roster) if hil_full else sorted(hil_picked), 'full': hil_full,
                         'uncovered': uncovered_by_roster(paths, roster)}
    return result


def uncovered_by_roster(paths, roster):
    """Firmware families a change reaches that no roster board on this host runs."""
    reached = set()
    for path in paths:
        effect, detail, _ = classify(path)
        if effect == 'family':
            reached.add(detail)
        elif effect == 'shared':
            reached.update(P.REPRESENTATIVES)
        elif effect == 'boards':
            reached.update(P.BOARDS[b]['family'] for b in detail)
    covered = {P.BOARDS[b]['family'] for b in roster}
    return [{'family': f, 'reason': 'no roster board of this family on this host'} for f in sorted(reached - covered)]


def changed_paths(args):
    root = P.ROOT
    if args.scope:
        out = []
        for s in args.scope:
            p = (root / s).resolve()
            if p != root and root not in p.parents:
                fail(f'--scope {s} is outside this checkout')
            if p.is_dir():
                for f in p.rglob('*'):
                    if not f.is_file() or '.git' in f.parts:
                        continue
                    target = f.resolve()
                    if root not in target.parents:
                        fail(f'--scope {s}: {f.relative_to(root)} resolves to {target}, outside this checkout')
                    out.append(rel(target))
            else:
                out.append(rel(p))
        return sorted(set(out))
    if args.diff_file:
        return sorted({line.strip() for line in Path(args.diff_file).read_text().splitlines() if line.strip()})
    def names(cmd, *a):   # -z: a path keeps its spaces and is not C-quoted (core.quotePath)
        return {n for n in git(cmd, '-z', *a).split('\0') if n}
    base = git('merge-base', args.base, 'HEAD').strip()
    paths = names('diff', '--name-only', '--no-renames', base, 'HEAD')
    if args.worktree:
        paths |= names('diff', '--name-only', '--no-renames', 'HEAD')
        paths |= names('ls-files', '--others', '--exclude-standard')
    return sorted(paths)


def rig_roster(path):
    """{name: entry} of a HIL roster whose every board has a rig profile here, resolved through hil_boards (a
    registered board's name must be its registry name, its lock); ValueError naming why not."""
    try:
        roster = json.loads(Path(path).read_text())
        unknown = [b['name'] for b in roster['boards'] if b['name'] not in P.BOARDS or 'span' not in P.BOARDS[b['name']]]
        if unknown:
            raise ValueError(f'boards with no rig profile here: {unknown}')
        boards = {b['name']: b for b in hil_boards.resolve_roster(roster)['boards']}
        for name, b in boards.items():
            hil_boards.lock_name(b, name)
    except (OSError, ValueError, KeyError, TypeError) as e:
        raise ValueError(f'roster {path}: {e}')
    return boards


def load_roster(path):
    try:
        return rig_roster(path)
    except ValueError as e:
        fail(str(e))


# ---- dependencies

def depends(props_path):
    """A library.properties' `depends=` names, version constraints dropped."""
    m = re.search(r'^depends=(.*)$', Path(props_path).read_text(errors='replace'), re.M)
    return [n for n in (re.sub(r'\s*\(.*\)\s*$', '', d).strip() for d in (m.group(1).split(',') if m else [])) if n]


def declared_dependencies():
    """library.properties `depends=` (a core has none) plus the profile's EXTRA_DEPENDENCIES, in order."""
    own = [] if core_mode() else depends(P.ROOT / 'library.properties')
    return list(dict.fromkeys([*own, *P.EXTRA_DEPENDENCIES]))


def _cli_json(*args):
    try:
        p = subprocess.run(['arduino-cli', *args, '--format', 'json'], capture_output=True, text=True, timeout=120)
    except (OSError, subprocess.TimeoutExpired) as e:
        fail(f'arduino-cli not usable: {e}', remedy='install arduino-cli on PATH')
    if p.returncode:
        fail(f'arduino-cli {" ".join(args)}: {p.stderr.strip()}')
    return json.loads(p.stdout or '{}')


def tree_digest(root):
    """sha256 over a library dir's files (relative path and the bytes they resolve to, symlinked files and dirs
    followed, each dir once, a symlinked dir's target bound too, .git excluded): what a dependency's identity binds,
    dirty or not, git checkout or not."""
    h, seen = hashlib.sha256(), set()
    root = Path(root)
    root_real = os.path.realpath(root)
    for dirpath, dirnames, filenames in os.walk(root, followlinks=True):
        real = os.path.realpath(dirpath)
        if dirpath != str(root) and os.path.islink(dirpath):
            # retargeting a link to an already-walked dir changes what compiles through it
            h.update(b'link\0' + Path(dirpath).relative_to(root).as_posix().encode() + b'\0' +
                     os.path.relpath(real, root_real).encode() + b'\0')
        if real in seen:
            dirnames[:] = []
            continue
        seen.add(real)
        dirnames[:] = sorted(d for d in dirnames if d != '.git')
        for name in sorted(filenames):
            f = Path(dirpath) / name
            h.update(f.relative_to(root).as_posix().encode() + b'\0')
            h.update(f.read_bytes() if f.is_file() else b'<dangling>')
            h.update(b'\0')
    return h.hexdigest()


def _lib_identity(lib):
    entry = {'name': lib['name'], 'version': lib.get('version'), 'dir': lib['install_dir'],
             'location': lib.get('location'), 'sha256': tree_digest(lib['install_dir'])}
    git = platform_git(lib['install_dir'])
    if git:
        entry['git'] = git
    return entry


def core_bundled_names():
    """Names of the libraries any installed core bundles (arduino-cli's packages and the sketchbook's hardware/)."""
    dirs = _cli_json('config', 'dump').get('config', {}).get('directories', {})
    cores = []
    if dirs.get('data'):
        cores += Path(dirs['data']).glob('packages/*/hardware/*/*')
    if dirs.get('user'):
        cores += Path(dirs['user']).glob('hardware/*/*')
    return {name for d in cores for name in bundled_libraries(str(d))}


def prepared_dependencies():
    """{name: identity} of the declared dependencies and everything their own library.properties `depends=` names,
    transitively, as installed in the user's libraries, checked once before any compile; exit 2 naming the
    `arduino-cli lib install` line when one is missing. A user copy of a library a core bundles joins too, marked
    `shadowOnly` (unexplained_libraries admits it per build), so it is hashed before any compile like the rest.
    In a core a library it bundles is its own: never a user copy, its depends= read from the bundled one.
    Nothing is installed here: the environment is prepared beforehand, never during parallel builds."""
    installed = {}
    for item in _cli_json('lib', 'list').get('installed_libraries', []):
        lib = item.get('library', {})
        if lib.get('location') == 'user' and lib.get('name'):
            installed.setdefault(lib['name'], lib)
    own = bundled_libraries(str(P.ROOT)) if core_mode() else {}
    order, missing, todo, seen_own = [], [], declared_dependencies(), set()
    while todo:
        name = todo.pop(0)
        if name in order or name in missing or name == P.LIBRARY_NAME or name in seen_own:
            continue
        if name in own:
            seen_own.add(name)
            props = Path(own[name]) / 'library.properties'
            todo += depends(props)
            continue
        if name not in installed:
            missing.append(name)
            continue
        order.append(name)
        props = Path(installed[name]['install_dir']) / 'library.properties'
        todo += depends(props) if props.is_file() else []
    if missing:
        fail(f'dependencies not installed: {missing}',
             remedy='arduino-cli lib install ' + ' '.join(f'"{n}"' for n in missing))
    prepared = {n: _lib_identity(installed[n]) for n in order}
    if core_mode():
        return prepared
    bundled = core_bundled_names()
    for n in sorted(set(installed) & bundled - set(order) - {P.LIBRARY_NAME}):
        prepared[n] = {**_lib_identity(installed[n]), 'shadowOnly': True}
    return prepared


def used_dependencies(prepared, rows):
    """The prepared dependencies the run binds: every declared one, and each shadowing user copy a build used, its
    `shadows` the core copy (the first by path when builds differ)."""
    shadows = {}
    for r in rows:
        for name, core_copy in r.get('shadows', {}).items():
            shadows[name] = min(shadows.get(name, core_copy), core_copy)
    return {n: ({k: v for k, v in dep.items() if k != 'shadowOnly'} | {'shadows': shadows[n]}
                if dep.get('shadowOnly') else dep)
            for n, dep in prepared.items() if not dep.get('shadowOnly') or n in shadows}


def dependency_drift(deps):
    """The dependencies whose files changed since they were resolved."""
    return [n for n, dep in deps.items() if tree_digest(dep['dir']) != dep['sha256']]


@functools.lru_cache(maxsize=None)
def bundled_libraries(platform_dir):
    """{name: dir} of the libraries a core ships under its libraries/."""
    out = {}
    for props in sorted(Path(platform_dir).glob('libraries/*/library.properties')):
        m = re.search(r'^name=(.*)$', props.read_text(errors='replace'), re.M)
        if m:
            out[m.group(1).strip()] = str(props.parent)
    return out


def unexplained_libraries(used, prepared, platform_dirs):
    """(bad, shadows): the used_libraries entries that are neither this library from its checkout, a prepared
    dependency from its installed dir, nor one bundled with a core the build used; and {name: core copy} of the
    prepared `shadowOnly` user copies this build used in place of its own core's (arduino-cli prefers the user's: the
    samd core's SPI includes Adafruit_ZeroDMA). Only this build's cores admit a shadow, so the verdict does not
    depend on what other builds ran."""
    bad, shadows = [], {}
    for lib in used:
        name, where = lib.get('name'), Path(lib.get('install_dir', '')).resolve()
        if name == P.LIBRARY_NAME:
            continue   # compile_sketch checks it came from the checkout
        if any(d and Path(d).resolve() in where.parents for d in platform_dirs):
            continue
        dep = prepared.get(name)
        if dep and Path(dep['dir']).resolve() == where:
            if not dep.get('shadowOnly'):
                continue
            core_copy = next((c for d in platform_dirs if d for c in [bundled_libraries(d).get(name)] if c), None)
            if core_copy:
                shadows[name] = core_copy
                continue
        bad.append(f'{name} from {lib.get("install_dir")}')
    return bad, shadows


# ---- build

def tool_version():
    return _cli_json('version').get('VersionString')


def foreign_headers(build_dir):
    """Headers a build's .d files took from another copy of this library (a core's bundled one, another checkout),
    or in a core from another copy of the core, each with its counterpart path here and whether the bytes match."""
    found, roots, seen = {}, {}, set()
    for dfile in Path(build_dir).rglob('*.d'):
        text = dfile.read_text(errors='replace').replace('\\\n', ' ')
        for token in (t.replace('\\ ', ' ') for t in re.split(r'(?<!\\)\s+', text) if t):
            if token.endswith(':') or not token.startswith('/') or token in seen:
                continue
            seen.add(token)
            p = Path(os.path.realpath(token)) if core_mode() else Path(token)   # the sketchbook links to ROOT
            if P.ROOT in p.parents:
                continue
            root = library_root(p, roots)
            if root:
                counterpart = P.ROOT / p.relative_to(root)
                digest, exists = sha256(p), counterpart.exists()
                found[token] = {'path': token, 'counterpart': rel(counterpart) if exists else
                                p.relative_to(root).as_posix(), 'sha256': digest,
                                'matches': exists and digest == sha256(counterpart)}
    return sorted(found.values(), key=lambda h: h['path'])


def library_root(path, cache):
    """The root of the copy of this library (a core: of this platform, by platform.txt `name=`) that `path` sits in,
    or None."""
    walked, root = [], None
    for parent in path.parents:
        if parent in cache:
            root = cache[parent]
            break
        walked.append(parent)
        props = parent / ('platform.txt' if core_mode() else 'library.properties')
        if props.exists():
            name = re.search(r'^name=(.*)$', props.read_text(errors='replace'), re.M)
            root = parent if name and name.group(1).strip() == P.LIBRARY_NAME else None
            break
    for d in walked:   # every dir walked resolves to the same answer, a root or none
        cache[d] = root
    return root


def sketch_source(board, example, build_dir):
    """The sketch dir to compile: the example, or for a board with a `fixture` a copy of it beside the build dir with
    the fixture added (examples stay untouched)."""
    fixture = P.BOARDS[board].get('fixture')
    if not fixture:
        return P.ROOT / example
    copy = build_dir.parent / '.sketch' / Path(example).name
    shutil.rmtree(copy, ignore_errors=True)
    shutil.copytree(P.ROOT / example, copy)
    shutil.copy2(P.ROOT / fixture, copy / Path(fixture).name)
    return copy


def cli_env(at, prepared):
    """A core's isolated arduino-cli environment under `at`: a config whose directories.user is a sketchbook with
    hardware/<vendor>/<arch> linked to ROOT and libraries/ holding links to the prepared dependencies only, so no user
    copy shadows a library the core bundles and no other copy of the core is in reach; data (toolchains) and
    downloads stay the user's. Returns the config path."""
    vendor, arch = P.PLATFORM.split(':')
    shutil.rmtree(at, ignore_errors=True)
    (at / 'sketchbook' / 'hardware' / vendor).mkdir(parents=True)
    (at / 'sketchbook' / 'hardware' / vendor / arch).symlink_to(P.ROOT, target_is_directory=True)
    libs = at / 'sketchbook' / 'libraries'
    libs.mkdir()
    for dep in prepared.values():
        (libs / Path(dep['dir']).name).symlink_to(Path(dep['dir']).resolve(), target_is_directory=True)
    config = _cli_json('config', 'dump').get('config', {})
    config.setdefault('directories', {})['user'] = str(at / 'sketchbook')
    path = at / 'arduino-cli.yaml'
    path.write_text(json.dumps(config))   # JSON is YAML
    return path


def compile_sketch(board, example, build_dir, properties, prepared, config=None):
    shutil.rmtree(build_dir, ignore_errors=True)
    build_dir.mkdir(parents=True)
    fqbn_ = fqbn(board, example)
    cmd = ['arduino-cli', 'compile', '--clean', '--format', 'json', '--warnings', 'all', '--fqbn', fqbn_,
           *(['--config-file', str(config)] if config else ['--library', str(P.ROOT)]), '--build-path', str(build_dir)]
    for prop in properties:
        cmd += ['--build-property', prop]
    cmd.append(str(sketch_source(board, example, build_dir)))
    try:
        p = subprocess.run(cmd, capture_output=True, text=True, timeout=COMPILE_TIMEOUT)
    except OSError as e:
        return {'board': board, 'example': example, 'status': 'error', 'buildDir': rel(build_dir),
                'firstError': f'arduino-cli: {e}', 'remedy': 'install arduino-cli on PATH'}
    except subprocess.TimeoutExpired:
        return {'board': board, 'example': example, 'status': 'failed', 'buildDir': rel(build_dir),
                'firstError': f'arduino-cli timed out after {COMPILE_TIMEOUT}s'}
    log = build_dir / 'check_build.log'
    log.write_text(' '.join(cmd) + '\n' + p.stdout + '\n' + p.stderr)
    row = {'board': board, 'example': example, 'buildDir': rel(build_dir), 'log': rel(log)}
    try:
        out = json.loads(p.stdout)
    except ValueError:
        out = {}
    err = (out.get('compiler_err') or '') + p.stderr
    if p.returncode or not out.get('success'):
        missing = re.search(r'(Platform .* is not found|platform not installed|Error resolving FQBN[^\n]*)', err)
        if missing:
            row.update(status='error', firstError=missing.group(0),
                       remedy=f'arduino-cli core install {":".join(fqbn_.split(":")[:2])}')
        else:
            lines = [ln for ln in err.splitlines() if 'error' in ln.lower()]
            row.update(status='failed', firstError=(lines or err.strip().splitlines()[-1:] or ['(no output)'])[0])
        return row
    result = out.get('builder_result', {})
    used_libs = result.get('used_libraries', [])
    platform = result.get('board_platform') or {}
    build_platform = result.get('build_platform') or {}
    if core_mode():
        wrong = [f'{p.get("id")} from {p.get("install_dir")}' for p in (platform, build_platform)
                 if not p.get('install_dir') or Path(p['install_dir']).resolve() != P.ROOT]
        if wrong:
            row.update(status='failed', firstError=f'the platform came from {wrong}, not this checkout')
            return row
        platform = {**platform, 'install_dir': str(P.ROOT)}
        build_platform = {**build_platform, 'install_dir': str(P.ROOT)}
    else:
        used = [lib for lib in used_libs if lib.get('name') == P.LIBRARY_NAME]
        if not used or Path(used[0]['install_dir']).resolve() != P.ROOT:
            row.update(status='failed', firstError=f'{P.LIBRARY_NAME} came from '
                       f'{used[0]["install_dir"] if used else "nowhere"}, not this checkout')
            return row
    bad, shadows = unexplained_libraries(used_libs, prepared,
                                         [platform.get('install_dir'), build_platform.get('install_dir')])
    if bad:
        row.update(status='failed', firstError=f'compiled against libraries outside the prepared dependencies and '
                   f'the core: {bad}', remedy='add them to library.properties depends= or the profile\'s '
                   'EXTRA_DEPENDENCIES, then arduino-cli lib install them')
        return row
    if 'chip' in P.BOARDS[board]:
        # flash_args names the core's boot_app0.bin beside the build's images: snapshot it into the build dir
        boot_app0 = Path(platform.get('install_dir', '')) / BOOT_APP0
        if not boot_app0.is_file():
            row.update(status='failed', firstError=f'{boot_app0} missing: the ESP core layout changed')
            return row
        shutil.copy2(boot_app0, build_dir / boot_app0.name)
    row.update(status='ok', fqbn=fqbn_, platform=platform, buildPlatform=build_platform, shadows=shadows,
               vidPid=P.vid_pid(build_dir, board, None), foreignHeaders=foreign_headers(build_dir),
               libraries=[{'name': lib.get('name'), 'version': lib.get('version'), 'dir': lib.get('install_dir')}
                          for lib in used_libs],
               artifacts=sorted(rel(f) for f in build_dir.glob('*.ino.*') if f.suffix in ('.elf', '.hex', '.bin',
                                                                                            '.uf2', '.map')))
    return row


def build_key(example):
    """The build dir name of a sketch under <root>/<board>/: its basename, or in a core, whose bundled libraries
    share example names, its whole path for an example (HIL sketches keep the basename the record keys on)."""
    return example.replace('/', '--') if core_mode() and is_example(example) else Path(example).name


def basename_clashes(plan):
    """Pairs of one board's sketches sharing a build key: both would build into, and the HIL record key on,
    <root>/<board>/<key>, the second replacing the first."""
    clashes = []
    for board, exs in plan.items():
        seen = {}
        for e in exs:
            first = seen.setdefault(build_key(e), e)
            if first != e:
                clashes.append(f'{board}: {first} and {e}')
    return clashes


def build_all(jobs, plan, root, properties, prepared, config=None):
    clashes = basename_clashes(plan)
    if clashes:
        fail(f'sketches sharing a basename would build into one dir: {"; ".join(clashes)}; rename one')
    rows = []
    with concurrent.futures.ThreadPoolExecutor(max_workers=jobs) as pool:
        futures = [pool.submit(compile_sketch, b, e, root / b / build_key(e), properties, prepared, config)
                   for b, exs in plan.items() for e in exs]
        for f in concurrent.futures.as_completed(futures):
            rows.append(f.result())
    return sorted(rows, key=lambda r: (r['board'], r['example']))


def run_tests(names):
    rows = []
    for name in names:
        script = P.ROOT / P.CONTRACT_TESTS[name]
        if not script.exists():
            rows.append({'test': name, 'status': 'error', 'firstError': f'{rel(script)} missing'})
            continue
        p = subprocess.run([sys.executable, str(script)], capture_output=True, text=True, timeout=600)
        rows.append({'test': name, 'status': 'ok' if p.returncode == 0 else 'failed',
                     'firstError': '' if p.returncode == 0 else (p.stderr.strip().splitlines() or ['?'])[-1]})
    return rows


# ---- images

def _elf32(path):
    data = Path(path).read_bytes()
    if data[:4] != b'\x7fELF' or data[4] != 1 or data[5] != 1:
        raise ValueError(f'{path}: not a little-endian ELF32')
    return data


def _segments(data):
    """(p_offset, p_paddr, p_filesz) of every PT_LOAD with file bytes."""
    phoff, = struct.unpack_from('<I', data, 28)
    phentsize, phnum = struct.unpack_from('<HH', data, 42)
    out = []
    for i in range(phnum):
        p_type, p_offset, _, p_paddr, p_filesz = struct.unpack_from('<IIIII', data, phoff + i * phentsize)
        if p_type == 1 and p_filesz:
            out.append((p_offset, p_paddr, p_filesz))
    return out


def elf_load_spans(path):
    """[(start, end)] the ELF's PT_LOAD segments write, by physical (load) address: what an ELF flash writes, ELF
    headers and padding included when a segment starts below the first section (the SAMD core's does, at 0)."""
    return merge(sorted((pa, pa + n) for _, pa, n in _segments(_elf32(path))))


def elf_section_spans(path):
    """[(start, end)] of the allocated sections with file bytes, at their load addresses: what objcopy puts in a
    HEX or BIN."""
    data = _elf32(path)
    segments = _segments(data)
    shoff, = struct.unpack_from('<I', data, 32)
    shentsize, shnum = struct.unpack_from('<HH', data, 46)
    spans = []
    for i in range(shnum):
        _, sh_type, sh_flags, _, sh_offset, sh_size = struct.unpack_from('<IIIIII', data, shoff + i * shentsize)
        if sh_flags & 0x2 and sh_type != 8 and sh_size:   # SHF_ALLOC, not SHT_NOBITS
            for off, pa, n in segments:
                if off <= sh_offset and sh_offset + sh_size <= off + n:
                    spans.append((pa + sh_offset - off, pa + sh_offset - off + sh_size))
                    break
    return merge(sorted(spans))


def elf_symbols(path, names):
    """{name: value} of the ELF32's .symtab symbols named in `names` (absent ones left out)."""
    data = _elf32(path)
    shoff, = struct.unpack_from('<I', data, 32)
    shentsize, shnum = struct.unpack_from('<HH', data, 46)
    headers = [struct.unpack_from('<IIIIIIIIII', data, shoff + i * shentsize) for i in range(shnum)]
    out, want = {}, set(names)
    for h in headers:
        if h[1] != 2:   # SHT_SYMTAB
            continue
        strtab = headers[h[6]]
        for off in range(h[4], h[4] + h[5], 16):
            st_name, st_value = struct.unpack_from('<II', data, off)
            end = data.index(b'\0', strtab[4] + st_name)
            name = data[strtab[4] + st_name:end].decode(errors='replace')
            if name in want:
                out[name] = st_value
    return out


def hex_runs(path):
    """[(start, bytes)] contiguous runs of an Intel HEX file."""
    runs, base = [], 0   # [(start, bytearray)]
    for line in Path(path).read_text().splitlines():
        rec = bytes.fromhex(line.strip()[1:])
        if sum(rec) & 0xff:
            raise ValueError(f'{path}: bad checksum')
        addr, kind, payload = int.from_bytes(rec[1:3], 'big'), rec[3], rec[4:4 + rec[0]]
        if kind == 0:
            start = base + addr
            if runs and runs[-1][0] + len(runs[-1][1]) == start:
                runs[-1][1].extend(payload)
            else:
                runs.append((start, bytearray(payload)))
        elif kind == 2:
            base = int.from_bytes(payload, 'big') << 4
        elif kind == 4:
            base = int.from_bytes(payload, 'big') << 16
        elif kind == 1:
            break
    return [(start, bytes(data)) for start, data in runs]


def esp_segments(build_dir, lo):
    """([(offset, file)], '') of the build's flash_args (the ESP core writes it: a line of options, then one
    "<offset> <file>" per line), or (None, why): no image on the options line, every file present in the build dir,
    offsets ascending and not overlapping."""
    try:
        lines = (Path(build_dir) / 'flash_args').read_text().splitlines()
    except OSError as e:
        return None, f'flash_args: {e}'
    options = lines[0].split() if lines else []
    if len(options) % 2 or not all(w.startswith('--') for w in options[::2]):
        return None, f'flash_args first line {lines[:1]}: want only "--option value" pairs'
    segments, end = [], lo
    for line in lines[1:]:
        words = line.split()
        if not words:
            continue
        if len(words) != 2 or not re.fullmatch(r'0x[0-9a-fA-F]+', words[0]):
            return None, f'flash_args line {line!r}: want "<offset> <file>"'
        off, name = int(words[0], 16), words[1]
        path = Path(build_dir) / name
        if '/' in name or not path.is_file():
            return None, f'flash_args names {name}, which is not a file in the build dir'
        if off < end:
            return None, f'flash_args {name} at {off:#x} overlaps the previous image (ends {end:#x})'
        segments.append((off, name))
        end = off + path.stat().st_size
    if not segments:
        return None, 'flash_args names no image'
    return segments, ''


def merge(spans):
    out = []
    for s, e in spans:
        if out and out[-1][1] == s:
            out[-1] = (out[-1][0], e)
        else:
            out.append((s, e))
    return out


# ---- HIL record

def source_identity():
    status = git('status', '--porcelain', '--untracked-files=all')
    diff = git('diff', 'HEAD', '--binary')
    ident = {'head': git('rev-parse', 'HEAD').strip(), 'dirty': bool(status.strip()),
             'diffSha256': hashlib.sha256(diff.encode()).hexdigest(), 'status': status}
    if core_mode():
        ident['submodules'] = submodules()
    return ident, diff


def submodules():
    """[{path, commit, state}] of the checkout's submodules, recursive: state is git's status prefix, ' ' at the
    gitlink, '-' not initialised, '+' at another commit, 'U' conflicted. A core bundles libraries this way."""
    out = []
    for line in git('submodule', 'status', '--recursive').splitlines():
        m = re.match(r'^([ +\-U])([0-9a-f]+) (\S+)', line)
        if m:
            out.append({'path': m.group(3), 'commit': m.group(2), 'state': m.group(1)})
    return out


def submodule_problems(ident):
    """Why the checkout's submodules keep a record from binding their bytes, [] when they do not: each must be
    initialised at its gitlink, and clean (the record's patch carries no submodule content)."""
    subs = ident.get('submodules', [])
    why = [f'{s["path"]} is {"not initialised" if s["state"] == "-" else "not at its gitlink"}'
           for s in subs if s['state'] != ' ']
    at_gitlink = {s['path'] for s in subs if s['state'] == ' '}
    return why + [f'{ln[3:]} has changes' for ln in ident['status'].splitlines() if ln[3:] in at_gitlink]


def platform_git(install_dir):
    p = subprocess.run(['git', '-C', install_dir, 'rev-parse', 'HEAD'], capture_output=True, text=True)
    if p.returncode:
        return None
    dirty = subprocess.run(['git', '-C', install_dir, 'status', '--porcelain', '--untracked-files=no'],
                           capture_output=True, text=True).stdout.strip()
    return {'commit': p.stdout.strip(), 'dirty': bool(dirty)}


def contract_identity():
    """What a HIL record binds besides its images: the library's contract files and the agentrc closure this
    process loaded (SELF and the harness's), each by sha256. The harness admits a record only when both match."""
    agentrc = Path(P.AGENTRC['root'])
    files = sorted({SELF, *P.AGENTRC.get('files', ())})
    return {'contract': {f: sha256(P.ROOT / f) for f in P.CONTRACT_FILES if (P.ROOT / f).is_file()},
            'agentrc': {'commit': P.AGENTRC['commit'], 'files': {f: sha256(agentrc / f) for f in files}}}


def agentrc_drift(files):
    """The closure files whose digests in `files` differ from the ones this process loaded (AGENTRC sha256): an
    engine already imported runs the loaded bytes, so a record or a stage must not bind newer ones."""
    loaded = P.AGENTRC['sha256']
    return sorted(f for f in {*loaded, *files} if loaded.get(f) != files.get(f))


def make_record(args, board, rows, before, diff, roster_path, prepared):
    """The HIL's build record, or (None, error)."""
    after, _ = source_identity()
    if {k: before[k] for k in ('head', 'diffSha256', 'status')} != {k: after[k] for k in ('head', 'diffSha256',
                                                                                          'status')}:
        return None, 'the source changed while building'
    lo, hi = P.BOARDS[board]['span']
    entry = rig_roster(roster_path)[board]
    flasher = entry['flasher']['name']
    if flasher not in IMAGE_KIND:
        return None, f'{board}: flasher {flasher!r} has no image kind; must be one of {sorted(IMAGE_KIND)}'
    image = IMAGE_KIND[flasher]   # what the harness flashes for this probe
    host = role(board) == 'host'
    sketches = {}
    for row in rows:
        build_dir = P.ROOT / row['buildDir']
        vid_pid = None if host else P.vid_pid(build_dir, board, entry)
        if not vid_pid and not host:
            return None, f'{row["example"]}: its VID:PID could not be resolved (the profile\'s vid_pid)'
        elf = next(build_dir.glob('*.ino.elf'))
        hexfile = next(build_dir.glob('*.ino.hex'), None)
        files, segments = [elf] + ([hexfile] if hexfile else []), None
        if image == 'elf':
            spans = elf_load_spans(elf)
        elif image == 'esptool':
            segments, why = esp_segments(build_dir, lo)
            if why:
                return None, f'{row["example"]}: {why}'
            spans = [[off, off + (build_dir / name).stat().st_size] for off, name in segments]
            files += [build_dir / 'flash_args'] + [build_dir / name for _, name in segments]
        elif not hexfile:
            return None, f'{row["example"]}: a {flasher} board flashes the HEX, and this build made none'
        else:
            spans = merge([(s, s + len(b)) for s, b in hex_runs(hexfile)])
            if spans != elf_section_spans(elf):
                return None, f'{row["example"]}: HEX runs {spans} differ from the ELF sections {elf_section_spans(elf)}'
        if not spans or any(s < lo or e > hi for s, e in spans):
            return None, f'{row["example"]}: the {image.upper()} writes {[(hex(s), hex(e)) for s, e in spans]} ' \
                         f'outside the board span [{lo:#x}, {hi:#x})'
        if not args.experimental:
            bad = [h for h in row['foreignHeaders'] if not h['matches']]
            if bad:
                return None, f'{row["example"]}: compiled against headers that differ from this checkout: ' \
                             f'{[h["path"] for h in bad]}'
        extra = {}
        if getattr(P, 'record_check', None):
            extra, why = P.record_check(board, {**row, 'loadSpans': spans}, build_dir, entry)
            if why:
                return None, f'{row["example"]}: {why}'
        if Path(row['example']).name in sketches:
            return None, f'{row["example"]}: another sketch of this record has its basename'
        sketches[Path(row['example']).name] = {
            'example': row['example'], 'fqbn': row.get('fqbn', P.BOARDS[board]['fqbn']), 'buildDir': row['buildDir'],
            'elf': rel(elf),
            'hex': rel(hexfile) if hexfile else None, 'image': image, 'loadSpans': [[s, e] for s, e in spans],
            'vidPid': vid_pid, 'foreignHeaders': row['foreignHeaders'], 'libraries': row.get('libraries', []),
            'sha256': {rel(f): sha256(f) for f in files},
            **({'segments': [[o, n] for o, n in segments]} if segments else {}), **(extra or {}),
        }
    platform = rows[0]['platform']
    identity = contract_identity()
    drift = agentrc_drift(identity['agentrc']['files'])
    if drift:
        return None, f'agentrc changed since this process loaded it: {drift}; build again'
    record = {
        'version': SCHEMA, 'kind': 'arduino-hil-build', 'library': P.LIBRARY_NAME, 'board': board,
        'fqbn': P.BOARDS[board]['fqbn'], 'experimental': bool(args.experimental),
        'source': {k: before[k] for k in ('head', 'dirty', 'diffSha256', 'submodules') if k in before},
        'buildProperties': args.build_property, 'arduinoCli': tool_version(),
        'platform': {**platform, 'git': platform_git(platform['install_dir'])},
        'dependencies': list(prepared.values()),
        'roster': {'path': rel(roster_path), 'sha256': sha256(roster_path)},
        **identity, 'agentrcSource': {'source': P.AGENTRC['source'], 'dirty': P.AGENTRC['dirty']},
        'span': [lo, hi], 'role': role(board), 'sketches': sketches,
        **({'chip': P.BOARDS[board]['chip']} if 'chip' in P.BOARDS[board] else {}),
        **({'fixture': {'path': P.BOARDS[board]['fixture'], 'sha256': sha256(P.ROOT / P.BOARDS[board]['fixture'])}}
           if 'fixture' in P.BOARDS[board] else {}),
    }
    if args.experimental:
        patch = Path(args.record).with_suffix('.patch')
        patch.parent.mkdir(parents=True, exist_ok=True)
        patch.write_text(diff)
        record['patch'] = {'path': rel(patch), 'sha256': sha256(patch)}
    return record, None


# ---- main

def parse(argv, doc):
    ap = argparse.ArgumentParser(description=(doc or __doc__).split('\n')[0])
    src = ap.add_mutually_exclusive_group()
    src.add_argument('--scope', nargs='+')
    src.add_argument('--base')
    src.add_argument('--diff-file')
    src.add_argument('--board', action='append')
    ap.add_argument('--worktree', action='store_true')
    ap.add_argument('--select-only', action='store_true')
    ap.add_argument('--hil-config')
    ap.add_argument('--example', action='append', help='narrow to these sketch dirs (examples/...)')
    ap.add_argument('--shared', action='store_true', help='keep build/hil/<board>/<sketch> (flash, HIL)')
    ap.add_argument('--record', help='write the HIL build record here (with --board B --shared --hil-config)')
    ap.add_argument('--experimental', action='store_true', help='a record from a dirty tree or with properties')
    ap.add_argument('--build-property', action='append', default=[], metavar='K=V')
    ap.add_argument('-j', '--jobs', type=int, default=4)
    args = ap.parse_args(argv)
    if not (args.scope or args.base or args.diff_file or args.board):
        fail('give --scope, --base, --diff-file or --board')
    if args.worktree and not args.base:
        fail('--worktree goes with --base')
    if args.select_only and args.board:
        fail('--select-only takes --scope, --base or --diff-file')
    if args.record and not (args.board and len(args.board) == 1 and args.shared and args.hil_config):
        fail('--record needs one --board, --shared and --hil-config')
    if args.experimental and not args.record:
        fail('--experimental only qualifies a --record')
    if args.record and args.build_property and not args.experimental:
        fail('a record with --build-property must be --experimental')
    if args.record and P.ROOT not in Path(args.record).resolve().parents:
        fail('--record must be inside this checkout (build/hil/<board>.build.json)')
    if args.record and args.example:
        fail('a record carries every HIL sketch; drop --example')
    for e in args.example or []:
        if e.rstrip('/') not in examples():
            fail(f'no sketch dir {e}')
    return args


def run(profile, argv=None):
    """The library's check_build.py entry point: bind `profile`, then main()."""
    bind(profile)
    return main(argv)


def main(argv=None):
    args = parse(argv if argv is not None else sys.argv[1:], getattr(P, '__doc__', None))
    roster = load_roster(args.hil_config) if args.hil_config else None
    # deduplicated: two workers on one sketch would share its build dir
    only = list(dict.fromkeys(e.rstrip('/') for e in args.example)) if args.example else None
    if args.board:
        unknown = [b for b in args.board if b not in P.BOARDS]
        if unknown:
            fail(f'unknown board(s) {unknown}; known: {sorted(P.BOARDS)}')
        sel = {'builds': {}, 'tests': [], 'paths': [], 'uncovered': []}
        for b in args.board:
            if args.record and b not in (roster or []):
                fail(f'{b} is not in {args.hil_config}')
            want = list(hil_sketches(b)) if args.record else (only or examples())
            sel['builds'][b] = [e for e in want if args.record or eligible(b, e)]
    else:
        sel = select(changed_paths(args), roster, only)
    # --board or --example sets what builds by hand; a record is a HIL build, not a check of a change
    narrowed = bool(args.board or args.example)
    if args.select_only:
        print(json.dumps({'version': SCHEMA, 'pass': True, 'narrowed': narrowed, 'build': sel,
                          **({'hil': sel.pop('hil')} if 'hil' in sel else {})}))
        return 0

    before, diff = source_identity()
    if args.record:
        if before['dirty'] and not args.experimental:
            fail('a HIL record needs a clean tree (commit first), or --experimental', dirty=before['status'])
        untracked = [ln[3:] for ln in before['status'].splitlines() if ln.startswith('?? ') and
                     ln[3:].startswith(SOURCE_DIRS)]
        if untracked:
            fail(f'untracked source would be missing from the record patch: {untracked}')
        stale = submodule_problems(before)
        if stale:
            fail(f'a HIL record binds submodules at their gitlinks, clean: {stale}',
                 remedy='git submodule update --init --recursive')
    prepared = prepared_dependencies() if any(sel['builds'].values()) else {}
    build_root = P.ROOT / 'build'
    shared = build_root / 'hil'
    root = shared if args.shared else build_root / f'agent-{os.getpid()}'
    lock = None
    if args.shared:
        shared.mkdir(parents=True, exist_ok=True)
        lock = open(shared / '.lock', 'w')
        try:
            fcntl.flock(lock, fcntl.LOCK_EX | fcntl.LOCK_NB)
        except BlockingIOError:
            fail(f'{rel(shared)} is in use by another build or HIL run (its .lock is held)')
    config = cli_env(build_root / f'.arduino-cli-{os.getpid()}', prepared) \
        if core_mode() and any(sel['builds'].values()) else None
    try:
        rows = build_all(args.jobs, sel['builds'], root, args.build_property, prepared, config)
    finally:
        if config:
            shutil.rmtree(config.parent, ignore_errors=True)
    tests = run_tests(sel['tests'])
    record_error, record_path = None, None
    deps = used_dependencies(prepared, rows)
    drift = dependency_drift(deps)
    if drift:
        record_error = f'dependencies changed during the build: {drift}'
    if args.record and not record_error and all(r['status'] == 'ok' for r in rows):
        record, record_error = make_record(args, args.board[0], rows, before, diff, Path(args.hil_config), deps)
        if record:
            record_path = Path(args.record)
            record_path.parent.mkdir(parents=True, exist_ok=True)
            record_path.write_text(json.dumps(record, indent=1) + '\n')
    if not args.shared:
        for r in rows:
            if r['status'] == 'ok':
                shutil.rmtree(P.ROOT / r['buildDir'], ignore_errors=True)
                r['removed'] = True
        if root.exists() and not any(root.rglob('check_build.log')):
            shutil.rmtree(root, ignore_errors=True)

    errors = [r for r in rows + tests if r['status'] == 'error']
    failed = [r for r in rows + tests if r['status'] == 'failed']
    warnings = sorted({h['path'] for r in rows for h in r.get('foreignHeaders', []) if not h['matches']})
    first = (errors or failed or [{}])[0]
    out = {
        'version': SCHEMA, 'pass': not errors and not failed and not record_error and not sel['uncovered'],
        'input': {k: before[k] for k in ('head', 'dirty', 'submodules') if k in before}, 'narrowed': narrowed, 'boards': rows, 'tests': tests,
        'paths': sel['paths'], 'uncovered': sel['uncovered'],
        'dependencies': list(deps.values()),
        'nothingToBuild': not rows and not tests,
        'firstError': record_error or first.get('firstError', ''),
        'warnings': [f'compiled against a foreign copy that differs from this checkout: {h}' for h in warnings] +
                    (['explicit --board/--example selection: automatic change selection was not verified']
                     if narrowed and not args.record else []),
    }
    if record_path:
        out['record'] = rel(record_path)
    if errors:
        out['error'] = errors[0].get('remedy') or errors[0]['firstError']
    print(json.dumps(out))
    return 2 if errors else 1 if failed or record_error else 3 if sel['uncovered'] else 0
