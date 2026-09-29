"""Tests for listing and replaying read-doc lookup history."""
import copy
import json
import os
import shlex
import subprocess
import sys
import tempfile
import unittest
from pathlib import Path

if os.name == 'nt':
    raise unittest.SkipTest('read-doc requires POSIX file locking')


ROOT = Path(__file__).resolve().parents[1]
SCRIPTS = ROOT / "skills" / "read-doc" / "scripts"
HISTORY = Path("read-doc") / "lookups.jsonl"
sys.path.insert(0, str(ROOT / "tests"))
from test_read_doc import FIXTURE, fake_library  # noqa: E402


SEARCH = {
    "v": 1,
    "id": "111111111111",
    "ts": "2026-09-18T11:22:33Z",
    "host": "workstation",
    "elapsed_ms": 17,
    "library": "/library with spaces",
    "op": "search",
    "session": {"claude": "claude-session-one", "codex": None},
    "query": {"keywords": ["STM32 H7", "USB"], "any": True,
              "kind": "reference-manual", "limit": 7},
    "exit": 0,
    "error": None,
    "result": {"db_mtime_ns": 123, "total": 4,
               "kinds": {"reference-manual": 4},
               "books": [[41, "reference-manual", "RM one"],
                         [42, "reference-manual", "RM two"],
                         [43, "reference-manual", "RM three"],
                         [44, "reference-manual", "RM four"]]},
}

FIND = {
    "v": 1,
    "id": "222222222222",
    "ts": "2026-09-19T01:02:03Z",
    "host": "workstation",
    "elapsed_ms": 31,
    "library": "/calibre",
    "op": "find",
    "session": {"claude": None, "codex": "codex-thread-two"},
    "query": {"book": 41, "term": "SIE CTRL", "context": 3,
              "limit": 4, "offset": 2, "max_chars": 900},
    "exit": 0,
    "error": None,
    "result": {"source": {"path": "Vendor/book.pdf", "size": 99,
                           "mtime_ns": 456, "pages": 100},
               "total_hits": 5,
               "hits": [[7, "first"], [11, "second"], [20, "third"], [30, "fourth"]]},
}


