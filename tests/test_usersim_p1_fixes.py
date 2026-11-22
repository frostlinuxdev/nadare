# SPDX-License-Identifier: GPL-3.0-or-later
# ruff: noqa: F811 - tests take the fixtures imported below as arguments
"""The P1s of the 2026-11 user simulation, checked again on a real KDE Wayland desktop.

* Move game: every game Nadare installed showed only "Move prefix only…", because Nadare's own
  ``shader-versions.json`` (written beside the game since #1364) counted as foreign state.
* Closing Nadare during a download dropped the games waiting in line without a word, and the
  warning named the game by its internal id. The line is now named, remembered, and offered back
  at the next start.
* A double-click on CANCEL put its second click on the Stop download question that the first
  click opened, a few pixels from "Stop and remove the game…".
"""

from __future__ import annotations

from pathlib import Path

import pytest
from _gui_smoke_helpers import (  # noqa: F401 - helpers and pytest fixtures, found by name
    _collect_garbage_between_tests,
    _empty_config,
    _wait_for,
    qapp,
)
from PyQt6.QtWidgets import QMessageBox

from nadare.core import paths, relink, shadercache
from nadare.gui.main_window import MainWindow

# ---- Move game --------------------------------------------------------------


@pytest.fixture
def profiles_root(tmp_path, monkeypatch):
    root = tmp_path / "data" / "profiles"
    root.mkdir(parents=True)
    monkeypatch.setattr(paths, "profiles_dir", lambda: root)
    return root


def _store_game(root: Path, ident: str) -> dict:
    home = root / ident
    (home / "prefix" / "drive_c").mkdir(parents=True)
    (home / "install").mkdir()
    (home / "install" / "game.exe").write_bytes(b"MZ" * 50)
    return {
        "backend": "direct-bd2",
        "title": "Brown Dust 2",
        "prefix": str(home / "prefix"),
        "install_dir": str(home / "install"),
        "owned_trees": ["install_dir", "prefix"],
    }


def test_the_shader_record_does_not_block_moving_the_whole_game(profiles_root):
    profile = _store_game(profiles_root, "bd2")
    (profiles_root / "bd2" / shadercache.VERSIONS_FILE).write_text('{"DXVK": "2.7"}')
    assert relink.managed_home(profile) == profiles_root / "bd2"


def test_a_backends_own_state_still_keeps_the_prefix_only_move(profiles_root):
    profile = _store_game(profiles_root, "bd2")
    (profiles_root / "bd2" / shadercache.VERSIONS_FILE).write_text("{}")
    (profiles_root / "bd2" / "session.json").write_text("{}")
    assert relink.managed_home(profile) is None


def test_the_locked_prefix_note_reads_as_one_sentence():
    from nadare.gui.settings import SettingsPage

    note = SettingsPage._LOCKED_NOTE
    assert "would make Nadare would" not in note
    assert "Nadare would no longer find the game" in note


# ---- Closing during downloads -----------------------------------------------


def _window_with(tmp_path, monkeypatch, *, running, waiting):
    config = _empty_config(tmp_path)
    titles = {"aether-gazer": "Aether Gazer", "arknights": "Arknights", "bd2": "Brown Dust 2"}
    for name in titles:
        config.profiles[name] = {"backend": "direct-" + name, "title": titles[name]}
    window = MainWindow(config)
    monkeypatch.setattr(window, "_game_title", lambda name: titles.get(name, name))
    monkeypatch.setattr(window.controller, "launching_profiles", lambda: [])
    monkeypatch.setattr(window.controller, "mutating_profiles", lambda: list(running))
    monkeypatch.setattr(window.controller, "running_operation", lambda name: "install")
    monkeypatch.setattr(window.controller, "queue_order", lambda: list(waiting))
    monkeypatch.setattr(window.controller, "waiting_operation", lambda name: "install")
    return window


def _drop(window) -> None:
    """Stop the status probes the test profiles started, then let Qt delete the window."""
    window.controller.shutdown()
    window.deleteLater()


def test_the_close_warning_names_games_by_title_and_counts_the_line(tmp_path, qapp, monkeypatch):
    from nadare.gui import main_window as mw

    window = _window_with(tmp_path, monkeypatch, running=["aether-gazer"], waiting=["arknights"])
    try:
        seen: list[str] = []
        monkeypatch.setattr(
            mw.QMessageBox,
            "warning",
            lambda *args, **kwargs: seen.append(args[2]) or QMessageBox.StandardButton.Cancel,
        )
        assert window._confirm_close() is False
        assert "Aether Gazer" in seen[0]
        assert "aether-gazer" not in seen[0]
        assert "Arknights" in seen[0]
    finally:
        _drop(window)


