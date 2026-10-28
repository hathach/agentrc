"""How agentrc's own files name agentrc content: through `"${AGENTRC_DIR:-$HOME/.claude}/..."` (`.codex` for
Codex), which a project install points at this checkout, always quoted, and only at paths that exist."""
import functools
import re
import sys
import unittest
from pathlib import Path

sys.path.insert(0, str(Path(__file__).resolve().parent))
from helpers import guard_survivors  # noqa: E402

ROOT = Path(__file__).resolve().parents[1]
SCANNED = ('agents', 'skills', 'workflows', 'instructions', 'hooks', 'README.md')

# A literal user-wide path to agentrc content misses a project install. Claude state (~/.claude/projects,
# settings.json, transcripts) is no agentrc content and is not matched.
USER_WIDE = re.compile(r'(?<!:-)(?:~|\$HOME)/\.(?:claude|codex)/(?:skills|agents|workflows|hooks)/[^\s`\'")]*')
# allowed path -> the file prefixes it is allowed in ('' is every file)
ALLOWED = {
    # captain is user-wide only (README, Per project): captain_guard.py allows these literal paths and rejects any
    # `$` form, and captain.md's hook, its SKILL.md recipes and user.md's captain rule run them
    '/.claude/skills/captain/': ('',),
    # captain_guard.py's PUBLISHERS: the one followup-issue script captain runs, by the same literal-path rule
    '/.claude/skills/followup-issue/scripts/publish.py': ('skills/captain/', 'agents/captain.md'),
}

BASE = r'\$\{AGENTRC_DIR:-\$HOME/\.(?:claude|codex)\}'
REST = r'([^\s`\'")\]\\]+)'  # a TOML basic string escapes the closing quote
# the expansion, or a workflow's AGENTRC constant (its quoted shell text), and the path after it
NAMED = re.compile(rf'{BASE}"?/{REST}|\$\{{AGENTRC\}}/{REST}')
# a variable set to an agentrc path: S="${AGENTRC_DIR:-$HOME/.claude}/x" in a recipe, const S = `${AGENTRC}/x`
ASSIGNED = re.compile(rf'^\s*(?:const\s+)?([A-Za-z_]\w*)\s*=\s*["`]?(?:{BASE}|\$\{{AGENTRC\}})/([^\s"`\']+)', re.M)


def setUpModule():
    guard_survivors(__name__)


@functools.cache
def scanned():
    found = []
    for top in SCANNED:
        path = ROOT / top
        for f in [path] if path.is_file() else sorted(p for p in path.rglob('*') if p.is_file()):
            if '__pycache__' in f.parts:
                continue
            try:
                found.append((f.relative_to(ROOT).as_posix(), f.read_text(encoding='utf-8')))
            except UnicodeDecodeError:
                continue
    return tuple(found)


def line_of(text, at):
    return text.count('\n', 0, at) + 1


class AgentrcPaths(unittest.TestCase):
    def test_content_is_named_through_agentrc_dir(self):
        bad = [f'{name}:{line_of(text, m.start())}: {m[0]}'
               for name, text in scanned() for m in USER_WIDE.finditer(text)
               if not any(allowed in m[0] and name.startswith(files) for allowed, files in ALLOWED.items())]
        self.assertEqual(bad, [], 'name agentrc content as "${AGENTRC_DIR:-$HOME/.claude}/..." or allow it here')

    def test_every_expansion_is_quoted(self):
        """Prose too: a model copies a path into a command as written. In a Markdown recipe, a variable holding
        one is quoted where used; a workflow's `${S}` splices in shell text whose root is already quoted."""
        bad = [f'{name}:{line_of(text, m.start())}' for name, text in scanned()
               for m in re.finditer(r'(?<!")\$\{AGENTRC_DIR', text)]
        for name, text in scanned():
            if name.endswith('.md'):
                bad += [f'{name}:{line_of(text, m.start())}: {m[0]}' for var, _ in ASSIGNED.findall(text)
                        for m in re.finditer(rf'(?<!")\$\{{?{var}\b', text)]
        self.assertEqual(bad, [], 'quote the whole path: python3 "${AGENTRC_DIR:-$HOME/.claude}/skills/x/y.py"')

    def test_every_named_path_exists(self):
        """Directly, or through a variable set to an agentrc path in the same file (`$S/x.py`, `${S}/x.py`)."""
        bad = []
        for name, text in scanned():
            named = [(m.start(), m[1] or m[2]) for m in NAMED.finditer(text)]
            for var, base in ASSIGNED.findall(text):
                named += [(m.start(), f'{base}/{m[1]}') for m in re.finditer(rf'\$\{{?{var}\}}?/{REST}', text)]
            for at, path in named:
                path = path.rstrip('.,;:')
                if not any(c in path for c in '<*$') and not (ROOT / path).exists():
                    bad.append(f'{name}:{line_of(text, at)}: {path}')
        self.assertEqual(bad, [], 'a path under AGENTRC_DIR names this checkout\'s files')


if __name__ == '__main__':
    unittest.main()
