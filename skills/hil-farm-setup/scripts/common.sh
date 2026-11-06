# shellcheck shell=bash disable=SC2034  # used by the sourcing scripts
# Sourced by setup.sh and verify.sh: the farm's users, runners and the sshd policy, so both agree.
# The host's settings come from its host file (farm.py host-sh): ADMIN, SHARED_HOST, IAR_CHECKS, RUNNERS and each
# runner's GitHub registration name and labels. ADMIN_HOME is empty when the admin does not exist.
_host_sh=$(python3 -I "$(dirname "${BASH_SOURCE[0]}")/farm.py" host-sh) || return 1
eval "$_host_sh"
unset _host_sh
ADMIN_HOME=$(getent passwd "$ADMIN" | cut -d: -f6) || ADMIN_HOME=''
shared_host() { [ "$SHARED_HOST" = 1 ]; }
RUNNER_GROUPS=(hil dialout plugdev audio)
HIL_ROOT=/usr/local/sbin/hil_root       # ghrunner's only sudo command
HOOKS=/usr/local/libexec/hil-farm      # the runners' job hooks (job_hook.py)
# runner_conf NAME: set that runner's RUNNER_*; each runs as its own user, so neither reads the other's runner root
runner_conf() {
  [[ " ${RUNNERS[*]} " == *" $1 "* ]] || return 1
  case $1 in
    tinyusb) RUNNER_USER=ghrunner RUNNER_SUDO=$HIL_ROOT RUNNER_URL=https://github.com/hathach/tinyusb
             RUNNER_STATE='' ;;
    # tinyuf2's HIL state root: hil.yml's HIL_OUT parent, hil_remote.py's ciStateRoot
    tinyuf2) RUNNER_USER=ghrunner-tinyuf2 RUNNER_SUDO='' RUNNER_URL=https://github.com/adafruit/tinyuf2
             RUNNER_STATE=_work/tinyuf2/hil-state ;;
    arduino-tinyusb) RUNNER_USER=ghrunner-arduino-tinyusb RUNNER_SUDO=''
                    RUNNER_URL=https://github.com/adafruit/Adafruit_TinyUSB_Arduino
                    RUNNER_STATE=_work/Adafruit_TinyUSB_Arduino/hil-state ;;
    nrf52-bootloader) RUNNER_USER=ghrunner-nrf52-bootloader RUNNER_SUDO=''
                      RUNNER_URL=https://github.com/adafruit/Adafruit_nRF52_Bootloader
                      RUNNER_STATE=_work/Adafruit_nRF52_Bootloader/hil-state ;;
  esac
  RUNNER_REG_NAME=${RUNNER_REG_NAMES[$1]} RUNNER_LABELS=${RUNNER_REG_LABELS[$1]}  # its name and labels on GitHub
  RUNNER_NAME=$1
  RUNNER_DIR=/opt/actions-runner/$1  # farm.py's RUNNER_DIRS
  RUNNER_UNIT=hil-runner-$1          # farm.py's RUNNER_UNITS
}
runner_env() {  # the .env lines of the runner runner_conf selected; the sourcing script defines farm
  echo LANG=C.UTF-8 && farm env \
    && echo "ACTIONS_RUNNER_HOOK_JOB_STARTED=$HOOKS/job-started-$RUNNER_NAME.sh" \
    && echo "ACTIONS_RUNNER_HOOK_JOB_COMPLETED=$HOOKS/job-completed-$RUNNER_NAME.sh"
}
runner_users() { local n; for n in "${RUNNERS[@]}"; do (runner_conf "$n"; echo "$RUNNER_USER"); done; }
# sshd -T spelling; sshd_config keywords are case-insensitive, so setup.sh writes these lines as they are
SSHD_EXPECT=("pubkeyauthentication yes" "permitrootlogin no" "passwordauthentication no"
  "kbdinteractiveauthentication no" "allowagentforwarding no" "x11forwarding no" "allowusers $ADMIN")
sshd_effective() { sshd -T -C "user=${1:-$ADMIN},host=devpc,addr=192.0.2.1"; }  # [USER]
sshd_denies() {  # USER: sshd prints every denied user on one line
  sshd_effective "$1" | awk -v user="$1" '$1 == "denyusers" {for (i = 2; i <= NF; i++) if ($i == user) found = 1}
    END {exit !found}'
}
NFT_DROPIN=/etc/systemd/system/nftables.service.d/hil-farm.conf  # a shared host's: its rules and ours in one load
