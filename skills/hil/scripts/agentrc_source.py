#!/usr/bin/env python3
"""The identity of the agentrc a consumer repository loads its shared scripts from (`hil` skill, Shared mechanisms),
once the consumer's own bootstrap has chosen where that agentrc is. Stdlib only.

A consumer bootstrap (tinyusb's test/hil/helper/agentrc.py, tinyuf2's agentrc bootstrap) picks the source and never
imports agentrc to do it:

  staged     <consumer root>/.agentrc/ exists: the copy a staging driver wrote there with its manifest
  pin        CI (GITHUB_ACTIONS or CI true): the consumer's pinned commit, fetched by the bootstrap
  installed  otherwise "${AGENTRC_DIR:-$HOME/.claude}", never fetched

then checks this file's API literal (read with ast, before running it), loads it through hil_load and calls frozen().
Everything after that is here: the closure of the scripts the consumer loads, the one checkout it resolves into, its
commit and closure-relative dirty flag, each file's sha256, the staged copy and its manifest, and drift.

An identity is {commit, dirty, source, root, files, sha256}: `root` is the directory the files load from (the
checkout behind an installed agentrc's skill symlinks, the pin checkout, or the staged copy). A record binds
portable(identity), {commit, sha256}: an installed agentrc staged to a rig is the same agentrc there.
"""

import ast
import hashlib
import json
import os
import re
import shutil
import subprocess
from pathlib import Path, PurePosixPath, PureWindowsPath

API = 1   # bump on a change a consumer bootstrap or record must follow
FILES = ()
LOADER = 'skills/hil/scripts/hil_load.py'   # the bootstrap loads this file through it
SELF = 'skills/hil/scripts/agentrc_source.py'
STAGED = '.agentrc'   # under the consumer root: written only by a staging driver
MANIFEST = 'manifest.json'
_FROZEN = {}   # (source, root, scripts) -> identity: this module is one per process (hil_load)


class SourceError(Exception):
    """Why the selected agentrc cannot be used; the consumer's bootstrap adds its remedy."""


def _git(root, *args):
    """git in root, deaf to a caller's GIT_* (a hook's GIT_DIR or GIT_INDEX_FILE would aim it elsewhere)."""
    return subprocess.run(['git', '-C', str(root), *args], capture_output=True, text=True,
                          env={k: v for k, v in os.environ.items() if not k.startswith('GIT_')})


def sha256(path):
    return hashlib.sha256(Path(path).read_bytes()).hexdigest()


def literal(root, rel, name, default=None):
    """The literal a script assigns to `name` at module level, read without running it; SourceError for a file
    that cannot be read or parsed."""
    try:
        for node in ast.parse((Path(root) / rel).read_text(encoding='utf-8')).body:
            if isinstance(node, ast.Assign) and any(getattr(t, 'id', None) == name for t in node.targets):
                return ast.literal_eval(node.value)
    except (OSError, SyntaxError, ValueError, RecursionError) as e:
        raise SourceError(f'agentrc {rel} at {root}: {name} is unreadable ({e})') from None
    return default


def _safe_rel(rel):
    """An agentrc-relative path that stays below its root."""
    if not isinstance(rel, str) or not rel or '\\' in rel or PureWindowsPath(rel).drive:
        return False
    p = PurePosixPath(rel)
    return not p.is_absolute() and '..' not in p.parts


def closure(root, scripts):
    """Every agentrc-relative file `scripts` load or launch by path (their FILES literals, transitive by hil_load's
    rule), themselves, hil_load and this file; sorted. Refuses a missing one."""
    out = {SELF, LOADER}
    for rel in scripts:
        out.add(rel)
        if rel.endswith('.py') and (Path(root) / rel).is_file():
            out.update(literal(root, rel, 'FILES', ()))
    unsafe = sorted(f for f in out if not _safe_rel(f))
    if unsafe:
        raise SourceError(f'agentrc at {root} names files outside itself: {unsafe}')
    missing = sorted(f for f in out if not (Path(root) / f).is_file())
    if missing:
        raise SourceError(f'agentrc at {root} lacks {", ".join(missing)}')
    return sorted(out)


