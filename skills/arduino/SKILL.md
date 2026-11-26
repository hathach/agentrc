---
name: arduino
description: Shared mechanics of an Arduino library's or core's build and HIL contracts (Adafruit TinyUSB Arduino, Adafruit SPIFlash, the Adafruit SAMD core) - arduino-cli builds against the checkout, prepared dependencies, the HIL build record, the HIL harness and its remote runs. Use when a library's or core's build or HIL contract names it, when adding agent build/HIL support to another Arduino library or core, or when a record, dependency or identity refusal from one of those contracts needs explaining; also before changing those libraries' C/C++ (format rules).
---

# arduino — the shared Arduino library contract mechanics

An Arduino library has no build of its own: sketches compile against its checkout with arduino-cli. Each library
that has agent support keeps a build contract and a HIL contract; their mechanics live here, loaded from the agentrc
commit the library pins, and the library keeps only what is its own. Read the library's contracts (its `AGENTS.md`
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

- `.claude/skills/hil-contract/scripts/agentrc_pin.py`: a verbatim copy of `scripts/agentrc_pin.py`, configured by
  `test/hil/agentrc.json`. It selects the staged tree, CI pin or installed checkout, in that order. Its docstring
  defines selection, configuration and commands; `hil`'s `agentrc_source.py` owns closure identity and drift.
  `agentrc_pin.py test` refuses a copy that differs from the selected canonical file, naming the copy/repin remedy,
  then a consumer that fails its conformance check (`scripts/pin_conformance.py`'s `problems()` docstring, loaded
  from the selected closure). A consumer's own tests keep only its configuration and profile wiring.
  A consumer reads its own sources itself (`ast`), never through bootstrap internals: conformance refuses any
  `agentrc_pin` name outside `pin_conformance.py`'s `PUBLIC` (the docstring's list plus `ci()`).
- `.claude/skills/build/scripts/check_build.py`: the build profile (board table with CI's FQBNs from
  adafruit/ci-arduino `all_platforms.py` plus the rig boards, representatives per family, source classification,
  HIL sketches per role, VID:PID adapter, extra dependencies) and `main()`, which binds it to `arduino_build.py`.
- `.claude/skills/hil-contract/scripts/hil_test.py`: the HIL profile (tests and their host-side checks, restore test
  per role, report schema, roster checks) and `main()`; `bind()` refuses tables that disagree (the restore test must
  run the first HIL sketch).
  `hil_remote.py` is a wrapper around `arduino_remote.py`.
