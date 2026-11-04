# Build contract

agentrc has no compiled build: a change is verified by the whole test suite, which also runs the workflow harnesses
under node. Any scope, docs included, runs the same command from the checkout's top level:

```bash
mkdir -p "$HOME/.cache/agentrc-tmp" && TMPDIR="$HOME/.cache/agentrc-tmp" python -X utf8 -m pytest -n auto --dist worksteal -p no:cacheprovider -q tests
```

- `TMPDIR` on disk: `/tmp` is a small tmpfs on the maintainer's hosts and the suite fills it. On Windows drop the
  `mkdir`/`TMPDIR` part; the default `TEMP` is on disk.
- Needs `git`, `node` and a CPython 3.12+ with PyYAML, pytest and pytest-xdist (the versions `.pre-commit-config.yaml`
  pins, or the distribution's packages). Setup checks them with
  `python -X utf8 -c 'import yaml, pytest, xdist'`: a host without them is not set up, which is no verdict on a
  change. About a minute with 16 workers (`-n auto` counts physical cores where psutil imports). The serial
  `python -X utf8 -m unittest discover -s tests` runs the same tests many times slower and is no verification, nor is
  a path or `-k` after `tests`: only quicker checks while working.
- `buildOk` is true only when the command exits 0 and its last line reports `passed` with no `N failed` or `N error(s)`
  count (`xfailed` and `xpassed` are not failures).
  `notes` quotes that line (`N passed, M skipped ... in Ts`); on a failure, the first failing test's id and its error.
- Never pipe it through `tail` or another filter: the pipeline's status is the filter's, not the suite's.
