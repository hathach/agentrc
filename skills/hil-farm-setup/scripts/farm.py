#!/usr/bin/env python3
"""Install and verify the HIL farm tools pinned in farm.toml.

  farm.py validate [--host FILE]    check farm.toml, and a host file when named (no root needed)
  farm.py fetch DIR [TOOL...]       download and hash-check remote inputs into DIR (dev PC pin check)
  farm.py apt                       print the apt packages (runtime, policy unless shared, build), one per line
  farm.py env                       print the farm's environment lines
  farm.py path                      print the farm's PATH
  farm.py runner-path               print the runners' PATH: the farm's plus the host's runner_path
  farm.py host-sh                   print the host file as shell assignments for common.sh
  farm.py host-install FILE         install FILE as the host file, refusing an identity change
  farm.py guard                     fail unless every runner is stopped and no job runs
  farm.py trusted PATH...           fail when a non-root user could replace anything on the way to a PATH
  farm.py install [--force] [--inputs DIR] [TOOL...]   fresh install: every tool when none is named
  farm.py update [--force] [--inputs DIR] [TOOL...]    same, but never the runner; refuses while a runner runs
  farm.py verify [TOOL...]

The host file is $HIL_FARM_HOST when set (setup.sh --host), else the installed one.
Kind local files come from --inputs DIR.
Run as root with python3 -I from the checkout (or /opt/hil-farm); setup.sh drives it.
"""
import argparse
import contextlib
import copy
import fcntl
import glob
import hashlib
import json
import os
import re
import shlex
import shutil
import stat
import subprocess
import sys
import tarfile
import tempfile
import tomllib
import urllib.request
from pathlib import Path

HERE = Path(__file__).resolve().parent
CONFIG = HERE / 'farm.toml'
FARM_HOME = Path('/opt/hil-farm')
TOOLS_ROOT = FARM_HOME / 'tools'
RECEIPTS = Path('/var/lib/hil-farm')
LOCK_FILE = '/run/hil-farm.lock'
LINK_ROOTS = ('/usr/local/bin/', '/usr/local/sbin/', '/opt/hil/')
FIRMWARE_ROOT = '/lib/firmware/'
INSTALLED_HOST = Path('/etc/hil-farm/host.toml')
OLD_MARKER = FARM_HOME / 'profile'  # an install from before host files: its profile name
KNOWN_RUNNERS = ('tinyusb', 'tinyuf2')  # common.sh runner_conf


def runner_units(names):
    return tuple(f'hil-runner-{n}' for n in names)  # common.sh's RUNNER_UNIT


def set_runners(names):
    """The host's runners (use_host); every known one until then."""
    global RUNNERS, RUNNER_DIRS, RUNNER_UNITS
    RUNNERS = tuple(names)
    RUNNER_DIRS = tuple(f'/opt/actions-runner/{n}' for n in RUNNERS)
    RUNNER_UNITS = runner_units(RUNNERS)


set_runners(KNOWN_RUNNERS)
TRUSTED_UIDS = {0}  # who may own an installed tool's links, targets and their directories
INPUTS = None  # --inputs DIR: where kind local tools' files are

KINDS = {  # kind: (required, optional, recipes)
    'release': ({'url', 'sha256', 'version'}, {'links', 'check', 'expect'}, None),
    'local': ({'file', 'sha256', 'version'}, {'links', 'check', 'expect'}, None),
    'source': ({'url', 'sha256', 'version'}, {'cflags', 'links', 'check', 'expect'}, None),  # one C file, cc
    'git': ({'repo', 'commit', 'version', 'recipe'}, {'configure', 'files', 'links', 'check', 'expect'},
            {'openocd', 'copy'}),
    'pip': ({'lock'}, {'system_site_packages', 'no_build_isolation', 'links'}, None),
    'deb': ({'url', 'sha256', 'version', 'check'}, {'form', 'expect'}, None),
    'firmware': ({'file', 'sha256', 'version', 'dest'}, set(), None),
    'runner': ({'url', 'sha256', 'version'}, set(), None),
}
DIR_KINDS = {'release', 'local', 'source', 'git', 'pip'}
ANSI = re.compile(r'\x1b\[[0-9;]*[A-Za-z]')


class FarmError(Exception):
    pass


def load(path=CONFIG):
    with open(path, 'rb') as f:
        cfg = tomllib.load(f)
    validate(cfg)
    return cfg


