# Build contract

agentrc has no compiled build: a change is verified by the whole test suite, which also runs the workflow harnesses
under node. Any scope, docs included, runs the same command from the checkout's top level:

```bash
mkdir -p "$HOME/.cache/agentrc-tmp" && TMPDIR="$HOME/.cache/agentrc-tmp" python -X utf8 -m unittest discover -s tests
```

- `TMPDIR` on disk: `/tmp` is a small tmpfs on the maintainer's hosts and the suite fills it. On Windows drop the
  `mkdir`/`TMPDIR` part; the default `TEMP` is on disk.
- Needs `git`, `node` and a CPython 3.12+ (`README.md`'s prerequisites). It takes about 12 minutes; a module or test
  name after `discover -s tests` is no verification of a change, only a quicker check while working.
- `buildOk` is true only when the command exits 0 and its last line starts with `OK`. `notes` quotes the `Ran N tests`
  line and the `OK (skipped=N)` or `FAILED (...)` line; on a failure, the first failing test's name and its error.
- Never pipe it through `tail` or another filter: the pipeline's status is the filter's, not the suite's.
