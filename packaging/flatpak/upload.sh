#!/usr/bin/env bash
# SPDX-License-Identifier: GPL-3.0-or-later
# Copyright (C) 2026 Nadare contributors
#
# Upload a repository built by build_repo.sh to the bucket behind https://flatpak.nadare.app/.
#
#   RCLONE=~/.local/share/nadare-tools/r2 packaging/flatpak/upload.sh build/flatpak-repo
#
# RCLONE is an rclone command that knows the remote "r2:" (credentials stay out of this script).
# Order matters: objects first, then refs, then the summary and the .flatpakrepo/.flatpakref
# files last, so a client that checks for updates mid-upload never sees a summary pointing at
# objects that are not there yet. Nothing is deleted: someone halfway through an update keeps
# working; pruning old commits is a separate, deliberate step (prune.sh).
set -euo pipefail

repo="${1:?usage: upload.sh <repo dir>}"
rclone="${RCLONE:?set RCLONE to an rclone command with an r2: remote}"
dest="${NADARE_FLATPAK_BUCKET:-r2:nadare-flatpak}"
[[ -f "$repo/summary" && -f "$repo/nadare.flatpakref" ]] || {
    echo "[!] $repo is not a built repository (run build_repo.sh)" >&2
    exit 1
}
immutable="Cache-Control: public, max-age=31536000, immutable"
fresh="Cache-Control: no-cache, max-age=0"

# Content-addressed objects and static deltas never change once written.
$rclone copy --header-upload "$immutable" "$repo/objects" "$dest/objects"
[[ -d "$repo/deltas" ]] && $rclone copy --header-upload "$immutable" "$repo/deltas" "$dest/deltas"
# Small files that move with every release.
$rclone copy --header-upload "$fresh" "$repo/refs" "$dest/refs"
[[ -d "$repo/extensions" ]] && $rclone copy --header-upload "$fresh" "$repo/extensions" "$dest/extensions"
$rclone copyto --header-upload "$fresh" "$repo/config" "$dest/config"
# Last: what clients read first.
for name in summary summary.sig summary.idx summary.idx.sig; do
    [[ -f "$repo/$name" ]] && $rclone copyto --header-upload "$fresh" "$repo/$name" "$dest/$name"
done
[[ -d "$repo/summaries" ]] && $rclone copy --header-upload "$fresh" "$repo/summaries" "$dest/summaries"
for name in index.flatpakrepo nadare.flatpakref; do
    $rclone copyto --header-upload "$fresh" "$repo/$name" "$dest/$name"
done
echo "[+] uploaded $repo to $dest"
