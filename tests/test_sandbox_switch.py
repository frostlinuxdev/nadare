# SPDX-License-Identifier: GPL-3.0-or-later
"""Sandbox (formerly "Secure mode"): the name, and a switch and pill on the game page (#840)."""

from __future__ import annotations

import os

os.environ.setdefault("QT_QPA_PLATFORM", "offscreen")

import time

import pytest

try:  # pragma: no cover - only when PyQt6 is missing
    from PyQt6.QtWidgets import QApplication, QMessageBox
except Exception:  # noqa: BLE001
    pytest.skip("PyQt6 is not importable in this interpreter", allow_module_level=True)

from nadare.core import backends
from nadare.core.config import Config
from nadare.gui.game_view import GameView
from nadare.gui.main_window import MainWindow

SUPPORTED = ("direct-pgr", "launcher-nikke", "stellasora-yostar")


@pytest.fixture(scope="module")
def qapp():
    app = QApplication.instance()
    if app is None:
        app = QApplication([])
    yield app


# ---- the name ----------------------------------------------------------------------------
@pytest.mark.parametrize("kind", SUPPORTED)
def test_the_option_is_called_sandbox_and_says_what_it_does(kind):
    option = next(o for o in backends.get(kind).options if o.key == "secure_mode")
    assert option.label == "Sandbox"
    assert "home folder" in option.help and "network" in option.help
    assert "Secure mode" not in option.label + option.help


def test_supports_sandbox_follows_the_backend_flag():
    for kind in SUPPORTED:
        assert backends.supports_sandbox(kind) is True
    assert backends.supports_sandbox("stove-czn") is False
    assert backends.supports_sandbox("no-such-kind") is False


# ---- the game page ------------------------------------------------------------------------
@pytest.fixture
def view(qapp):
    page = GameView()
    page.set_profile("pgr", {"backend": "direct-pgr", "title": "PGR"})
    yield page
    page.deleteLater()
    qapp.processEvents()


def test_the_switch_is_hidden_for_a_game_without_sandbox(view):
    view.set_sandbox(False, False)
    assert view.sandbox.isHidden() and view.sandbox_pill.isHidden()


def test_the_switch_and_pill_show_the_saved_state(view):
    view.set_sandbox(True, True)
    assert not view.sandbox.isHidden() and view.sandbox.isChecked()
    assert not view.sandbox_pill.isHidden() and view.sandbox_pill.text() == "SANDBOX"
    view.set_sandbox(True, False)
    assert not view.sandbox.isChecked() and view.sandbox_pill.isHidden()


def test_clicking_the_switch_tells_the_window_and_moves_the_pill(view):
    seen: list[bool] = []
    view.sandbox_toggled.connect(seen.append)
    view.set_sandbox(True, False)
    view.sandbox.click()
    assert seen == [True] and view.sandbox.isChecked() and not view.sandbox_pill.isHidden()
    view.sandbox.click()
    assert seen == [True, False] and view.sandbox_pill.isHidden()


def test_setting_the_state_from_code_does_not_fire_the_signal(view):
    seen: list[bool] = []
    view.sandbox_toggled.connect(seen.append)
    view.set_sandbox(True, True)
    assert seen == []


def test_a_blocked_switch_is_greyed_out_with_the_reason(view):
    view.set_sandbox(True, False, blocked="Not available right now.")
    assert not view.sandbox.isEnabled()
    assert "Not available" in view.sandbox.toolTip()


def test_the_tooltip_says_it_can_still_use_the_network(view):
    view.set_sandbox(True, False)
    tip = view.sandbox.toolTip()
    assert "home folder" in tip and "network" in tip


# ---- the window ---------------------------------------------------------------------------
def _window(tmp_path, backend="direct-pgr", **profile):
    config = Config.load(tmp_path / "config.json")
    config.profiles["pgr"] = {"backend": backend, "install_dir": str(tmp_path / "g"), **profile}
    window = MainWindow(config)
    window._current_profile = "pgr"
    window.game.set_profile("pgr", config.profiles["pgr"])
    return window


