"""Tests for hooks/headless-chief/chief_guard.py, run as Claude Code runs it: hook JSON on stdin."""
import json
import os
import subprocess
import sys
import tempfile
import unittest
from pathlib import Path

SCRIPT = Path(__file__).resolve().parents[1] / 'hooks' / 'headless-chief' / 'chief_guard.py'


@unittest.skipIf(os.name == 'nt', 'the hook ships for POSIX sessions')
class ChiefGuard(unittest.TestCase):
    def setUp(self):
        td = tempfile.TemporaryDirectory()
        self.addCleanup(td.cleanup)
        self.root = Path(td.name).resolve()
        self.project = self.root / 'wt'
        (self.project / 'src').mkdir(parents=True)
        (self.project / 'src' / 'a.c').write_text('int a;\n')
        # GIT_* from a calling hook would point git at another repository
        self.env = {k: v for k, v in os.environ.items() if k != 'CLAUDE_PROJECT_DIR' and not k.startswith('GIT_')}
        subprocess.run(['git', 'init', '-q', str(self.project)], check=True, env=self.env)

    def hook(self, command=None, tool='Bash', agent_type='chief', agent_id=None, file_path=None):
        event = {'hook_event_name': 'PreToolUse', 'tool_name': tool, 'cwd': str(self.project), 'agent_type': agent_type,
                 'tool_input': {'command': command} if tool == 'Bash' else {'file_path': file_path, 'content': 'x'}}
        if agent_id:
            event['agent_id'] = agent_id
        return subprocess.run([sys.executable, str(SCRIPT)], input=json.dumps(event), env=self.env,
                              capture_output=True, text=True, timeout=30)

    def assert_denied(self, command, *named, **kw):
        r = self.hook(command, **kw)
        self.assertEqual(r.returncode, 2, f'{command!r} passed')
        for want in named:
            self.assertIn(want, r.stderr)

    def assert_allowed(self, command, **kw):
        r = self.hook(command, **kw)
        self.assertEqual(r.returncode, 0, f'{command!r}: {r.stderr}')

    def test_only_chief_own_calls_are_checked(self):
        self.assert_allowed('git push', agent_type='code-writer', agent_id='a1')
        # a subagent chief dispatched carries an agent_id even under chief's agent_type
        self.assert_allowed('git push', agent_type='chief', agent_id='a2')
        self.assert_allowed('git commit -m x', agent_type=None)
        self.assert_denied('git push', 'changes the repository')

    def test_git_reads_pass(self):
        for c in ('git status --porcelain', 'git log --oneline abc..HEAD', 'git -C /x log -1', 'git -c core.pager=cat diff',
                  'git --no-pager show HEAD', 'git log --name-only --no-renames --format= a..HEAD', 'git fetch -q origin',
                  'git branch --show-current', 'git branch -a --contains abc', 'git tag -l v*', 'git stash list',
                  'git worktree list', 'git remote -v', 'git config --get user.name', 'git rev-parse HEAD',
                  'git symbolic-ref HEAD', 'git remote'):
            self.assert_allowed(c)

    def test_git_writes_are_denied(self):
        for c in ('git push origin HEAD', 'git commit -m x', 'git -C /x commit -am y', 'git -c a=b reset --hard',
                  'git rebase main', 'git checkout -b x', 'git switch main', 'git add -- src', 'git stash',
                  'git stash push -m x', 'git branch new', 'git branch -D old', 'git tag v1', 'git tag -d v1',
                  'git worktree remove x', 'git remote add o u', 'git config user.name x', 'git cherry-pick abc',
                  'git restore src/a.c', 'git clean -fd', 'git merge x', 'git pull', 'git symbolic-ref HEAD refs/heads/x'):
            self.assert_denied(c, 'changes the repository')

    def test_wrappers_chains_and_substitutions_are_seen(self):
        for c in ('GIT_DIR=x git push', 'env -u X git push', 'timeout 60 git push', 'nice -n 5 git push',
                  'git status && git push', 'git log | head; git commit -m x', 'true || git push', '(cd /tmp && git push)',
                  'echo "$(git push)"', 'echo `git push`', "bash -c 'git push'", 'sh -c "git log && git push"',
                  'if git push; then echo y; fi', 'xargs -n1 git push', '/usr/bin/git push', "eval 'git push'"):
            self.assert_denied(c)

    def test_quoted_words_are_data(self):
        self.assert_allowed("git log --grep='git push' --oneline")
        self.assert_allowed('echo "git commit"')

    def test_heredoc_body_is_data_but_its_line_counts(self):
        self.assert_allowed("cat > /tmp/brief.md <<'EOF'\ngit push\ngh pr merge 1\nEOF")
        self.assert_denied("cat > src/x.md <<'EOF'\nhello\nEOF", 'inside the task worktree')
        self.assert_denied("cat <<EOF | tee src/x\nhi\nEOF", 'inside the task worktree')

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
        for c in ('echo x > src/a.c', 'echo x >> ./notes.md', f'echo x > {self.project}/a', 'rm src/a.c', 'rm -rf /tmp/x src',
                  'mv src/a.c /tmp/', 'touch new.c', 'cp /tmp/a src/b.c', "sed -i 's/a/b/' src/a.c", 'mkdir build',
                  'printf x | tee src/a.c'):
            self.assert_denied(c, 'inside the task worktree')

    def test_writes_outside_the_worktree_pass(self):
        for c in ('echo x > /tmp/out.md', 'git log > /tmp/log 2>&1', 'git status 2>/dev/null', 'cp src/a.c /tmp/a.c',
                  'mkdir -p /tmp/chief-run', 'rm -f /tmp/x', "sed 's/a/b/' src/a.c", 'cat src/a.c >&2'):
            self.assert_allowed(c)

    def test_chief_directory_inside_a_primary_checkout_is_writable(self):
        self.assert_allowed('mkdir -p .git/chief && git log > .git/chief/u1-commit-check.txt')
        self.assertEqual(self.hook(tool='Write', file_path=str(self.project / '.git' / 'chief' / 'brief.md')).returncode, 0)
        self.assert_denied('echo x > .git/config', 'inside the task worktree')

    def test_write_tool_only_outside_the_worktree(self):
        self.assertEqual(self.hook(tool='Write', file_path='/tmp/report.md').returncode, 0)
        r = self.hook(tool='Write', file_path=str(self.project / 'src' / 'new.c'))
        self.assertEqual(r.returncode, 2)
        self.assertIn('outside the task worktree', r.stderr)

    def test_a_directory_change_moves_where_relative_paths_land(self):
        self.assert_denied(f'cd /tmp && cd {self.project} && printf done > chief-report.md', 'inside the task worktree')
        self.assert_allowed('cd /tmp && printf done > chief-report.md')
        self.assert_denied('cd "$SOMEWHERE" && printf done > r.md', 'inside the task worktree')
        self.assert_allowed('cd "$SOMEWHERE" && printf done > /tmp/r.md')

    def test_combined_shell_flags_and_xargs_values(self):
        self.assert_denied("bash -lc 'git push origin HEAD'", 'changes the repository')
        self.assert_denied('xargs -n 1 git push', 'changes the repository')
        self.assert_denied('xargs -I {} git push origin {}', 'changes the repository')

    def test_attached_gh_fields_imply_post(self):
        self.assert_denied('gh api repos/o/r/issues -ftitle=x', 'non-GET')
        self.assert_denied('gh api repos/o/r/issues -Ftitle=x', 'non-GET')

    def test_unquoted_heredoc_substitutions_run(self):
        self.assert_denied("cat > /tmp/r.md <<EOF\n$(git push origin HEAD)\nEOF", 'changes the repository')
        self.assert_denied("cat > /tmp/r.md <<EOF\n'`git push`'\nEOF", 'changes the repository')
        self.assert_allowed("cat > /tmp/r.md <<'EOF'\n$(git push origin HEAD)\nEOF")

    def test_git_output_options_are_writes(self):
        self.assert_denied('git diff --output=chief-report.patch', 'inside the task worktree')
        self.assert_denied('git log -p --output chief.patch', 'inside the task worktree')
        self.assert_allowed('git diff --output=/tmp/chief-report.patch')

    def test_simple_variables_and_modes_resolve(self):
        self.assert_allowed('out=/tmp/chief-report.md; printf done > "$out"')
        self.assert_allowed('out=/tmp/chief; mkdir -p "${out}/u1"')
        self.assert_allowed('chmod 600 /tmp/chief-report.md')
        self.assert_denied('chmod 600 src/a.c', 'inside the task worktree')
        self.assert_denied('printf done > "$unset_var"', 'inside the task worktree')

    def test_single_quoted_substitutions_are_data(self):
        self.assert_allowed("printf '%s\\n' '$(git push)' > /tmp/chief-report.md")
        self.assert_allowed("grep -n '`git push`' /tmp/notes.md")

    def test_subshells_and_child_shells_keep_their_own_directory(self):
        for c in ('(cd /tmp && pwd); printf done > chief-report.md', "bash -c 'cd /tmp'; printf done > chief-report.md",
                  'echo "$(cd /tmp && pwd)"; printf done > chief-report.md'):
            self.assert_denied(c, 'inside the task worktree')
        self.assert_allowed('(cd /tmp && printf done > chief-report.md)')

    def test_substitution_output_is_an_unresolved_path(self):
        self.assert_denied('printf done > "$(git rev-parse --show-toplevel)/chief-report.md"', 'inside the task worktree')
        self.assert_denied('mkdir -p "$(git rev-parse --git-path chief)"', 'inside the task worktree')
        self.assert_allowed('echo "$(date)" > /tmp/stamp')

    def test_git_output_follows_git_dash_c(self):
        self.assert_denied(f'cd /tmp && git -C {self.project} diff --output=chief-report.patch', 'inside the task worktree')
        self.assert_allowed(f'git -C /tmp diff --output=chief-report.patch')
        self.assert_allowed('git ls-files -o --exclude-standard')
        self.assert_allowed('git grep -o chief -- src')

    def test_an_explicit_copy_destination_counts(self):
        self.assert_denied('cp -t . /tmp/brief.md', 'inside the task worktree')
        self.assert_denied('cp --target-directory=src /tmp/brief.md', 'inside the task worktree')
        self.assert_allowed('cp -t /tmp src/a.c')
        self.assert_denied('rsync -t /tmp/brief.md .', 'inside the task worktree')
        self.assert_allowed('rsync -t src/a.c /tmp/chief-a.c')

    def test_cd_option_terminator(self):
        self.assert_denied(f'cd /tmp && cd -- {self.project} && printf done > r.md', 'inside the task worktree')
        self.assert_allowed('cd -- /tmp && printf done > chief-report.md')

    def test_an_unparsable_command_is_denied(self):
        self.assert_denied('echo "unterminated', 'cannot parse')


if __name__ == '__main__':
    unittest.main()