- Rosters `test/hil/ci.json` (the farm) and a gitignored `test/hil/local.json` (a dev PC's boards); contract tests in
  `.claude/test/`, run by pre-commit through `agentrc_pin.py test`. `check_build.py` runs the profile's
  `CONTRACT_TESTS` entry `agentrc_pin` the same way, the bootstrap's `test` from the checkout root, so its copy and
  conformance checks run there too; every other entry runs its own script (`arduino_build.py`'s `run_tests`). A farm
  board names its probe (`"probe"`, the `hil` skill's board registry), which supplies its flasher, under the registry's
  name, which is its board lock; a dev PC's unregistered board keeps a flasher of its own.

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
  `shadows`); any other fails the sketch. A dependency binds its directory as `arduino_build.py`'s `tree_digest()`
  documents, and a change to it during the run fails it.
- **The HIL build record**: the rig's installed core need not match or exist; HIL validates the recorded images.
  Git-checkout cores are content-bound, other cores have id/version provenance only. The report's `modified` label
  means only a recorded `dirty: true`; `arduino_hil.py`'s `core_provenance()` renders a malformed field as `?` or
  `malformed`, never as a claim. `arduino_build.py`'s `platform_git()` and `content_digest()` document the binding
  limits. Rebuild a refused record. After an agentrc upgrade, close and reopen a session before staging
  records from the new closure; closing still uses its original staged restoration image and harness.
- **Core TinyUSB** (informational): each build row's `coreTinyUSB` lists, per copy of the TinyUSB an ESP32 core
  bundles (`include/arduino_tinyusb`), the headers the build's `.d` files took from it, its `tusb_option.h` version
  and the lib-builder's `versions.txt` `tinyusb:` ref (null when unreadable). Nothing admits or refuses on it.
- **DUT identity**: the roster `uid` is the DUT's USB serial; `usb_port` additionally pins its bus port. `no_serial`
  (with `usb_port`) is for firmware that reports no serial (ESP32 under Arduino): the serial-less device at that port.
  `hil_test.py commission` finds and confirms either under the restoration sketch.
- **Identity per USB stack**: an ESP32-S3's TinyUSB and USB-Serial/JTAG share one PHY and bus port but report
  different serials, so a roster `uids` (`{"<USBMode option>": "<serial>"}`, distinct serials, `uid` among them, no
  `no_serial`) gives each; admission picks the one a sketch's recorded FQBN `USBMode` names and refuses an image it
  names none for. Without `uids`, `uid` identifies only images built with the rig FQBN's `USBMode`; another is
  refused. Each serial is the one observed on the board under that image, never derived from another. An
  `openocd-esp32` debug route on the DUT's own `usb_port` needs a `uids` entry naming its `debug.uid`, and `gdb`
  refuses a sketch whose image enumerates as another; a route on a port of its own (the P4's) runs under any image.
- **Probe detach**: before a flash or console reset through an openocd flasher whose args name `target/rp2040.cfg`
  or `target/rp2350.cfg`, the engine clears the USB pull-up so the host sees the new image enumerate. A console
  reset, with no record's VID:PID and no roster `usb_port`, waits only on the ports carrying the DUT's serial before
  the detach; a DUT found at none fails the reset without issuing the probe detach or reset.
- **Locks**: every library's harness takes the same physical board lock (`/tmp/tinyusb-hil-locks/<name>.lock`, the
  `hil` skill), so two libraries, and tinyusb or tinyuf2, exclude each other on a shared board. A run's or
  commission's lock ends with it: a cleanup it could not prove (a failed restore, children not proven ended) is a
  blocker, logged in the board's cleanup log with the board released, and the next run on the board, whatever
  library, reports it (`previousCleanupFailure`). A manual session's detached hold stays until `restore --release`,
  `release` or `session close`; a session command that leaves a child unproven, or a restore that fails, logs the
  failure under it. A passing restore marks restored each open failure whose children and retained
  sessions are proven ended. `restore --release` releases the hold whatever the restore did: exit 0 restored, 6
  restore failed (released), 5 not released; `session close` counts 6 closed, and on a closed session prints its
  recorded exits without the rig (exit 0). After a failed close (`restoreExit` 6, hold released), recovery takes a new
  hold, first stops or reports every child the session left unproven, then restores with the session's record
  (`restoreRecord`), never with the session's `result.json` as `--report`. An older engine's pending record
  (kind `arduino-hil`) is reported (`previousPending`), never a refusal.
- **Busy check**: a run or commission is one `hil_lock.board_task` whose busy check, under its lock and before any tool,
  is agentrc `hil_flash.probe_users` on the board's probe; its docstring has what it covers (the probe's serial on a
  command line, holders of its usbfs node or esptool tty) and misses (GDB clients, DUT-side tools, anything started
  after the check). A probe in use refuses with nothing touched: a run's row `locked`, naming the users in `probeUsers`,
  and a commission's exit 2, neither logging a failure; a check that cannot tell refuses the same way (a run's row not
  run, `refused: probe check`). A session command refused either way exits 2 with nothing touched, though a refused
  `restore` is logged in the board's cleanup log as any refused restore is, and `restore --release` still releases the
  hold and exits 6, or 5 when the release fails. A run's or commission's refusal whose lock did not end cleanly is a
  blocker (exit 4) instead. Session commands under one hold run one at a time, from any tree or OS user, through the
  hold's session mutex (`hil_lock.session_mutex`), taken before the hold is checked, and run the same busy check
  themselves. A run's cleanup verdict is failed (logged, a blocker) unless its cleanup passed, then restored when a test
  ran and its restore passed (open failures whose evidence is resolved marked restored), else clean; a commission that
  passes is a restoration. `cleanup.lock` (a commission's `lock`) names a lock that did not end as its verdict said: a
  blocker.

## Code format

- The library's `.pre-commit-config.yaml` is authoritative: its clang-format hook (v15) on C, C++ and headers minus
  the hook's `exclude` list; CI runs the same hook. Format through it, never a `clang-format` from `PATH` (another
  version formats differently): `pre-commit run --files <changed files>` rewrites them in place and reports failed
  when it changed any; read the rewrite.
