#!/usr/bin/env python3
"""Analyze an etm_capture.py output dir: hot functions, coverage, itrace digest,
and ISR episode timing.

Input:  code_profile.txt (Ozone Export.CodeProfile text report), and optionally
        itrace.csv (Export.Trace raw instruction history) with --elf for
        address->function mapping (arm-none-eabi-nm).
Output: markdown report on stdout.

--isr SYM[,SYM..]  episode timing for an interrupt handler: first symbol is the
        entry anchor (its first instruction marks each ISR entry), all symbols
        form the body address set. Example: --isr OTG_HS_IRQHandler,dcd_int_handler
        Durations are calibrated against a periodic handler the firmware runs:
        --tick-symbol names it and --tick-hz gives its rate (e.g. SysTick_Handler
        1000). Both come from the firmware, neither is guessed; the itrace must be
        captured with timestamps enabled. Check both entries in the ELF's
        disassembly: the --isr entry instruction must execute once per invocation
        (a loop head placed there splits one invocation into several episodes),
        and the tick handler must return to other code between beats (short, not
        tail-chained into itself).
"""

import argparse
import bisect
import csv
import math
import os
import re
import statistics
import subprocess
import sys


def parse_profile(path):
    """Parse the two report sections. Function rows are indented 2 spaces under
    a non-indented module row; columns are '|'-separated."""
    lines = open(path, errors="replace").read().splitlines()
    try:
        cov_start = lines.index("Code Coverage Summary")
        prof_start = lines.index("Code Profile Summary")
    except ValueError:
        sys.exit(f"error: {path} is not an Ozone code-profile text report")

    def rows(section):
        module = None
        for ln in section:
            if "|" not in ln:
                continue
            cells = ln.split("|")
            name = cells[0].rstrip()
            if (not name or name.startswith("Module/Function")
                    or set(name.strip()) <= {"-", "+"}):
                continue
            if not name.startswith("  "):
                module = name.strip()
                continue
            yield module, name.strip(), cells[1:]

    def num(s):
        s = s.strip().replace(" ", "")
        return int(s) if s else 0

    cov_pat = re.compile(r"^\s*([\d ]+)/\s*([\d ]+)\s+([\d.]+)%")
    funcs, totals = {}, {}
    for module, name, cells in rows(lines[prof_start:]):
        run, fetch = (num(cells[0]) if cells else 0), (num(cells[1]) if len(cells) > 1 else 0)
        if name == "Total":
            totals["run"], totals["fetch"] = run, fetch
        elif name == "[Unaccounted]":
            totals["unaccounted"] = fetch
        elif name in funcs and funcs[name]["module"] != module:
            # same-named static from another module: keep both rows distinct
            funcs[f"{name} [{module}]"] = {"module": module, "run": run,
                                           "fetch": fetch}
        else:
            funcs[name] = {"module": module, "run": run, "fetch": fetch}
    for module, name, cells in rows(lines[cov_start:prof_start]):
        m_src = cov_pat.match(cells[0]) if cells else None
        m_inst = cov_pat.match(cells[1]) if len(cells) > 1 else None
        if name == "Total" and m_inst and m_src:
            totals["src_cov"] = num(m_src.group(1)), num(m_src.group(2))
            totals["inst_cov"] = num(m_inst.group(1)), num(m_inst.group(2))
        elif m_inst:
            # match the de-collided key when a same-named static from another
            # module was renamed during the profile pass
            key = name if (name in funcs and funcs[name]["module"] == module) \
                else f"{name} [{module}]"
            if key in funcs:
                funcs[key]["inst_pct"] = float(m_inst.group(3))
    return funcs, totals


def load_symbols(elf):
    """Sorted (addr, size, name) from nm; for mapping itrace addresses."""
    nm = os.environ.get("ETM_NM") or "arm-none-eabi-nm"
    try:
        out = subprocess.run([nm, "-S", "--defined-only", "-C", elf],
                             capture_output=True, text=True)
    except OSError as e:
        sys.exit(f"error: cannot run {nm}: {e} - install it or set ETM_NM=<path>")
    if out.returncode != 0:
        sys.exit(f"error: {nm} failed on {elf}: {out.stderr.strip()}")
    syms = []
    for ln in out.stdout.splitlines():
        parts = ln.split(maxsplit=3)
        if len(parts) == 4 and parts[2].lower() in ("t", "w"):
            syms.append((int(parts[0], 16) & ~1, int(parts[1], 16), parts[3]))
        elif len(parts) == 3 and parts[1].lower() in ("t", "w"):
            # sizeless symbol (e.g. weak asm stub): assume a 2-byte body
            syms.append((int(parts[0], 16) & ~1, 2, parts[2]))
    return sorted(syms)


