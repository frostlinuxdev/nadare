#!/usr/bin/env bash
# SPDX-License-Identifier: GPL-3.0-or-later
# Copyright (C) 2026 Nadare contributors
#
# Smoke-test a built Nadare AppImage inside a throwaway distrobox.
#
#   scripts/test_appimage.sh dist/Nadare-0.1.0-x86_64.AppImage nadare-test-2204
#   scripts/test_appimage.sh dist/Nadare-0.1.0-x86_64.AppImage nadare-test-mint22
#
# Create a box from the matching official image first, e.g.:
#   distrobox create --name nadare-test-mint22 \
#       --image docker.io/linuxmintd/mint22.1-amd64:latest
# (the same pattern works for ubuntu:22.04, debian:12, fedora:43,
# archlinux:latest). The Linux Mint 22.1 image is Ubuntu 24.04 based and ships
# a newer glibc than the AppImage's 2.35 build floor.
#
# The box must be a *test* distrobox with its own throwaway HOME (never your
# real one). The script copies the AppImage into that HOME, then runs:
#   * CLI:  --help, list
#   * GUI:  QT_QPA_PLATFORM=offscreen with NADARE_SMOKE_EXIT_MS auto-quit
#   * a scan of stdout/stderr for ImportError, missing shared objects and
#     Qt platform-plugin failures
#   * a check that XDG config/data landed under the throwaway HOME
#
# It prints PASS/FAIL per check and exits non-zero if anything failed.
#
# Containers have no FUSE, so the AppImage is run with
# `--appimage-extract-and-run`. Plain execution is attempted once for the
# record; inside a container it is expected to fail.
set -uo pipefail

