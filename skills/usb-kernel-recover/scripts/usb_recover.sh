#!/usr/bin/env bash
# usb_recover.sh — USB recovery helper for a HIL host; run with sudo. Writes only
# to the specific sysfs control files below; arg regexes block path traversal.
#
# Usage, by path (sudo's PATH does not include the script; <skill dir> is this skill's directory):
#   sudo <skill dir>/scripts/usb_recover.sh <verb> ...
#   authorized <busport>   # e.g. 3-2  -> deauthorize+reauthorize: unconfigure and reconfigure, no re-enumeration, NO VBUS cut
#   root-cycle <busport> [serial]  # e.g. 13-1.6 -> port power off, 5 s, on at the ROOT port
#                          # feeding it, sent over the root hub's own usbfs node only
#                          # (root_port_cycle.py); [serial] is verified against the device and
#                          # refused on mismatch. Bounces every sibling under that root port.
#                          # Exit 3: refused before the power-off was sent.
#   hub-cycle <busport> [serial]  # e.g. 5-3.2 -> ask the kernel to power the hub port feeding that
#                          # LEAF off and on (its `disable` file: 1, 2 s, 0) under the hub's lock.
#                          # Whether VBUS drops is the hub's: success proves re-enumeration, not a
#                          # power cycle. Its serial must answer: a wedged leaf would block the
#                          # write. Exit 3: refused before anything was written.
#   pci-rebind <pciaddr>   # e.g. 0000:05:00.0 -> unbind+bind the whole xHCI
#                          # controller. For a DEAD CONTROLLER, not a wedged
#                          # device: it renumbers every bus it owns.
#   pci-bind   <pciaddr> [drv]  # re-attach a driver to a DRIVERLESS controller
#   resolve    <devnode>   # e.g. /dev/ttyACM3 -> print its <busport> (no privilege needed)
#   shield     <busport> <owner-pid>    # chmod 000 the nine locking attrs of the wedged leaf, its
#                          # parent hub and the root hub so non-root libusb enumerators skip it;
#                          # records every original mode first. Refused while another shield
#                          # covers any of the same objects.
#   unshield   <busport> [owner-pid]    # restore the recorded modes on the surviving originals
#                          # (same inode) and drop the record; refused while a DIFFERENT owner
#                          # is still alive. A stale record (owner gone) needs no pid.
#   shield-status [busport] # records, their owners (alive/dead) and what is still shielded
#
# Run by path from tinyusb's hil-contract SKILL.md and agentrc's skills/hil/scripts/hil_reserve.py:
# a rename or CLI change needs those consumers updated.
set -euo pipefail

# The shield's locking attributes: served under the device lock, so a wedged device blocks
# every reader (usb-kernel-recover SKILL.md section 2). descriptors/busnum/devnum/speed/
# idVendor/idProduct are lock-free and libusb needs them -- never in this list.
SHIELD_ATTRS='bNumInterfaces bmAttributes bMaxPower configuration bConfigurationValue product manufacturer serial avoid_reset_quirk'
# Test seams, honoured only WITHOUT root: a fake sysfs tree (every /sys path below goes through
# $SYSFS) and a scratch state dir. Under sudo they are ignored, so the script cannot be pointed at
# arbitrary paths as a privileged chmod or sysfs write.
SYSFS=/sys
SHIELD_STATE=/run/tinyusb-hil/shield
FAIL_CHMOD=''          # a path whose chmod the tests make fail, to reach the rollback-failure branch
PORT_WRITE_TIMEOUT=''
ROOT_PORT_CYCLE="$(dirname "$0")/root_port_cycle.py"
KILL_SIG=KILL          # the tests' stand-in for a D-state child: a signal their stub survives
if [ "$(id -u)" -ne 0 ]; then
  SYSFS=${USB_RECOVER_SYSFS:-/sys}
  SHIELD_STATE=${USB_RECOVER_STATE:-/run/tinyusb-hil/shield}
  FAIL_CHMOD=${USB_RECOVER_TEST_FAIL_CHMOD:-}
  PORT_WRITE_TIMEOUT=${USB_RECOVER_PORT_WRITE_TIMEOUT:-}
  ROOT_PORT_CYCLE=${USB_RECOVER_ROOT_PORT_CYCLE:-$ROOT_PORT_CYCLE}
  KILL_SIG=${USB_RECOVER_TEST_KILL_SIG:-KILL}
fi