def validate(cfg):
    errs = []
    if set(cfg) - {'farm', 'apt', 'tool'}:
        errs.append(f'unknown top-level tables {sorted(set(cfg) - {"farm", "apt", "tool"})}')
    if set(cfg.get('farm', {})) - {'path', 'env'}:
        errs.append('farm: unknown fields')
    if set(cfg.get('apt', {})) - {'packages', 'build', 'policy'}:
        errs.append('apt: unknown fields')
    for name, t in cfg.get('tool', {}).items():
        errs += [f'{name}: {e}' for e in validate_tool(name, t)]
    if errs:
        raise FarmError('farm.toml:\n  ' + '\n  '.join(errs))


def validate_tool(name, t):
    if not re.fullmatch(r'[a-z0-9][a-z0-9_-]*', name):
        return ['bad tool name']
    kind = t.get('kind')
    if kind not in KINDS:
        return [f'unknown kind {kind!r}']
    req, opt, recipes = KINDS[kind]
    errs = []
    fields = set(t) - {'kind'}
    if req - fields:
        errs.append(f'missing {sorted(req - fields)}')
    if fields - req - opt:
        errs.append(f'unknown fields {sorted(fields - req - opt)}')
    if recipes and t.get('recipe') not in recipes:
        errs.append(f'recipe must be one of {sorted(recipes)}')
    if 'sha256' in t and not re.fullmatch(r'[0-9a-f]{64}', t['sha256']):
        errs.append('sha256 is not 64 hex digits')
    if 'commit' in t and not re.fullmatch(r'[0-9a-f]{40}', t['commit']):
        errs.append('commit is not a full 40-digit SHA')
    for k in ('url', 'repo'):
        if k in t and not t[k].startswith('https://'):
            errs.append(f'{k} is not https')
    if 'version' in t and not re.fullmatch(r'[A-Za-z0-9._+-]+', t['version']):
        errs.append('version has unsafe characters')
    if 'expect' in t and 'check' not in t:
        errs.append('expect without check')
    files = t.get('files', [])
    if t.get('recipe') == 'copy' and not files:
        errs.append('recipe copy needs files')
    if len({Path(f).name for f in files}) != len(files):
        errs.append('files share a basename')
    for rel in [*files, t.get('lock'), t.get('file')]:
        if rel is not None and not is_relative_inside(rel):
            errs.append(f'{rel} is not a relative path inside its tree')
    if 'dest' in t and not (t['dest'].startswith(FIRMWARE_ROOT) and '..' not in Path(t['dest']).parts):
        errs.append(f'dest outside {FIRMWARE_ROOT}')
    for link, rel in t.get('links', {}).items():
        if not link.startswith(LINK_ROOTS) or '..' in Path(link).parts:
            errs.append(f'link {link} outside {LINK_ROOTS}')
        if not is_relative_inside(rel):
            errs.append(f'link target {rel} escapes the tool dir')
    return errs


# iar: verify.sh checks iccarm, cmake and ninja on the runner PATH
HOST_DEFAULTS = {'shared': False, 'iar': False, 'runner_path': []}
HOST_FIELDS = {'profile', 'admin', 'runner', *HOST_DEFAULTS}
HOST_REQUIRED = {'profile', 'admin', 'runner'}
IDENTITY = ('profile', 'admin', 'shared', 'runner')  # what only a reinstall or re-registration changes
NAME = r'[A-Za-z0-9_.-]+'


def strs(v):
    return isinstance(v, list) and all(isinstance(s, str) for s in v)


def validate_host(h, cfg):
    errs = []
    typed = {'profile': str, 'admin': str, 'shared': bool, 'iar': bool, 'runner': dict}
    bad = [k for k, v in h.items() if k in typed and not isinstance(v, typed[k])
           or k in HOST_FIELDS - set(typed) and not strs(v)]
    if bad:
        return [f'wrong types for {sorted(bad)}: profile, admin strings; shared, iar booleans; runner a table; '
                'the rest lists of strings']
    if HOST_REQUIRED - set(h):
        errs.append(f'missing {sorted(HOST_REQUIRED - set(h))}')
    if set(h) - HOST_FIELDS:
        errs.append(f'unknown fields {sorted(set(h) - HOST_FIELDS)}')
    if not re.fullmatch(r'[a-z0-9][a-z0-9_-]*', str(h.get('profile', ''))):
        errs.append('profile must be a lowercase name')
    if not re.fullmatch(r'[a-z_][a-z0-9_-]*', str(h.get('admin', ''))):
        errs.append('admin must be a user name')
    reg = h.get('runner', {})
    if not reg or set(reg) - set(KNOWN_RUNNERS):
        errs.append(f'runner must hold a table for each of some of {list(KNOWN_RUNNERS)}')
    for n, r in reg.items():
        if not isinstance(r, dict) or set(r) != {'name', 'labels'} or not isinstance(r['name'], str) \
                or not re.fullmatch(NAME, r['name']) or not strs(r['labels']) or not r['labels'] \
                or not all(re.fullmatch(NAME, lb) for lb in r['labels']):
            errs.append(f'runner.{n}: name and labels, each {NAME}')
    if not all(d.startswith('/') and '..' not in Path(d).parts and ':' not in d for d in h.get('runner_path', [])):
        errs.append('runner_path entries must be absolute, without .. or :')
    return errs


