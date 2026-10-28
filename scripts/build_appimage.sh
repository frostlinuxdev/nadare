#!/usr/bin/env bash
# SPDX-License-Identifier: GPL-3.0-or-later
# Copyright (C) 2026 Nadare contributors
#
# Build Nadare as an AppImage.
#
#   scripts/build_appimage.sh
#
# Steps:
#   1. PyInstaller onedir bundle  -> dist/nadare/
#   2. appimagetool wraps it      -> dist/Nadare-<version>-x86_64.AppImage
#
# Build on the oldest distro you intend to support: an AppImage still runs
# against the host glibc, so the bundle must not require a newer GLIBC than the
# target. The release build is done on Ubuntu 22.04 (glibc 2.35).
#
# Requirements (build host):
#   uv sync --frozen --extra build   # PyInstaller and the locked runtime set (uv.lock)
#   appimagetool                     # on PATH, or set APPIMAGETOOL=/path
#
# `appimage-builder` is intentionally not used: on Ubuntu 22.04 it drags in a
# large apt dependency closure and resolves libraries with a Python 3.10-era
# stack. appimagetool only needs the PyInstaller bundle and a static runtime.
#
# Environment:
#   PYTHON        interpreter to build with (overrides an existing .venv/)
#   APPIMAGETOOL  path to appimagetool (a native binary or an AppImage)
#   OUTPUT_DIR    where the AppImage is written (default: dist/)
#   NADARE_RELEASE=1     mark a release build: requires the pin + digest below
#   APPIMAGETOOL_RELEASE appimagetool release tag (fixed tag, never `continuous`)
#   APPIMAGETOOL_SHA256  published sha256 of that tag's appimagetool (required)
#   APPIMAGETOOL_ALLOW_UNVERIFIED=1  local builds only: accept an unverified tool
#   APPIMAGE_RUNTIME_RELEASE  type2-runtime release tag whose `runtime-x86_64` is
#                        embedded in the AppImage (fixed tag, never `continuous`)
#   APPIMAGE_RUNTIME_SHA256  published sha256 of that runtime (required for a
#                        release build); APPIMAGE_RUNTIME_FILE uses a local copy
set -euo pipefail

root="$(cd "$(dirname "${BASH_SOURCE[0]}")/.." && pwd)"
cd "$root"

# An explicit PYTHON wins over a source checkout's .venv, which may have been
# created by a different host/libc than the one we must build on.
if [[ -n "${PYTHON:-}" ]]; then
    PY="$PYTHON"
elif [[ -x .venv/bin/python ]]; then
    PY=".venv/bin/python"
else
    PY="python3"
fi

# --- resolve and verify appimagetool (fail before the long build) -----------
#: Release tag and SHA-256 of the appimagetool build pinned for releases.  Both
#: are intentionally empty in the source tree: a release build (NADARE_RELEASE=1)
#: MUST set APPIMAGETOOL_RELEASE and APPIMAGETOOL_SHA256 so the tool is fetched
#: from a fixed tag and verified against a published digest, and cannot be
#: swapped for a compromised build.  `continuous` is deliberately not a default:
#: it is a moving tag.  Any build with no digest fails closed unless the caller
#: explicitly opts in with APPIMAGETOOL_ALLOW_UNVERIFIED=1 (local builds only).
APPIMAGETOOL_RELEASE="${APPIMAGETOOL_RELEASE:-}"
APPIMAGETOOL_SHA256="${APPIMAGETOOL_SHA256:-}"
APPIMAGETOOL_ALLOW_UNVERIFIED="${APPIMAGETOOL_ALLOW_UNVERIFIED:-}"
NADARE_RELEASE="${NADARE_RELEASE:-}"

