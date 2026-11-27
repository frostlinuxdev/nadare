# SPDX-License-Identifier: GPL-3.0-or-later
# Copyright (C) 2026 Nadare contributors
"""Add a game to the app menu or to Steam from its page, and take it out again (#1759).

The work is :mod:`nadare.core.shortcuts`, the same as ``nadare shortcut``; this module picks
the call for a menu choice and words the result for a person.
"""

from __future__ import annotations

import logging
import struct
from typing import Any

from PyQt6.QtWidgets import QMessageBox

from ...core import shortcuts
from .. import i18n

log = logging.getLogger(__name__)

ACTIONS = ("menu-add", "menu-remove", "steam-add", "steam-remove")

_CLOSE_STEAM = (
    "Close Steam first, then try again. Steam rewrites its list of games when it exits, "
    "so a change made while it runs would be lost."
)


def shortcut_state(profile: str) -> tuple[bool, bool]:
    """``(in the app menu, in Steam)`` for ``profile``; unreadable files count as absent."""
    try:
        in_menu = any(owner == profile for _, owner in shortcuts.installed_desktop_entries())
    except OSError:
        in_menu = False
    try:
        in_steam = any(owner == profile for _, owner, _ in shortcuts.installed_steam_shortcuts())
    except OSError:
        in_steam = False
    return in_menu, in_steam


def apply_shortcut(config: Any, profile: str, action: str) -> tuple[bool, str]:
    """Run one menu choice; ``(worked, what to tell the user)``. Never raises for a user error."""
    if action not in ACTIONS:
        raise ValueError(f"unknown shortcut action: {action}")
    data = config.profiles.get(profile)
    if data is None:
        return False, f"{profile} is no longer in the library."
    title = str(data.get("title") or profile)
    try:
        if action == "menu-add":
            shortcuts.install_desktop_entry(
                profile, title, argv=shortcuts.launcher_argv(), profiles=config.profiles
            )
            # Some launchers (Noctalia, rofi and others) read the app list only at start (#1876).
            return True, (
                f"{title} is in the app menu. Some launchers only show it after they restart "
                "or after you log in again."
            )
        if action == "menu-remove":
            removed = shortcuts.remove_desktop_entries(profile)
            if not removed:
                return True, f"{title} was not in the app menu."
            return True, f"{title} is no longer in the app menu."
        if shortcuts.steam_running():
            return False, _CLOSE_STEAM
        if action == "steam-add":
            files = shortcuts.find_steam_shortcut_files()
            if not files:
                return False, (
                    "Nadare found no Steam account on this computer. Start Steam and sign in "
                    "once, close it, then try again."
                )
            argv = shortcuts.launcher_argv()
            for path in files:
                shortcuts.add_steam_shortcut(path, profile, title, argv, profiles=config.profiles)
            return (
                True,
                f"{title} is in Steam. It shows in your library the next time Steam starts.",
            )
        removed, warnings = shortcuts.remove_steam_shortcuts(profile)
        if warnings:
            return False, "\n".join(warnings)
        if not removed:
            return True, f"{title} was not in Steam."
        return True, f"{title} is no longer in Steam."
    except shortcuts.ShortcutError as exc:
        text = str(exc)
        if text.startswith("Steam is running"):
            return False, _CLOSE_STEAM
        return False, text
    except (OSError, ValueError, struct.error) as exc:
        log.warning("shortcut %s for %s failed: %s", action, profile, exc)
        return False, f"That did not work: {exc}"


class ShortcutsMixin:
    """MainWindow: the game page's app menu / Steam entries."""

    def _on_shortcut_requested(self, profile: str, action: str) -> None:
        ok, text = apply_shortcut(self.config, profile, action)
        self._tell_shortcut(ok, text)

    def _tell_shortcut(self, ok: bool, text: str) -> None:
        if ok:
            QMessageBox.information(self, i18n.tr("Shortcuts"), text)
        else:
            QMessageBox.warning(self, i18n.tr("Shortcuts"), text)