def host_path():
    return Path(os.environ.get('HIL_FARM_HOST') or INSTALLED_HOST)


def load_host(cfg, path=None):
    path = Path(path or host_path())
    try:
        with open(path, 'rb') as f:
            h = tomllib.load(f)
    except FileNotFoundError:
        raise FarmError(f'no host file {path}; name one with setup.sh --host FILE')
    except tomllib.TOMLDecodeError as e:
        raise FarmError(f'{path}: {e}')
    errs = validate_host(h, cfg)
    if errs:
        raise FarmError(f'{path}:\n  ' + '\n  '.join(errs))
    return {**copy.deepcopy(HOST_DEFAULTS), **h}


def use_host(h):
    """Set the runner globals to the host's runners; returns h."""
    set_runners(h['runner'])
    return h


def host_sh(h):
    """common.sh's view of the host file, every value shell-quoted."""
    q = shlex.quote
    def arr(name, vals):
        return f'{name}=({" ".join(q(v) for v in vals)})'
    def assoc(name, field):
        vals = ' '.join(f'[{n}]={q(field(r))}' for n, r in h['runner'].items())
        return f'declare -gA {name}=({vals})'
    return '\n'.join([
        f'ADMIN={q(h["admin"])}', f'SHARED_HOST={int(h["shared"])}', f'IAR_CHECKS={int(h["iar"])}',
        arr('RUNNERS', h['runner']),
        assoc('RUNNER_REG_NAMES', lambda r: r['name']), assoc('RUNNER_REG_LABELS', lambda r: ','.join(r['labels']))])


def install_host(cfg, src):
    """Make src the installed host file. A host keeps its identity; an install from before host files keeps its
    profile name. Refuses while any of its runners runs."""
    new = load_host(cfg, src)
    data = Path(src).read_bytes()
    old = load_host(cfg, INSTALLED_HOST) if INSTALLED_HOST.exists() else None
    if old is not None:
        if INSTALLED_HOST.read_bytes() == data:
            return False
        changed = [k for k in IDENTITY if old[k] != new[k]]
        if changed:
            raise FarmError(f'{src} changes {", ".join(changed)} of this host ({INSTALLED_HOST}); '
                            'that takes a reinstall, or for a runner its re-registration')
    elif OLD_MARKER.exists():
        if OLD_MARKER.read_text().strip() != new['profile']:
            raise FarmError(f'this host is installed as profile {OLD_MARKER.read_text().strip()}; '
                            f'{src} is for {new["profile"]}')
    elif (FARM_HOME / 'farm.toml').exists():
        raise FarmError(f'{FARM_HOME} holds an install with no host file and no profile marker; write its '
                        f'profile name to {OLD_MARKER} to confirm it is {new["profile"]}, then rerun')
    guard(sorted(runner_units(new['runner'])))  # the runners are identity: old and new name the same
    INSTALLED_HOST.parent.mkdir(mode=0o755, parents=True, exist_ok=True)
    if old is not None:
        shutil.copyfile(INSTALLED_HOST, INSTALLED_HOST.with_name('host.toml.prev'))
    tmp = INSTALLED_HOST.with_name('.host.toml.hil-farm')
    tmp.write_bytes(data)
    tmp.chmod(0o644)
    os.replace(tmp, INSTALLED_HOST)
    OLD_MARKER.unlink(missing_ok=True)
    return True


def is_relative_inside(rel):
    return bool(rel) and not Path(rel).is_absolute() and '..' not in Path(rel).parts


def contained(path, root):
    """True when path, symlinks resolved, stays under root."""
    return os.path.commonpath([os.path.realpath(path), os.path.realpath(root)]) == os.path.realpath(root)


def inside(root, rel):
    """root/rel after resolving symlinks; refuses a path that leaves root."""
    if not contained(Path(root) / rel, root):
        raise FarmError(f'{rel} resolves outside {root}')
    return Path(os.path.realpath(Path(root) / rel))


