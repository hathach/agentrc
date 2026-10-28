"""Tests for ci-rerun's circleci.py and rtd.py against fake endpoints and a fake CLI."""
import importlib.util
import io
import http.server
import json
import os
import subprocess
import sys
import tempfile
import threading
import unittest
from contextlib import chdir, redirect_stdout, redirect_stderr
from pathlib import Path
from unittest import mock
sys.path.insert(0, str(Path(__file__).resolve().parent))
from helpers import guard_survivors, remotes_checkout  # noqa: E402

SCRIPTS = Path(__file__).resolve().parents[1] / 'skills' / 'ci-rerun' / 'scripts'


def load(name):
    spec = importlib.util.spec_from_file_location(f'ci_{name}', SCRIPTS / f'{name}.py')
    module = importlib.util.module_from_spec(spec)
    spec.loader.exec_module(module)
    return module


ci, rtd = load('circleci'), load('rtd')

W1, W2, NEW = '3a705162-e09b-4ef8-9488-d034f9818421', '7a72feba-da90-4613-bd4e-7b6b4b674475', 'd2814134-464c-4966-9dff-c403909287d9'
BASE = 'https://circleci.com/api/v1.1/project/github/o/r/'
WORKFLOW = 'https://circleci.com/api/v2/workflow/'


def setUpModule():
    guard_survivors(__name__)


