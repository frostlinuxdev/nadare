# SPDX-License-Identifier: GPL-3.0-or-later
# Copyright (C) 2026 Nadare contributors
"""The one way into Play, for the GUI and ``nadare launch`` alike (#1451).

Every check that refuses Play before the game starts runs here, in order, and then the
backend's own ``launch``. The checks used to be called by each caller; a new one added to one
caller only would have let the other Play past it. ``tests/test_launchgate_1451.py`` fails when
anything outside the backends starts a game without this module.
"""

from __future__ import annotations

from typing import Any

from . import execmount, flatpakgl, holds, nvidiarestart
from .backends import BackendError

#: ``nadare launch`` exit code when a maintainer's hold refuses the launch (#805).
HOLD_EXIT = 13


class HoldBlocked(BackendError):
    """A signed hold list says this game must not start."""

    def __init__(self, hold: holds.Hold) -> None:
        self.hold = hold
        lines = [f"This game is blocked by Nadare: {hold.reason}"]
        if hold.link:
            lines.append(hold.link)
        lines.append("Start it anyway from Nadare, or use --ignore-hold.")
        super().__init__("\n".join(lines), HOLD_EXIT)


def check(backend: Any, *, ignore_hold: bool = False) -> None:
    """Raise :class:`~nadare.core.backends.BackendError` when Play must not start."""
    # The Flatpak's graphics package for this driver (a game would show a black window).
    flatpakgl.guard_launch(backend)
    # An NVIDIA driver updated without a restart (the game would crash at start).
    nvidiarestart.guard_launch(backend)
    # A drive mounted noexec (the game would stop at once with "exit code 5"; #1861).
    execmount.guard_launch(backend)
    # A maintainer's signed hold list (#805). Test doubles without a context skip it.
    context = getattr(backend, "ctx", None)
    if not ignore_hold and context is not None:
        hold = holds.blocking_hold(
            context.config, context.name, str(getattr(backend, "kind", "") or "")
        )
        if hold is not None:
            raise HoldBlocked(hold)


def play(backend: Any, *, ignore_hold: bool = False) -> int:
    """Run every pre-Play check, then the game; returns the game's exit code."""
    check(backend, ignore_hold=ignore_hold)
    return int(backend.launch() or 0)


__all__ = ["HOLD_EXIT", "HoldBlocked", "check", "play"]
