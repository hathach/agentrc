---
name: arduino
description: Shared mechanics of an Arduino library's build and HIL contracts (Adafruit TinyUSB Arduino, Adafruit SPIFlash) - arduino-cli builds against the checkout, prepared dependencies, the HIL build record, the HIL harness and its remote runs. Use when a library's build or HIL contract names it, when adding agent build/HIL support to another Arduino library, or when a record, dependency or identity refusal from one of those contracts needs explaining; also before changing those libraries' C/C++ (format rules).
---

# arduino — the shared Arduino library contract mechanics

An Arduino library has no build of its own: sketches compile against its checkout with arduino-cli. Each library
that has agent support keeps a build contract and a HIL contract; their mechanics live here, loaded from the agentrc
commit the library pins, and the library keeps only what is its own. Read the library's contracts (its `CLAUDE.md`
names them) for usage, exits and limits; this page is what they share and how a library adopts them.

| Script                      | Gives                                                                                                     |
|-----------------------------|-----------------------------------------------------------------------------------------------------------|
| `scripts/arduino_build.py`  | selection, compiles, dependency and header provenance, image span checks, the HIL build record            |
| `scripts/arduino_hil.py`    | record admission, flashing, DUT identity, the board lifecycle, manual sessions, commissioning, the report |
| `scripts/arduino_remote.py` | staging a run or a session on a rig (default ci.lan) and collecting it                                    |
| `scripts/arduino_ci.py`     | CI: a hosted runner's ci-arduino setup and packed tree per board, the rig runner's battery over them      |

Each script's docstring lists the profile names a library supplies. The `hil` skill's rules (locks, verdicts,
retries, recovery) apply unchanged; these scripts load its `hil_*` scripts through `hil_load.py`.

## A library's side

- `.claude/skills/hil-contract/scripts/agentrc_pin.py`: selects the agentrc the contracts load and loads the
  engines from it: `agentrc/` in a tree staged on a rig (its `manifest.json` names the commit); in CI (`GITHUB_ACTIONS`
  or `CI` true, any other value refused) the commit `test/hil/agentrc.json` pins (`{url, commit}`), fetched into
  `~/.cache/<library>/agentrc-<commit>`; otherwise the installed agentrc, `"${AGENTRC_DIR:-$HOME/.claude}"`, never
  fetched. It is the one piece each library copies: code cannot find itself. Its `ENGINES` closure is what a rig tree
  carries and what a record binds, with the commit of the one checkout the closure resolves into and whether the
  closure differs from it; it refuses engines whose `PROFILE_API` is not the library's before loading any.
- `.claude/skills/build/scripts/check_build.py`: the build profile (board table with CI's FQBNs from
  adafruit/ci-arduino `all_platforms.py` plus the rig boards, representatives per family, source classification,
  HIL sketches per role, VID:PID adapter, extra dependencies) and `main()`, which binds it to `arduino_build.py`.
- `.claude/skills/hil-contract/scripts/hil_test.py`: the HIL profile (tests and their host-side checks, restore test
  per role, report schema, roster checks) and `main()`; `bind()` refuses tables that disagree (the restore test must
  run the first HIL sketch).
  `hil_remote.py` is a wrapper around `arduino_remote.py`.
