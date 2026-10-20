#!/usr/bin/env python3
"""The HIL report document shared by the project harnesses (`hil` skill, Reporting and
Verdicts). Stdlib only, no registry: a project passes its BodySchema and its own rows.

A report is the project's native JSON body (its field paths unchanged) plus one reserved
`_hil` metadata object. `<stem>.json` is authoritative; `<stem>.md` is a view rendered from the
serialised JSON and regenerable from it (prepare_pair), so the pair is not crash-atomic.
Every attempt that a later one replaces is archived first, create-only, under
`<stem>.attempts/<attempt id>/`; an accumulated attempt names its predecessor in
`_hil.previous_attempt`, which read_chain follows; an archived attempt is sealed. When an
attempt cannot publish, withdraw_current archives the live report and replaces its JSON with
a withdrawal record (outcome 'withdrawn', reserved `_hil_withdrawn` field), which no
preparation renders as evidence and which the next accumulated attempt links;
recover_publish picks that or the Markdown repair after a failed publish, and read_current
resolves a withdrawal to the report it archived. adopt makes a report published elsewhere (a
rig's, fetched) the current one, importing its attempt records, or withdraws the local one.
One writer per report dir: each call that writes holds `.<stem>.lock` and refuses, never
waits, while another holds it.

`_hil.roster` is the caller's; roster_identity gives the portable {name, sha256} form, which
read_chain compares whole across attempts.

Adapters keep the native outcome fields their verdicts need (a runner's exit status, an
in-process holder) in the body; `_hil.termination` says only how the run ended.

Verdicts follow the `hil` skill: apply_run_rules for one attempt's rows, sequence_verdict
for a retry sequence, which stays apart from the current attempt's view.
"""

from __future__ import annotations

import contextlib
import errno
import hashlib
import json
import os
import re
import secrets
import shutil
import time
import unicodedata
from pathlib import Path
from typing import Callable, NamedTuple

try:
    import fcntl
except ImportError:   # Windows: a consumer's copy-back location
    fcntl = None
    import msvcrt

META = '_hil'
CHAIN_LIMIT = 256     # links followed from one report: read_chain, read_current, adopt
META_VERSION = 1
TERMINATIONS = ('completed', 'timeout', 'aborted', 'no-boards')
VERDICTS = ('blocker', 'fail', 'not-run', 'pass')   # precedence order
EVIDENCE = ('firmware-validation', 'farm-health')
READABLE = ('empty', 'valid')   # the other outcomes: missing, corrupt, unsupported, withdrawn
WITHDRAWN = '_hil_withdrawn'
NOTICE = '**HIL report unavailable'
_ID_TEXT = re.compile(r'[A-Za-z0-9][A-Za-z0-9._-]{0,127}\Z')
# derived archive references; an attempt id never starts with one of these
_RESERVED = ('legacy-', 'corrupt-', 'unsupported-', 'withdrawn-', 'unreadable')
_DIGEST_REF = re.compile(r'(legacy|legacy-text|corrupt)-[0-9a-f]{16}\Z')
_META_TYPES = {'version': int, 'body_schema': str, 'body_version': int, 'attempt_id': str,
               'previous_attempt': (str, type(None)), 'termination': str, 'invocation': list,
               'selection': list, 'host': str, 'roster': dict}
_META_OPTIONAL = {'build': dict, 'legacy_text': str}
_ROW_KEYS = {'board', 'verdict', 'detail', 'evidence'}


class ReportError(Exception):
    """A report operation refused or failed. `stage` names where; `published` the files it had
    already replaced, so a caller reports exactly what reached the dir."""

    def __init__(self, stage: str, msg: str, published=()):
        super().__init__(f'{stage}: {msg}')
        self.stage = stage
        self.published = tuple(published)


class BodySchema(NamedTuple):
    """A project's report body. `name` and `version` go into `_hil` as body_schema and
    body_version; `reads` are the older body versions it still reads. `check(body)` raises
    ValueError or TypeError on a body it cannot use; `empty(body)` says a valid body holds no
    result; `render(body)` returns the whole Markdown file; `legacy(raw)` returns the body of a
    document without `_hil`, or is None to refuse every such document. `evidence` is what the
    document may count as: a 'farm-health' body never yields validation verdicts."""
    name: str
    version: int
    stem: str
    evidence: str
    check: Callable[[dict], None]
    empty: Callable[[dict], bool]
    render: Callable[[dict], str]
    legacy: Callable[[dict], dict] | None = None
    reads: tuple = ()


class Reading(NamedTuple):
    """One document read: `outcome` missing | corrupt | unsupported | empty | valid; `body` the
    native fields (every project field kept, `_hil` removed), `meta` its `_hil` (None for a
    legacy document) and `raw` the file's bytes."""
    outcome: str
    body: dict | None
    meta: dict | None
    detail: str = ''
    raw: bytes | None = None

    @property
    def legacy(self) -> bool:
        return self.meta is None and self.outcome in READABLE


class Attempt(NamedTuple):
    """An attempt started by start_attempt. `previous_attempt` is set only when accumulating;
    `prior` is then the reading it accumulates onto. `archived` is the archive id of the
    report it replaces, `legacy_text` a prior Markdown whose JSON could not be read."""
    attempt_id: str
    previous_attempt: str | None
    prior: Reading | None
    archived: str | None
    legacy_text: str | None


class AttemptContext(NamedTuple):
    """One attempt of a chain (read_chain, oldest first). The project fills `run`
    (apply_run_rules of a readable attempt), `retry` (this attempt is an eligible retry of its
    predecessor's fails: no locked peer, within the retry count) and `final` (boards whose
    fail the contract names final)."""
    attempt_id: str
    previous_attempt: str | None
    outcome: str   # a Reading outcome, or 'mismatched'
    reading: Reading
    run: dict | None = None
    retry: bool = False
    final: frozenset = frozenset()


# --- tables ---------------------------------------------------------------------------------
def display_width(s: str) -> int:
    """Terminal columns, not characters: the ✅ ❌ ⚪ marks are one character and two
    columns, so len() pads a cell holding one a column short of the header rule."""
    return sum(2 if unicodedata.east_asian_width(c) in 'WF' else 1 for c in s)


def pad(s: str, width: int, center: bool = False) -> str:
    """str.ljust/center, measured in display columns."""
    room = max(0, width - display_width(s))
    if not center:
        return s + ' ' * room
    left = room // 2
    return ' ' * left + s + ' ' * (room - left)


