"""The environment a session started by another one gets (a headless chief, a coworker turn): the starting session's
own, minus what names that session. Configuration passes, CLAUDE_CONFIG_DIR and settings among it, so this is no
blanket CLAUDE_* or CODEX_* drop.

A script reaches it as it reaches windows.py: `sys.path.insert(0, str(Path(__file__).resolve().parents[N] / 'lib'))`,
then `import lead_session`.

Checked against Claude Code 2.1.311 and Codex 0.164.0 (#273). Each CLI sets these on its own tool and hook children,
so a session started from one of those inherits them:
- Claude Code: CLAUDECODE, CLAUDE_PID, CLAUDE_CODE_SESSION_ID, CLAUDE_CODE_SESSION_ATTENDED and the turn's
  CLAUDE_EFFORT; and CLAUDE_CODE_MESSAGING_SOCKET/_TOKEN and CLAUDE_CODE_BRIDGE_SESSION_ID, the session's own
  messaging endpoint and remote-control bridge.
- Codex: CODEX_THREAD_ID, CODEX_SESSION_ID and CODEX_TOOL_CALL_ID.
- Herdr: HERDR_*, the pane the session runs in.
- cowork: COWORK_READ_ONLY, the coworker relay's own scope (coworker_guard.py).
Kept: CLAUDE_CODE_CHILD_SESSION, which Claude Code reads as "started by a model-driven session" and which is true of
the child too; CLAUDE_CODE_ENTRYPOINT, the host's launch surface, which Claude Code re-derives for -p (cli becomes
sdk-cli) and pairs with the host's auth and gateway variables; CLAUDE_CODE_SUBAGENT_MODEL, user configuration
(settings.json env).
"""
import os

LEAD_SESSION = frozenset({
    'CLAUDECODE', 'CLAUDE_PID', 'CLAUDE_CODE_SESSION_ID', 'CLAUDE_CODE_SESSION_ATTENDED', 'CLAUDE_EFFORT',
    'CLAUDE_CODE_MESSAGING_SOCKET', 'CLAUDE_CODE_MESSAGING_TOKEN', 'CLAUDE_CODE_BRIDGE_SESSION_ID',
    'CODEX_THREAD_ID', 'CODEX_SESSION_ID', 'CODEX_TOOL_CALL_ID',
    'COWORK_READ_ONLY'})
PANE = 'HERDR_'


def child_env(environ=None):
    """environ (default os.environ) without the starting session's identity."""
    environ = os.environ if environ is None else environ
    return {k: v for k, v in environ.items() if k not in LEAD_SESSION and not k.startswith(PANE)}