class CircleciTest(unittest.TestCase):
    def setUp(self):
        self.jobs = {}      # number -> record
        self.outputs = {}   # output_url -> messages
        self.cli = []       # argv of each circleci call
        self.cli_result = lambda wid: (0, json.dumps({'workflow_id': NEW}), '')
        self.status = {}    # workflow id -> v2 status, 'failed' when absent

        def fetch(url):
            if url.startswith(WORKFLOW):
                status = self.status.get(url[len(WORKFLOW):], 'failed')
                if isinstance(status, Exception):
                    raise status
                return {'id': url[len(WORKFLOW):], 'status': status}
            if url.startswith(BASE):
                n = url[len(BASE):]
                if n in self.jobs:
                    return self.jobs[n]
                raise ci.Failed(f'{url}: HTTP Error 404')
            if url in self.outputs:
                return self.outputs[url]
            raise ci.Failed(f'{url}: HTTP Error 502')

        def run(argv, **kw):
            self.cli.append(argv)
            rc, out, err = self.cli_result(argv[3])
            return mock.Mock(returncode=rc, stdout=out, stderr=err)
        for target, fake in (('fetch', fetch), ('subprocess', mock.Mock(run=run))):
            patcher = mock.patch.object(ci, target, fake)
            patcher.start()
            self.addCleanup(patcher.stop)

    def job(self, number, wid, steps=()):
        self.jobs[str(number)] = {'workflows': {'workflow_id': wid}, 'steps': list(steps)}

    def main(self, *argv):
        out, err = io.StringIO(), io.StringIO()
        with redirect_stdout(out), redirect_stderr(err):
            rc = ci.main([*argv, '--repo', 'o/r'])
        return rc, out.getvalue(), err.getvalue()

    def test_jobs_of_one_workflow_are_one_rerun(self):
        self.job(1, W1); self.job(2, W1); self.job(3, W2)
        rc, out, _ = self.main('rerun', '1', '2', '3')
        self.assertEqual(rc, 0)
        self.assertEqual(json.loads(out.strip().splitlines()[-1]), {'reruns': [
            {'workflow': W1, 'jobs': [1, 2], 'newWorkflow': NEW}, {'workflow': W2, 'jobs': [3], 'newWorkflow': NEW}], 'running': [], 'errors': []})
        self.assertEqual(self.cli, [['circleci', 'workflow', 'rerun', W1, '--from-failed', '--json'],
                                    ['circleci', 'workflow', 'rerun', W2, '--from-failed', '--json']])

    def test_unknown_job_and_failed_cli_are_errors_the_rest_still_runs(self):
        self.job(1, W1); self.job(3, W2)
        self.cli_result = lambda wid: (0, json.dumps({'workflow_id': NEW}), '') if wid == W2 else (1, '', 'Unauthorized')
        rc, out, _ = self.main('rerun', '1', '2', '3')
        self.assertEqual(rc, 1)
        r = json.loads(out.strip().splitlines()[-1])
        self.assertEqual(r['reruns'], [{'workflow': W2, 'jobs': [3], 'newWorkflow': NEW}])
        self.assertEqual(len(r['errors']), 2)
        self.assertIn('404', r['errors'][0])
        self.assertIn('Unauthorized', r['errors'][1])

    def test_a_record_without_a_workflow_uuid_is_refused(self):
        self.jobs['5'] = {'workflows': {'workflow_id': 'build'}}
        rc, out, _ = self.main('rerun', '5')
        self.assertEqual(rc, 1)
        self.assertEqual(self.cli, [])
        self.assertIn('no workflow id', json.loads(out)['errors'][0])

    def test_a_cli_answer_without_a_new_workflow_is_an_error(self):
        self.job(1, W1)
        self.cli_result = lambda wid: (0, 'not json', '')
        rc, out, _ = self.main('rerun', '1')
        self.assertEqual(rc, 1)
        self.assertIn('rerun failed', json.loads(out)['errors'][0])

    def test_a_running_workflow_is_reported_not_rerun(self):
        self.job(1, W1); self.job(2, W1); self.job(3, W2)
        self.status[W1] = 'failing'
        rc, out, _ = self.main('rerun', '1', '2', '3')
        self.assertEqual(rc, 3)
        self.assertEqual(json.loads(out), {'reruns': [{'workflow': W2, 'jobs': [3], 'newWorkflow': NEW}],
                                           'running': [{'workflow': W1, 'jobs': [1, 2], 'status': 'failing'}], 'errors': []})
        self.assertEqual([argv[3] for argv in self.cli], [W2])

    def test_a_queued_workflow_is_not_finished(self):
        self.job(1, W1)
        self.status[W1] = 'queued'
        rc, out, _ = self.main('rerun', '1')
        self.assertEqual((rc, self.cli, json.loads(out)['running']), (3, [], [{'workflow': W1, 'jobs': [1], 'status': 'queued'}]))

    def test_an_error_outranks_a_running_workflow(self):
        self.job(1, W1)
        self.status[W1] = 'on_hold'
        rc, out, _ = self.main('rerun', '1', '2')
        r = json.loads(out)
        self.assertEqual((rc, self.cli, r['running'][0]['status'], len(r['errors'])), (1, [], 'on_hold', 1))

    def test_a_status_that_cannot_be_read_is_an_error_not_running(self):
        self.job(1, W1); self.job(2, W2)
        self.status[W1] = ci.Failed(f'{WORKFLOW}{W1}: HTTP Error 503')
        self.status[W2] = None
        rc, out, _ = self.main('rerun', '1', '2')
        r = json.loads(out)
        self.assertEqual((rc, self.cli, r['reruns'], r['running']), (1, [], [], []))
        self.assertIn('503', r['errors'][0])
        self.assertIn('jobs 1', r['errors'][0])
        self.assertIn('unknown status None', r['errors'][1])

    def test_log_prints_the_failed_steps_tail(self):
        self.job(9, W1, [{'name': 'Checkout code', 'actions': [{'status': 'success', 'output_url': 'u0'}]},
                         {'name': 'Build', 'actions': [{'status': 'failed', 'output_url': 'u1'}]}])
        self.outputs['u1'] = [{'message': 'line1\nline2\n'}, {'message': 'FAILED: x\n'}]
        rc, out, _ = self.main('log', '9', '--lines', '2')
        self.assertEqual(rc, 0)
        self.assertEqual(out, '== Build\nline2\nFAILED: x\n')

    def test_failed_steps_read_whole_outputs_and_say_when_one_is_not_whole(self):
        # #319: the failure sat on line 1 of 200, outside the old 150-line tail.
        out = [{'message': 'region RAM overflowed\n' + ''.join(f'line {i}\n' for i in range(199))}]
        self.job(9, W1, [{'name': 'Build', 'actions': [{'status': 'failed', 'output_url': 'u1', 'truncated': False}]}])
        self.outputs['u1'] = out
        [(name, text, whole)] = ci.failed_steps('o/r', 9)
        self.assertEqual((name, text.splitlines()[0], len(text.splitlines()), whole), ('Build', 'region RAM overflowed', 200, True))
        self.assertFalse(ci.failed_steps('o/r', 9, 150)[0][2], 'a tail is never whole')
        for action in ({'truncated': True}, {}, {'truncated': None}):
            self.job(9, W1, [{'name': 'Build', 'actions': [{'status': 'failed', 'output_url': 'u1', **action}]}])
            self.assertFalse(ci.failed_steps('o/r', 9)[0][2], action)
        with mock.patch.object(ci, 'MAX_LINES', 100):
            self.job(9, W1, [{'name': 'Build', 'actions': [{'status': 'failed', 'output_url': 'u1', 'truncated': False}]}])
            [(_, text, whole)] = ci.failed_steps('o/r', 9)
            self.assertEqual((len(text.splitlines()), whole), (100, False))
        self.job(9, W1, [{'name': 'Build', 'actions': [{'status': 'failed', 'output_url': 'u1', 'truncated': False}]},
                         {'name': 'Upload', 'actions': [{'status': 'failed'}]}])
        self.assertEqual([(n, t == '', w) for n, t, w in ci.failed_steps('o/r', 9)], [('Build', False, True), ('Upload', True, False)],
                         'a failed step with no output is listed, not whole')
        rc, printed, _ = self.main('log', '9', '--lines', '0')
        self.assertEqual((rc, printed.count('line ')), (0, 199), '--lines 0 prints all of it')

    def test_log_without_a_failed_step_is_an_error(self):
        self.job(9, W1, [{'name': 'Build', 'actions': [{'status': 'success', 'output_url': 'u0'}]}])
        rc, _, err = self.main('log', '9')
        self.assertEqual(rc, 1)
        self.assertIn('no failed step', err)

    def run_without_repo(self, *remotes):
        out, err = io.StringIO(), io.StringIO()
        with chdir(remotes_checkout(self, *remotes)), redirect_stdout(out), redirect_stderr(err):
            rc = ci.main(['rerun', '1'])
        return rc, out.getvalue(), err.getvalue()

    def test_without_repo_the_checkouts_one_github_repository_is_named(self):
        self.job(1, W1)
        rc, out, _ = self.run_without_repo(('origin', 'git@github.com:o/r.git'))
        self.assertEqual(rc, 0)
        self.assertEqual(json.loads(out.strip().splitlines()[-1])['reruns'], [{'workflow': W1, 'jobs': [1], 'newWorkflow': NEW}])

    def test_without_repo_several_repositories_refuse_before_any_call(self):
        fetched = []
        self.enterContext(mock.patch.object(ci, 'fetch', lambda url: fetched.append(url)))
        rc, _, err = self.run_without_repo(('origin', 'https://github.com/me/r.git'), ('upstream', 'https://github.com/o/r.git'))
        self.assertEqual(rc, 2)
        self.assertIn('none is marked gh-resolved: name the repository (--repo OWNER/NAME)', err)
        self.assertEqual((fetched, self.cli), ([], []))


