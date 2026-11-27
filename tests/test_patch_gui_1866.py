# SPDX-License-Identifier: GPL-3.0-or-later
"""The game page offers a declared overlay only where one exists (#1866)."""

from __future__ import annotations

import os

import pytest

os.environ.setdefault("QT_QPA_PLATFORM", "offscreen")
try:
    from PyQt6.QtWidgets import QApplication
except ImportError:  # pragma: no cover
    pytest.skip("PyQt6 is not importable in this interpreter", allow_module_level=True)

from nadare.core import patchspecs
from nadare.core.backends import Status
from nadare.gui import patch_choice
from nadare.gui.game_view import GameView

AZUR = {"backend": "azurpromilia-jp-yostar"}


@pytest.fixture(scope="module")
def qapp():
    return QApplication.instance() or QApplication([])


@pytest.fixture
def view(qapp):
    page = GameView()
    yield page
    page.close()
    page.deleteLater()


def test_only_a_game_with_a_declared_overlay_shows_the_switch(view):
    view.set_profile("azur", AZUR)
    view.set_status(Status(state="installed"))
    assert not view.patch_switch.isHidden()
    assert view.patch_switch.text().startswith("English patch")
    view.set_profile("ak", {"backend": "arknights-yostar"})
    view.set_status(Status(state="installed"))
    assert view.patch_switch.isHidden()


def test_the_switch_shows_whether_the_patch_is_applied(view):
    view.set_profile("azur", AZUR)
    view.set_status(Status(state="installed"))
    view.set_patch(True)
    assert view.patch_switch.isChecked()
    view.set_patch(False)
    assert not view.patch_switch.isChecked()


def test_the_switch_hides_without_an_install_and_locks_while_busy_or_running(view):
    view.set_profile("azur", AZUR)
    view.set_status(Status(state="not_installed"))
    assert view.patch_switch.isHidden()
    view.set_status(Status(state="installed", running=True))
    assert not view.patch_switch.isHidden()
    assert not view.patch_switch.isEnabled()
    assert "running" in view.patch_switch.toolTip().lower()
    view.set_status(Status(state="installed"))
    assert view.patch_switch.isEnabled()
    view.set_busy("update")
    assert not view.patch_switch.isEnabled()


def test_clicking_the_switch_tells_the_window_on_or_off(view):
    view.set_profile("azur", AZUR)
    view.set_status(Status(state="installed"))
    view.set_patch(False)
    asked = []
    view.patch_toggled.connect(lambda profile, on: asked.append((profile, on)))
    view.patch_switch.click()
    view.set_patch(True)
    view.patch_switch.click()
    assert asked == [("azur", True), ("azur", False)]


def test_the_offer_says_unofficial_ban_risk_and_that_updates_undo_it():
    choice = patch_choice.offer(patchspecs.KIBOTL_EN)
    assert choice.button == "Turn on"
    text = choice.text.lower()
    for words in ("unofficial", "ban", "update", "repair", "kibotl"):
        assert words in text


# ---- the window ---------------------------------------------------------------------------
def _window(tmp_path, monkeypatch, applied):
    from nadare.core import patchops
    from nadare.core.config import Config
    from nadare.gui.main_window import MainWindow

    config = Config.load(tmp_path / "config.json")
    config.profiles["azur"] = {**AZUR, "install_dir": str(tmp_path / "g")}
    monkeypatch.setattr(patchops, "recorded", lambda backend: ("kibotl-en",) if applied else ())
    window = MainWindow(config)
    window._current_profile = "azur"
    window.game.set_profile("azur", config.profiles["azur"])
    window.game.set_status(Status(state="installed"))
    started = []
    monkeypatch.setattr(window, "_start", lambda *args: started.append(args))
    return window, started


def _close(window, qapp):
    window.close()
    window.deleteLater()
    qapp.processEvents()


def test_the_window_shows_the_recorded_state(qapp, tmp_path, monkeypatch):
    window, _ = _window(tmp_path, monkeypatch, applied=True)
    try:
        window._sync_patch("azur")
        assert window.game.patch_switch.isChecked()
    finally:
        _close(window, qapp)


def test_turning_it_on_asks_once_about_the_risk_then_applies(qapp, tmp_path, monkeypatch):
    from PyQt6.QtWidgets import QMessageBox

    window, started = _window(tmp_path, monkeypatch, applied=False)
    asked = []
    try:
        monkeypatch.setattr(window, "_confirm_patch", lambda spec: asked.append(spec.id) or True)
        window._on_patch_toggled("azur", True)
        assert asked == ["kibotl-en"]
        assert started == [("azur", "apply_patch", "kibotl-en")]
        assert QMessageBox is not None
    finally:
        _close(window, qapp)


def test_saying_no_puts_the_switch_back_off(qapp, tmp_path, monkeypatch):
    window, started = _window(tmp_path, monkeypatch, applied=False)
    try:
        monkeypatch.setattr(window, "_confirm_patch", lambda spec: False)
        window.game.patch_switch.setChecked(True)
        window._on_patch_toggled("azur", True)
        assert started == []
        assert not window.game.patch_switch.isChecked()
    finally:
        _close(window, qapp)


def test_turning_it_off_removes_without_a_question(qapp, tmp_path, monkeypatch):
    window, started = _window(tmp_path, monkeypatch, applied=True)
    try:
        monkeypatch.setattr(window, "_confirm_patch", lambda spec: pytest.fail("asked"))
        window._on_patch_toggled("azur", False)
        assert started == [("azur", "remove_patch", "kibotl-en")]
    finally:
        _close(window, qapp)


def test_the_switch_follows_the_files_after_an_operation(qapp, tmp_path, monkeypatch):
    window, _ = _window(tmp_path, monkeypatch, applied=True)
    try:
        window.game.set_patch(False)
        window._on_busy("azur", "apply_patch", False)
        assert window.game.patch_switch.isChecked()
    finally:
        _close(window, qapp)
