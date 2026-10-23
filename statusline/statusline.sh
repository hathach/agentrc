#!/usr/bin/env bash
# Claude Code status line.
#   grey   hostname            (short form)
#   blue   working dir         ($HOME shown as ~)
#   dim    email domain · model · effort · context %
#   usage  session / week(all) / week(Fable)  from /api/oauth/usage
#   $      extra-usage credits spent (after the weekly reset)
#   C      Codex week, from the codex app-server (account/rateLimits/read)
#   ↻      reset time, local, short form (HH:MM today, else "MonDD HH:MM");
#          one after the Claude weekly %s, one after the Codex %
#   #N     open PRs this session linked, newest first, by check state: ✓ passed, ● running, ✗ failed, ? unknown
#
# Usage is fetched in the BACKGROUND and cached, so rendering never blocks on
# the network. Colors: green <50%, yellow <80%, red >=80%.

input=$(cat)
# Windows Python reads files and writes stdout in its ANSI code page; the JSON and the glyphs are UTF-8
export PYTHONUTF8=1

# ---- config dir (holds .credentials.json + caches); each config dir is a different account ----
CFG="${CLAUDE_CONFIG_DIR:-$HOME/.claude}"
creds=""
[ -f "$CFG/.credentials.json" ] && creds="$CFG/.credentials.json"

# ---- usage: read from cache; refresh in background when stale ----
cache="$CFG/statusline-usage.json"
lock="$CFG/.statusline-usage.lock"
max_age_min=2
# a poll's own mark, set when it starts, so a failed poll also waits out the interval
polled_recently() { [ -f "$1" ] && [ -z "$(find "$1" -mmin +$max_age_min 2>/dev/null)" ]; }
# true once this render holds lock $1 for a poll that mark $2 says is due; it then marks the poll started and
# sweeps cache $3's temp files that a killed poll left (a poll lives under a minute)
start_poll() {
  polled_recently "$2" && return 1
  # atomic lock; clear a stale lock (>1 min) left by a killed refresh
  [ -d "$1" ] && [ -n "$(find "$1" -mmin +1 2>/dev/null)" ] && rmdir "$1" 2>/dev/null
  mkdir "$1" 2>/dev/null || return 1
  # a poll that finished between the check above and the lock already counts
  polled_recently "$2" && { rmdir "$1" 2>/dev/null; return 1; }
  touch "$2" || { rmdir "$1" 2>/dev/null; return 1; }  # an unmarked poll would retry on every render
  find "${3%/*}" -maxdepth 1 -name "${3##*/}.*.tmp" -mmin +1 -delete 2>/dev/null
  return 0
}
if [ -n "$creds" ] && start_poll "$lock" "$CFG/.statusline-usage.polled" "$cache"; then
  # a poll's own temp file: a stale-lock recovery race can still start a second poll
  ( tmp="$cache.$BASHPID.tmp"
    tok=$(python3 -c 'import json,sys;print(json.load(open(sys.argv[1]))["claudeAiOauth"]["accessToken"])' "$creds" 2>/dev/null)
    if [ -n "$tok" ]; then
      curl -sS --max-time 8 \
        -H "Authorization: Bearer $tok" \
        -H "anthropic-beta: oauth-2025-04-20" \
        -H "Content-Type: application/json" \
        https://api.anthropic.com/api/oauth/usage 2>/dev/null > "$tmp"
      # only replace the cache with a real usage payload; a 429/error body
      # (or unparseable junk) must NOT clobber the last-good values.
      if python3 -c 'import json,sys
d=json.load(open(sys.argv[1]))
sys.exit(0 if isinstance(d,dict) and not d.get("error") and (d.get("limits") or d.get("five_hour") or d.get("seven_day")) else 1)' "$tmp" 2>/dev/null; then
        mv "$tmp" "$cache"
      else
        rm -f "$tmp"
      fi
    fi
    rmdir "$lock" 2>/dev/null
  ) >/dev/null 2>&1 &
fi