API = 'https://app.readthedocs.org/api/v3/projects/tinyusb'
URL = 'https://app.readthedocs.org/projects/tinyusb/builds/{}/'


class RtdTest(unittest.TestCase):
    def setUp(self):
        self.builds = {}   # build id -> record
        self.notes = {}    # build id -> notification messages
        self.posts = []    # version slugs a build was triggered for
        self.keys = set()
        self.commands = {}  # build id -> v2 commands

        def call(url, key, method='GET'):
            self.keys.add(key)
            if method == 'POST':
                version = url[len(f'{API}/versions/'):-len('/builds/')]
                self.posts.append(version)
                return {'build': {'id': 900 + len(self.posts)}}
            if '/api/v2/build/' in url:
                return {'commands': self.commands.get(url.rstrip('/').rsplit('/', 1)[1], [])}
            build = url[len(f'{API}/builds/'):].split('/')[0]
            if build not in self.builds:
                raise rtd.Failed(f'GET {url}: HTTP 404')
            if url.endswith('/notifications/'):
                return {'results': [{'message': m} for m in self.notes.get(build, [])]}
            return self.builds[build]
        for patcher in (mock.patch.object(rtd, 'call', call), mock.patch.dict(rtd.os.environ, {'RTD_TOKEN': 'k'}),
                        mock.patch.object(rtd, 'bash', lambda: 'bash')):
            patcher.start()
            self.addCleanup(patcher.stop)

    def main(self, *argv):
        out, err = io.StringIO(), io.StringIO()
        with redirect_stdout(out), redirect_stderr(err):
            rc = rtd.main(list(argv))
        return rc, out.getvalue(), err.getvalue()

    def test_rerun_triggers_each_builds_version(self):
        self.builds['1'] = {'version': '3978'}
        self.builds['2'] = {'version': 'latest'}
        rc, out, _ = self.main('rerun', URL.format(1), URL.format(2))
        self.assertEqual(rc, 0)
        self.assertEqual(json.loads(out), {'reruns': [{'build': 1, 'version': '3978', 'newBuild': 901},
                                                      {'build': 2, 'version': 'latest', 'newBuild': 902}], 'errors': []})
        self.assertEqual((self.posts, self.keys), (['3978', 'latest'], {'k'}))

    def test_unknown_build_and_bad_url_are_errors_the_rest_still_runs(self):
        self.builds['1'] = {'version': '3978'}
        rc, out, _ = self.main('rerun', URL.format(7), 'https://example.com/projects/tinyusb/builds/2/', URL.format(1))
        self.assertEqual(rc, 1)
        r = json.loads(out)
        self.assertEqual(r['reruns'], [{'build': 1, 'version': '3978', 'newBuild': 901}])
        self.assertIn('404', r['errors'][0])
        self.assertIn('not a build URL', r['errors'][1])

    def test_builds_of_one_version_share_one_trigger(self):
        self.builds['1'] = {'version': '3978'}
        self.builds['2'] = {'version': '3978'}
        rc, out, _ = self.main('rerun', URL.format(1), URL.format(2))
        self.assertEqual((rc, self.posts), (0, ['3978']))
        self.assertEqual([r['newBuild'] for r in json.loads(out)['reruns']], [901, 901])

    def test_an_unconfirmed_trigger_is_not_repeated_for_the_same_version(self):
        self.builds['1'] = {'version': '3978'}
        self.builds['2'] = {'version': '3978'}
        fake = rtd.call

        def lost_answer(url, key, method='GET'):
            if method == 'POST':
                self.posts.append(url)
                raise rtd.Failed(f'POST {url}: timed out')
            return fake(url, key, method)
        with mock.patch.object(rtd, 'call', lost_answer):
            rc, out, _ = self.main('rerun', URL.format(1), URL.format(2))
        errors = json.loads(out)['errors']
        self.assertEqual((rc, len(self.posts)), (1, 1))
        self.assertIn('timed out', errors[0])
        self.assertIn('already triggered in this run without a confirmed build', errors[1])

    def test_a_record_without_a_version_is_not_triggered(self):
        self.builds['1'] = {'version': None}
        rc, out, _ = self.main('rerun', URL.format(1))
        self.assertEqual((rc, self.posts), (1, []))
        self.assertIn('no version', json.loads(out)['errors'][0])

    def test_log_prints_state_and_notifications_without_markup(self):
        self.builds['5'] = {'state': {'code': 'finished'}, 'success': False, 'duration': 3, 'commit': 'abc'}
        self.notes['5'] = [{'type': 'error', 'header': 'Error while checking out the repository',
                            'body': 'Failed to checkout revision: <code>abc</code> &amp; more'}]
        self.commands['5'] = [{'exit_code': 0, 'command': 'git clone', 'output': 'ok'},
                              {'exit_code': 128, 'command': 'git checkout --force abc',
                               'output': '\n'.join(f'line {i}' for i in range(50)) + '\nfatal: reference is not a tree: abc'}]
        rc, out, _ = self.main('log', URL.format(5))
        self.assertEqual(rc, 0)
        self.assertEqual(out, 'build 5: finished, success False, 3 s, commit abc\n'
                              '== error: Error while checking out the repository\nFailed to checkout revision: abc & more\n'
                              '== command exited 128: git checkout --force abc\n'
                              + '\n'.join(f'line {i}' for i in range(11, 50)) + '\nfatal: reference is not a tree: abc\n')

    def test_log_prints_the_records_error_and_says_when_there_is_no_reason(self):
        self.builds['5'] = {'state': {'code': 'finished'}, 'success': False, 'duration': 3, 'commit': 'abc', 'error': 'Build timed out'}
        self.builds['6'] = {'state': {'code': 'finished'}, 'success': False, 'duration': 3, 'commit': 'abc'}
        self.assertIn('error: Build timed out\n', self.main('log', URL.format(5))[1])
        self.assertIn('no failure reason recorded by the API', self.main('log', URL.format(6))[1])

    def test_no_token_anywhere_stops_before_any_call(self):
        del rtd.os.environ['RTD_TOKEN']
        with mock.patch.object(rtd.subprocess, 'run', return_value=mock.Mock(stdout='')):
            rc, _, err = self.main('rerun', URL.format(1))
        self.assertEqual((rc, self.keys), (2, set()))
        self.assertIn('RTD_TOKEN is not set', err)

    def test_a_login_shell_that_prints_more_than_the_token_is_refused_without_echoing_it(self):
        del rtd.os.environ['RTD_TOKEN']
        with mock.patch.object(rtd.subprocess, 'run', return_value=mock.Mock(stdout='welcome\nSECRET')):
            rc, out, err = self.main('rerun', URL.format(1))
        self.assertEqual((rc, self.keys), (2, set()))
        self.assertIn('not a single token', err)
        self.assertNotIn('SECRET', out + err)

    def test_a_non_ascii_token_is_a_setup_error(self):
        rtd.os.environ['RTD_TOKEN'] = '秘密'
        rc, _, err = self.main('log', URL.format(1))
        self.assertEqual((rc, self.keys), (2, set()))
        self.assertIn('not a single token', err)

    def test_the_login_shell_is_the_bash_on_path(self):
        """Windows resolves a bare 'bash' to System32's WSL launcher before PATH."""
        del rtd.os.environ['RTD_TOKEN']
        run = mock.Mock(return_value=mock.Mock(stdout='tok'))
        with mock.patch.object(rtd.subprocess, 'run', run), mock.patch.object(rtd, 'bash', lambda: '/opt/bash'):
            self.main('log', URL.format(1))
        self.assertEqual((run.call_args.args[0][:2], self.keys), (['/opt/bash', '-lc'], {'tok'}))

    def test_no_bash_for_the_login_shell_is_a_setup_error_before_any_call(self):
        del rtd.os.environ['RTD_TOKEN']
        run = mock.Mock()
        with mock.patch.object(rtd.subprocess, 'run', run), \
                mock.patch.object(rtd, 'bash', mock.Mock(side_effect=rtd.Failed('no bash.exe is on PATH'))):
            rc, _, err = self.main('log', URL.format(1))
        self.assertEqual((rc, self.keys, run.called), (2, set(), False))
        self.assertIn('no bash.exe is on PATH', err)

    def test_a_refusal_prints_utf8_whatever_the_console(self):
        # PATH an empty dir: no gh or bash to reach; and none of Python's own UTF-8 switches, so the script's is what is tested
        with tempfile.TemporaryDirectory() as bare:
            env = {**{k: v for k, v in os.environ.items() if k.upper() not in ('PATH', 'PYTHONUTF8', 'PYTHONIOENCODING')},
                   'PATH': bare, 'RTD_TOKEN': 'k'}
            done = subprocess.run([sys.executable, str(SCRIPTS / 'rtd.py'), 'log', 'https://example.org/修复/🙂'],
                                  capture_output=True, env=env)
        self.assertEqual(done.returncode, 1)
        self.assertIn('修复/🙂: not a build URL', done.stderr.decode('utf-8'))

    def test_a_login_shell_that_hangs_is_a_setup_error(self):
        del rtd.os.environ['RTD_TOKEN']
        with mock.patch.object(rtd.subprocess, 'run', side_effect=rtd.subprocess.TimeoutExpired('bash', 10)):
            rc, _, err = self.main('log', URL.format(1))
        self.assertEqual(rc, 2)
        self.assertIn('login shell failed', err)


class RtdBashTest(unittest.TestCase):
    def test_no_bash_on_path_is_its_error(self):
        # the lookup itself is windows.bash()'s, tested in test_windows_runtime
        with mock.patch.object(rtd.windows, 'bash', return_value=None), self.assertRaisesRegex(rtd.Failed, 'no bash.exe is on PATH'):
            rtd.bash()


class RtdRedirectTest(unittest.TestCase):
    def test_a_redirect_is_refused_not_followed_with_the_token(self):
        hits = []

        class Handler(http.server.BaseHTTPRequestHandler):
            def do_GET(self):
                hits.append((self.path, self.headers.get('Authorization')))
                self.send_response(302)
                self.send_header('Location', '/elsewhere')
                self.end_headers()

            def log_message(self, *args):
                pass
        server = http.server.HTTPServer(('127.0.0.1', 0), Handler)
        threading.Thread(target=server.handle_request, daemon=True).start()
        self.addCleanup(server.server_close)
        with self.assertRaisesRegex(rtd.Failed, 'HTTP 302'):
            rtd.call(f'http://127.0.0.1:{server.server_port}/api/v3/x/', 'k')
        self.assertEqual(hits, [('/api/v3/x/', 'Token k')])


if __name__ == '__main__':
    unittest.main()