def addr_to_func(syms, addr):
    i = bisect.bisect_right(syms, (addr, 1 << 62, "")) - 1
    if i >= 0 and syms[i][0] <= addr < syms[i][0] + syms[i][1]:
        return syms[i][2]
    return None


def sym_ranges(syms, names):
    """(lo, hi) address ranges for the named symbols (base name match);
    None if any symbol is missing (caller degrades gracefully). A name that
    matches several addresses (same-named statics) is refused: picking one
    would time the wrong function."""
    out = []
    for want in names:
        hits = sorted({(a, a + sz) for a, sz, n in syms
                       if n == want or n.split("(")[0] == want})
        if len(hits) > 1:
            sys.exit(f"error: symbol '{want}' is ambiguous in the ELF: "
                     + ", ".join(f"0x{lo:08x}" for lo, _ in hits))
        if not hits:
            print(f"note: symbol '{want}' not found in ELF")
            return None
        out.append(hits[0])
    return out


def iter_itrace(path):
    """Yield (t_raw, addr) chronologically-reversed (file order: newest first).
    Also returns the timestamp unit from the header via generator .send? No -
    caller reads unit separately with itrace_unit()."""
    with open(path, newline="", errors="replace") as f:
        rd = csv.reader(f)
        hdr = next(rd, None) or []
        # --no-timestamps captures drop the Timestamp column entirely: locate
        # the Address column from the header and yield t=None for such rows
        # (consumers count instructions but skip time math)
        has_ts = any("Timestamp" in c for c in hdr)
        try:
            addr_i = next(i for i, c in enumerate(hdr) if "Address" in c)
        except StopIteration:
            addr_i = 1 if has_ts else 0
        for row in rd:
            if not row or len(row) <= addr_i:
                continue
            try:
                addr = int(row[addr_i], 16)
            except ValueError:
                continue
            if has_ts and row[0] != "PC":
                try:
                    yield float(row[0]), addr
                    continue
                except ValueError:
                    pass
            yield None, addr


def itrace_unit(path):
    hdr = open(path, errors="replace").readline()
    m = re.search(r"Timestamp\[([^\]]+)\]", hdr)
    return m.group(1) if m else "?"


def time_by_func(path, syms):
    """Per-function raw-time and instruction attribution from the itrace.
    Rows are newest-first; the gap to the next (older) row is attributed to the
    older instruction's function. Outlier gaps (trace-block boundaries) are
    capped so one discontinuity cannot skew a function. Shares are scale-free."""
    sample = []
    t_prev = None
    for t, _ in iter_itrace(path):
        if t is None:
            continue
        if t_prev is not None and t_prev - t > 0:
            sample.append(t_prev - t)
            if len(sample) >= 200000:
                break
        t_prev = t
    cap = 10000 * statistics.median(sample) if sample else float("inf")
    t_prev = None
    tf, cf = {}, {}
    n = 0
    for t, a in iter_itrace(path):
        n += 1
        fn = addr_to_func(syms, a)
        if fn:
            cf[fn] = cf.get(fn, 0) + 1
        if t is None:
            continue
        if t_prev is not None:
            d = t_prev - t
            if 0 < d < cap and fn:
                tf[fn] = tf.get(fn, 0) + d
        t_prev = t
    return tf, cf, n


