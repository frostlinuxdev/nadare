#!/usr/bin/env bash
# SPDX-License-Identifier: GPL-3.0-or-later
# Copyright (C) 2026 Nadare contributors
#
# Build the Chaos Zero Nightmare PCSDK stub (a tiny Windows .exe) with mingw-w64.
#
#   scripts/build_stub.sh                # host build -> dist/czn_pcsdk_stub.exe
#   scripts/build_stub.sh /path/out.exe  # explicit output
#   scripts/build_stub.sh --container    # reproducible pinned-Debian build
#
# Nadare builds this stub automatically at CZN install time if it is missing;
# this script is for packaging and for verifying a standalone build.
#
# Requirements:
#   host build:      x86_64-w64-mingw32-gcc  (mingw-w64 / gcc-mingw-w64-x86-64)
#   --container:     podman or docker (builds in a pinned Debian + apt snapshot)
set -euo pipefail

root="$(cd "$(dirname "${BASH_SOURCE[0]}")/.." && pwd)"
real="$root/src/nadare/backends/czn/build_stub.sh"

[[ -f "$real" ]] || { echo "[!] missing $real" >&2; exit 1; }

out="${CZN_STUB_OUT:-$root/dist/czn_pcsdk_stub.exe}"

# --container does not need a host compiler, so only check for a host build.
if [[ "${1:-}" != "--container" ]]; then
    if ! command -v "${CC:-x86_64-w64-mingw32-gcc}" >/dev/null 2>&1; then
        cat >&2 <<'EOF'
[!] mingw-w64 cross compiler not found.
    Arch:    sudo pacman -S mingw-w64-gcc
    Fedora:  sudo dnf install mingw64-gcc
    Debian:  sudo apt install gcc-mingw-w64-x86-64
    Or build in a container: scripts/build_stub.sh --container
EOF
        exit 1
    fi
fi

mkdir -p "$(dirname "$out")"
echo "[*] Building the PCSDK stub -> $out"
exec env CZN_STUB_OUT="$out" bash "$real" "$@"
