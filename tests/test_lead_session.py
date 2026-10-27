"""Tests for lib/lead_session.py: what a session started by another one inherits."""
import os
import sys
import unittest
from pathlib import Path
from unittest import mock

ROOT = Path(__file__).resolve().parents[1]
sys.path.insert(0, str(ROOT / 'lib'))
import lead_session  # noqa: E402
sys.path.insert(0, str(ROOT / 'tests'))
from helpers import guard_survivors  # noqa: E402

# what the starting session's CLI or pane sets for it alone (lead_session's docstring has the evidence)
IDENTITY = {'CLAUDECODE', 'CLAUDE_PID', 'CLAUDE_CODE_SESSION_ID', 'CLAUDE_CODE_SESSION_ATTENDED', 'CLAUDE_EFFORT',
            'CLAUDE_CODE_MESSAGING_SOCKET', 'CLAUDE_CODE_MESSAGING_TOKEN', 'CLAUDE_CODE_BRIDGE_SESSION_ID',
            'CODEX_THREAD_ID', 'CODEX_SESSION_ID', 'CODEX_TOOL_CALL_ID', 'COWORK_READ_ONLY',
            'HERDR_ENV', 'HERDR_PANE_ID', 'HERDR_TAB_ID', 'HERDR_WORKSPACE_ID', 'HERDR_SOCKET_PATH', 'HERDR_BIN_PATH'}
# configuration and what is as true of the child: no blanket CLAUDE_* or CODEX_* drop
PASSED = {'CLAUDE_CONFIG_DIR', 'CLAUDE_CODE_CHILD_SESSION', 'CLAUDE_CODE_ENTRYPOINT', 'CLAUDE_CODE_SUBAGENT_MODEL',
          'CLAUDE_CODE_EXECPATH', 'CODEX_HOME', 'CODEX_VERSION', 'CODEX_CI', 'CODEX_SANDBOX_NETWORK_DISABLED',
          'COWORK_TURN', 'PATH', 'HOME', 'AN_HERDR_LOOKALIKE'}


def setUpModule():
    guard_survivors(__name__)


class ChildEnvTest(unittest.TestCase):
    def test_drops_the_starting_sessions_identity_and_passes_the_rest(self):
        environ = {k: f'v-{k}' for k in IDENTITY | PASSED}
        self.assertEqual(lead_session.child_env(environ), {k: f'v-{k}' for k in PASSED})

    def test_the_drop_list_is_exactly_the_verified_one(self):
        self.assertEqual(lead_session.LEAD_SESSION, {k for k in IDENTITY if not k.startswith('HERDR_')})

    def test_defaults_to_this_process_environment_and_copies_it(self):
        with mock.patch.dict('os.environ', {'CLAUDE_PID': '1', 'CLAUDE_CONFIG_DIR': '/c'}):
            env = lead_session.child_env()
            env['X'] = '1'
            self.assertNotIn('X', os.environ)
        self.assertNotIn('CLAUDE_PID', env)
        self.assertEqual(env['CLAUDE_CONFIG_DIR'], '/c')


if __name__ == '__main__':
    unittest.main()
