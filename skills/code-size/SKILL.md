---
name: code-size
description: Use when firmware's flash/RAM footprint must be measured or compared - sizing a project's builds per file, section or symbol (report), diffing them against a base ref to judge a change's size impact (diff), or reading or debugging a PR's "Code size" comment (CI snapshot/compare) - in a repository whose build contract has a Code size section (tinyusb, tinyuf2).
---

# Code Size

`scripts/code_size.py` is the engine; a project runs it through its own launcher with its profile,
both named by the `## Code size` section of the skill or document its `CLAUDE.md`'s
`Build contract:` line names. Read that section first: it gives the launcher, the default engine
and board, what is sized, and the project's limits. No section means the project has not adopted
this skill: measure nothing rather than compose a build. The engine's module docstring defines the
commands, the engines and the profile protocol; `<launcher> <command> --help` the flags.

`report` sizes the working tree, uncommitted changes included. `diff` sizes the working tree against
a base ref (default the profile's `BASE_BRANCH`), pairs elfs by board and elf path, and reports
per-file deltas. Pick the narrowest scope that exercises the change; sizes are never summed or
averaged across boards, examples or images.

| Scope                       | Command                                  |
|-----------------------------|------------------------------------------|
| one board (one example)     | `<launcher> diff -b BOARD [-e EXAMPLE]`  |
| several boards, combined    | `<launcher> diff -b B1 -b B2 --combined` |
| CI's board set, combined    | `<launcher> diff --ci` (when offered)    |
| one tree, no diff           | `<launcher> report -b BOARD`             |

The base comes from the CI snapshots the project stores for the base commit, or its nearest stored
ancestor (the `Base:` line names the commit used and how far back it is; changes after it count as
yours), when the profile offers them; otherwise, or when `--base-source local`, `-f`, `--bloaty` or
another `--engine` needs an elf, the base is built locally in a detached worktree under the output
dir, and the `Base:` line says why. `--base-source ci` fails instead of falling back.
`--base-branch` means its tip; pass a merge-base SHA to diff against that. A run holds
`<out dir>/.lock` and refuses while another run holds it; it removes only a base worktree an earlier
run of its own left (owner marker plus git registration), and refuses any other occupant.

## Engines

- `membrowse`: symbols of `membrowse report`, attributed to source files; needs the profile's link
  inputs and the membrowse version CI pins (the engine's `MEMBROWSE_CI_VERSION`).
- `linkermap`: the GNU ld map's input sections, when the profile names linkermap.
- `bloaty`: DWARF compile units, with `bloaty` on PATH. An LTO build (`<artificial>` unit) has no
  per-file attribution: the filter check fails, reported, never zero.
- `sections`: the elf's allocated sections, each a row, with its Flash/RAM buckets as columns; no
  filter, no tool. Its total is the elf's allocated-section Flash/RAM: what a size budget is
  usually about, but not the image length or a region's occupancy.

Flash/RAM buckets come from the elf's section and program headers for every engine (a section
loaded in flash and run in RAM counts in both). Each other engine's whole-elf total sums what it
attributes, so compare totals only within one engine. `--symbols` when Flash/RAM cancel: a pair
counts as changed when its sections or symbols moved.

## Results

Reports go under the project's output dir, `<board>/report*.md` or `<board>/diff*.md`, combined
diffs under `_combined/`; `--json` adds the data beside each. A command exits nonzero on a build or
sizing failure or when no project file matched; `report` also when it sized no elf, `diff` when it
compared no pair. A scope the board does not build is skipped: no report and no failure. Unknown
boards, and an example in neither tree, are refused before anything builds. A failed build prints
an excerpt; its report records the first error.

Each report opens with a coverage line. `INCOMPLETE` means a build, measurement or filter match
failed, or an elf exists on one side only; those elfs are listed and excluded, never counted as
zero. Show the coverage line and the relevant tables, then:

- A many-elf report summarizes each elf, a many-pair diff each file; per-elf tables are in the
  `.md`'s `<details>`. Diff statistics cover compared pairs only: `Changed / present` is the pairs
  whose Flash or RAM of the file changed over the compared pairs containing it. min > 0 means growth
  in every such pair, max > 0 in at least one; name the worst-growth pair.
- A whole-elf Δ with a zero project-file Δ (`filtered` under a custom `-f`) is code outside the
  filter (inlined headers, vendor or board code) or an attribution difference; check the per-file
  table for rows that cancel, and the `sections` engine for the whole-elf view.
- A local base symlinks or clones its dependencies as the project's section says; a dependency
  bump the base does not build at its own pin is not in the diff.
- Corroborate a surprising delta on its own board, not the whole sweep: another `--engine`, plus
  `--bloaty` if bloaty is on PATH.

A diff rebuilds every tree it builds on each run, so ask for `--symbols` or `--bloaty` in the same
run, and run a long sweep in the background.

On Windows run the launcher as `python -X utf8` (or with `PYTHONUTF8=1`), else redirected output
fails on `Δ`. A non-ASCII checkout, CMake or toolchain path needs ninja >= 1.11 whose
`ninja -t wincodepage` reports UTF-8, and a local base symlinks its dependencies: enable Developer
Mode unless running elevated.

## CI snapshots

A project whose profile stores snapshots runs `snapshot` in each CI build leg (one
`code-size-<board>.json` per board, plus `leg.json`) and `compare` on two runs' downloaded
artifacts, which writes the full report, its JSON and the comment body. CI never rebuilds for
sizing. Its comment lists every changed project file with its min and max Flash and RAM Δ across
the builds containing it; `Not compared:` is the full report's `INCOMPLETE`. Compiler and
membrowse version differences are noted, not failed. The project's section says how its workflow
selects the baseline run, posts the comment and how to reproduce one.
