"""Tests for hooks/headless-chief/chief_guard.py, run as Claude Code runs it: hook JSON on stdin."""
import contextlib
import importlib.util
import io
import json
import os
import shutil
import subprocess
import sys
import tempfile
import time
import unittest
from pathlib import Path
from unittest import mock

SCRIPT = Path(__file__).resolve().parents[1] / 'hooks' / 'headless-chief' / 'chief_guard.py'
# Windows judges a /tmp path as MSYS bash maps it, through cygpath; without one it is denied
needs_msys = unittest.skipIf(os.name == 'nt' and not shutil.which('cygpath'), 'no cygpath to map /tmp')


class ChiefGuard(unittest.TestCase):
    def setUp(self):
        td = tempfile.TemporaryDirectory()
        self.addCleanup(td.cleanup)
        self.root = Path(td.name).resolve()
        self.project = self.root / 'wt'
        self.wt = self.project.as_posix()   # as a bash command names it: an unquoted backslash is an escape
        (self.project / 'src').mkdir(parents=True)
        (self.project / 'src' / 'a.c').write_text('int a;\n')
        # GIT_* from a calling hook would point git at another repository
        self.env = {k: v for k, v in os.environ.items() if k != 'CLAUDE_PROJECT_DIR' and not k.startswith('GIT_')}
        subprocess.run(['git', 'init', '-q', str(self.project)], check=True, env=self.env)

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

    def main(self, guard, command):
        err = io.StringIO()
        with contextlib.redirect_stderr(err), mock.patch.dict(os.environ, self.env, clear=True):
            return guard.main(self.event(command)), err.getvalue()

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
        self.assert_denied('xargs -n 1 git push', 'changes the repository')
        self.assert_denied('xargs -I {} git push origin {}', 'changes the repository')

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

    def test_a_cygwin_or_msys_python_denies_every_call(self):
        # os.name is 'posix' there, so every Windows check would be skipped
        guard = self.module()
        for platform in ('cygwin', 'msys'):
            with mock.patch.object(sys, 'platform', platform):
                code, err = self.main(guard, 'git status')
            self.assertEqual(code, 2)
            self.assertIn(f'needs a native Windows Python; this is {platform}', err)
        self.assertEqual(self.main(guard, 'git status')[0], 0)

    @unittest.skipIf(os.name == 'nt' or sys.platform == 'darwin', 'NTFS and APFS names are Unicode')
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