def table(headers: list, rows: list, center=frozenset()) -> str:
    """An aligned Markdown table of string cells, each column as wide as its widest cell; the
    columns whose index is in `center` are centred (`:--:`), the rest left-aligned."""
    if any(len(r) != len(headers) for r in rows):
        raise ValueError(f'every row needs {len(headers)} cells')
    widths = [max([display_width(h)] + [display_width(r[i]) for r in rows])
              for i, h in enumerate(headers)]

    def line(values):
        return '| ' + ' | '.join(pad(v, w, i in center)
                                 for i, (v, w) in enumerate(zip(values, widths))) + ' |'

    sep = '| ' + ' | '.join(':' + '-' * (w - 2) + ':' if i in center else '-' * w
                            for i, w in enumerate(widths)) + ' |'
    return '\n'.join([line(headers), sep] + [line(r) for r in rows])


def matrix(label: str, columns: list, rows: list, marks: dict, headers: list | None = None) -> str:
    """Rows of (row label, {column: value}) as a table: the label column left-aligned, then one
    centred column per entry of `columns`, in that order, headed by `headers` (default the
    column keys). A value shows as marks.get(value, value), an absent one blank."""
    headers = list(columns) if headers is None else list(headers)
    if len(headers) != len(columns):
        raise ValueError('one header per column')
    body = []
    for name, cells in rows:
        if any(not isinstance(v, str) for v in cells.values()):
            raise TypeError(f'row {name}: cell values must be strings')
        body.append([name] + [marks.get(cells[c], cells[c]) if c in cells else '' for c in columns])
    return table([label] + headers, body, center=frozenset(range(1, len(columns) + 1)))


# --- reading --------------------------------------------------------------------------------
def _digest(data: bytes) -> str:
    return hashlib.sha256(data).hexdigest()[:16]


def _is_id(s) -> bool:
    """An attempt id: path-safe, at most 128 characters, outside the derived namespaces."""
    return isinstance(s, str) and bool(_ID_TEXT.match(s)) and not s.startswith(_RESERVED)


def _check_id(attempt_id) -> None:
    if not _is_id(attempt_id):
        raise ReportError('attempt', f'attempt id {attempt_id!r} must match {_ID_TEXT.pattern} '
                                     f'and start with none of {_RESERVED}')


def _is_ref(s) -> bool:
    """An archive reference: an attempt id, `withdrawn-<attempt id>`, or a digest-named record."""
    return _is_id(s) or isinstance(s, str) and (
        s.startswith('withdrawn-') and _is_id(s[len('withdrawn-'):]) or bool(_DIGEST_REF.match(s)))


def _meta_problem(meta, schema: BodySchema):
    """(outcome, detail) for `_hil` this schema cannot use, else None."""
    if not isinstance(meta, dict):
        return 'corrupt', f'{META} is not an object'
    if type(meta.get('version')) is not int or meta['version'] != META_VERSION:
        return 'unsupported', f'{META} version {meta.get("version")!r}; this core reads {META_VERSION}'
    if meta.get('body_schema') != schema.name:
        return 'unsupported', f'body schema {meta.get("body_schema")!r}, not {schema.name!r}'
    if type(meta.get('body_version')) is not int or \
            meta['body_version'] not in (schema.version, *schema.reads):
        return 'unsupported', f'{schema.name} body version {meta.get("body_version")!r} is not readable here'
    unknown = set(meta) - set(_META_TYPES) - set(_META_OPTIONAL)
    if unknown:
        return 'corrupt', f'{META} has unknown fields {sorted(unknown)}'
    for k, t in {**_META_TYPES, **{k: v for k, v in _META_OPTIONAL.items() if k in meta}}.items():
        if k not in meta or not isinstance(meta[k], t) or isinstance(meta[k], bool):
            return 'corrupt', f'{META}.{k} is missing or not {t}'
    if not _is_id(meta['attempt_id']) or \
            (meta['previous_attempt'] is not None and not _is_ref(meta['previous_attempt'])):
        return 'corrupt', f'{META}.attempt_id must be an attempt id, previous_attempt an archive reference'
    if meta['termination'] not in TERMINATIONS:
        return 'corrupt', f'{META}.termination {meta["termination"]!r} is not one of {TERMINATIONS}'
    if not all(isinstance(x, str) for x in meta['invocation'] + meta['selection']):
        return 'corrupt', f'{META}.invocation and .selection must be lists of strings'
    return None


def parse(data: bytes, schema: BodySchema) -> Reading:
    """A document's bytes read through `schema`: an unknown `_hil` version or body schema is
    unsupported, never read as legacy; a document without `_hil` is read only by
    schema.legacy."""
    try:
        raw = json.loads(data.decode('utf-8'))
    except (UnicodeDecodeError, ValueError) as e:
        return Reading('corrupt', None, None, f'not JSON: {e}', data)
    if not isinstance(raw, dict):
        return Reading('corrupt', None, None, 'not a JSON object', data)
    if WITHDRAWN in raw:
        return _withdrawal(raw, schema, data)
    meta = raw.get(META)
    body = {k: v for k, v in raw.items() if k != META}
    if META in raw:
        problem = _meta_problem(meta, schema)
        if problem:
            return Reading(problem[0], None, None, problem[1], data)
    elif schema.legacy is None:
        return Reading('unsupported', None, None, f'no {META} metadata and {schema.name} reads no legacy report', data)
    try:
        if META not in raw:
            body = schema.legacy(body)
            if not isinstance(body, dict) or META in body:
                raise ValueError('the legacy reader returned no body')
        schema.check(body)
        empty = schema.empty(body)
    except (ValueError, TypeError, KeyError, AttributeError) as e:
        return Reading('corrupt', None, None, f'{schema.name} body: {e}', data)
    return Reading('empty' if empty else 'valid', body, meta, '', data)


def _withdrawal(raw: dict, schema: BodySchema, data: bytes) -> Reading:
    """A withdrawal record: outcome 'withdrawn', its fields as the body, never evidence."""
    w = raw[WITHDRAWN]
    if set(raw) != {WITHDRAWN} or not isinstance(w, dict) or \
            set(w) != {'version', 'body_schema', 'attempt_id', 'archived', 'reason'}:
        return Reading('corrupt', None, None, f'malformed {WITHDRAWN} record', data)
    if w['version'] != META_VERSION or w['body_schema'] != schema.name:
        return Reading('unsupported', None, None, f'{WITHDRAWN} record of {w["body_schema"]!r} '
                                                  f'version {w["version"]!r}', data)
    if not (_is_id(w['attempt_id']) and isinstance(w['reason'], str)
            and (w['archived'] is None or _is_ref(w['archived']))):
        return Reading('corrupt', None, None, f'malformed {WITHDRAWN} record', data)
    prior = f'the previous report, attempt {w["archived"]}, is archived and' if w['archived'] else 'no previous report'
    return Reading('withdrawn', w, None, f'attempt {w["attempt_id"]} could not publish its report '
                                         f'({w["reason"]}); {prior} is not current evidence', data)


