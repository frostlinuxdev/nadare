# SPDX-License-Identifier: GPL-3.0-or-later
"""Relinking re-checks that the game is not running at the moment of the click (edge hunt)."""

from __future__ import annotations

import os

os.environ.setdefault("QT_QPA_PLATFORM", "offscreen")

import threading
from types import SimpleNamespace

import pytest

try:  # pragma: no cover - only when PyQt6 is missing
    from PyQt6.QtCore import QThreadPool
    from PyQt6.QtWidgets import QApplication, QFileDialog, QMessageBox
except Exception:  # noqa: BLE001
    pytest.skip("PyQt6 is not importable in this interpreter", allow_module_level=True)

from nadare.core.config import Config
from nadare.gui import settings as settings_mod
from nadare.gui.settings import SettingsPage


@pytest.fixture(scope="module")
def qapp():
    app = QApplication.instance()
    if app is None:
        app = QApplication([])
    yield app


def _settle(qapp):
    QThreadPool.globalInstance().waitForDone(5000)
    for _ in range(5):
        qapp.processEvents()


class Game:
    """A backend whose answer can change between the page's probe and the relink click."""

    def __init__(self):
        self.state = "installed"
        self.fail = False
        self.gate: threading.Event | None = None
        self.calls = 0

    def status(self):
        self.calls += 1
        if self.gate is not None:
            self.gate.wait(5)
        if self.fail:
            raise RuntimeError("cannot look")
        return SimpleNamespace(state=self.state)


@pytest.fixture
def rig(tmp_path, qapp, monkeypatch):
    game = Game()
    monkeypatch.setattr(
        settings_mod, "create_backend", lambda profile, config: SimpleNamespace(status=game.status)
    )
    new = tmp_path / "elsewhere" / "pfx"
    (new / "drive_c").mkdir(parents=True)
    dialogs: list[str] = []
    monkeypatch.setattr(
        QFileDialog, "getExistingDirectory", lambda *a, **k: dialogs.append("d") or str(new)
    )
    shown: list[str] = []
    yes = QMessageBox.StandardButton.Yes
    monkeypatch.setattr(QMessageBox, "question", lambda *a, **k: yes)
    monkeypatch.setattr(QMessageBox, "warning", lambda *a, **k: shown.append(a[2]))
    monkeypatch.setattr(QMessageBox, "critical", lambda *a, **k: shown.append(a[2]))
    config = Config.load(tmp_path / "config.json")
    config.add_profile("sample", backend="stove-czn", prefix=str(tmp_path / "pfx"))
    page = SettingsPage()
    page.load(config, "sample")
    _settle(qapp)
    yield SimpleNamespace(
        page=page, config=config, game=game, new=new, shown=shown, dialogs=dialogs
    )
    page.deleteLater()
    qapp.processEvents()


def test_a_game_started_after_the_page_looked_blocks_the_relink(rig, qapp):
    rig.game.state = "running"  # started outside Nadare after the page's own probe
    rig.page.relink_button.click()
    _settle(qapp)
    assert rig.config.profiles["sample"]["prefix"] != str(rig.new)
    assert any("running" in text.lower() for text in rig.shown)
    assert not rig.page.relink_button.isEnabled(), "the page now knows the game is running"


def test_an_idle_game_is_relinked_as_before(rig, qapp):
    rig.page.relink_button.click()
    _settle(qapp)
    assert rig.config.profiles["sample"]["prefix"] == str(rig.new)
    assert Config.load(rig.config.path).profiles["sample"]["prefix"] == str(rig.new)
    assert rig.page.option_widget("prefix").text() == str(rig.new)
    assert rig.shown == []


def test_a_probe_that_cannot_answer_refuses_rather_than_guesses(rig, qapp):
    rig.game.fail = True
    rig.page.relink_button.click()
    _settle(qapp)
    assert rig.config.profiles["sample"]["prefix"] != str(rig.new)
    assert any("could not check" in text.lower() for text in rig.shown)


def test_a_second_click_while_the_check_runs_does_nothing(rig, qapp):
    rig.game.gate = threading.Event()
    before = rig.game.calls
    rig.page.relink_button.click()
    assert not rig.page.relink_button.isEnabled(), "disabled while the check runs"
    rig.page.relink_button.click()
    rig.game.gate.set()
    _settle(qapp)
    assert rig.game.calls == before + 1
    assert rig.dialogs == ["d"]
    assert rig.config.profiles["sample"]["prefix"] == str(rig.new)
