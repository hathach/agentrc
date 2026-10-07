---
name: read-pcb
description: Use when a board-wiring question should be answered from the schematic, not memory or a pinout image — which MCU pin or GPIO drives a signal, what sits on a net, what a connector, jumper or switch connects, a part's value, MPN or package, per revision — for boards in hathach/pcb, READ_PCB_SOURCES repos or a design directory the user names (KiCad/EAGLE), including while writing firmware for it.
---

# Read PCB

Answer from the design source. `scripts/pcb.py` owns the mechanics: finding
schematics, reading EAGLE XML, getting KiCad connectivity from `kicad-cli`,
and printing a `source:` line to cite. Run it as
`python3 <skill dir>/scripts/pcb.py` from any checkout; below, `pcb.py`
stands for that, and `pcb.py --help` lists the commands.

## Sources

- `hathach/pcb`: the user's boards, KiCad, the default source.
- `READ_PCB_SOURCES` lists the source repos as `owner/repo`, separated like
  `PATH`, in place of that default. On the user's PC it adds their other board
  repos; a session without it sees `hathach/pcb` only.
- Any location the user names, such as `~/code/jtrace/metro_m7_1011_trace`:
  `find --in PATH`, then query its files by filesystem path. It is not
  remembered; the user names it again next time.

On the user's PC, `READ_PCB_CLONES` lists its working trees of those repos,
and answers come from them. Elsewhere, as in a cloud session, answers
come from a cache the script fetches on every command. Where GitHub access is
SSH only, set `READ_PCB_REMOTE_BASE=git@github.com:` before the first use; an
existing cache keeps its origin, so delete `~/.cache/read-pcb/<owner>/<repo>`
to switch.

## Steps

1. **Find the schematic.** `pcb.py find WORD...` with words of the board name.
   Use the revision the question names; when it names none and the
   revisions may differ on the point, ask the user or answer per revision.
2. **Query.** `nets SCH PATTERN` to discover a net's name, `net SCH NAME` for
   what sits on it, `part SCH REF` for a part's pins and the net on each,
   `parts SCH WORD...` to find a part. Follow a signal hop by hop,
   `net` -> `part` -> `net`: a resistor, jumper or switch joins two
   different nets, and a switch's position or off-board wiring needs other
   evidence. The NeoPixel on a Feather: `nets SCH NEO`, then `net SCH NEOPIX`
   shows the MCU pin.
3. **Cite.** Quote the `source:` line with the refdes and net names. `@ sha`
   is the commit the bytes read were compared with; `working tree clean`
   means they match it. `modified`, `untracked` or `not in HEAD` means the
   answer includes uncommitted edits, and `provenance unavailable` means git
   could not tell, so say which. An `inputs` note gives the same state for
   each KiCad sub-sheet that is not clean. A file outside git has no sha:
   cite its path. Combine answers from several queries when they cite the
   same sha and each is clean or cached; otherwise re-run them together.

## Reading the output

- A terminal is `REF PIN (NAME)  pad P  value  device`. An EAGLE pin shows
  its gate (`1.D`) when the pin name repeats across the part's gates.
- KiCad pins are numbers with the pin name in parentheses; net names carry
  sheet paths (`/NEOPIX`). KiCad's export leaves power symbols off nets, so
  `net GND` lists the real parts only.
- `[no-connect]` is an explicit no-connect flag. `(no net)` is a pin wired to
  nothing.
- Values are the schematic's base values. `[DNP in variant X]` marks a part
  not populated in that named variant, not in the built board.
- A part's own behaviour (registers, electrical limits) comes from its
  datasheet through `read-doc`, by value or MPN.

## Exit codes

`pcb.py --help` lists them. Report a 3 with its message: the script refused
because the answer would not hold.
