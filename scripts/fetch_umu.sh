#!/usr/bin/env bash
# SPDX-License-Identifier: GPL-3.0-or-later
# Put the pinned umu-launcher zipapp archive into the bundle (#1338).
#
# Every Proton launch goes through umu (user decision 2026-11-14). The AppImage carries this
# archive so Nadare's managed umu is installed from it with no network (core/umu.py
# BUNDLED_ARCHIVE). Version and sha256 must match core/umu.py UMU_VERSION / UMU_TAR_SHA256;
# tests/test_umu_always.py checks that. NADARE_UMU_ARCHIVE uses a local copy (offline builds).
set -euo pipefail

root="$(cd "$(dirname "${BASH_SOURCE[0]}")/.." && pwd)"
version="1.4.4"
expected="eb590691841f7fad3fc3ad8fd5db4ccb87849fe7948e62b28ece7a4ee48cc851"
url="https://github.com/Open-Wine-Components/umu-launcher/releases/download/${version}/umu-launcher-${version}-zipapp.tar"
dest="$root/src/nadare/assets/umu-launcher-${version}-zipapp.tar"

if [[ -f "$dest" ]] && [[ "$(sha256sum "$dest" | cut -d' ' -f1)" == "$expected" ]]; then
    echo "[*] umu ${version} archive already bundled"
    exit 0
fi
tmp="$(mktemp -d)"
trap 'rm -rf "$tmp"' EXIT
archive="${NADARE_UMU_ARCHIVE:-}"
if [[ -z "$archive" ]]; then
    archive="$tmp/umu.tar"
    curl -fL --retry 3 -o "$archive" "$url"
fi
actual="$(sha256sum "$archive" | cut -d' ' -f1)"
if [[ "$actual" != "$expected" ]]; then
    echo "[!] umu archive SHA-256 mismatch: expected $expected, got $actual" >&2
    exit 1
fi
tar -tf "$archive" | grep -qx "umu/umu-run" || { echo "[!] the archive has no umu/umu-run" >&2; exit 1; }
install -m 0644 "$archive" "$dest"
echo "[*] umu ${version} archive bundled: $dest"
