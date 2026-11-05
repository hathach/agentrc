#!/usr/bin/env bash
# Provision a Debian 13 install as the HIL farm, from a checkout of this directory.
#   setup.sh install [--host FILE] [--inputs DIR]
#                              base install: packages, users, sudo, ssh, firewall, tools, rig config (re-runnable)
#   setup.sh update [--host FILE] [--inputs DIR] [TOOL...]
#                              converge tools, firmware, udev rules and the environment on this farm.toml;
#                              never users, sudo, ssh, firewall or the runner's registration
#   setup.sh add-runner NAME --host FILE
#                              add one runner to an installed host, disabled and unregistered; existing runners stay
#   setup.sh runner <tinyusb|tinyuf2|arduino-tinyusb> <token>
#                              register that runner as its user (token from the dev PC), its unit left disabled
#                              and stopped; enable --now it only after hardware proof
# --host FILE installs FILE as /etc/hil-farm/host.toml (last for add-runner); otherwise the installed one is used.
# --inputs DIR holds the files of farm.toml's kind "local" tools (STM32CubeProgrammer's tar), needed only to (re)install
# them. install, update and add-runner refuse while a runner service runs; stop it when idle, start after verify.sh.
# Run as root from a root-owned checkout with no uncommitted changes.
set -euo pipefail
umask 022  # whatever sudo passed: the runners read and traverse what root creates (tools, .env, profile.d)

B=$(cd "$(dirname "${BASH_SOURCE[0]}")" && pwd)
OS_RELEASE=/etc/os-release
die() { echo "setup: $*" >&2; exit 1; }
log() { echo "== $*"; }

bootstrap() {  # what common.sh needs before it can read the host file: farm.py's python3 (3.11+ for tomllib)
  [ "$(id -u)" = 0 ] || die "run as root"
  # shellcheck source=/dev/null
  . "$OS_RELEASE"
  [ "$ID" = debian ] && [ "$VERSION_ID" = 13 ] || die "expects Debian 13, found $PRETTY_NAME"
  command -v python3 >/dev/null || { apt-get update -qq && apt-get install -y -qq python3 >/dev/null; }
}

ARGV=("$@")  # the locked re-exec below gets them as given
CMD=${1:-} ARGS=() HOST_FILE='' INPUTS=''
shift || true
while [ $# -gt 0 ]; do
  case $1 in
    --host) [ $# -ge 2 ] && [ -n "$2" ] || die "--host needs a file"
            HOST_FILE=$(realpath -e -- "$2") || exit 1; shift 2 ;;
    --inputs) [ $# -ge 2 ] && [ -d "$2" ] || die "--inputs needs a directory"
              INPUTS=$(realpath -e -- "$2") || exit 1; shift 2 ;;
    *) ARGS+=("$1"); shift ;;
  esac
done
# every farm.py call below reads the proposed file; only --host sets one, so it always goes through host-install's
# checks, and empty (an inherited value overwritten) is the installed one
export HIL_FARM_HOST=$HOST_FILE
[ "${BASH_SOURCE[0]}" != "$0" ] || bootstrap
. "$B/common.sh"
FARM_HOME=/opt/hil-farm  # farm.py, farm.toml, the locks, verify.sh and fw/renesas_fw_check.sh; updates run from a new checkout
LOCK_FILE=/run/hil-farm.lock
RUNNER_USAGE="runner <$(IFS='|'; echo "${RUNNERS[*]}")> <token>"

farm() { python3 -I "$B/farm.py" "$@"; }

preflight() {
  SOURCE_REV=$(source_rev)
  id "$ADMIN" >/dev/null 2>&1 && [ -n "$ADMIN_HOME" ] || die "admin user $ADMIN missing (create it in the installer)"
  farm validate >/dev/null
}

