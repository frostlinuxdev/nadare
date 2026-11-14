# SPDX-License-Identifier: GPL-3.0-or-later
# Copyright (C) 2026 Nadare contributors
"""Helpers and constants shared by the parts of nadare.gui.settings.

Split out of :mod:`nadare.gui.settings`; the code is unchanged.
"""

from __future__ import annotations

import re
from typing import Any

from PyQt6.QtWidgets import (
    QComboBox,
)

from ...core import monitors

#: Options every game page lists but only people who know what they do should touch:
#: shown under "Show advanced options", whatever the backend says.
GAME_SUBTITLE = "How this game is installed, launched and displayed."

_ENV_NAME = re.compile(r"[A-Za-z_][A-Za-z0-9_]*")


def _fill_monitor_combo(combo: QComboBox, saved: Any = None) -> None:
    """Fill ``combo`` with Auto and the connected monitors.

    A saved name that is not connected here (a config copied from another PC) stays
    in the list, marked, so saving does not silently drop the choice.
    """
    name = str(saved or "").strip()
    combo.clear()
    combo.addItem("Auto", "")
    listed = set()
    for monitor in monitors.connected():
        combo.addItem(monitor.label, monitor.name)
        listed.add(monitor.name)
    if name and name != "auto" and name not in listed:
        combo.addItem(f"{name} (not connected)", name)


def _text_to_env(text: str, warnings: list[str] | None = None) -> dict[str, str]:
    """Parse ``KEY=VALUE`` lines into a mapping (blank lines ignored).

    A non-empty line without an ``=`` is reported through ``warnings`` instead
    of disappearing silently; the valid lines are still returned.
    """
    out: dict[str, str] = {}
    for line in text.splitlines():
        line = line.strip()
        if not line or line.startswith("#"):
            continue
        key, sep, value = line.partition("=")
        key = key.strip()
        if sep and _ENV_NAME.fullmatch(key):
            out[key] = value.strip()
        elif sep and key and warnings is not None:
            warnings.append(
                f"Environment: '{key}' is not a valid variable name (letters, digits and "
                "underscores, not starting with a digit), so the line was not saved."
            )
        elif warnings is not None:
            warnings.append(
                f"Environment: '{line}' is not KEY=VALUE, so it was not saved. "
                "Add an = sign or remove the line."
            )
    return out