def input_hash(name, t):
    """What the install depends on: the tool's table plus any repo file it reads."""
    h = hashlib.sha256(json.dumps([name, t], sort_keys=True).encode())
    if t['kind'] == 'pip':
        h.update(inside(HERE, t['lock']).read_bytes())
    return h.hexdigest()


def new_tool_dir(name, t):
    """A fresh final directory per attempt, so no install ever builds over a live one."""
    parent = TOOLS_ROOT / name
    parent.mkdir(parents=True, exist_ok=True)
    d = Path(tempfile.mkdtemp(prefix=f'{t.get("version", "lock")}-{input_hash(name, t)[:12]}-', dir=parent))
    d.chmod(0o755)
    return d


class HttpsOnly(urllib.request.HTTPRedirectHandler):
    def redirect_request(self, req, fp, code, msg, headers, newurl):
        if not newurl.startswith('https://'):
            raise FarmError(f'refusing non-https redirect to {newurl}')
        return super().redirect_request(req, fp, code, msg, headers, newurl)


def fetch(url, sha256, dest, form=None):
    data = form.encode() if form else None
    req = urllib.request.Request(url, data=data, headers={'User-Agent': 'hil-farm'})
    opener = urllib.request.build_opener(HttpsOnly)
    with opener.open(req, timeout=300) as r, open(dest, 'wb') as f:
        shutil.copyfileobj(r, f)
    try:
        check_file(dest, sha256)
    except FarmError:
        os.unlink(dest)
        raise


def sha_file(path):
    with open(path, 'rb') as f:
        return hashlib.file_digest(f, 'sha256').hexdigest()


def check_file(path, sha256):
    got = sha_file(path)
    if got != sha256:
        raise FarmError(f'sha256 mismatch for {path}: got {got}')


def file_matches(path, sha256):
    try:
        check_file(path, sha256)
        return True
    except (OSError, FarmError):
        return False


def run(argv, **kw):
    p = subprocess.run(argv, capture_output=True, text=True, **kw)
    if p.returncode:
        raise FarmError(f'{argv[0]} failed ({p.returncode}):\n{(p.stdout + p.stderr)[-2000:]}')
    return p.stdout


def fetch_commit(repo, commit, dest):
    os.makedirs(dest)
    run(['git', '-C', dest, 'init', '-q'])
    run(['git', '-C', dest, 'fetch', '-q', '--depth', '1', repo, commit])
    run(['git', '-C', dest, 'checkout', '-q', 'FETCH_HEAD'])


def extract(archive, dest):
    try:
        with tarfile.open(archive) as tf:
            tf.extractall(dest, filter='data')
    except tarfile.FilterError as e:
        raise FarmError(f'{archive}: unsafe member: {e}')


def local_input(t):
    """A kind local tool's file, from the directory --inputs names."""
    if not INPUTS:
        raise FarmError(f'needs {t["file"]}: name the directory holding it with setup.sh --inputs DIR')
    src = inside(INPUTS, t['file'])
    if not src.is_file():
        raise FarmError(f'{src} missing; prepare it with prepare-stm32cubeprogrammer.sh (README)')
    return src


def build(name, t, dest, work):
    """Fill dest, the tool's final directory, from its pinned inputs."""
    kind = t['kind']
    if kind == 'release':
        fetch(t['url'], t['sha256'], work / 'a')
        extract(work / 'a', dest)
    elif kind == 'local':
        # checked and extracted from root's copy, which no one can rewrite in between; on disk beside the install,
        # not in work's tmpfs, as the input can be hundreds of MB
        with tempfile.NamedTemporaryFile(dir=dest.parent, prefix='.input-') as copy_:
            with open(local_input(t), 'rb') as f:
                shutil.copyfileobj(f, copy_)
            copy_.flush()
            check_file(copy_.name, t['sha256'])
            extract(copy_.name, dest)
    elif kind == 'source':
        src = work / Path(t['url'].split('?')[0]).name
        fetch(t['url'], t['sha256'], src)
        run(['cc', *t.get('cflags', []), '-o', str(dest / name), str(src)])
    elif kind == 'git':
        src = work / 'src'
        fetch_commit(t['repo'], t['commit'], src)
        if t['recipe'] == 'copy':
            for f in t['files']:
                shutil.copyfile(inside(src, f), dest / Path(f).name)
                os.chmod(dest / Path(f).name, 0o755)
        else:
            run(['./bootstrap', 'nosubmodule'], cwd=src)
            run(['./configure', '-q', f'--prefix={dest}', *t.get('configure', [])], cwd=src)
            # INFO_DEPS= skips the info manual: configure's version.texi is newer than the tracked doc/*.info,
            # so make would rewrite them mid-build and the version banner would read -dirty
            run(['make', '-s', f'-j{os.cpu_count()}', 'INFO_DEPS='], cwd=src)
            run(['make', '-s', 'install', 'INFO_DEPS='], cwd=src)
    elif kind == 'pip':
        os.rmdir(dest)  # venv creates it
        venv = [sys.executable, '-I', '-m', 'venv']  # -I: no module from the caller's cwd runs as root
        if t.get('system_site_packages'):
            venv.append('--system-site-packages')
        run([*venv, str(dest)])
        pip = [str(dest / 'bin' / 'python3'), '-I', '-m', 'pip', '-q']
        lock = str(inside(HERE, t['lock']))
        # download builds an sdist's metadata too: isolated, it would fetch and run unpinned build requirements
        iso = ['--no-build-isolation'] if t.get('no_build_isolation') else []
        run([*pip, 'download', '--no-deps', '--require-hashes', *iso, '-r', lock, '--dest', str(work / 'wheels')])
        opts = ['--no-index', '--find-links', str(work / 'wheels'), '--require-hashes', *iso]
        if t.get('system_site_packages'):
            opts.append('--ignore-installed')  # the lock's versions, never Debian's
        run([*pip, 'install', *opts, '-r', lock])


