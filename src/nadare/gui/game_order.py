# SPDX-License-Identifier: GPL-3.0-or-later
# Copyright (C) 2026 Nadare contributors
"""The one display order for games, shared by the sidebar and the library grid.

Running games come first, then installed (or updatable) ones, then the rest, each
group alphabetical by title. Both views use :func:`game_sort_key`, so the two
orders cannot drift apart. The library grid counts a running game as installed: a card
must not jump to the front of the grid when its game is started from it.
"""

from __future__ import annotations

from ..core import gamefacts
from ..core.backends import Status
from ..core.gamefacts import InstallFacts, display_state, update_available


def is_installed(status: Status | None) -> bool:
    """True when ``status`` means the game is on disk / running / updatable."""
    if status is None or InstallFacts.from_status(status).offers_resume():
        return False
    state = display_state(status).strip().lower()
    if state in ("installed", "running") or status.running:  # running is on disk (#1207 b232)
        return True
    return update_available(status)


def game_sort_key(
    name: str, title: str, status: Status | None = None, *, running_first: bool = True
) -> tuple[int, str, str]:
    """Sort key for one game: running first (unless ``running_first`` is off), then
    installed, then the rest."""
    if running_first and status is not None and gamefacts.is_running(status):
        rank = 0
    elif is_installed(status):
        rank = 1
    else:
        rank = 2  # not installed yet, or no status reported yet
    return (rank, title.lower(), name)