- Neither library has a `.clang-format`, so clang-format falls back to LLVM style; never add one or copy tinyusb's.
  clang-format takes the first `.clang-format` above the file, so format only in a checkout with none above its root
  (`~/Arduino/libraries/<lib>`, its `.worktrees/`): one nested under tinyusb or a core's `libraries/` (arduino-esp32
  has its own) formats in that tree's style, and v15 cannot parse tinyusb's file.
- TinyUSB Arduino's vendored TinyUSB (the paths its `AGENTS.md` names) keeps tinyusb's format: fix it in
  hathach/tinyusb under tinyusb's `.clang-format`, then copy it back as that `AGENTS.md` says.

## A core

An Arduino core (a platform checkout such as adafruit/ArduinoCore-samd) uses the same engines in core mode: its build
profile names `PLATFORM` (`vendor:arch`) and the other optional names `arduino_build.py`'s docstring lists. What
changes from a library:

- **Builds** compile through an isolated arduino-cli environment per run (`build/.arduino-cli-<pid>/`): a config
  whose sketchbook links `hardware/<vendor>/<arch>` to the checkout and holds only the prepared dependencies, the
  user's data dir (toolchains) unchanged. A build whose platform resolves anywhere but the checkout fails, so neither
  an installed copy of the core nor a `~/Arduino/hardware` link to another checkout is ever tested by mistake.
  Each compile appends `-ffile-prefix-map=<that dir>=.arduino-cli` to the FQBN's resolved
  `compiler.{c,cpp,S}.extra_flags` (the platform's and `--build-property` values kept), so an image embedding
  `__FILE__` names no PID and two builds of one commit match; the core's compilers must be GCC 8 or later.
- **Libraries the core bundles** are its own: a user copy of one is never prepared and fails a build that used it
  (library mode admits such a shadow). Dependencies are the profile's `EXTRA_DEPENDENCIES` and their `depends=`.
- **Sketches** are every `*.ino` dir under `SKETCH_ROOTS` (the bundled libraries, `extras/` included, as a core's
  `tools/build_all.py` scans), eligible by that script's `.all.test.skip` / `.<platform>.test.skip` /
  `.<platform>.test.only` markers and the profile's `exclude()`; they build under their path, since bundled libraries
  share example names. A changed sketch file selects that sketch before the profile's rules (in a library too).
- **Selection** adds two effects for `classify_source`: `boards` (every eligible sketch on the named boards, e.g. a
  `boards.txt` or `platform.txt` change on CI's matrix) and `sketches` (the named sketches on every representative,
  e.g. a bundled library's source).
- **One rig board, several menus**: the profile's `fqbn(board, example)` gives a HIL sketch its own FQBN (a USB stack
  menu); the record keeps each sketch's FQBN and the report names them (`firmware.fqbns`). A named build (feature
  `named`) builds one sketch dir under several names: a `HIL_SKETCHES` entry `{name: (sketch dir, 'k=v,...')}` sets
  those menu options on the board's FQBN, and the name keys its build dir, `fqbn()`, its record sketch (which also
  names its `source`) and the test that runs it; a change to the dir builds every name it has. `bind()` refuses a name
  that is no plain file name, an id listed twice in a role and one defined two ways across roles.