def read_document(path, schema: BodySchema) -> Reading:
    """The report at `path`."""
    path = Path(path)
    if not path.exists():
        return Reading('missing', None, None, f'{path} not found')
    try:
        data = path.read_bytes()
    except OSError as e:
        return Reading('corrupt', None, None, f'cannot read {path}: {e}')
    return parse(data, schema)


def paths(report_dir, schema: BodySchema) -> tuple:
    """(json, md, attempts root) of `schema` in `report_dir`."""
    d = Path(report_dir)
    return d / f'{schema.stem}.json', d / f'{schema.stem}.md', d / f'{schema.stem}.attempts'


# --- metadata and serialisation -------------------------------------------------------------
def new_attempt_id() -> str:
    return f'{time.strftime("%Y%m%dT%H%M%S")}-{os.getpid()}-{secrets.token_hex(4)}'


def make_meta(schema: BodySchema, attempt: Attempt, *, termination: str, invocation: list,
              selection: list, host: str, roster: dict, build: dict | None = None,
              legacy_text: str | None = None) -> dict:
    """`_hil` for a document of `attempt`. `build` is provenance the caller has, omitted when
    None, never filled in; `legacy_text` is attempt.legacy_text when the caller publishes it."""
    meta = {'version': META_VERSION, 'body_schema': schema.name, 'body_version': schema.version,
            'attempt_id': attempt.attempt_id, 'previous_attempt': attempt.previous_attempt,
            'termination': termination, 'invocation': list(invocation), 'selection': list(selection),
            'host': host, 'roster': dict(roster)}
    if build is not None:
        meta['build'] = dict(build)
    if legacy_text is not None:
        meta['legacy_text'] = legacy_text
    problem = _meta_problem(meta, schema)
    if problem:
        raise ValueError(problem[1])
    return meta


def serialise(schema: BodySchema, body: dict, meta: dict) -> str:
    """The document's JSON text, validated: what publish writes and what a stdout-only caller
    prints. Raises ReportError('validate' | 'serialise')."""
    try:
        if not isinstance(body, dict) or META in body:
            raise ValueError(f'a body is an object without the reserved {META} field')
        problem = _meta_problem(meta, schema)
        if problem:
            raise ValueError(problem[1])
        if meta['body_version'] != schema.version:
            raise ValueError(f'{schema.name} writes body version {schema.version} only')
        schema.check(body)
    except (ValueError, TypeError, KeyError, AttributeError) as e:
        raise ReportError('validate', str(e)) from e
    try:
        return json.dumps({**body, META: meta}, indent=2, allow_nan=False) + '\n'
    except (ValueError, TypeError) as e:
        raise ReportError('serialise', str(e)) from e


def _legacy_section(text: str) -> str:
    return ('\n_Legacy text: a previous attempt\'s rendered output whose JSON could not be read. '
            'Its rows are not in this report\'s JSON, no verdict is read from them and no later '
            'run merges them._\n\n' + text)


def render_markdown(schema: BodySchema, doc: dict) -> str:
    """The Markdown view of a parsed document: schema.render of its body, then any labelled
    legacy text its `_hil` carries."""
    meta = doc.get(META) or {}
    md = schema.render({k: v for k, v in doc.items() if k != META})
    if not isinstance(md, str):
        raise TypeError(f'{schema.name} render returned {type(md).__name__}, not str')
    if meta.get('legacy_text'):
        md += _legacy_section(meta['legacy_text'])
    return md


def _markdown_bytes(schema: BodySchema, doc: dict, where) -> bytes:
    """render_markdown encoded, or ReportError('render'): a project renderer's any failure,
    or a lone surrogate a JSON string may carry, leaves the dir as it was."""
    try:
        return render_markdown(schema, doc).encode('utf-8')
    except Exception as e:  # noqa: BLE001
        raise ReportError('render', f'{where}: {type(e).__name__}: {e}') from e


def _notice(reading: Reading, name: str) -> str:
    return f'{NOTICE}: {name} is {reading.outcome}.** {reading.detail}\n'


# --- files ----------------------------------------------------------------------------------
@contextlib.contextmanager
def _writer(report_dir: Path, schema: BodySchema):
    lock = report_dir / f'.{schema.stem}.lock'
    try:
        report_dir.mkdir(parents=True, exist_ok=True)
        fd = os.open(lock, os.O_RDWR | os.O_CREAT, 0o666)
    except OSError as e:
        raise ReportError('writer', f'cannot open {lock}: {e}') from e
    try:
        try:
            if fcntl:
                fcntl.flock(fd, fcntl.LOCK_EX | fcntl.LOCK_NB)
            else:
                msvcrt.locking(fd, msvcrt.LK_NBLCK, 1)
        except OSError as e:
            raise ReportError('writer', f'another writer holds {lock}') from e
        try:
            yield
        except OSError as e:
            raise ReportError('io', str(e)) from e
        finally:
            if not fcntl:
                os.lseek(fd, 0, os.SEEK_SET)
                msvcrt.locking(fd, msvcrt.LK_UNLCK, 1)
    finally:
        os.close(fd)


def _stage(path: Path, data: bytes) -> Path:
    """`data` in a fresh temp file beside `path`, synced; the umask applies, as to the file it replaces."""
    tmp = path.with_name(f'.{path.name}.{secrets.token_hex(6)}.tmp')
    fd = os.open(tmp, os.O_WRONLY | os.O_CREAT | os.O_EXCL, 0o666)
    try:
        with os.fdopen(fd, 'wb') as f:
            f.write(data)
            f.flush()
            os.fsync(f.fileno())
    except BaseException:
        with contextlib.suppress(OSError):
            os.unlink(tmp)
        raise
    return tmp


def _replace_one(path: Path, data: bytes) -> None:
    tmp = _stage(path, data)
    try:
        os.replace(tmp, path)
    except BaseException:
        with contextlib.suppress(OSError):
            os.unlink(tmp)
        raise


def _read_bytes(path: Path) -> bytes | None:
    try:
        return path.read_bytes()
    except FileNotFoundError:
        return None


def _doc(reading: Reading) -> dict:
    """A readable reading as the document it was parsed from."""
    return {**reading.body, **({META: reading.meta} if reading.meta else {})}