def pin_problem(root, commit):
    """Why `root` is not a clean checkout at `commit`, '' when it is."""
    if not (Path(root) / '.git').exists():
        return f'{root} is not a git checkout'
    head = _git(root, 'rev-parse', 'HEAD')
    if head.returncode or head.stdout.strip() != commit:
        return f'{root} is at {head.stdout.strip() or "?"}, not the pin {commit}'
    status = _git(root, 'status', '--porcelain')
    if status.returncode or status.stdout.strip():
        return f'{root} has local changes: {status.stdout.strip()[:200]}'
    return ''


def checkout_of(root, scripts):
    """(checkout, closure) of an installed agentrc at `root`: the git checkout its copy of this file resolves into,
    and the closure read there (an install links skills, not lib/). Every closure file present under root must
    resolve to that checkout's own file: a mix of installs is refused."""
    anchor = (Path(root) / SELF).resolve()
    if not anchor.is_file():
        raise SourceError(f'{root} holds no agentrc {SELF}: no agentrc is installed there')
    top = _git(anchor.parent, 'rev-parse', '--show-toplevel')
    if top.returncode:
        raise SourceError(f'{anchor} is in no git checkout: its commit is unknown')
    co = Path(top.stdout.strip()).resolve()
    files = closure(co, scripts)
    stray = [f for f in files if os.path.lexists(Path(root) / f) and (Path(root) / f).resolve() != (co / f).resolve()]
    if stray:
        raise SourceError(f'{", ".join(stray[:3])} under {root} resolve outside {co}: one agentrc checkout must hold '
                          'them all')
    return co, files


def read_staged(staged, scripts):
    """The identity of the staged copy at `staged` (<consumer root>/.agentrc): its manifest names a commit, a dirty
    flag and the digest of exactly the closure of `scripts`, each a regular file with those bytes. Any gap is a
    SourceError, never a reason to look elsewhere."""
    staged = Path(staged)
    try:
        m = json.loads((staged / MANIFEST).read_text(encoding='utf-8'))
    except (OSError, ValueError, RecursionError) as e:
        raise SourceError(f'{staged / MANIFEST} is not readable JSON ({e})') from None
    if (not isinstance(m, dict) or not isinstance(m.get('commit'), str)
            or not re.fullmatch(r'[0-9a-f]{40}', m['commit']) or not isinstance(m.get('dirty'), bool)
            or not isinstance(m.get('sha256'), dict)
            or not all(_safe_rel(k) and isinstance(v, str) for k, v in m['sha256'].items())):
        raise SourceError(f'{staged / MANIFEST} names no commit, dirty flag and file digests')
    for rel in m['sha256']:
        parts = PurePosixPath(rel).parts
        if (any(staged.joinpath(*parts[:i]).is_symlink() for i in range(len(parts) + 1))
                or not (staged / rel).is_file()):
            raise SourceError(f'staged agentrc {rel} is missing or not a regular file in a plain directory')
    # the bytes first: a closure is read from the scripts' FILES, which only verified files may declare
    bad = [f for f, digest in m['sha256'].items() if sha256(staged / f) != digest]
    if bad:
        raise SourceError(f'staged agentrc files differ from their manifest: {", ".join(sorted(bad))}')
    files = closure(staged, scripts)
    if sorted(m['sha256']) != files:
        raise SourceError(f'{staged / MANIFEST} stages {sorted(set(m["sha256"]) ^ set(files))} unlike the closure '
                          'this harness loads')
    return {'commit': m['commit'], 'dirty': m['dirty'], 'source': 'staged', 'root': str(staged), 'files': files,
            'sha256': dict(m['sha256'])}