- **Identity**: a record also binds the submodules (`git submodule status --recursive`) and refuses one that is not
  initialised, not at its gitlink or dirty (each one's own worktree, at any depth, whatever its `ignore` setting),
  `--experimental` included: a record's patch carries no submodule bytes. A record also refuses any untracked file
  (a library: under `src/`, `examples/` or `test/hil/`; either can set its own `SOURCE_DIRS`).
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

`arduino_ci.py` (a library's `hil_ci.py` wraps it) splits a run between hosted builds and the farm's binary-only
battery. Its docstring defines setup, packing, admission, lock waits, retries, deadlines and pruning. Packed trees
pin the inner `agentrc/manifest.json` in the outer manifest; old bootstraps retain the outer commit/dirty identity.
Admission checks consistency, not trust: a pull request's code, harness included, runs as the rig runner's user.
The library's workflow and report comment are its own.

## Adding a library

1. Copy `skills/arduino/scripts/agentrc_pin.py` verbatim to
   `.claude/skills/hil-contract/scripts/agentrc_pin.py` and configure `test/hil/agentrc.json` as its docstring
   specifies. Put both files in the profile's `CONTRACT_FILES` so staged trees carry them. Copy and repin together;
   keep profile/config tests in the consumer, with common bootstrap behavior and the staged round trip (`test`'s
   conformance check) in agentrc.
2. Write the build profile: CI's matrix boards with their FQBNs, the rig boards it will run on (named as in agentrc's
   board registry, each with its `span`), representatives, classification of its `src/` tree, HIL sketches. Prove it
   with `check_build.py --select-only` on representative diffs and a full `--record` build per rig board.
3. Write dedicated HIL sketches, never examples, that end each exchange with a result line tied to the host's nonce,
   and a restoration sketch with no erase or write path, so the state a battery leaves survives it (name any
   side effect its init still has, as SPIFlash's `flash.begin()` setting a quad-enable bit). Write their host-side
   checks in `hil_test.py`. A core reuses the shared battery instead (feature `battery`): its `core_cdc` and
   `core_timing` sketches answer `arduino_hil.py`'s INFO / ECHO / TIME / DELAY protocol (its docstring), its profile
   names them in `CORE_TESTS` with the stack INFO reports, sets `ECHO_LENGTHS` and its tolerances, and writes only
   its own checks in `TEST_FUNCS`, built from the same pieces (an nRF52's RTOS check after `core_timing_check`).
4. Add the boards to `test/hil/ci.json`, commission each (`hil_test.py commission` on the board's host), run the
   battery on the farm (`hil_remote.py run`), and record what no board covers as the contract's Limits.
5. Contract tests for the profile in `.claude/test/`, a pre-commit hook running `agentrc_pin.py test` (the installed
   agentrc locally, the pin in CI) beside the clang-format hook (Code format).
6. Agent files with no tracked symlink: arduino-lint LS005 keeps a library release holding one out of the Library
   Manager, and a core keeps the libraries' layout. A regular `AGENTS.md` names the build and HIL contracts and, in an
   "Agent files" section, says the skills are in `.claude/skills/<name>/SKILL.md`; `CLAUDE.md` holds only
   `@AGENTS.md`; no `.agents`. Copy SPIFlash's `no-symlinks` pre-commit hook unchanged. `install.py --project` links
   are untracked, so they never reach a release.

A core follows the same steps with its core-mode profile. Set `features` to include `core`, plus `base` for a rig
board's base image, `named` for named HIL builds and `battery` for `CORE_TESTS`. These are additive capabilities of
profile API 1, checked before any engine executes. A consumer that runs the shared report commenter adds
`skills/hil/scripts/gh_pr_comment.py` to `extra_engines`; one with a `hil_ci.py` wrapper also adds
`skills/arduino/scripts/arduino_ci.py`. Every entry point loaded through the bootstrap must belong to its closure.
