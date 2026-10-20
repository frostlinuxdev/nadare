#!/usr/bin/env bash
# SPDX-License-Identifier: GPL-3.0-or-later
# Copyright (C) 2026 Nadare contributors
# Derived from czn-linux scripts/build_stub.sh (GPL-3.0-or-later).
# build_stub.sh — reproducible build of the STOVE PCSDK stub for Chaos Zero
# Nightmare, no STOVE launcher required.
#
#   build_stub.sh [OUT_EXE]              host mingw-w64 build
#   build_stub.sh --container [OUT_EXE]  pinned Debian + snapshot build
#
# The output path can also come from $CZN_STUB_OUT. Nadare's backend sets that
# to <profile data dir>/stub/czn_pcsdk_stub.exe; when run by hand the default is
# $XDG_DATA_HOME/nadare/stub/czn_pcsdk_stub.exe.
#
# Reproducible: no PE timestamp, stripped, no build paths, same toolchain.
set -euo pipefail

# Debian trixie image (pinned by digest) + its package archive frozen at
# SNAPSHOT via snapshot.debian.org, so the compiler never drifts:
# gcc-mingw-w64-x86-64-win32 14.2.0-19+27+b1, binutils 2.44, mingw-w64 12.0.0.
IMAGE="docker.io/library/debian@sha256:bcf83fd8af414eda0c0a99d25a4e61dedd88e7aac7f7bc519d04555ff0c40ec5"
SNAPSHOT="20261001T000000Z"

here="$(cd "$(dirname "$0")" && pwd)"
pkg="$here"
src="$here/stub_src/czn_pcsdk_stub.c"

default_out="${XDG_DATA_HOME:-$HOME/.local/share}/nadare/stub/czn_pcsdk_stub.exe"

if [ "${1:-}" = "--container" ]; then
    shift
    engine="$(command -v podman || command -v docker)" || { echo "need podman or docker" >&2; exit 1; }
    out="${1:-${CZN_STUB_OUT:-$default_out}}"
    outdir="$(cd "$(dirname "$out")" && pwd)"
    exec "$engine" run --rm -v "$pkg:/src:ro,Z" -v "$outdir:/out:Z" -w /src -e SNAPSHOT="$SNAPSHOT" \
        -e CZN_STUB_OUT="/out/$(basename "$out")" "$IMAGE" bash -c '
        set -e; export DEBIAN_FRONTEND=noninteractive
        # ca-certificates (not part of the build output) to reach the snapshot over https
        apt-get -qq update >/dev/null
        apt-get -qq install -y --no-install-recommends ca-certificates git >/dev/null
        apt-get -qq -o Acquire::Check-Valid-Until=false --snapshot "$SNAPSHOT" update >/dev/null 2>&1
        apt-get -qq --snapshot "$SNAPSHOT" install -y --no-install-recommends \
            gcc-mingw-w64-x86-64-win32 >/dev/null
        git config --global --add safe.directory /src
        bash build_stub.sh'
fi

out="${1:-${CZN_STUB_OUT:-$default_out}}"
# Fixed default so host and container builds are byte-identical.
VERSION="${VERSION:-${STUB_VERSION:-nadare}}"
VERSION="${VERSION#v}"
CC="${CC:-x86_64-w64-mingw32-gcc}"
mkdir -p "$(dirname "$out")"

"$CC" -O2 -mwindows -s -Wl,--no-insert-timestamp \
    -ffile-prefix-map="$pkg"=. \
    -DSTUB_VERSION="\"$VERSION\"" \
    -o "$out" "$src" -lbcrypt -lcrypt32

echo "[+] $out"
echo "    sha256 $(sha256sum "$out" | cut -d' ' -f1)"
echo "    $("$CC" --version | head -1)"
