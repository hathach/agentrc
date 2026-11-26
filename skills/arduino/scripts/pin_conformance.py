#!/usr/bin/env python3
"""A consumer's conformance check, which its agentrc_pin.py bootstrap loads from the selected closure and runs
before `test` runs its tests: a fix here reaches consumers with a repin, not a bootstrap copy. Stdlib only."""

import ast
import json
import os
import shutil
import subprocess
import sys
import tempfile
from pathlib import Path

FILES = ()  # loads and launches nothing by path (hil_load.py)
# agentrc_pin.py's docstring names them
PUBLIC = frozenset({"ci", "config", "identity", "load", "stage", "conformance", "PinError"})


class ConformanceError(Exception):
    """The consumer's profile is unreadable; the bootstrap reports it as its own error."""


def _load_calls(path, own=False):
    """(line, target or None when not one string literal) of each agentrc_pin.load() call in a script, through any
    `import agentrc_pin as x` or `from agentrc_pin import load as y` name; `own` (the bootstrap) counts its bare
    load() too."""
    tree = ast.parse(path.read_text(encoding="utf-8"))
    modules, loads = {"agentrc_pin"}, {"load"} if own else set()
    for node in ast.walk(tree):
        if isinstance(node, ast.Import):
            modules.update(a.asname or a.name for a in node.names if a.name == "agentrc_pin")
        elif isinstance(node, ast.ImportFrom) and node.module == "agentrc_pin":
            loads.update(a.asname or a.name for a in node.names if a.name == "load")
    for node in ast.walk(tree):
        f = getattr(node, "func", None)
        if (isinstance(f, ast.Attribute) and f.attr == "load" and getattr(f.value, "id", None) in modules) or (
            isinstance(f, ast.Name) and f.id in loads
        ):
            arg = node.args[0] if len(node.args) == 1 and not node.keywords else None
            yield node.lineno, arg.value if isinstance(arg, ast.Constant) and isinstance(arg.value, str) else None


_FUNCTIONS = (ast.FunctionDef, ast.AsyncFunctionDef, ast.Lambda)
_COMPREHENSIONS = (ast.ListComp, ast.SetComp, ast.DictComp, ast.GeneratorExp)
_SCOPES = (*_FUNCTIONS, ast.ClassDef, *_COMPREHENSIONS)


def _outer_parts(scope):
    """The parts of a nested scope its enclosing scope evaluates: decorators, defaults, annotations, bases, and a
    comprehension's first iterable."""
    if isinstance(scope, _COMPREHENSIONS):
        return [scope.generators[0].iter]
    parts = list(getattr(scope, "decorator_list", []))
    if isinstance(scope, ast.ClassDef):
        return parts + scope.bases + scope.keywords
    a = scope.args
    parts += a.defaults + [d for d in a.kw_defaults if d]
    if not isinstance(scope, ast.Lambda):
        params = (*a.posonlyargs, *a.args, *a.kwonlyargs, a.vararg, a.kwarg)
        parts += [x.annotation for x in params if x and x.annotation] + ([scope.returns] if scope.returns else [])
    return parts


def _own_nodes(scope):
    """The nodes `scope` itself evaluates, each nested scope included as a node but not its body."""
    if isinstance(scope, _COMPREHENSIONS):
        g = scope.generators
        todo = [*(getattr(scope, k) for k in ("elt", "key", "value") if hasattr(scope, k)), *(x.target for x in g)]
        todo += [c for x in g for c in x.ifs] + [x.iter for x in g[1:]]
    else:
        todo = [scope.body] if isinstance(scope, ast.Lambda) else list(scope.body)
    while todo:
        node = todo.pop()
        yield node
        todo.extend(_outer_parts(node) if isinstance(node, _SCOPES) else ast.iter_child_nodes(node))


