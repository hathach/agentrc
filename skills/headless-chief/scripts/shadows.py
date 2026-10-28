"""The worktree entries that would shadow agentrc's installed workflows and roles (#59).

Project scope wins over the user scope for `Workflow({name})` and for role lookup, so a
branch's stale copy of an agentrc entry runs in place of the installed one.
"""
import filecmp
import os
import subprocess
import sys
from pathlib import Path

AGENTRC = Path(__file__).resolve().parents[3]   # the agentrc checkout this module runs from
sys.path.insert(0, str(AGENTRC / 'lib'))
import windows  # noqa: E402

KINDS = {'workflows': '.js', 'agents': '.md'}
FOLD = str.casefold if os.name == 'nt' else str   # NTFS names match without case


def on_path(name, exts):
    """Windows: the program as PATH alone finds it (windows.on_path), else None. Elsewhere the name, as execvp
    finds it."""
    return windows.on_path(name, exts) if os.name == 'nt' else name


def project(payload, timeout=None):
    """The hook session's project top level and its chief directory (`git rev-parse --git-path chief`),
    absolute; outside a repository, the start directory and None. A git slower than timeout raises TimeoutExpired."""
    # the session's project, not a directory a Bash cd moved the payload's cwd to; its GIT_* would name another repository
    start = os.environ.get('CLAUDE_PROJECT_DIR') or payload.get('cwd') or '.'
    env = {k: v for k, v in os.environ.items() if not k.startswith('GIT_')}
    git = on_path('git', ('.exe',))
    if git is None:
        raise FileNotFoundError('git is not on PATH')
    got = subprocess.run([git, '-C', start, 'rev-parse', '--show-toplevel', '--path-format=absolute', '--git-path', 'chief'],
                         capture_output=True, env=env, timeout=timeout)
    if got.returncode:
        return Path(start), None
    top, chief = map(os.fsdecode, got.stdout.splitlines())   # git prints the path's bytes: UTF-8 on Windows
    return Path(top), Path(chief)


def user_scope():
    return Path(os.environ.get('CLAUDE_CONFIG_DIR') or Path.home() / '.claude')


def project_install(path):
    """A link `install.py --project` made: it resolves to this checkout's entry of the same name."""
    return path.is_symlink() and os.path.normcase(os.path.realpath(path)) == \
        os.path.normcase(os.path.realpath(AGENTRC / path.parent.name / path.name))


def shadows(worktree):
    """The worktree's `.claude/workflows` and `.claude/agents` entries that differ from the same-named
    agentrc ones installed in the user scope, except the links a project install made to this checkout;
    an entry it cannot read raises OSError."""
    stale, scope = [], user_scope()
    for kind, suffix in KINDS.items():
        with os.scandir(AGENTRC / kind) as entries:
            names = {FOLD(e.name) for e in entries if FOLD(e.name).endswith(suffix)}
        d = worktree / '.claude' / kind
        if not d.is_dir():
            continue
        with os.scandir(d) as entries:
            candidates = sorted(e.name for e in entries if FOLD(e.name) in names and not project_install(d / e.name))
        # an absent or dangling peer is no installed copy to shadow
        stale += [f'.claude/{kind}/{name}' for name in candidates
                  if (scope / kind / name).is_file() and not filecmp.cmp(d / name, scope / kind / name, shallow=False)]
    return stale


def refusal(root, remedy):
    """Why nothing may run in root, None when nothing shadows; remedy is the step after the merge."""
    try:
        stale = shadows(root)
    except OSError as e:
        return f'cannot inspect {root}/.claude: {e}'
    if stale:
        return (f'{root} carries {", ".join(stale)}, which would shadow the agentrc copies installed in {user_scope()}: '
                f'merge the default branch into the task branch (remove an untracked copy), then {remedy}')
    return None
