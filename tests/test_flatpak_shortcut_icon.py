# SPDX-License-Identifier: GPL-3.0-or-later
"""A shortcut made by the Flatpak names an icon the host has (rig, 2026-11-15).

The Flatpak exports its icon as ``io.github.frostlinuxdev.Nadare``; ``Icon=nadare`` resolved
to nothing on the host, so menu entries showed a blank icon.
"""

from __future__ import annotations

from nadare.core import shortcuts


def _icon(text: str) -> str:
    return next(line for line in text.splitlines() if line.startswith("Icon=")).split("=", 1)[1]


def test_flatpak_shortcut_uses_the_exported_icon(monkeypatch):
    monkeypatch.setattr(shortcuts.sandbox, "in_flatpak", lambda: True)
    text = shortcuts.desktop_entry("bd2", "bd2", ["flatpak", "run", shortcuts.FLATPAK_ID])
    assert _icon(text) == shortcuts.FLATPAK_ID


def test_appimage_shortcut_keeps_its_icon(monkeypatch):
    monkeypatch.setattr(shortcuts.sandbox, "in_flatpak", lambda: False)
    assert _icon(shortcuts.desktop_entry("bd2", "bd2", ["/x/Nadare.AppImage"])) == "nadare"


def test_an_explicit_icon_wins(monkeypatch):
    monkeypatch.setattr(shortcuts.sandbox, "in_flatpak", lambda: True)
    text = shortcuts.desktop_entry("bd2", "bd2", ["x"], icon="/art/bd2.png")
    assert _icon(text) == "/art/bd2.png"
