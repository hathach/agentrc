import json
import tomllib
import unittest
from pathlib import Path

import yaml

AGENTS = Path(__file__).resolve().parents[1] / 'agents'


class AgentFiles(unittest.TestCase):
    def test_every_agent_names_itself_and_every_codex_adapter_has_its_md(self):
        mds = {p.stem for p in AGENTS.glob('*.md')}
        self.assertLessEqual({p.stem for p in AGENTS.glob('*.toml')}, mds, 'a toml without its md')
        for stem in mds:
            with self.subTest(stem):
                self.assertEqual(yaml.safe_load((AGENTS / f'{stem}.md').read_text().split('---')[1])['name'], stem)
                if (AGENTS / f'{stem}.toml').exists():  # the toml is optional: install.py links it when present
                    toml = tomllib.loads((AGENTS / f'{stem}.toml').read_text())
                    self.assertEqual(toml['name'], stem)
                    self.assertIn(f'~/.codex/agents/{stem}.md', toml['developer_instructions'])

    def test_pr_review_validator_keeps_the_keys_tinyusb_dismissals_are_keyed_on(self):
        """tinyusb's pr-babysit keys dismissal debt on findingId and detects
        edited comments through commentDigest; its tests read this file."""
        body = (AGENTS / 'pr-review-validator.md').read_text()
        example = json.loads(body.split('## Output contract')[1].split('\n\n')[2])
        finding = example['findings'][0]
        self.assertEqual(finding['findingId'], f"{finding['commentId']}#1")
        self.assertRegex(finding['commentDigest'], r'^[0-9a-f]{12}$')
        self.assertIn('`findingId` of `<commentId>#<n>`', body)
        self.assertIn('`commentDigest` of the first 12 hex characters of that body\'s sha256', body)


if __name__ == '__main__':
    unittest.main()