USBPATH_RE='^[0-9]+-[0-9]+(\.[0-9]+)*$'
PCI_RE='^[0-9a-fA-F]{4}:[0-9a-fA-F]{2}:[0-9a-fA-F]{2}\.[0-9]$'
DRIVER_RE='^[A-Za-z0-9_-]+$'

die() { echo "usb_recover: $*" >&2; exit 1; }
refuse() { echo "usb_recover: $*" >&2; exit 3; }   # nothing was written

lock_read() {
  # Read an attribute served under the device lock (serial, product) with a 2s bound.
  # Prints the value, '' when the attribute is absent, or '?' when it did not answer.
  #
  # Bounding these is load-bearing, not defensive: they are the FIRST thing root-cycle
  # does, so on a real wedge an unbounded read blocks before reaching the power request at all
  # (observed live: one attempt sat 3h; three concurrent invocations all frozen there).
  # The operator then reads that as "recovery didn't work" and escalates to a bare
  # `uhubctl -a cycle`, which tears the subtree down and blocks holding the ROOT HUB
  # lock -- taking the whole bus with it. That is how one wedge becomes an incident.
  #
  # `timeout` ends it: v6.12.101 drivers/usb/core/sysfs.c takes the lock for every READ through
  # usb_lock_device_interruptible, so the waiter sleeps interruptibly; only the usbfs ioctl
  # holder is not.
  local v rc=0
  # `|| rc=$?`, never a bare assignment: under this script's `set -e` a command
  # substitution that FAILS (an absent attribute -- most hubs and probes have no
  # iSerialNumber, and `product` is often missing) exits the whole recovery script.
  v=$(timeout 2 cat "$1" 2>/dev/null) || rc=$?
  [ "$rc" -eq 124 ] && { echo '?'; return; }      # timed out: nobody answered
  printf '%s\n' "$v"
}

# Refuse to touch a PCI function that is not a USB controller (class 0x0c03xx), so a stray or
# mistyped BDF can't unbind/reset an unrelated device (storage, NIC) on a shared HIL host.
require_usb_controller() {
  local addr=$1 cls
  cls=$(cat "$SYSFS/bus/pci/devices/$addr/class" 2>/dev/null) || die "no such pci device: $addr"
  [[ "$cls" =~ ^0x0c03 ]] || die "$addr is not a USB controller (class $cls); refusing"
}

# Generation marker for "did this device actually re-enumerate". A real disconnect destroys the
# usb_device and its sysfs kobject; reconnecting creates a new one, and kernfs hands out inode
# numbers monotonically, so the directory inode changes. Verified on the rig: ports re-enumerated
# minutes ago carry inodes in the millions while ports untouched since boot are still in the tens
# of thousands, ranking identically to their mtimes.
#
# This beats comparing devnum, which Linux reuses once the per-bus map wraps (observed live: a
# single cycle moved one device 123 -> 113). It also beats watching for the node to vanish, since
# the cycle holds the whole power-off window before the poll starts, which can never witness the
# gap. The inode survives the gap, so no observation window is needed.
#
# Crucially, if the disconnect is blocked on the wedged device's lock the kobject is never
# recreated -- same inode -- which is exactly the case that must be reported as a failure. Verified
# against kernfs: __kernfs_new_node() allocates via idr_alloc_cyclic() but kernfs_id_ino() exposes
# the full 64-bit (id_highbits<<32 | lowbits) as st_ino on 64-bit ino_t, so a repeat needs ~2^64
# node creations. authorized-toggle, set_configuration and suspend/resume all leave the parent
# device kobject alone, so none of them can move the marker and fake a success.
#
# The trailing slash is load-bearing: /sys/bus/usb/devices/<busport> is a SYMLINK with its own
# separate inode, so without it stat reports the link rather than the device it points at, and the
# value would never change. Do not "tidy" it away.
sysfs_gen() { stat -c %i "$SYSFS/bus/usb/devices/$1/" 2>/dev/null || echo none; }

await_reenum() {  # await_reenum TARGET GEN: print its new generation; non-zero after ten 1 s looks without one
  local now
  for _ in $(seq 1 10); do
    sleep 1
    now=$(sysfs_gen "$1")
    if [ "$now" != none ] && [ "$now" != "$2" ]; then echo "$now"; return 0; fi
  done
  return 1
}