if [[ $# -ne 2 ]]; then
    echo "usage: $0 <appimage> <distrobox-name>" >&2
    exit 2
fi

appimage="$(realpath "$1")"
box="$2"
[[ -f "$appimage" ]] || { echo "no such AppImage: $appimage" >&2; exit 2; }
chmod +x "$appimage" 2>/dev/null || true

# The host distrobox build has a hardened create path; keep it out of the way
# for enter/exec too. Harmless when unset on a stock distrobox.
export DBX_HARDENED_NO_PRIVILEGED=1

fails=0
passes=0
ck() { # ck <PASS|FAIL|INFO> <label> [detail]
    printf '%-4s  %s' "$1" "$2"
    [[ $# -ge 3 && -n "$3" ]] && printf '  (%s)' "$3"
    printf '\n'
    case "$1" in
        PASS) passes=$((passes + 1)) ;;
        FAIL) fails=$((fails + 1)) ;;
    esac
}

inx() { # run a command inside the box, throwaway HOME
    distrobox enter "$box" -- bash -lc "$1"
}

inx_argv() { # run a command inside the box with argv passed safely (no splicing)
    distrobox enter "$box" -- "$@"
}

echo "=== testing $appimage in distrobox '$box' ==="

# Warm up the box (first enter installs its basic packages); failures here are
# fatal and reported before any check.  A private log avoids collisions between
# parallel runs (they otherwise share the host /tmp).
warmup_log="$(mktemp "${TMPDIR:-/tmp}/nadare-test-warmup.XXXXXX.log")"
trap 'rm -f "$warmup_log"' EXIT
if ! inx 'echo ready' >"$warmup_log" 2>&1; then
    echo "could not enter box '$box':" >&2
    cat "$warmup_log" >&2
    exit 1
fi

boxinfo="$(inx 'printf "%s|%s|%s" "$HOME" "$(. /etc/os-release; echo "$PRETTY_NAME")" "$(ldd --version 2>/dev/null | head -1 | grep -o "[0-9]\+\.[0-9]\+$")"')"
IFS='|' read -r boxhome boxname boxglibc <<<"$boxinfo"
echo "HOME=$boxhome  distro=$boxname  glibc=$boxglibc"
echo

# --- copy in and make executable -------------------------------------------
# Pass the path as an argument ($1), never spliced into the shell string: a
# path containing a quote must not break out of the remote shell.
if inx_argv bash -c \
    'cp "$1" "$HOME/nadare-test.AppImage" && chmod +x "$HOME/nadare-test.AppImage"' \
    _ "$appimage"; then
    ck PASS "AppImage copied into throwaway HOME and executable"
else
    ck FAIL "AppImage copied into throwaway HOME and executable"
fi

# --- plain execution (FUSE); informational in containers --------------------
plain="$(inx 'timeout 20 "$HOME/nadare-test.AppImage" --help' 2>&1)"
if [[ $? -eq 0 ]]; then
    ck INFO "plain exec (FUSE)" "worked"
else
    reason="$(printf '%s' "$plain" | grep -m1 -iE "fuse|AppImage" || printf 'failed')"
    ck INFO "plain exec (FUSE)" "unavailable in container: ${reason:0:60}"
fi

# --- helper: run an AppImage command and check output + exit code -----------
run_check() { # run_check <label> <shell body producing output; exit status checked>
    local label="$1" body="$2"
    local out rc
    out="$(inx "export APPIMAGE_EXTRACT_AND_RUN=1; cd \"\$HOME\"; $body" 2>&1)"
    rc=$?
    if [[ $rc -ne 0 ]]; then
        ck FAIL "$label" "exit $rc"
        printf '%s\n' "$out" | sed 's/^/        /' | tail -20
        return 1
    fi
    if printf '%s' "$out" | grep -qE "ImportError|ModuleNotFoundError|cannot open shared object|Could not load the Qt platform plugin|error while loading shared libraries|Traceback"; then
        ck FAIL "$label" "error in output"
        printf '%s\n' "$out" | sed 's/^/        /' | tail -20
        return 1
    fi
    ck PASS "$label"
    return 0
}

run_check "CLI --help" '"./nadare-test.AppImage" --help >/dev/null'
run_check "CLI list" '"./nadare-test.AppImage" list >/dev/null'
# Backends are imported lazily; a module left out of the bundle only shows up
# as "unknown backend" when a profile uses it. doctor reports every one.
# `grep -v MISSING | grep -q .` takes the exit status only from the backends
# line: a missing backend on that line leaves nothing to match, so the check
# fails, and an absent backends line cannot pass vacuously.
# A build made with NADARE_WITHOUT_FGO=1 (the beta) is expected to lack fgo-lepton
# and nothing else; export the same variable when running this script for it.
without_fgo='cat'
if [ "${NADARE_WITHOUT_FGO:-}" = 1 ]; then
    without_fgo="sed -E 's/, MISSING: fgo-lepton\$//'"
fi
run_check "all backends bundled" '"./nadare-test.AppImage" doctor | grep -E "^backends: " | tee /dev/stderr | '"$without_fgo"' | grep -v MISSING | grep -q .'

# GUI headless with the packaging auto-quit hook.
gui_label="GUI headless (offscreen, auto-quit)"
gui_out="$(inx 'export APPIMAGE_EXTRACT_AND_RUN=1 QT_QPA_PLATFORM=offscreen NADARE_SMOKE_EXIT_MS=3000; cd "$HOME"; timeout 60 "./nadare-test.AppImage"' 2>&1)"
gui_rc=$?
if [[ $gui_rc -ne 0 ]]; then
    ck FAIL "$gui_label" "exit $gui_rc"
    printf '%s\n' "$gui_out" | sed 's/^/        /' | tail -20
elif printf '%s' "$gui_out" | grep -qE "ImportError|ModuleNotFoundError|cannot open shared object|Could not load the Qt platform plugin|error while loading shared libraries|Traceback"; then
    ck FAIL "$gui_label" "error in output"
    printf '%s\n' "$gui_out" | sed 's/^/        /' | tail -20
else
    ck PASS "$gui_label"
fi

# --- config under the throwaway HOME, nowhere else --------------------------
cfg_out="$(inx 'for d in "$HOME/.config/nadare" "$HOME/.local/share/nadare" "$HOME/.local/state/nadare" "$HOME/.cache/nadare"; do [ -d "$d" ] && echo "$d"; done')"
if printf '%s' "$cfg_out" | grep -q "^$boxhome/.config/nadare$"; then
    ck PASS "config/data under throwaway HOME" "$(printf '%s' "$cfg_out" | wc -l) dirs"
else
    ck FAIL "config/data under throwaway HOME" "no ~/.config/nadare created"
    printf '%s\n' "$cfg_out" | sed 's/^/        /'
fi
if printf '%s' "$cfg_out" | grep -qv "^$boxhome/"; then
    ck FAIL "no files outside throwaway HOME"
else
    ck PASS "no files outside throwaway HOME"
fi

echo
if [[ $fails -eq 0 ]]; then
    echo "RESULT: PASS ($passes checks) for $box"
    exit 0
else
    echo "RESULT: FAIL ($fails failed, $passes passed) for $box"
    exit 1
fi
