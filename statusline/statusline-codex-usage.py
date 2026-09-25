#!/usr/bin/env python3
"""Print the Codex weekly rate-limit snapshot as JSON: {"percent": N, "resets_at": epoch}.

Codex 0.154 exposes rate limits only through the app-server JSON-RPC
(`account/rateLimits/read`); there is no `codex usage` subcommand.
"""
import json, subprocess, sys

WEEK_MINS = 10080
WEEK_MIN, WEEK_MAX = 9576, 10584   # codex TUI treats +/-5% of a week as weekly


def read_rate_limits(timeout=25):
    p = subprocess.Popen(["codex", "-c", "analytics.enabled=false", "app-server"], stdin=subprocess.PIPE,
                         stdout=subprocess.PIPE, stderr=subprocess.DEVNULL,
                         text=True, bufsize=1)
    try:
        def send(o):
            p.stdin.write(json.dumps(o) + "\n"); p.stdin.flush()
        send({"jsonrpc": "2.0", "id": 1, "method": "initialize",
              "params": {"clientInfo": {"name": "statusline", "title": "statusline",
                                        "version": "1.0.0"}}})
        send({"jsonrpc": "2.0", "method": "initialized", "params": {}})
        send({"jsonrpc": "2.0", "id": 2, "method": "account/rateLimits/read",
              "params": {"excludeResetCreditDetails": True}})
        for _ in range(50):
            line = p.stdout.readline()
            if not line:
                return None
            try:
                m = json.loads(line)
            except ValueError:
                continue
            if m.get("id") == 2:
                return m.get("result")
        return None
    finally:
        p.kill()


def weekly(result):
    """The weekly window of the `codex` limit, or of the legacy top-level snapshot.

    Only that one bucket: other limit ids (per-model quotas) carry their own
    weekly windows and are not the account's weekly usage. Within it, either
    slot may hold the week -- prolite puts it in `primary` with no `secondary`.
    """
    by_id = result.get("rateLimitsByLimitId") or {}
    snap = by_id.get("codex")
    if not isinstance(snap, dict):
        snap = result.get("rateLimits")
        if not isinstance(snap, dict):
            return None
        lid = snap.get("limitId")
        if lid not in (None, "codex"):
            return None

    wins = [w for w in (snap.get("primary"), snap.get("secondary"))
            if isinstance(w, dict) and isinstance(w.get("usedPercent"), (int, float))]
    exact = [w for w in wins if w.get("windowDurationMins") == WEEK_MINS]
    if exact:
        return exact[0]
    near = [w for w in wins
            if isinstance(w.get("windowDurationMins"), int)
            and WEEK_MIN <= w["windowDurationMins"] <= WEEK_MAX]
    return near[0] if len(near) == 1 else None


def main():
    r = read_rate_limits()
    if not isinstance(r, dict):
        return 1
    w = weekly(r)
    if not w:
        return 1
    json.dump({"percent": round(w["usedPercent"]), "resets_at": w.get("resetsAt")}, sys.stdout)
    return 0


if __name__ == "__main__":
    sys.exit(main())