# s for one port request (a `disable` write and its read-back, or one root_port_cycle.py run);
# a positive integer, never evaluated as an expression
[[ $PORT_WRITE_TIMEOUT =~ ^[1-9][0-9]*$ ]] || PORT_WRITE_TIMEOUT=15
PORT_OFF_SECS=2
ROOT_OFF_SECS=5
interrupted='' survivor=''
supervised() {
  # supervised CMD...: run it in a child bounded by PORT_WRITE_TIMEOUT. Its own status; 124 when
  # it was killed at the bound; 125 when it outlived SIGKILL (D state) for 1 s and was left
  # running, its pid in $survivor -- it may still act, so no other request may follow it.
  local child n=0
  "$@" &
  child=$!
  while kill -0 "$child" 2>/dev/null; do
    if [ $n -ge $((PORT_WRITE_TIMEOUT * 5)) ]; then
      kill -"$KILL_SIG" "$child" 2>/dev/null
      for _ in 1 2 3 4 5; do
        sleep 0.2
        kill -0 "$child" 2>/dev/null || { wait "$child" 2>/dev/null; return 124; }
      done
      survivor=$child
      return 125
    fi
    sleep 0.2; n=$((n + 1))
  done
  wait "$child"
}
cycle_port() {
  # cycle_port LABEL SECS OFF_CMD... -- ON_CMD...: the off request, SECS, the on request, each
  # supervised. From the off request on, any failure or interruption still runs the on request,
  # unless a run survived (it could still act after the restore); anything but a clean cycle
  # dies, never with 3.
  local label=$1 secs=$2 off=0 on=0 restored offcmd=()
  shift 2
  while [ "$1" != -- ]; do offcmd+=("$1"); shift; done
  shift
  trap 'interrupted=yes' INT TERM HUP
  # a signal already taken (root-cycle arms the trap before its check) must not reach the off request
  [ -z "$interrupted" ] || { trap - INT TERM HUP; die "$label: interrupted before the power-off; nothing switched"; }
  supervised "${offcmd[@]}" || off=$?
  # `|| :`: a signal to the process group kills the sleep too, and set -e must not skip the restore
  if [ $off -eq 0 ] && [ -z "$interrupted" ]; then sleep "$secs" || :; fi
  if [ -n "$survivor" ]; then
    restored="not attempted: pid $survivor survived"
  else
    supervised "$@" || on=$?
    [ $on -eq 0 ] && restored=restored || restored="NOT restored (${on})${survivor:+: pid $survivor survived}"
  fi
  trap - INT TERM HUP
  if [ $off -ne 0 ] || [ $on -ne 0 ] || [ -n "$interrupted" ]; then
    die "$label failed (off ${off}, on ${on}${interrupted:+, interrupted}); power-on ${restored}"
  fi
}
usage() { sed -n '/^# Usage/,/^set -euo pipefail/p' "$0" | grep -E '^#   (sudo|[a-z])' >&2; exit 2; }

# ---- shield -----------------------------------------------------------------------------
# One record per shielded busport under $SHIELD_STATE, written BEFORE the first chmod:
#   owner <pid> <start-time> <boot-id>
#   obj <name> <dir-inode>            (leaf, parent hub, root hub)
#   attr <name>/<attr> <inode> <mode>
# The inode is the object's generation: re-enumeration destroys the kobject and its
# attributes and creates new ones, so unshield restores only what still carries the
# recorded inode and reports the rest as gone. Modes are restored from the record, never
# copied from a sibling (siblings differ, and may carry another shield). Every check and
# mutation runs under one flock, so two shields cannot both pass the overlap check.