# ---- codex weekly usage: same background-refresh-and-cache scheme ----
# one Codex account regardless of which Claude config dir is active -> fetcher
# and cache always live in ~/.claude, so alternate dirs show it and poll once.
cx_dir="$HOME/.claude"
cx_cache="$cx_dir/statusline-codex-usage.json"
cx_lock="$cx_dir/.statusline-codex-usage.lock"
cx_fetch="$cx_dir/statusline-codex-usage.py"
if [ -f "$cx_fetch" ] && command -v codex >/dev/null 2>&1 \
   && start_poll "$cx_lock" "$cx_dir/.statusline-codex-usage.polled" "$cx_cache"; then
  ( tmp="$cx_cache.$BASHPID.tmp"
    if timeout 30 python3 "$cx_fetch" > "$tmp" 2>/dev/null && [ -s "$tmp" ]; then
      mv "$tmp" "$cx_cache"
    else
      rm -f "$tmp"   # keep the last-good values on a failed poll
    fi
    rmdir "$cx_lock" 2>/dev/null
  ) >/dev/null 2>&1 &
fi

# ---- one python3 reads the status-line JSON input, the account's org and both usage caches, a field per line ----
# The input's rate_limits.seven_day is the authoritative weekly window (fresh, no rate-limit), so its reset wins
# over the cache's. org from oauthAccount: config-dir-local first, then ~/.claude.json for the default dir only
# (each config dir is a different account; do NOT cross-fall-back into the other).
accounts=("$CFG/.claude.json")
{ [ "$CFG" = "$HOME/.claude" ] || [ "$CFG" -ef "$HOME/.claude" ]; } && accounts+=("$HOME/.claude.json")
fields=$(printf '%s' "$input" | python3 -c '
import sys, json, re, datetime
sys.stdout.reconfigure(newline="\n")   # Windows would end each field with CR, which read keeps
R="\033[0m"; LBL="\033[97m"
def get(d, *keys):
    for k in keys: d=d.get(k) if isinstance(d,dict) else None
    return d
def load(f):
    try: return json.load(open(f))
    except Exception: return None
def guard(f, *a, default=""):  # a malformed section renders as absent and leaves the others
    try: return f(*a)
    except Exception: return default
def pct(lbl, p):  # green <50%, yellow <80%, red >=80%; None: the window is missing from the payload, not unused
    if p is None: return LBL+lbl+R+"\033[92m?"+R
    return LBL+lbl+R+("\033[91m" if p>=80 else ("\033[93m" if p>=50 else "\033[92m"))+str(round(p))+"%"+R
def when(v, parse):  # local short form: today -> HH:MM, else "Jul29 HH:MM"
    try:
        t=parse(v).astimezone()
        return t.strftime("%H:%M" if t.date()==datetime.datetime.now().astimezone().date() else "%b%d %H:%M")
    except Exception: return ""
epoch=datetime.datetime.fromtimestamp; iso=datetime.datetime.fromisoformat
def org(files):
    for f in files:
        e=get(load(f), "oauthAccount", "emailAddress")
        if isinstance(e,str) and e.split("@")[-1]: return e.split("@")[-1]
    return ""
def ctx(p):
    return pct("c",p) if isinstance(p,(int,float)) else ""
def codex(d):
    p=get(d,"percent")
    if not isinstance(p,(int,float)): return ""
    rs=when(d.get("resets_at"),epoch)
    return pct("C",p)+(" \033[33m↻"+rs+R if rs else "")
def usage(d):  # session / week(all) / week(Fable) percentages, and the weekly reset
    if not isinstance(d,dict) or d.get("error"): return "",""  # error/rate-limit body -> show nothing
    lims={}; week_iso=None; scoped_iso=None
    for l in (d.get("limits") or []):
        k=l.get("kind"); p=l.get("percent")
        if k=="session":
            lims["sess"]=p
        elif k=="weekly_scoped":
            lims["fable"]=p
            if l.get("resets_at"): scoped_iso=l.get("resets_at")
        elif k in ("weekly","weekly_all"):
            lims["week"]=p
            if l.get("resets_at"): week_iso=l.get("resets_at")
    # fallbacks to the raw window objects
    def util(o): return (o or {}).get("utilization")
    if "sess" not in lims:  lims["sess"]=util(d.get("five_hour"))
    if "week" not in lims:  lims["week"]=util(d.get("seven_day"))
    if "fable" not in lims: lims["fable"]=util(d.get("seven_day_opus"))
    if lims.get("sess") is None and lims.get("week") is None and lims.get("fable") is None:
        return "",""  # nothing usable -> render nothing rather than a fake 0%/0%/0%
    # weekly reset ONLY: weekly_all -> seven_day window -> Fable-scoped weekly.
    reset_iso=week_iso or (d.get("seven_day") or {}).get("resets_at") or scoped_iso
    line=pct("s",lims.get("sess"))+"\033[90m/"+R+" "+pct("w",lims.get("week"))+" "+pct("F",lims.get("fable"))
    return line, when(reset_iso,iso)   # "/" splits session from the weeklies
def credits(d):  # extra-usage credits already spent; minor units -> "$31.54"
    sp=d.get("spend") or {}
    amt=(sp.get("used") or {}).get("amount_minor"); exp=(sp.get("used") or {}).get("exponent")
    if not isinstance(amt,(int,float)):
        eu=d.get("extra_usage") or {}
        amt=eu.get("used_credits"); exp=eu.get("decimal_places")
    if not (isinstance(amt,(int,float)) and (sp.get("enabled") or (d.get("extra_usage") or {}).get("is_enabled"))):
        return ""
    exp=exp if isinstance(exp,int) else 2
    v=amt/float(10**exp)
    c="\033[91m" if v>30 else ("\033[38;5;208m" if v>20 else LBL)   # >$30 red, >$20 orange
    return LBL+"$"+R+c+("%.*f" % (exp, v))+R
try: d=json.load(sys.stdin)
except Exception: d={}
cache=load(sys.argv[1])
use, reset=guard(usage, cache, default=("",""))
print(get(d,"workspace","current_dir") or "")
print(get(d,"session_id") or "")
print(get(d,"transcript_path") or "")
print(org(sys.argv[3:]))
print(re.sub(r" *\([^)]*\)$", "", str(get(d,"model","display_name") or "")))
print(get(d,"effort","level") or "")
print(guard(ctx, get(d,"context_window","used_percentage")))
print(use)
print(when(get(d,"rate_limits","seven_day","resets_at"),epoch) or reset)
print(guard(credits, cache) if use else "")
print(guard(codex, load(sys.argv[2])))
' "$cache" "$cx_cache" "${accounts[@]}" 2>/dev/null)
for v in cwd sid transcript org model effort ctx usage reset_at credits codex_usage; do IFS= read -r "$v"; done <<<"$fields"
[ -n "$cwd" ] || cwd=$PWD
# Git Bash/MSYS2: Claude Code passes a Windows path, $HOME is /c/Users/...
command -v cygpath >/dev/null 2>&1 && cwd=$(cygpath -u "$cwd")

# ---- $HOME -> ~ like PS1 \w ----
case "$cwd" in
  "$HOME") dir='~' ;;
  "$HOME"/*) dir="~${cwd#"$HOME"}" ;;
  *) dir=$cwd ;;
esac

# ---- PRs this session linked: the helper prints from its cache and refreshes it detached ----
# (its own per-session cache, backoff and rate-limit wait, so not the mkdir/find scheme above)
pr_helper="$HOME/.claude/statusline-prs.py"
prs=""
[ -f "$pr_helper" ] && [ -n "$sid" ] && prs=$(python3 "$pr_helper" render "$CFG/statusline-prs" "$sid" "$transcript" 2>/dev/null)

# ---- render ----
# user@host muted (recedes); path cyan (distinct); info bright white; usage keeps its colors
host='\033[90m'; path='\033[36m'; info='\033[97m'; reset='\033[0m'
hn=$(hostname -s 2>/dev/null) || hn=${HOSTNAME%%.*}   # Git Bash's hostname has no -s
printf "${host}%s:${reset}${path}%s${reset}" "$hn" "$dir"
[ -n "$org" ] && printf " ${info}%s${reset}" "$org"
[ -n "$model" ] && printf " ${info}%s${reset}" "$model"
[ -n "$effort" ] && printf " ${path}%s${reset}" "$effort"
[ -n "$ctx" ] && printf " %s" "$ctx"
# usage unavailable (rate-limited, error body, or no cache yet) -> show "? ? ?"
[ -z "$usage" ] && usage=$'\033[97ms\033[92m?\033[0m\033[90m/\033[0m \033[97mw\033[92m?\033[0m \033[97mF\033[92m?\033[0m'
printf " %s" "$usage"
[ -n "$reset_at" ] && printf " \033[33m↻%s${reset}" "$reset_at"
[ -n "$credits" ] && printf " %s" "$credits"
# "/" separates the Claude group (s/w/F) from the Codex one
[ -n "$codex_usage" ] && printf "${host}/${reset} %s" "$codex_usage"
[ -n "$prs" ] && printf "${host}/${reset} %s" "$prs"
exit 0   # never let a false final test leak a non-zero exit (hides the whole statusline)