def test_closing_remembers_the_downloads_in_line_and_the_next_start_offers_them(
    tmp_path, qapp, monkeypatch
):
    window = _window_with(tmp_path, monkeypatch, running=["aether-gazer"], waiting=["arknights"])
    try:
        window._remember_interrupted_downloads()
        assert window.config.data["interrupted_downloads"] == [
            ["aether-gazer", "install"],
            ["arknights", "install"],
        ]
        started: list[tuple[str, str]] = []
        monkeypatch.setattr(window, "_start", lambda p, op, *a, **k: started.append((p, op)) or 1)
        asked: list[str] = []
        monkeypatch.setattr(
            window,
            "_ask_continue_downloads",
            lambda titles: asked.append(", ".join(titles)) or True,
        )
        window._offer_interrupted_downloads()
        assert asked == ["Aether Gazer, Arknights"]
        assert started == [("aether-gazer", "install"), ("arknights", "install")]
        assert "interrupted_downloads" not in window.config.data
        # Said once: a second start does not ask again.
        window._offer_interrupted_downloads()
        assert len(asked) == 1
    finally:
        _drop(window)


def test_declining_forgets_the_line_and_games_removed_meanwhile_are_skipped(
    tmp_path, qapp, monkeypatch
):
    window = _window_with(tmp_path, monkeypatch, running=[], waiting=[])
    try:
        window.config.data["interrupted_downloads"] = [["gone", "install"], ["bd2", "update"]]
        started: list[tuple[str, str]] = []
        monkeypatch.setattr(window, "_start", lambda p, op, *a, **k: started.append((p, op)) or 1)
        asked: list[list[str]] = []
        monkeypatch.setattr(
            window, "_ask_continue_downloads", lambda titles: asked.append(list(titles)) or False
        )
        window._offer_interrupted_downloads()
        assert asked == [["Brown Dust 2"]]
        assert started == []
        assert "interrupted_downloads" not in window.config.data
    finally:
        _drop(window)


def test_a_hand_edited_record_is_ignored(tmp_path, qapp, monkeypatch):
    window = _window_with(tmp_path, monkeypatch, running=[], waiting=[])
    try:
        window.config.data["interrupted_downloads"] = ["bd2", ["bd2", "rm -rf"], 3, None]
        asked: list[list[str]] = []
        monkeypatch.setattr(
            window, "_ask_continue_downloads", lambda titles: asked.append(list(titles)) or True
        )
        window._offer_interrupted_downloads()
        assert asked == []
    finally:
        _drop(window)


# ---- Double-click into a question -------------------------------------------


def test_a_question_ignores_clicks_for_a_moment_after_it_opens(qapp):
    from nadare.gui import clickguard

    box = QMessageBox()
    try:
        box.addButton("Stop and remove the game…", QMessageBox.ButtonRole.DestructiveRole)
        box.addButton("Keep downloading", QMessageBox.ButtonRole.RejectRole)
        clickguard.hold_clicks(box, 60)
        assert not any(button.isEnabled() for button in box.buttons())
        assert _wait_for(qapp, lambda: all(button.isEnabled() for button in box.buttons()), 2.0)
    finally:
        box.deleteLater()


def test_the_stop_download_question_opens_with_its_buttons_held(tmp_path, qapp, monkeypatch):
    from nadare.gui import main_window as mw

    window = MainWindow(_empty_config(tmp_path))
    try:
        states: list[list[bool]] = []

        def fake_exec(box):
            states.append([button.isEnabled() for button in box.buttons()])
            return 0

        monkeypatch.setattr(QMessageBox, "exec", fake_exec)
        assert mw._REAL_ASK_CANCEL_CHOICE(window, "Aether Gazer", True) == "continue"
        assert states and states[0] and not any(states[0])
    finally:
        window.deleteLater()


# ---- The download speed limit field -----------------------------------------


@pytest.mark.parametrize("kind", ["QSpinBox", "QDoubleSpinBox"])
def test_number_fields_get_the_dark_field_background(qapp, kind):
    """The speed limit is a QDoubleSpinBox, which the theme missed: white on white on KDE."""
    from PyQt6 import QtWidgets

    from nadare.gui import theme

    field = getattr(QtWidgets, kind)()
    try:
        field.setStyleSheet(theme.stylesheet())
        field.resize(220, 40)
        image = field.grab().toImage()
        assert image.pixelColor(30, 20).lightness() < 100
    finally:
        field.deleteLater()
