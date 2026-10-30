import contextlib
import importlib.util
import io
import json
import os
import shlex
import shutil
import subprocess
import sys
import tempfile
import unittest
from pathlib import Path
from unittest import mock
sys.path.insert(0, str(Path(__file__).resolve().parent))
from helpers import guard_survivors  # noqa: E402

ROOT = Path(__file__).resolve().parents[1]
SCRIPT = ROOT / 'install.py'
USER_MD = ROOT / 'instructions' / 'user.md'
SKILLS = sorted(p.name for p in (ROOT / 'skills').iterdir() if (p / 'SKILL.md').is_file())
spec = importlib.util.spec_from_file_location('install', SCRIPT)
install = importlib.util.module_from_spec(spec)
spec.loader.exec_module(install)
SKILL = SKILLS[0]
SKILL_HOOKS = [s for s in SKILLS if (ROOT / 'hooks' / s / 'hooks.json').exists()]


def guard_command(script):
    """The settings command of a Python hook: under the installing interpreter on Windows, where bash may find no
    python3 and a hook that cannot start lets the call through."""
    command = shlex.quote(script.as_posix())
    return f'{shlex.quote(Path(sys.executable).as_posix())} {command}' if os.name == 'nt' else command


def setUpModule():
    guard_survivors(__name__)


