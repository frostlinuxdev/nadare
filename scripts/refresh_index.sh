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
# set NADARE_SKIP_CANARY=1 to disable it. The Kuro token canary
# (scripts/check_kuro_tokens.py) runs with it and reports a refused or malformed token
# (the one published in catalog/kuro-tokens.json, else the one built into Nadare), and
# scripts/check_installer_pins.py reports a stale NTE installer pin or Endfield installer host.
# With the catalog on, a source that fails three catalog runs in a row also turns it "down".
#
# The CZN latest-version probe needs curl_cffi importable by python3
# (`python3 -m pip install --user curl_cffi`); without it the run fails.
#
# Set NADARE_GITHUB_TOKEN (any GitHub token, no scopes needed) on the index nodes so the
# runtime canary is not rate limited; a rejected token is reported and ignored.
#
# Set NADARE_INDEX_CATALOG=1 to also publish catalog/ (latest game versions and cover-art pointers).
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

# The lock lives in the user's runtime dir, else in a private 0700 directory of our own: a
# shared /tmp path could be pre-created (or made unwritable) by another local user.
lock_dir="${XDG_RUNTIME_DIR:-}"
if [[ -z "$lock_dir" ]]; then
    lock_dir="${TMPDIR:-/tmp}/nadare-refresh-$(id -u)"
    [[ ! -L "$lock_dir" ]] || { echo "refresh_index: lock directory is a symlink: $lock_dir" >&2; exit 1; }
    mkdir -p -m 700 "$lock_dir" 2>/dev/null || true
    # Re-check after creating it: the path must still be a plain directory of ours.
    [[ ! -L "$lock_dir" && -d "$lock_dir" && -O "$lock_dir" ]] \
        || { echo "refresh_index: cannot use lock directory $lock_dir" >&2; exit 1; }
fi
lock="$lock_dir/nadare-refresh-index.lock"
[[ ! -L "$lock" ]] || { echo "refresh_index: lock path is a symlink: $lock" >&2; exit 1; }
exec 9>"$lock" || { echo "refresh_index: cannot open lock file $lock" >&2; exit 1; }
flock -n 9 || { echo "refresh_index: already running"; exit 0; }

# A Yostar env that is a symlink is refused up front (it used to skip Yostar silently and freeze
# those manifests); a wrong owner or mode is refused here too, before any network work.
yostar_env="${NADARE_YOSTAR_ENV:-$HOME/nadare-index-refresh/yostar.env}"
if [[ -L "$yostar_env" ]]; then
    echo "refresh_index: $yostar_env is a symlink; point NADARE_YOSTAR_ENV at the real file" >&2
    exit 1
fi
if [[ -f "$yostar_env" ]]; then
    # The file holds the salts and is sourced as shell: owner-only (400/600).
    perms="$(stat -c '%a %u' "$yostar_env")"
    if [[ ! "${perms% *}" =~ ^[46]00$ || "${perms#* }" != "$(id -u)" ]]; then
        echo "refresh_index: $yostar_env must be owned by you with mode 600" >&2
        exit 1
    fi
fi

# shellcheck source=index_git.sh
source "$root/scripts/index_git.sh"
index_pull "$index" || { echo "refresh_index: git pull failed" >&2; exit 1; }

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

# Kuro token canary: checks the tokens published in catalog/kuro-tokens.json, or the
# ones built into Nadare when nothing is published, so it needs no catalog switch.
# Exit 4 = problem, joined to the same "down" message; any other failure is logged
# and ignored like the runtime canary.
if [[ -z "${NADARE_SKIP_CANARY:-}" ]]; then
    kuro_out="$(python3 "$root/scripts/check_kuro_tokens.py" --index "$index" 2>&1)"
    kuro_rc=$?
    [[ -z "$kuro_out" ]] || echo "$kuro_out" >&2
    if [[ $kuro_rc -eq 4 ]]; then
        kuro_down="$(grep '^CANARY:' <<<"$kuro_out" | head -c 400)"
        canary_down="${canary_down:+$canary_down; }$kuro_down"
        canary_down="${canary_down:0:400}"
    elif [[ $kuro_rc -ne 0 ]]; then
        echo "refresh_index: kuro token canary failed (exit $kuro_rc), ignored" >&2
    fi