# root runs this checkout and --host is one of its hosts/ files: refuse either if a non-root user could change it or
# it holds changes its commit does not, so SOURCE names the settings too
source_rev() {
  local bad st hosts
  hosts=$(realpath -m -- "$B/../hosts")
  [ -z "$HOST_FILE" ] || [ "${HOST_FILE%/*}" = "$hosts" ] || die "--host $HOST_FILE is not in $hosts; commit it there (README)"
  farm trusted "$B" ${HOST_FILE:+"$HOST_FILE"} >&2 \
    || die "a non-root user could replace $B or the host file; use a root-owned checkout (README)"
  # a link could lead anywhere
  bad=$(find "$B" ${HOST_FILE:+"$HOST_FILE"} \( -type l -o ! -user root -o -perm /022 \) -print -quit)
  [ -z "$bad" ] || die "$bad is a symlink, not root-owned or group/world-writable; use a root-owned checkout (README)"
  st=$(git -C "$B" status --porcelain --untracked-files=all -- . ${HOST_FILE:+"$HOST_FILE"}) || die "$B is not a git checkout"
  [ -z "$st" ] || die "$B holds changes no commit has; commit or remove them first:"$'\n'"$st"
  [ -z "$HOST_FILE" ] || git -C "$B" ls-files --error-unmatch -- "$HOST_FILE" >/dev/null 2>&1 \
    || die "$HOST_FILE is not committed; commit it first (README)"  # status omits an ignored file
  git -C "$B" rev-parse HEAD
}