class InstallTest(unittest.TestCase):
    def setUp(self):
        self.tmp = tempfile.TemporaryDirectory(dir=ROOT.parent if os.name == 'nt' else None)
        self.home = Path(self.tmp.name)
        self.claude, self.codex = self.home / '.claude', self.home / '.codex'

    def tearDown(self):
        self.tmp.cleanup()

    def run_cli(self, *argv, cwd=None):
        return subprocess.run([sys.executable, str(SCRIPT), *argv], capture_output=True, text=True, cwd=cwd,
                              env={**os.environ, 'HOME': str(self.home), 'USERPROFILE': str(self.home)})

    def ok(self, *argv, cwd=None):
        done = self.run_cli(*argv, cwd=cwd)
        self.assertEqual(done.returncode, 0, done.stderr)
        return done.stdout

    def settings(self):
        return json.loads((self.claude / 'settings.json').read_text())

    def commands(self, data):
        return {e: [h['command'] for g in gs for h in g['hooks']] for e, gs in data.get('hooks', {}).items()}

    # --- selection ------------------------------------------------------------

    def test_nothing_is_selected_by_default_and_bad_selections_are_usage_errors(self):
        for argv in (['install'], ['install', '--skill', SKILL], ['install', '--agent', 'all'],
                     ['install', '--hook'], ['remove'], ['frob', '--skill']):
            done = self.run_cli(*argv)
            self.assertEqual(done.returncode, 2, argv)
        self.assertFalse(self.claude.exists(), 'nothing was created')

    def test_the_removed_herdr_plugin_flag_is_a_usage_error_before_any_change(self):
        done = self.run_cli('install', '--claude-md', '--herdr-plugin')
        self.assertEqual(done.returncode, 2)
        self.assertIn('unrecognized arguments: --herdr-plugin', done.stderr)
        self.assertEqual(os.listdir(self.home), [], 'nothing was linked')
        self.ok('install', '--claude-md')
        done = self.run_cli('remove', '--claude-md', '--herdr-plugin')
        self.assertEqual(done.returncode, 2)
        self.assertIn('unrecognized arguments: --herdr-plugin', done.stderr)
        self.assertTrue((self.claude / 'CLAUDE.md').is_symlink(), 'nothing was unlinked')

    # --- install --------------------------------------------------------------

    def test_skills_link_into_both_cli_dirs_and_nothing_else(self):
        out = self.ok('install', '--skill')
        for d in (self.claude / 'skills', self.codex / 'skills'):
            self.assertTrue(os.path.samefile(d / SKILL, ROOT / 'skills' / SKILL))
            self.assertEqual(sorted(p.name for p in d.iterdir()), SKILLS)
        self.assertEqual(out.count('->'), 2 * len(SKILLS) + len(SKILL_HOOKS), "and each skill's hook")
        self.assertFalse((self.claude / 'CLAUDE.md').exists())
        self.assertFalse((self.claude / 'agents').exists())
        self.assertEqual(self.ok('install', '--skill'), '', 'a rerun is silent')

    def test_each_flag_covers_its_kind_and_a_skill_brings_its_hook(self):
        self.ok('install', '--skill', '--agent', '--workflow', '--bin', '--claude-md')
        self.assertEqual(sorted(p.name for p in (self.codex / 'skills').iterdir()), SKILLS)
        self.assertTrue(os.path.samefile(self.claude / 'agents' / 'pvs-studio.md', ROOT / 'agents' / 'pvs-studio.md'))
        self.assertEqual(sorted(p.name for p in (self.codex / 'agents').iterdir()),
                         sorted(p.name for p in (ROOT / 'agents').iterdir()), 'every agent md and toml')
        self.assertTrue(os.path.samefile(self.claude / 'hooks' / 'simplify-gate', ROOT / 'hooks' / 'simplify-gate'))
        self.assertTrue(os.path.samefile(self.claude / 'workflows' / 'fix-issue.js', ROOT / 'workflows' / 'fix-issue.js'))
        self.assertTrue(os.path.samefile(self.claude / 'workflows' / 'code-audit.js', ROOT / 'workflows' / 'code-audit.js'))
        self.assertFalse((self.codex / 'workflows').exists(), 'workflows are Claude only')
        self.assertTrue(os.path.samefile(self.home / '.local' / 'bin' / 'lsusbt', ROOT / 'bin' / 'lsusbt'))
        self.assertTrue(os.path.samefile(self.claude / 'CLAUDE.md', USER_MD))
        self.assertTrue(os.path.samefile(self.codex / 'AGENTS.md', USER_MD))
        self.assertEqual((self.codex / 'AGENTS.md').read_text(encoding='utf-8'), USER_MD.read_text(encoding='utf-8'))
        self.assertEqual(self.ok('install', '--claude-md'), '', 'relative Codex link is idempotent outside HOME')

    def test_a_link_to_the_old_root_file_moves_to_the_user_file(self):
        self.claude.mkdir()
        os.symlink(ROOT / 'CLAUDE.md', self.claude / 'CLAUDE.md')
        self.ok('install', '--claude-md')
        self.assertTrue(os.path.samefile(self.claude / 'CLAUDE.md', USER_MD))

    def test_a_skill_hook_is_registered_once_with_absolute_quoted_commands(self):
        (self.claude).mkdir()
        before = {'model': 'x', 'hooks': {
            'SessionStart': [{'matcher': '*', 'hooks': [{'type': 'command', 'command': 'other'}]}],
            'Stop': [{'hooks': [{'type': 'command', 'command': 'other-stop'}]}]}}
        (self.claude / 'settings.json').write_text(json.dumps(before))
        self.ok('install', '--skill')
        launcher = (self.claude / 'hooks' / 'simplify-gate' / 'simplify-gate').as_posix()  # bash runs it, also on Windows
        guards = [guard_command(self.claude / 'hooks' / 'headless-chief' / g) for g in ('shadow_guard.py', 'chief_guard.py')]
        data = self.settings()
        self.assertEqual(data['model'], 'x')
        self.assertEqual(self.commands(data), {'SessionStart': ['other'], 'Stop': ['other-stop', launcher],
                                               'UserPromptSubmit': [launcher], 'PreToolUse': guards})
        self.assertEqual([g['matcher'] for g in data['hooks']['PreToolUse']], ['Workflow|Agent', 'Bash|Write'])
        stop = data['hooks']['Stop'][-1]['hooks'][0]
        self.assertEqual(stop['timeout'], 650)
        self.assertNotIn('matcher', data['hooks']['Stop'][-1])
        self.assertEqual(json.loads((self.claude / 'settings.json.before-agentrc').read_text()), before)
        self.assertEqual(self.ok('install', '--skill'), '', 'idempotent')
        self.assertEqual(self.settings(), data)

    def test_a_foreign_hook_with_relative_words_survives_a_run_from_inside_the_hooks(self):
        self.claude.mkdir()
        foreign = {'hooks': {'Stop': [{'hooks': [{'type': 'command', 'command': 'bash other-stop.sh'}]}]}}
        (self.claude / 'settings.json').write_text(json.dumps(foreign))
        self.ok('install', '--skill', cwd=ROOT / 'hooks')
        self.ok('remove', '--skill', cwd=ROOT / 'hooks')
        self.assertEqual(self.settings(), foreign)

    def test_a_hook_path_with_a_space_is_quoted(self):
        home = self.home / 'my home'
        home.mkdir()
        done = subprocess.run([sys.executable, str(SCRIPT), 'install', '--skill'],
                              capture_output=True, text=True,
                              env={**os.environ, 'HOME': str(home), 'USERPROFILE': str(home)})
        self.assertEqual(done.returncode, 0, done.stderr)
        data = json.loads((home / '.claude' / 'settings.json').read_text())
        launcher = (home / '.claude' / 'hooks' / 'simplify-gate' / 'simplify-gate').as_posix()
        command = data['hooks']['Stop'][0]['hooks'][0]['command']
        self.assertEqual(command, f"'{launcher}'")
        if os.name == 'nt':  # Claude Code runs it through bash: a backslash path would lose its separators
            done = subprocess.run([shutil.which('bash'), '-c', f'printf %s {command}'], capture_output=True, text=True)
            self.assertEqual(done.stdout, launcher)

    def test_settings_are_utf8(self):
        self.claude.mkdir()
        before = {'model': 'Zoë ✔ ő'}  # outside the Windows ANSI code pages
        (self.claude / 'settings.json').write_bytes(json.dumps(before, ensure_ascii=False).encode())
        env = {**os.environ, 'HOME': str(self.home), 'USERPROFILE': str(self.home)}
        env.pop('PYTHONUTF8', None)
        done = subprocess.run([sys.executable, str(SCRIPT), 'install', '--skill'], capture_output=True, env=env)
        self.assertEqual(done.returncode, 0, done.stderr)
        self.assertEqual(json.loads((self.claude / 'settings.json').read_bytes())['model'], before['model'])
        self.assertEqual(json.loads((self.claude / 'settings.json.before-agentrc').read_bytes()), before)

    def test_a_reinstall_replaces_the_entries_an_earlier_version_wrote(self):
        self.claude.mkdir()
        launcher = str(self.claude / 'hooks' / 'simplify-gate' / 'simplify-gate')
        old = [f"'{launcher}'", subprocess.list2cmdline([launcher])]  # backslashes on Windows
        (self.claude / 'settings.json').write_text(json.dumps({'hooks': {'Stop': [
            {'hooks': [{'type': 'command', 'command': command}]} for command in old]}}))
        self.ok('install', '--skill')
        self.assertEqual(self.commands(self.settings())['Stop'], [Path(launcher).as_posix()])

    def test_a_home_with_quotes_or_spaces_keeps_one_entry_per_hook(self):
        for name in ("O'Brien", "O'Brien Doe", 'John Doe'):
            with self.subTest(name):
                home = self.home / name
                claude = home / '.claude'
                claude.mkdir(parents=True)
                launcher = claude / 'hooks' / 'simplify-gate' / 'simplify-gate'
                guard = claude / 'hooks' / 'headless-chief' / 'chief_guard.py'
                # forms earlier versions wrote: bash quoting of the native path, and list2cmdline on Windows
                old = [shlex.quote(str(launcher))] + ([subprocess.list2cmdline([str(launcher)])] if os.name == 'nt' else [])
                old_guard = [shlex.quote(str(guard))] + ([subprocess.list2cmdline([str(guard)])] if os.name == 'nt' else [])
                (claude / 'settings.json').write_text(json.dumps({'hooks': {
                    'Stop': [{'hooks': [{'type': 'command', 'command': c}]} for c in old],
                    'PreToolUse': [{'hooks': [{'type': 'command', 'command': c}]} for c in old_guard]}}))
                env = {**os.environ, 'HOME': str(home), 'USERPROFILE': str(home)}
                for _ in range(2):
                    done = subprocess.run([sys.executable, str(SCRIPT), 'install', '--skill'], capture_output=True,
                                          text=True, env=env)
                    self.assertEqual(done.returncode, 0, done.stderr)
                    data = json.loads((claude / 'settings.json').read_text())
                    self.assertEqual(self.commands(data)['Stop'], [shlex.quote(launcher.as_posix())])
                    self.assertEqual(self.commands(data)['PreToolUse'].count(guard_command(guard)), 1)
                    self.assertEqual(len(self.commands(data)['PreToolUse']), 2, 'each guard once')
                done = subprocess.run([sys.executable, str(SCRIPT), 'remove', '--skill'], capture_output=True,
                                      text=True, env=env)
                self.assertEqual(done.returncode, 0, done.stderr)
                self.assertNotIn('hooks', json.loads((claude / 'settings.json').read_text()))

    @unittest.skipUnless(os.name == 'nt', 'POSIX runs a Python hook by its shebang')
    def test_a_python_hook_names_this_interpreter_for_bash_on_windows(self):
        self.ok('install', '--skill')
        guard = self.claude / 'hooks' / 'headless-chief' / 'chief_guard.py'
        command = self.commands(self.settings())['PreToolUse'][1]
        self.assertEqual(command, guard_command(guard))
        done = subprocess.run([shutil.which('bash'), '-c', f"printf '%s\n' {command}"], capture_output=True, text=True)
        self.assertEqual(done.stdout.splitlines(), [Path(sys.executable).as_posix(), guard.as_posix()])

    @unittest.skipUnless(os.name == 'nt', 'POSIX needs no privilege to link')
    def test_no_symlink_privilege_refuses_before_touching_anything(self):
        def no_privilege(*args, **kwargs):
            raise OSError(None, 'A required privilege is not held by the client', None, 1314)

        with mock.patch.dict(os.environ, {'HOME': str(self.home), 'USERPROFILE': str(self.home)}),                 mock.patch.object(install.os, 'symlink', no_privilege), mock.patch('sys.stderr') as err,                 self.assertRaises(SystemExit) as done:
            install.main(['install', '--skill', '--claude-md'])
        self.assertEqual(done.exception.code, 1)
        self.assertIn('Developer Mode', ''.join(c.args[0] for c in err.write.call_args_list))
        self.assertEqual(os.listdir(self.home), [], 'nothing created')

    def test_a_dead_link_of_ours_is_pruned_and_foreign_links_and_content_stay(self):
        (self.claude / 'skills').mkdir(parents=True)
        os.symlink(ROOT / 'skills' / 'gone', self.claude / 'skills' / 'gone')
        os.symlink('/nonexistent/elsewhere', self.claude / 'skills' / 'foreign')
        (self.claude / 'skills' / 'mine').mkdir()
        (self.claude / 'skills' / 'mine' / 'SKILL.md').write_text('x')
        self.ok('install', '--skill')
        names = sorted(p.name for p in (self.claude / 'skills').iterdir())
        self.assertEqual(names, sorted(SKILLS + ['foreign', 'mine']))

    def test_a_skill_directory_without_skill_md_is_not_linked_and_its_link_is_pruned(self):
        repo = self.home / 'repo'
        (repo / 'skills' / 'kept').mkdir(parents=True)
        (repo / 'skills' / 'kept' / 'SKILL.md').write_text('x')
        (repo / 'skills' / 'moved' / 'scripts' / '__pycache__').mkdir(parents=True)
        (repo / 'skills' / 'odd' / 'SKILL.md').mkdir(parents=True)
        (repo / 'hooks').mkdir()
        for d in (self.claude / 'skills', self.codex / 'skills'):
            d.mkdir(parents=True)
            os.symlink(repo / 'skills' / 'moved', d / 'moved')
            os.symlink(Path(os.path.relpath(repo / 'skills' / 'odd', d)), d / 'odd')
        (self.claude / 'skills' / 'real').mkdir()
        os.symlink(self.home, self.claude / 'skills' / 'foreign')
        with mock.patch.object(install, 'REPO', repo), mock.patch.object(Path, 'home', return_value=self.home), \
                contextlib.redirect_stdout(io.StringIO()):
            install.main(['install', '--skill'])
        self.assertEqual(sorted(p.name for p in (self.claude / 'skills').iterdir()), ['foreign', 'kept', 'real'])
        self.assertEqual(sorted(p.name for p in (self.codex / 'skills').iterdir()), ['kept'])
        self.assertTrue((repo / 'skills' / 'moved' / 'scripts' / '__pycache__').is_dir(), 'the directory itself stays')

    def test_replacing_a_link_never_touches_a_neighbouring_file(self):
        (self.claude / 'skills').mkdir(parents=True)
        os.symlink('/old/target', self.claude / 'skills' / SKILL)
        (self.claude / 'skills' / f'{SKILL}.agentrc-old').write_text('precious')
        self.ok('install', '--skill')
        self.assertEqual((self.claude / 'skills' / f'{SKILL}.agentrc-old').read_text(), 'precious')
        self.assertEqual(sorted(p.name for p in (self.claude / 'skills').iterdir()), sorted(SKILLS + [f'{SKILL}.agentrc-old']),
                         'no staging dir left behind')

    def test_prune_resolves_dot_dot_before_deciding_a_link_is_ours(self):
        (self.claude / 'skills').mkdir(parents=True)
        os.symlink(ROOT / 'skills' / '..' / '..' / 'elsewhere' / 'missing', self.claude / 'skills' / 'tricky')
        os.symlink(Path(os.path.relpath(ROOT / 'skills' / 'gone', self.claude / 'skills')), self.claude / 'skills' / 'gone')
        self.ok('install', '--skill')
        names = sorted(p.name for p in (self.claude / 'skills').iterdir())
        self.assertEqual(names, sorted(SKILLS + ['tricky']), 'a relative dead link of ours goes, a dotted foreign one stays')

    def test_unusable_settings_refuse_before_any_link_or_unlink(self):
        self.claude.mkdir()
        for bad in ('{not json', '{"hooks": []}', '{"hooks": {"Stop": [{"hooks": "x"}]}}'):
            (self.claude / 'settings.json').write_text(bad)
            done = self.run_cli('install', '--skill')
            self.assertEqual(done.returncode, 1, bad)
            self.assertIn('fix it first', done.stderr)
            self.assertFalse((self.claude / 'skills').exists(), bad)
        self.ok('install', '--agent')  # no hook, settings never read
        (self.claude / 'settings.json').unlink()
        self.ok('install', '--skill')
        (self.claude / 'settings.json').write_text('{not json')
        self.assertEqual(self.run_cli('remove', '--skill').returncode, 1)
        self.assertTrue((self.claude / 'hooks' / 'simplify-gate').is_symlink(), 'still linked')
        (self.claude / 'settings.json').write_text('{"model": "x"}')
        self.assertNotIn('updated', self.ok('remove', '--skill'))
        self.assertEqual((self.claude / 'settings.json').read_text(), '{"model": "x"}', 'a no-op keeps the file byte for byte')
        self.assertFalse((self.claude / 'settings.json.before-agentrc').exists())

    def test_statusline_links_its_files_and_sets_the_setting_once(self):
        self.claude.mkdir()
        (self.claude / 'settings.json').write_text(json.dumps({'model': 'x', 'hooks': {}}))
        self.ok('install', '--statusline')
        for name in ('statusline.sh', 'statusline-codex-usage.py', 'statusline-prs.py'):
            # Windows reads an absolute link target back with its \\?\ prefix and \ separators
            self.assertEqual(os.path.normcase(os.readlink(self.claude / name).removeprefix('\\\\?\\')),
                             os.path.normcase(ROOT / 'statusline' / name))
        self.assertEqual(self.settings(), {'model': 'x', 'hooks': {}, 'statusLine': {
            'type': 'command', 'command': 'bash "$HOME/.claude/statusline.sh"'}}, 'hooks untouched')
        self.assertEqual(self.ok('install', '--statusline'), '', 'a rerun is silent')
        self.ok('remove', '--statusline')
        self.assertFalse((self.claude / 'statusline.sh').is_symlink())
        self.assertEqual(self.settings(), {'model': 'x', 'hooks': {}})

    def test_another_statusline_refuses_install_and_survives_remove(self):
        self.claude.mkdir()
        mine = {'statusLine': {'type': 'command', 'command': 'bash ~/.claude/statusline.sh'}}
        (self.claude / 'settings.json').write_text(json.dumps(mine))
        done = self.run_cli('install', '--statusline')
        self.assertEqual(done.returncode, 1)
        self.assertIn('another statusLine', done.stderr)
        self.assertFalse((self.claude / 'statusline.sh').is_symlink(), 'refused before linking')
        os.symlink('/dotfiles/statusline.sh', self.claude / 'statusline.sh')
        self.assertIn('left alone', self.ok('remove', '--statusline'))
        self.assertEqual(self.settings(), mine)
        self.assertEqual(os.readlink(self.claude / 'statusline.sh'), '/dotfiles/statusline.sh', 'the link it runs stays')

    # --- refusals ---------------------------------------------------------------

    def test_refuses_before_touching_anything(self):
        cases = {
            'content at a destination': lambda: ((self.claude / 'skills' / SKILL).mkdir(parents=True),
                                                 (self.claude / 'skills' / SKILL / 'own').write_text('x')),
            'a file where a link dir goes': lambda: (self.claude.mkdir(), (self.claude / 'agents').write_text('x')),
            'a dangling link dir': lambda: (self.claude.mkdir(), os.symlink('/nonexistent', self.claude / 'hooks')),
            'a real CLAUDE.md': lambda: (self.claude.mkdir(), (self.claude / 'CLAUDE.md').write_text('mine')),
            'a real status line script': lambda: (self.claude.mkdir(), (self.claude / 'statusline.sh').write_text('mine')),
            'a file above a link dir': lambda: (self.home / '.local').write_text('x'),
            'a dangling link above a link dir': lambda: os.symlink('/nonexistent', self.home / '.local'),
        }
        for case, arrange in cases.items():
            with self.subTest(case):
                self.tearDown()
                self.setUp()
                arrange()
                before = sorted(str(p) for p in self.home.rglob('*'))
                done = self.run_cli('install', '--skill', '--agent', '--workflow', '--bin', '--claude-md', '--statusline')
                self.assertEqual(done.returncode, 1, case)
                self.assertIn('move it aside first', done.stderr)
                self.assertEqual(sorted(str(p) for p in self.home.rglob('*')), before, 'nothing changed')

    # --- remove -----------------------------------------------------------------

    def test_remove_unlinks_whatever_the_link_points_to_but_never_content(self):
        self.ok('install', '--skill', '--agent', '--workflow', '--bin')
        os.remove(self.codex / 'skills' / SKILL)
        os.symlink('/somewhere/else', self.codex / 'skills' / SKILL)
        (self.claude / 'agents' / 'pvs-studio.md').unlink()
        (self.claude / 'agents' / 'pvs-studio.md').write_text('my own copy')
        out = self.ok('remove', '--skill', '--agent', '--workflow', '--bin')
        self.assertFalse((self.claude / 'skills' / SKILL).exists())
        self.assertFalse((self.home / '.local' / 'bin' / 'lsusbt').is_symlink())
        self.assertFalse((self.claude / 'workflows' / 'fix-issue.js').exists())
        self.assertFalse((self.codex / 'skills' / SKILL).is_symlink(), 'a foreign link is removed too')
        self.assertEqual((self.claude / 'agents' / 'pvs-studio.md').read_text(), 'my own copy')
        self.assertIn('left alone', out)
        self.assertFalse((self.codex / 'agents' / 'pvs-studio.toml').exists())
        self.assertFalse((self.claude / 'hooks' / 'simplify-gate').exists())
        self.assertNotIn('hooks', self.settings())
        (self.claude / 'settings.json').unlink()
        self.assertEqual(self.ok('remove', '--skill'), '', 'no settings file, nothing to say')
        self.assertFalse((self.claude / 'settings.json').exists())

    def test_remove_claude_md_only_when_it_links_into_this_checkout(self):
        self.ok('install', '--claude-md')
        (self.claude / 'CLAUDE.md').unlink()
        os.symlink('/elsewhere/CLAUDE.md', self.claude / 'CLAUDE.md')
        out = self.ok('remove', '--claude-md')
        self.assertTrue((self.claude / 'CLAUDE.md').is_symlink(), 'someone else\'s link stays')
        self.assertTrue((self.codex / 'AGENTS.md').is_symlink(), 'and the Codex link now resolves through it')
        self.assertEqual(out.count('left alone'), 2)
        (self.claude / 'CLAUDE.md').unlink()
        os.symlink(ROOT / 'CLAUDE.md', self.claude / 'CLAUDE.md')
        self.ok('remove', '--claude-md')
        self.assertFalse((self.claude / 'CLAUDE.md').is_symlink())
        self.assertFalse((self.codex / 'AGENTS.md').is_symlink(), 'the relative Codex link is judged before Claude\'s goes')

    def test_a_user_wide_install_writes_no_agentrc_dir(self):
        self.ok('install', '--skill', '--agent', '--workflow')
        self.assertNotIn('env', self.settings())
        self.assertFalse((self.claude / 'settings.local.json').exists())


