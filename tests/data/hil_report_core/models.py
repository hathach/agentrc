"""Three deliberately different report bodies over hil_report_core, modelled on the real
projects for test_hil_report_core (the projects' own adapters come later, in their repos):

- TINYUSB: test/hil/helper/hil_report.py's {rows, scope, caveat} board x test matrix, its
  classifier and column order kept here as the project keeps them; legacy reports read as is.
- TINYUF2: hil_test.py's run report {pass, caveat, results, reportPath, ...} with per-board
  steps and cleanup, a new Markdown view of its steps, and hil_ci.py's verdict() mapping. The
  runner's outcome persists natively as `exitCode` and `heldInProcess`, so verdict() is
  reproducible from the file (hil_ci.py reads the exit, and None for a holder kept in process).
- FARM: a farm check's health document in its own namespace, never validation evidence.
"""
import importlib.util
from pathlib import Path

CORE = Path(__file__).resolve().parents[3] / 'skills' / 'hil' / 'scripts' / 'hil_report_core.py'


def _load_core():
    spec = importlib.util.spec_from_file_location('agentrc_hil_report_core', CORE)
    mod = importlib.util.module_from_spec(spec)
    spec.loader.exec_module(mod)
    return mod


core = _load_core()

# --- tinyusb ----------------------------------------------------------------------------------
REPORT_CELL = {'pass': '✅', 'fail': '❌', 'skip': '⚪'}
PINNED = ['usbtest', 'cdc_msc_throughput', 'msc_file_explorer', 'msc_file_explorer_freertos']
DURATION = '\0duration'   # the trailing column, outside any test name


def cell_state(v):
    if v == 'fail' or (isinstance(v, str) and v.startswith(REPORT_CELL['fail'])):
        return 'fail'
    if v == 'skip' or (isinstance(v, str) and v.startswith(REPORT_CELL['skip'])):
        return 'skip'
    return 'pass'


def tinyusb_matrix(rows_all):
    seen = set()
    for _, cells, _ in rows_all:
        seen.update(cells)
    if not seen:
        return 'No tests were run.'

    def col_key(t):
        name = t.rsplit('/', 1)[-1]
        return (PINNED.index(name) if name in PINNED else len(PINNED), name, t)

    columns = sorted(seen, key=col_key)
    rows = [(lbl, {**cells, **({DURATION: dur} if dur else {})}) for lbl, cells, dur in rows_all]
    md = core.matrix('Board', columns + [DURATION], rows, REPORT_CELL,
                     headers=[c.rsplit('/', 1)[-1] for c in columns] + ['duration'])
    kinds = [cell_state(v) for _, cells, _ in rows_all for v in cells.values()]
    summary = (f'**{REPORT_CELL["pass"]} {kinds.count("pass")} passed · {REPORT_CELL["fail"]} {kinds.count("fail")} '
               f'failed · {REPORT_CELL["skip"]} {kinds.count("skip")} skipped · blank not run**')
    return summary + '\n\n' + md


def tinyusb_render_report(doc):
    md = tinyusb_matrix([(r.get('board', '?'), r.get('cells') or {}, r.get('duration'))
                         for r in doc.get('rows') or [] if isinstance(r, dict) and not r.get('retired')])
    if doc.get('scope'):
        md = f'_Scoped run: {doc["scope"]}. Boards/tests not listed were not run._\n\n' + md
    if doc.get('caveat'):
        md = doc['caveat'] + '\n' + md
    return md


def _tinyusb_check(body):
    if not isinstance(body.get('rows'), list) or not isinstance(body.get('scope'), str) or \
            not isinstance(body.get('caveat'), str):
        raise ValueError('rows a list, scope and caveat strings')


TINYUSB = core.BodySchema(name='tinyusb-hil-report', version=1, stem='hil_report', evidence='firmware-validation',
                          check=_tinyusb_check, empty=lambda b: not b['rows'],
                          render=lambda b: tinyusb_render_report(b) + '\n', legacy=lambda raw: raw)


def tinyusb_rows(body, requested):
    """A simplified per-board fold: any fail cell fails, a lock cell is not-run, else pass."""
    rows = []
    by = {r['board']: r.get('cells') or {} for r in body['rows'] if isinstance(r, dict) and 'board' in r}
    for b in requested:
        if b not in by:
            continue
        cells = by[b]
        if 'board-locked' in cells:
            v = 'not-run'
        elif any(cell_state(c) == 'fail' for c in cells.values()):
            v = 'fail'
        else:
            v = 'pass'
        rows.append({'board': b, 'verdict': v, 'detail': ', '.join(f'{k}={c}' for k, c in sorted(cells.items())),
                     'evidence': [f'hil_report.json#{b}']})
    return rows