def isr_report(itrace, elf, isr_arg, tick_symbol, tick_hz):
    if not tick_symbol:
        print("\n## ISR timing: unavailable - trace timestamps are seconds only as "
              "far as the capture's core clock was right; pass --tick-symbol and "
              "--tick-hz to calibrate them")
        return
    syms = load_symbols(elf)
    names = [s.strip() for s in isr_arg.split(",") if s.strip()]
    body = sym_ranges(syms, names)
    tick = sym_ranges(syms, [tick_symbol])
    if not body or not tick:
        print("\n## ISR timing: skipped (missing symbols above - needs the ISR "
              f"symbol(s) and {tick_symbol} for calibration)")
        return
    entry = body[0][0]
    tick_entry = tick[0][0]
    unit = itrace_unit(itrace)

    usb_rows, starts, tmin, tmax = [], [], None, None

    def in_tick(a):
        return any(lo <= a < hi for lo, hi in tick)

    def take(row, older_addr):
        t, a = row
        if any(lo <= a < hi for lo, hi in body):
            usb_rows.append((t, a))
        # independent, not elif: the ISR under test may be the tick handler itself.
        # A beat is the entry instruction reached from outside the handler: a
        # compiler may put a loop head there, and those re-executions are not beats
        if a == tick_entry and older_addr is not None and not in_tick(older_addr):
            starts.append(t)

    newer = None
    for t, a in iter_itrace(itrace):
        if t is None:
            continue  # --no-timestamps capture: the too-few-beats message below applies
        tmin = t if tmin is None else min(tmin, t)
        tmax = t if tmax is None else max(tmax, t)
        if newer:
            take(newer, a)
        newer = (t, a)
    if newer:
        take(newer, None)
    usb_rows.reverse()
    starts.reverse()
    # a beat is one execution of the handler's first instruction; fewer than
    # three leaves no period to take a median of
    if len(starts) < 3:
        print(f"\n## ISR timing: not enough {tick_symbol} beats for calibration "
              f"({len(starts)}) - capture with timestamps enabled, for longer "
              f"than a few periods of {tick_hz:g} Hz")
        return

    # median, not mean: one trace-overflow gap must not stretch the period
    raw_period = statistics.median(b - a for a, b in zip(starts, starts[1:]))
    if raw_period <= 0:
        print(f"\n## ISR timing: {tick_symbol} beats carry no usable timestamps")
        return
    raw_s = raw_period * tick_hz
    gap = 30e-6 * raw_s
    edge = 50e-6 * raw_s

    def episodes(rows, g):
        out, cur = [], []
        for t, a in rows:
            if cur and t - cur[-1][0] > g:
                out.append(cur)
                cur = []
            cur.append((t, a))
        if cur:
            out.append(cur)
        return out

    def local_scale(t):
        i = bisect.bisect_left(starts, t)
        if 0 < i < len(starts):
            sp = starts[i] - starts[i - 1]
            # a lost beat doubles the interval: that is not clock drift to follow
            if 0.8 * raw_period < sp < 1.2 * raw_period:
                return 1 / (tick_hz * sp)
        return 1 / raw_s

    # body rows resuming after a gap without passing the entry belong to the
    # episode before the gap (preempted, or running outside the body symbols):
    # its visible part is a prefix, not a duration
    eps, interrupted, open_ep = [], 0, False
    for cluster in episodes(usb_rows, gap):
        if cluster[0][1] != entry and open_ep:
            eps.pop()
            interrupted += 1
        cur = None
        for t, a in cluster:
            if a == entry:
                if cur:
                    eps.append(cur)
                cur = [(t, a)]
            elif cur:
                cur.append((t, a))
        if cur:
            eps.append(cur)
        open_ep = cur is not None
    eps = [e for e in eps if e[0][0] - tmin > edge and tmax - e[-1][0] > edge
          and len(e) >= 10]
    print(f"\n## ISR timing: {names[0]} (+{len(names) - 1} body syms), "
          f"unit '{unit}', {len(starts)} {tick_symbol} beats at {tick_hz:g} Hz")
    if interrupted:
        print(f"- {interrupted} episode(s) excluded: a hole longer than 30 us inside "
              f"(preempted, or a timestamp jump), only a prefix is visible")
    if not eps:
        print("- no complete episodes in window (wrong symbols? window missed "
              "the traffic phase?)")
        return
    stats = sorted(((e[-1][0] - e[0][0]) * local_scale(e[0][0]), len(e), e[0][0])
                   for e in eps)
    cal = [s[0] for s in stats]
    print(f"- episodes: {len(cal)}  |  fastest {min(cal) * 1e6:.2f} us, "
          f"median {statistics.median(cal) * 1e6:.2f} us, "
          f"avg {statistics.mean(cal) * 1e6:.2f} us, "
          f"worst {max(cal) * 1e6:.2f} us")
    insts = [s[1] for s in stats]
    print(f"- instructions/episode: min {min(insts)}, avg "
          f"{statistics.mean(insts):.0f}, max {max(insts)}")
    print("- worst episodes (duration, instructions, raw start):")
    for c, n, t0 in stats[-5:][::-1]:
        print(f"  - {c * 1e6:8.2f} us  {n:5d} instr  t={t0:.6f}")
    print("- caveats: timestamps are interpolated between packets (sub-us "
          "values are approximate); episodes may merge if trace overflow "
          "dropped an entry")