AGENTS_MD = sorted(p.name for p in (ROOT / 'agents').glob('*.md'))
WORKFLOWS = sorted(p.name for p in (ROOT / 'workflows').glob('*.js'))


class ProjectInstallTest(unittest.TestCase):
    """install.py --project: the Claude entries in one work tree's .claude, nothing user-wide."""

    def setUp(self):
        self.tmp = tempfile.TemporaryDirectory(dir=ROOT.parent if os.name == 'nt' else None)
        top = Path(self.tmp.name)
        self.home, self.repo, self.wt = top / 'home', top / 'repo', top / 'wt'
        self.home.mkdir()
        self.repo.mkdir()
        # git here and in install.py reads no user or system config: a global ignore would hide what a test commits
        self.env = {**{k: v for k, v in os.environ.items() if not k.startswith('GIT_') and k != 'XDG_CONFIG_HOME'},
                    'HOME': str(self.home), 'USERPROFILE': str(self.home), 'GIT_CONFIG_GLOBAL': os.devnull,
                    'GIT_CONFIG_NOSYSTEM': '1'}
        self.git('init', '-q')
        own = self.repo / '.claude' / 'skills' / 'own-skill'
        own.mkdir(parents=True)
        (own / 'SKILL.md').write_text('the project\'s own skill')
        (self.repo / '.claude' / 'settings.json').write_text('{"model": "tracked"}')
        self.git('add', '-A')
        self.git('commit', '-qm', 'init')
        self.exclude = self.repo / '.git' / 'info' / 'exclude'
        self.exclude.parent.mkdir(exist_ok=True)
        self.exclude.write_text('# the user\'s own\n*.local-note\n')

    def tearDown(self):
        self.tmp.cleanup()

    def git(self, *args, cwd=None, input=None):
        done = subprocess.run(['git', '-c', 'core.fsmonitor=false', '-c', 'user.name=t', '-c', 'user.email=t@t',
                               '-c', 'core.autocrlf=false', *args], cwd=cwd or self.repo, capture_output=True, text=True,
                              env=self.env, input=input)
        self.assertEqual(done.returncode, 0, done.stderr)
        return done.stdout

    def run_cli(self, *argv):
        return subprocess.run([sys.executable, str(SCRIPT), *argv], capture_output=True, text=True, env=self.env)

    def ok(self, *argv):
        done = self.run_cli(*argv)
        self.assertEqual(done.returncode, 0, done.stderr)
        return done.stdout

    def local(self, tree=None):
        return json.loads(((tree or self.repo) / '.claude' / 'settings.local.json').read_text(encoding='utf-8'))

    def block(self):
        lines = self.exclude.read_bytes().decode('utf-8', 'surrogateescape').split('\n')
        if install.BLOCK[0] not in lines:
            return None
        return lines[lines.index(install.BLOCK[0]) + 1:lines.index(install.BLOCK[1])]

    def status(self, tree=None):
        return self.git('status', '--short', '--untracked-files=all', cwd=tree)

    def snapshot(self):
        return sorted(str(p) for p in Path(self.tmp.name).rglob('*') if '.git' not in p.parts), self.exclude.read_bytes()

    def test_entries_link_into_the_work_tree_and_nothing_user_wide(self):
        out = self.ok('install', '--project', str(self.repo), '--skill', '--agent', '--workflow')
        claude = self.repo / '.claude'
        self.assertEqual(sorted(p.name for p in (claude / 'skills').iterdir()), sorted(SKILLS + ['own-skill']))
        self.assertTrue(os.path.samefile(claude / 'skills' / SKILL, ROOT / 'skills' / SKILL))
        self.assertEqual(sorted(p.name for p in (claude / 'agents').iterdir()), AGENTS_MD, 'the .md only: no Codex toml')
        self.assertEqual(sorted(p.name for p in (claude / 'workflows').iterdir()), WORKFLOWS)
        self.assertEqual(sorted(p.name for p in (claude / 'hooks').iterdir()), SKILL_HOOKS)
        self.assertEqual(os.listdir(self.home), [], 'no ~/.claude, ~/.codex or ~/.local')
        self.assertEqual((claude / 'settings.json').read_text(), '{"model": "tracked"}', 'the tracked settings untouched')
        data = self.local()
        self.assertEqual(data['env'], {'AGENTRC_DIR': ROOT.as_posix()})
        hooks = claude / 'hooks'
        self.assertEqual(TestCommands.of(data), {
            'PreToolUse': [guard_command(hooks / 'headless-chief' / g) for g in ('shadow_guard.py', 'chief_guard.py')],
            'UserPromptSubmit': [shlex.quote((hooks / 'simplify-gate' / 'simplify-gate').as_posix())],
            'Stop': [shlex.quote((hooks / 'simplify-gate' / 'simplify-gate').as_posix())]})
        self.assertFalse((claude / 'settings.local.json.before-agentrc').exists())
        self.assertEqual(self.status(), '', 'every link and settings.local.json excluded')
        block = self.block()
        self.assertIn(f'/.claude/skills/{SKILL}', block)
        self.assertIn('/.claude/settings.local.json', block)
        self.assertEqual(len(block), len(SKILLS) + len(AGENTS_MD) + len(WORKFLOWS) + len(SKILL_HOOKS) + 1)
        self.assertTrue(self.exclude.read_text().startswith('# the user\'s own\n*.local-note\n'))
        self.assertIn('updated', out)
        before = self.snapshot()
        self.assertEqual(self.ok('install', '--project', str(self.repo), '--skill', '--agent', '--workflow'), '',
                         'a rerun is silent')
        self.assertEqual(self.snapshot(), before)

    def test_remove_takes_out_links_hooks_env_and_the_block_but_nothing_else(self):
        claude = self.repo / '.claude'
        claude.joinpath('settings.local.json').write_text(json.dumps({'permissions': {'allow': ['Bash(ls)']}}))
        self.ok('install', '--project', str(self.repo), '--skill', '--workflow')
        self.ok('remove', '--project', str(self.repo), '--workflow')
        self.assertEqual(self.local()['env'], {'AGENTRC_DIR': ROOT.as_posix()}, 'the skills still need it')
        self.assertNotIn('/.claude/workflows/pr-babysit.js', self.block())
        self.assertEqual(self.status(), '')
        self.ok('remove', '--project', str(self.repo), '--skill')
        self.assertEqual(self.local(), {'permissions': {'allow': ['Bash(ls)']}}, 'only ours went')
        self.assertEqual(sorted(p.name for p in (claude / 'skills').iterdir()), ['own-skill'])
        self.assertEqual(self.block(), ['/.claude/settings.local.json'], 'the file stays, so does its entry')
        (claude / 'settings.local.json').unlink()
        self.ok('install', '--project', str(self.repo), '--agent')
        self.ok('remove', '--project', str(self.repo), '--agent')
        self.assertFalse((claude / 'settings.local.json').exists(), 'a local file with nothing left goes')
        self.assertEqual(self.exclude.read_text(), '# the user\'s own\n*.local-note\n', 'block gone, the rest kept')
        self.assertEqual(self.status(), '')

    def test_the_exclude_block_is_shared_by_worktrees(self):
        self.git('worktree', 'add', '-q', str(self.wt))
        self.ok('install', '--project', str(self.repo), '--workflow')
        self.ok('install', '--project', str(self.wt), '--workflow')
        self.assertEqual(self.local(self.wt)['env'], {'AGENTRC_DIR': ROOT.as_posix()})
        self.assertFalse((self.wt / '.git' / 'info').exists(), 'a linked worktree uses the common exclude file')
        self.ok('remove', '--project', str(self.repo), '--workflow')
        self.assertEqual(self.status(self.wt), '', 'the worktree still has them: their entries stay')
        self.assertEqual(self.status(), '')
        self.ok('remove', '--project', str(self.wt), '--workflow')
        self.assertIsNone(self.block())

    def test_claude_md_is_the_projects_claude_local_md(self):
        local = self.repo / 'CLAUDE.local.md'
        self.ok('install', '--project', str(self.repo), '--claude-md')
        self.assertTrue(os.path.samefile(local, ROOT / 'instructions' / 'user.md'))
        self.assertIn('/CLAUDE.local.md', self.block())
        self.assertEqual(self.status(), '')
        self.assertEqual(os.listdir(self.home), [], 'no ~/.claude/CLAUDE.md or ~/.codex/AGENTS.md')
        self.ok('remove', '--project', str(self.repo), '--claude-md')
        self.assertFalse(local.is_symlink() or local.exists())
        self.assertIsNone(self.block())
        local.write_text('the user\'s own')
        done = self.run_cli('install', '--project', str(self.repo), '--claude-md')
        self.assertEqual(done.returncode, 1)
        self.assertIn('real file', done.stderr)
        self.assertIn('skipped', self.ok('install', '--project', str(self.repo), '--claude-md', '--skip-clashes'))
        self.assertIn('left alone', self.ok('remove', '--project', str(self.repo), '--claude-md'))
        self.assertEqual(local.read_text(), 'the user\'s own')

    def test_claude_md_keeps_agentrc_dir_while_it_stays(self):
        self.ok('install', '--project', str(self.repo), '--claude-md', '--workflow')
        self.ok('remove', '--project', str(self.repo), '--workflow')
        self.assertEqual(self.local()['env'], {'AGENTRC_DIR': ROOT.as_posix()}, 'CLAUDE.local.md is still ours')
        self.ok('remove', '--project', str(self.repo), '--claude-md')
        self.assertFalse((self.repo / '.claude' / 'settings.local.json').exists())
        self.assertIsNone(self.block())

    def test_claude_md_alone_refuses_a_claude_dir_it_cannot_write_into(self):
        shutil.rmtree(self.repo / '.claude')
        (self.repo / '.claude').write_text('not a directory')
        before = self.snapshot()
        done = self.run_cli('install', '--project', str(self.repo), '--claude-md')
        self.assertEqual(done.returncode, 1)
        self.assertIn('is not a directory', done.stderr)
        self.assertEqual(self.snapshot(), before, 'no CLAUDE.local.md left behind')

    def test_a_settings_local_file_already_ignored_gets_no_entry(self):
        (self.repo / '.gitignore').write_text('.claude/settings.local.json\n')
        self.git('add', '.gitignore')
        self.git('commit', '-qm', 'ignore')
        self.ok('install', '--project', str(self.repo), '--workflow')
        self.assertNotIn('/.claude/settings.local.json', self.block())
        self.assertEqual(self.status(), '')

    def test_a_clash_refuses_before_any_change_unless_skipped(self):
        def tracked_skill():
            (self.repo / '.claude' / 'skills' / SKILL).mkdir()
            (self.repo / '.claude' / 'skills' / SKILL / 'SKILL.md').write_text('project copy')
            self.git('add', '-A')
            self.git('commit', '-qm', 'clash')

        def tracked_link():
            os.symlink('elsewhere', self.repo / '.claude' / 'skills' / SKILL)
            self.git('add', '-A')
            self.git('commit', '-qm', 'clash')

        def untracked_workflow():
            (self.repo / '.claude' / 'workflows').mkdir()
            (self.repo / '.claude' / 'workflows' / 'pr-babysit.js').write_text('local copy')

        def bytecode_only():
            cache = self.repo / '.claude' / 'skills' / SKILL / 'scripts' / '__pycache__'
            cache.mkdir(parents=True)
            (cache / 'x.cpython-312.pyc').write_bytes(b'')

        def empty_directories_only():
            (self.repo / '.claude' / 'skills' / SKILL / 'scripts' / '__pycache__').mkdir(parents=True)

        cases = {'a tracked skill': (tracked_skill, 'is tracked'), 'a tracked symlink': (tracked_link, 'is tracked'),
                 'an untracked real file': (untracked_workflow, 'real file'),
                 'a leftover bytecode cache': (bytecode_only, 'bytecode caches'),
                 'empty directories only': (empty_directories_only, 'real file or directory')}
        for case, (arrange, why) in cases.items():
            with self.subTest(case):
                self.tearDown()
                self.setUp()
                arrange()
                before, untracked = self.snapshot(), self.status()
                done = self.run_cli('install', '--project', str(self.repo), '--skill', '--agent', '--workflow')
                self.assertEqual(done.returncode, 1, case)
                self.assertIn(why, done.stderr)
                self.assertIn('--skip-clashes', done.stderr)
                self.assertEqual(self.snapshot(), before, 'nothing changed')
                out = self.ok('install', '--project', str(self.repo), '--skill', '--agent', '--workflow', '--skip-clashes')
                self.assertIn('skipped', out)
                clash = self.repo / '.claude' / ('workflows/pr-babysit.js' if case == 'an untracked real file' else f'skills/{SKILL}')
                self.assertFalse(install.ours(clash), 'not linked over')
                self.assertEqual(self.status(), untracked, 'only what the project had shows')
                self.assertNotIn('/' + clash.relative_to(self.repo).as_posix(), self.block())
                self.assertIn('left alone', self.ok('remove', '--project', str(self.repo), '--skill', '--workflow',
                                                    '--agent'))
                self.assertTrue(clash.is_symlink() or clash.exists(), 'the project\'s own stays')

    def test_a_skipped_skill_takes_its_hook_with_it(self):
        own = self.repo / '.claude' / 'skills' / 'headless-chief'
        own.mkdir(parents=True)
        (own / 'SKILL.md').write_text('the project\'s own headless-chief')
        self.git('add', '-A')
        self.git('commit', '-qm', 'own skill')
        out = self.ok('install', '--project', str(self.repo), '--skill', '--skip-clashes')
        self.assertIn('belongs to the skipped skill headless-chief', out)
        self.assertFalse((self.repo / '.claude' / 'hooks' / 'headless-chief').exists())
        self.assertNotIn('PreToolUse', TestCommands.of(self.local()), 'its guards are not registered')
        self.assertIn('Stop', TestCommands.of(self.local()), 'another skill\'s hook still is')

    def test_a_skill_the_project_takes_over_later_loses_agentrcs_hook(self):
        self.ok('install', '--project', str(self.repo), '--skill')
        skill = self.repo / '.claude' / 'skills' / 'headless-chief'
        skill.unlink()
        skill.mkdir()
        (skill / 'SKILL.md').write_text('the project\'s own headless-chief')
        self.ok('install', '--project', str(self.repo), '--skill', '--skip-clashes')
        self.assertFalse((self.repo / '.claude' / 'hooks' / 'headless-chief').is_symlink(), 'the earlier link went')
        self.assertNotIn('PreToolUse', TestCommands.of(self.local()), 'and its registration')
        self.assertNotIn('/.claude/hooks/headless-chief', self.block())

    def test_a_skipped_skills_hook_of_the_projects_own_keeps_its_registration(self):
        skill = self.repo / '.claude' / 'skills' / 'headless-chief'
        skill.mkdir(parents=True)
        (skill / 'SKILL.md').write_text('the project\'s own headless-chief')
        self.git('add', '-A')
        self.git('commit', '-qm', 'own skill')
        own_hook = Path(self.tmp.name) / 'own-hook'
        own_hook.mkdir()
        hook = self.repo / '.claude' / 'hooks' / 'headless-chief'
        hook.parent.mkdir()
        os.symlink(own_hook, hook, target_is_directory=True)
        command = shlex.quote((hook / 'project_guard.py').as_posix())
        (self.repo / '.claude' / 'settings.local.json').write_text(json.dumps(
            {'hooks': {'PreToolUse': [{'matcher': 'Bash', 'hooks': [{'type': 'command', 'command': command}]}]}}))
        self.ok('install', '--project', str(self.repo), '--skill', '--skip-clashes')
        self.assertTrue(os.path.samefile(hook, own_hook), 'the project\'s hook link untouched')
        self.assertIn(command, TestCommands.of(self.local())['PreToolUse'], 'and its registration')

    def test_an_install_that_skips_every_entry_writes_nothing(self):
        for case in ('no settings yet', 'settings from an earlier install'):
            with self.subTest(case):
                self.tearDown()
                self.setUp()
                local = self.repo / 'CLAUDE.local.md'
                if case == 'settings from an earlier install':
                    self.ok('install', '--project', str(self.repo), '--claude-md')
                    local.unlink()
                local.write_text('the user\'s own')
                settings = self.repo / '.claude' / 'settings.local.json'
                before = self.snapshot(), settings.read_bytes() if settings.exists() else None
                self.assertIn('skipped', self.ok('install', '--project', str(self.repo), '--claude-md', '--skip-clashes'))
                self.assertEqual((self.snapshot(), settings.read_bytes() if settings.exists() else None), before,
                                 'no settings.local.json or exclude change')

    @unittest.skipIf(os.name == 'nt' or os.geteuid() == 0, 'needs a directory the user cannot write into')
    def test_an_exclude_file_in_a_read_only_info_directory_is_rewritten_in_place(self):
        info = self.exclude.parent
        info.chmod(0o555)
        try:
            self.ok('install', '--project', str(self.repo), '--workflow')
        finally:
            info.chmod(0o755)
        self.assertIn('/.claude/workflows/pr-babysit.js', self.block())
        self.assertEqual(self.status(), '')

    def test_a_malformed_block_refuses_before_any_change(self):
        start, end = install.BLOCK
        cases = {'a start without an end': [start, '/x'], 'an end without a start': ['/x', end],
                 'a repeated start': [start, start, '/x', end], 'an end before its start': [end, '/x', start],
                 'two blocks': [start, '/x', end, start, '/y', end]}
        for case, block in cases.items():
            with self.subTest(case):
                self.exclude.write_text('# the user\'s own\n' + ''.join(f'{line}\n' for line in block) + '*.after\n')
                before = self.snapshot()
                for action in ('install', 'remove'):
                    done = self.run_cli(action, '--project', str(self.repo), '--workflow')
                    self.assertEqual(done.returncode, 1, action)
                    self.assertIn(os.path.join('info', 'exclude'), done.stderr)
                self.assertEqual(self.snapshot(), before, 'no link made, no line lost')

    def test_the_exclude_file_is_read_as_git_numbers_its_lines(self):
        start, end = install.BLOCK
        rule = b'.claude/settings.local.json\n'  # the user's, after our block
        cases = {'characters str.splitlines also splits on': b'# own\nv\x0bt\x0cf\n',  # 2 lines more would make it ours
                 'bytes that are not UTF-8': b'# caf\xe9\n'}
        for case, own in cases.items():
            with self.subTest(case):
                self.tearDown()
                self.setUp()
                self.exclude.write_bytes(own + f'{start}\n/.claude/workflows/pr-babysit.js\n{end}\n'.encode() + rule)
                self.ok('install', '--project', str(self.repo), '--workflow')
                data = self.exclude.read_bytes()
                self.assertTrue(data.startswith(own) and rule[:-1] in data.split(b'\n'),
                                'the user\'s lines kept byte for byte')
                self.assertNotIn('/.claude/settings.local.json', self.block(), 'the user\'s rule already ignores it')
                self.assertEqual(self.status(), '')

    def test_an_entry_goes_once_no_work_tree_has_our_link_there(self):
        self.ok('install', '--project', str(self.repo), '--workflow')
        workflow = self.repo / '.claude' / 'workflows' / 'pr-babysit.js'
        workflow.unlink()
        os.symlink(Path(self.tmp.name) / 'own.js', workflow)  # the project's own link now
        self.ok('install', '--project', str(self.repo), '--skill')
        self.assertNotIn('/.claude/workflows/pr-babysit.js', self.block())
        self.assertIn('.claude/workflows/pr-babysit.js', self.status(), 'the project\'s own link shows')

    def test_a_project_install_changes_nothing_outside_the_work_tree_and_its_git_dir(self):
        other = Path(self.tmp.name) / 'other'
        other.mkdir()
        self.git('init', '-q', cwd=other)
        (other / 'file').write_text('x')

        def outside():
            return {str(p): p.read_bytes() if p.is_file() else None for p in Path(self.tmp.name).rglob('*')
                    if not p.is_relative_to(self.repo)}

        before = outside()
        self.ok('install', '--project', str(self.repo), '--skill', '--agent', '--workflow', '--claude-md')
        self.assertEqual(outside(), before)
        self.ok('remove', '--project', str(self.repo), '--skill', '--agent', '--workflow', '--claude-md')
        self.assertEqual(outside(), before)
        self.assertEqual(os.listdir(self.home), [])

    @unittest.skipUnless(os.name == 'nt', 'a junction is Windows\' own link')
    def test_a_junction_on_the_way_refuses_before_any_change(self):
        elsewhere = Path(self.tmp.name) / 'elsewhere'
        elsewhere.mkdir()
        subprocess.run(['cmd', '/c', 'mklink', '/J', str(self.repo / '.claude' / 'workflows'), str(elsewhere)],
                       check=True, capture_output=True)
        done = self.run_cli('install', '--project', str(self.repo), '--workflow')
        self.assertEqual(done.returncode, 1)
        self.assertIn('symlink or junction', done.stderr)
        self.assertEqual(os.listdir(elsewhere), [])

    def test_a_tracked_settings_local_file_is_found_in_any_case_where_git_ignores_case(self):
        if self.git('config', '--bool', '--default', 'false', 'core.ignorecase').strip() != 'true':
            self.skipTest('git matches names case-sensitively here')
        blob = subprocess.run(['git', 'hash-object', '-w', '--stdin'], cwd=self.repo, input='{"model": "team"}',
                              capture_output=True, text=True, check=True, env=self.env).stdout.strip()
        self.git('update-index', '--add', '--cacheinfo', f'100644,{blob},.CLAUDE/settings.local.json')  # tracked in other case
        (self.repo / '.claude' / 'settings.local.json').write_text('{"model": "team"}')
        done = self.run_cli('install', '--project', str(self.repo), '--workflow')
        self.assertEqual(done.returncode, 1, done.stdout)
        self.assertIn('tracked or a link', done.stderr)

    def test_each_run_in_one_process_reads_core_ignorecase_afresh(self):
        blob = self.git('hash-object', '-w', '--stdin', input='{"model": "team"}').strip()
        self.git('update-index', '--add', '--cacheinfo', f'100644,{blob},.CLAUDE/settings.local.json')
        err = io.StringIO()
        with mock.patch.dict(os.environ, self.env, clear=True), contextlib.redirect_stdout(io.StringIO()):
            self.git('config', 'core.ignorecase', 'false')
            install.main(['install', '--project', str(self.repo), '--workflow'])  # another name to git
            self.git('config', 'core.ignorecase', 'true')
            with contextlib.redirect_stderr(err), self.assertRaises(SystemExit):
                install.main(['install', '--project', str(self.repo), '--workflow'])
        self.assertIn('tracked or a link', err.getvalue())

    def test_a_tracked_or_linked_settings_local_file_refuses_before_any_change(self):
        def tracked(local, _):
            local.write_text('{"model": "team"}')
            self.git('add', '-A')
            self.git('commit', '-qm', 'tracked local settings')

        for case, arrange in {'tracked': tracked, 'a link': lambda local, elsewhere: os.symlink(elsewhere, local)}.items():
            with self.subTest(case):
                self.tearDown()
                self.setUp()
                local = self.repo / '.claude' / 'settings.local.json'
                elsewhere = Path(self.tmp.name) / 'settings.json'
                elsewhere.write_text('{}')
                arrange(local, elsewhere)
                before = self.snapshot()
                for action in ('install', 'remove'):
                    done = self.run_cli(action, '--project', str(self.repo), '--workflow')
                    self.assertEqual(done.returncode, 1, action)
                    self.assertIn('tracked or a link', done.stderr)
                self.assertEqual(self.snapshot(), before)
                self.assertEqual(elsewhere.read_text(), '{}', 'the file a link names is untouched')

    def test_a_symlinked_directory_on_the_way_refuses_before_any_change(self):
        elsewhere = Path(self.tmp.name) / 'elsewhere'
        elsewhere.mkdir()
        shutil.rmtree(self.repo / '.claude')
        os.symlink(elsewhere, self.repo / '.claude', target_is_directory=True)
        for action in ('install', 'remove'):
            done = self.run_cli(action, '--project', str(self.repo), '--workflow')
            self.assertEqual(done.returncode, 1, action)
            self.assertIn('is a symlink', done.stderr)
        self.assertEqual(os.listdir(elsewhere), [], 'nothing written where the link points')

    def test_pruning_leaves_a_tracked_link_alone(self):
        workflows = self.repo / '.claude' / 'workflows'
        workflows.mkdir()
        os.symlink(ROOT / 'workflows' / 'gone.js', workflows / 'gone.js')
        self.git('add', '-A')
        self.git('commit', '-qm', 'a tracked link into agentrc')
        out = self.ok('install', '--project', str(self.repo), '--workflow')
        self.assertIn('is tracked by the repository, left alone', out)
        self.assertTrue((workflows / 'gone.js').is_symlink())
        self.assertEqual(self.status(), '')

    def test_a_clashing_hook_is_skipped_with_its_registration(self):
        hook = self.repo / '.claude' / 'hooks' / 'headless-chief'
        hook.mkdir(parents=True)
        (hook / 'mine.py').write_text('')
        self.git('add', '-A')
        self.git('commit', '-qm', 'own hook')
        self.ok('install', '--project', str(self.repo), '--skill', '--skip-clashes')
        self.assertEqual(sorted(TestCommands.of(self.local())), ['Stop', 'UserPromptSubmit'], 'no PreToolUse guard')

    def test_a_dead_link_of_ours_is_pruned_with_its_entry(self):
        self.ok('install', '--project', str(self.repo), '--workflow')
        gone = self.repo / '.claude' / 'workflows' / 'gone.js'
        os.symlink(ROOT / 'workflows' / 'gone.js', gone)   # a workflow since removed from agentrc
        lines = self.exclude.read_text().splitlines()
        lines.insert(lines.index(install.BLOCK[1]), '/.claude/workflows/gone.js')
        self.exclude.write_text(''.join(f'{line}\n' for line in lines))
        self.ok('install', '--project', str(self.repo), '--workflow')
        self.assertFalse(gone.is_symlink())
        self.assertNotIn('/.claude/workflows/gone.js', self.block())
        self.assertEqual(self.status(), '')

    def test_another_agentrc_dir_refuses(self):
        claude = self.repo / '.claude'
        claude.joinpath('settings.local.json').write_text(json.dumps({'env': {'AGENTRC_DIR': '/other/agentrc'}}))
        done = self.run_cli('install', '--project', str(self.repo), '--workflow')
        self.assertEqual(done.returncode, 1)
        self.assertIn('another checkout', done.stderr)
        self.assertFalse((claude / 'workflows').exists())
        claude.joinpath('settings.local.json').write_text('{"env": []}')
        self.assertIn('fix it first', self.run_cli('install', '--project', str(self.repo), '--workflow').stderr)

    def test_user_wide_flags_and_a_directory_that_is_no_top_level_are_refused(self):
        for flag in ('--bin', '--statusline'):
            done = self.run_cli('install', '--project', str(self.repo), '--skill', flag)
            self.assertEqual(done.returncode, 2, flag)
            self.assertIn('user-wide', done.stderr)
        self.assertEqual(self.run_cli('install', '--skill', '--skip-clashes').returncode, 2)
        done = self.run_cli('install', '--project', str(self.repo / '.claude'), '--skill')
        self.assertEqual(done.returncode, 1)
        self.assertIn('not a work tree top level', done.stderr)
        plain = Path(self.tmp.name) / 'plain'
        plain.mkdir()
        self.assertEqual(self.run_cli('install', '--project', str(plain), '--skill').returncode, 1)
        self.assertEqual(self.run_cli('install', '--project', str(plain / 'missing'), '--skill').returncode, 1)
        self.assertEqual(os.listdir(self.home), [])
        self.assertFalse((self.repo / '.claude' / 'agents').exists())

    def test_a_project_hook_runs_from_its_settings_command(self):
        self.ok('install', '--project', str(self.repo), '--skill')
        command = TestCommands.of(self.local())['PreToolUse'][0]
        if os.name == 'nt':  # Claude Code runs it through bash
            done = subprocess.run([shutil.which('bash'), '-c', f"printf '%s\n' {command}"], capture_output=True, text=True)
            self.assertEqual(done.stdout.splitlines()[-1],
                             (self.repo / '.claude' / 'hooks' / 'headless-chief' / 'shadow_guard.py').as_posix())
        payload = json.dumps({'tool_name': 'Workflow', 'cwd': str(self.repo)})
        bash = shutil.which('bash') if os.name == 'nt' else '/bin/sh'
        done = subprocess.run([bash, '-c', command], input=payload, capture_output=True, text=True,
                              env={**self.env, 'CLAUDE_PROJECT_DIR': str(self.repo)})
        self.assertEqual(done.returncode, 0, done.stderr)


class TestCommands:
    @staticmethod
    def of(data):
        return {e: [h['command'] for g in gs for h in g['hooks']] for e, gs in data.get('hooks', {}).items()}


if __name__ == '__main__':
    unittest.main()
