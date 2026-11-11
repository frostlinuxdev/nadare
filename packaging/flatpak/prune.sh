#!/usr/bin/env bash
# SPDX-License-Identifier: GPL-3.0-or-later
# Copyright (C) 2026 Nadare contributors
#
# Keep only the last KEEP releases in the Flatpak repository and in the bucket behind
# https://flatpak.nadare.app/, so storage stays small (R2 bills storage, not downloads).
#
#   RCLONE=~/.local/share/nadare-tools/r2 packaging/flatpak/prune.sh build/flatpak-repo 3 \
#       --gpg-sign=<fingerprint> [--gpg-homedir=<dir>]
#
# Order: prune and re-sign the local repository, upload its refs and summary (upload.sh), and
# only then delete from the bucket what the local repository no longer has. A client that read
# the old summary a moment ago may miss an object, but never one of the last KEEP releases.
set -euo pipefail

repo="${1:?usage: prune.sh <repo dir> <keep> [gpg args for build-update-repo]}"
keep="${2:?usage: prune.sh <repo dir> <keep> [gpg args for build-update-repo]}"
shift 2
rclone="${RCLONE:?set RCLONE to an rclone command with an r2: remote}"
dest="${NADARE_FLATPAK_BUCKET:-r2:nadare-flatpak}"
here="$(cd "$(dirname "${BASH_SOURCE[0]}")" && pwd)"
[[ "$keep" =~ ^[0-9]+$ && "$keep" -ge 2 ]] || { echo "[!] keep at least 2 releases" >&2; exit 1; }
[[ -f "$repo/summary" && -f "$repo/nadare.flatpakref" ]] || {
    echo "[!] $repo is not a built repository (run build_repo.sh)" >&2
    exit 1
}

flatpak build-update-repo "$@" --prune --prune-depth=$((keep - 1)) "$repo"
"$here/upload.sh" "$repo"
immutable="Cache-Control: public, max-age=31536000, immutable"
$rclone sync --header-upload "$immutable" "$repo/objects" "$dest/objects"
[[ -d "$repo/deltas" ]] && $rclone sync --header-upload "$immutable" "$repo/deltas" "$dest/deltas"
[[ -d "$repo/summaries" ]] && $rclone sync --header-upload "Cache-Control: no-cache, max-age=0" \
    "$repo/summaries" "$dest/summaries"
echo "[+] kept the last $keep releases in $repo and $dest"