verify_appimagetool() { # verify_appimagetool <path>
    local tool="$1" actual
    if [[ -n "$NADARE_RELEASE" && ( -z "$APPIMAGETOOL_RELEASE" || -z "$APPIMAGETOOL_SHA256" ) ]]; then
        echo "[!] release build: APPIMAGETOOL_RELEASE and APPIMAGETOOL_SHA256 are both required" >&2
        echo "    so the appimagetool is fetched from a fixed tag and verified." >&2
        return 1
    fi
    if [[ -z "$APPIMAGETOOL_SHA256" ]]; then
        if [[ "$APPIMAGETOOL_ALLOW_UNVERIFIED" != "1" ]]; then
            echo "[!] APPIMAGETOOL_SHA256 is not set: refusing to use an unverified appimagetool." >&2
            echo "    Set it to the published sha256 of the pinned tag, or, for a local" >&2
            echo "    build only, set APPIMAGETOOL_ALLOW_UNVERIFIED=1." >&2
            return 1
        fi
        echo "[!] APPIMAGETOOL_SHA256 is not set: using an unverified appimagetool" >&2
        echo "    (APPIMAGETOOL_ALLOW_UNVERIFIED=1 set by the caller)." >&2
        return 0
    fi
    if ! command -v sha256sum >/dev/null 2>&1; then
        echo "[!] sha256sum is required to verify appimagetool" >&2
        return 1
    fi
    actual="$(sha256sum "$tool" | cut -d' ' -f1)"
    if [[ "$actual" != "$APPIMAGETOOL_SHA256" ]]; then
        echo "[!] appimagetool integrity check FAILED" >&2
        echo "    expected $APPIMAGETOOL_SHA256" >&2
        echo "    actual   $actual" >&2
        return 1
    fi
    echo "[*] appimagetool verified (sha256 $actual)"
    return 0
}

appimagetool="${APPIMAGETOOL:-}"
if [[ -z "$appimagetool" ]]; then
    if command -v appimagetool >/dev/null 2>&1; then
        appimagetool="$(command -v appimagetool)"
    else
        for candidate in \
            "$HOME/appimagetool-x86_64.AppImage" \
            "$root/tools/appimagetool-x86_64.AppImage"; do
            if [[ -x "$candidate" ]]; then
                appimagetool="$candidate"
                break
            fi
        done
    fi
fi
if [[ -z "$appimagetool" ]]; then
    cat >&2 <<EOF
[!] appimagetool not found.

    Download a pinned static build (no FUSE needed to run it); substitute the
    fixed release tag and its published sha256:

        APPIMAGETOOL_RELEASE=<pinned-tag>
        curl -fL -o ~/appimagetool-x86_64.AppImage \\
            "https://github.com/AppImage/appimagetool/releases/download/\${APPIMAGETOOL_RELEASE}/appimagetool-x86_64.AppImage"
        chmod +x ~/appimagetool-x86_64.AppImage

    Then build with the pinned tag and digest so the tool is verified:

        APPIMAGETOOL_RELEASE=<pinned-tag> APPIMAGETOOL_SHA256=<published-sha256> scripts/build_appimage.sh
EOF
    exit 1
fi
verify_appimagetool "$appimagetool"

