#!/usr/bin/env python3
"""Where tests overlap, from a coverage database recorded with per-test contexts.

  coverage_overlap.py --repo PATH --data FILE [--tests FILE] --out FILE

For each test module: the tests with measured lines in the repository's code (files under a tests/ or test/
directory, the tests themselves, are left out), how many of them reach no line (no arc, when
branch data exists) that another test of the module misses, and, with --tests (one pytest node id per line, as
`pytest --collect-only -q` prints them), the tests with no measured line at all, subprocess-only tests among them.
This locates overlapping families to read; it never says a test is redundant: two tests over the same lines can
check different outputs. provenance.json beside FILE, when present, is copied into the report; without it the
provenance is unknown. stdout is the per-module table; FILE (create-only) adds the per-test lists. Exit 0 done,
2 refused.
"""
import argparse
import json
import os
import sys
from collections import Counter, defaultdict
from pathlib import Path


class Refused(Exception):
    pass


def load(data):
    try:
        import coverage
    except ImportError:
        raise Refused('the coverage package is not installed') from None
    if not os.path.isfile(data):
        raise Refused(f'{data}: no such coverage database')
    cov = coverage.Coverage(data_file=data)
    try:
        cov.load()
    except coverage.CoverageException as e:
        raise Refused(f'{data}: {e}') from None
    return cov.get_data()


def units_by_test(d, repo):
    """{context: set of (file, unit)}, units being line numbers, or arcs when the data has them."""
    files = [f for f in d.measured_files() if Path(f).resolve().is_relative_to(repo)
             and not {'tests', 'test'} & set(Path(f).resolve().relative_to(repo).parts[:-1])]
    units = defaultdict(set)
    if d.has_arcs():
        for ctx in d.measured_contexts():
            if not ctx:
                continue
            d.set_query_context(ctx)
            reached = {(f, a) for f in files for a in d.arcs(f) or ()}
            if reached:             # a test reaching only the tests themselves stays unmeasured
                units[ctx] = reached
        d.set_query_contexts(None)
    else:
        for f in files:
            for line, contexts in d.contexts_by_lineno(f).items():
                for ctx in contexts:
                    if ctx:
                        units[ctx].add((f, line))
    return units


def node_to_context(node):
    """tests/test_x.py::Class::test_y -> test_x.Class.test_y (coverage's test_function context)."""
    path, _, rest = node.partition('::')
    return '.'.join([Path(path).stem, *rest.split('::')]) if rest else None


def analyse(units, tests=None):
    modules = defaultdict(list)
    for ctx in units:
        modules[ctx.split('.')[0]].append(ctx)
    known = set(units)
    unmeasured = defaultdict(list)
    for node in tests or ():
        ctx = node_to_context(node)
        if ctx and ctx.split('[')[0] not in known and ctx not in known:
            unmeasured[ctx.split('.')[0]].append(node)
    report = {}
    for mod in sorted(set(modules) | set(unmeasured)):
        ctxs = modules.get(mod, [])
        reach = Counter(u for c in ctxs for u in units[c])
        shared = sorted(c for c in ctxs if all(reach[u] > 1 for u in units[c]))
        report[mod] = {'measured': len(ctxs), 'no_unique_unit': shared, 'unmeasured': sorted(unmeasured.get(mod, []))}
    return report


def main(argv=None):
    p = argparse.ArgumentParser(description=__doc__.split('\n\n')[0])
    p.add_argument('--repo', required=True)
    p.add_argument('--data', required=True)
    p.add_argument('--tests')
    p.add_argument('--out', required=True)
    args = p.parse_args(argv)
    try:
        if os.path.exists(args.out):
            raise Refused(f'{args.out} exists')
        repo = Path(args.repo).resolve()
        d = load(args.data)
        tests = None
        if args.tests:
            tests = [ln.strip() for ln in Path(args.tests).read_text(encoding='utf-8').splitlines() if '::' in ln]
        units = units_by_test(d, repo)
        if not units:
            raise Refused(f'{args.data}: no per-test contexts on files under {repo} (record with dynamic_context)')
        prov = Path(args.data).with_name('provenance.json')
        result = {'data': str(Path(args.data).resolve()), 'unit': 'arc' if d.has_arcs() else 'line',
                  'provenance': json.loads(prov.read_text(encoding='utf-8')) if prov.is_file() else 'unknown',
                  'tests_inventory': bool(tests), 'modules': analyse(units, tests)}
    except (Refused, OSError, ValueError) as e:
        print(f'refused: {e}', file=sys.stderr)
        return 2
    with open(args.out, 'x', encoding='utf-8') as f:
        json.dump(result, f, indent=1)
    unit = result['unit']
    print(f"unit {unit}; provenance {'recorded' if result['provenance'] != 'unknown' else 'unknown'}; "
          f"unmeasured {'counted' if tests else 'unknown (no --tests)'}")
    print(f'{"measured":>8} {"no unique " + unit:>16} {"unmeasured":>10}  module')
    for mod, r in result['modules'].items():
        um = len(r['unmeasured']) if tests else '?'
        print(f"{r['measured']:8d} {len(r['no_unique_unit']):16d} {um:>10}  {mod}")
    return 0


if __name__ == '__main__':
    sys.exit(main())
