#!/usr/bin/env bash
# Check every Renesas uPD720201/202 controller is bound to xhci-pci-renesas and running firmware
# 2.0.2.6 or later. On older firmware the command ring dies under usbtest unlink stress and the host needs a
# power cycle. Nothing else checks it: run this after every reboot (verify.sh runs it too). Run as
# root. One ok/FAIL line per check; exits 1 on any FAIL.
set -uo pipefail

fails=0
ok()   { echo "ok    $*"; }
fail() { echo "FAIL  $*"; fails=$((fails + 1)); }

# 1912:0014 uPD720201, 1912:0015 uPD720202; since v6.12 xhci-pci rejects both, so only
# xhci-pci-renesas may bind
if ! out=$(lspci -Dn -d 1912:); then
  fail "lspci -d 1912: failed"
  exit 1
fi
bdfs=$(awk '$3 == "1912:0014" || $3 == "1912:0015" {print $1}' <<<"$out")
for bdf in $bdfs; do
  # ffffffff = config space unreadable, not a firmware version; the version is bits 23:8 (fw/README.md)
  ver=$(setpci -s "$bdf" 0x6c.l) && [[ $ver =~ ^[0-9a-f]{8}$ ]] && [ "$ver" != ffffffff ] \
    && [ $(((16#$ver >> 8) & 0xffff)) -ge $((16#2026)) ] \
    && ok "$bdf firmware $ver" || fail "$bdf firmware '$ver', want version 2026 (2.0.2.6) or later in bits 23:8"
  drv=$(basename "$(readlink -f "/sys/bus/pci/devices/$bdf/driver" 2>/dev/null)")
  [ "$drv" = xhci-pci-renesas ] && ok "$bdf driver $drv" || fail "$bdf driver '$drv'"
done
[ "$fails" = 0 ]