attr_tx() {
  # One python3 per verb: attr_tx shield|restore|status ... Per attribute, O_PATH|O_NOFOLLOW
  # pins the object without opening it for reading (no ->show() on a wedged attribute, no read
  # permission needed), fstat gives the inode of exactly that object, and chmod through
  # /proc/self/fd changes it and nothing that replaced it. An attribute is done, gone (absent,
  # or a different inode: re-enumerated, replaced) or could not be inspected or changed --
  # never mistaken for gone.
  SYSFS="$SYSFS" FAIL_CHMOD="$FAIL_CHMOD" SHIELD_ATTRS="$SHIELD_ATTRS" python3 - "$@" <<'PY'
import os, stat, sys, tempfile
DEV = os.path.join(os.environ['SYSFS'], 'bus/usb/devices')
FAIL = os.environ.get('FAIL_CHMOD')
DONE, FAILED, GONE = 0, 1, 2


def die(msg):
    print(f'usb_recover: {msg}', file=sys.stderr)
    sys.exit(1)


def attr(path, ino=None, mode=None):
    """(outcome, text): with ino/mode a chmod of THAT inode, else '<inode> <mode>'."""
    if mode is not None and path == FAIL and mode != '000':
        return FAILED, 'chmod failed: injected'      # a RESTORE that fails, the rollback branch
    try:
        fd = os.open(path, os.O_PATH | os.O_NOFOLLOW | os.O_CLOEXEC)
    except FileNotFoundError:
        return GONE, 'gone'
    except OSError as e:
        return FAILED, f'inspect failed: {e.strerror}'
    try:
        st = os.fstat(fd)
        if not stat.S_ISREG(st.st_mode):
            return FAILED, 'not a regular file'
        if mode is None:
            return DONE, f'{st.st_ino} {st.st_mode & 0o777:o}'
        if str(st.st_ino) != ino:
            return GONE, 'gone'
        if st.st_mode & 0o777 != int(mode, 8):     # already there: nothing to change, nothing to fail
            try:
                os.chmod(f'/proc/self/fd/{fd}', int(mode, 8))
            except OSError as e:
                return FAILED, f'chmod failed: {e.strerror}'
        return DONE, 'ok'
    finally:
        os.close(fd)


def restore(lines):
    """chmod each attr line back to its recorded mode: (restored, gone, failures)."""
    restored = gone = 0
    failed = []
    for name, ino, mode in lines:
        rc, out = attr(f'{DEV}/{name}', ino, mode)
        if rc == DONE:
            restored += 1
        elif rc == GONE:
            gone += 1
        else:
            failed.append(f'{name} ({out})')
    return restored, gone, failed


def recorded(rec):
    with open(rec) as f:
        return [line.split()[1:4] for line in f if line.startswith('attr ')]


op, args = sys.argv[1], sys.argv[2:]
if op == 'shield':
    # snapshot every attribute before anything is published or changed; an attribute that
    # cannot be inspected stops the shield here, with nothing done
    rec, owner, bp, pid, objs = args[0], args[1], args[2], args[3], args[4:]
    lines, attrs = [owner], []
    for obj in objs:
        d = f'{DEV}/{obj}'
        try:
            lines.append(f'obj {obj} {os.stat(d + "/").st_ino}')
        except OSError as e:
            die(f'shield: cannot inspect {obj} ({e.strerror}); nothing changed')
        for f in os.environ['SHIELD_ATTRS'].split():
            rc, out = attr(f'{d}/{f}')
            if rc == DONE:
                lines.append(f'attr {obj}/{f} {out}')
                attrs.append([f'{obj}/{f}', *out.split()])
            elif rc == GONE:
                lines.append(f'absent {obj}/{f}')
            else:
                die(f'shield: cannot inspect {obj}/{f} ({out}); nothing changed')
    fd, tmp = tempfile.mkstemp(dir=os.path.dirname(rec), prefix=f'.{bp}.')
    try:
        with os.fdopen(fd, 'w') as f:
            f.write('\n'.join(lines) + '\n')
        os.replace(tmp, rec)
    except OSError as e:
        os.unlink(tmp)
        die(f'shield: cannot write {rec} ({e.strerror}); nothing changed')
    # mutate only the inodes just recorded, and roll back on the first failure so a
    # half-shield never survives; a rollback that itself fails keeps the record
    changed = []
    for name, ino, mode in attrs:
        rc, out = attr(f'{DEV}/{name}', ino, '000')
        if rc != DONE:
            restored, _, failed = restore(changed)
            if failed:
                die(f'shield: chmod 000 {name} failed ({out}); rollback incomplete: {" ".join(failed)}; '
                    f'record {rec} KEPT for unshield')
            os.unlink(rec)
            die(f'shield: chmod 000 {name} failed ({out}); rolled back {restored} attribute(s), no record kept')
        changed.append([name, ino, mode])
    print(f'shielded {bp}: {len(changed)} attribute(s) on {",".join(objs)} set 000; record {rec} (owner pid {pid})')
elif op == 'restore':
    rec, bp = args
    restored, gone, failed = restore(recorded(rec))
    if failed:
        die(f'unshield: could not restore {" ".join(failed)}; record {rec} kept (restored {restored}, gone {gone})')
    os.unlink(rec)
    print(f'unshielded {bp}: restored {restored} attribute(s), {gone} gone (re-enumerated or unplugged); record removed')
else:   # status REC...: "<still> <open> <gone> <unknown>" per record, in order
    for rec in args:
        n = [0, 0, 0, 0]
        for name, ino, _ in recorded(rec):
            rc, out = attr(f'{DEV}/{name}')
            if rc == DONE and out.split()[0] == ino:
                n[0 if out.split()[1] == '0' else 1] += 1
            elif rc == FAILED:
                n[3] += 1
            else:
                n[2] += 1
        print(*n)
PY
}