def resolve_links(t, dest):
    """{link path: target path} for a tool installed in dest; refuses globs that miss or escape."""
    out = {}
    for link, rel in t.get('links', {}).items():
        if link.endswith('/'):
            matches = sorted(glob.glob(str(dest / rel)))
            if not matches:
                raise FarmError(f'link glob {rel} matches nothing in {dest}')
            pairs = [(Path(link) / Path(m).name, Path(m)) for m in matches]
        else:
            pairs = [(Path(link), dest / rel if rel != '.' else dest)]
        for lk, target in pairs:
            if Path(lk) in out:
                raise FarmError(f'two targets for {lk}')
            if not contained(target, dest):
                raise FarmError(f'{target} escapes {dest}')
            if link.endswith('/') and not target.is_file():
                raise FarmError(f'{target} is not a file')
            if not target.exists():
                raise FarmError(f'{target} does not exist')
            out[lk] = target
    return out


def owned_links(name):
    """Links under LINK_ROOTS that currently point into this tool's directories."""
    mine = str(TOOLS_ROOT / name) + '/'
    found = {}
    for root in LINK_ROOTS:
        if not os.path.isdir(root):
            continue
        for entry in os.scandir(root):
            if entry.is_symlink() and os.readlink(entry.path).startswith(mine):
                found[Path(entry.path)] = Path(os.readlink(entry.path))
    return found


def switch_links(name, links):
    mine = str(TOOLS_ROOT / name) + '/'
    for lk in links:
        if lk.is_symlink():
            if not os.readlink(lk).startswith(mine):
                raise FarmError(f'{lk} is a link {name} does not own (-> {os.readlink(lk)}); remove it first')
        elif lk.exists():
            raise FarmError(f'{lk} exists and is not a link; remove it first')
    for lk, target in links.items():
        lk.parent.mkdir(parents=True, exist_ok=True)
        tmp = lk.with_name(f'.{lk.name}.hil-farm')
        if tmp.is_symlink():
            tmp.unlink()
        os.symlink(target, tmp)
        os.replace(tmp, lk)
    for lk in set(owned_links(name)) - set(links):
        lk.unlink()


def run_check(t, links=None):
    """True when the check exits 0 with `expect` (default: `version`) as a token; links maps argv[0] to a staged
    target."""
    if 'check' not in t:
        return True
    argv = list(t['check'])
    if links and Path(argv[0]) in links:
        argv[0] = str(links[Path(argv[0])])
    try:
        p = subprocess.run(argv, capture_output=True, text=True, timeout=120)
    except (OSError, subprocess.TimeoutExpired):
        return False
    return p.returncode == 0 and expected(t) in ANSI.sub('', p.stdout + p.stderr).split()


def expected(t):
    return t.get('expect', t.get('version'))


def lock_matches(t, dest):
    """True when the venv holds exactly the lock's pinned versions."""
    pins = re.findall(r'^([A-Za-z0-9_.-]+)==(\S+)', inside(HERE, t['lock']).read_text(), re.M)
    code = ('import importlib.metadata as m, json, sys\n'
            'bad = [n for n, v in json.loads(sys.argv[1]) if m.version(n) != v]\n'
            'sys.exit(bool(bad))')
    try:
        return subprocess.run([str(dest / 'bin' / 'python3'), '-I', '-c', code, json.dumps(pins)],
                              capture_output=True).returncode == 0
    except OSError:
        return False