def tinyusb_run(reading, requested):
    """apply_run_rules over a readable tinyusb attempt, its termination from `_hil`."""
    termination = reading.meta['termination'] if reading.meta else 'completed'
    return core.apply_run_rules(TINYUSB, requested, tinyusb_rows(reading.body, requested), termination,
                                reading.body['caveat'])


# --- tinyuf2 ----------------------------------------------------------------------------------
def tinyuf2_verdict(code, report, board):
    """hil_ci.py verdict() at tinyuf2 ce6db76, as its report adapter will carry it."""
    if report is None:
        return 'blocker', f'run ended without a report (exit {code})'
    row = next((r for r in report.get('results', []) if r.get('board') == board), None)
    if row is None:
        return 'blocker', f'report has no row for {board} (exit {code}): {report.get("caveat") or report.get("error")}'
    cleanup = row.get('cleanup') or {}
    if code in (4, None) or row.get('ran') is None or cleanup.get('heldAfter'):
        return 'blocker', f'cleanup failed or no report; board held: {row.get("detail")} {cleanup}'[:400]
    if row.get('probeUsers'):
        return 'blocker', str(row.get('detail'))
    if row.get('locked'):
        return ('blocker' if row.get('recoveryBlocker') else 'not-run'), f'locked: {row.get("detail")}'
    if not row.get('ran'):
        return 'not-run', str(row.get('detail') or report.get('error'))
    failed = next((s for s in row.get('steps', []) if not s.get('pass')), None)
    if row.get('pass') and cleanup.get('pass'):
        return 'pass', ''
    if failed is None:
        return 'blocker', f'aborted: {row.get("detail")}'
    if cleanup.get('state') == 'untouched':
        return 'not-run', f'{failed["step"]}: {failed.get("detail")} (nothing written)'
    return 'fail', f'{failed["step"]}: {failed.get("detail")}'


STEP_MARK = {True: '✅', False: '❌'}


def tinyuf2_render(body):
    lines = [f'HIL run: {"pass" if body["pass"] else "fail"}']
    if body.get('error'):
        lines.append(f'Error: {body["error"]}')
    for r in body['results']:
        steps = [[s.get('step', '?'), STEP_MARK[bool(s.get('pass'))], s.get('detail', '')]
                 for s in r.get('steps') or []]
        cleanup = r.get('cleanup') or {}
        if cleanup:
            steps.append(['cleanup', STEP_MARK[bool(cleanup.get('pass'))], cleanup.get('state', '')])
        lines += ['', f'### {r.get("board", "?")}', '',
                  core.table(['Step', 'Result', 'Detail'], steps, center={1}) if steps else '_No steps ran._']
    return '\n'.join(lines) + '\n'


def _tinyuf2_check(body):
    if not isinstance(body.get('pass'), bool) or not isinstance(body.get('results'), list) or \
            not isinstance(body.get('caveat'), str) or not isinstance(body.get('reportPath'), str):
        raise ValueError('pass a bool, results a list, caveat and reportPath strings')
    if type(body.get('exitCode', 0)) is not int or not isinstance(body.get('heldInProcess', False), bool):
        raise ValueError('exitCode an int, heldInProcess a bool')


def tinyuf2_exit(body):
    """hil_ci.verdict()'s `code` from the report alone: None for a holder kept in process, and
    for a legacy report that never recorded its exit."""
    if body.get('heldInProcess') or 'exitCode' not in body:
        return None
    return body['exitCode']


TINYUF2 = core.BodySchema(name='tinyuf2-hil-run', version=1, stem='hil_report', evidence='firmware-validation',
                          check=_tinyuf2_check, empty=lambda b: not b['results'], render=tinyuf2_render,
                          legacy=lambda raw: raw)


def tinyuf2_rows(code, report, requested):
    rows = []
    for b in requested:
        v, detail = tinyuf2_verdict(code, report, b)
        rows.append({'board': b, 'verdict': v, 'detail': detail,
                     'evidence': [report['reportPath']] if report and report.get('reportPath') else []})
    return rows


# --- farm check -------------------------------------------------------------------------------
FARM_MARKS = {'ok': '✅ ok', 'failed': '❌ failed', 'blocked': '⛔ blocked'}


def _farm_check(body):
    if not isinstance(body.get('rows'), list) or not isinstance(body.get('counts'), dict):
        raise ValueError('rows a list, counts an object')


FARM = core.BodySchema(name='farm-check', version=1, stem='farm_check', evidence='farm-health',
                       check=_farm_check, empty=lambda b: not b['rows'],
                       render=lambda b: core.matrix('Board', ['probe', 'status'],
                                                    [(r['name'], r) for r in b['rows']], FARM_MARKS) + '\n')
