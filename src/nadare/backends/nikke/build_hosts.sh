#!/usr/bin/env bash
# SPDX-License-Identifier: GPL-3.0-or-later
# Copyright (C) 2026 Nadare contributors
# build_hosts.sh — reproducible build of the two Windows helpers that start
# NIKKE without nikke_launcher.exe (the INTL service host and the Sail host).
#
#   build_hosts.sh                 host mingw-w64 build into src/nadare/assets
#   build_hosts.sh --container     pinned Debian + mingw-w64 build (podman/docker)
#
# Both helpers ship prebuilt in src/nadare/assets so users need no compiler; the
# backend only uses a helper when it matches its pinned digest, and a test keeps
# the source and binary digests in step (tests/test_nikke_launcherless_hosts.py).
#
# Reproducible: no PE timestamp, stripped, no build paths, same toolchain.
# Windowless (-mwindows): a console-subsystem .exe makes Proton open a console window.
set -euo pipefail

here="$(cd "$(dirname "$0")" && pwd)"
assets="$here/../../assets"
src="$here/hosts_src"

build() {
    local out="$1"
    CC="${CC:-x86_64-w64-mingw32-gcc}"
    mkdir -p "$(dirname "$out")"
    "$CC" -O2 -s -mwindows -Wl,--no-insert-timestamp \
        -ffile-prefix-map="$here"=. \
        -DSTUB_VERSION="\"${VERSION:-nadare}\"" \
        -o "$out" "$2"
    echo "[+] $out"
    echo "    sha256 $(sha256sum "$out" | cut -d' ' -f1)"
    echo "    $("$CC" --version | head -1)"
}

if [ "${1:-}" = "--container" ]; then
    engine="$(command -v podman || command -v docker)" || { echo "need podman or docker" >&2; exit 1; }
    exec "$engine" run --rm -v "$here:/src:ro,Z" -v "$assets:/out:Z" -w /src \
        -e VERSION="${VERSION:-nadare}" docker.io/library/debian:trixie bash -c '
        set -e; export DEBIAN_FRONTEND=noninteractive
        apt-get -qq update >/dev/null
        apt-get -qq install -y --no-install-recommends gcc-mingw-w64-x86-64-win32 >/dev/null
        mkdir -p /tmp/build
        # Build in a writable copy: the source mount is read-only.
        cp -r /src /tmp/build/src
        cd /tmp/build/src
        OUT=/out CC=x86_64-w64-mingw32-gcc bash build_hosts.sh'
fi

outdir="${OUT:-$assets}"
build "$outdir/nikke_intlsvc_host.exe" "$src/intlsvc_host.c"
build "$outdir/nikke_sail_host.exe" "$src/sail_host.c"
