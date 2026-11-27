# SPDX-License-Identifier: GPL-3.0-or-later
# Copyright (C) 2026 Nadare contributors
"""Small, best-effort memory of startup failures across games (#1733).

Only launch-reported runner names/versions are used. Games are profile ids, never paths.
Failure evidence expires after a day; a notice stays acknowledged until success or a
runner change, subject to the bounded runner history.
"""

from __future__ import annotations

import json
import math
import time
from typing import Literal

from . import atomicfile, paths

_WINDOW = 86400
_MAX_RUNNERS = 32
_MAX_GAMES = 128
_MAX_BYTES = 1024 * 1024


def _load(path, now: float) -> dict:
    try:
        with path.open("rb") as handle:
            raw = handle.read(_MAX_BYTES + 1)
        if len(raw) > _MAX_BYTES:
            return {}
        state = json.loads(raw)
        if not isinstance(state, dict):
            return {}
        clean = {}
        for runner, entry in state.items():
            if not isinstance(entry, dict) or not isinstance(entry.get("games"), dict):
                continue
            updated = entry.get("updated")
            if not isinstance(updated, (int, float)) or not math.isfinite(updated):
                continue
            games = {
                game: stamp
                for game, stamp in entry["games"].items()
                if isinstance(stamp, (int, float)) and now - _WINDOW < stamp <= now
            }
            games = dict(sorted(games.items(), key=lambda item: item[1])[-_MAX_GAMES:])
            noticed = entry.get("noticed") is True
            if games or noticed:
                profiles = entry.get("profiles", list(games))
                if not isinstance(profiles, list) or not all(isinstance(p, str) for p in profiles):
                    profiles = list(games)
                clean[runner] = {
                    "games": games,
                    "noticed": noticed,
                    "updated": updated,
                    "profiles": profiles[-_MAX_GAMES:],
                }
        return clean
    except (OSError, ValueError, UnicodeError):
        return {}


def record_result(
    game: str,
    runner: str,
    outcome: Literal["failure", "success", "neutral"],
    *,
    now: float | None = None,
) -> bool:
    """Return True once when two distinct games fail on this runner within 24 hours.

    A successful session clears the runner. Seeing a game use another runner clears
    its previous runner's history, allowing a fresh notice if the player returns to it.
    Stops and late crashes are neutral: they are not evidence of a startup failure.
    """
    if not game or not runner:
        return False
    now = time.time() if now is None else now
    path = paths.data_dir() / "runner-failures.json"
    state = _load(path, now)
    for previous in list(state):
        if previous != runner and game in state[previous]["profiles"]:
            del state[previous]
    notice = False
    if outcome == "success":
        state.pop(runner, None)
    elif outcome == "failure":
        entry = state.setdefault(
            runner, {"games": {}, "noticed": False, "updated": now, "profiles": []}
        )
        entry["profiles"] = [p for p in entry["profiles"] if p != game][-(_MAX_GAMES - 1) :]
        entry["profiles"].append(game)
        entry["games"][game] = now
        entry["games"] = dict(
            sorted(entry["games"].items(), key=lambda item: item[1])[-_MAX_GAMES:]
        )
        entry["updated"] = now
        notice = len(entry["games"]) >= 2 and not entry["noticed"]
        entry["noticed"] = entry["noticed"] or notice
    state = dict(sorted(state.items(), key=lambda item: item[1]["updated"])[-_MAX_RUNNERS:])
    try:
        atomicfile.write(path, json.dumps(state), make_parent=True)
    except OSError:
        pass  # A full/read-only disk must never turn a game exit into an application error.
    return notice