def _bindings(scope):
    """({name: whether `import agentrc_pin as name` binds it} of the names `scope` binds, its `global` names)."""
    bound, declared, glob = {}, set(), set()
    if isinstance(scope, _FUNCTIONS):
        a = scope.args
        bound.update((x.arg, False) for x in (*a.posonlyargs, *a.args, *a.kwonlyargs, a.vararg, a.kwarg) if x)
    for node in _own_nodes(scope):
        if isinstance(node, (ast.Global, ast.Nonlocal)):
            declared.update(node.names)
            if isinstance(node, ast.Global):
                glob.update(node.names)
        elif isinstance(node, (ast.Import, ast.ImportFrom)):
            for a in node.names:
                name = a.asname or a.name.partition(".")[0]
                pin = isinstance(node, ast.Import) and a.name == "agentrc_pin"
                bound[name] = bound.get(name, False) or pin
        elif isinstance(node, ast.Name) and not isinstance(node.ctx, ast.Load):
            bound.setdefault(node.id, False)
        elif isinstance(node, (ast.FunctionDef, ast.AsyncFunctionDef, ast.ClassDef, ast.ExceptHandler)) and node.name:
            bound.setdefault(node.name, False)
    return {k: v for k, v in bound.items() if k not in declared}, glob


def _internal_uses(path):
    """(line, name) of each bootstrap name outside PUBLIC a script uses: an attribute of `agentrc_pin`, or of an
    `import agentrc_pin as x` name where that import binds it (its scope and the nested ones that do not rebind x),
    or a `from agentrc_pin import` name. Module dunders such as __file__ are not the bootstrap's own."""

    def internal(name):
        return name not in PUBLIC and not (name.startswith("__") and name.endswith("__"))

    def scan(scope, visible, module):
        bound, glob = _bindings(scope)
        here = {**visible, **bound, **{n: module.get(n, False) for n in glob}}
        module = here if isinstance(scope, ast.Module) else module
        inner = visible if isinstance(scope, ast.ClassDef) else here  # a class body is no enclosing scope
        for node in _own_nodes(scope):
            if isinstance(node, ast.Attribute) and isinstance(node.value, ast.Name) and internal(node.attr) and (
                node.value.id == "agentrc_pin" or here.get(node.value.id)
            ):
                uses.append((node.lineno, node.attr))
            elif isinstance(node, _SCOPES):
                scan(node, inner, module)

    tree = ast.parse(path.read_text(encoding="utf-8"))
    uses = [
        (node.lineno, a.name)
        for node in ast.walk(tree)
        if isinstance(node, ast.ImportFrom) and node.module == "agentrc_pin"
        for a in node.names
        if internal(a.name)
    ]
    scan(tree, {}, {})
    return sorted(uses)


def _load_problems(root, bootstrap, pin_file, closure):
    problems = []
    claude = root / ".claude"
    for path in sorted((*(claude / "skills").rglob("*.py"), *(claude / "test").rglob("*.py"))):
        where = path.relative_to(root).as_posix()
        try:
            uses = _internal_uses(path)
            calls = list(_load_calls(path, own=where == bootstrap)) if path.is_relative_to(claude / "skills") else []
        except (OSError, ValueError, SyntaxError, RecursionError) as e:
            problems.append(f"{where} is unreadable ({e})")
            continue
        problems += [f"{where}:{line}: uses agentrc_pin.{name}, internal to the bootstrap" for line, name in uses]
        for line, target in calls:
            if target is None:
                problems.append(f"{where}:{line}: agentrc_pin.load() takes one string literal, not a computed target")
            elif target not in closure:
                problems.append(
                    f"{where}:{line}: agentrc_pin.load({target!r}) is outside the closure; add it to "
                    f"{pin_file} extra_engines"
                )
    return problems


def _contract_files(root, profile):
    """The profile's CONTRACT_FILES, read with ast: the profile imports the bootstrap."""
    try:
        body = ast.parse((root / profile).read_text(encoding="utf-8")).body
    except (OSError, ValueError, SyntaxError, RecursionError) as e:
        raise ConformanceError(f"{profile} is unreadable ({e})") from None
    for node in body:
        if isinstance(node, ast.Assign) and any(getattr(t, "id", None) == "CONTRACT_FILES" for t in node.targets):
            try:
                files = ast.literal_eval(node.value)
            except (ValueError, TypeError, SyntaxError, RecursionError):
                break
            if isinstance(files, (list, tuple)) and all(isinstance(f, str) for f in files):
                return files
    raise ConformanceError(f"{profile} has no CONTRACT_FILES literal of paths")