def _sealed(root: Path, aid: str) -> bool:
    """`aid` is archived or withdrawn: it never writes again."""
    return (root / aid).exists() or (root / f'withdrawn-{aid}').exists()


def _archive_id(reading: Reading) -> str:
    if reading.meta is not None:
        return reading.meta['attempt_id']
    if reading.outcome == 'withdrawn':
        return f'withdrawn-{reading.body["attempt_id"]}'
    kind = 'legacy' if reading.outcome in READABLE else reading.outcome
    return f'{kind}-{_digest(reading.raw)}'


def _archive(root: Path, aid: str, files: dict) -> None:
    """Create-only: an existing record of `aid` must hold the same authoritative file (the
    JSON, else the only file); it is never rewritten."""
    final = root / aid
    key = next(iter(files))
    if final.exists():
        if _read_bytes(final / key) == files[key]:
            return
        raise ReportError('archive', f'{final} already holds a different record; an attempt record is never rewritten')
    root.mkdir(parents=True, exist_ok=True)
    tmp = root / f'.{aid}.{secrets.token_hex(6)}.tmp'
    os.mkdir(tmp)
    try:
        for name, data in files.items():
            (tmp / name).write_bytes(data)
        os.rename(tmp, final)
    except BaseException:
        shutil.rmtree(tmp, ignore_errors=True)
        if final.exists() and _read_bytes(final / key) == files[key]:
            return
        raise


def _used(root: Path, current: Reading, attempt_id: str) -> bool:
    """`attempt_id` has a record, archived or live, or was withdrawn: it never writes again."""
    live = current.meta['attempt_id'] if current.meta else \
        current.body['attempt_id'] if current.outcome == 'withdrawn' else None
    return attempt_id == live or _sealed(root, attempt_id)


def _archive_current(report_dir: Path, schema: BodySchema, attempt_id: str | None = None):
    """Archive the published report, which stays in place; (archive id, reading, legacy text)
    or (None, reading, None) when there is none. An unsupported report, or `attempt_id`
    already used, is refused before anything is archived."""
    jpath, mpath, root = paths(report_dir, schema)
    data, md = _read_bytes(jpath), _read_bytes(mpath)
    reading = Reading('missing', None, None, f'{jpath} not found') if data is None else parse(data, schema)
    if reading.outcome == 'unsupported':
        raise ReportError('predecessor', f'{jpath}: {reading.detail}; refusing to replace or accumulate onto it')
    aid = None if data is None and md is None else \
        f'legacy-text-{_digest(md)}' if data is None else _archive_id(reading)
    if attempt_id is not None and _used(root, reading, attempt_id):
        raise ReportError('attempt', f'attempt id {attempt_id} is already used')
    if aid is None:
        return None, reading, None
    if data is None:
        _archive(root, aid, {mpath.name: md})
        return aid, reading, md.decode('utf-8', errors='replace')
    if _read_bytes(root / aid / jpath.name) == data:   # sealed already: no render needed
        return aid, reading, None
    if reading.outcome in READABLE:
        files = {jpath.name: data, mpath.name: _markdown_bytes(schema, _doc(reading), f'archiving {jpath}')}
        legacy = None
    else:
        files = {jpath.name: data, **({mpath.name: md} if md is not None else {})}
        legacy = md.decode('utf-8', errors='replace') if md is not None and reading.outcome != 'withdrawn' else None
    _archive(root, aid, files)
    return aid, reading, legacy


def archive_current(report_dir, schema: BodySchema) -> str | None:
    """Archive the current pair as start_attempt does, and seal it: its archive id, or None
    when there is no current report. The pair stays in place."""
    with _writer(Path(report_dir), schema):
        return _archive_current(Path(report_dir), schema)[0]


def withdraw_current(report_dir, schema: BodySchema, attempt_id: str, reason: str) -> str | None:
    """For an attempt whose publication failed: archive the current report (sealing it; an
    existing identical archive is reused without rendering), verify the archived JSON, then
    atomically replace the JSON with a withdrawal record naming `attempt_id`, and the
    Markdown with the notice prepare_pair writes for it. Returns the archived id. The record
    is written first, so an interruption still leaves it as the current report; a repeat call
    for the same attempt keeps that record and rewrites only the notice. An attempt archived
    or withdrawn earlier is refused ('sealed')."""
    report_dir = Path(report_dir)
    _check_id(attempt_id)
    with _writer(report_dir, schema):
        return _withdraw(report_dir, schema, attempt_id, reason)


def _withdraw(report_dir: Path, schema: BodySchema, attempt_id: str, reason: str) -> str | None:
    """withdraw_current under the writer lock its caller holds."""
    jpath, mpath, root = paths(report_dir, schema)
    record = {WITHDRAWN: {'version': META_VERSION, 'body_schema': schema.name, 'attempt_id': attempt_id,
                          'archived': None, 'reason': ' '.join(reason.split())}}
    data = _read_bytes(jpath)
    current = parse(data, schema) if data is not None else None
    if current is not None and current.outcome == 'withdrawn' and current.body['attempt_id'] == attempt_id:
        aid = current.body['archived']   # a retry: the record stands
    else:
        if _sealed(root, attempt_id):
            raise ReportError('sealed', f'attempt {attempt_id} is archived or withdrawn already')
        aid, reading, _ = _archive_current(report_dir, schema)
        if reading.raw is not None and _read_bytes(root / aid / jpath.name) != reading.raw:
            raise ReportError('archive', f'{root / aid} does not hold {jpath}; it stays current')
        record[WITHDRAWN]['archived'] = aid
        data = (json.dumps(record, indent=2) + '\n').encode('utf-8', errors='backslashreplace')
        _replace_one(jpath, data)
    _replace_one(mpath, _notice(parse(data, schema), jpath.name).encode('utf-8', errors='backslashreplace'))
    return aid


class Recovery(NamedTuple):
    """recover_publish's result: `outcome` repaired (the Markdown regenerated from the attempt's
    published JSON), withdrawn, removed (the live files byte-identical to the report the attempt
    archived, named in `removed`) or unresolved; `problems` what failed on the way."""
    outcome: str
    problems: list
    removed: list


