#!/usr/bin/env python3
"""Regenerate golden.json from the projects' own report code, for test_hil_report_core:

  gen_golden.py TINYUSB_CHECKOUT TINYUF2_CHECKOUT

tinyusb's helper/hil_report.py render_report() of every inputs.json `tinyusb_docs` document
(the Markdown its write_report writes, less the trailing newline) and tinyuf2's hil_ci.py
verdict() of every `tinyuf2_verdicts` case, with both checkouts' commits recorded.
"""
import importlib.util
import json
import subprocess
import sys
from pathlib import Path

HERE = Path(__file__).resolve().parent


def load(name, path):
    spec = importlib.util.spec_from_file_location(name, path)
    mod = importlib.util.module_from_spec(spec)
    spec.loader.exec_module(mod)
    return mod


def commit(checkout):
    return subprocess.run(['git', '-C', str(checkout), 'rev-parse', 'HEAD'], capture_output=True, text=True,
                          check=True).stdout.strip()


def main(tinyusb, tinyuf2):
    tinyusb, tinyuf2 = Path(tinyusb), Path(tinyuf2)
    report = load('tinyusb_hil_report', tinyusb / 'test/hil/helper/hil_report.py')
    ci = load('tinyuf2_hil_ci', tinyuf2 / '.claude/skills/hil-contract/scripts/hil_ci.py')
    inputs = json.loads((HERE / 'inputs.json').read_text(encoding='utf-8'))
    golden = {
        'sources': {'tinyusb': {'commit': commit(tinyusb), 'path': 'test/hil/helper/hil_report.py'},
                    'tinyuf2': {'commit': commit(tinyuf2), 'path': '.claude/skills/hil-contract/scripts/hil_ci.py'}},
        'tinyusb_render_report': {k: report.render_report(doc) for k, doc in inputs['tinyusb_docs'].items()},
        'tinyuf2_verdict': {c['name']: list(ci.verdict(c['code'], c['report'], 'B'))
                            for c in inputs['tinyuf2_verdicts']},
    }
    (HERE / 'golden.json').write_text(json.dumps(golden, indent=2, ensure_ascii=False) + '\n', encoding='utf-8')


if __name__ == '__main__':
    if len(sys.argv) != 3:
        sys.exit(__doc__)
    main(*sys.argv[1:])