fi

# Installer pin canary: the pinned NTE installer and the Endfield installer host. Exit 4 =
# a pin went stale; handled like the canaries above and never blocks publishing.
if [[ -z "${NADARE_SKIP_CANARY:-}" ]]; then
    pins_out="$(python3 "$root/scripts/check_installer_pins.py" 2>&1)"
    pins_rc=$?
    [[ -z "$pins_out" ]] || echo "$pins_out" >&2
    if [[ $pins_rc -eq 4 ]]; then
        pins_down="$(grep '^CANARY:' <<<"$pins_out" | head -c 400)"
        canary_down="${canary_down:+$canary_down; }$pins_down"
        canary_down="${canary_down:0:400}"
    elif [[ $pins_rc -ne 0 ]]; then
        echo "refresh_index: installer pin canary failed (exit $pins_rc), ignored" >&2
    fi
fi

# Art index canary: the cover URLs published in catalog/art.json. Exit 4 = a game has no working
# cover left (clients then use the older art sources); handled like the canaries above and never
# blocks publishing.
if [[ -z "${NADARE_SKIP_CANARY:-}" ]]; then
    art_out="$(python3 "$root/scripts/check_art_index.py" --index "$index" 2>&1)"
    art_rc=$?
    [[ -z "$art_out" ]] || echo "$art_out" >&2
    if [[ $art_rc -eq 4 ]]; then
        art_down="$(grep '^CANARY:' <<<"$art_out" | head -c 400)"
        canary_down="${canary_down:+$canary_down; }$art_down"
        canary_down="${canary_down:0:400}"
    elif [[ $art_rc -ne 0 ]]; then
        echo "refresh_index: art index canary failed (exit $art_rc), ignored" >&2
    fi
fi

skip_yostar=--skip-yostar
if [[ -f "$yostar_env" ]]; then
    set -a
    # shellcheck disable=SC1090
    . "$yostar_env"
    set +a
    skip_yostar=
fi
# The game catalog (catalog/, latest versions + art pointers) is opt-in: set NADARE_INDEX_CATALOG=1
# (e.g. in /etc/nadare-index-refresh.env) to publish it. It runs at most every 6 hours.
catalog=
[[ "${NADARE_INDEX_CATALOG:-}" == 1 ]] && catalog=--catalog
publish_out="$(python3 "$root/scripts/publish_index.py" --index "$index" $skip_yostar $catalog 2>&1)"
rc=$?
[[ -z "$publish_out" ]] || echo "$publish_out"
# A catalog source that keeps failing prints CANARY: lines (see publish_catalog); they
# turn the heartbeat "down" like the other canaries.
catalog_down="$(grep '^CANARY:' <<<"$publish_out" | head -c 400)"
if [[ -n "$catalog_down" ]]; then
    canary_down="${canary_down:+$canary_down; }$catalog_down"
    canary_down="${canary_down:0:400}"
fi
if [[ $rc -eq 3 ]]; then
    echo "refresh_index: nothing to publish"
    heartbeat
    exit 0
fi
[[ $rc -eq 0 ]] || { echo "refresh_index: publish failed (exit $rc)" >&2; exit "$rc"; }

if "$root/scripts/stage_index_changes.sh" "$index" "${NADARE_INDEX_CATALOG:-0}"; then
    :
else
    stage_rc=$?
    if [[ $stage_rc -ne 3 ]]; then
        echo "refresh_index: staging/commit failed (exit $stage_rc)" >&2
        exit "$stage_rc"
    fi
    echo "refresh_index: nothing to publish"
    heartbeat
    exit 0
fi
index_push "$index" || { echo "refresh_index: git push failed" >&2; exit 1; }
echo "refresh_index: published"
heartbeat