# The AppImage runtime stub is the most privileged part of the artifact (it runs
# before AppRun). appimagetool embeds its own copy unless told otherwise, so a
# release build fetches a fixed runtime tag, verifies its digest, and passes it
# explicitly with --runtime-file.
APPIMAGE_RUNTIME_RELEASE="${APPIMAGE_RUNTIME_RELEASE:-}"
APPIMAGE_RUNTIME_SHA256="${APPIMAGE_RUNTIME_SHA256:-}"
APPIMAGE_RUNTIME_FILE="${APPIMAGE_RUNTIME_FILE:-}"
runtime_args=()
if [[ -n "$NADARE_RELEASE" || -n "$APPIMAGE_RUNTIME_RELEASE" || -n "$APPIMAGE_RUNTIME_FILE" ]]; then
    if [[ -z "$APPIMAGE_RUNTIME_SHA256" ]]; then
        echo "[!] APPIMAGE_RUNTIME_SHA256 is not set: a pinned runtime needs its published sha256." >&2
        echo "    Set APPIMAGE_RUNTIME_RELEASE=<tag> (or APPIMAGE_RUNTIME_FILE) and the digest." >&2
        exit 1
    fi
    runtime="$APPIMAGE_RUNTIME_FILE"
    if [[ -z "$runtime" ]]; then
        if [[ -z "$APPIMAGE_RUNTIME_RELEASE" ]]; then
            echo "[!] set APPIMAGE_RUNTIME_RELEASE (a fixed tag) or APPIMAGE_RUNTIME_FILE." >&2
            exit 1
        fi
        mkdir -p build
        runtime="build/runtime-x86_64-${APPIMAGE_RUNTIME_RELEASE}"
        if [[ ! -s "$runtime" ]]; then
            echo "[*] fetching AppImage runtime ${APPIMAGE_RUNTIME_RELEASE}"
            "$PY" -c 'import sys, urllib.request; urllib.request.urlretrieve(sys.argv[1], sys.argv[2])' \
                "https://github.com/AppImage/type2-runtime/releases/download/${APPIMAGE_RUNTIME_RELEASE}/runtime-x86_64" \
                "$runtime" || { echo "[!] could not download the runtime" >&2; exit 1; }
        fi
    fi
    runtime_actual="$(sha256sum "$runtime" 2>/dev/null | cut -d' ' -f1)"
    if [[ "$runtime_actual" != "$APPIMAGE_RUNTIME_SHA256" ]]; then
        echo "[!] runtime integrity check FAILED" >&2
        echo "    expected $APPIMAGE_RUNTIME_SHA256" >&2
        echo "    actual   $runtime_actual" >&2
        exit 1
    fi
    echo "[*] AppImage runtime verified (sha256 $runtime_actual)"
    runtime_args=(--runtime-file "$runtime")
fi

# The bundle must carry the OS-keyring backends, or "Remember me" is silently
# unavailable for every user of the AppImage/Flatpak (found on a real KWallet box).
if ! "$PY" -c 'import keyring, secretstorage' 2>/dev/null; then
    if [[ "${NADARE_ALLOW_NO_KEYRING:-0}" != "1" ]]; then
        echo "[!] keyring/SecretStorage are not installed for ${PY}." >&2
        echo "    pip install -r requirements.txt   (needs keyring + SecretStorage)" >&2
        echo "    (NADARE_ALLOW_NO_KEYRING=1 builds without them; Remember me will not work)" >&2
        exit 1
    fi
fi

version="$("$PY" -c 'import nadare; print(nadare.__version__)' 2>/dev/null || true)"
if [[ ! "$version" =~ ^[0-9]+\.[0-9]+(\.[0-9]+)?([A-Za-z0-9.+-]*)$ ]]; then
    # A silent fallback would label the artifact with a version it is not.
    echo "[!] cannot read nadare.__version__ with ${PY} (got '${version}'): refusing to build." >&2
    exit 1
fi
output_dir="${OUTPUT_DIR:-dist}"
mkdir -p "$output_dir"
image="$(cd "$output_dir" && pwd)/Nadare-${version}-x86_64.AppImage"

echo "[*] Nadare ${version}: building PyInstaller onedir bundle (${PY})"
bash scripts/fetch_7zip.sh
"$PY" -m PyInstaller --clean -y packaging/nadare.spec

# --- stage the AppDir -------------------------------------------------------
echo "[*] Staging AppDir"
rm -rf AppDir
mkdir -p AppDir/usr/bin \
    AppDir/usr/share/applications \
    AppDir/usr/share/icons/hicolor/scalable/apps
cp -a dist/nadare/. AppDir/usr/bin/

