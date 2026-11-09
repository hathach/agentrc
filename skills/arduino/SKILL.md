---
name: arduino
description: Shared mechanics of an Arduino library's or core's build and HIL contracts (Adafruit TinyUSB Arduino, Adafruit SPIFlash, the Adafruit SAMD core) - arduino-cli builds against the checkout, prepared dependencies, the HIL build record, the HIL harness and its remote runs. Use when a library's or core's build or HIL contract names it, when adding agent build/HIL support to another Arduino library or core, or when a record, dependency or identity refusal from one of those contracts needs explaining; also before changing those libraries' C/C++ (format rules).
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
  closure by sha256 with its commit (provenance: the digests decide), and per sketch its images, load spans, VID:PID,
  foreign headers and used libraries. Without `--experimental`, a foreign header that differs from the checkout
  refuses the record unless it is the build's own core's copy: a core's bundled copy lags the library between core
  releases, so it is recorded and the report names it (`firmware.driftedHeaders`), results covering that exact mix
  and no ABI verdict beyond it.
  The harness, and `hil_remote.py` before staging, refuse a record whose library, schema, contract files or agentrc
  files differ from the ones running: rebuild it. Each process freezes the agentrc closure's digests
  when it loads it and refuses to write, admit or stage a record once those files have changed under it; a manual
  session refuses records built with other agentrc files than the ones it opened with.
- **DUT identity**: the roster `uid` is the DUT's USB serial; `usb_port` additionally pins its bus port. `no_serial`
  (with `usb_port`) is for firmware that reports no serial (ESP32 under Arduino): the serial-less device at that port.
  `hil_test.py commission` finds and confirms either under the restoration sketch.
- **Probe detach**: before a flash or console reset through an openocd flasher whose args name `target/rp2040.cfg`
  or `target/rp2350.cfg`, the engine clears the USB pull-up so the host sees the new image enumerate.
- **Locks**: every library's harness takes the same physical board lock (`/tmp/tinyusb-hil-locks/<name>.lock`, the
  `hil` skill), so two libraries, and tinyusb or tinyuf2, exclude each other on a shared board. A run's or
  commission's lock ends with it: a cleanup it could not prove (a failed restore, children not proven ended) is a
  blocker, logged in the board's cleanup log with the board released, and the next run on the board, whatever
  library, reports it (`previousCleanupFailure`). A manual session's detached hold stays until `restore --release`,
  `release` or `session close`; a session command that leaves a child unproven, or a restore that fails, logs the
  failure under it. A passing restore marks restored each open failure whose children and retained
  sessions are proven ended. `restore --release` releases the hold whatever the restore did: exit 0 restored, 6
  restore failed (released), 5 not released; `session close` counts 6 closed. An older engine's pending record
  (kind `arduino-hil`) is reported (`previousPending`), never a refusal.