def short(name):
    return re.sub(r"\(.*\)$", "()", name)


def branch_bias(insts_csv, funcs, top):
    """One-sided conditional branches in executed functions (profile_insts.csv):
    a conditional fetched N times but taken/executed 0 or N times = a branch
    that never varied -> hot always-true assert, dead path, or an invariant
    that could hoist out of a loop (UM08025 SS5.19)."""
    def n(v):
        v = (v or "").replace(" ", "")
        return int(v) if v.lstrip("-").isdigit() else 0
    biased = []
    with open(insts_csv, newline="", errors="replace") as f:
        for r in csv.DictReader(f):
            if (r.get("Is Conditional") or "0").strip() != "1":
                continue
            fetched = n(r.get("Times Fetched"))
            executed = n(r.get("Times Executed"))
            if fetched < 1000:            # only hot conditionals matter
                continue
            if executed == 0 or executed == fetched:
                biased.append((fetched, r.get("Function", "?"),
                               r.get("Address", ""), r.get("AsmCode", "").strip(),
                               "always-taken" if executed == fetched else "never-taken"))
    if not biased:
        return
    biased.sort(reverse=True)
    print(f"\n## One-sided hot branches (profile_insts.csv)\n")
    print("Conditionals that never varied - candidates to hoist/remove "
          "(UM08025 §5.19):")
    for fetched, fn, addr, asm, kind in biased[:top]:
        print(f"- `{short(fn)}` @{addr} {kind} ({fetched:,}x): `{asm[:50]}`")


