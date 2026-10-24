#!/usr/bin/env bash
# SPDX-License-Identifier: GPL-3.0-or-later
# Copyright (C) 2026 Nadare contributors
#
# Refresh the WebView2 installer hash list in a nadare-index checkout and push
# it if Microsoft published a new build. Safe to run as often as you like: it
# only downloads when Microsoft's size/Last-Modified changed, keeps the last
# good file on any failure, and never overlaps itself.
#
#   NADARE_INDEX_DIR=~/nadare-index scripts/refresh_index.sh
#
# Yostar manifests (Arknights/Stella Sora) need the per-game signing salts
# (SALT_<TAG>/VERSION_<TAG>, see ci/yostar-manifests/README.md). Put them in
# NADARE_YOSTAR_ENV (default ~/nadare-index-refresh/yostar.env, mode 600); when
# that file is absent the Yostar step is skipped, as before.
#
# The runtime canary (scripts/check_runtime_canary.py) runs first and turns the
# heartbeat "down" when a new runner needs a Steam Runtime the pinned umu lacks;
# set NADARE_SKIP_CANARY=1 to disable it.
#
# The CZN latest-version probe needs curl_cffi importable by python3
# (`python3 -m pip install --user curl_cffi`); without it the run fails.
#
# Set NADARE_HEARTBEAT_URL (e.g. an Uptime Kuma push monitor URL) to be pinged
# after every successful run, including "nothing to publish". A monitor that
# expects a ping every ~15 minutes then alerts when the whole pipeline stops.
set -uo pipefail

root="$(cd "$(dirname "${BASH_SOURCE[0]}")/.." && pwd)"
index="${NADARE_INDEX_DIR:-$root/../nadare-index}"
[[ -d "$index/.git" ]] || { echo "refresh_index: no git checkout at $index" >&2; exit 1; }

# Set by the runtime canary below when a runner needs a Steam Runtime the pinned
# umu does not know; the heartbeat then reports "down" with that message.
canary_down=

heartbeat() {
    [[ -n "${NADARE_HEARTBEAT_URL:-}" ]] || return 0
    if [[ -n "$canary_down" ]]; then
        curl -fsS -m 10 --retry 2 -o /dev/null -G \
            --data-urlencode "status=down" --data-urlencode "msg=$canary_down" \
            "${NADARE_HEARTBEAT_URL%%\?*}" \
            || echo "refresh_index: heartbeat ping failed" >&2
        return 0
    fi
    curl -fsS -m 10 --retry 2 -o /dev/null "$NADARE_HEARTBEAT_URL" \
        || echo "refresh_index: heartbeat ping failed" >&2
}

lock="${XDG_RUNTIME_DIR:-/tmp}/nadare-refresh-index.lock"
[[ ! -L "$lock" ]] || { echo "refresh_index: lock path is a symlink: $lock" >&2; exit 1; }
exec 9>"$lock"
flock -n 9 || { echo "refresh_index: already running"; exit 0; }

git -C "$index" pull --ff-only --quiet || { echo "refresh_index: git pull failed" >&2; exit 1; }

# Runtime canary: warn before a new Proton runner outruns the Steam Runtimes the
# pinned umu knows. Exit 4 = problem; any other failure is only logged, and a
# canary result never blocks publishing the index.
if [[ -z "${NADARE_SKIP_CANARY:-}" ]]; then
    canary_out="$(python3 "$root/scripts/check_runtime_canary.py" 2>&1)"
    canary_rc=$?
    [[ -z "$canary_out" ]] || echo "$canary_out" >&2
    if [[ $canary_rc -eq 4 ]]; then
        canary_down="$(grep '^CANARY:' <<<"$canary_out" | head -c 400)"
    elif [[ $canary_rc -ne 0 ]]; then
        echo "refresh_index: runtime canary failed (exit $canary_rc), ignored" >&2
    fi
fi

skip_yostar=--skip-yostar
yostar_env="${NADARE_YOSTAR_ENV:-$HOME/nadare-index-refresh/yostar.env}"
if [[ -f "$yostar_env" && ! -L "$yostar_env" ]]; then
    # The file holds the salts and is sourced as shell: owner-only (400/600).
    perms="$(stat -c '%a %u' "$yostar_env")"
    if [[ ! "${perms% *}" =~ ^[46]00$ || "${perms#* }" != "$(id -u)" ]]; then
        echo "refresh_index: $yostar_env must be owned by you with mode 600" >&2
        exit 1
    fi
    set -a
    # shellcheck disable=SC1090
    . "$yostar_env"
    set +a
    skip_yostar=
fi
python3 "$root/scripts/publish_index.py" --index "$index" $skip_yostar
rc=$?
if [[ $rc -eq 3 ]]; then
    echo "refresh_index: nothing to publish"
    heartbeat
    exit 0
fi
[[ $rc -eq 0 ]] || { echo "refresh_index: publish failed (exit $rc)" >&2; exit "$rc"; }

git -C "$index" add webview2
git -C "$index" add yostar
git -C "$index" add vcredist
git -C "$index" add czn
if git -C "$index" diff --cached --quiet; then
    echo "refresh_index: nothing to publish"
    heartbeat
    exit 0
fi
git -C "$index" commit -q -m "webview2: refresh installer hash list" \
    || { echo "refresh_index: commit failed" >&2; exit 1; }
git -C "$index" push --quiet origin HEAD || exit 1
echo "refresh_index: published"
heartbeat