# every line a public key entry (options allowed), at least one: ssh-keygen -lf also accepts a private key file
authorized_keys_ok() {  # FILE
  local line n=0
  while IFS= read -r line || [ -n "$line" ]; do
    [[ $line =~ ^[[:space:]]*(#|$) ]] && continue
    ssh-keygen -lf /dev/stdin <<<"$line" >/dev/null 2>&1 || return 1
    n=$((n + 1))
  done < "$1"
  [ "$n" -gt 0 ]
}

# sshd is about to accept keys only: the admin must already hold one sshd will use (StrictModes)
check_admin_key() {
  local home=$ADMIN_HOME keys d
  keys=$home/.ssh/authorized_keys
  [ -f "$keys" ] && [ ! -L "$keys" ] || die "$keys missing; give $ADMIN your key by cloud-init or ssh-copy-id first (README)"
  authorized_keys_ok "$keys" || die "$keys holds no valid key, or a line that is not one"
  for d in "$keys" "$home/.ssh" "$home"; do
    [ "$(stat -c %U "$d")" = "$ADMIN" ] || die "$d is not owned by $ADMIN"
    [ $(( 8#$(stat -c %a "$d") & 8#022 )) = 0 ] || die "$d is group/world-writable; sshd ignores its keys"
  done
  d=$(dirname "$home")
  while :; do
    [ "$(stat -c %U "$d")" = root ] && [ $(( 8#$(stat -c %a "$d") & 8#022 )) = 0 ] \
      || die "$d, above $home, is not root-owned 0755; sshd ignores the keys under it"
    [ "$d" != / ] || break
    d=$(dirname "$d")
  done
}

install_host() {  # before anything else changes, so a failed first install retries as this host
  [ -z "$HOST_FILE" ] || farm host-install "$HOST_FILE"
}

install_packages() {
  log packages
  export DEBIAN_FRONTEND=noninteractive
  # tshark asks whether non-root users may capture; no: usbmon access comes from the udev rule
  echo "wireshark-common wireshark-common/install-setuid boolean false" | debconf-set-selections
  apt-get update -qq
  local pkgs; pkgs=$(farm apt)
  # shellcheck disable=SC2086  # one package per line
  apt-get install -y -qq $pkgs >/dev/null
  ! shared_host || return 0  # the owner keeps the update policy
  cat > /etc/apt/apt.conf.d/20auto-upgrades <<'EOF'
APT::Periodic::Update-Package-Lists "1";
APT::Periodic::Unattended-Upgrade "1";
EOF
}

setup_users() {
  log users
  groupadd -f hil
  groupadd -f -r wireshark  # tshark creates it only for a setuid dumpcap; the usbmon udev rule needs it
  local sudoers=() name
  if shared_host; then   # the owner keeps its own sudo, keys and home; hil keeps its boards' access
    usermod -aG hil "$ADMIN"
  else
    usermod -aG sudo,hil,dialout,plugdev,audio,wireshark "$ADMIN"
    sudoers+=("$ADMIN ALL=(ALL) NOPASSWD: ALL")
    chmod 0700 "$ADMIN_HOME"
  fi
  for name in "${RUNNERS[@]}"; do
    setup_runner_user "$name"
    runner_conf "$name"
    [ -z "$RUNNER_SUDO" ] || sudoers+=("$RUNNER_USER ALL=(root) NOPASSWD: $RUNNER_SUDO")
  done

  printf '%s\n' "${sudoers[@]}" > /etc/sudoers.d/hil-farm
  chmod 0440 /etc/sudoers.d/hil-farm
  visudo -cq || die "sudoers check failed"
}

setup_runner_user() (
  runner_conf "$1"
  if ! id "$RUNNER_USER" >/dev/null 2>&1; then
    useradd -m -U -s /bin/bash -G "$(IFS=,; echo "${RUNNER_GROUPS[*]}")" "$RUNNER_USER"
  fi
  passwd -l "$RUNNER_USER" >/dev/null
  chmod 0750 "/home/$RUNNER_USER"
  # the admin reads each runner's root, _work and _diag through its group
  usermod -aG "$RUNNER_USER" "$ADMIN"
)

setup_ssh() {
  log ssh
  if shared_host; then  # the owner's sshd: only the runner users are refused
    local u
    echo "DenyUsers $(runner_users | paste -sd' ' -)" > /etc/ssh/sshd_config.d/10-hil-farm.conf
    sshd -t || die "sshd config invalid"
    while IFS= read -r u; do sshd_denies "$u" || die "effective sshd config does not deny $u"; done < <(runner_users)
    systemctl reload ssh
    return 0
  fi
  printf '%s\n' "${SSHD_EXPECT[@]}" > /etc/ssh/sshd_config.d/10-hil-farm.conf
  sshd -t || die "sshd config invalid"
  local eff kv
  eff=$(sshd_effective)
  for kv in "${SSHD_EXPECT[@]}"; do
    grep -qx "$kv" <<<"$eff" || die "effective sshd config lacks '$kv'"
  done
  systemctl reload ssh
  echo "   keep this session open until a new 'ssh $ADMIN@<this host> command -v python3' prints /opt/hil/venv/bin/python3"
}

setup_firewall() {
  log firewall
  local dns
  dns=$(awk '/^nameserver [0-9.]+$/{print $2}' /etc/resolv.conf | paste -sd, -)
  [ -n "$dns" ] || die "no IPv4 nameserver in /etc/resolv.conf"
  if shared_host; then shared_firewall "$dns"; return; fi
  nft_ruleset "$dns" > /etc/nftables.conf
  nft -c -f /etc/nftables.conf || die "nftables config invalid"
  systemctl enable -q nftables
  systemctl restart nftables
}

# The owner's /etc/nftables.conf stays its own: nftables.service loads it and the hil_farm table in one nft
# transaction, on start and reload, so its `flush ruleset` never leaves the runners unfiltered. Never enables the
# owner's firewall for it.
shared_firewall() {  # DNS
  local frag dropins changed rc=0
  systemctl is-enabled -q nftables || die "nftables.service is not enabled; the owner enables its firewall first"
  frag=$(systemctl show -p FragmentPath --value nftables) && dropins=$(systemctl show -p DropInPaths --value nftables) \
    || die "cannot inspect nftables.service"
  # the drop-in replaces Debian's ExecStart and ExecReload, which load /etc/nftables.conf; anything else is the owner's
  [ "$frag" = /usr/lib/systemd/system/nftables.service ] || die "nftables.service comes from $frag, not Debian's unit"
  changed=$(dpkg -V nftables) || rc=$?  # 1: differences, the owner's edited /etc/nftables.conf among them
  [ "$rc" -le 1 ] || die "cannot verify the nftables package (dpkg -V exited $rc)"
  ! grep -q ' /usr/lib/systemd/system/nftables.service$' <<<"$changed" || die "Debian's nftables.service was edited"
  [ -z "$dropins" ] || [ "$dropins" = "$NFT_DROPIN" ] \
    || die "nftables.service has drop-ins this setup would override: $dropins"
  install -d -m 0755 /etc/hil-farm "${NFT_DROPIN%/*}"
  { echo 'table inet hil_farm {}'; echo 'delete table inet hil_farm'; nft_table "$1"; } > /etc/hil-farm/hil_farm.nft
  printf 'include "%s"\n' /etc/nftables.conf /etc/hil-farm/hil_farm.nft > /etc/hil-farm/load.nft
  nft -c -f /etc/hil-farm/load.nft || die "nftables config invalid"
  printf '%s\n' '[Service]' 'ExecStart=' 'ExecStart=/usr/sbin/nft -f /etc/hil-farm/load.nft' \
    'ExecReload=' 'ExecReload=/usr/sbin/nft -f /etc/hil-farm/load.nft' > "$NFT_DROPIN"
  systemctl daemon-reload
  systemctl reload-or-restart nftables
}

nft_ruleset() {  # DNS: comma-separated IPv4 nameservers
  printf '%s\n' '#!/usr/sbin/nft -f' 'flush ruleset'
  nft_table "$1"
}

nft_table() {  # DNS
  local users
  users=$(runner_users | sed 's/.*/"&"/' | paste -sd, - | sed 's/,/, /g')
  # the runner users: IPv4 internet and DNS only, whatever the interface; no IPv6 policy yet, so no IPv6
  cat <<EOF
table inet hil_farm {
  chain output {
    type filter hook output priority filter; policy accept;
    meta skuid != { $users } accept
    ip daddr { $1 } meta l4proto { tcp, udp } th dport 53 accept
    ip daddr { 10.0.0.0/8, 100.64.0.0/10, 169.254.0.0/16, 172.16.0.0/12, 192.168.0.0/16, 224.0.0.0/4 } counter reject
    meta nfproto ipv6 oifname != "lo" counter reject
  }
}
EOF
}

install_tools() {  # [update] [TOOL...]
  log tools
  farm "$@" ${INPUTS:+--inputs "$INPUTS"}
  install_metadata
  write_env
}

install_metadata() {
  install -d -m 0755 "$FARM_HOME"
  install -m 0644 "$B/farm.toml" "$B"/*.lock "$B/common.sh" "$FARM_HOME/"
  install -m 0755 "$B/farm.py" "$B/verify.sh" "$FARM_HOME/"
  echo "agentrc $SOURCE_REV" > "$FARM_HOME/SOURCE"
  chmod 0644 "$FARM_HOME/SOURCE"
  install -D -m 0755 "$B/fw/renesas_fw_check.sh" "$FARM_HOME/fw/renesas_fw_check.sh"
}

write_env() {  # the farm's environment files and every installed runner's
  local path name
  for name in "${RUNNERS[@]}"; do write_runner_env "$name"; done
  if shared_host; then return 0; fi  # the owner's sessions keep their own environment
  path=$(farm path)
  # PATH for ssh commands (pam_env) and login shells; Debian's /etc/profile resets PATH
  { echo "PATH=\"$path\""; farm env; } > /etc/environment
  { echo "export PATH=\"$path\""; farm env | while IFS='=' read -r k v; do printf 'export %s=%q\n' "$k" "$v"; done
  } > /etc/profile.d/hil-farm.sh
}

write_runner_env() (  # NAME: its .env and .path, once installed
  runner_conf "$1"
  [ -d "$RUNNER_DIR/bin" ] || exit 0
  farm runner-path > "$RUNNER_DIR/.path"
  runner_env > "$RUNNER_DIR/.env"
)

install_rules() {
  printf 'usbmon\nusbtest\n' > /etc/modules-load.d/tinyusb.conf
  modprobe -a usbmon usbtest  # now, not only from the next boot
  install -m 0644 "$B"/udev/*.rules /etc/udev/rules.d/
  udevadm control --reload-rules
  apply_rules
}

# devices already attached keep their old group until a change event: a host with no reboot after setup (a shared host)
# block: USB drives only (TinyUF2's), and so a system disk only when it is on USB; a property match would AND with
# the others
apply_rules() {
  timeout 120 udevadm trigger --settle --action=change --subsystem-match=usb --subsystem-match=hidraw \
    --subsystem-match=tty --subsystem-match=usbmon \
    && timeout 120 udevadm trigger --settle --action=change --subsystem-match=block --property-match=ID_BUS=usb \
    || die "udev did not apply the rules to the attached devices"
}

setup_rig() {
  log rig
  install_rules
  # agentrc hil_lock shares its lock dir between users only when it is root-owned and sticky;
  # x keeps tmpfiles' /tmp age cleanup off held locks
  printf '%s\n' 'd /run/tinyusb-hil 0700 root root -' \
    'd /tmp/tinyusb-hil-locks 1777 root root -' 'x /tmp/tinyusb-hil-locks' > /etc/tmpfiles.d/hil-farm.conf
  systemd-tmpfiles --create /etc/tmpfiles.d/hil-farm.conf
}

install_job_hook() {  # shared by every runner: install and update only
  install -d -m 0755 "$HOOKS"
  install -m 0755 "$B/job_hook.py" "$HOOKS/"
}

install_hooks() (  # NAME: its pair; the runner's .env cannot pass its unit, so each wrapper names it
  runner_conf "$1"
  cat > "$HOOKS/job-started-$1.sh" <<EOF
#!/bin/sh
set -e
# runs as $RUNNER_USER before each job: refuse while an earlier job may have left processes behind,
# then drop what earlier jobs left in /tmp and the caches.
# _work is wiped by the workflow's own step; _work/hil-report stays for re-run attempts.
/usr/bin/python3 -I $HOOKS/job_hook.py start $RUNNER_UNIT
find /tmp -mindepth 1 -maxdepth 1 -user "\$(id -un)" -exec rm -rf {} +
rm -rf "\$HOME/.cache"
EOF
  cat > "$HOOKS/job-completed-$1.sh" <<EOF
#!/bin/sh
exec /usr/bin/python3 -I $HOOKS/job_hook.py complete $RUNNER_UNIT
EOF
  chmod 0755 "$HOOKS/job-started-$1.sh" "$HOOKS/job-completed-$1.sh"
)

setup_runners() {  # install: what the runners share, then each one's hooks and unit
  install_job_hook
  # apt packages, the same for every runner dir
  (runner_conf "${RUNNERS[0]}"; "$RUNNER_DIR/bin/installdependencies.sh" >/dev/null)
  local name
  for name in "${RUNNERS[@]}"; do setup_runner_service "$name"; done
}

setup_runner_service() (  # NAME: its hook pair and unit
  runner_conf "$1"
  log "runner service ($1)"
  install_hooks "$1"
  local dep=Requires=nftables.service hide=''
  # BindsTo: the runner also stops when the owner's nftables stops (its ExecStop flushes hil_farm too) or fails;
  # the unit never sees the owner's files
  if shared_host; then dep=BindsTo=nftables.service hide=$'\nInaccessiblePaths='$ADMIN_HOME; fi
  cat > "/etc/systemd/system/$RUNNER_UNIT.service" <<EOF
[Unit]
Description=GitHub Actions runner ($RUNNER_NAME HIL)
After=network-online.target nftables.service
Wants=network-online.target
$dep
ConditionPathExists=$RUNNER_DIR/.runner

[Service]
ExecStart=$RUNNER_DIR/runsvc.sh
User=$RUNNER_USER
WorkingDirectory=$RUNNER_DIR
# the admin, in the runner's group, reads job files; group write would let it add files a job cannot remove
UMask=0022
# upstream's template uses KillMode=process, which leaves a job's flashers running
KillMode=control-group
TimeoutStopSec=5min$hide

[Install]
WantedBy=multi-user.target
EOF
  systemctl daemon-reload
)

# a registered runner's jobs could plant links there; root's install -d, chmod and chown would follow them
runner_dirs_ok() {  # each runner-owned dir, and every component of its path under RUNNER_DIR, a real dir or missing
  local rel p part parts
  for rel in _work _diag ${RUNNER_STATE:+"$RUNNER_STATE"}; do
    p=$RUNNER_DIR
    IFS=/ read -ra parts <<<"$rel"
    for part in "${parts[@]}"; do
      p=$p/$part
      [ ! -L "$p" ] || die "$p is a symlink; a runner's tree is its own"
      [ ! -e "$p" ] || [ -d "$p" ] || die "$p is not a directory"
    done
  done
}

add_runner() {  # NAME: the proposed host file differs only by this runner
  farm host-install "$HOST_FILE" --add-runner "$1" --check
  # The shared runner receipt must still match the pin, and no old runner may need installing.
  HIL_FARM_HOST='' farm verify actions-runner
  runner_conf "$1"
  # setup_users writes the sudoers file whole; a sudo-bearing runner comes with install
  [ -z "$RUNNER_SUDO" ] || die "$1 needs sudo ($RUNNER_SUDO): add it with setup.sh install"
  [ ! -e "$RUNNER_DIR/.runner" ] || die "$RUNNER_DIR is already registered"
  # install_metadata publishes the checkout's pins: they must be the installed ones
  local f
  for f in "$B/farm.toml" "$B"/*.lock; do
    cmp -s "$f" "$FARM_HOME/${f##*/}" || die "${f##*/} differs from the installed one; run setup.sh update first"
  done
  setup_runner_user "$1"
  shared_host && setup_ssh
  setup_firewall
  farm install actions-runner
  runner_dirs_ok
  write_runner_env "$1"
  runner_root_mode
  setup_runner_service "$1"
  systemctl disable -q "$RUNNER_UNIT" || die "cannot disable $RUNNER_UNIT"
  install_metadata
  # Publish last: a failed addition retries against the unchanged installed host.
  farm host-install "$HOST_FILE" --add-runner "$1"
  log "added $1, disabled; check access, then setup.sh runner $1 <token> and hardware proof (README)"
}

runner_root_mode() {  # root's, runner-group-writable and sticky: register_runner says why
  chown root:"$RUNNER_USER" "$RUNNER_DIR"
  chmod 1770 "$RUNNER_DIR"
}

register_runner() {  # NAME TOKEN, checked by the caller
  local token=$2
  runner_conf "$1"
  if [ -n "$RUNNER_SUDO" ]; then
    [ -x "$RUNNER_SUDO" ] || die "$RUNNER_SUDO not installed; the $1 runner waits for it"
  fi
  [ -d "$RUNNER_DIR/bin" ] || die "runner not installed; run setup.sh install first"
  ! systemctl is-active -q "$RUNNER_UNIT" || die "$RUNNER_UNIT is running; stop it when idle first"
  runner_dirs_ok
  setup_runner_service "$1"
  # disabled and stopped until its hardware proof, a reboot included
  systemctl disable -q "$RUNNER_UNIT" || die "cannot disable $RUNNER_UNIT"
  log "runner registration ($1)"
  # config.sh refuses root and writes its config into the runner root; 0700 until .credentials is 0600
  chown "$RUNNER_USER:$RUNNER_USER" "$RUNNER_DIR"
  chmod 0700 "$RUNNER_DIR"
  # hil_remote.py refuses until the state root exists, which otherwise only the first CI job makes
  install -d -m 0750 -o "$RUNNER_USER" -g "$RUNNER_USER" "$RUNNER_DIR/_work" "$RUNNER_DIR/_diag" \
    ${RUNNER_STATE:+"$RUNNER_DIR/$RUNNER_STATE"}
  runuser -u "$RUNNER_USER" -- env -C "$RUNNER_DIR" PATH="$(farm path)" ./config.sh --unattended --replace \
    --url "$RUNNER_URL" --token "$token" --name "$RUNNER_REG_NAME" \
    --labels "$RUNNER_LABELS" --work _work --disableupdate
  install -m 0755 "$RUNNER_DIR/bin/runsvc.sh" "$RUNNER_DIR/runsvc.sh"
  write_runner_env "$1"
  # Binaries, scripts, .env and .path root-owned. The root dir must stay runner-writable:
  # v2.337 deletes and rewrites .runner_migrated/.credentials_migrated there. Sticky keeps root's
  # files, but runner-owned config persists across jobs: acceptable only for trusted runs.
  # Group = the runner's own, which holds the admin too; nothing for others, the other runner included.
  chown -R root:root "$RUNNER_DIR"
  runner_dirs_ok   # again, now that no runner process can change the tree: config.sh ran as the runner
  chown -R "$RUNNER_USER:$RUNNER_USER" "$RUNNER_DIR/_work" "$RUNNER_DIR/_diag"
  chmod 0750 "$RUNNER_DIR/_work" "$RUNNER_DIR/_diag"
  [ -f "$RUNNER_DIR/.runner" ] && [ ! -L "$RUNNER_DIR/.runner" ] || die "config.sh left no $RUNNER_DIR/.runner"
  find "$RUNNER_DIR" -maxdepth 1 -type f \( -name .runner -o -name '.credentials*' \) \
    -exec chown -h "$RUNNER_USER:$RUNNER_USER" {} + -exec chmod 0600 {} +
  runner_root_mode
  log "registered; after hardware proof: systemctl enable --now $RUNNER_UNIT"
}

[ "${BASH_SOURCE[0]}" = "$0" ] || return 0  # sourced by the tests for its functions
# one setup at a time; farm.py sees HIL_FARM_LOCK_HELD and leaves the lock to this run
if [ "${HIL_FARM_LOCK_HELD:-}" != 1 ]; then
  rc=0
  HIL_FARM_LOCK_HELD=1 flock -n -E 75 "$LOCK_FILE" "$0" ${ARGV[@]+"${ARGV[@]}"} || rc=$?
  [ "$rc" != 75 ] || die "another setup holds $LOCK_FILE"
  exit "$rc"
fi

case "$CMD" in
  install)
    [ ${#ARGS[@]} = 0 ] || die "usage: setup.sh install [--host FILE] [--inputs DIR]"
    preflight
    shared_host || check_admin_key
    farm guard
    install_host
    install_packages
    setup_users
    setup_ssh
    setup_firewall
    install_tools install
    setup_rig
    setup_runners
    log "done; reboot to load the Renesas firmware, then run $FARM_HOME/verify.sh"
    ;;
  update)
    preflight
    farm guard
    install_host
    install_tools update ${ARGS[@]+"${ARGS[@]}"}
    install_rules
    install_job_hook
    for name in "${RUNNERS[@]}"; do setup_runner_service "$name"; done
    log "done; run $FARM_HOME/verify.sh --maintenance, then start the runner units (reboot first if firmware changed)"
    ;;
  add-runner)
    if [ -z "$HOST_FILE" ] || [ -n "$INPUTS" ] || [ ${#ARGS[@]} != 1 ] || ! runner_conf "${ARGS[0]}"; then
      die "usage: setup.sh add-runner NAME --host FILE"
    fi
    preflight
    add_runner "${ARGS[0]}"
    ;;
  runner)
    if [ -n "$HOST_FILE$INPUTS" ] || [ ${#ARGS[@]} != 2 ] || [ -z "${ARGS[1]}" ] || ! runner_conf "${ARGS[0]}"; then
      die "usage: setup.sh $RUNNER_USAGE"
    fi
    preflight
    register_runner "${ARGS[0]}" "${ARGS[1]}"
    ;;
  *) die "usage: setup.sh install [--host FILE] [--inputs DIR] | update [--host FILE] [--inputs DIR] [TOOL...]" \
       "| add-runner NAME --host FILE | $RUNNER_USAGE" ;;
esac