def recover_publish(report_dir, schema: BodySchema, attempt: Attempt | None, err: BaseException) -> Recovery:
    """After `attempt`'s publish failed with `err`, leave no report in report_dir that reads as
    this attempt's, all under one writer lock. A failure at the 'markdown' stage left this
    attempt's JSON in place: its Markdown is regenerated (prepare_pair). Otherwise the report
    is withdrawn (withdraw_current, `err` the reason). When that fails too, each live file
    byte-identical to its copy in the record of the report the attempt archived is removed:
    that is the predecessor, never this run's. A writer lock held by another writer raises
    ReportError('writer') before anything changes; nothing here mutates the dir unlocked."""
    report_dir = Path(report_dir)
    jpath, mpath, root = paths(report_dir, schema)
    problems = []
    with _writer(report_dir, schema):
        if isinstance(err, ReportError) and err.stage == 'markdown':
            try:
                _prepare(report_dir, schema)
                return Recovery('repaired', [], [])
            except (ReportError, OSError) as e:
                problems.append(f'{mpath.name} could not be regenerated from {jpath.name} ({e})')
        elif attempt is None:
            problems.append('no attempt was begun here, so none can be withdrawn')
        else:
            try:
                _withdraw(report_dir, schema, attempt.attempt_id, f'{type(err).__name__}: {err}')
                return Recovery('withdrawn', [], [])
            except (ReportError, OSError) as e:
                problems.append(f'the report could not be withdrawn ({e})')
        removed = []
        if attempt is not None and attempt.archived is not None:
            for live in (jpath, mpath):
                archived = _read_bytes(root / attempt.archived / live.name)
                with contextlib.suppress(OSError):
                    if archived is not None and _read_bytes(live) == archived:
                        live.unlink()
                        removed.append(live.name)
        return Recovery('removed' if removed else 'unresolved', problems, removed)


def start_attempt(report_dir, schema: BodySchema, accumulate: bool, attempt_id: str | None = None) -> Attempt:
    """Begin an attempt: archive the report it replaces (fresh or accumulated alike), refuse an
    unsupported one or a reused attempt id. Only an accumulating attempt links its predecessor
    and gets the prior reading, a withdrawal resolved to the report it archived (read_current);
    a corrupt prior is linked, so its sequence never passes."""
    report_dir = Path(report_dir)
    attempt_id = attempt_id or new_attempt_id()
    _check_id(attempt_id)
    with _writer(report_dir, schema):
        aid, reading, legacy = _archive_current(report_dir, schema, attempt_id)
        if accumulate:
            reading = _resolved(reading, paths(report_dir, schema)[2], schema)
    if not accumulate:
        return Attempt(attempt_id, None, None, aid, None)
    return Attempt(attempt_id, aid, reading, aid, legacy)


def read_current(report_dir, schema: BodySchema) -> Reading:
    """The report an accumulating writer builds on: the current one, or for a withdrawal the
    report it archived, followed through further withdrawals, each looked up by its archive id
    under report_dir's own attempts root. A withdrawal that archived no report is returned
    itself: there is nothing to build on. A missing archive reads as missing; a cycle, or more
    than CHAIN_LIMIT withdrawals, as corrupt."""
    jpath, _, root = paths(report_dir, schema)
    return _resolved(read_document(jpath, schema), root, schema)


def _resolved(reading: Reading, root: Path, schema: BodySchema) -> Reading:
    seen = set()
    while reading.outcome == 'withdrawn' and reading.body['archived'] is not None:
        aid = reading.body['archived']
        if aid in seen or len(seen) >= CHAIN_LIMIT:
            return Reading('corrupt', None, None, f'withdrawals cycle through {aid} or exceed {CHAIN_LIMIT}')
        seen.add(aid)
        reading = read_document(root / aid / f'{schema.stem}.json', schema)
    return reading


def roster_identity(path) -> dict:
    """A roster as `_hil.roster` names it: its file name and SHA-256, the same wherever the file
    is (a checkout, a rig's tree), since read_chain compares the whole object across attempts."""
    path = Path(path)
    return {'name': path.name, 'sha256': hashlib.sha256(path.read_bytes()).hexdigest()}


def _guard_predecessor(report_dir: Path, schema: BodySchema, meta: dict) -> None:
    """Refuse a write by a sealed (archived) attempt, by an attempt other than the current
    report's own unless that report is archived, or naming a predecessor that is neither the
    archived current report nor an archived record."""
    jpath, mpath, root = paths(report_dir, schema)
    aid, previous = meta['attempt_id'], meta['previous_attempt']
    if _sealed(root, aid):
        raise ReportError('sealed', f'attempt {aid} is archived or withdrawn; it never writes again')
    data = _read_bytes(jpath)
    if data is None:
        md = _read_bytes(mpath)
        if md is not None and _read_bytes(root / f'legacy-text-{_digest(md)}' / mpath.name) != md:
            raise ReportError('predecessor', f'{mpath} is not archived: begin the attempt with start_attempt')
        if previous is not None and not (root / previous).is_dir():
            raise ReportError('predecessor', f'predecessor {previous} has no archived record')
        return
    reading = parse(data, schema)
    if reading.outcome == 'withdrawn' and reading.body['attempt_id'] == aid:
        raise ReportError('sealed', f'attempt {aid} was withdrawn; it never publishes again')
    if reading.meta is not None and reading.meta['attempt_id'] == aid:
        if reading.meta['previous_attempt'] != previous:
            raise ReportError('predecessor', f'attempt {aid} already names predecessor '
                                             f'{reading.meta["previous_attempt"]}, not {previous}')
        return   # a repeated write inside one attempt
    if reading.outcome == 'unsupported' or _read_bytes(root / _archive_id(reading) / jpath.name) != data:
        raise ReportError('predecessor', f'{jpath} belongs to another attempt and is not archived: '
                                         'begin the attempt with start_attempt')
    if previous is not None and previous != _archive_id(reading):
        raise ReportError('predecessor', f'{jpath} is attempt {_archive_id(reading)}, not the predecessor {previous}')


def publish(report_dir, schema: BodySchema, body: dict, meta: dict) -> tuple:
    """Validate and serialise, render the Markdown from the serialised snapshot, stage both
    beside their targets, then replace the JSON and then the Markdown, each atomically.
    Returns (json path, md path). Raises ReportError: before 'replace' nothing in the dir
    changed; at 'markdown' the JSON is the new one and the Markdown is removed or left stale
    (`published`), for prepare_pair to regenerate."""
    report_dir = Path(report_dir)
    text = serialise(schema, body, meta)
    md = _markdown_bytes(schema, json.loads(text), 'publishing')
    jpath, mpath, _ = paths(report_dir, schema)
    with _writer(report_dir, schema):
        _guard_predecessor(report_dir, schema, meta)
        staged = []
        try:
            try:
                staged.append(_stage(jpath, text.encode('utf-8')))
                staged.append(_stage(mpath, md))
                os.replace(staged[0], jpath)
            except OSError as e:
                raise ReportError('replace', f'{jpath}: {e}') from e
            try:
                os.replace(staged[1], mpath)
            except OSError as e:
                with contextlib.suppress(OSError):
                    os.unlink(mpath)
                state = 'left STALE' if mpath.exists() else 'absent'
                raise ReportError('markdown', f'{mpath} not replaced ({e}); the previous one is {state}',
                                  published=(str(jpath),)) from e
        finally:
            for tmp in staged:
                with contextlib.suppress(OSError):
                    os.unlink(tmp)
    return jpath, mpath