def identity(source, root, scripts, commit=None):
    """The identity of the agentrc at `root` the consumer chose as `source`, closed over `scripts`; `commit` is the
    pin's for 'pin'."""
    if source == 'staged':
        return read_staged(root, scripts)
    if source == 'pin':
        why = pin_problem(root, commit)
        if why:
            raise SourceError(f'agentrc pin: {why}')
        co, files, dirty = Path(root).resolve(), closure(root, scripts), False
    elif source == 'installed':
        co, files = checkout_of(root, scripts)
        head, status = _git(co, 'rev-parse', 'HEAD'), _git(co, 'status', '--porcelain', '--', *files)
        if head.returncode or status.returncode:
            raise SourceError(f'git in {co}: {(head.stderr or status.stderr).strip()}')
        commit, dirty = head.stdout.strip(), bool(status.stdout.strip())
    else:
        raise SourceError(f'unknown agentrc source {source!r}')
    return {'commit': commit, 'dirty': dirty, 'source': source, 'root': str(co), 'files': files,
            'sha256': {f: sha256(co / f) for f in files}}


def frozen(source, root, scripts, commit=None):
    """identity() taken once per process for (source, root, scripts), at first use: what the process loads is what
    later records and stages bind, however many bootstraps ask."""
    key = (source, str(Path(root).absolute()), tuple(sorted(scripts)), commit)
    if key not in _FROZEN:
        _FROZEN[key] = identity(source, root, scripts, commit)
    return _FROZEN[key]


def drift(ident):
    """The closure files whose bytes now differ from `ident`'s digests (a missing one included)."""
    root = Path(ident['root'])
    return [f for f in ident['files'] if not (root / f).is_file() or sha256(root / f) != ident['sha256'][f]]


def _drifted(ident):
    files = drift(ident)
    return f'agentrc {", ".join(files)} changed since this process loaded it' if files else ''


def require(ident, rel):
    """The path of the closure file `rel` to load or run, refusing one outside the closure or any closure file whose
    bytes are no longer the identity's: a script loads its own FILES by path, past any per-file check."""
    if rel not in ident['files']:
        raise SourceError(f'{rel} is not in the agentrc closure this process took: add the script that loads it to '
                          "the consumer's SCRIPTS")
    why = _drifted(ident)
    if why:
        raise SourceError(why)
    return Path(ident['root']) / rel


def portable(ident):
    """What a record binds and admission compares: the commit and the closure digests."""
    return {'commit': ident['commit'], 'sha256': dict(ident['sha256'])}


def binding(ident):
    """portable(ident) for a record being written, refusing drift since load: the record must bind what ran."""
    why = _drifted(ident)
    if why:
        raise SourceError(why)
    return portable(ident)


def mismatch(recorded, ident):
    """Why a record's agentrc (portable form) is not `ident`'s, or the closure drifted since load, '' when neither:
    a record without one predates the binding and is rebuilt, never admitted."""
    why = _drifted(ident)
    if why:
        return why
    if not isinstance(recorded, dict) or not isinstance(recorded.get('sha256'), dict):
        return 'it binds no agentrc identity'
    if recorded != portable(ident):
        files = sorted(f for f in {*recorded['sha256'], *ident['sha256']}
                       if recorded['sha256'].get(f) != ident['sha256'].get(f))
        return (f'it was built with agentrc {str(recorded.get("commit"))[:12]}, this run loads {ident["commit"][:12]}'
                + (f' (files differ: {", ".join(files[:5])})' if files else ''))
    return ''


def stage(ident, into):
    """Copy `ident`'s closure into <into>/.agentrc as regular files with its manifest, checking each copy against
    the frozen digest (a file edited since load is refused, never staged); {.agentrc-relative path: sha256}."""
    dest = Path(into) / STAGED
    if os.path.lexists(dest):
        raise SourceError(f'{dest} exists: a staged tree is written once')
    root, out = Path(ident['root']), {}
    for f in ident['files']:
        target = dest / f
        target.parent.mkdir(parents=True, exist_ok=True)
        shutil.copyfile(root / f, target)
        if sha256(target) != ident['sha256'][f]:
            raise SourceError(f'agentrc {f} changed since this process loaded it; rerun')
        out[f'{STAGED}/{f}'] = ident['sha256'][f]
    (dest / MANIFEST).write_text(json.dumps({'commit': ident['commit'], 'dirty': ident['dirty'],
                                             'sha256': ident['sha256']}, indent=1, sort_keys=True) + '\n',
                                 encoding='utf-8')
    out[f'{STAGED}/{MANIFEST}'] = sha256(dest / MANIFEST)
    return out