- **Claims** (agentrc#467): a run, commission or session command claims its board (kind `arduino-hil-claim`, the
  `hil` skill's Board locks) after its lock or hold check and before its first tool. Another live invocation on the
  board refuses it (a run's row `locked`, a session command's exit 2). An earlier invocation that died with children
  not proven ended is logged in the board's cleanup log once and reported (`previousCleanupFailure`); a child of it
  still running (`pending`) gets 30 s to end, else the attempt is refused the same way, its lock released, and every
  later attempt is refused until that child ends; an `unknown` one never holds the board. Another OS user's earlier
  invocation, whose files this one may not read, counts as running while `/proc` shows one of its supervisors or may
  hide one, and the refusal names who can clear it. A clean end removes its claim before the release; one that left a
  child unproven keeps it. `claimNote` reports a claim not removed or a failure not logged.

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

## A core

An Arduino core (a platform checkout such as adafruit/ArduinoCore-samd) uses the same engines in core mode: its build
profile names `PLATFORM` (`vendor:arch`) and the other optional names `arduino_build.py`'s docstring lists. What
changes from a library:

- **Builds** compile through an isolated arduino-cli environment per run (`build/.arduino-cli-<pid>/`): a config
  whose sketchbook links `hardware/<vendor>/<arch>` to the checkout and holds only the prepared dependencies, the
  user's data dir (toolchains) unchanged. A build whose platform resolves anywhere but the checkout fails, so neither
  an installed copy of the core nor a `~/Arduino/hardware` link to another checkout is ever tested by mistake.
- **Libraries the core bundles** are its own: a user copy of one is never prepared and fails a build that used it
  (library mode admits such a shadow). Dependencies are the profile's `EXTRA_DEPENDENCIES` and their `depends=`.
- **Sketches** are every `*.ino` dir under `SKETCH_ROOTS` (the bundled libraries, `extras/` included, as a core's
  `tools/build_all.py` scans), eligible by that script's `.all.test.skip` / `.<platform>.test.skip` /
  `.<platform>.test.only` markers and the profile's `exclude()`; they build under their path, since bundled libraries
  share example names. A changed sketch file selects that sketch before the profile's rules.
- **Selection** adds two effects for `classify_source`: `boards` (every eligible sketch on the named boards, e.g. a
  `boards.txt` or `platform.txt` change on CI's matrix) and `sketches` (the named sketches on every representative,
  e.g. a bundled library's source).
- **One rig board, several menus**: the profile's `fqbn(board, example)` gives a HIL sketch its own FQBN (a USB stack
  menu); the record keeps each sketch's FQBN and the report names them (`firmware.fqbns`).
- **Identity**: a record also binds the submodules (`git submodule status --recursive`) and refuses one that is not
  initialised, not at its gitlink or dirty (each one's own worktree, at any depth, whatever its `ignore` setting),
  `--experimental` included: a record's patch carries no submodule bytes. A record also refuses any untracked file.
  Foreign headers are those from another copy of the core (by `platform.txt` `name=`).
- **Base image** (feature `base`): a rig board whose sketches run only above other firmware (an nRF52's MBR,
  SoftDevice, bootloader and its settings) names those HEX files as `BOARDS[...]['base']`, with the address ranges
  never touched as `base_exclude` (UICR). The record binds the files and their runs, which must lie outside the
  board's span; the harness admits them with the record and, before every J-Link flash (battery, commissioning, a
  manual flash or restore), verifies each run in place. Only a `verifybin` of the check's own script that read the
  target after a clean `halt` and found it different is a difference: then the runs are rewritten and verified, with
  no reset or go before the sketch's flash, and the row's `base` says `matched` or which run differed. A check that
  proves nothing (no connection, a failed or partial read, a timeout) writes nothing and fails the step. A rewrite
  may erase the rest of each flash page a run touches, so those pages must be the base's. Only runs are checked: a
  word the boot reads that the HEX leaves unprogrammed (the MBR's bootloader address at 0xFF8) is a run of its
  erased value in a profile HEX, or another image's bytes there pass as `matched`. The board's budget and each step's
  reserve grow by a check and a rewrite. Through J-Link only: a record refuses a base on any other flasher.

## CI

`arduino_ci.py` (a library's `hil_ci.py` wraps it) splits a HIL run across GitHub runners: a hosted runner per board
sets the board up through adafruit/ci-arduino's own `install_library_deps()` and `install_platform()` (latest
releases, nothing pinned; ci-arduino and its arduino-cli kept out of the checkout's status), builds the clean record
and packs the tree `hil_remote.py` would stage, with a `provenance.json` naming the run; the rig runner admits each
packed tree against its run, head and roster and runs the battery in it board by board, with a lock wait, one retry of
a fail and a deadline, into a state dir kept across runs (`summary.json` per run, finished clean runs pruned). The
admission checks consistency, not trust: a pull request's code, harness included, runs on the rig as the runner's
user. The library's workflow and report comment are its own.

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

A core follows the same steps with its core-mode profile; its `agentrc_pin.py` copy also refuses engines without
core mode (`'core'` missing from `arduino_build.PROFILE_FEATURES`), and one whose rig board has a base image without
`'base'`, since `PROFILE_API` stays 1 for it.
