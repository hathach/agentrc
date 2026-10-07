#!/usr/bin/env python3
"""Switch agentrc's simplify gate for the repository of the current directory,
all worktrees included, by writing `<git common dir>/simplify-gate`; prune the
user's gate cache.

    gate.py status
    gate.py on [--model M] [--effort E]  switch on; flags override the hook's defaults and persist
    gate.py off
    gate.py prune [--apply] [--days N] [--report-days N]  dry run unless --apply
    gate.py [help]
"""

import argparse
import importlib.util
import subprocess
import sys
from pathlib import Path

HOOK = Path(__file__).resolve().parents[3] / 'hooks' / 'simplify-gate' / 'simplify_gate.py'
spec = importlib.util.spec_from_file_location('simplify_gate', HOOK)
gate = importlib.util.module_from_spec(spec)
spec.loader.exec_module(gate)


def show(marker):
    overrides = gate.read_marker(marker) if marker.exists() else {}
    print(f"simplify-gate: {'on' if marker.exists() else 'off'}  ({marker})")
    for key, default in gate.DEFAULTS.items():
        value, source = (overrides[key], 'repo') if key in overrides else (default, 'default')
        print(f'{key + ":":8}{value} ({source})')


def main():
    parser = argparse.ArgumentParser(description=__doc__, formatter_class=argparse.RawDescriptionHelpFormatter)
    parser.add_argument('action', nargs='?', choices=('status', 'on', 'off', 'prune', 'help'), default='help')
    parser.add_argument('--model')
    parser.add_argument('--effort', choices=gate.EFFORTS)
    parser.add_argument('--apply', action='store_true', help='prune: act, not just report')
    parser.add_argument('--days', type=int, help=f'prune: settled sessions idle longer expire (default {gate.PRUNE_DAYS})')
    parser.add_argument('--report-days', type=int,
                        help=f'prune: report unresolved sessions idle longer (default {gate.REPORT_DAYS})')
    args = parser.parse_args()
    if args.action == 'help':
        parser.print_help()
        return
    if args.action != 'on' and (args.model or args.effort):
        parser.error('--model and --effort belong to "on"')
    if args.action != 'prune' and (args.apply or args.days is not None or args.report_days is not None):
        parser.error('--apply, --days and --report-days belong to "prune"')
    if args.action == 'prune':
        options = {k: v for k, v in (('days', args.days), ('report_days', args.report_days)) if v is not None}
        print(gate.prune(gate.cache_dir(), args.apply, **options))
        return
    try:
        marker = gate.marker_path(Path.cwd())
    except subprocess.CalledProcessError:
        sys.exit(f'{Path.cwd()}: not a git checkout')
    try:
        if args.action == 'on':
            overrides = gate.read_marker(marker) if marker.exists() else {}
            overrides.update({k: v for k, v in (('model', args.model), ('effort', args.effort)) if v})
            marker.write_text(''.join(f'{k}={v}\n' for k, v in overrides.items()))
        elif args.action == 'off':
            marker.unlink(missing_ok=True)
        show(marker)
    except ValueError as error:
        sys.exit(f'{error}\nfix the marker by hand, or "off" then "on" with the flags you want')


if __name__ == '__main__':
    main()