def main():
    p = argparse.ArgumentParser(description=__doc__,
                                formatter_class=argparse.RawDescriptionHelpFormatter)
    p.add_argument("capture_dir", help="etm_capture.py output dir")
    p.add_argument("--top", type=int, default=10, help="table size")
    p.add_argument("--elf", help="firmware ELF, enables itrace analyses")
    p.add_argument("--isr", help="entry[,body..] symbols for ISR episode timing")
    p.add_argument("--tick-symbol", help="periodic handler that calibrates --isr "
                   "durations (e.g. SysTick_Handler); needs --tick-hz")
    p.add_argument("--tick-hz", type=float, help="rate the firmware runs "
                   "--tick-symbol at, in Hz (e.g. 1000)")
    args = p.parse_args()
    if (args.tick_symbol is None) != (args.tick_hz is None):
        p.error("--tick-symbol and --tick-hz go together")
    if args.tick_symbol is not None and not args.tick_symbol.strip():
        p.error("--tick-symbol is empty")
    if args.tick_symbol is not None and not args.isr:
        p.error("--tick-symbol/--tick-hz only calibrate --isr")
    if args.tick_hz is not None and not (math.isfinite(args.tick_hz) and args.tick_hz > 0):
        p.error("--tick-hz must be a positive, finite rate")

    profile = os.path.join(args.capture_dir, "code_profile.txt")
    itrace = os.path.join(args.capture_dir, "itrace.csv")
    funcs, totals = parse_profile(profile)
    total_fetch = totals.get("fetch") or 1
    hot = sorted(funcs.items(), key=lambda kv: kv[1]["fetch"], reverse=True)[:args.top]

    print(f"# ETM profile: {args.capture_dir}\n")
    print(f"## Top {args.top} hottest functions (instruction-fetch share)\n")
    print("| # | Function | Module | Run Count | Fetch Count | Load % |")
    print("|---|----------|--------|-----------|-------------|--------|")
    for i, (name, v) in enumerate(hot, 1):
        print(f"| {i} | `{short(name)}` | {v['module']} | {v['run']:,} "
              f"| {v['fetch']:,} | {100.0 * v['fetch'] / total_fetch:.2f}% |")
    print(f"\nTotal fetches {totals.get('fetch', 0):,} "
          f"(runs {totals.get('run', 0):,}, unaccounted {totals.get('unaccounted', 0):,})\n")

    ic, sc = totals.get("inst_cov"), totals.get("src_cov")
    print("## Coverage (NOPs excluded)\n")
    if ic:
        print(f"- instructions fully executed: {ic[0]:,} / {ic[1]:,} "
              f"({100.0 * ic[0] / ic[1]:.1f}%)")
    if sc:
        print(f"- source lines fully covered:  {sc[0]:,} / {sc[1]:,} "
              f"({100.0 * sc[0] / sc[1]:.1f}%)")
    partial = [n for n, v in funcs.items()
               if v["fetch"] > 0 and 0 < v.get("inst_pct", 100) < 100]
    print(f"- executed but only partially covered: {len(partial)} functions")
    dead = sorted(n for n, v in funcs.items()
                  if v["fetch"] == 0 and "(always inlined)" not in n)
    print(f"- never-executed out-of-line functions: {len(dead)}")
    by_mod = {}
    for n in dead:
        by_mod.setdefault(funcs[n]["module"], []).append(short(n))
    for mod in sorted(by_mod, key=str):
        print(f"  - {mod}: {', '.join('`%s`' % f for f in by_mod[mod])}")

    if os.path.isfile(itrace) and args.elf:
        syms = load_symbols(args.elf)
        tf, cf, n = time_by_func(itrace, syms)
        tt, tc = sum(tf.values()) or 1, sum(cf.values()) or 1
        tshare = {k: v / tt for k, v in tf.items()}
        cshare = {k: v / tc for k, v in cf.items()}
        unit = itrace_unit(itrace)
        print(f"\n## Instruction history (itrace.csv, unit '{unit}')\n")
        if not tf:
            print(f"- {n:,} instructions, NO timestamps (--no-timestamps "
                  f"capture): time shares unavailable, top {args.top} by "
                  f"instruction share:")
            for name, cs in sorted(cshare.items(), key=lambda kv: -kv[1])[:args.top]:
                print(f"  - `{short(name)}`: {100 * cs:.1f}% instructions")
        else:
            print(f"- {n:,} instructions; top {args.top} by TIME share "
                  f"(vs instruction share):")
            for name, ts in sorted(tshare.items(), key=lambda kv: -kv[1])[:args.top]:
                print(f"  - `{short(name)}`: {100 * ts:.1f}% time, "
                      f"{100 * cshare.get(name, 0):.1f}% instructions")

    lines_csv = os.path.join(args.capture_dir, "profile_lines.csv")
    if os.path.isfile(lines_csv):
        def n(v):  # Ozone groups thousands with spaces
            v = (v or "").replace(" ", "")
            return int(v) if v.isdigit() else 0
        with open(lines_csv, newline="", errors="replace") as f:
            rows = [r for r in csv.DictReader(f)
                    if r.get("File") and n(r.get("Instructions Fetched")) > 0]
        rows.sort(key=lambda r: -n(r["Instructions Fetched"]))
        print(f"\n## Hottest source lines (profile_lines.csv)\n")
        for r in rows[:args.top]:
            src = (r.get("Content") or "").strip()
            print(f"- {os.path.basename(r['File'])}:{r['Line']}  "
                  f"{n(r['Instructions Fetched']):,} fetches  `{src[:60]}`")

    insts_csv = os.path.join(args.capture_dir, "profile_insts.csv")
    if os.path.isfile(insts_csv):
        branch_bias(insts_csv, funcs, args.top)

    if args.isr:
        if not (os.path.isfile(itrace) and args.elf):
            sys.exit("error: --isr needs itrace.csv (--trace-csv capture) and --elf")
        isr_report(itrace, args.elf, args.isr, args.tick_symbol, args.tick_hz)
    return 0


if __name__ == "__main__":
    sys.exit(main())
