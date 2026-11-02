#!/usr/bin/env bash
# Check a provisioned HIL farm. Run as root after setup.sh and a reboot; prints FAIL lines and
# exits non-zero on any. Board-level proof (hil_remote smoke per flasher, a usbtest battery,
# a green master HIL job) is a hardware run on top of this.
#   verify.sh                 the farm in service: every registered runner must be running
#   verify.sh --maintenance   during an update: the runners must be stopped with no job left
set -uo pipefail

HERE=$(cd "$(dirname "$0")" && pwd)
. "$HERE/common.sh" || exit 2
cd /  # python3 -c imports from the cwd first: an admin home can hold a hid.py
fails=0
farm() { python3 -I "$HERE/farm.py" "$@"; }
ok()   { echo "ok    $*"; }
fail() { echo "FAIL  $*"; fails=$((fails + 1)); }
check() {  # description command...
  local d=$1; shift
  if out=$("$@" 2>&1); then ok "$d"; else fail "$d: $(tail -1 <<<"$out")"; fi
}

maintenance=0
case "${1:-}" in "") ;; --maintenance) maintenance=1 ;; *) echo "usage: verify.sh [--maintenance]" >&2; exit 2 ;; esac
[ "$(id -u)" = 0 ] || { echo "run as root" >&2; exit 2; }
# the PATH ssh commands get; a shared host's sessions keep the owner's, so the checks take the farm's
if ! shared_host; then set -a; . /etc/environment; set +a; fi
farm_env=$(farm env) && farm_path=$(farm path) && runner_path=$(farm runner-path) && tools=$(farm tools) \
  || { echo "verify: cannot read $HERE/farm.toml" >&2; exit 2; }
if shared_host; then export PATH=$farm_path; fi
[ -n "$ADMIN_HOME" ] || { echo "verify: no home for $ADMIN" >&2; exit 2; }

# the Renesas controllers' firmware, on a host that installs it
if grep -qx renesas-fw <<<"$tools"; then
  "$HERE/fw/renesas_fw_check.sh" || fail "Renesas firmware (fw/renesas_fw_check.sh)"
fi
check "usbmon loaded" test -d /sys/module/usbmon
check "usbtest loaded" test -d /sys/module/usbtest

# the pinned tools: versions, links, file hashes and venv locks against farm.toml
out=$(farm verify 2>&1); rc=$?
while IFS= read -r line; do
  case $line in FAIL*) fail "${line#FAIL  }" ;; *) echo "$line" ;; esac
done <<<"$out"
[ "$rc" = 0 ] || grep -q '^FAIL' <<<"$out" || fail "farm.py verify exited $rc"
if ! shared_host; then
  check "/etc/environment PATH is farm.toml's" test "$PATH" = "$farm_path"
  while IFS= read -r kv; do
    check "/etc/environment sets ${kv%%=*}" grep -qxF "$kv" /etc/environment
  done <<<"$farm_env"
fi
check "JLinkExe" test -x /usr/bin/JLinkExe  # dpkg can list jlink with its files gone
check "lm4flash" sh -c 'command -v lm4flash'
check "dfu-util" dfu-util -V
check "uhubctl" uhubctl -v
check "arm-none-eabi-nm" arm-none-eabi-nm --version
check "python3 on PATH is the installed hil-venv" python3 -c 'import json, os, sys
d = json.load(open("/var/lib/hil-farm/hil-venv.json"))["dir"]
sys.exit(os.path.realpath(sys.prefix) != os.path.realpath(d))'
check "python imports esptool serial usb hid" python3 -c 'import esptool, serial, usb.core, hid'
check "libmtp loads" python3 -c 'import ctypes.util, sys; sys.exit(ctypes.util.find_library("mtp") is None)'