def prepare_pair(report_dir, schema: BodySchema) -> Reading:
    """Make `<stem>.md` match `<stem>.json` before a consumer (copy-back, CI upload) exposes
    the pair: regenerated from a readable JSON, else an explicit unavailable notice with any
    earlier Markdown kept below it as labelled legacy text. Never leaves stale Markdown."""
    report_dir = Path(report_dir)
    with _writer(report_dir, schema):
        return _prepare(report_dir, schema)


def _prepare(report_dir: Path, schema: BodySchema) -> Reading:
    """prepare_pair under the writer lock its caller holds."""
    jpath, mpath, _ = paths(report_dir, schema)
    reading = read_document(jpath, schema)
    md = None
    if reading.outcome in READABLE:
        try:
            md = _markdown_bytes(schema, _doc(reading), 'render failed')
        except ReportError as e:   # shown as the notice instead
            reading = reading._replace(outcome='corrupt', detail=str(e))
    if md is None:
        md = _notice(reading, jpath.name)
        old = _read_bytes(mpath)
        text = old.decode('utf-8', errors='replace') if old is not None else ''
        if text.startswith(NOTICE):   # an earlier notice: its legacy text is labelled already
            md += text.split('\n', 1)[1] if '\n' in text else ''
        elif text.strip():
            md += _legacy_section(text)
        md = md.encode('utf-8', errors='backslashreplace')
    if _read_bytes(mpath) != md:
        _replace_one(mpath, md)
    return reading


def _entry(path: Path):
    data = _read_bytes(path) if path.is_file() else None
    return None if data is None else {'size': len(data), 'sha256': hashlib.sha256(data).hexdigest()}


def inventory(report_dir, schema: BodySchema) -> dict:
    """The current pair and every archived attempt's files, with sizes and SHA-256."""
    jpath, mpath, root = paths(report_dir, schema)
    attempts = []
    if root.is_dir():
        for d in sorted(root.iterdir()):
            if d.is_dir() and _is_ref(d.name):
                attempts.append({'attempt_id': d.name,
                                 'files': {f.name: _entry(f) for f in sorted(d.iterdir()) if f.is_file()}})
    return {'current': {jpath.name: _entry(jpath), mpath.name: _entry(mpath)}, 'attempts': attempts}


def read_chain(report_dir, schema: BodySchema, limit: int = CHAIN_LIMIT) -> list:
    """The current report and the predecessors its `previous_attempt` links name, oldest first,
    as AttemptContexts. A link whose record is missing, unreadable, of another schema, roster
    or host, or not the attempt it claims is kept with that outcome and ends the walk."""
    jpath, _, root = paths(report_dir, schema)
    current = read_document(jpath, schema)
    if current.outcome == 'missing':
        return []
    chain = [AttemptContext(_archive_id(current) if current.raw is not None else 'unreadable',
                            (current.meta or {}).get('previous_attempt'), current.outcome, current)]
    seen = {chain[0].attempt_id}
    while chain[-1].previous_attempt and chain[-1].outcome in READABLE:
        aid, newer = chain[-1].previous_attempt, chain[-1].reading
        reading = read_document(root / aid / jpath.name, schema)
        outcome = reading.outcome
        if aid in seen or len(chain) >= limit:
            outcome, reading = 'mismatched', reading._replace(detail=f'{aid} repeats in the chain or exceeds {limit}')
        elif outcome in READABLE:
            claimed = reading.meta['attempt_id'] if reading.meta else f'legacy-{_digest(reading.raw)}'
            same = all((reading.meta or {}).get(k) == newer.meta.get(k) for k in ('host', 'roster')) \
                if reading.meta and newer.meta else True
            if claimed != aid or not same:
                outcome = 'mismatched'
                reading = reading._replace(detail=f'record {aid} is {claimed}, or of another host or roster')
        seen.add(aid)
        chain.append(AttemptContext(aid, (reading.meta or {}).get('previous_attempt') if outcome in READABLE else None,
                                    outcome, reading))
    return chain[::-1]


# --- adoption -------------------------------------------------------------------------------
class Adoption(NamedTuple):
    """adopt's result. `copied`: the report dir's files the incoming pair replaced; `imported`:
    the attempt records moved in; `problems`: why the incoming report is not, or not wholly,
    the dir's current one (empty when it is); `conflicts`: ids the dir holds with a different
    record, the incoming version left in incoming_dir; `reading`: the incoming report as
    prepared, None when it was not."""
    copied: list
    imported: list
    problems: list
    conflicts: list
    reading: Reading | None


def _links(reading: Reading) -> list:
    """The archive ids a report names: its predecessor, or the report a withdrawal archived."""
    if reading.meta:
        return [reading.meta['previous_attempt']]
    return [reading.body['archived']] if reading.outcome == 'withdrawn' else []


def _reachable(refs: list, roots: tuple, schema: BodySchema) -> set:
    """Every archive id reachable from `refs` through the links of each version of its record
    under `roots` (attempts roots, looked up by id only), at most CHAIN_LIMIT of them."""
    seen, todo = set(), list(refs)
    while todo:
        aid = todo.pop()
        if not _is_ref(aid) or aid in seen or len(seen) >= CHAIN_LIMIT:
            continue
        seen.add(aid)
        for root in roots:
            todo += _links(read_document(root / aid / f'{schema.stem}.json', schema))
    return seen


def _authoritative(files: dict, key: str):
    """An attempt record's authoritative file entry, as _archive compares it: the JSON, else its only file."""
    return files.get(key) or next(iter(files.values()), None)


