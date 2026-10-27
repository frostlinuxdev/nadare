#!/usr/bin/env bash
# SPDX-License-Identifier: GPL-3.0-or-later
# Copyright (C) 2026 Nadare contributors
#
# Git helpers for refresh_index.sh (sourced). A generated commit whose push failed stays
# local; a plain `pull --ff-only` would then fail on every later run. Rebasing keeps it.

# Bring the checkout up to date; keep unpushed generated commits on top.
index_pull() {
    local index="$1"
    git -C "$index" pull --rebase --autostash --quiet && return 0
    git -C "$index" rebase --abort >/dev/null 2>&1
    return 1
}

# Push HEAD; if the remote moved meanwhile, rebase once and try again.
index_push() {
    local index="$1"
    git -C "$index" push --quiet origin HEAD && return 0
    index_pull "$index" || return 1
    git -C "$index" push --quiet origin HEAD
}
