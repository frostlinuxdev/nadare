#!/usr/bin/env bash
# SPDX-License-Identifier: GPL-3.0-or-later
# Fetch the pinned official static 7-Zip binary and its licence.
set -euo pipefail

root="$(cd "$(dirname "${BASH_SOURCE[0]}")/.." && pwd)"
url="https://github.com/ip7z/7zip/releases/download/26.05/7z2605-linux-x64.tar.xz"
expected="22cf82fcc6eb7474f1d5bb4f02c0f8ebd573a718089d6833dc30f8350a3b232e"
assets="$root/src/nadare/assets"
binary="$assets/7zzs"
license="$assets/licenses/7zip-License.txt"

mkdir -p "$assets/licenses"
tmp="$(mktemp -d)"
trap 'rm -rf "$tmp"' EXIT
curl -fL --retry 3 -o "$tmp/7zip.tar.xz" "$url"
actual="$(sha256sum "$tmp/7zip.tar.xz" | cut -d' ' -f1)"
if [[ "$actual" != "$expected" ]]; then
    echo "[!] 7-Zip tarball SHA-256 mismatch: expected $expected, got $actual" >&2
    exit 1
fi
tar -xJf "$tmp/7zip.tar.xz" -C "$tmp"
test -s "$tmp/7zzs"
test -s "$tmp/License.txt"
install -m 0755 "$tmp/7zzs" "$binary"
install -m 0644 "$tmp/License.txt" "$license"
echo "[*] 7-Zip binary and licence installed (sha256 $expected)"
