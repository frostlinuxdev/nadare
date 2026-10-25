#!/usr/bin/env bash
# SPDX-License-Identifier: GPL-3.0-or-later
# Stage and commit only index paths owned by the refresh job.
set -euo pipefail

if [[ $# -ne 2 ]]; then
    echo "usage: $0 INDEX_DIR CATALOG_ENABLED(0|1)" >&2
    exit 2
fi

index=$1
catalog_enabled=$2
[[ -d "$index/.git" ]] || { echo "stage_index_changes: no git checkout at $index" >&2; exit 1; }
[[ "$catalog_enabled" == 0 || "$catalog_enabled" == 1 ]] || {
    echo "stage_index_changes: catalog flag must be 0 or 1" >&2
    exit 2
}

allowed=()
for path in webview2 yostar vcredist czn; do
    if [[ -e "$index/$path" ]]; then
        git -C "$index" add -- "$path"
        allowed+=("$path")
    fi
done
if [[ "$catalog_enabled" == 1 && -e "$index/catalog" ]]; then
    git -C "$index" add -- catalog
    allowed+=(catalog)
fi

if (( ${#allowed[@]} == 0 )) || git -C "$index" diff --cached --quiet -- "${allowed[@]}"; then
    exit 3
fi

# --only prevents unrelated changes that were already staged in this checkout
# from sneaking into the refresh commit.
git -C "$index" commit -q -m "webview2: refresh installer hash list" --only -- "${allowed[@]}"
