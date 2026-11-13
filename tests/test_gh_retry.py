"""Tests for lib/gh_retry.py: which gh calls are reads, and the bounded retry of a transient server error (#523)."""
import sys
import unittest
from pathlib import Path
from unittest import mock
sys.path.insert(0, str(Path(__file__).resolve().parent))
from helpers import guard_survivors  # noqa: E402
sys.path.insert(0, str(Path(__file__).resolve().parents[1] / 'lib'))
import gh_retry  # noqa: E402


def setUpModule():
    guard_survivors(__name__)


class Boom(Exception):
    def __init__(self, message, limit=None):
        super().__init__(message)
        self.limit = limit


class IsReadTest(unittest.TestCase):
    def test_reads(self):
        for args in (('pr', 'view', '7', '--json', 'headRefOid'), ('pr', 'checks', '7'), ('run', 'list', '--limit', '20'),
                     ('run', 'view', '9', '--json', 'jobs'), ('issue', 'view', '3'), ('repo', 'view'),
                     ('api', 'repos/o/r/pulls/7'), ('api', '--paginate', '--slurp', 'repos/o/r/issues/7/comments?per_page=100'),
                     ('api', 'repos/o/r', '--jq', '.permissions'), ('api', 'repos/o/r', '-q.permissions'),
                     ('api', 'repos/o/r/actions/jobs/1/logs', '--allow-escape-sequences'),
                     ('api', '-X', 'GET', 'search/issues', '-f', 'q=repo:o/r', '-f', 'per_page=10'),
                     ('api', '--method=get', 'search/issues', '--raw-field=q=x'), ('api', '-XGET', 'search/issues', '-Fq=x'),
                     ('api', '-X=HEAD', 'repos/o/r'),
                     ('api', 'graphql', '-f', 'query=query($o: String!) { repository(owner: $o) { id } }', '-F', 'o=x'),
                     ('api', 'graphql', '--raw-field=query=# reads\n{ viewer { login } }', '--paginate')):
            with self.subTest(args):
                self.assertTrue(gh_retry.is_read(args))

    def test_writes_and_unknown_forms_are_not_reads(self):
        for args in ((), ('pr', 'merge', '7'), ('pr', 'comment', '7', '-b', 'x'), ('run', 'rerun', '9'), ('run', 'cancel', '9'),
                     ('workflow', 'run', 'x'), ('pr',), ('api',),
                     ('api', '-X', 'POST', 'repos/o/r/actions/runs/7/rerun'), ('api', '-XPOST', 'repos/o/r/x'),
                     ('api', '-X=PATCH', 'repos/o/r/x'), ('api', '--method=DELETE', 'repos/o/r/x'), ('api', '--method', 'put', 'repos/o/r/x'),
                     ('api', 'repos/o/r/issues/5/comments', '-f', 'body=x'), ('api', 'repos/o/r/issues/5/comments', '-fbody=x'),
                     ('api', 'repos/o/r/issues/5/comments', '--field=body=x'), ('api', 'repos/o/r/issues/5/comments', '--raw-field', 'body=x'),
                     ('api', 'repos/o/r/x', '--input', '-'), ('api', 'repos/o/r/x', '--input=f.json'), ('api', '-X', 'GET', 'x', '--input', '-'),
                     ('api', '-X', 'GET', '-X', 'POST', 'x'), ('api', 'x', '-H', 'X-HTTP-Method-Override: DELETE'),
                     ('api', 'x', '--bogus'), ('api', 'x', '-Z'), ('api', 'x', '-iXPOST'), ('api', 'x', 'y'), ('api', 'x', '--jq'),
                     ('api', 'x', '--paginate=true'),
                     ('api', 'graphql', '-f', 'query=mutation { resolveReviewThread(input: {threadId: "t"}) { thread { id } } }'),
                     ('api', 'graphql', '-f', 'query=  mutation M { x }'), ('api', 'graphql', '-f', 'query=subscription { x }'),
                     ('api', 'graphql', '-F', 'query=@q.graphql'), ('api', 'graphql', '-F', 'o=x'),
                     ('api', 'graphql', '-f', 'query={ a }', '-f', 'query={ b }'), ('api', 'graphql', '-X', 'GET', '-f', 'query={ a }'),
                     ('api', 'graphql', '--input', 'q.json')):
            with self.subTest(args):
                self.assertFalse(gh_retry.is_read(args))


class ReadTest(unittest.TestCase):
    def read(self, answers, args=('api', 'repos/o/r'), stop=None):
        calls, waits = [], []

        def once():
            calls.append(1)
            got = answers.pop(0) if len(answers) > 1 else answers[0]
            if isinstance(got, Exception):
                raise got
            return got
        with mock.patch.object(gh_retry.time, 'sleep', waits.append):
            try:
                return gh_retry.read(args, once, Boom, stop=stop), len(calls), waits
            except Exception as e:
                return e, len(calls), waits

    def test_a_transient_error_then_the_answer(self):
        for err in ('gh: Server Error (HTTP 502)', 'HTTP 503: No server is currently available', 'HTTP 504: Gateway Timeout'):
            with self.subTest(err):
                self.assertEqual(self.read([Boom(err), 'ok']), ('ok', 2, [2]))

    def test_exhaustion_raises_the_last_error(self):
        got, calls, waits = self.read([Boom('HTTP 502 first'), Boom('HTTP 503 second'), Boom('HTTP 504 last')])
        self.assertEqual((str(got), calls, waits), ('HTTP 504 last', 3, list(gh_retry.WAITS)))

    def test_nothing_else_is_made_again(self):
        for what, err, args in (('a 500', Boom('HTTP 500'), None), ('a 404', Boom('Not Found (HTTP 404)'), None),
                                ('a rate limit in the text', Boom('HTTP 503: API rate limit exceeded'), None),
                                ('a rate limit the caller read', Boom('HTTP 503', limit={'resetAt': None}), None),
                                ('another exception', ValueError('HTTP 503'), None),
                                ('a mutation', Boom('HTTP 502'), ('api', 'graphql', '-f', 'query=mutation { x }')),
                                ('a POST', Boom('HTTP 502'), ('api', '-X', 'POST', 'repos/o/r/actions/runs/7/rerun'))):
            with self.subTest(what):
                got, calls, waits = self.read([err, 'ok'], *([args] if args else []))
                self.assertIs(got, err)
                self.assertEqual((calls, waits), (1, []))

    def test_stop_runs_before_every_attempt_and_ends_it(self):
        seen = []

        def stop():
            seen.append(1)
            if len(seen) == 2:
                raise Boom('stopped before the next GitHub call')
        got, calls, waits = self.read([Boom('HTTP 502')], stop=stop)
        self.assertEqual((str(got), calls, waits, len(seen)), ('stopped before the next GitHub call', 1, [2], 2))


if __name__ == '__main__':
    unittest.main()