class History(unittest.TestCase):
    def setUp(self):
        self.tmp = tempfile.TemporaryDirectory()
        self.addCleanup(self.tmp.cleanup)
        self.path = Path(self.tmp.name) / HISTORY
        self.env = dict(os.environ, XDG_STATE_HOME=self.tmp.name)

    def write(self, *records):
        self.path.parent.mkdir(parents=True, exist_ok=True)
        self.path.write_text("".join(json.dumps(record) + "\n" for record in records),
                             encoding="utf-8")

    def run_history(self, *args):
        return subprocess.run([sys.executable, str(SCRIPTS / "history.py"), *args],
                              capture_output=True, text=True, env=self.env)

    def test_list_sorts_and_filters_by_both_session_ids_term_and_date(self):
        older = copy.deepcopy(SEARCH)
        older["id"] = "000000000000"
        older["ts"] = "2026-09-17T23:59:59Z"
        older["session"] = {"claude": None, "codex": None}
        older["query"]["keywords"] = ["unrelated"]
        self.write(FIND, SEARCH, older)

        done = self.run_history("list")
        self.assertEqual(done.returncode, 0, done.stderr)
        lines = done.stdout.splitlines()
        self.assertEqual([line.split()[3] for line in lines],
                         [older["id"], SEARCH["id"], FIND["id"]])
        self.assertIn(" - search ", lines[0])
        self.assertIn(" claude-s search ", lines[1])
        self.assertIn(" books=RM one (41); RM two (42); RM three (43) exit=0", lines[1])
        self.assertIn(" codex-th find ", lines[2])
        self.assertIn('term="SIE CTRL" in book 41 pages=7,11,20 exit=0', lines[2],
                      'a record from before titles were logged')

        for args, expected in ((["--session", "claude-s"], SEARCH["id"]),
                               (["--session", "CODEX-TH"], FIND["id"]),
                               (["--term", "m32"], SEARCH["id"]),
                               (["--term", "ctrl"], FIND["id"]),
                               (["--since", "2026-09-19"], FIND["id"])):
            with self.subTest(args=args):
                filtered = self.run_history("list", *args)
                self.assertEqual(filtered.returncode, 0, filtered.stderr)
                self.assertIn(expected, filtered.stdout)
                self.assertEqual(len(filtered.stdout.splitlines()), 1)

        absent = self.run_history("list", "--term", "does not exist")
        self.assertEqual(absent.returncode, 1)
        self.assertIn("no matching lookup history", absent.stdout)

        titled = copy.deepcopy(FIND)
        titled["id"] = "333333333333"
        titled["result"]["title"] = "A reference manual whose title runs well past the cut"
        self.write(titled)
        self.assertIn('term="SIE CTRL" in A reference manual whose title runs wel… (41) pages',
                      self.run_history("list").stdout)

    def test_show_prints_the_full_record_and_exact_replay_argv(self):
        self.write(SEARCH, FIND)
        cases = (
            (SEARCH, ["python3", str(SCRIPTS / "search.py"), "--any", "--kind",
                      "reference-manual", "--limit", "7", "--", "STM32 H7", "USB"]),
            (FIND, ["python3", str(SCRIPTS / "locate.py"), "find", "--book", "41",
                    "--term=SIE CTRL", "--context", "3", "--limit", "4",
                    "--offset", "2", "--max-chars", "900"]),
        )
        for record, expected in cases:
            with self.subTest(op=record["op"]):
                done = self.run_history("show", record["id"])
                self.assertEqual(done.returncode, 0, done.stderr)
                rendered, command = done.stdout.rstrip().split("\n\n")
                self.assertEqual(json.loads(rendered), record)
                words = shlex.split(command)
                self.assertEqual(words[0], f"CALIBRE_LIBRARY={record['library']}")
                self.assertEqual(words[1:], expected)
                self.assertTrue(Path(words[2]).is_absolute())

    def test_show_command_repeats_a_real_logged_find(self):
        library = Path(self.tmp.name) / "library"
        library.mkdir()
        fake_library(str(library),
                     [(1, "RM0001 reference manual", ["reference-manual"], True)]).close()
        pdf = library / "Vendor" / "RM0001 reference manual (1)" / "RM0001 reference manual.pdf"
        pdf.write_bytes(FIXTURE.read_bytes())
        env = dict(self.env, CALIBRE_LIBRARY=str(library))
        env.pop("READ_DOC_HISTORY", None)
        invoke_main = ("import sys; sys.path.insert(0, sys.argv[1]); import locate; "
                       "raise SystemExit(locate.main(sys.argv[2:]))")
        # A term that looks like an option must replay as a term, not a usage error.
        for term, code in (("SIE_CTRL", 0), ("-SIE_CTRL", 1)):
            with self.subTest(term=term):
                original = subprocess.run(
                    [sys.executable, "-c", invoke_main, str(SCRIPTS), "find", "--book", "1",
                     "--term=" + term, "--context", "0", "--limit", "2"],
                    capture_output=True, text=True, env=env)
                self.assertEqual(original.returncode, code, original.stderr)
                record = json.loads(self.path.read_text().splitlines()[-1])

                shown = self.run_history("show", record["id"])
                self.assertEqual(shown.returncode, 0, shown.stderr)
                command = shown.stdout.rstrip().rsplit("\n\n", 1)[1]
                replay = subprocess.run(["sh", "-c", command], capture_output=True, text=True,
                                        env=env)
                self.assertEqual(replay.returncode, code, replay.stderr)
                self.assertEqual(replay.stdout, original.stdout)

    def test_bad_json_and_unknown_versions_are_reported_and_skipped(self):
        missing = copy.deepcopy(SEARCH)
        del missing["result"]
        no_limit = copy.deepcopy(FIND)
        del no_limit["query"]["limit"]
        self.path.parent.mkdir(parents=True)
        self.path.write_text('{broken\n' + json.dumps({"v": 99}) + "\n"
                             + json.dumps(missing) + "\n" + json.dumps(no_limit) + "\n"
                             + json.dumps(SEARCH) + "\n",
                             encoding="utf-8")
        done = self.run_history("list")
        self.assertEqual(done.returncode, 0, done.stderr)
        self.assertIn(SEARCH["id"], done.stdout)
        self.assertIn("line 1: malformed record", done.stderr)
        self.assertIn("line 2: malformed record: unknown version 99", done.stderr)
        self.assertIn("line 3: malformed record: 'result'", done.stderr)
        self.assertIn("line 4: malformed record: 'limit'", done.stderr)

    def test_missing_history_is_an_empty_history_not_unavailable(self):
        done = self.run_history("list")
        self.assertEqual(done.returncode, 1)
        self.assertIn("nothing was logged yet", done.stderr)

    def test_unreadable_history_and_bad_arguments_have_distinct_exits(self):
        self.path.mkdir(parents=True)
        unavailable = self.run_history("list")
        self.assertEqual(unavailable.returncode, 3)
        self.assertIn("history", unavailable.stderr)
        self.assertIn("unavailable", unavailable.stderr)

        bad_date = self.run_history("list", "--since", "18-09-2026")
        self.assertEqual(bad_date.returncode, 2)
        bad_id = self.run_history("show", "not-an-id")
        self.assertEqual(bad_id.returncode, 2)


if __name__ == "__main__":
    unittest.main()