def file_digests(links):
    """{link: [sha256, mode]} of the links' file targets, each of which must be executable."""
    out = {}
    for lk, target in links.items():
        if target.is_file():
            if not os.access(target, os.X_OK):
                raise FarmError(f'{target} is not executable')
            out[str(lk)] = [sha_file(target), oct(target.stat().st_mode & 0o7777)]
    return out


def writable_by_others(path, hops=40):
    """The first entry on the way to path (every directory walked and every symlink followed, their own
    chains included) that anyone outside TRUSTED_UIDS could replace, or None: root runs some of these
    (sudo hil_root), so a writable link, target or directory is root for its writer. `..` applies to
    the directory reached so far, after its symlinks resolved, as the kernel walks it."""
    path = os.path.join(os.getcwd(), path)   # no normalising: `..` after a symlink is not lexical
    cur = Path('/')
    for name in ['/', *path.split('/')]:
        if name in ('', '.'):
            continue
        if name == '..':
            cur = cur.parent
            continue
        p = cur / name
        st = os.lstat(p)
        sticky_dir = stat.S_ISDIR(st.st_mode) and st.st_mode & stat.S_ISVTX
        if st.st_uid not in TRUSTED_UIDS or (not stat.S_ISLNK(st.st_mode) and st.st_mode & 0o022 and not sticky_dir):
            return p
        if stat.S_ISLNK(st.st_mode):
            if hops == 0:
                return p
            bad = writable_by_others(os.path.join(str(cur), os.readlink(p)), hops - 1)
            if bad:
                return bad
            p = Path(os.path.realpath(p))
        cur = p
    return None


def candidate_problems(t, dest, links=None):
    """Problems with the tool in dest: its check (through links when staged, else the public path) and its lock."""
    probs = []
    if not run_check(t, links):
        probs.append(f'{" ".join(t["check"])} does not report {expected(t)}')
    if t['kind'] == 'pip' and not lock_matches(t, dest):
        probs.append(f'venv {dest} does not match {t["lock"]}')
    return probs


def read_receipt(name):
    try:
        return json.loads((RECEIPTS / f'{name}.json').read_text())
    except (OSError, ValueError):
        return None


def write_receipt(name, t, **extra):
    RECEIPTS.mkdir(mode=0o755, parents=True, exist_ok=True)
    rec = {'input': input_hash(name, t), 'version': t.get('version'), **extra}
    tmp = RECEIPTS / f'.{name}.json'
    tmp.write_text(json.dumps(rec, indent=1) + '\n')
    os.replace(tmp, RECEIPTS / f'{name}.json')


def verify_tool(name, t):
    """List of problems with the installed tool; empty when it matches farm.toml."""
    rec = read_receipt(name)
    if not rec:
        return ['not installed (no receipt)']
    probs = []
    if rec['input'] != input_hash(name, t):
        probs.append(f'installed {rec.get("version")} differs from farm.toml {t.get("version", "")}'.rstrip())
    kind = t['kind']
    if kind in DIR_KINDS:
        if 'dir' not in rec:
            return probs + ['installed as another kind (receipt has no dir)']
        dest = Path(rec['dir'])
        try:
            want = resolve_links(t, dest)
            if file_digests(want) != rec.get('files'):
                probs.append(f'files in {dest} changed since install')
        except FarmError as e:
            return probs + [str(e)]
        for lk, target in want.items():
            if not lk.is_symlink() or Path(os.readlink(lk)) != target:
                probs.append(f'{lk} does not point to {target}')
                continue
            bad = writable_by_others(lk)   # follows lk, so its target's chain too
            if bad:
                probs.append(f'{bad} on the path to {lk} is writable by a non-root user')
        if probs:   # never run a check through a link or file that is not the one installed
            return probs
        return candidate_problems(t, dest)
    if kind == 'firmware':
        if not file_matches(t['dest'], t['sha256']):
            probs.append(f'{t["dest"]} is missing or differs from {t["file"]}')
    elif kind == 'runner':  # Runner.Listener needs installdependencies.sh before it can run
        probs += [f'{d} holds no runner' for d in RUNNER_DIRS if not os.path.isfile(f'{d}/bin/Runner.Listener')]
    else:
        probs += candidate_problems(t, None)
    return probs