# PyInstaller deliberately leaves the OpenGL stack to the host so a bundled
# libGL never shadows the GPU driver. Minimal distros (Fedora/Arch container
# images) ship none at all, and Qt6Gui hard-links libGL.so.1, so bundle the
# GLVND loader stack too. The vendor driver is still discovered at runtime via
# the host's GLVND vendor configuration, so this does not pin users to Mesa.
is_elf64() { # is_elf64 <file>: ELF magic and EI_CLASS == 2 (64-bit)
    [[ "$(head -c 4 "$1" 2>/dev/null | od -An -c | tr -d ' \n')" == '177ELF' ]] \
        && [[ "$(od -An -tx1 -j4 -N1 "$1" 2>/dev/null | tr -d ' ')" == "02" ]]
}
copy_system_libs() {
    local dest="AppDir/usr/bin/_internal"
    local lib src
    for lib in "$@"; do
        # Only entries tagged x86-64: a multiarch build host also lists the
        # i386 copy, and a 32-bit loader in the x86_64 AppDir fails at load.
        # awk must read to EOF: exiting at the first match SIGPIPEs ldconfig, which
        # under `set -o pipefail` aborts the whole build (exit 141).
        src="$(ldconfig -p 2>/dev/null | awk -v l="$lib" '$1 == l && /x86-64/ && !done { print $NF; done = 1 }')"
        if [[ -z "$src" ]]; then
            src="$(find /usr/lib64 /usr/lib/x86_64-linux-gnu /usr/lib /lib64 /lib -name "$lib" 2>/dev/null \
                | while read -r cand; do is_elf64 "$cand" && echo "$cand" && break; done | head -1)"
        fi
        if [[ -n "$src" && -e "$src" ]] && is_elf64 "$src"; then
            cp -L "$src" "$dest/"
            echo "    + $lib"
        else
            echo "[!] 64-bit system library not found: $lib" >&2
            return 1
        fi
    done
}
copy_system_libs libGL.so.1 libEGL.so.1 libGLX.so.0 libOpenGL.so.0 libGLdispatch.so.0

# Qt's xcb platform plugin links these; a minimal X11 desktop has none of them. Bundle them
# explicitly (and fail the build when the host lacks one) instead of trusting the binary
# analysis to find them. scripts/test_appimage.sh checks the same list.
copy_system_libs libxkbcommon-x11.so.0 libxcb-cursor.so.0 libxcb-icccm.so.4 libxcb-util.so.1 \
    libxcb-image.so.0 libxcb-keysyms.so.1 libxcb-render-util.so.0 libxcb-xkb.so.1 \
    libxcb-shape.so.0 libxcb-sync.so.1 libxcb-xfixes.so.0 libxcb-randr.so.0 libxcb-shm.so.0 \
    libxcb-render.so.0

# Optional GTK3 platform theme plugin: it needs libgtk-3 we do not bundle and
# Qt falls back to its own theme cleanly. Drop it so the bundle has no dangling
# shared-library dependency.
rm -f AppDir/usr/bin/_internal/PyQt6/Qt6/plugins/platformthemes/libqgtk3.so

# GPL: redistributed binaries carry the license text.
mkdir -p AppDir/usr/share/licenses/nadare
cp LICENSE AppDir/usr/share/licenses/nadare/LICENSE

cp packaging/nadare.desktop AppDir/nadare.desktop
cp packaging/nadare.svg AppDir/nadare.svg
cp packaging/nadare.desktop AppDir/usr/share/applications/nadare.desktop
cp packaging/nadare.svg AppDir/usr/share/icons/hicolor/scalable/apps/nadare.svg
ln -sf usr/bin/nadare AppDir/AppRun

echo "[*] Building $image"
rm -f "$image"
# APPIMAGE_EXTRACT_AND_RUN lets an AppImage-packaged appimagetool run without
# FUSE (containers, minimal CI); it is ignored by a native appimagetool.
ARCH=x86_64 APPIMAGE_EXTRACT_AND_RUN=1 "$appimagetool" "${runtime_args[@]}" AppDir "$image"

echo "[+] Built: $image  ($(du -h "$image" | cut -f1))"
echo "    sha256 $(sha256sum "$image" | cut -d' ' -f1)"

# One machine-readable checksum file next to the artifact (sha256sum format,
# bare file names) so packagers and the release job have a single thing to fetch.
( cd "$(dirname "$image")" && sha256sum "$(basename "$image")" > SHA256SUMS )
echo "[+] Wrote $(dirname "$image")/SHA256SUMS"
