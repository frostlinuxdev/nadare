# SPDX-License-Identifier: GPL-3.0-or-later
"""Inherited AppImage variables must not redirect a Flatpak's game shortcuts."""

from nadare.core import sandbox, shortcuts


def test_flatpak_game_shortcut_runs_flatpak_even_with_an_inherited_appimage():
    argv = shortcuts.launcher_argv({"APPIMAGE": "/downloads/Other.AppImage"}, in_flatpak=True)
    entry = shortcuts.desktop_entry("sample", "Sample", argv)
    assert f"Exec=flatpak run {shortcuts.FLATPAK_ID} launch -p sample" in entry
    assert "Other.AppImage" not in entry


def test_flatpak_command_hint_does_not_suggest_an_inherited_appimage(monkeypatch):
    monkeypatch.setenv("APPIMAGE", "/downloads/Other.AppImage")
    monkeypatch.setattr(sandbox, "in_flatpak", lambda: True)
    assert shortcuts.command_hint("report") == f"flatpak run {shortcuts.FLATPAK_ID} report"
