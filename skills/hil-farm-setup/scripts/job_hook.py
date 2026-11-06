#!/usr/bin/python3 -I
"""The runner's job hooks: hold the farm after a job that may have left processes behind.

  job_hook.py start UNIT      before each job (ACTIONS_RUNNER_HOOK_JOB_STARTED), as that runner's user
  job_hook.py complete UNIT   after it (ACTIONS_RUNNER_HOOK_JOB_COMPLETED), an always() post step

`start` refuses the job (exit 1) while the runner unit's cgroup subtree holds any process
besides this hook and its ancestors: what is left from an earlier job, root helpers
included. A marker naming a job that never reached `complete` (a killed worker, a host
crash) is only a warning once that scan is clean: its board locks ended with its processes,
and the next task on each board logs what it left as a failed cleanup (agentrc#464). It decides only at
the next start, after the runner's orphan cleanup, which runs after every post step (runner
v2.338.0 JobExtension.FinalizeJob). `complete` only marks its own job reached. Any doubt (an
unreadable or unsettled cgroup, no job id) refuses.

The marker is the runner user's own file, so each runner has its own: a guard against accidents in
trusted jobs, not a boundary.
An operator clears a hold by checking the named processes and the boards, then ending those processes.
"""
import os
import sys
from pathlib import Path

MARKER = Path.home() / '.hil-job-active'
CGROUP_ROOT = Path('/sys/fs/cgroup/system.slice')
PROC = Path('/proc')
DONE = ' done'


class Hold(Exception):
    pass


def _raise(e):
    raise e


def job_id():
    parts = [os.environ.get(k, '') for k in ('GITHUB_RUN_ID', 'GITHUB_RUN_ATTEMPT', 'GITHUB_JOB')]
    if not all(parts):
        raise Hold('no GITHUB_RUN_ID/GITHUB_RUN_ATTEMPT/GITHUB_JOB to name this job')
    return '-'.join(parts)


def unit_pids(unit):
    """Every pid in the unit's cgroup and its child cgroups; unreadable is a Hold, never empty."""
    root = CGROUP_ROOT / f'{unit}.service'
    pids = set()
    try:
        for d, _, _ in os.walk(root, onerror=_raise):
            pids |= {int(p) for p in (Path(d) / 'cgroup.procs').read_text().split()}
    except (OSError, ValueError) as e:
        raise Hold(f'cannot read the {unit} cgroup: {e}')
    if not pids:
        raise Hold(f'the {unit} cgroup lists no process, not even this hook')
    return pids


def parent(pid):
    """ppid, or None once the process is gone. comm may hold spaces and ')': parse after the last ')'."""
    try:
        stat = (PROC / str(pid) / 'stat').read_text(errors='replace')
    except FileNotFoundError:
        return None
    return int(stat.rpartition(')')[2].split()[1])


def survivors(unit, rounds=5):
    """Pids in the unit's cgroup that are neither this hook, its ancestors nor its children. A
    candidate whose ancestry breaks off mid-walk counts unless it is gone itself; the listing is
    taken again until no pid joined it while looking, so a child forked by an exiting parent is seen."""
    me = os.getpid()
    mine = set()
    pid = me
    while pid and pid != 1:
        mine.add(pid)
        pid = parent(pid)
        if pid is None:
            raise Hold('an ancestor of this hook vanished while it looked')
    seen = unit_pids(unit)
    for _ in range(rounds):
        left = []
        for pid in sorted(seen - mine):
            p, chain = pid, set()
            while p and p not in (1, me) and p not in chain:
                chain.add(p)
                p = parent(p)
            if p == me:                                  # the hook's own child
                continue
            if p is None and parent(pid) is None:        # the candidate itself exited
                continue
            left.append(pid)
        now = unit_pids(unit)
        if now <= seen:
            return left
        seen |= now
    raise Hold(f'the {unit} cgroup kept gaining processes while it was scanned')


def describe(pids):
    out = []
    for pid in pids:
        try:
            cmd = (PROC / str(pid) / 'cmdline').read_bytes().replace(b'\0', b' ').decode(errors='replace').strip()
        except OSError:
            cmd = '?'
        out.append(f'{pid} {cmd}')
    return '; '.join(out)


def held():
    try:
        return MARKER.read_text().strip()
    except FileNotFoundError:
        return None


def start(unit):
    prev = held()
    left = survivors(unit)
    if left:
        raise Hold(f'processes left from an earlier job: {describe(left)}')
    me = job_id()
    if prev is not None and not prev.endswith(DONE):
        print(f'HIL farm: job {prev} never reached its completed hook; nothing of it runs, so this job proceeds '
              "(the next task on each of its boards logs what it left: hil_lock.py status)", file=sys.stderr)
    MARKER.write_text(me + '\n')


def complete(_unit):        # the unit only keeps main's dispatch uniform
    me = job_id()
    if held() == me:        # a refused job never wrote its id, so it leaves another job's hold alone
        MARKER.write_text(me + DONE + '\n')


def main(argv):
    if len(argv) != 2 or argv[0] not in ('start', 'complete'):
        print('usage: job_hook.py start|complete UNIT', file=sys.stderr)
        return 2
    try:
        (start if argv[0] == 'start' else complete)(argv[1])
    except (Hold, OSError) as e:
        print(f'HIL farm held: {e}. An operator checks the named processes and the boards, then ends those '
              'processes; the next job starts once none is left.', file=sys.stderr)
        return 1
    return 0


if __name__ == '__main__':
    sys.exit(main(sys.argv[1:]))
