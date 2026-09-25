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
#
# Usage is fetched in the BACKGROUND and cached, so rendering never blocks on
# the network. Colors: green <50%, yellow <80%, red >=80%.

input=$(cat)

# ---- config dir (holds .credentials.json + caches). Falls back sensibly. ----
CFG="${CLAUDE_CONFIG_DIR:-$HOME/.claude}"
creds=""
for c in "$CFG/.credentials.json" "$HOME/.claude-thach/.credentials.json" "$HOME/.claude/.credentials.json"; do
  [ -f "$c" ] && { creds="$c"; break; }
done

# ---- parse the status-line JSON input (cwd + model + effort + weekly reset) ----
# The input's rate_limits.seven_day is the authoritative weekly window (fresh,
# no rate-limit); emit its reset already formatted to short local form.
fields=$(printf '%s' "$input" | python3 -c '
import sys, json, datetime
try: d = json.load(sys.stdin)
except Exception: d = {}
print((d.get("workspace") or {}).get("current_dir") or "")
print((d.get("model") or {}).get("display_name") or "")
print((d.get("effort") or {}).get("level") or "")
def rfmt(ra):
    if not isinstance(ra,(int,float)): return ""
    try:
        t=datetime.datetime.fromtimestamp(ra).astimezone()
        now=datetime.datetime.now().astimezone()
        if t.date()==now.date(): return t.strftime("%H:%M")
        return t.strftime("%b%d %H:%M")
    except Exception: return ""
rl=(d.get("rate_limits") or {})
print(rfmt((rl.get("seven_day") or {}).get("resets_at")))          # weekly reset
p=(d.get("context_window") or {}).get("used_percentage")
if isinstance(p,(int,float)):
    c="\033[91m" if p>=80 else ("\033[93m" if p>=50 else "\033[92m")
    print("\033[97mc\033[0m"+c+str(round(p))+"%\033[0m")
else:
    print("")
' 2>/dev/null)
cwd=$(printf '%s' "$fields" | sed -n '1p')
model=$(printf '%s' "$fields" | sed -n '2p' | sed -E 's/ *\([^)]*\)$//')
effort=$(printf '%s' "$fields" | sed -n '3p')
wk_reset=$(printf '%s' "$fields" | sed -n '4p')
ctx=$(printf '%s' "$fields" | sed -n '5p')
[ -n "$cwd" ] || cwd=$PWD

# ---- org from oauthAccount: config-dir-local first, then home-dir default ----
# (each config dir is a different account; do NOT cross-fall-back into the other)
org=$(python3 -c '
import json,sys
for f in sys.argv[1:]:
    try:
        a=json.load(open(f)).get("oauthAccount") or {}
        e=(a.get("emailAddress") or "").split("@")[-1]
        if e: print(e); break
    except Exception: pass
' "$CFG/.claude.json" "$HOME/.claude.json" 2>/dev/null)

# ---- $HOME -> ~ like PS1 \w ----
case "$cwd" in
  "$HOME") dir='~' ;;
  "$HOME"/*) dir="~${cwd#"$HOME"}" ;;
  *) dir=$cwd ;;
esac

# ---- usage: read from cache; refresh in background when stale ----
cache="$CFG/statusline-usage.json"
lock="$CFG/.statusline-usage.lock"
max_age_min=2
if [ -n "$creds" ]; then
  stale=1
  [ -f "$cache" ] && [ -z "$(find "$cache" -mmin +$max_age_min 2>/dev/null)" ] && stale=0
  if [ "$stale" = 1 ]; then
    # atomic lock; clear a stale lock (>1 min) left by a killed refresh
    [ -d "$lock" ] && [ -n "$(find "$lock" -mmin +1 2>/dev/null)" ] && rmdir "$lock" 2>/dev/null
    if mkdir "$lock" 2>/dev/null; then
      ( tok=$(python3 -c "import json;print(json.load(open('$creds'))['claudeAiOauth']['accessToken'])" 2>/dev/null)
        if [ -n "$tok" ]; then
          curl -sS --max-time 8 \
            -H "Authorization: Bearer $tok" \
            -H "anthropic-beta: oauth-2025-04-20" \
            -H "Content-Type: application/json" \
            https://api.anthropic.com/api/oauth/usage 2>/dev/null > "$cache.tmp"
          # only replace the cache with a real usage payload; a 429/error body
          # (or unparseable junk) must NOT clobber the last-good values.
          if python3 -c 'import json,sys
d=json.load(open(sys.argv[1]))
sys.exit(0 if isinstance(d,dict) and not d.get("error") and (d.get("limits") or d.get("five_hour") or d.get("seven_day")) else 1)' "$cache.tmp" 2>/dev/null; then
            mv "$cache.tmp" "$cache"
          else
            rm -f "$cache.tmp"
          fi
        fi
        rmdir "$lock" 2>/dev/null
      ) >/dev/null 2>&1 &
    fi
  fi
fi

# ---- codex weekly usage: same background-refresh-and-cache scheme ----
# one Codex account regardless of which Claude config dir is active -> fetcher
# and cache always live in ~/.claude, so alternate dirs show it and poll once.
cx_dir="$HOME/.claude"
cx_cache="$cx_dir/statusline-codex-usage.json"
cx_lock="$cx_dir/.statusline-codex-usage.lock"
cx_fetch="$cx_dir/statusline-codex-usage.py"
if [ -f "$cx_fetch" ] && command -v codex >/dev/null 2>&1; then
  stale=1
  [ -f "$cx_cache" ] && [ -z "$(find "$cx_cache" -mmin +$max_age_min 2>/dev/null)" ] && stale=0
  if [ "$stale" = 1 ]; then
    [ -d "$cx_lock" ] && [ -n "$(find "$cx_lock" -mmin +1 2>/dev/null)" ] && rmdir "$cx_lock" 2>/dev/null
    if mkdir "$cx_lock" 2>/dev/null; then
      ( if timeout 30 python3 "$cx_fetch" > "$cx_cache.tmp" 2>/dev/null && [ -s "$cx_cache.tmp" ]; then
          mv "$cx_cache.tmp" "$cx_cache"
        else
          rm -f "$cx_cache.tmp"   # keep the last-good values on a failed poll
        fi
        rmdir "$cx_lock" 2>/dev/null
      ) >/dev/null 2>&1 &
    fi
  fi
fi

codex_usage=""
if [ -f "$cx_cache" ]; then
  codex_usage=$(python3 -c '
import json,sys,datetime
try: d=json.load(open(sys.argv[1]))
except Exception: sys.exit()
p=d.get("percent")
if not isinstance(p,(int,float)): sys.exit()
col="\033[91m" if p>=80 else ("\033[93m" if p>=50 else "\033[92m")
r="\033[0m"
ra=d.get("resets_at"); rs=""
if isinstance(ra,(int,float)):
    try:
        t=datetime.datetime.fromtimestamp(ra).astimezone()
        now=datetime.datetime.now().astimezone()
        rs=t.strftime("%H:%M") if t.date()==now.date() else t.strftime("%b%d %H:%M")
    except Exception: pass
sr=(" \033[33m↻"+rs+r) if rs else ""
print("\033[97mC"+r+col+str(round(p))+"%"+r+sr)
' "$cx_cache" 2>/dev/null)
fi

# usage percentages (line 1) + weekly reset time, short form (line 2)
usage=""; reset_at=""; credits=""
if [ -f "$cache" ]; then
  usageout=$(python3 -c '
import json,sys,datetime
try: d=json.load(open(sys.argv[1]))
except Exception: sys.exit()
if not isinstance(d,dict) or d.get("error"): sys.exit()  # error/rate-limit body -> show nothing
def col(p):
    p=0 if p is None else p
    if p>=80: return "\033[91m"      # bright red
    if p>=50: return "\033[93m"      # bright yellow
    return "\033[92m"                # bright green
lims={}; week_iso=None; scoped_iso=None
for l in (d.get("limits") or []):
    k=l.get("kind"); pct=l.get("percent")
    if k=="session":
        lims["sess"]=pct
    elif k=="weekly_scoped":
        lims["fable"]=pct
        if l.get("resets_at"): scoped_iso=l.get("resets_at")
    elif k in ("weekly","weekly_all"):
        lims["week"]=pct
        if l.get("resets_at"): week_iso=l.get("resets_at")
# fallbacks to the raw window objects
def util(o): return (o or {}).get("utilization")
if "sess" not in lims:  lims["sess"]=util(d.get("five_hour"))
if "week" not in lims:  lims["week"]=util(d.get("seven_day"))
if "fable" not in lims: lims["fable"]=util(d.get("seven_day_opus"))
# weekly reset ONLY: weekly_all -> seven_day window -> Fable-scoped weekly.
reset_iso=week_iso or (d.get("seven_day") or {}).get("resets_at") or scoped_iso
def rfmt(iso):  # ISO -> local short form: today -> HH:MM, else -> "Jul29 HH:MM"
    if not iso: return ""
    try:
        t=datetime.datetime.fromisoformat(iso).astimezone()
        now=datetime.datetime.now().astimezone()
        if t.date()==now.date(): return t.strftime("%H:%M")
        return t.strftime("%b%d %H:%M")
    except Exception: return ""
def fmt(p):
    if p is None: p=0
    return col(p)+str(round(p))+"%"+"\033[0m"
if lims.get("sess") is None and lims.get("week") is None and lims.get("fable") is None:
    sys.exit()  # nothing usable -> render nothing rather than a fake 0%/0%/0%
r="\033[0m"; lbl="\033[97m"; sep="\033[90m/"+r   # "/" splits session from the weeklies
s=fmt(lims.get("sess")); w=fmt(lims.get("week")); f=fmt(lims.get("fable"))
print(lbl+"s"+r+s+sep+" "+lbl+"w"+r+w+" "+lbl+"F"+r+f)
print(rfmt(reset_iso))   # weekly reset (trailing ↻)
# extra-usage credits already spent; minor units -> "$31.54"
sp=d.get("spend") or {}
amt=(sp.get("used") or {}).get("amount_minor"); exp=(sp.get("used") or {}).get("exponent")
if not isinstance(amt,(int,float)):
    eu=d.get("extra_usage") or {}
    amt=eu.get("used_credits"); exp=eu.get("decimal_places")
if isinstance(amt,(int,float)) and (sp.get("enabled") or (d.get("extra_usage") or {}).get("is_enabled")):
    exp=exp if isinstance(exp,int) else 2
    v=amt/float(10**exp)
    c="\033[91m" if v>30 else ("\033[38;5;208m" if v>20 else lbl)   # >$30 red, >$20 orange
    print(lbl+"$"+r+c+("%.*f" % (exp, v))+r)
else:
    print("")
' "$cache" 2>/dev/null)
  usage=$(printf '%s' "$usageout" | sed -n '1p')
  reset_at=$(printf '%s' "$usageout" | sed -n '2p')
  credits=$(printf '%s' "$usageout" | sed -n '3p')
fi

# ---- render ----
# user@host muted (recedes); path cyan (distinct); info bright white; usage keeps its colors
host='\033[90m'; path='\033[36m'; info='\033[97m'; reset='\033[0m'
printf "${host}%s:${reset}${path}%s${reset}" "$(hostname -s)" "$dir"
[ -n "$org" ] && printf " ${info}%s${reset}" "$org"
[ -n "$model" ] && printf " ${info}%s${reset}" "$model"
[ -n "$effort" ] && printf " ${path}%s${reset}" "$effort"
[ -n "$ctx" ] && printf " %s" "$ctx"
# usage unavailable (rate-limited, error body, or no cache yet) -> show "? ? ?"
[ -z "$usage" ] && usage=$'\033[97ms\033[92m?\033[0m\033[90m/\033[0m \033[97mw\033[92m?\033[0m \033[97mF\033[92m?\033[0m'
printf " %s" "$usage"
# weekly reset: input's seven_day (fresh) wins; else the cache-derived weekly reset
[ -n "$wk_reset" ] && reset_at=$wk_reset
[ -n "$reset_at" ] && printf " \033[33m↻%s${reset}" "$reset_at"
[ -n "$credits" ] && printf " %s" "$credits"
# "/" separates the Claude group (s/w/F) from the Codex one
[ -n "$codex_usage" ] && printf "${host}/${reset} %s" "$codex_usage"
exit 0   # never let a false final test leak a non-zero exit (hides the whole statusline)
