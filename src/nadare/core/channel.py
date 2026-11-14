# SPDX-License-Identifier: GPL-3.0-or-later
# Copyright (C) 2026 Nadare contributors
"""Which build this is: the normal (stable) one or an unstable one.

Research work (new stores, new games, fixes still being tried) ships first in an unstable
AppImage, built with ``NADARE_CHANNEL=unstable scripts/build_appimage.sh``. The build writes
``assets/CHANNEL``; a source checkout can set ``NADARE_CHANNEL`` instead. An unstable build keeps
apart from the stable one: its own folders (:func:`nadare.core.paths.app_name`), keyring
service, single-instance socket and title, and it never checks for or installs launcher
updates. Anything unknown or unreadable means stable, so a stable install never changes.

This module imports nothing from Nadare: ``paths`` depends on it.
"""

from __future__ import annotations

import os
from pathlib import Path

STABLE = "stable"
UNSTABLE = "unstable"
_KNOWN = frozenset({STABLE, UNSTABLE})
ENV = "NADARE_CHANNEL"

#: Written by ``scripts/build_appimage.sh`` for an unstable build; never committed.
MARKER = Path(__file__).resolve().parents[1] / "assets" / "CHANNEL"


def _marker_value() -> str:
    try:
        return MARKER.read_text(encoding="utf-8").strip()
    except (OSError, UnicodeDecodeError):
        return ""


def current() -> str:
    """``"stable"`` or ``"unstable"``: the environment first, then the build marker."""
    value = os.environ.get(ENV)
    if value is not None and value in _KNOWN:
        return value
    marker = _marker_value()
    return marker if marker in _KNOWN else STABLE


def is_unstable() -> bool:
    return current() == UNSTABLE


def suffix() -> str:
    """``"-unstable"`` for folder, socket and service names; ``""`` for the stable build."""
    return "-unstable" if is_unstable() else ""


def title(name: str) -> str:
    """A window title that says when this is the unstable build."""
    return f"{name} (unstable)" if is_unstable() else name


__all__ = ["ENV", "MARKER", "STABLE", "UNSTABLE", "current", "is_unstable", "suffix", "title"]
