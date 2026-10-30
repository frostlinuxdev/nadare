#!/usr/bin/env bash
# SPDX-License-Identifier: GPL-3.0-or-later
# Copyright (C) 2026 Nadare contributors
#
# Publish the block-pin documents (catalog/archive-pins/<game>.json) from an index node.
# A wrapper around scripts/publish_pins.py: pull the index, publish, commit only the pin
# directory, push, write a status file and ping a heartbeat. Safe to run often: a pin is
# only rebuilt (a full streaming pass over the archive) when the publisher's build changed.
#
# Use a checkout of its own, not the one refresh_index.sh uses: that job stages all of
# catalog/ and would commit the pins under its own message.
#
#   NADARE_PIN_INDEX_DIR=~/nadare-pins/nadare-index scripts/publish_pins_node.sh
#
# Settings (environment, or an EnvironmentFile for the systemd unit):
#   NADARE_PIN_INDEX_DIR      git checkout of the index (required)
#   NADARE_PIN_GAMES          space-separated game tags (default: aethergazer mongil)
#   NADARE_PIN_DEADLINE       seconds for the whole run (default: publish_pins.py's own)
#   NADARE_PIN_PUSH           0 = commit but do not push
#   NADARE_PIN_STATUS         status file (default ~/nadare-pins/status.json)
#   NADARE_PIN_HEARTBEAT_URL  Uptime Kuma push URL, pinged after every run: "down" with the
#                             reason when the run failed. The URL is a secret: it is never
#                             printed or written to the status file.
#   NADARE_PIN_PUBLISHER      publisher command (default: python3 scripts/publish_pins.py)
#
# Exit status: 0 published, committed or nothing to do; 1 failure.
set -uo pipefail

root="$(cd "$(dirname "${BASH_SOURCE[0]}")/.." && pwd)"
index="${NADARE_PIN_INDEX_DIR:-}"
games="${NADARE_PIN_GAMES:-aethergazer mongil}"
status_file="${NADARE_PIN_STATUS:-$HOME/nadare-pins/status.json}"
push="${NADARE_PIN_PUSH:-1}"
heartbeat_url="${NADARE_PIN_HEARTBEAT_URL:-}"

[[ -n "$index" && -d "$index/.git" ]] || {
    echo "publish_pins_node: no git checkout at '${index}' (set NADARE_PIN_INDEX_DIR)" >&2
    exit 1
}

lock="${XDG_RUNTIME_DIR:-/tmp}/nadare-publish-pins.lock"
[[ ! -L "$lock" ]] || { echo "publish_pins_node: lock path is a symlink: $lock" >&2; exit 1; }
exec 9>"$lock"
flock -n 9 || { echo "publish_pins_node: already running"; exit 0; }

# shellcheck source=index_git.sh
source "$root/scripts/index_git.sh"

heartbeat() { # heartbeat ok|down MESSAGE
    [[ -n "$heartbeat_url" ]] || return 0
    if [[ "$1" == ok ]]; then
        curl -fsS -m 10 --retry 2 -o /dev/null "$heartbeat_url" >/dev/null 2>&1 \
            || echo "publish_pins_node: heartbeat ping failed" >&2
    else
        curl -fsS -m 10 --retry 2 -o /dev/null -G \
            --data-urlencode "status=down" --data-urlencode "msg=${2:0:400}" \
            "${heartbeat_url%%\?*}" >/dev/null 2>&1 \
            || echo "publish_pins_node: heartbeat ping failed" >&2
    fi
}

write_status() { # write_status ok|fail RESULT MESSAGE
    mkdir -p "$(dirname "$status_file")" 2>/dev/null || true
    STATUS_OK="$1" STATUS_RESULT="$2" STATUS_MESSAGE="$3" STATUS_GAMES="$games" \
        STATUS_COMMIT="$(git -C "$index" rev-parse HEAD 2>/dev/null || true)" \
        STATUS_HOST="$(hostname)" STATUS_FILE="$status_file" python3 - <<'PY' \
        || echo "publish_pins_node: could not write $status_file" >&2
import json, os, tempfile, time

doc = {
    "ok": os.environ["STATUS_OK"] == "ok",
    "result": os.environ["STATUS_RESULT"],
    "message": os.environ["STATUS_MESSAGE"][:400],
    "games": os.environ["STATUS_GAMES"].split(),
    "commit": os.environ["STATUS_COMMIT"],
    "host": os.environ["STATUS_HOST"],
    "time": int(time.time()),
}
path = os.environ["STATUS_FILE"]
fd, tmp = tempfile.mkstemp(prefix=".status-", dir=os.path.dirname(path))
with os.fdopen(fd, "w") as handle:
    json.dump(doc, handle, sort_keys=True)
    handle.write("\n")
os.replace(tmp, path)
PY
}

fail() { # fail MESSAGE
    echo "publish_pins_node: $1" >&2
    write_status fail failed "$1"
    heartbeat down "$1"
    exit 1
}

index_pull "$index" || fail "git pull failed"

game_args=()
for game in $games; do game_args+=(--game "$game"); done
deadline_args=()
[[ -z "${NADARE_PIN_DEADLINE:-}" ]] || deadline_args=(--deadline "$NADARE_PIN_DEADLINE")

if [[ -n "${NADARE_PIN_PUBLISHER:-}" ]]; then
    publisher=("$NADARE_PIN_PUBLISHER")
else
    publisher=(python3 "$root/scripts/publish_pins.py")
fi
publish_out="$("${publisher[@]}" --index "$index" "${game_args[@]}" "${deadline_args[@]}" 2>&1)"
rc=$?
[[ -z "$publish_out" ]] || echo "$publish_out"

# publish_pins writes each game's file atomically, so whatever exists is complete: commit it
# even when another game failed, then report the failure below.
git -C "$index" add -- catalog/archive-pins 2>/dev/null
result=unchanged
if ! git -C "$index" diff --cached --quiet -- catalog/archive-pins; then
    git -C "$index" commit -q -m "catalog: refresh archive pins" --only -- catalog/archive-pins \
        || fail "git commit failed"
    result=committed
    if [[ "$push" != 0 ]]; then
        index_push "$index" || fail "git push failed"
        result=published
    fi
fi

if [[ $rc -ne 0 && $rc -ne 3 ]]; then
    msg="$(grep -E 'FAILED' <<<"$publish_out" | head -c 300)"
    echo "publish_pins_node: publisher exit $rc" >&2
    write_status fail failed "${msg:-publisher exit $rc}"
    heartbeat down "${msg:-publisher exit $rc}"
    exit 1
fi

echo "publish_pins_node: $result"
write_status ok "$result" ""
heartbeat ok ""
exit 0