def _import(root: Path, incoming: Path, held: dict, records: list, key: str, problems: list) -> tuple:
    """Move the incoming `records` (inventory entries) `held` lacks into `root`, create-only:
    (imported, conflicts), conflicts the ids held, or created meanwhile, with a different
    authoritative file, whose incoming version stays where it is."""
    if root.is_symlink() or root.exists() and not root.is_dir():
        problems.append(f'{root} is not a directory of attempt records: none imported')
        return [], [r['attempt_id'] for r in records]
    imported, conflicts = [], []
    for r in records:
        aid, files, src = r['attempt_id'], r['files'], incoming / r['attempt_id']
        if aid in held:
            if _authoritative(held[aid], key) != _authoritative(files, key):
                conflicts.append(aid)
            continue
        if src.is_symlink():
            problems.append(f'incoming attempt record {aid} is a link: not imported')
            continue
        root.mkdir(parents=True, exist_ok=True)
        try:
            if (root / aid).exists():
                raise FileExistsError(errno.EEXIST, 'created meanwhile', str(root / aid))
            os.rename(src, root / aid)
            imported.append(aid)
        except OSError as e:
            if not (root / aid).is_dir() or (root / aid).is_symlink():
                problems.append(f'attempt record {aid} could not be imported ({e})')
                continue
            now = {f.name: _entry(f) for f in sorted((root / aid).iterdir()) if f.is_file()}
            if _authoritative(now, key) != _authoritative(files, key):
                conflicts.append(aid)
    return imported, conflicts


def adopt(report_dir, incoming_dir, schema: BodySchema, *, unusable: str | None = None,
          reason: str = 'report adoption failed') -> Adoption:
    """Make the report another writer published in `incoming_dir` (a rig's copy, fetched; its
    `<stem>.json`, `<stem>.md` and `<stem>.attempts/`) report_dir's current report, or show
    that it is not. `unusable` is the caller's reason the incoming pair cannot be promoted
    (it did not all arrive): its records are still imported and the local report withdrawn.

    The incoming pair is prepared first (prepare_pair, in incoming_dir). Then, under one
    report_dir writer lock: the current report is archived (refused: it stays, unreplaced and
    unwithdrawn, a problem); the incoming attempt records the dir lacks move in create-only,
    one held differently, or created meanwhile, staying a conflict; the incoming report is
    judged; and it is promoted, or the current report is withdrawn with `reason` and why.

    Not promoted: an unsupported or missing incoming report; one whose attempt id the dir
    holds already, current, archived or as `withdrawn-<id>` (the run published none); one
    whose chain, followed through predecessors and withdrawals' archived reports over both
    attempts roots, reaches a conflict. Its predecessor need not be the dir's current report.
    Promoted, the JSON is replaced, then the Markdown; a Markdown that cannot be promoted is
    regenerated from the promoted JSON, else removed. A promoted report that is not readable
    evidence (a withdrawal, a corrupt one), or whose chain names a record neither side holds,
    is named in `problems`. Conflicting records are always listed; they block only a chain
    that reaches them."""
    report_dir, incoming_dir = Path(report_dir), Path(incoming_dir)
    jpath, mpath, root = paths(report_dir, schema)
    ijson, imd, iroot = paths(incoming_dir, schema)
    why, incoming, incoming_id = unusable, None, None
    if why is None:
        try:
            incoming = prepare_pair(incoming_dir, schema)
        except ReportError as e:
            why = f'the incoming report could not be prepared: {e}'
    if incoming is not None:
        incoming_id = incoming.meta['attempt_id'] if incoming.meta else \
            incoming.body['attempt_id'] if incoming.outcome == 'withdrawn' else None
    problems, copied = [], []
    with _writer(report_dir, schema):
        try:
            local_id, refused = _archive_current(report_dir, schema)[0], None
        except ReportError as e:
            local_id, refused = None, f'the current {jpath.name} cannot be archived ({e}), so it is not replaced'
        held = {a['attempt_id']: a['files'] for a in inventory(report_dir, schema)['attempts']}
        imported, conflicts = _import(root, iroot, held, inventory(incoming_dir, schema)['attempts'],
                                      jpath.name, problems)
        named = _reachable(_links(incoming), (root, iroot), schema) if incoming is not None else set()
        if incoming is None:
            pass
        elif incoming.outcome in ('missing', 'unsupported'):
            why = f'the incoming report is {incoming.outcome}: {incoming.detail}'
        elif incoming_id is not None and (incoming_id == local_id or incoming_id in held
                                          or f'withdrawn-{incoming_id}' in held):
            why = f'the incoming report is attempt {incoming_id}, which {report_dir} holds already: it is no new run\'s'
            incoming_id = None
        elif named & set(conflicts):
            why = (f'the incoming report\'s chain names attempt record(s) {", ".join(sorted(named & set(conflicts)))}, '
                   f'which {report_dir} holds differently')
        problems += filter(None, (refused, why))

        def withdraw(cause):
            aid = incoming_id if incoming_id and not _sealed(root, incoming_id) else new_attempt_id()
            try:
                _withdraw(report_dir, schema, aid, f'{reason}: {cause}')
            except (ReportError, OSError) as e:
                problems.append(f'the current report could not be withdrawn ({e}); it is not the incoming run\'s')
        if refused:
            pass
        elif why:
            withdraw(why)
        else:
            try:
                os.replace(ijson, jpath)
            except OSError as e:
                problems.append(f'the incoming {jpath.name} could not be promoted ({e})')
                withdraw(problems[-1])
            else:
                copied.append(jpath.name)
                try:
                    os.replace(imd, mpath)
                except OSError as e:
                    problems.append(f'{mpath.name} could not be promoted ({e})')
                    try:
                        _prepare(report_dir, schema)
                    except (ReportError, OSError) as e2:
                        with contextlib.suppress(OSError):
                            mpath.unlink(missing_ok=True)
                        problems.append(f'{mpath.name} could not be rendered from the promoted JSON ({e2}); removed')
                if mpath.is_file():
                    copied.append(mpath.name)
                if incoming.outcome not in READABLE:
                    problems.append(f'{mpath.name} is an unavailable notice: {jpath.name} is {incoming.outcome}'
                                    f'{" -- " + incoming.detail if incoming.detail else ""}')
                gaps = [c.attempt_id for c in read_chain(report_dir, schema) if c.outcome == 'missing']
                if gaps:
                    problems.append(f'the report\'s chain names attempt record(s) {", ".join(gaps)}, which neither '
                                    f'side holds')
    return Adoption(copied, imported, problems, conflicts, incoming)


# --- verdicts -------------------------------------------------------------------------------
def _fold(rows: list) -> str:
    if not rows:
        return 'not-run'
    return min((r['verdict'] for r in rows), key=VERDICTS.index)


def _validation(schema: BodySchema) -> None:
    if schema.evidence != 'firmware-validation':
        raise ValueError(f'{schema.name} is {schema.evidence} evidence, never a validation verdict')