shield_objects() {
  # leaf, its parent hub, the root hub -- unique, in that order
  local bp=$1 bus=${1%%-*} parent
  if [[ "$bp" == *.* ]]; then parent=${bp%.*}; else parent="usb$bus"; fi
  echo "$bp"
  [ "$parent" != "usb$bus" ] && echo "$parent"
  echo "usb$bus"
}

proc_start() {  # start time of a pid (clock ticks since boot), '' when it is gone
  local stat
  stat=$(cat "/proc/$1/stat" 2>/dev/null) || return 0
  stat=${stat##*) }          # skip past the comm field, which may contain spaces
  set -- $stat
  echo "${20:-}"             # field 22 of the full line
}

owner_alive() {  # pid start boot -> 0 when that very process still runs
  [ "$3" = "$(cat /proc/sys/kernel/random/boot_id)" ] || return 1
  [ -n "$2" ] && [ "$(proc_start "$1")" = "$2" ]
}

record_owner() {  # the record's "owner pid start boot" fields, plus alive|dead
  local pid start boot
  read -r _ pid start boot < <(grep -m1 '^owner ' "$1")
  if owner_alive "$pid" "$start" "$boot"; then echo "$pid $start $boot alive"; else echo "$pid $start $boot dead"; fi
}

shield_lock() {
  mkdir -p -m 700 "$SHIELD_STATE" || die "cannot create $SHIELD_STATE"
  exec 9>>"$SHIELD_STATE/.lock" || die "cannot open $SHIELD_STATE/.lock"
  flock -w 30 9 || die "another shield/unshield has held $SHIELD_STATE/.lock for 30 s"
}

shield() {
  local bp=$1 pid=$2 start objs obj rec
  [[ "$bp" =~ $USBPATH_RE ]] || die "bad usb path: $bp"
  [[ "$pid" =~ ^[0-9]+$ ]] || die "shield: owner pid '$pid' is not a pid"
  start=$(proc_start "$pid")
  [ -n "$start" ] || die "shield: owner pid $pid is not a running process"
  objs=$(shield_objects "$bp")
  for obj in $objs; do
    [ -d "$SYSFS/bus/usb/devices/$obj/" ] || die "shield: no such usb object: $obj"
  done
  shield_lock
  # overlap: one owner's unshield must never drop another's root-hub shield
  for rec in "$SHIELD_STATE"/*; do
    [ -f "$rec" ] || continue
    for obj in $objs; do
      if grep -q "^obj $obj " "$rec"; then
        die "shield: $obj is already covered by the shield on $(basename "$rec") (owner $(record_owner "$rec")); refusing"
      fi
    done
  done
  attr_tx shield "$SHIELD_STATE/$bp" "owner $pid $start $(cat /proc/sys/kernel/random/boot_id)" "$bp" "$pid" $objs
}

unshield() {
  local bp=$1 pid=${2:-} rec owner
  [[ "$bp" =~ $USBPATH_RE ]] || die "bad usb path: $bp"
  shield_lock
  rec="$SHIELD_STATE/$bp"
  [ -f "$rec" ] || die "unshield: no shield record for $bp under $SHIELD_STATE"
  owner=$(record_owner "$rec")
  set -- $owner
  if [ "$4" = alive ] && [ "$pid" != "$1" ]; then
    die "unshield: $bp is shielded by live pid $1; pass that pid, or wait for it"
  fi
  attr_tx restore "$rec" "$bp"
}

shield_status() {
  local only=${1:-} rec bp still open gone unknown counts err recs=()
  # the state dir is root-owned 0700: anything but a listable dir or a missing one is "cannot
  # inspect", never "no shields"; the same for a record this caller cannot read
  err=$(LC_ALL=C ls -A -- "$SHIELD_STATE" 2>&1 >/dev/null) || {
    case "$err" in
      *"No such file"*) echo "no shields recorded under $SHIELD_STATE"; return 0 ;;
      *) die "shield-status: cannot inspect $SHIELD_STATE ($err; run with sudo)" ;;
    esac
  }
  # listable is not enough: without search permission every record reads as absent
  [ -x "$SHIELD_STATE" ] || die "shield-status: cannot inspect $SHIELD_STATE (no search permission; run with sudo)"
  for rec in "$SHIELD_STATE"/*; do
    [ -f "$rec" ] || continue
    [ -r "$rec" ] || die "shield-status: cannot inspect $rec (run with sudo)"
    bp=$(basename "$rec")
    [ -z "$only" ] || [ "$bp" = "$only" ] || continue
    recs+=("$rec")
  done
  [ ${#recs[@]} -gt 0 ] || { echo "no shields recorded under $SHIELD_STATE"; return 0; }
  counts=$(attr_tx status "${recs[@]}") || die "shield-status: cannot inspect the records"
  for rec in "${recs[@]}"; do
    read -r still open gone unknown
    set -- $(record_owner "$rec")
    echo "$(basename "$rec"): owner pid $1 $4; $still attribute(s) still shielded, $open recorded but not shielded, $gone gone, $unknown uninspectable; objects: $(grep '^obj ' "$rec" | cut -d' ' -f2 | tr '\n' ' ')"
  done <<< "$counts"
}

# Resolve a /dev node (ttyACMx, ttyUSBx, sgN, ...) up to its USB device busport.
resolve() {
  local node=$1 syspath dev
  [ -e "$node" ] || die "no such device node: $node"
  syspath=$(udevadm info -q path -n "$node" 2>/dev/null) || die "udevadm failed for $node"
  dev="$SYSFS$syspath"
  while [ "$dev" != "$SYSFS" ] && [ -n "$dev" ]; do
    if [ -e "$dev/busnum" ] && [ -e "$dev/devnum" ] && [ -e "$dev/authorized" ]; then
      basename "$dev"; return 0
    fi
    dev=$(dirname "$dev")
  done
  die "could not find parent USB device for $node"
}

action=${1:-}; target=${2:-}
[ -n "$action" ] || usage
[ -n "$target" ] || [ "$action" = shield-status ] || usage

case "$action" in
  shield)
    shield "$target" "${3:-}"
    ;;
  unshield)
    unshield "$target" "${3:-}"
    ;;
  shield-status)
    shield_status "$target"
    ;;
  resolve)
    resolve "$target"
    ;;
  authorized)
    [[ "$target" =~ $USBPATH_RE ]] || die "bad usb path: $target"
    d="$SYSFS/bus/usb/devices/$target"
    [ -e "$d/authorized" ] || die "no such usb device: $target"
    echo 0 > "$d/authorized"; sleep 1; echo 1 > "$d/authorized"
    echo "re-authorized $target"
    ;;
  pci-rebind)
    [[ "$target" =~ $PCI_RE ]] || die "bad pci addr: $target"
    require_usb_controller "$target"
    [ -e "$SYSFS/bus/pci/devices/$target/driver" ] || die "no driver bound to $target"
    drv=$(basename "$(readlink -f "$SYSFS/bus/pci/devices/$target/driver")")
    echo "$target" > "$SYSFS/bus/pci/drivers/$drv/unbind"; sleep 1
    echo "$target" > "$SYSFS/bus/pci/drivers/$drv/bind"
    echo "rebound pci $target ($drv)"
    ;;
  pci-bind)
    # Re-attach a driver to a controller left DRIVERLESS (e.g. a pci-rebind whose re-bind hung).
    [[ "$target" =~ $PCI_RE ]] || die "bad pci addr: $target"
    require_usb_controller "$target"
    [ -e "$SYSFS/bus/pci/devices/$target" ] || die "no such pci device: $target"
    [ -e "$SYSFS/bus/pci/devices/$target/driver" ] && die "$target already has a driver bound"
    drv=${3:-}
    if [ -n "$drv" ]; then
      [[ "$drv" =~ $DRIVER_RE ]] || die "bad driver name: $drv"
      [ -e "$SYSFS/bus/pci/drivers/$drv/bind" ] || die "no such pci driver: $drv"
      echo "$target" > "$SYSFS/bus/pci/drivers/$drv/bind"
      echo "bound pci $target ($drv)"
    else
      # Auto-try the xHCI drivers (Renesas uPD720201 uses xhci-pci-renesas; others xhci_hcd).
      for cand in xhci-pci-renesas xhci_hcd; do
        [ -e "$SYSFS/bus/pci/drivers/$cand/bind" ] || continue
        if echo "$target" > "$SYSFS/bus/pci/drivers/$cand/bind" 2>/dev/null; then
          echo "bound pci $target ($cand)"; exit 0
        fi
      done
      die "could not bind $target with a known xHCI driver; pass the driver explicitly"
    fi
    ;;
  root-cycle)
    # Port power off/on at the ROOT port. NOTE: a controller's ppps may only drop D+/D- (VBUS stays
    # up; the contract's Limits say what this host's does), so treat it as a forced re-enumeration,
    # not a power cycle. It goes straight at the root port -- no leaf walk (a leaf hub may claim
    # ganged switching and never cut power) -- with root_port_cycle.py's raw hub request, never the
    # port's `disable` file: disable_store usb_disconnect()s the child under the root hub's lock
    # before cutting power (v6.12.107 port.c:132-141), which a wedged child blocks. The requests'
    # exit status proves only the request; the sysfs_gen check below is the verdict.
    [[ "$target" =~ $USBPATH_RE ]] || refuse "bad usb path: $target"
    # Existence alone only proves *something* occupies that path -- bus numbers renumber every
    # boot, so a stale busport can name a different device entirely and we would cut power to its
    # whole subtree (every fixture under that root port). Callers that know what they expect pass the
    # serial as a third argument and we refuse on mismatch; otherwise print the identity so a
    # wrong target is at least visible.
    [ -e "$SYSFS/bus/usb/devices/$target" ] || refuse "no such usb device: $target"
    idf="$SYSFS/bus/usb/devices/$target"
    serial=$(lock_read "$idf/serial")
    expect=${3:-}
    if [ -n "$expect" ] && [ "$serial" = '?' ]; then
      # Warn and PROCEED: an unreadable serial is the wedge signature itself, so refusing
      # here would block the cure on exactly the condition it exists for. The identity
      # guard is lost for this call -- say so, because the cost of a wrong target is the
      # whole subtree.
      echo "root-cycle: WARNING $target's serial did not answer (it is wedged), so '$expect'" \
           "could NOT be confirmed; proceeding, but verify the busport if siblings drop" >&2
    elif [ -n "$expect" ] && [ "$expect" != "$serial" ]; then
      refuse "root-cycle: $target has serial '$serial', expected '$expect' — stale busport, refusing"
    fi
    # idVendor/idProduct are usb_descriptor_attr_le16: served WITHOUT the device lock, so
    # a plain cat is safe on a wedged device. serial/product are usb_string_attr and are not.
    echo "root-cycle: target $target is $(cat "$idf/idVendor" 2>/dev/null || echo -):$(cat "$idf/idProduct" 2>/dev/null || echo -)" \
         "serial=$serial product=$(lock_read "$idf/product")"
    bus=${target%%-*}; rest=${target#*-}; rootport=${rest%%.*}
    gen=$(sysfs_gen "$target")
    # check prints the root hub's identity token, which off and on must still find; the traps are
    # armed first, so a signal before the power-off ends the cycle with nothing switched
    tokf=$(mktemp) || die "root-cycle: cannot create a temporary file"
    trap 'rm -f "$tokf"' EXIT
    rc=0 token=''
    trap 'interrupted=yes' INT TERM HUP
    supervised "$ROOT_PORT_CYCLE" check "$bus" "$rootport" > "$tokf" || rc=$?
    read -r token < "$tokf" || :
    [ -z "$survivor" ] || die "root-cycle: check of bus $bus port $rootport failed (125): pid $survivor survived; nothing switched, no further request"
    [ -z "$interrupted" ] || die "root-cycle: interrupted before the power-off; nothing switched"
    [ $rc -ne 3 ] || refuse "root-cycle: bus $bus port $rootport refused (above); nothing switched"
    [ $rc -eq 0 ] || die "root-cycle: check of bus $bus port $rootport failed (${rc}); nothing switched"
    [[ "$token" =~ ^[0-9]+:[0-9]+:[0-9]+$ ]] || die "root-cycle: check printed no identity token; nothing switched"
    echo "root-cycle: cycling bus $bus root port $rootport (feeds $target, bounces its siblings; VBUS drop depends on the host's hub)"
    cycle_port "root-cycle: cycle of bus $bus port $rootport" "$ROOT_OFF_SECS" \
      "$ROOT_PORT_CYCLE" off "$bus" "$rootport" "$token" -- "$ROOT_PORT_CYCLE" on "$bus" "$rootport" "$token"
    if now=$(await_reenum "$target" "$gen"); then
      echo "root-cycled $bus port $rootport: $target re-enumerated"\
           "(devnum $(cat "$SYSFS/bus/usb/devices/$target/devnum" 2>/dev/null || echo ?), gen $gen -> $now)"; exit 0
    fi
    die "root-cycle: $target did not re-enumerate after cycling bus $bus port $rootport (sysfs generation still $gen: no disconnect happened)"
    ;;
  hub-cycle)
    # A targeted kernel port power-off/on request (`disable` 1, 2 s, 0) under the hub's lock.
    # Whether VBUS physically drops is the hub's: a ganged hub cuts the gang only when this was
    # the last port in it needing power, and restoring one port powers the gang (USB 2.0 11.11);
    # a hub without switches drops the port only logically; a USB 3 hub's companion port may keep
    # VBUS up (USB 3.2 Table 10-2). So success proves re-enumeration, not a power cycle or a
    # firmware restart; physical power guarantees belong to the rig contract's Limits.
    # `disable` rather than a raw hub request: disable_store runs under the hub's lock, after any
    # enumeration in progress, where a raw hub request that met an enumeration killed an AMD
    # 500-series xHCI (tinyuf2 bench history, 2026-10-14); that choice is made against that
    # incident, not verified under it. The price is root-cycle's hazard: disable_store
    # disconnects the child under the hub's lock first, so a wedged target would block it --
    # hence the serial that must answer, and a wedged leaf is left to root-cycle, whose request
    # has no child-disconnect in front and opens only the root hub's node, though a root hub lock
    # already held still blocks it.
    [[ "$target" =~ $USBPATH_RE ]] || refuse "bad usb path: $target"
    [[ "$target" == *.* ]] || refuse "hub-cycle: $target is on a root port; that is root-cycle's"
    hub=${target%.*}; port=${target##*.}
    idf="$SYSFS/bus/usb/devices/$target"; hubd="$SYSFS/bus/usb/devices/$hub"
    [ -e "$idf" ] || refuse "no such usb device: $target"
    # bDeviceClass is a lock-free descriptor attribute, safe on a wedged device
    cls=$(cat "$hubd/bDeviceClass" 2>/dev/null) || refuse "hub-cycle: cannot read $hub's class"
    [ "$cls" = 09 ] || refuse "hub-cycle: $hub is not a hub"
    cls=$(cat "$idf/bDeviceClass" 2>/dev/null) || refuse "hub-cycle: cannot read $target's class"
    [ "$cls" != 09 ] || refuse "hub-cycle: $target is a hub; only a leaf is cycled"
    disable="$hubd/$hub:1.0/$hub-port$port/disable"
    [ -f "$disable" ] || refuse "hub-cycle: no port control file $disable"
    serial=$(lock_read "$idf/serial")
    [ "$serial" != '?' ] || refuse "hub-cycle: $target's serial did not answer (wedged): the port write would block on its lock; root-cycle is next"
    [ -n "$serial" ] || refuse "hub-cycle: $target has no readable serial to prove it answers"
    expect=${3:-}
    [ -z "$expect" ] || [ "$expect" = "$serial" ] || refuse "hub-cycle: $target has serial '$serial', expected '$expect' — stale busport, refusing"
    gen=$(sysfs_gen "$target")
    echo "hub-cycle: target $target serial=$serial; writing 1, then 0 after ${PORT_OFF_SECS} s, to $disable"
    # each write is read back (both take the hub's lock) by the shell's own `read`, so the
    # bound's SIGKILL reaches every process doing the work
    write='echo "$2" > "$1" && read -r v < "$1" && [ "$v" = "$2" ]'
    cycle_port "hub-cycle: cycle of $hub port $port" "$PORT_OFF_SECS" \
      sh -c "$write" sh "$disable" 1 -- sh -c "$write" sh "$disable" 0
    if now=$(await_reenum "$target" "$gen"); then
      echo "hub-cycled $hub port $port: $target re-enumerated (gen $gen -> $now)"; exit 0
    fi
    die "hub-cycle: $target did not re-enumerate after cycling $hub port $port (sysfs generation still $gen)"
    ;;
  *)
    usage
    ;;
esac
