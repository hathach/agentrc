"""Tests for hooks/headless-chief/chief_guard.py, run as Claude Code runs it: hook JSON on stdin."""
import contextlib
import importlib.util
import io
import json
import os
import shutil
import subprocess
import sys
import time
import unittest
from pathlib import Path
from unittest import mock

sys.path.insert(0, str(Path(__file__).resolve().parent))
from helpers import tmpdir  # noqa: E402

SCRIPT = Path(__file__).resolve().parents[1] / 'hooks' / 'headless-chief' / 'chief_guard.py'
# Windows judges a /tmp path as MSYS bash maps it, through cygpath; without one it is denied
needs_msys = unittest.skipIf(os.name == 'nt' and not shutil.which('cygpath'), 'no cygpath to map /tmp')


class ChiefGuard(unittest.TestCase):
    def setUp(self):
        self.root = tmpdir(self)
        self.project = self.root / 'wt'
        self.wt = self.project.as_posix()   # as a bash command names it: an unquoted backslash is an escape
        (self.project / 'src').mkdir(parents=True)
        (self.project / 'src' / 'a.c').write_text('int a;\n')
        # GIT_* from a calling hook would point git at another repository
        self.env = {k: v for k, v in os.environ.items() if k != 'CLAUDE_PROJECT_DIR' and not k.startswith('GIT_')}
        subprocess.run(['git', 'init', '-q', str(self.project)], check=True, env=self.env)
        self.guard = self.module()

    def event(self, command=None, tool='Bash', agent_type='chief', agent_id=None, file_path=None):
        event = {'hook_event_name': 'PreToolUse', 'tool_name': tool, 'cwd': str(self.project), 'agent_type': agent_type,
                 'tool_input': {'command': command} if tool == 'Bash' else {'file_path': file_path, 'content': 'x'}}
        if agent_id:
            event['agent_id'] = agent_id
        return event

    def hook(self, command=None, raw=None, **kw):
        return subprocess.run([sys.executable, str(SCRIPT)], input=raw or json.dumps(self.event(command, **kw), ensure_ascii=False),
                              env=self.env, capture_output=True, encoding='utf-8', timeout=30)

    def module(self):
        """The guard loaded in this process, its cygpath cache and budget fresh."""
        spec = importlib.util.spec_from_file_location('chief_guard_under_test', SCRIPT)
        guard = importlib.util.module_from_spec(spec)
        spec.loader.exec_module(guard)
        return guard

    def main(self, guard, command=None, **kw):
        """(exit code, stderr) of guard.main on this event, in this process."""
        err = io.StringIO()
        with contextlib.redirect_stderr(err), mock.patch.dict(os.environ, self.env, clear=True):
            return guard.main(self.event(command, **kw)), err.getvalue()

    def check(self, command=None, **kw):
        """The test's guard run in this process (the agent filter runs only as a hook), its deadline fresh."""
        self.guard.DEADLINE = time.monotonic() + 6
        return self.main(self.guard, command, **kw)

    def assert_denied(self, command, *named, **kw):
        code, err = self.check(command, **kw)
        self.assertEqual(code, 2, f'{command!r} passed')
        for want in named:
            self.assertIn(want, err)

    def assert_allowed(self, command, **kw):
        code, err = self.check(command, **kw)
        self.assertEqual(code, 0, f'{command!r}: {err}')

    def test_only_chief_own_calls_are_checked(self):
        # run as Claude Code runs the hook: the agent filter, the exit code and the reason on stderr
        for kw in ({'agent_type': 'code-writer', 'agent_id': 'a1'}, {'agent_type': None},
                   # a subagent chief dispatched carries an agent_id even under chief's agent_type
                   {'agent_type': 'chief', 'agent_id': 'a2'}):
            r = self.hook('git push', **kw)
            self.assertEqual((r.returncode, r.stderr), (0, ''), kw)
        r = self.hook('git push')
        self.assertEqual(r.returncode, 2)
        self.assertRegex(r.stderr, r'^chief guard: `git push` changes the repository')
        r = self.hook(None, tool='Write', file_path=str(self.project / 'src' / 'new.c'))
        self.assertEqual(r.returncode, 2)
        self.assertIn('literal absolute path', r.stderr)
        self.assertEqual(self.hook('git status').returncode, 0)

    def test_git_reads_pass(self):
        for c in ('git status --porcelain', 'git log --oneline abc..HEAD', 'git -C /x log -1', 'git -c core.pager=cat diff',
                  'git --no-pager show HEAD', 'git log --name-only --no-renames --format= a..HEAD', 'git fetch -q origin',
                  'git branch --show-current', 'git branch -a --contains abc', 'git tag -l v*', 'git stash list',
                  'git worktree list', 'git remote -v', 'git config --get user.name', 'git rev-parse HEAD',
                  'git symbolic-ref HEAD', 'git remote'):
            self.assert_allowed(c)

    def test_git_writes_are_denied(self):
        for c in ('git push origin HEAD', 'git commit -m x', 'git -C /x commit -am y', 'git reset --hard',
                  'git rebase main', 'git checkout -b x', 'git switch main', 'git add -- src', 'git stash',
                  'git stash push -m x', 'git branch new', 'git branch -D old', 'git tag v1', 'git tag -d v1',
                  'git worktree remove x', 'git remote add o u', 'git config user.name x', 'git cherry-pick abc',
                  'git restore src/a.c', 'git clean -fd', 'git merge x', 'git pull', 'git symbolic-ref HEAD refs/heads/x'):
            self.assert_denied(c, 'changes the repository')

    def test_wrappers_chains_and_substitutions_are_seen(self):
        for c in ('GIT_DIR=x git push', 'env -u X git push', 'timeout 60 git push', 'nice -n 5 git push',
                  'git status && git push', 'git log | head; git commit -m x', 'true || git push', '(cd /tmp && git push)',
                  'echo "$(git push)"', 'echo `git push`', "bash -c 'git push'", 'sh -c "git log && git push"',
                  'if git push; then echo y; fi', 'xargs -n1 git push', '/usr/bin/git push', "eval 'git push'",
                  'command -- git push', 'exec -a x git push', 'git status # look\ngit push origin HEAD',
                  "# don't edit\nprintf x > src/a.c", "# it's fine\ngit status && git push origin HEAD",
                  "printf '%s\\n' https://github.com/o/r/issues/1#c2 > src/a.c", 'echo a#b; git push'):
            self.assert_denied(c)

    @needs_msys
    def test_quoted_words_are_data(self):
        self.assert_allowed("git log --grep='git push' --oneline")
        self.assert_allowed('echo "git commit"')
        self.assert_allowed("rg '>' src/a.c")
        self.assert_allowed('command -v git python3 node')
        self.assert_allowed('command -p -v git python3 node')
        self.assert_allowed("cat > /tmp/r.md <<'EOF'\n# it's a title\nEOF")
        self.assert_allowed('grep -c "a;b|c" src/a.c && echo \\> done')

    @needs_msys
    def test_heredoc_body_is_data_but_its_line_counts(self):
        self.assert_allowed("cat > /tmp/brief.md <<'EOF'\ngit push\ngh pr merge 1\nEOF")
        self.assert_denied("cat > src/x.md <<'EOF'\nhello\nEOF", 'literal absolute path')
        self.assert_denied("cat <<EOF | tee src/x\nhi\nEOF", 'literal absolute path')

    def test_gh_reads_pass(self):
        for c in ('gh pr view 12 --json state', 'gh pr checks 12', 'gh -R o/r pr list', 'gh issue view 3',
                  'gh run view 99 --log-failed', 'gh api repos/o/r/pulls/1', 'gh api -X GET search/issues -f q=x',
                  "gh api graphql -f query='query { viewer { login } }'", 'gh search issues foo', 'gh auth status'):
            self.assert_allowed(c)

    def test_gh_publishing_is_denied(self):
        for c in ('gh pr create -t x', 'gh pr merge 1', 'gh pr comment 1 -b x', 'gh pr review 1 --approve',
                  'gh issue create -t x', 'gh issue comment 3 -b y', 'gh issue close 3', 'gh pr edit 1',
                  'gh api -X POST repos/o/r/issues', 'gh api --method=PATCH repos/o/r/pulls/1', 'gh api repos/o/r/issues -f title=x',
                  "gh api graphql -f query='mutation { resolveReviewThread }'", 'gh release create v1', 'gh repo delete o/r'):
            self.assert_denied(c)

    def test_workflow_only_publishers_are_denied(self):
        self.assert_denied('python3 ~/.claude/skills/pr-babysit/scripts/push.py --pr 1', 'inside its workflow')
        self.assert_denied('python3 -X utf8 /x/sonar.py --pr 1', 'inside its workflow')
        self.assert_allowed('python3 ~/.claude/skills/pr-babysit/scripts/launch_result.py --output /tmp/o')

    def test_writes_inside_the_worktree_are_denied(self):
        for c in (f'echo x > {self.wt}/a', f'rm {self.wt}/src/a.c', f'mv /tmp/a {self.wt}/src/',
                  f'cp /tmp/a {self.wt}/src/b.c', f'printf x | tee {self.wt}/src/a.c',
                  f'git diff --output={self.wt}/p.patch', f'chmod --reference=/bin/sh {self.wt}/src/a.c'):
            self.assert_denied(c, 'literal absolute path')
        self.assert_denied(None, 'literal absolute path', tool='Write', file_path=str(self.project / 'src' / 'new.c'))

    def test_a_write_target_must_be_a_literal_absolute_path(self):
        # no shell state is tracked: after any cd, a relative or variable target could be the worktree
        for c in ('echo x > src/a.c', 'cd /tmp && printf done > chief-report.md', 'out=/tmp/r.md; printf done > "$out"',
                  'printf done > "$(git rev-parse --show-toplevel)/r.md"', 'mkdir -p "$(git rev-parse --git-path chief)"',
                  'touch new.c', 'git diff --output=chief-report.patch', 'cp -t . /tmp/brief.md', 'rsync -t /tmp/brief.md .',
                  'cp -rt . /tmp/brief.md', 'cp --target-dir . /tmp/brief.md', 'cp -t. /tmp/brief.md',
                  'cp --t . /tmp/brief.md', 'cp --t=. /tmp/brief.md',
                  'chmod 600 src/a.c', 'echo x > ~/chief-note.md', 'echo x > "$HOME/chief-note.md"'):
            self.assert_denied(c, 'literal absolute path')

    @needs_msys
    def test_writes_outside_the_worktree_pass(self):
        for c in ('echo x > /tmp/out.md', 'git log > /tmp/log 2>&1', 'git status 2>/dev/null', 'cp src/a.c /tmp/a.c',
                  'mkdir -p /tmp/chief-run', 'rm -f /tmp/x', "sed 's/a/b/' src/a.c", 'cat src/a.c >&2',
                  'echo "$(date)" > /tmp/stamp', "echo 'a > b' > /tmp/x.md",
                  'chmod 600 /tmp/chief-report.md', 'cp -t /tmp src/a.c', 'rsync -t src/a.c /tmp/chief-a.c',
                  'git -C /tmp diff --output=/tmp/p.patch', 'chmod --reference=/bin/sh /tmp/chief-report.md'):
            self.assert_allowed(c)
        self.assert_allowed(None, tool='Write', file_path='/tmp/report.md')

    def test_in_place_edits_are_denied(self):
        # chief writes its artifacts whole with Write; a worktree edit goes to a writer
        for c in ("sed -i 's/a/b/' /tmp/x.md", "sed -Ei 's/a/b/' /tmp/x.md", "sed --in-place=.bak 's/a/b/' /tmp/x.md",
                  "perl -pi -e 's/a/b/' /tmp/x.md", "cd hooks && sed -i 's/old/new/' chief_guard.py"):
            self.assert_denied(c, 'in place')

    def test_home_and_tmpdir_are_not_expanded(self):
        for c in (f'TMPDIR={self.wt}; printf done > "$TMPDIR/r.md"', f'HOME={self.wt} && printf done > "$HOME/r.md"',
                  f'export HOME={self.wt}; printf done > ~/r.md', f"bash -c 'TMPDIR={self.wt}; printf x > $TMPDIR/r'"):
            self.assert_denied(c, 'literal absolute path')

    def test_chief_directory_inside_a_primary_checkout_is_writable(self):
        chief = self.project / '.git' / 'chief'
        self.assert_allowed(f'mkdir -p {chief.as_posix()} && git log > {chief.as_posix()}/u1-commit-check.txt')
        self.assert_allowed(None, tool='Write', file_path=str(chief / 'brief.md'))
        self.assert_denied(f'echo x > {self.wt}/.git/config', 'literal absolute path')

    def test_combined_shell_flags_and_xargs_values(self):
        self.assert_denied("bash -lc 'git push origin HEAD'", 'changes the repository')
        self.assert_denied('xargs -n 1 git push', 'run time')
        self.assert_denied('xargs -I {} git push origin {}', 'run time')
        for cmd in ('echo /usr/bin/git | xargs -I X X push', "xargs -I '{}' '{}' push", 'xargs -IX /x/X push',
                    'xargs -0 -I X env X push'):
            self.assert_denied(cmd, 'command word')
        self.assert_allowed('xargs -I X grep -n pattern X')
        self.assert_denied("printf 'input\\n' | xargs -a /tmp/programs -I X xargs -I Y X push", 'xargs under xargs')

    def test_attached_gh_fields_imply_post(self):
        self.assert_denied('gh api repos/o/r/issues -ftitle=x', 'non-GET')
        self.assert_denied('gh api repos/o/r/issues -Ftitle=x', 'non-GET')

    @needs_msys
    def test_unquoted_heredoc_substitutions_run(self):
        self.assert_denied("cat > /tmp/r.md <<EOF\n$(git push origin HEAD)\nEOF", 'changes the repository')
        self.assert_denied("cat > /tmp/r.md <<EOF\n'`git push`'\nEOF", 'changes the repository')
        self.assert_allowed("cat > /tmp/r.md <<'EOF'\n$(git push origin HEAD)\nEOF")

    @needs_msys
    def test_single_quoted_substitutions_are_data(self):
        self.assert_allowed("printf '%s\\n' '$(git push)' > /tmp/chief-report.md")
        self.assert_allowed("grep -n '`git push`' /tmp/notes.md")

    def test_git_reads_keep_their_own_short_options(self):
        self.assert_allowed('git ls-files -o --exclude-standard')
        self.assert_allowed('git grep -o chief -- src')

    def test_an_unparsable_command_is_denied(self):
        self.assert_denied('echo "unterminated', 'cannot parse')

    def test_dashed_git_words_and_scalar_are_denied(self):
        # a dashed builtin runs the subcommand without the `git` word; judge it as `git <sub>`
        for c in ('/usr/lib/git-core/git-push origin', 'git-push origin', 'git-commit -m x', 'git-lfs push',
                  'git-upload-pack .'):
            self.assert_denied(c, 'changes the repository')
        for c in ('scalar clone https://x', 'scalar register', 'scalar run commit-graph'):
            self.assert_denied(c, 'scalar')
        for c in ('git-log --oneline', 'git-status --porcelain', 'git-diff HEAD'):
            self.assert_allowed(c)

    def test_a_runtime_command_word_or_git_subcommand_is_denied(self):
        for c in ('$SHELL -c "git push"', '$(which git) push', '"$COMSPEC" //c git push', '${GIT} push',
                  'git${IFS}push', 'git $sub', 'git ${verb} origin', 'git $(echo push) origin'):
            self.assert_denied(c)
        # a glob or brace in the command word names a program only at run time; a quoted one is a literal name
        for c in ('/usr/bin/g?t push', '/usr/bin/gi* push', '/usr/bin/[g]it push', '{git,x} push', '/usr/bin/e?v git push'):
            self.assert_denied(c, 'cannot tell what runs')
        self.assert_allowed("'/tmp/a*b' --version")
        # a wrapper resolved by path still reaches the command it runs
        self.assert_denied('/usr/bin/env git push', 'changes the repository')

    def test_wrapper_options_pass_only_when_every_letter_is_known(self):
        for c in ('env -S "git push"', 'env -iS "git push"', 'env --split-string="git push"', 'env -Sgit\\ push',
                  'env -iX git push', 'timeout --frobnicate 5 git log', 'nice --bogus git log', 'env --null=x git log',
                  'xargs -0Z cat', 'xargs -i git push {}', 'xargs --replace git push {}', f'time -o {self.wt}/t git log'):
            self.assert_denied(c, 'option')
        for c in ('timeout 60 git log', 'nice -n 5 git status', 'nice -n5 git status', 'env -u FOO git log',
                  'env -uFOO git log', 'env -iuFOO git log', 'env -i -u FOO git log', 'stdbuf -oL git status',
                  'timeout --kill-after=5 10 git log', 'env - git log', 'nohup git log', 'xargs -0r -n1 cat'):
            self.assert_allowed(c)

    def test_git_gh_shells_and_writers_under_xargs_or_find_exec_are_denied(self):
        # their arguments are only known at run time: `--output=<wt>/x` read from stdin made `git log` write
        for c in (f"printf -- '--output={self.wt}/x\\n' | xargs git log", 'xargs git log', 'echo push | xargs git',
                  'xargs gh pr view', 'xargs sh -c x', 'xargs bash', 'xargs rm', 'xargs cp -t /tmp', 'xargs python3',
                  'xargs env git status', 'xargs sed -n p', 'xargs git-log', 'xargs -I {} curl -o {} https://x',
                  r'find / -maxdepth 0 -exec git push \;', r'find /tmp -exec git log {} \;', r'find /tmp -execdir sh -c x \;',
                  r'find /tmp -ok rm {} \;', r'find /tmp -okdir git status \;', r'find /tmp -exec tee /tmp/x \;',
                  r'find /tmp -exec xargs git log \;', "printf '/tmp/a /tmp/b\\n' | xargs link", 'xargs ln',
                  r'find /tmp -exec link {} /tmp/h \;', r'find /tmp -exec cp -l {} /tmp/h \;',
                  # the found path itself as the command, quoted or not
                  r'find /usr/bin/git -maxdepth 0 -exec {} push \;', r"find /usr/bin/git -exec '{}' push \;",
                  r'find /tmp -execdir ./{} \;', r'find /tmp -ok x{}y \;',
                  # behind a wrapper, at any depth
                  r"find /usr/bin/git -maxdepth 0 -exec /usr/bin/env '{}' push \;",
                  r"find /usr/bin/git -exec env nice -n 5 timeout 9 '{}' push \;",
                  # a wrapper with no program runs the words xargs reads
                  "printf 'git push\\n' | xargs env", 'xargs nohup', 'xargs timeout 5', 'xargs -0 env -i'):
            self.assert_denied(c, 'run time')
        for c in ('echo /tmp/a | xargs cat', 'xargs grep -l foo', "find /tmp -name '*.c' -exec grep -l x {} +",
                  r'find /tmp -exec wc -l {} \;', 'find /tmp -name x -type f', 'xargs', 'echo a | xargs -n1'):
            self.assert_allowed(c)

    def test_shell_options_outside_the_known_set_and_stdin_are_denied(self):
        for c in ('bash', 'sh', 'echo git push | bash', 'echo git push | sh', 'bash -',
                  'bash -- -', 'bash <<< "git push"', 'cat script | bash -l', "printf 'git push\\n' | bash /dev/stdin",
                  'bash /dev/fd/0', 'sh /dev/fd/0', 'sh //dev/./stdin', 'bash -- /proc/self/fd/0', 'sudo -ns', 'sudo -i',
                  'sudo -n -s'):
            self.assert_denied(c, 'stdin')
        for c in ('bash -s', 'bash -s arg', 'bash -O expand_aliases', 'bash -O expand_aliases /tmp/s.sh', 'bash -i', 'bash +x /tmp/s.sh',
                  'bash --rcfile /tmp/r /tmp/s.sh', 'bash -o vi -c "git log"'):
            self.assert_denied(c, 'shell option')
        self.assert_denied('sh -e -c -x "git push"', 'changes the repository')
        # a -c string is checked (a read passes), and a script operand is not followed
        for c in ('bash -c "git log"', 'bash -lc "git log"', 'bash -e -c "git status"', 'bash /tmp/s.sh arg',
                  'sh /tmp/s.sh', 'bash -- /tmp/s.sh', 'bash --norc /tmp/s.sh',
                  'sh -ec -x "git log"'):
            self.assert_allowed(c)

    def test_aliases_are_denied(self):
        for c in ('shopt -s expand_aliases', 'shopt -qs expand_aliases', 'shopt -s -q expand_aliases',
                  'shopt expand_aliases && git log', "bash -c 'shopt -s expand_aliases'", "alias g='git push'",
                  "BASHOPTS=expand_aliases bash -c 'g'"):
            self.assert_denied(c)
        for c in ('shopt -s globstar', 'alias'):
            self.assert_allowed(c)

    def test_git_configuration_that_runs_a_command_is_denied(self):
        for c in ('git -c core.fsmonitor="git push" status', 'git -c core.sshCommand="sh -c x" fetch',
                  "git -c credential.helper='!x' status", 'git -ccore.pager=less log', 'git -c a=b log',
                  'git --config-env=core.pager=X status', 'git --config-env core.sshCommand=Y fetch',
                  'git --exec-path=/tmp status', 'GIT_EXEC_PATH=/tmp git status',
                  'GIT_CONFIG_COUNT=1 GIT_CONFIG_KEY_0=core.fsmonitor GIT_CONFIG_VALUE_0=x git status',
                  "GIT_CONFIG_PARAMETERS=\"'core.fsmonitor'='x'\" git status", 'GIT_CONFIG_GLOBAL=/tmp/c git status',
                  'GIT_CONFIG_SYSTEM=/tmp/c git log', 'env GIT_CONFIG_COUNT=1 git status'):
            self.assert_denied(c)
        # git appends trace output to the file, socket or fd a GIT_TRACE* value names, with no redirect to judge
        for c in (f'GIT_TRACE={self.wt}/src/a.c git status', f'export GIT_TRACE2_EVENT={self.wt}/x; git log',
                  'GIT_TRACE2=af_unix:/tmp/s git log', 'env GIT_TRACE_PACKET=/tmp/t git status', 'GIT_TRACE=3 git log',
                  'GIT_TRACE+=1 git status'):
            self.assert_denied(c, 'trace output')
        for c in ('GIT_TRACE=1 git status', 'GIT_TRACE=true git log', 'GIT_TRACE2_PERF=2 git status',
                  'GIT_TRACE=0 git log', 'export GIT_TRACE=False'):
            self.assert_allowed(c)
        for c in ('git -c core.pager=cat diff', 'git -c color.ui=never log', 'git -c core.quotepath=off status',
                  'git -ccolor.ui=always log', 'git -c core.quotepath=false -c color.ui=auto status'):
            self.assert_allowed(c)
        # only a destination is denied: the other GIT_TRACE2_* are settings (git's trace2/tr2_sysenv.c), and GIT_TRACE_REDACT
        # and GIT_TRACE_CURL_NO_DATA booleans; GIT_TRACE_BARE is a trace key git opens like any other
        for c in ('GIT_TRACE2_EVENT=1 GIT_TRACE2_EVENT_NESTING=10 git status --porcelain', 'GIT_TRACE2_EVENT_NESTING=10 git status',
                  'GIT_TRACE2_CONFIG_PARAMS=core.bare git status', 'GIT_TRACE2_ENV_VARS=LANG git status',
                  'GIT_TRACE2_MAX_FILES=5 git status', 'GIT_TRACE2_PERF_BRIEF=yes git status', 'GIT_TRACE2_DST_DEBUG=yes git log',
                  'export GIT_TRACE2_ENV_VARS=LANG,HOME', 'env GIT_TRACE2_BRIEF=yes git log', 'GIT_TRACE_REDACT=no git fetch',
                  'GIT_TRACE_CURL_NO_DATA=yes git fetch'):
            self.assert_allowed(c)
        for c in ('GIT_TRACE2_PERF=/tmp/p git status', 'GIT_TRACE_BARE=/tmp/b git status', 'GIT_TRACE_CURL=/tmp/c git fetch'):
            self.assert_denied(c, 'trace output')
        code, err = self.check('GIT_TRACE2_EVENT_NESTING=10 GIT_TRACE2_EVENT=/tmp/e git status')
        self.assertEqual(code, 2)
        self.assertIn('`GIT_TRACE2_EVENT=/tmp/e`', err)
        self.assertNotIn('NESTING', err)

    def test_windows_reads_env_names_without_case(self):
        # Git for Windows reads its environment through GetEnvironmentVariableW, which ignores case
        cases = (('git_trace=C:/repo/src/a.c git status', 'trace output'), ('env Git_Trace2_Event=C:/repo/x git status', 'trace output'),
                 ('export git_trace=C:/repo/x; git status', 'trace output'),
                 ('declare -x git_trace2_perf=C:/repo/x; git status', 'trace output'), ('git_pager=less git log', 'pager'),
                 ('Git_Ssh_Command=x git fetch', 'cannot vet'), ('git_config_parameters=x git status', 'cannot vet'))
        with mock.patch.object(self.guard.os, 'name', 'nt'):
            for c, why in cases:
                self.assert_denied(c, why)
            self.assert_allowed('git_trace2_event_nesting=10 git status')
        if os.name != 'nt':   # elsewhere git reads the name as written
            for c, _ in cases:
                self.assert_allowed(c)

    def test_env_assignments_that_run_a_command_are_denied(self):
        for c in ('GIT_SSH_COMMAND="ssh -i k" git fetch', 'GIT_PAGER=less git log', 'PAGER=more git log',
                  'GIT_EXTERNAL_DIFF=x git diff', 'env GIT_SSH_COMMAND=x git fetch', 'GIT_EDITOR=vim git log'):
            self.assert_denied(c)
        for c in ('GIT_PAGER=cat git log', 'PAGER=cat git log', 'env GIT_PAGER=cat git log', 'GIT_DIR=/tmp/r git log'):
            self.assert_allowed(c)

    def test_append_assignments_are_assignments(self):
        # bash's `NAME+=value` prefixes a command as `NAME=value` does: read as a program word, it hid the command
        self.assert_denied(f'REVIEW_VAR+=x rm {self.wt}/src/a.c', 'literal absolute path')
        for c in ('X+=1 git push', 'env X+=1 git push', 'X+=1 Y=2 git push'):
            self.assert_denied(c, 'changes the repository')
        # an append extends a pager this guard cannot see, even with cat
        for c in ('GIT_PAGER+=less git log', 'PAGER+=cat git log', 'export GIT_PAGER+=cat'):
            self.assert_denied(c, 'pager')
        for c in ('GIT_SSH_COMMAND+=x git fetch', 'GIT_CONFIG_COUNT+=1 git status', 'export TAR_OPTIONS+=x'):
            self.assert_denied(c, 'cannot vet')
        for c in ('X+=1 git status', 'export PATH+=:/tmp/bin', 'X+=1'):
            self.assert_allowed(c)

    def test_env_reads_every_word_with_an_equals_sign_as_an_assignment(self):
        # GNU env sets any `x=y` word before its program, a name bash would refuse included, past `--` too
        for c in ('env a.b=c git push', 'env -i a.b=c git push', 'env -u FOO a.b=c git push', 'env -- a.b=c git push',
                  'env 1a=x git push', 'env a-b=c git push', 'nice env a.b=c git push', 'env -- -x=y git push'):
            self.assert_denied(c, 'changes the repository')
        self.assert_denied(f'env a.b=c rm {self.wt}/src/a.c', 'literal absolute path')
        self.assert_denied('env -- GIT_PAGER=less git log', 'pager')
        # env sets a variable named `GIT_PAGER+`, which leaves the pager alone
        for c in ('env GIT_PAGER+=less git log', 'env a.b=c git log', 'env -- a=b git status'):
            self.assert_allowed(c)

    def test_sudo_reads_words_with_an_equals_sign_before_its_command_as_assignments(self):
        # sudo sets `x=y` words before `--` that do not start with `/`, then reads its options again
        for c in ('sudo a.b=c git push', 'sudo -n a.b=c git push', 'sudo A=1 -u root git push', 'sudo a-b=c git push'):
            self.assert_denied(c, 'changes the repository')
        self.assert_denied(f'sudo a.b=c rm {self.wt}/src/a.c', 'literal absolute path')
        self.assert_denied('sudo GIT_PAGER=less git log', 'pager')
        # past `--`, or from `/`, sudo runs the word itself; like env it sets a variable named `GIT_PAGER+`
        for c in ('sudo -- a.b=c git push', 'sudo /tmp/a=b git push', 'sudo GIT_PAGER+=less git log', 'sudo a.b=c git log'):
            self.assert_allowed(c)

    def test_a_wrapper_word_expanded_at_run_time_is_denied(self):
        # bash splits an unquoted expansion: one option value, duration or assignment may hold the program
        for c in (f"X='FOO rm {self.wt}/src/a.c'; env -u $X true", f"X='5 rm {self.wt}/src/a.c'; timeout $X true",
                  f"X='b rm {self.wt}/src/a.c'; env a=$X true", "X='5 git push'; timeout $X",
                  "O='-s KILL'; timeout $O 30 git push", 'nice -n $N git log', 'timeout --kill-after=$K 5 git log',
                  'sudo -u $U git log', 'sudo A=$X git log', 'xargs -n $(nproc) cat', 'env -u "$X" git log'):
            self.assert_denied(c, 'expanded at run time')
        # bash does not split an assignment's value, and the program's own arguments are its own
        for c in ('GIT_DIR=$HOME/r git log', 'timeout 5 echo $X', 'env -u FOO cat "$F"', 'nice -n 5 git log -- $P'):
            self.assert_allowed(c)

    def test_a_wrapper_word_bash_expands_into_more_words_is_denied(self):
        # an unquoted brace or glob in a word a wrapper strips becomes words bash hands it: the program among them
        for c in ('timeout {5,git,push}', f'env -u {{A,rm,{self.wt}/src/a.c}} true', f'nice -n {{5,rm,{self.wt}/src/a.c}}',
                  f'cd {self.wt} && timeout * true', 'timeout {5,{git,push}}', 'env -u{A,B} git log', 'env A={x,y} git log',
                  'sudo -u {a,b} git log', 'timeout -s {KILL,TERM} 5 git log', 'timeout --kill-after={1..3} 5 git log',
                  'nice -n [5] git log', 'timeout 5? git log', 'xargs -n {1,2} cat',
                  # behind another wrapper
                  'nice -n 5 timeout {5,git,push}', 'env -i nice -n {1..3} git log', 'echo | xargs env nice -n {1..3} cat'):
            self.assert_denied(c, 'expanded at run time')
        # a quoted brace or glob is literal, a bare `{}` is no expansion, and the program's own arguments are its own
        for c in ("timeout '{5,git,push}' true", "env -u 'A*' git log", 'env -u "{A,B}" git log', 'nice -n \\{5,git\\} true',
                  'echo a | xargs -I {} grep -l x {}', 'xargs -I{} cat {}', r'find /tmp -exec timeout 5 cat {} \;',
                  'timeout 5 cat /tmp/*.c {a,b}', 'env A={} git log'):
            self.assert_allowed(c)

    def test_a_git_or_gh_brace_list_before_the_double_dash_is_denied(self):
        # #190: bash expands each into words, an option among them
        for c in (f'git log {{--output={self.wt}/src/a.c,-1}}', r'git fetch {--upload-pack=touch\ /tmp/pwn,.}',
                  'gh api {-XPOST,repos/o/r/issues}', 'gh api repos/o/r/issues {-XPOST,-q.}', 'git {-C,/tmp} log',
                  'git log x{a,b}y', 'git log {1..3}', 'timeout 5 git log {--output=/tmp/o,-1}',
                  'git-log {--output=/tmp/o,-1}', 'git log {-p,-1} -- src/{a,b}.c',
                  # a `--` an option may take as its value ends nothing
                  'git grep -e -- {--open-files-in-pager=echo,-n} -- README.md', 'gh api -q -- {-XPOST,repos/o/r/issues}',
                  'git log -1 -- src/{a,b}.c'):
            self.assert_denied(c, 'brace list')
        # quoted or escaped is literal, a bare `{}` is no expansion, and past `--` a pathspec is git's own
        for c in ("git log '{--output=x,-1}'", 'git log "{a,b}"', r'git log \{a,b\}', 'git log --format={}',
                  'git log -- src/{a,b}.c', 'git log --format=%H -- src/{a,b}.c', "gh api repos/o/r/pulls -q '.[]|{a,b}'",
                  'gh pr list -- {a,b}'):
            self.assert_allowed(c)

    def test_a_brace_list_given_to_a_command_whose_words_the_guard_reads_is_denied(self):
        # #199: each hides what its expanded form shows, past `--` too; the expanded form must stay denied
        for c, expanded in (('sed {-i,s/a/b/,src/a.c}', 'sed -i s/a/b/ src/a.c'),
                            ('tar tf {/tmp/a.tar,--checkpoint=1,--checkpoint-action=exec=echo}',
                             'tar tf /tmp/a.tar --checkpoint=1 --checkpoint-action=exec=echo'),
                            ('perl {-pi,-e,s/a/b/,src/a.c}', 'perl -pi -e s/a/b/ src/a.c'),
                            ('curl {-K,/tmp/cfg,https://x}', 'curl -K /tmp/cfg https://x'),
                            ('find {/tmp,-delete}', 'find /tmp -delete'), ('find -- {/tmp,-delete}', 'find -- /tmp -delete'),
                            (f'curl {{-o,{self.wt}/src/a}} https://h/', f'curl -o {self.wt}/src/a https://h/'),
                            (r'find . {-exec,rm} {} \;', r'find . -exec rm {} \;'),
                            ("bash {-c,'git push'}", "bash -c 'git push'"),
                            (f'dd {{of={self.wt}/src/a,if=/tmp/in}}', f'dd of={self.wt}/src/a if=/tmp/in'),
                            (f'chmod {{--reference={self.wt}/src/a,{self.wt}/src/b}}',
                             f'chmod --reference={self.wt}/src/a {self.wt}/src/b'),
                            ('export {GIT_PAGER=less,X=1}', 'export GIT_PAGER=less X=1'),
                            ('shopt -s {expand_aliases,x}', 'shopt -s expand_aliases x'),
                            ('python3 {push.py,x}', 'python3 push.py x'),
                            ('timeout 5 sed {-i,x}', 'timeout 5 sed -i x'),
                            ('env A=1 find {/tmp,-delete}', 'env A=1 find /tmp -delete'),
                            (f'rm {{-r,{self.wt}/src}}', f'rm -r {self.wt}/src'),
                            (f'ln {{-s,/tmp/x,{self.wt}/src/y}}', f'ln -s /tmp/x {self.wt}/src/y'),
                            (f'cp /tmp/a {self.wt}/{{src,x}}/', f'cp /tmp/a {self.wt}/src/ {self.wt}/x/')):
            with self.subTest(c=c):
                self.assert_denied(c, 'brace list')
                self.assert_denied(expanded)
        # nested, a sequence, a prefix and suffix, an empty alternative, one too long to expand, inside a substitution
        for c in ('sed {{-i,-n},x}', 'sed -{i..i} x', 'sed x{-i,y}z', 'sed {,-i} x', 'sort {1..999999999}',
                  'cat <<EOF\n$(sed {-i,x})\nEOF',
                  # one unquoted separator is enough
                  r'sed {-i\,x,y}', "sed {-i',',x}", r'sed -{i.\.i,i} x'):
            self.assert_denied(c, 'brace list')
        # a brace target stays denied even when every path it expands to is outside the worktree
        self.assert_denied('rm /tmp/{a,b}', 'brace list')
        # quoted or escaped is literal, `{}` and `{a}` expand to nothing else, an unscreened command, heredoc data
        for c in ("sed -n p '{-i,x}'", 'sed -n p "{-i,x}"', r'sed -n p \{-i,x\}', 'sed -n p {}', 'sed -n p {a}',
                  r'find . -exec cat {} \;', 'xargs -I {} cat {}', 'A={a,b} git log', "cat <<'EOF'\nsed {-i,x}\nEOF",
                  'ls src/{a,b}.c',
                  # bash expands no brace whose separators are all quoted or escaped
                  r'sed -n p /tmp/{a\,b}', r'sed -n p /tmp/{a\..b}', r'sed -n p /tmp/{a.\.b}',
                  "sed -n p /tmp/{a','b}", r'rm /tmp/{a\,b}', r'rm /tmp/{a\..b}'):
            self.assert_allowed(c)
        # its path is still placed, and a quoted dot stays a dot
        self.assert_denied(rf'rm {self.wt}/src/{{a\,b}}', 'literal absolute path')
        self.assert_denied("python3 'push.py'", 'workflow')
        # a long word of `{,` with no `}` is no brace list, read in linear time
        start = time.monotonic()
        self.assert_allowed('sed -n p x' + '{,' * 3200)
        self.assertLess(time.monotonic() - start, 1)

    def test_read_options_that_run_a_program_are_denied_in_any_spelling(self):
        for c in ("git fetch --upload-pack='git push' .", 'git fetch --upload=x .', 'git ls-remote --upload-pack=x origin',
                  'git ls-remote --upload-pa=x .', 'git ls-remote --upl x .', 'git ls-remote --exec=x .',
                  'git ls-remote -u x origin', 'git ls-remote -qu x .', 'git grep --open-files-in-pager=vi chief',
                  'git grep --open-files=vi chief', 'git grep --open chief', 'git grep -O chief', 'git grep -iO chief',
                  # -e takes `--` as its pattern, so the option after it still runs
                  "git grep -e -- --open-files-in-pager='sh -c \"git push\"' -- README.md"):
            self.assert_denied(c, 'runs a program')
        for c in ('git fetch -q origin', 'git fetch --prune origin', 'git ls-remote origin', 'git ls-remote --heads origin',
                  'git grep -o chief -- src', 'git grep -n chief'):
            self.assert_allowed(c)

    def test_link_sources_must_be_literal_paths_outside_the_worktree(self):
        chief, root = (self.project / '.git' / 'chief').as_posix(), self.root.as_posix()
        # the root holding the worktree is its drive on Windows, where `/` is Git's install or the cwd's drive
        top, below = Path(self.project.anchor).as_posix(), self.project.relative_to(self.project.anchor).as_posix()
        for c in (f'ln {self.wt}/src/a.c {chief}/h', f'ln {self.wt}/src/a.c /tmp/h', f'link {self.wt}/src/a.c /tmp/h',
                  f'cp -l {self.wt}/src/a.c /tmp/h', f'cp --link {self.wt}/src/a.c /tmp/h',
                  f'rsync -a --link-dest={self.wt}/src /tmp/a/ /tmp/b/',
                  # a glob in the source expands to the worktree, which the literal word does not name
                  f'ln {root}/w?/src/a.c {chief}/h', f'link {root}/w*/src/a.c /tmp/h', f'cp -l {root}/[w]t/src/a.c /tmp/h',
                  # any spelling getopt accepts for cp's link options
                  f'cp --l {self.wt}/src/a.c /tmp/h', f'cp --li {self.wt}/src/a.c /tmp/h', f'cp --sy {self.wt}/src/a.c /tmp/h',
                  f'cp --symbolic-link {self.wt}/src/a.c /tmp/h', f'cp -as {self.wt}/src/a.c /tmp/h', f'cp -al {self.wt}/src /tmp/s',
                  # an -s that is the value of -S or --suffix leaves ln making a hard link
                  f'ln -S -s {self.wt}/src/a.c /tmp/h', f'ln -fS -s {self.wt}/src/a.c /tmp/h',
                  f'ln --suffix -s {self.wt}/src/a.c /tmp/h', f'ln --suff --symbolic {self.wt}/src/a.c /tmp/h',
                  # a long option's value is the next word: -S is --suffix's, so -l still makes a hard link
                  f'cp --suffix -S -l {self.wt}/src/a.c /tmp/hard && printf changed > /tmp/hard',
                  f'cp --su -S --link {self.wt}/src/a.c /tmp/h', f'cp --target-directory -S -l {self.wt}/src/a.c',
                  # every path is placed before the command runs: a write cannot see the symlink made before it
                  f'ln -s {self.wt}/src /tmp/link && printf changed > /tmp/link/a.c',
                  f'ln -s {self.wt}/src /tmp/l', f'ln -S~ -s {self.wt}/src /tmp/l', f'ln --suffix=x -s {self.wt}/src /tmp/l',
                  # a relative symlink names a path from the link's directory, which this does not place
                  'ln -s src /tmp/l', 'ln -s ../x /tmp/l',
                  # a directory holding the worktree reaches it through the link as the worktree itself does
                  f'ln -s {self.wt}/.. /tmp/n && printf x > /tmp/n/wt/src/a.c', f'ln -s {top} /tmp/n && printf x > /tmp/n/{below}/src/a.c',
                  f'ln -s {self.wt}/.. /tmp/n && rm /tmp/n/wt/src/a.c', f'ln -s {root} /tmp/n', f'cp -as {root} /tmp/n'):
            self.assert_denied(c, 'would link')
        # a copy and a link from outside are fine
        for c in (f'cp {self.wt}/src/a.c /tmp/h', 'ln /tmp/a /tmp/b', 'ln -s /tmp/a /tmp/b', 'cp --suffix -S /tmp/a /tmp/b',
                  'cp -l /tmp/a /tmp/b', 'rsync -a --link-dest=/tmp/prev /tmp/a/ /tmp/b/', f'ln -s {root}/x /tmp/l'):
            self.assert_allowed(c)

    def test_unquoted_globs_in_a_write_target_are_denied(self):
        for c in (f'rm {self.wt}/sr?/a.c', f'rm {self.wt}/sr*/a.c', f'rm {self.wt}/[s]rc/a.c', 'touch /tmp/x*',
                  'printf x > /tmp/{a,b}'):
            self.assert_denied(c, 'glob')
        # a quoted metacharacter is a literal path outside the worktree
        for c in ("rm '/tmp/a*.c'", "touch '/tmp/x[1].md'", "echo x > '/tmp/{a}.md'", "rm '/tmp/a?.c'"):
            self.assert_allowed(c)

    def test_dd_sort_and_refused_writers(self):
        for c in (f'dd if=/dev/zero of={self.wt}/src/a.c', f'sort -o {self.wt}/src/a.c /tmp/in',
                  f'sort --output={self.wt}/src/a.c /tmp/in',
                  # -T takes `--` as its directory, so -o after it is still the output
                  f'sort -T -- -o {self.wt}/src/a.c /tmp/x'):
            self.assert_denied(c, 'literal absolute path')
        # sort writes only through a separate -o or --output=: a cluster holding o, another spelling, a compressor
        for c in (f'sort -no{self.wt}/x /tmp/in', 'sort -no /tmp/x /tmp/in', 'sort --output /tmp/x /tmp/in',
                  f'sort --out={self.wt}/x /tmp/in', 'sort --compress-program=x /tmp/in', 'sort --compress=x /tmp/in'):
            self.assert_denied(c, 'sort')
        for c in ('wget https://x', 'wget -O /tmp/f https://x', 'patch -p1 < /tmp/p'):
            self.assert_denied(c)
        for c in ('dd if=/dev/zero of=/tmp/x bs=1M', 'sort -o /tmp/out /tmp/in', 'sort -n --output=/tmp/out /tmp/in',
                  'sort -rn /tmp/in', 'sort -k2 /tmp/in'):
            self.assert_allowed(c)

    def test_find_deleting_or_writing_primaries_are_denied(self):
        for c in ('find /tmp -delete', f"find {self.wt} -name '*.o' -delete", 'find /tmp -fprint /tmp/l',
                  'find /tmp -fprint0 /tmp/l', "find /tmp -fprintf /tmp/l '%p'", 'find /tmp -fls /tmp/l'):
            self.assert_denied(c, 'cannot place')
        self.assert_allowed('find /tmp -name x -print')

    def test_tar_passes_only_as_a_local_listing(self):
        for c in ('tar cf /tmp/a.tar src', 'tar -cf /tmp/a.tar src', 'tar xf /tmp/a.tar', 'tar -xzf /tmp/a.tgz -C /tmp',
                  'tar --extr -f /tmp/a.tar', 'tar --cre -f /tmp/a.tar src', 'tar -g /tmp/snap -cf /tmp/a.tar src',
                  'tar -tf host:/a.tar', 'tar tf /tmp/a.tar --to-command=x', 'tar -tvf /tmp/a.tar member',
                  'tar -I x -tf /tmp/a.tar', 'tar -tzf /tmp/a.tgz', 'tar'):
            self.assert_denied(c, 'listing')
        for c in ('tar tf /tmp/a.tar', 'tar -tf /tmp/a.tar', 'tar -tvf /tmp/a.tar', 'tar tvf /tmp/a.tar'):
            self.assert_allowed(c)
        # TAR_OPTIONS adds options to a listing too, --checkpoint-action=exec included
        for c in ("""TAR_OPTIONS="--checkpoint=1 --checkpoint-action='exec=git push'" tar tf /tmp/a.tar""",
                  'env TAR_OPTIONS=x tar tf /tmp/a.tar', 'export TAR_OPTIONS=x; tar tf /tmp/a.tar',
                  'declare -x TAR_OPTIONS=x'):
            self.assert_denied(c, 'TAR_OPTIONS')
        self.env['TAR_OPTIONS'] = '--checkpoint=1'
        self.assert_denied('tar tf /tmp/a.tar', 'listing')

    def test_curl_passes_only_as_a_plain_get(self):
        for c in (f'curl -o {self.wt}/src/a.c https://x', f'curl --output {self.wt}/src/a.c https://x',
                  f'curl --output={self.wt}/x https://x', f'curl -o{self.wt}/x https://x'):
            self.assert_denied(c, 'literal absolute path')
        for c in ('curl -O https://x/f', 'curl -J -O https://x/f', 'curl --remote-name https://x/f', 'curl -K /tmp/cfg https://x',
                  'curl --config /tmp/cfg', "curl -w '%{output}' https://x", 'curl --write-out x https://x',
                  f'curl --tlsv1.2 -o {self.wt}/x https://x', 'curl -fsSLo /tmp/x https://x', 'curl -d x https://x',
                  f'curl -D {self.wt}/h https://x', f'curl -c {self.wt}/j https://x', 'curl --outp /tmp/x https://x',
                  'curl -k https://x'):
            self.assert_denied(c, 'plain GET')
        for c in ('curl -X POST https://x', 'curl --request=PUT https://x'):
            self.assert_denied(c, 'publishes')
        self.assert_denied("curl -o '/tmp/#1' 'https://h/{a,b}'", 'literal path')
        for c in ('curl -fsSL -o /tmp/x https://x', 'curl -s -H "A: b" -o /tmp/x https://x', 'curl -sSfL https://x',
                  'curl --fail --silent --location --output /tmp/x https://x', 'curl -X GET https://x', 'curl -o - https://x',
                  'curl -o/tmp/x https://x', 'curl --header=A:b https://x'):
            self.assert_allowed(c)

    def test_a_non_ascii_worktree_is_still_the_worktree(self):
        # git names it in UTF-8: read in a locale's encoding (GBK) the guard crashed or missed it, and an erroring hook passes
        self.project = self.root / 'wt-ü✓'
        (self.project / 'src').mkdir(parents=True)
        subprocess.run(['git', 'init', '-q', str(self.project)], check=True, env=self.env)
        self.assert_denied(f"printf x > '{self.project.as_posix()}/src/a.c'", 'literal absolute path')
        self.assert_denied(None, 'literal absolute path', tool='Write', file_path=str(self.project / 'src' / 'ü.c'))
        self.assert_allowed(f"printf x > '{self.root.as_posix()}/ü-report.md'")

    @unittest.skipUnless(os.name == 'nt', 'Windows path forms')
    def test_windows_forms_of_a_worktree_path_are_denied(self):
        msys = '/' + self.wt[0].lower() + self.wt[2:]   # E:/x as Git Bash names it: /e/x
        native = str(self.project)
        for c in (f'echo x > {msys}/src/a.c', f'rm {msys}/src/a.c', f'cp /tmp/a {msys}/src/', f'echo x > /c/..{msys}/src/a.c',
                  f"echo x > '{native}\\src\\a.c'", f"echo x > '{self.wt.upper()}/SRC/A.C'", f"echo x > '//?/{self.wt}/src/a.c'",
                  f"echo x > '//./{self.wt}/src/a.c'", f"echo x > '{self.wt}/src/a.c:s'", "echo x > '//host/share/a.c'"):
            self.assert_denied(c, 'literal absolute path')
        for path in (f'{msys}/src/b.c', f'//?/{self.wt}/src/b.c', f'{native}\\src\\b.c'):
            self.assert_denied(None, 'literal absolute path', tool='Write', file_path=path)
        self.assert_allowed(f'echo x > /{self.wt[0].lower()}/chief-report-elsewhere.md')
        self.assert_allowed(f"echo x > '{self.root.as_posix()}/chief-report.md'")

    @unittest.skipUnless(os.name == 'nt', 'Windows has no /tmp of its own')
    def test_a_tmp_path_without_cygpath_is_denied(self):
        # bash maps /tmp through its mount table; with no cygpath to read it, the target cannot be placed
        self.env['PATH'] = os.pathsep.join(d for d in self.env['PATH'].split(os.pathsep) if d and not shutil.which('cygpath', path=d))
        self.assert_denied('echo x > /tmp/out.md', 'literal absolute path')
        self.assert_allowed(f"echo x > '{self.root.as_posix()}/out.md'")
        self.assert_allowed('tee /dev/tty; echo x > /dev/stderr')   # a device needs no mapping

    @unittest.skipUnless(os.name == 'nt', 'Windows finds a program without case or .exe')
    def test_windows_command_names_are_matched_without_case_or_suffix(self):
        for c in ('git.exe push', 'GIT push', 'Git.Exe commit -m x', '"C:/Program Files/Git/cmd/git.exe" push',
                  'env.exe git push', 'TIMEOUT.EXE 60 git push', 'GIT_DIR=E:/x/.git git push'):
            self.assert_denied(c, 'changes the repository')
        self.assert_denied('gh.exe pr merge 1', 'not a read')
        self.assert_denied('py -3 E:/x/push.py --pr 1', 'inside its workflow')
        self.assert_denied('python.exe E:/x/PUSH.PY --pr 1', 'inside its workflow')
        self.assert_allowed('git.exe status')

    @unittest.skipUnless(os.name == 'nt', 'Windows shells a bash command can start')
    def test_windows_shells_are_denied(self):
        native = str(self.project)
        for c in (f"cmd //c del '{native}\\src\\a.c'", f"cmd /k 'del {native}\\src\\a.c'", 'cmd.exe /C dir',
                  f"powershell -Command Set-Content '{native}\\src\\a.c' x", f"powershell Remove-Item '{native}\\src\\a.c'",
                  'pwsh -c "git push"', 'wsl git push', "bash -c 'cmd /c dir'", 'xargs powershell'):
            self.assert_denied(c, 'cannot follow')

    def test_any_error_denies(self):
        # Claude Code lets a hook that exits 1 pass: a payload or check it cannot handle must deny
        for raw in ('not json', json.dumps({**self.event(), 'tool_input': 'git push'}), json.dumps(['chief']),
                    json.dumps(self.event(['git', 'push']))):
            with self.subTest(raw=raw):
                r = self.hook(raw=raw)
                self.assertEqual(r.returncode, 2, r.stderr)
                self.assertIn('chief guard: the check failed', r.stderr)
                self.assertEqual(len(r.stderr.strip().splitlines()), 1, r.stderr)

    def test_a_link_suffix_or_target_directory_is_no_link_source(self):
        # the Windows link sources (every link), read here on POSIX paths: a value -S or -t takes is not a source
        sources, wt = self.guard.link_sources, self.wt
        for cmd, args in (('ln', ['-S', '.bak', '/tmp/a', '/tmp/b']), ('ln', ['-sfS', '~', '/tmp/a', '/tmp/b']),
                          ('ln', ['--suffix', '.bak', '-s', '/tmp/a', '/tmp/b']), ('cp', ['-sS', '.bak', '/tmp/a', '/tmp/b']),
                          ('ln', ['-s', '-t', '/tmp', '/tmp/a']), ('ln', ['-t', '/tmp', '-S', '.bak', '/tmp/a']),
                          ('cp', ['-s', '-t', '/tmp', '-S', '.bak', '/tmp/a'])):
            self.assertEqual(sources(cmd, args, symlinks=True), ['/tmp/a'], args)
        for cmd, args in (('ln', ['-S', '.bak', f'{wt}/src', '/tmp/b']), ('ln', ['-S', '-s', f'{wt}/src', '/tmp/b']),
                          ('ln', ['-s', '--', '-S', f'{wt}/src', '/tmp/b']), ('cp', ['-sS', '.bak', f'{wt}/src', '/tmp/b']),
                          ('ln', ['-t', '/tmp', '-S', '.bak', f'{wt}/src']), ('ln', ['-st', '/tmp', f'{wt}/src']),
                          ('ln', ['-s', '--target-dir', '/tmp', f'{wt}/src']), ('ln', ['-st/tmp', f'{wt}/src'])):
            self.assertIn(f'{wt}/src', sources(cmd, args, symlinks=True), args)

    def test_a_cygwin_or_msys_python_denies_every_call(self):
        # os.name is 'posix' there, so every Windows check would be skipped
        guard = self.module()
        for platform in ('cygwin', 'msys'):
            with mock.patch.object(sys, 'platform', platform):
                code, err = self.main(guard, 'git status')
            self.assertEqual(code, 2)
            self.assertIn(f'needs a native Windows Python; this is {platform}', err)
        self.assertEqual(self.main(guard, 'git status')[0], 0)

    @unittest.skipIf(os.name == 'nt', 'NTFS names are Unicode')
    def test_a_non_utf8_worktree_is_still_the_worktree(self):
        # git prints the path's bytes: decoded strictly as UTF-8 the guard crashed instead of placing it
        self.project = Path(os.fsdecode(os.fsencode(str(self.root)) + b'/wt-\xff'))
        (self.project / 'src').mkdir(parents=True)
        subprocess.run(['git', 'init', '-q', str(self.project)], check=True, env=self.env)
        (self.root / 'link').symlink_to(self.project)   # an ASCII name for the payload, which is UTF-8 JSON
        self.env['CLAUDE_PROJECT_DIR'] = str(self.project)
        self.project = self.root / 'link'
        self.assert_denied(None, 'literal absolute path', tool='Write', file_path=str(self.root / 'link' / 'src' / 'a.c'))
        self.assert_allowed(None, tool='Write', file_path=str(self.root / 'report.md'))

    @needs_msys
    @unittest.skipUnless(os.name == 'nt', 'MSYS devices')
    def test_msys_devices_are_outside_the_worktree_and_placed_at_once(self):
        # cygpath names /dev/tty as itself: placing it recursed until RecursionError, an exit 1 that let `git push` pass
        started = time.monotonic()
        self.assert_denied('tee /dev/tty; git push', 'changes the repository')
        self.assertLess(time.monotonic() - started, 5)
        for c in ('tee /dev/tty', 'echo x > /dev/stderr', 'git log >/dev/console', 'tee /dev/fd/2', 'cat /etc/hosts > /dev/stdout',
                  'echo x > /dev//tty', 'echo x > /dev/./null'):
            self.assert_allowed(c)
        self.assert_denied('echo x > /dev/sda', 'literal absolute path')   # cygpath names a disk device
        self.assert_denied(f'echo x > /dev/..{self.msys()}/src/a.c', 'literal absolute path')

    @needs_msys
    @unittest.skipUnless(os.name == 'nt', 'Cygwin path resolution')
    def test_links_under_dev_and_proc_and_any_dotdot_are_denied(self):
        # Cygwin follows /dev/fd and /proc/self/cwd before a later `..`: /dev/fd/../cwd is bash's working directory
        for c in ('echo hi > /dev/fd/../cwd/x', 'touch /dev/fd/../cwd/x', 'rm -rf /dev/fd/../cwd/.git', 'cp /c/tmp/a /dev/fd/../cwd/x',
                  'echo x > /proc/self/cwd/../../x', 'echo x > /proc/self/cwd/src/a.c', 'echo x > /proc/cygdrive/e/x',
                  'echo x > /proc/cpuinfo', 'echo x > /dev/shm/x', 'echo x > /dev/fd/2/x', 'echo x > /dev/../dev/tty',
                  "echo x > 'C:/tmp/l/../x'", 'echo x > /tmp/../tmp/x'):
            self.assert_denied(c, 'literal absolute path')
        self.assert_denied(None, 'literal absolute path', tool='Write', file_path='/dev/fd/../cwd/x')
        self.assert_denied('ln -s /dev/fd/../cwd/src /tmp/l', 'cannot follow')

    def msys(self):
        return '/' + self.wt[0].lower() + self.wt[2:]   # E:/x as Git Bash names it: /e/x

    @unittest.skipUnless(os.name == 'nt', 'cygpath is asked on Windows only')
    def test_cygpath_answers_are_cached_and_share_one_deadline(self):
        guard, calls, real = self.module(), [], subprocess.run

        def fast(args, **kw):
            if args[0] != 'cygpath.exe':
                return real(args, **kw)
            calls.append(kw['timeout'])
            return subprocess.CompletedProcess(args, 0, 'C:/Program Files/Git/usr/x\n', '')
        with mock.patch.object(guard, 'on_path', lambda name, exts: name + '.exe'), mock.patch.object(subprocess, 'run', fast):
            self.assertEqual(self.main(guard, 'echo a > /usr/x; echo b > /usr/x; touch /usr/x')[0], 0)
        self.assertEqual(len(calls), 1, 'one cygpath per path and run')
        self.assertLessEqual(calls[0], 6)

        def slow(args, **kw):
            if args[0] != 'cygpath.exe':
                return real(args, **kw)
            calls.append(kw['timeout'])
            time.sleep(kw['timeout'])
            raise subprocess.TimeoutExpired(args, kw['timeout'])
        guard, calls = self.module(), []
        guard.DEADLINE = time.monotonic() + 0.5
        started = time.monotonic()
        with mock.patch.object(guard, 'on_path', lambda name, exts: name + '.exe'), mock.patch.object(subprocess, 'run', slow):
            code, err = self.main(guard, 'echo a > /usr/x; echo b > /usr/y')
            self.assertEqual(code, 2)
            self.assertIn('took too long', err)
            self.assertLess(time.monotonic() - started, 2)
            self.assertEqual(len(calls), 1)
            self.assertLessEqual(calls[0], 0.5)
            guard.DEADLINE = time.monotonic()
            self.assertEqual(self.main(guard, 'echo a > /usr/z')[0], 2, 'a spent budget denies before asking')
        self.assertEqual(len(calls), 1)

    def test_one_deadline_covers_the_whole_check(self):
        # Claude Code lets a hook that outlives its 10 s timeout pass: a slow git or a long command denies first
        guard, calls, real = self.module(), [], subprocess.run

        def slow(args, **kw):
            if not os.path.basename(args[0]).startswith('git'):
                return real(args, **kw)
            calls.append(kw.get('timeout'))
            time.sleep(kw['timeout'])
            raise subprocess.TimeoutExpired(args, kw['timeout'])
        guard.DEADLINE = time.monotonic() + 0.5
        started = time.monotonic()
        with mock.patch.object(subprocess, 'run', slow):
            code, err = self.main(guard, f'echo x > {self.wt}/r.md')
        self.assertEqual(code, 2)
        self.assertIn('took too long', err)
        self.assertLess(time.monotonic() - started, 2)
        self.assertEqual(len(calls), 1)
        self.assertLessEqual(calls[0], 0.5)
        guard.DEADLINE = time.monotonic()
        code, err = self.main(guard, 'git status')
        self.assertEqual(code, 2, 'a spent deadline denies before the next segment')
        self.assertIn('took too long', err)

    @needs_msys
    @unittest.skipUnless(os.name == 'nt', 'MSYS links and mounts')
    def test_windows_links_and_mounts_are_denied(self):
        # resolve() cannot see an MSYS symlink, mount or subst drive: a link from outside into the worktree is a way in
        for c in ('mount C:/x /mnt/x', 'subst X: E:/x', 'MOUNT.EXE -c /x', f'ln -s {self.wt}/src /tmp/l',
                  f'ln {self.msys()}/src/a.c /tmp/a.c', 'ln -s src /tmp/l', f'ln -s -t /tmp {self.wt}/src',
                  f'cp -s {self.wt}/src/a.c /tmp/a.c', f'cp -al {self.wt}/src /tmp/s', f'cp --link {self.wt}/src/a.c /tmp/a.c',
                  'MSYS=winsymlinks:nativestrict ln -s /tmp/a /tmp/b', 'export MSYS=winsymlinks:native',
                  'MSYS+=winsymlinks:native ln -s /tmp/a /tmp/b', 'export CYGWIN+=winsymlinks',
                  'env CYGWIN=winsymlinks:lnk ln -s /tmp/a /tmp/b',
                  # getopt abbreviations and clusters, coreutils link, rsync's reference directories
                  f'cp --l {self.wt}/src/a.c /tmp/a.c', f'cp --lin {self.wt}/src/a.c /tmp/a.c', f'cp --sym {self.wt}/src/a.c /tmp/a.c',
                  f'cp -lS.bak {self.wt}/src/a.c /tmp/a.c', f'cp -sS~ {self.wt}/src/a.c /tmp/a.c', f'link {self.wt}/src/a.c /tmp/a.c',
                  f'link /tmp/a {self.wt}/src/b.c', f'rsync -a --link-dest={self.wt}/src /tmp/a/ /tmp/b/',
                  f'rsync -a --copy-dest {self.wt} /tmp/a/ /tmp/b/', f'rsync -a --compare-dest={self.msys()} /tmp/a/ /tmp/b/'):
            self.assert_denied(c, 'cannot follow')
        for c in ('ln -s /tmp/a /tmp/b', f'cp {self.wt}/src/a.c /tmp/a.c', 'MSYS=noglob git status', 'cp -aS.bal /tmp/a /tmp/b',
                  f'cp --sparse=always {self.wt}/src/a.c /tmp/a.c', 'link /tmp/a /tmp/b', 'rsync -a --link-dest=/tmp/prev /tmp/a/ /tmp/b/'):
            self.assert_allowed(c)

    @needs_msys
    @unittest.skipUnless(os.name == 'nt', 'Windows programs a bash command can start')
    def test_windows_launchers_wsl_and_writers_are_denied(self):
        apps = Path(os.environ.get('LOCALAPPDATA', 'C:/Users/x/AppData/Local')) / 'Microsoft' / 'WindowsApps'
        system = Path(os.environ.get('SystemRoot', 'C:/Windows'))
        for c in ('start notepad', 'START.EXE x', 'winpty git push', 'mintty -e git push', 'conhost cmd', 'git-bash -c x',
                  'git-cmd.exe', 'forfiles /c "cmd /c del x"', 'cscript x.vbs', 'wscript x.js', 'schtasks /create /tn x',
                  'mshta x.hta', 'rundll32 x.dll,y', 'runas /user:x cmd', 'explorer .',
                  f"'{system}\\System32\\bash.exe' -c 'git status'", f"'{system.as_posix().upper()}/SYSTEM32/BASH.EXE' -c x",
                  f'/{system.as_posix()[0].lower()}{system.as_posix()[2:]}/System32/bash.exe -c x', './bash -c x',
                  'ubuntu2204.exe run git push', 'debian run x', 'kali x', 'opensuse-leap-15.5 run x', 'wslconfig /l',
                  f"'{apps.as_posix()}/MicrosoftCorporationII.WindowsSubsystemForLinux.exe' x",
                  'wt git push', 'wmic process call create x', 'msiexec /i x.msi', 'sc.exe create x', 'pengwin run x'):
            self.assert_denied(c, 'cannot follow')
        for c in ('robocopy /tmp E:/x', 'xcopy a b', 'fsutil hardlink create a b', 'certutil -decode a b', 'expand a b',
                  'mklink /j a b', 'Robocopy.exe a b', "net use Z: '\\\\host\\share'", 'mountvol X: /d', 'replace a b',
                  'attrib +r src/a.c', 'icacls src /deny x:W'):
            self.assert_denied(c, 'cannot follow')
        for c in ("bash -c 'git status'", "/usr/bin/bash -c 'git status'", 'python3 -V', 'ssh host true'):
            self.assert_allowed(c)

    @unittest.skipUnless(os.name == 'nt', 'Windows drops trailing dots, spaces and a :stream from a file name')
    def test_windows_publisher_names_are_matched_as_windows_opens_them(self):
        for c in ('python E:/x/push.py. --pr 1', "python 'E:/x/push.py ' --pr 1", "python 'E:/x/push.py::$DATA' --pr 1",
                  'pyw E:/x/push.py --pr 1', 'pythonw E:/x/sonar.py.. --pr 1', 'E:/x/push.py. --pr 1'):
            self.assert_denied(c, 'inside its workflow')
        self.assert_denied('git.exe. push', 'changes the repository')

    @unittest.skipUnless(os.name == 'nt', 'Windows names a path with a trailing dot or space in its \\\\?\\ form')
    def test_a_component_with_a_trailing_dot_or_space_is_still_the_worktree(self):
        for name in ('src. ', 'src.', 'src '):
            # a file not there yet: resolve() names it \\?\C:\..., which no worktree path starts with
            self.assert_denied(f"echo x > '{self.wt}/{name}/new.c'", 'literal absolute path')
            self.assert_denied(None, 'literal absolute path', tool='Write', file_path=f'{self.project}\\{name}\\new.c')


if __name__ == '__main__':
    unittest.main()