def apply_run_rules(schema: BodySchema, requested: list, rows: list, termination: str, caveat: str) -> dict:
    """One attempt's verdict from the project's rows, one {board, verdict, detail, evidence}
    per requested board (`hil` skill, Verdicts): a missing row is a blocker; an abort or
    timeout makes every board a blocker; no selection leaves every non-blocker board
    not-run, a board without a row too; any other run-level caveat denies a pass, as an inconclusive run (blocker).
    No requested board is not-run, never a pass."""
    _validation(schema)
    if termination not in TERMINATIONS:
        raise ValueError(f'termination {termination!r} is not one of {TERMINATIONS}')
    if not isinstance(caveat, str):
        raise ValueError('caveat must be a string')
    if len(set(requested)) != len(requested) or not all(isinstance(b, str) and b for b in requested):
        raise ValueError(f'requested boards must be distinct names: {requested}')
    got = {}
    for r in rows:
        if not isinstance(r, dict) or set(r) != _ROW_KEYS:
            raise ValueError(f'a row has exactly {sorted(_ROW_KEYS)}: {r!r}')
        if r['verdict'] not in VERDICTS or not isinstance(r['detail'], str) or \
                not (isinstance(r['evidence'], list) and all(isinstance(e, str) for e in r['evidence'])):
            raise ValueError(f'row {r["board"]!r}: verdict one of {VERDICTS}, detail a string, evidence a list of strings')
        if r['board'] not in requested or r['board'] in got:
            raise ValueError(f'row {r["board"]!r} is not one requested board\'s only row')
        got[r['board']] = dict(r, evidence=list(r['evidence']))
    out = []
    for b in requested:
        r = got.get(b) or {'board': b, 'verdict': 'not-run' if termination == 'no-boards' else 'blocker',
                           'detail': 'no report row for this board', 'evidence': []}
        if termination in ('aborted', 'timeout'):
            r = dict(r, verdict='blocker', detail=f'run {termination}: {r["detail"]}')
        elif termination == 'no-boards' and r['verdict'] != 'blocker':
            r = dict(r, verdict='not-run', detail=f'the run selected no boards: {r["detail"]}')
        elif caveat and r['verdict'] == 'pass':
            r = dict(r, verdict='blocker', detail=f'run caveat denies a pass: {caveat.strip()}')
        out.append(r)
    return {'verdict': _fold(out), 'termination': termination, 'caveat': caveat, 'rows': out}


def _retryable(c: AttemptContext, board: str) -> bool:
    """`board`'s fail in `c` may be retried (`hil` skill, Caller rules): a completed run with
    no blocker, an empty caveat, and a result the contract does not name final."""
    run = c.run
    return (run is not None and run['termination'] == 'completed' and not run['caveat']
            and not any(r['verdict'] == 'blocker' for r in run['rows']) and board not in c.final
            and any(r['board'] == board and r['verdict'] == 'fail' for r in run['rows']))


def _eligible(before: AttemptContext, after: AttemptContext, board: str) -> bool:
    """`after` is an eligible retry of `before`'s fail on `board`."""
    return after.retry and after.previous_attempt == before.attempt_id and _retryable(before, board)


def sequence_verdict(schema: BodySchema, requested: list, chain: list) -> dict:
    """The retry-sequence verdict over `chain` (read_chain, oldest first, each readable
    attempt's `run` filled), apart from `current`, the last attempt's own run narrowed to the
    requested boards (its verdict folded over them, not-run when none has a row); eligibility
    reads every attempt's whole run. An earlier blocker stands; only an eligible
    fail-then-pass is a pass, marked flaky, citing both attempts, and only when it is the
    board's one fail so far; a fail that was not retryable, or a second fail, stays unresolved
    for the rest of the sequence. A missing, corrupt, unsupported or mismatched attempt in the
    chain denies every pass; a withdrawn one ended without a report, so every requested board
    is a blocker."""
    _validation(schema)
    broken = [{'attempt_id': c.attempt_id, 'outcome': c.outcome, 'detail': c.reading.detail}
              for c in chain if c.outcome not in READABLE or c.run is None]
    rows = []
    for b in requested:
        verdict, flaky, retried, detail, evidence, seen = None, False, False, 'not run in any attempt', [], []
        sticky = False   # a fail no later pass may clear
        before = None
        for c in chain:
            mine = next((r for r in (c.run or {}).get('rows', []) if r['board'] == b), None)
            if mine is not None:
                seen.append(c.attempt_id)
                v, was = mine['verdict'], verdict
                # this attempt retries the board's fail: counted whatever it found, so once only
                is_retry = verdict == 'fail' and before is not None and _eligible(before, c, b)
                if verdict == 'blocker' or (v == 'not-run' and verdict is not None):
                    pass
                elif v == 'pass' and verdict == 'fail':
                    if is_retry and not retried and not sticky:
                        verdict, flaky = 'pass', True
                        evidence = evidence + [f'{c.attempt_id}: {e}' for e in mine['evidence']]
                        detail = f'flaky: failed in {before.attempt_id} ({detail}), passed on retry {c.attempt_id}'
                    else:
                        detail += f'; a later pass in {c.attempt_id} is not an eligible retry'
                elif v == 'fail' and was == 'fail':   # every unresolved fail stays cited
                    detail += f'; failed again in {c.attempt_id}: {mine["detail"]}'
                    evidence = evidence + [f'{c.attempt_id}: {e}' for e in mine['evidence']]
                else:
                    flaky = flaky and v == 'pass' and verdict == 'pass'
                    if not flaky:
                        verdict, detail = v, mine['detail']
                        evidence = [f'{c.attempt_id}: {e}' for e in mine['evidence']]
                if v == 'fail' and (was == 'fail' or retried):
                    sticky = True
                retried = retried or is_retry
            before = c
        if verdict is None:
            verdict = 'not-run'
        withdrawn = [x['attempt_id'] for x in broken if x['outcome'] == 'withdrawn']
        if withdrawn:
            verdict, flaky = 'blocker', False
            detail = f'{", ".join(withdrawn)} ended without a published report; ' + detail
        elif verdict == 'pass' and broken:
            verdict, flaky = 'blocker', False
            detail = 'earlier evidence unavailable: ' + '; '.join(f'{x["attempt_id"]} {x["outcome"]}' for x in broken)
        rows.append({'board': b, 'verdict': verdict, 'flaky': flaky, 'detail': detail,
                     'evidence': evidence, 'attempts': seen})
    current = chain[-1].run if chain else None
    if current is not None:
        mine = [r for r in current['rows'] if r['board'] in requested]
        current = {**current, 'verdict': _fold(mine), 'rows': mine}
    return {'current': current,
            'sequence': {'verdict': _fold(rows), 'rows': rows,
                         'attempts': [c.attempt_id for c in chain], 'broken': broken}}
