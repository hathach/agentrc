"""The worktree entries that would shadow agentrc's installed workflows and roles (#59).

Project scope wins over the user scope for `Workflow({name})` and for role lookup, so a
branch's stale copy of an agentrc entry runs in place of the installed one.
"""
import filecmp
import os
from pathlib import Path

AGENTRC = Path(__file__).resolve().parents[3]   # the agentrc checkout this module runs from
KINDS = {'workflows': '.js', 'agents': '.md'}


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