rejects() { nft list table inet hil_farm | awk '/reject/ {for (i = 1; i < NF; i++) if ($i == "packets") s += $(i + 1)} END {print s + 0}'; }
gw=$(ip -o route show default | awk '{print $3; exit}')
# the debug probes' nodes (ST, SEGGER, Arm): one attached before setup keeps its old group until replayed
probes=()
for d in /sys/bus/usb/devices/*; do
  case $(cat "$d/idVendor" 2>/dev/null) in
    0483|1366|0d28) probes+=("$(printf '/dev/bus/usb/%03d/%03d' "$(cat "$d/busnum")" "$(cat "$d/devnum")")") ;;
  esac
done
for name in "${RUNNERS[@]}"; do
  runner_conf "$name"
  want_groups=$(printf '%s\n' "$RUNNER_USER" "${RUNNER_GROUPS[@]}" | sort | xargs)
  check "$RUNNER_USER groups" test "$(id -nG "$RUNNER_USER" | tr ' ' '\n' | sort | xargs)" = "$want_groups"
  sudo_l=$(sudo -l -U "$RUNNER_USER")
  rules=$(awk '/may run the following/{f=1; next} f && NF {sub(/^ +/, ""); print}' <<<"$sudo_l")
  if [ -n "$RUNNER_SUDO" ]; then
    [ "$rules" = "(root) NOPASSWD: $RUNNER_SUDO" ] && ok "$RUNNER_USER sudo is $RUNNER_SUDO only" \
      || fail "$RUNNER_USER sudo rules: $rules"
  elif grep -q "is not allowed to run sudo" <<<"$sudo_l"; then ok "$RUNNER_USER has no sudo"
  else fail "$RUNNER_USER sudo: $(tail -1 <<<"$sudo_l")"
  fi
  check "$RUNNER_USER password locked" sh -c "passwd -S $RUNNER_USER | awk '{l = \$2} END {exit l != \"L\"}'"
  check "$RUNNER_USER reaches github" runuser -u "$RUNNER_USER" -- curl -fsS -m 10 -o /dev/null https://api.github.com
  before=$(rejects)
  runuser -u "$RUNNER_USER" -- curl -s -m 5 -o /dev/null "http://$gw/"
  [ "$(rejects)" -gt "$before" ] && ok "$RUNNER_USER to LAN gateway $gw rejected by nft" \
    || fail "$RUNNER_USER to $gw not rejected by the hil_farm table"
  # a shared host's sshd is the owner's: a runner user's session would run outside its unit's sandbox
  if shared_host; then check "sshd denies $RUNNER_USER" sshd_denies "$RUNNER_USER"; fi
  [ ${#probes[@]} = 0 ] || check "$RUNNER_USER opens all ${#probes[@]} probes" runuser -u "$RUNNER_USER" -- sh -c \
    'bad=; for n; do [ -r "$n" ] && [ -w "$n" ] || bad="$bad $n"; done; [ -z "$bad" ] || { echo "cannot open:$bad"; exit 1; }' \
    _ "${probes[@]}"
done
if ! shared_host; then
  sshd_t=$(sshd_effective 2>/dev/null)
  for kv in "${SSHD_EXPECT[@]}"; do
    grep -qx "$kv" <<<"$sshd_t" && ok "sshd $kv" || fail "sshd: want '$kv'"
  done
else
  check "nftables enabled" systemctl is-enabled -q nftables
  check "nftables loads the owner's rules with hil_farm (drop-in only)" \
    test "$(systemctl show -p DropInPaths --value nftables)" = "$NFT_DROPIN"
fi
IFS=: read -ra runner_dirs <<<"$runner_path"
if out=$(farm trusted "${runner_dirs[@]}" 2>&1); then
  ok "runner PATH writable by root only"
  if [ "$IAR_CHECKS" = 1 ]; then  # IAR builds run there: the host's iccarm, tinyusb's cmake_minimum_required
    # without the job's IAR_LMS_BEARER_TOKEN iccarm prints its banner, then fails the licence check
    check "iccarm on the runner PATH" env PATH="$runner_path" sh -c 'iccarm --version 2>&1 | grep -q "IAR ANSI C/C++ Compiler"'
    check "cmake >= 3.20 on the runner PATH" env PATH="$runner_path" sh -c \
      'v=$(cmake --version | awk "NR == 1 {print \$3}") && [ "$(printf "3.20\n%s\n" "$v" | sort -V | head -1)" = 3.20 ]'
    check "ninja on the runner PATH" env PATH="$runner_path" ninja --version
  fi
else
  fail "runner PATH writable by root only, so none of its tools run here: $(tail -1 <<<"$out")"
fi
for h in /home/*; do
  if shared_host && [ "$h" = "$ADMIN_HOME" ]; then continue; fi  # the runner units hide it instead
  [ $(( 8#$(stat -c %a "$h") & 8#007 )) = 0 ] && ok "$h not world-accessible" || fail "$h mode $(stat -c %a "$h")"
done

hooks=("$HOOKS/job_hook.py")
for name in "${RUNNERS[@]}"; do hooks+=("$HOOKS/job-started-$name.sh" "$HOOKS/job-completed-$name.sh"); done
for f in "${hooks[@]}"; do
  check "job hook ${f##*/} root-owned 0755" sh -c "[ \"\$(stat -c %U:%a $f)\" = root:755 ]"
done
check "job hooks' paths writable by root only" farm trusted "${hooks[@]}"
check "lock dir root-owned 1777" sh -c '[ "$(stat -c %U:%a /tmp/tinyusb-hil-locks)" = root:1777 ]'
bad=$(find /tmp/tinyusb-hil-locks -mindepth 1 ! -type f)
[ -z "$bad" ] && ok "lock dir holds regular files only" || fail "non-regular lock entries: $bad"
check "/run/tinyusb-hil root 0700" sh -c '[ "$(stat -c %U:%a /run/tinyusb-hil)" = root:700 ]'

if [ "$maintenance" = 1 ]; then
  check "runners stopped, no job (maintenance)" farm guard
fi
for name in "${RUNNERS[@]}"; do
  runner_conf "$name"
  if [ ! -f "$RUNNER_DIR/.runner" ]; then
    echo "skip  $name runner not registered yet (setup.sh runner $name <token>)"
    continue
  fi
  [ "$maintenance" = 1 ] || check "$name runner service active" systemctl is-active -q "$RUNNER_UNIT"
  if ! want_env=$(runner_env) || [ -z "$want_env" ]; then
    fail "$name runner .env: cannot generate its lines"
  else
    while IFS= read -r kv; do
      check "$name runner .env sets ${kv%%=*}" grep -qxF "$kv" "$RUNNER_DIR/.env"
    done <<<"$want_env"
  fi
  check "$name runner .path is farm.toml's" test "$(cat "$RUNNER_DIR/.path")" = "$runner_path"
  check "$name runner unit UMask 0022" test "$(systemctl show -p UMask --value "$RUNNER_UNIT")" = 0022
  if shared_host; then
    check "$name runner unit hides $ADMIN_HOME" \
      test "$(systemctl show -p InaccessiblePaths --value "$RUNNER_UNIT")" = "$ADMIN_HOME"
  fi
  check "$name runner root sticky root:$RUNNER_USER 1770" \
    sh -c "[ \"\$(stat -c %U:%G:%a $RUNNER_DIR)\" = root:$RUNNER_USER:1770 ]"
  for f in _work _diag; do
    check "$name runner $f $RUNNER_USER:$RUNNER_USER 0750" \
      sh -c "[ \"\$(stat -c %U:%G:%a $RUNNER_DIR/$f)\" = $RUNNER_USER:$RUNNER_USER:750 ]"
  done
  # a job cannot remove what the admin left there (UMask=0002 units before 0022)
  check "$name runner _work and _diag hold nothing of $ADMIN's" \
    sh -c 'o=$(find "$1/_work" "$1/_diag" -user "$2" -print -quit) || exit 1; [ -z "$o" ] || { echo "$o"; exit 1; }' \
    _ "$RUNNER_DIR" "$ADMIN"
  for f in .env .path runsvc.sh run.sh bin; do
    check "$name runner $f not writable by $RUNNER_USER" sh -c "! runuser -u $RUNNER_USER -- test -w $RUNNER_DIR/$f"
  done
  for f in _work ${RUNNER_STATE:+"$RUNNER_STATE"}; do
    check "$ADMIN reads $name runner $f" runuser -u "$ADMIN" -- test -r "$RUNNER_DIR/$f" -a -x "$RUNNER_DIR/$f"
  done
  while IFS= read -r other; do
    [ "$other" != "$RUNNER_USER" ] || continue
    for f in "$RUNNER_DIR" "$RUNNER_DIR/.credentials" "$RUNNER_DIR/_work" "$RUNNER_DIR/_diag"; do
      check "$other cannot read $f" sh -c "! runuser -u $other -- test -r $f -o -x $f"
    done
  done < <(runner_users)
done

echo "probes: $(lsusb | grep -ci 'segger\|j-link') J-Link, $(lsusb | grep -ci 'stlink\|st-link') ST-Link, $(lsusb | grep -ci 'cmsis') CMSIS-DAP"
[ "$fails" = 0 ] && echo "verify: all checks passed" || { echo "verify: $fails failed"; exit 1; }