def _close(window, qapp):
    window.close()
    window.deleteLater()
    qapp.processEvents()


def test_the_window_saves_the_switch_to_the_profile(qapp, tmp_path):
    window = _window(tmp_path)
    try:
        window._on_sandbox_toggled(True)
        assert window.config.profiles["pgr"]["secure_mode"] is True
        again = Config.load(tmp_path / "config.json")
        assert again.profiles["pgr"]["secure_mode"] is True
        window._on_sandbox_toggled(False)
        assert Config.load(tmp_path / "config.json").profiles["pgr"]["secure_mode"] is False
    finally:
        _close(window, qapp)


def test_a_failed_save_puts_the_switch_back_and_says_so(qapp, tmp_path, monkeypatch):
    window = _window(tmp_path)
    warned: list[str] = []
    try:
        monkeypatch.setattr(QMessageBox, "critical", lambda *a, **k: warned.append(a[2]))
        monkeypatch.setattr(window.config, "save", lambda: (_ for _ in ()).throw(OSError("full")))
        window.game.set_sandbox(True, True)
        window._on_sandbox_toggled(True)
        assert window.config.profiles["pgr"].get("secure_mode") in (None, False)
        assert not window.game.sandbox.isChecked()
        assert warned and "full" in warned[0]
    finally:
        _close(window, qapp)


def test_opening_a_game_shows_its_saved_sandbox_state(qapp, tmp_path, monkeypatch):
    from nadare.core import gamesandbox

    # The pill needs a bubblewrap that really starts.
    monkeypatch.setattr(gamesandbox, "ready", lambda: True)
    window = _window(tmp_path, secure_mode=True)
    try:
        window._sync_sandbox("pgr")
        assert window.game.sandbox.isChecked() and not window.game.sandbox.isHidden()
        # The bubblewrap probe answers from a background thread (#1207 b191).
        deadline = time.monotonic() + 3
        while window._sandbox_probe_running and time.monotonic() < deadline:
            qapp.processEvents()
            time.sleep(0.01)
        qapp.processEvents()
        assert not window.game.sandbox_pill.isHidden()
    finally:
        _close(window, qapp)


def test_a_game_without_sandbox_shows_no_switch(qapp, tmp_path):
    window = _window(tmp_path, backend="stove-czn")
    try:
        window._sync_sandbox("pgr")
        assert window.game.sandbox.isHidden()
    finally:
        _close(window, qapp)


def test_in_the_flatpak_the_switch_is_enabled(qapp, tmp_path, monkeypatch):
    from nadare.core import gamesandbox, sandbox

    monkeypatch.setattr(sandbox, "in_flatpak", lambda: True)
    monkeypatch.setattr(
        gamesandbox, "_flatpak_spawn", lambda: "/usr/bin/flatpak-spawn", raising=False
    )
    window = _window(tmp_path)
    try:
        window._sync_sandbox("pgr")
        assert window.game.sandbox.isEnabled()
    finally:
        _close(window, qapp)


def test_the_switch_is_locked_while_the_game_runs_and_back_after(view):
    """A running game was started with the old setting, so a new one cannot apply to it."""
    view.set_sandbox(True, False)
    view._busy_op = "launch"
    view._refresh_actions()
    assert not view.sandbox.isEnabled()
    assert "stop the game" in view.sandbox.toolTip().lower()
    view._busy_op = None
    view._refresh_actions()
    assert view.sandbox.isEnabled()


def test_a_blocked_switch_stays_blocked_after_the_game_stops(view):
    view.set_sandbox(True, False, "Not available right now.")
    view._busy_op = "launch"
    view._refresh_actions()
    view._busy_op = None
    view._refresh_actions()
    assert not view.sandbox.isEnabled()
    assert "Not available" in view.sandbox.toolTip()
