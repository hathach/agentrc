---
name: arduino
description: Shared mechanics of an Arduino library's build and HIL contracts (Adafruit TinyUSB Arduino, Adafruit SPIFlash) - arduino-cli builds against the checkout, prepared dependencies, the HIL build record, the HIL harness and its remote runs. Use when a library's build or HIL contract names it, when adding agent build/HIL support to another Arduino library, or when a record, dependency or identity refusal from one of those contracts needs explaining.
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

Each script's docstring lists the profile names a library supplies. The `hil` skill's rules (locks, verdicts,
retries, recovery) apply unchanged; these scripts load its `hil_*` scripts through `hil_load.py`.

## A library's side

- `.claude/skills/hil-contract/scripts/agentrc_pin.py`: fetches the pinned commit (`test/hil/agentrc.json`, `{url,
  commit}`) into `~/.cache/<library>/agentrc-<commit>` and loads the engines from it, or from `agentrc/` in a tree
  staged on a rig. It is the one piece each library copies: code cannot fetch itself. Its `ENGINES` closure is what a
  rig tree carries and what a record binds.
- `.claude/skills/build/scripts/check_build.py`: the build profile (board table with CI's FQBNs from
  adafruit/ci-arduino `all_platforms.py` plus the rig boards, representatives per family, source classification,
  HIL sketches per role, VID:PID adapter, extra dependencies) and `main()`, which binds it to `arduino_build.py`.
- `.claude/skills/hil-contract/scripts/hil_test.py`: the HIL profile (tests and their host-side checks, restore test
  per role, report schema, roster checks) and `main()`; `bind()` refuses tables that disagree (the restore test must
  run the first HIL sketch).
  `hil_remote.py` is a wrapper around `arduino_remote.py`.
- Rosters `test/hil/ci.json` (the farm) and a gitignored `test/hil/local.json` (a dev PC's boards); contract tests in
  `.claude/test/`, run by pre-commit after `agentrc_pin.py fetch`.

## What every library gets

- **Builds**: `arduino-cli compile --clean ... --library <checkout>`; the build fails when arduino-cli takes this
  library from anywhere else. Sketch eligibility follows ci-arduino's `.test.only`/`.test.skip`/`.skip.txt` rules; a
  HIL sketch outside `examples/` (`test/hil/<sketch>/`) builds only on rig boards whose role runs it.
- **Dependencies**: library.properties `depends=` plus the profile's `EXTRA_DEPENDENCIES` (CI's install list) must be
  installed in the user's Arduino libraries before a build; a missing one is exit 2 naming the `arduino-cli lib
  install` line. Nothing installs during builds. Every library a compile used must be this checkout, a declared
  dependency or one of their transitive `depends=` from its resolved directory, one bundled with the core, or a
  prepared user copy shadowing a library the build's own core bundles (hashed before any compile and recorded with
  `shadows`); any other fails the sketch.
- **The HIL build record** (schema 2) binds a run to its firmware: HEAD and a clean tree, the FQBN, the core
  (`{id, version, install_dir}`, and `{commit, dirty}` when it is a git checkout: a dirty core's exact bytes are not
  identified), the resolved dependencies, the roster's sha256, the library's contract files and the agentrc engines'
  closure by sha256, and per sketch its images, load spans, VID:PID and used libraries. The harness, and
  `hil_remote.py` before staging, refuse a record whose library, schema, contract files or agentrc files differ from
  the ones running: rebuild it.
- **DUT identity**: the roster `uid` is the DUT's USB serial; `usb_port` additionally pins its bus port. `no_serial`
  (with `usb_port`) is for firmware that reports no serial (ESP32 under Arduino): the serial-less device at that port.
  `hil_test.py commission` finds and confirms either under the restoration sketch.
- **Probe detach**: before a flash or console reset through an openocd flasher whose args name `target/rp2040.cfg`
  or `target/rp2350.cfg`, the engine clears the USB pull-up so the host sees the new image enumerate.
- **Locks**: every library's harness takes the same physical board lock (`/tmp/tinyusb-hil-locks/<board>.lock`, the
  `hil` skill) and records unproven children under the one pending kind `arduino-hil`, so two libraries, and tinyusb
  or tinyuf2, exclude each other on a shared board, and one library's leftover children block the others.

## Adding a library

1. Copy `agentrc_pin.py` from a library that has it, changing only its cache namespace, and pin a commit of this
   repository that has `skills/arduino/`.
2. Write the build profile: CI's matrix boards with their FQBNs, the rig boards it will run on (names as on tinyusb's
   farm roster, each with its `span`), representatives, classification of its `src/` tree, HIL sketches. Prove it
   with `check_build.py --select-only` on representative diffs and a full `--record` build per rig board.
3. Write dedicated HIL sketches, never examples, that end each exchange with a result line tied to the host's nonce,
   and a restoration sketch with no erase or write path, so the state a battery leaves survives it (name any
   side effect its init still has, as SPIFlash's `flash.begin()` setting a quad-enable bit). Write their host-side
   checks in `hil_test.py`.
4. Add the boards to `test/hil/ci.json`, commission each (`hil_test.py commission` on the board's host), run the
   battery on the farm (`hil_remote.py run`), and record what no board covers as the contract's Limits.
5. Contract tests for the profile in `.claude/test/`, a pre-commit hook that fetches the pin and runs them, and a
   `CLAUDE.md` naming the build and HIL contracts.