# Run with -I -B from the staged tree: what it selects and loads, and every non-stdlib module that loading imported.
_ROUND_TRIP = """if True:
    import json, os, sys, sysconfig
    sys.path.insert(0, sys.argv[1])
    import agentrc_pin as pin
    before = set(sys.modules)
    try:
        pin.check()
        ident = pin.identity()
        engines = [pin.load(e).__file__ for e in (*pin.ENGINES, *pin.config()['extra_engines'])]
    except pin.PinError as e:
        sys.exit(f'error: {e.with_remedy()}')
    paths = {k: os.path.join(v, '') for k, v in sysconfig.get_paths().items()}
    lib = (paths['stdlib'], paths['platstdlib'])
    if os.name == 'nt':  # stdlib extension modules sit in <base_exec_prefix>/DLLs, which sysconfig does not name
        lib += (os.path.join(sys.base_exec_prefix, sys.platlibdir, ''),)
    site = (paths['purelib'], paths['platlib'])  # may sit inside stdlib
    modules = sorted(f for n in set(sys.modules) - before
                     if (f := getattr(sys.modules[n], '__file__', None))
                     and (f.startswith(site) or not f.startswith(lib)))
    print(json.dumps({'source': ident['source'], 'commit': ident['commit'], 'files': ident['files'],
                      'sha256': ident['sha256'], 'engines': engines, 'modules': modules}))
"""


def round_trip(root, contract, ident, stage, bootstrap, manifest):
    """Stage the contract files and the closure as a rig tree, select from it with CI set and no install, and
    compare what it selects and loads with `ident`, the calling process's identity."""
    with tempfile.TemporaryDirectory(prefix="agentrc-pin-") as d:
        tree = Path(d).resolve()
        for rel in contract:
            if (root / rel).is_file():
                (tree / rel).parent.mkdir(parents=True, exist_ok=True)
                shutil.copyfile(root / rel, tree / rel)
        stage(tree)
        (tree / manifest).write_text(
            json.dumps({"agentrc": {"commit": ident["commit"], "dirty": ident["dirty"]}}) + "\n", encoding="utf-8"
        )
        env = {k: v for k, v in os.environ.items() if not k.startswith("GIT_") and k != "GITHUB_ACTIONS"}
        env.update(CI="true", AGENTRC_DIR=str(tree / "absent"))
        p = subprocess.run(
            [sys.executable, "-I", "-B", "-c", _ROUND_TRIP, str((tree / bootstrap).parent)],
            capture_output=True,
            text=True,
            env=env,
        )
        if p.returncode:
            return [f"a staged tree does not load: {(p.stderr or p.stdout).strip()[-800:]}"]
        got = json.loads(p.stdout.splitlines()[-1])
        want = {"source": "staged", **{k: ident[k] for k in ("commit", "files", "sha256")}}
        differ = [k for k, v in want.items() if got[k] != v]
        problems = [f"a staged tree's identity differs from this process's in {differ}"] if differ else []
        own = tree / "agentrc"
        leaked = [f for f in (*got["engines"], *got["modules"]) if not Path(f).resolve().is_relative_to(own)]
        if leaked:
            problems.append(f"a staged tree loads from outside its agentrc/: {', '.join(leaked[:5])}")
        return problems


def problems(root, *, ident, stage, required, profile, bootstrap, pin_file, manifest):
    """Why the consumer at `root` would not load the selected agentrc (`ident`) in its rig trees as its checkout
    does, [] when nothing: its `profile`'s CONTRACT_FILES must carry the `bootstrap` and its `pin_file`
    configuration, the identity must hold the `required` files (the engines, the bootstrap and its FILES), every
    agentrc_pin.load() in .claude/skills, the bootstrap's own included, must name a closure file as one string
    literal, .claude/skills and .claude/test must use only the bootstrap's PUBLIC names (both read with ast), and a
    tree staged from the contract files and the closure with `stage` must select this identity and load only from its
    own copy. Paths are consumer-relative; ConformanceError when the profile has no readable CONTRACT_FILES."""
    root = Path(root)
    contract = _contract_files(root, profile)
    found = []
    missing = [f for f in (bootstrap, pin_file) if f not in contract]
    if missing:
        found.append(f"{profile} CONTRACT_FILES lacks {missing}: a staged tree would not carry them")
    absent = [f for f in required if f not in ident["files"]]
    if absent:
        found.append(f"the agentrc closure lacks {absent}")
    found += _load_problems(root, bootstrap, pin_file, ident["files"])
    return found + ([] if missing else round_trip(root, contract, ident, stage, bootstrap, manifest))
