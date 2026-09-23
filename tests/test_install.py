import json
import os
import subprocess
import sys
import tempfile
import unittest
from pathlib import Path

ROOT = Path(__file__).resolve().parents[1]
SCRIPT = ROOT / 'install.py'
SKILLS = sorted(p.name for p in (ROOT / 'skills').iterdir() if p.is_dir())
SKILL = SKILLS[0]


class InstallTest(unittest.TestCase):
    def setUp(self):
        self.tmp = tempfile.TemporaryDirectory(dir=ROOT.parent if os.name == 'nt' else None)
        self.home = Path(self.tmp.name)
        self.claude, self.codex = self.home / '.claude', self.home / '.codex'

    def tearDown(self):
        self.tmp.cleanup()

    def run_cli(self, *argv):
        return subprocess.run([sys.executable, str(SCRIPT), *argv], capture_output=True, text=True,
                              env={**os.environ, 'HOME': str(self.home), 'USERPROFILE': str(self.home)})

    def ok(self, *argv):
        done = self.run_cli(*argv)
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

    # --- install --------------------------------------------------------------

    def test_skills_link_into_both_cli_dirs_and_nothing_else(self):
        out = self.ok('install', '--skill')
        for d in (self.claude / 'skills', self.codex / 'skills'):
            self.assertTrue(os.path.samefile(d / SKILL, ROOT / 'skills' / SKILL))
            self.assertEqual(sorted(p.name for p in d.iterdir()), SKILLS)
        self.assertEqual(out.count('->'), 2 * len(SKILLS) + 1, 'and the one hook')
        self.assertFalse((self.claude / 'CLAUDE.md').exists())
        self.assertFalse((self.claude / 'agents').exists())
        self.assertEqual(self.ok('install', '--skill'), '', 'a rerun is silent')

    def test_each_flag_covers_its_kind_and_a_skill_brings_its_hook(self):
        self.ok('install', '--skill', '--agent', '--workflow', '--claude-md')
        self.assertEqual(sorted(p.name for p in (self.codex / 'skills').iterdir()), SKILLS)
        self.assertTrue(os.path.samefile(self.claude / 'agents' / 'pvs-studio.md', ROOT / 'agents' / 'pvs-studio.md'))
        self.assertEqual(sorted(p.name for p in (self.codex / 'agents').iterdir()),
                         sorted(p.name for p in (ROOT / 'agents').iterdir()), 'every agent md and toml')
        self.assertTrue(os.path.samefile(self.claude / 'hooks' / 'simplify-gate', ROOT / 'hooks' / 'simplify-gate'))
        self.assertTrue(os.path.samefile(self.claude / 'workflows' / 'fix-issue.js', ROOT / 'workflows' / 'fix-issue.js'))
        self.assertTrue(os.path.samefile(self.claude / 'workflows' / 'code-audit.js', ROOT / 'workflows' / 'code-audit.js'))
        self.assertFalse((self.codex / 'workflows').exists(), 'workflows are Claude only')
        self.assertTrue(os.path.samefile(self.claude / 'CLAUDE.md', ROOT / 'CLAUDE.md'))
        self.assertTrue(os.path.samefile(self.codex / 'AGENTS.md', ROOT / 'CLAUDE.md'))
        self.assertEqual((self.codex / 'AGENTS.md').read_text(), (ROOT / 'CLAUDE.md').read_text())
        self.assertEqual(self.ok('install', '--claude-md'), '', 'relative Codex link is idempotent outside HOME')

    def test_a_skill_hook_is_registered_once_with_absolute_quoted_commands_and_older_entries_replaced(self):
        old = str(ROOT / 'hooks' / 'simplify-gate')  # the pre-folder install pointed straight into the repo
        (self.claude).mkdir()
        (self.claude / 'settings.json').write_text(json.dumps({'model': 'x', 'hooks': {
            'SessionStart': [{'matcher': '*', 'hooks': [{'type': 'command', 'command': 'other'}]}],
            'PreToolUse': [{'matcher': 'Bash', 'hooks': [{'type': 'command', 'command': old}]}],
            'Stop': [{'hooks': [{'type': 'command', 'command': 'other-stop'}, {'type': 'command', 'command': old}]}]}}))
        self.ok('install', '--skill')
        launcher = str(self.claude / 'hooks' / 'simplify-gate' / 'simplify-gate')
        data = self.settings()
        self.assertEqual(data['model'], 'x')
        self.assertEqual(self.commands(data), {'SessionStart': ['other'], 'Stop': ['other-stop', launcher],
                                               'UserPromptSubmit': [launcher]})
        stop = data['hooks']['Stop'][-1]['hooks'][0]
        self.assertEqual(stop['timeout'], 650)
        self.assertNotIn('matcher', data['hooks']['Stop'][-1])
        self.assertEqual(json.loads((self.claude / 'settings.json.before-agentrc').read_text())['hooks']['PreToolUse'][0]['hooks'][0]['command'], old)
        self.assertEqual(self.ok('install', '--skill'), '', 'idempotent')
        self.assertEqual(self.settings(), data)

    def test_a_hook_path_with_a_space_is_quoted(self):
        home = self.home / 'my home'
        home.mkdir()
        done = subprocess.run([sys.executable, str(SCRIPT), 'install', '--skill'],
                              capture_output=True, text=True,
                              env={**os.environ, 'HOME': str(home), 'USERPROFILE': str(home)})
        self.assertEqual(done.returncode, 0, done.stderr)
        data = json.loads((home / '.claude' / 'settings.json').read_text())
        launcher = str(home / '.claude' / 'hooks' / 'simplify-gate' / 'simplify-gate')
        want = subprocess.list2cmdline([launcher]) if os.name == 'nt' else f"'{launcher}'"
        self.assertEqual(data['hooks']['Stop'][0]['hooks'][0]['command'], want)

    def test_a_dead_link_of_ours_is_pruned_and_foreign_links_and_content_stay(self):
        (self.claude / 'skills').mkdir(parents=True)
        os.symlink(ROOT / 'skills' / 'gone', self.claude / 'skills' / 'gone')
        os.symlink('/nonexistent/elsewhere', self.claude / 'skills' / 'foreign')
        (self.claude / 'skills' / 'mine').mkdir()
        (self.claude / 'skills' / 'mine' / 'SKILL.md').write_text('x')
        self.ok('install', '--skill')
        names = sorted(p.name for p in (self.claude / 'skills').iterdir())
        self.assertEqual(names, sorted(SKILLS + ['foreign', 'mine']))

    def test_the_whole_dir_link_of_an_earlier_installer_is_replaced_only_when_skills_are_selected(self):
        self.claude.mkdir()
        os.symlink(ROOT / 'skills', self.claude / 'skills')
        self.ok('install', '--agent')
        self.assertTrue((self.claude / 'skills').is_symlink(), 'an agent install leaves the skills alone')
        self.ok('install', '--skill')
        self.assertFalse((self.claude / 'skills').is_symlink())
        self.assertTrue((self.claude / 'skills' / SKILL).is_symlink())

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

    # --- refusals ---------------------------------------------------------------

    def test_refuses_before_touching_anything(self):
        cases = {
            'content at a destination': lambda: ((self.claude / 'skills' / SKILL).mkdir(parents=True),
                                                 (self.claude / 'skills' / SKILL / 'own').write_text('x')),
            'a file where a link dir goes': lambda: (self.claude.mkdir(), (self.claude / 'agents').write_text('x')),
            'a dangling link dir': lambda: (self.claude.mkdir(), os.symlink('/nonexistent', self.claude / 'hooks')),
            'a real CLAUDE.md': lambda: (self.claude.mkdir(), (self.claude / 'CLAUDE.md').write_text('mine')),
        }
        for case, arrange in cases.items():
            with self.subTest(case):
                self.tearDown()
                self.setUp()
                arrange()
                before = sorted(str(p) for p in self.home.rglob('*'))
                done = self.run_cli('install', '--skill', '--agent', '--workflow', '--claude-md')
                self.assertEqual(done.returncode, 1, case)
                self.assertIn('move it aside first', done.stderr)
                self.assertEqual(sorted(str(p) for p in self.home.rglob('*')), before, 'nothing changed')

    # --- remove -----------------------------------------------------------------

    def test_remove_unlinks_whatever_the_link_points_to_but_never_content(self):
        self.ok('install', '--skill', '--agent', '--workflow')
        os.remove(self.codex / 'skills' / SKILL)
        os.symlink('/somewhere/else', self.codex / 'skills' / SKILL)
        (self.claude / 'agents' / 'pvs-studio.md').unlink()
        (self.claude / 'agents' / 'pvs-studio.md').write_text('my own copy')
        out = self.ok('remove', '--skill', '--agent', '--workflow')
        self.assertFalse((self.claude / 'skills' / SKILL).exists())
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


if __name__ == '__main__':
    unittest.main()
