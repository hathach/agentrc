"""A GitHub read through gh made again after a transient server error (#424, #523). Stdlib only.

read(args, once, retry_on, stop=None) returns once(), the caller's own single run of `gh ARGS`. When once() raises one of
retry_on naming HTTP 502, 503 or 504, with no rate limit (the error's `limit`, or its text), and is_read(args), it is
made again after each of WAITS seconds, and the last error raised; anything else is raised at once. stop(), when given,
runs before every attempt, the first included, and raises the caller's own error to end it. What once() decodes, the
exit codes it accepts and the errors it raises stay the caller's, so a read it accepts is never made again.

is_read(args) recognizes reads only; anything else, an unknown command or flag included, runs once:
- `gh api ENDPOINT` sent as GET or HEAD: no -X/--method or one naming GET or HEAD, fields (-f, -F, --field,
  --raw-field) only with an explicit GET or HEAD, as gh then sends them as query parameters, and no --input;
- `gh api graphql` with one `query` field whose document names no mutation or subscription, no method and no --input;
- `gh pr view|checks|diff|list`, `gh run view|list`, `gh issue view|list` and `gh repo view`.
Flags are read in every spelling gh's parser takes (`-XPOST`, `-X=POST`, `--method=POST`, `-fkey=v`, `--field=k=v`).

A script reaches it through its resolved path, as it reaches lib/seal.py:
`sys.path.insert(0, str(Path(__file__).resolve().parents[N] / 'lib'))`, then `import gh_retry`."""
import re
import time

from seal import RATE_LIMITED

TRANSIENT = re.compile(r'\bHTTP 50[234]\b')
WAITS = (2, 5)
READ_COMMANDS = {'pr': ('view', 'checks', 'diff', 'list'), 'run': ('view', 'list'), 'issue': ('view', 'list'), 'repo': ('view',)}
# gh api's flags taking a value, and those taking none, by their long and short names.
VALUED = {'--method': 'method', '-X': 'method', '--field': 'field', '-F': 'field', '--raw-field': 'field', '-f': 'field',
          '--input': 'input', '--header': 'header', '-H': 'header', '--jq': None, '-q': None, '--template': None, '-t': None,
          '--hostname': None, '--cache': None, '--preview': None, '-p': None}
BARE = {'--paginate', '--slurp', '--include', '-i', '--silent', '--verbose', '--allow-escape-sequences'}
READ_METHODS = ('GET', 'HEAD')
OPERATION = re.compile(r'\b(mutation|subscription)\b')


def api_flags(args):
    """(the endpoint, [(kind, value)] of its valued flags), None when a token is no flag gh api takes or a second
    endpoint."""
    endpoint, flags, i = None, [], 0
    while i < len(args):
        token = args[i]
        if token.startswith('--'):
            name, eq, value = token.partition('=')
        elif token.startswith('-') and len(token) > 1:
            # A shorthand takes its value attached, after an optional `=`, or as the next token.
            name, value = token[:2], token[2:]
            eq = bool(value)
            value = value[1:] if value.startswith('=') else value
        else:
            if endpoint is not None:
                return None
            endpoint, i = token, i + 1
            continue
        if name in BARE and not eq and name == token:
            i += 1
        elif name in VALUED:
            if not eq:
                if i + 1 >= len(args):
                    return None
                value, i = args[i + 1], i + 1
            flags.append((VALUED[name], value))
            i += 1
        else:
            return None
    return (endpoint, flags) if endpoint else None


def is_read(args):
    """Whether `gh ARGS` is a read this module may make again."""
    args = list(args)
    if not args:
        return False
    if args[0] != 'api':
        return len(args) > 1 and args[1] in READ_COMMANDS.get(args[0], ())
    parsed = api_flags(args[1:])
    if parsed is None:
        return False
    endpoint, flags = parsed
    kinds = [k for k, _ in flags]
    methods = [v.upper() for k, v in flags if k == 'method']
    if 'input' in kinds or any('override' in v.lower() for k, v in flags if k == 'header'):
        return False
    if endpoint == 'graphql':
        if methods:
            return False
        fields = [v for k, v in flags if k == 'field']
        queries = [v.partition('=')[2] for v in fields if v.partition('=')[0] == 'query']
        return len(queries) == 1 and not queries[0].startswith('@') and not OPERATION.search(queries[0])
    if len(methods) > 1:
        return False
    return methods[0] in READ_METHODS if methods else 'field' not in kinds


def read(args, once, retry_on, stop=None):
    """once(), made again after each of WAITS while it raises a transient server error on a read (above)."""
    again = is_read(args)
    for wait in (*WAITS, None):
        if stop:
            stop()
        try:
            return once()
        except retry_on as e:
            if wait is None or not again or getattr(e, 'limit', None) or RATE_LIMITED.search(str(e)) or not TRANSIENT.search(str(e)):
                raise
        time.sleep(wait)