def install_tool(name, t, force):
    if not force and not verify_tool(name, t):
        print(f'ok    {name} {t.get("version", "")}')
        return
    kind = t['kind']
    with tempfile.TemporaryDirectory(prefix=f'hil-farm-{name}-') as work:
        work = Path(work)
        if kind in DIR_KINDS:
            dest = new_tool_dir(name, t)
            build(name, t, dest, work)
            links = resolve_links(t, dest)
            probs = candidate_problems(t, dest, links)
            if probs:
                raise FarmError(f'{name}: the new install in {dest}: ' + '; '.join(probs))
            files = file_digests(links)
            switch_links(name, links)
            write_receipt(name, t, dir=str(dest), files=files)
        elif kind == 'deb':
            fetch(t['url'], t['sha256'], work / f'{name}.deb', t.get('form'))
            run(['apt-get', 'install', '-y', '-qq', str(work / f'{name}.deb')],
                env={**os.environ, 'DEBIAN_FRONTEND': 'noninteractive'})
            write_receipt(name, t)
        elif kind == 'firmware':
            src = inside(HERE, t['file'])
            check_file(src, t['sha256'])
            rec = read_receipt(name) or {}
            matches = file_matches(t['dest'], t['sha256'])
            done = matches and rec.get('input') == input_hash(name, t)
            if not done:  # until initramfs holds the new file, nothing may say it is installed
                (RECEIPTS / f'{name}.json').unlink(missing_ok=True)
            if not matches:
                tmp = Path(t['dest']).with_name(f'.{Path(t["dest"]).name}.hil-farm')
                shutil.copyfile(src, tmp)
                os.chmod(tmp, 0o644)
                os.replace(tmp, t['dest'])
            if not done:
                run(['update-initramfs', '-u', '-k', 'all'])
                print(f'      {name}: {t["dest"]} installed; reboot pending')
            write_receipt(name, t)
        elif kind == 'runner':  # the same pin into every runner dir that has none; never over an installed one
            held = [d for d in RUNNER_DIRS if os.path.exists(f'{d}/bin')]
            rec = read_receipt(name)
            if len(held) == len(RUNNER_DIRS) or (held and not (rec and rec['input'] == input_hash(name, t))):
                raise FarmError(f'{name}: {", ".join(held)} already hold a runner; runner updates are not supported')
            fetch(t['url'], t['sha256'], work / 'runner.tar.gz')
            write_receipt(name, t)  # before any dir is published, so a retry after an interruption fills the rest
            for d in RUNNER_DIRS:
                if d not in held:
                    install_runner(work / 'runner.tar.gz', Path(d))
    probs = verify_tool(name, t)
    if probs:
        raise FarmError(f'{name}: ' + '; '.join(probs))
    print(f'inst  {name} {t.get("version", "")}')


def install_runner(archive, d):
    """Extract archive beside d and rename it into place whole: a runner dir with bin/ counts as installed, so an
    interrupted extraction must never leave one."""
    d.parent.mkdir(mode=0o755, parents=True, exist_ok=True)
    tmp = Path(tempfile.mkdtemp(prefix=f'.{d.name}.hil-farm-', dir=d.parent))
    try:
        tmp.chmod(0o755)
        extract(archive, tmp)
        try:
            os.rename(tmp, d)   # replaces only a missing or empty d
        except OSError as e:
            raise FarmError(f'{d} exists without a runner and is not empty ({e.strerror}); remove it first')
    except BaseException:
        shutil.rmtree(tmp, ignore_errors=True)
        raise


def guard(units=None):
    """Refuse to change the farm while a runner may run a job; fail closed on any doubt."""
    try:
        states = {u: subprocess.run(['systemctl', 'is-active', u], capture_output=True, text=True).stdout.strip()
                  for u in (units or RUNNER_UNITS)}
        workers = subprocess.run(['pgrep', '-x', 'Runner.Worker'], capture_output=True).returncode
    except OSError as e:
        raise FarmError(f'cannot inspect the runners: {e}')
    for unit, state in states.items():
        if state not in ('inactive', 'failed'):
            raise FarmError(f'{unit} is {state or "unknown"}; wait for idle, then systemctl stop {unit}')
    if workers != 1:
        raise FarmError('a Runner.Worker is running' if workers == 0 else 'pgrep failed; cannot tell whether a job runs')


def take_lock():
    if os.environ.get('HIL_FARM_LOCK_HELD') == '1':  # setup.sh holds it for the whole run
        return None
    f = open(LOCK_FILE, 'w')
    try:
        fcntl.flock(f, fcntl.LOCK_EX | fcntl.LOCK_NB)
    except BlockingIOError:
        raise FarmError(f'another setup holds {LOCK_FILE}')
    return f