- Rosters `test/hil/ci.json` (the farm) and a gitignored `test/hil/local.json` (a dev PC's boards); contract tests in
  `.claude/test/`, run by pre-commit through `agentrc_pin.py test`. A farm board names its probe (`"probe"`, the `hil`
  skill's board registry), which supplies its flasher, under the registry's name, which is its board lock; a dev PC's
  unregistered board keeps a flasher of its own.

## What every library gets

- **Builds**: `arduino-cli compile --clean ... --library <checkout>`; the build fails when arduino-cli takes this
  library from anywhere else. Sketch eligibility follows ci-arduino's `.test.only`/`.test.skip`/`.skip.txt` rules; a
  HIL sketch outside `examples/` (`test/hil/<sketch>/`) builds only on rig boards whose role runs it.
- **Selection** from a diff: the profile's own rules first; `library.properties` is shared code (its `name=` and
  `depends=` gate every build). A changed example no board may build is uncovered; each library's contract tests pin
  `unbuildable_examples()`, the examples no build here compiles. A run given `--board` or `--example` is
  `narrowed: true` and, unless it writes a record, warns that the change's automatic selection was not verified: its
  pass is that command's, not the whole change's.
- **Dependencies**: library.properties `depends=` plus the profile's `EXTRA_DEPENDENCIES` (CI's install list) must be
  installed in the user's Arduino libraries before a build; a missing one is exit 2 naming the `arduino-cli lib
  install` line. Nothing installs during builds. Every library a compile used must be this checkout, a declared
  dependency or one of their transitive `depends=` from its resolved directory, one bundled with the core, or a
  prepared user copy shadowing a library the build's own core bundles (hashed before any compile and recorded with
  `shadows`); any other fails the sketch.
- **The HIL build record** (schema 2) binds a run to its firmware: HEAD and a clean tree, the FQBN, the core (`{id,
  version, install_dir}`, and `{commit, dirty}` when it is a git checkout: a dirty core's exact bytes are not
  identified), the resolved dependencies, the roster's sha256, the library's contract files and the agentrc engines'
  closure by sha256 with its commit (provenance: the digests decide), and per sketch its images, load spans, VID:PID
  and used libraries. The harness, and `hil_remote.py` before staging, refuse a record whose library, schema, contract
  files or agentrc files differ from the ones running: rebuild it. Each process freezes the agentrc closure's digests
  when it loads it and refuses to write, admit or stage a record once those files have changed under it; a manual
  session refuses records built with other agentrc files than the ones it opened with.
- **DUT identity**: the roster `uid` is the DUT's USB serial; `usb_port` additionally pins its bus port. `no_serial`
  (with `usb_port`) is for firmware that reports no serial (ESP32 under Arduino): the serial-less device at that port.
  `hil_test.py commission` finds and confirms either under the restoration sketch.
- **Probe detach**: before a flash or console reset through an openocd flasher whose args name `target/rp2040.cfg`
  or `target/rp2350.cfg`, the engine clears the USB pull-up so the host sees the new image enumerate.
- **Locks**: every library's harness takes the same physical board lock (`/tmp/tinyusb-hil-locks/<name>.lock`, the
  `hil` skill) and records unproven children under the one pending kind `arduino-hil`, so two libraries, and tinyusb
  or tinyuf2, exclude each other on a shared board, and one library's leftover children block the others.

## Code format

- The library's `.pre-commit-config.yaml` is authoritative: its clang-format hook (v15) on C, C++ and headers minus
  the hook's `exclude` list; CI runs the same hook. Format through it, never a `clang-format` from `PATH` (another
  version formats differently): `pre-commit run --files <changed files>` rewrites them in place and reports failed
  when it changed any; read the rewrite.
- Neither library has a `.clang-format`, so clang-format falls back to LLVM style; never add one or copy tinyusb's.
  clang-format takes the first `.clang-format` above the file, so format only in a checkout with none above its root
  (`~/Arduino/libraries/<lib>`, its `.worktrees/`): one nested under tinyusb or a core's `libraries/` (arduino-esp32
  has its own) formats in that tree's style, and v15 cannot parse tinyusb's file.
- TinyUSB Arduino's vendored TinyUSB (the paths its `CLAUDE.md` names) keeps tinyusb's format: fix it in
  hathach/tinyusb under tinyusb's `.clang-format`, then copy it back as that `CLAUDE.md` says.

## CI

`arduino_ci.py` (a library's `hil_ci.py` wraps it) splits a HIL run across GitHub runners: a hosted runner per board
sets the board up through adafruit/ci-arduino's own `install_library_deps()` and `install_platform()` (latest
releases, nothing pinned), builds the clean record and packs the tree `hil_remote.py` would stage, with a
`provenance.json` naming the run; the rig runner admits each packed tree against its run, head and roster and runs the
battery in it board by board, with a lock wait, one retry of a fail and a deadline, into a state dir kept across runs
(`summary.json` per run, finished clean runs pruned). The admission checks consistency, not trust: a pull request's
code, harness included, runs on the rig as the runner's user. The library's workflow and report comment are its own.

## Adding a library

1. Copy `agentrc_pin.py` from a library that has it, changing only its cache namespace, and pin a commit of this
   repository that has `skills/arduino/` for CI. Its `PROFILE_API` is `arduino_build.py`'s: a change to the profile
   names the engines read bumps it there and in every library's copy together.
2. Write the build profile: CI's matrix boards with their FQBNs, the rig boards it will run on (named as in agentrc's
   board registry, each with its `span`), representatives, classification of its `src/` tree, HIL sketches. Prove it
   with `check_build.py --select-only` on representative diffs and a full `--record` build per rig board.
3. Write dedicated HIL sketches, never examples, that end each exchange with a result line tied to the host's nonce,
   and a restoration sketch with no erase or write path, so the state a battery leaves survives it (name any
   side effect its init still has, as SPIFlash's `flash.begin()` setting a quad-enable bit). Write their host-side
   checks in `hil_test.py`.
4. Add the boards to `test/hil/ci.json`, commission each (`hil_test.py commission` on the board's host), run the
   battery on the farm (`hil_remote.py run`), and record what no board covers as the contract's Limits.
5. Contract tests for the profile in `.claude/test/`, a pre-commit hook running `agentrc_pin.py test` (the installed
   agentrc locally, the pin in CI) beside the clang-format hook (Code format), and a `CLAUDE.md` naming the build and
   HIL contracts.
