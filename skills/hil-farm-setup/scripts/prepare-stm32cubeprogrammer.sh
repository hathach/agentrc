#!/usr/bin/env bash
# Pack a Linux x86-64 STM32CubeProgrammer install into the tar farm.toml pins, for setup.sh --inputs DIR.
# ST's download (https://www.st.com/en/development-tools/stm32cubeprog.html) needs a login, so the farm cannot fetch it.
# Usage: prepare-stm32cubeprogrammer.sh <install-dir> <out-dir>
#   install-dir: the installer's STM32CubeProgrammer directory, e.g. ~/STMicroelectronics/STM32Cube/STM32CubeProgrammer
set -euo pipefail

die() { echo "prepare: $*" >&2; exit 1; }
[ $# = 2 ] || die "usage: prepare-stm32cubeprogrammer.sh <install-dir> <out-dir>"
HERE=$(cd "$(dirname "$0")" && pwd)
IN=$(realpath -e -- "$1") || die "no $1"
OUT=$2
[ "$(basename "$IN")" = STM32CubeProgrammer ] || die "$IN: name the STM32CubeProgrammer directory itself"
[ -x "$IN/bin/STM32_Programmer_CLI" ] || die "$IN holds no bin/STM32_Programmer_CLI"
[ -d "$OUT" ] || die "no directory $OUT"
read -r file sha version < <(python3 -I -c 'import sys, tomllib
t = tomllib.load(open(sys.argv[1], "rb"))["tool"]["stm32cubeprogrammer"]
print(t["file"], t["sha256"], t["version"])' "$HERE/farm.toml")
[ ! -e "$OUT/$file" ] || die "$OUT/$file exists; remove it first"

# reproducible, so farm.toml can pin it; SVD files, the updater and the uninstaller only serve the GUI
tmp=$(mktemp "$OUT/.$file.XXXXXX")
trap 'rm -f "$tmp"' EXIT
tar -C "$(dirname "$IN")" -cf "$tmp" --sort=name --mtime=@0 --owner=0 --group=0 --numeric-owner \
  --exclude='STM32CubeProgrammer/SVD' --exclude='STM32CubeProgrammer/updater' \
  --exclude='STM32CubeProgrammer/uninstaller' --exclude='hs_err_pid*.log' STM32CubeProgrammer
got=$(sha256sum "$tmp" | cut -d' ' -f1)
[ "$got" = "$sha" ] || die "$IN packs to $got, farm.toml pins $sha ($version); install $version, or update the pin"
chmod 0644 "$tmp"
mv "$tmp" "$OUT/$file"
trap - EXIT
echo "prepare: $OUT/$file ($version, sha256 $sha)"
