"""The worktree entries that would shadow agentrc's installed workflows and roles (#59).

Project scope wins over the user scope for `Workflow({name})` and for role lookup, so a
branch's stale copy of an agentrc entry runs in place of the installed one.
"""
import filecmp
import os
import subprocess
from pathlib import Path

AGENTRC = Path(__file__).resolve().parents[3]   # the agentrc checkout this module runs from
KINDS = {'workflows': '.js', 'agents': '.md'}


def project(payload, *git_paths):
    """The hook session's project top level and each named `git rev-parse --git-path`, absolute;
    outside a repository, the start directory and None for each."""
    # the session's project, not a directory a Bash cd moved the payload's cwd to; its GIT_* would name another repository
    start = os.environ.get('CLAUDE_PROJECT_DIR') or payload.get('cwd') or '.'
    env = {k: v for k, v in os.environ.items() if not k.startswith('GIT_')}
    asked = ['--path-format=absolute', *(a for p in git_paths for a in ('--git-path', p))] if git_paths else []
    got = subprocess.run(['git', '-C', start, 'rev-parse', '--show-toplevel', *asked], capture_output=True, text=True, env=env)
    if got.returncode:
        return Path(start), [None] * len(git_paths)
    lines = got.stdout.splitlines()
    return Path(lines[0]), [Path(p) for p in lines[1:]]


def user_scope():
    return Path(os.environ.get('CLAUDE_CONFIG_DIR') or Path.home() / '.claude')


def shadows(worktree):
    """The worktree's `.claude/workflows` and `.claude/agents` entries that differ from the same-named
    agentrc ones installed in the user scope; an entry it cannot read raises OSError."""
    stale, scope = [], user_scope()
    for kind, suffix in KINDS.items():
        with os.scandir(AGENTRC / kind) as entries:
            names = {e.name for e in entries if e.name.endswith(suffix)}
        d = worktree / '.claude' / kind
        if not d.is_dir():
            continue
        with os.scandir(d) as entries:
            candidates = sorted(e.name for e in entries if e.name in names)
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