@contextlib.contextmanager
def root_lock():
    """Root, holding the setup lock unless setup.sh holds it, with umask 022 whatever sudo passed: the runners must
    read and traverse what root creates."""
    if os.geteuid() != 0:
        raise FarmError('run as root')
    lock = take_lock()
    umask = os.umask(0o022)
    try:
        yield
    finally:
        os.umask(umask)
        if lock:
            lock.close()


def select(cfg, names, skip_runner):
    tools = cfg['tool']
    if not names:
        picked = {n: t for n, t in tools.items() if not (skip_runner and t['kind'] == 'runner')}
        if skip_runner:
            for n in set(tools) - set(picked):
                print(f'skip  {n}: runner updates are not supported')
        return picked
    unknown = [n for n in names if n not in tools]
    if unknown:
        raise FarmError(f'unknown tools {unknown}')
    if skip_runner and any(tools[n]['kind'] == 'runner' for n in names):
        raise FarmError('runner updates are not supported')
    return {n: tools[n] for n in names}


def main(argv=None):
    ap = argparse.ArgumentParser(description=__doc__, formatter_class=argparse.RawDescriptionHelpFormatter)
    sub = ap.add_subparsers(dest='cmd', required=True)
    sub.add_parser('validate').add_argument('--host')
    p = sub.add_parser('fetch')
    p.add_argument('dir')
    p.add_argument('tools', nargs='*')
    sub.add_parser('apt')
    sub.add_parser('env')
    sub.add_parser('path')
    sub.add_parser('runner-path')
    sub.add_parser('host-sh')
    sub.add_parser('host-install').add_argument('file')
    sub.add_parser('guard')
    sub.add_parser('trusted').add_argument('paths', nargs='+')
    for cmd in ('install', 'update'):
        p = sub.add_parser(cmd)
        p.add_argument('--force', action='store_true', help='reinstall even when verify passes')
        p.add_argument('--inputs', help="the directory holding kind local tools' files")
        p.add_argument('tools', nargs='*')
    sub.add_parser('verify').add_argument('tools', nargs='*')
    args = ap.parse_args(argv)
    global INPUTS
    INPUTS = getattr(args, 'inputs', None)
    try:
        cfg = load()
        if args.cmd == 'validate':
            if args.host:
                load_host(cfg, args.host)
                print(f'{args.host}: ok')
            print(f'farm.toml: {len(cfg["tool"])} tools ok')
            return 0
        if args.cmd == 'host-install':
            with root_lock():
                print(f'host  {INSTALLED_HOST} {"installed" if install_host(cfg, args.file) else "unchanged"}')
            return 0
        host = use_host(load_host(cfg)) if args.cmd not in ('fetch', 'env', 'path', 'trusted') else None
        if args.cmd == 'host-sh':
            print(host_sh(host))
        elif args.cmd == 'fetch':
            for n, t in select(cfg, args.tools, False).items():
                if 'url' in t:
                    fetch(t['url'], t['sha256'], Path(args.dir) / n, t.get('form'))
                    print(f'ok    {n}')
        elif args.cmd == 'apt':
            policy = [] if host['shared'] else cfg['apt']['policy']
            print('\n'.join(cfg['apt']['packages'] + policy + cfg['apt']['build']))
        elif args.cmd == 'env':
            print('\n'.join(f'{k}={v}' for k, v in cfg['farm']['env'].items()))
        elif args.cmd == 'path':
            print(cfg['farm']['path'])
        elif args.cmd == 'runner-path':
            print(':'.join([cfg['farm']['path'], *host['runner_path']]))
        elif args.cmd == 'guard':
            guard()
        elif args.cmd == 'trusted':
            bad = []
            for p in args.paths:
                try:
                    if (b := writable_by_others(p)):
                        bad.append(f'{p}: {b} is writable by a non-root user')
                except OSError as e:
                    bad.append(f'{p}: {e}')
            if bad:
                raise FarmError('\n'.join(bad))
        elif args.cmd == 'verify':
            bad = 0
            for n, t in select(cfg, args.tools, False).items():
                probs = verify_tool(n, t)
                print(f'ok    {n} {t.get("version", "")}' if not probs else f'FAIL  {n}: ' + '; '.join(probs))
                bad += bool(probs)
            return 1 if bad else 0
        else:
            with root_lock():
                guard()
                for n, t in select(cfg, args.tools, args.cmd == 'update').items():
                    install_tool(n, t, args.force)
    except FarmError as e:
        print(f'farm: {e}', file=sys.stderr)
        return 1
    return 0


if __name__ == '__main__':
    sys.exit(main())
