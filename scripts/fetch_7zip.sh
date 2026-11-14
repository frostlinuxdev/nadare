#!/usr/bin/env bash
# SPDX-License-Identifier: GPL-3.0-or-later
# Build the bundled static 7-Zip (7zzs) from the pinned official source, WITHOUT the RAR code.
#
# The stock 7zzs includes unRAR, whose licence forbids using that code to re-create the RAR
# compression algorithm: a non-free restriction distributions reject (#1227 F5). Nadare only
# opens publisher installers (NSIS, PE, CAB, 7z, zip), never RAR, so the RAR code is deleted from
# the source before building and the result is checked to list no RAR format. Needs make + g++
# (the release build container has both). The output is reproducible: core/sevenzip.py pins it.
set -euo pipefail

root="$(cd "$(dirname "${BASH_SOURCE[0]}")/.." && pwd)"
url="https://github.com/ip7z/7zip/releases/download/26.05/7z2605-src.tar.xz"
expected="0b6080b8c3e7a579161d2d495224c0e51c467576cb4c7048b0ab485ef44cc22e"
assets="$root/src/nadare/assets"
binary="$assets/7zzs"
license="$assets/licenses/7zip-License.txt"
source_archive="${NADARE_7ZIP_SOURCE:-}"

mkdir -p "$assets/licenses"
tmp="$(mktemp -d)"
trap 'rm -rf "$tmp"' EXIT
if [[ -z "$source_archive" ]]; then
    source_archive="$tmp/7zip-src.tar.xz"
    curl -fL --retry 3 -o "$source_archive" "$url"
fi
actual="$(sha256sum "$source_archive" | cut -d' ' -f1)"
if [[ "$actual" != "$expected" ]]; then
    echo "[!] 7-Zip source SHA-256 mismatch: expected $expected, got $actual" >&2
    exit 1
fi
# A fixed path keeps the build reproducible (no random directory names in the object files).
work="$tmp/7zip-26.05"
mkdir "$work"
tar -xJf "$source_archive" -C "$work"
# The RAR handler and codecs go before the build, so not a line of unRAR code is compiled.
rm -rf "$work/CPP/7zip/Archive/Rar" "$work"/CPP/7zip/Compress/Rar*
(
    cd "$work/CPP/7zip/Bundles/Alone2"
    SOURCE_DATE_EPOCH=0 make -j"${JOBS:-4}" -f makefile.gcc DISABLE_RAR=1 COMPL_STATIC=1 >"$tmp/build.log" 2>&1 \
        || { tail -40 "$tmp/build.log" >&2; exit 1; }
)
built="$work/CPP/7zip/Bundles/Alone2/_o/7zzs"
test -s "$built"
"$built" i >"$tmp/formats.txt"
if grep -Eiq '\bRar[0-9]*\b' "$tmp/formats.txt"; then
    echo "[!] the built 7zzs still lists a RAR format" >&2
    exit 1
fi
for format in Nsis Cab PE 7z zip; do
    grep -qw "$format" "$tmp/formats.txt" || { echo "[!] 7zzs lacks the $format format" >&2; exit 1; }
done
install -m 0755 "$built" "$binary"
install -m 0644 "$work/DOC/License.txt" "$license"
echo "[*] 7-Zip (no RAR) built and installed: sha256 $(sha256sum "$binary" | cut -d' ' -f1)"
