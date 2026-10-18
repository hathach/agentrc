#!/usr/bin/env bash
# usb_dyndbg.sh — toggle kernel dynamic-debug on USB drivers (host or gadget
# side); run with sudo. Flips +p/-p only on an allowlisted set of USB modules,
# so it can't reach arbitrary kernel debug or unrelated subsystems.
set -euo pipefail

CTL=${USB_DYNDBG_CTL:-/sys/kernel/debug/dynamic_debug/control}
# Allowlist: USB core + host-controller + common class drivers, plus the
# gadget/UDC side of a Linux peer (dwc2/dwc3, udc_core, libcomposite).
ALLOW='usbcore xhci_hcd xhci_pci xhci_pci_renesas ehci_hcd ehci_pci ohci_hcd ohci_pci uhci_hcd dwc2 dwc3 cdc_acm usb_storage uas libcomposite udc_core'

die() { echo "usb_dyndbg: $*" >&2; exit 1; }
help() {
  cat <<EOF
usage: sudo usb_dyndbg.sh on  <module>...   enable the print flag (+p) at every site of each module
       sudo usb_dyndbg.sh off <module>...   disable it (-p); always do this when done, it is very noisy
                                            both read the flags back and fail unless every site changed;
                                            a module with no site at all (not loaded?) is an error
       sudo usb_dyndbg.sh status [module]   sites with the print flag set, for one module or every allowlisted one

modules (host side, then a Linux gadget peer's device side):
  $ALLOW
Pick the host-controller module from \`lsusb -t\` (Driver=); usbcore covers enumeration and hub logic.
Needs CONFIG_DYNAMIC_DEBUG and a mounted debugfs ($CTL).
EOF
}
usage() { help >&2; exit 2; }
allowed() { local m; for m in $ALLOW; do [ "$m" = "$1" ] && return 0; done; return 1; }

# Control lines are: file:line [module]function =flags "format"; `p` in flags
# is the print flag, `=_` means none set.
sites() {  # sites <module|-> [all] : lines of the allowlisted module(s), with p set unless "all"
  # gawk skips an unreadable operand with exit 0; cat fails, and pipefail carries it
  cat -- "$CTL" | awk -v module="$1" -v every="${2:-}" -v allow=" $ALLOW " '
    {
      m = $2; sub(/\].*/, "", m); sub(/^\[/, "", m)
      if (module != "-" ? m != module : index(allow, " " m " ") == 0) next
      if (every == "all" || $3 ~ /^=[a-z_]*p/) print
    }'
}
count() {  # count <module> [all] : how many such sites; fails when the control file cannot be read
  local out
  out=$(sites "$@") || return 1
  [ -z "$out" ] && echo 0 || printf '%s\n' "$out" | wc -l
}
require_ctl() {
  [ -e "$CTL" ] && return
  [ -e "${CTL%/*/*}" ] && [ ! -r "${CTL%/*/*}" ] && die "cannot access ${CTL%/*/*} (run with sudo?)"
  die "dynamic_debug unavailable (need CONFIG_DYNAMIC_DEBUG + debugfs mounted at $CTL)"
}

action=${1:-}; shift || true
case "$action" in -h|--help|help) ;; *)
  [ "$(uname -s)" = Linux ] || die "Linux-only (kernel dynamic debug); this is $(uname -s)" ;;
esac
case "$action" in
  -h|--help|help)
    help
    ;;
  on|off)
    [ "$#" -ge 1 ] || usage
    require_ctl
    flag='+p'; [ "$action" = off ] && flag='-p'
    for m in "$@"; do allowed "$m" || die "module not allowlisted: $m"; done
    for m in "$@"; do
      total=$(count "$m" all) || die "cannot read $CTL (run with sudo?)"
      [ "$total" -gt 0 ] || die "module $m has no dynamic-debug site: not loaded (lsmod), or built without them"
      echo "module $m $flag" > "$CTL" || die "cannot write $CTL (run with sudo?)"
      total=$(count "$m" all) && printing=$(count "$m") || die "cannot read $CTL back"
      want=$total; [ "$action" = off ] && want=0
      [ "$printing" -eq "$want" ] ||
        die "wrote '$flag' for $m, but $printing of its $total sites print (expected $want)"
      echo "dynamic debug $action: $m ($printing of $total sites print)"
    done
    ;;
  status)
    [ "$#" -le 1 ] || usage
    m=${1:--}
    [ "$m" = - ] || allowed "$m" || die "module not allowlisted: $m"
    require_ctl
    [ -r "$CTL" ] || die "cannot read $CTL (run with sudo?)"
    out=$(sites "$m") || die "cannot read $CTL"
    total=1; [ "$m" = - ] || total=$(count "$m" all) || die "cannot read $CTL"
    if [ -n "$out" ]; then
      printf '%s\n' "$out"
    elif [ "$m" = - ]; then
      echo "(no print sites enabled in any allowlisted module)"
    elif [ "$total" -eq 0 ]; then
      echo "(module $m has no dynamic-debug site: not loaded, or built without them)"
    else
      echo "(no print sites enabled for $m)"
    fi
    ;;
  *)
    usage
    ;;
esac
