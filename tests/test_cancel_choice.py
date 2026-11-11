# SPDX-License-Identifier: GPL-3.0-or-later
"""#991: stopping a download says the files stay, and a first install can be removed instead."""

from __future__ import annotations

import os
from pathlib import Path

os.environ.setdefault("QT_QPA_PLATFORM", "offscreen")

import pytest

try:  # pragma: no cover - only when PyQt6 is missing
    from PyQt6.QtWidgets import QApplication
except Exception:  # noqa: BLE001
    pytest.skip("PyQt6 is not importable in this interpreter", allow_module_level=True)

from nadare.core.config import Config
from nadare.gui import main_window as main_window_mod
from nadare.gui.main_window import MainWindow
from nadare.gui.workers import BackendController


@pytest.fixture(scope="module")
def qapp():
    return QApplication.instance() or QApplication([])


@pytest.fixture
def rig(tmp_path: Path, qapp, monkeypatch):
    monkeypatch.setattr(BackendController, "start", lambda *a, **k: True)
    config = Config.load(tmp_path / "config.json")
    config.add_profile("wuwa", backend="direct-wuwa", title="Wuthering Waves")
    config.save()
    window = MainWindow(config)
    asked: list[tuple[str, bool]] = []
    answer = {"choice": "keep"}
    cancelled: list[str] = []
    removed: list[tuple[str, frozenset]] = []

    def fake_ask(self, title, first_install):
        asked.append((title, first_install))
        return answer["choice"]

    monkeypatch.setattr(MainWindow, "_ask_cancel_choice", fake_ask)
    monkeypatch.setattr(window.controller, "cancel", cancelled.append)
    monkeypatch.setattr(
        window,
        "_on_remove_requested",
        lambda profile, preselect=frozenset(): removed.append((profile, frozenset(preselect))),
    )
    window.open_game("wuwa")
    yield window, asked, answer, cancelled, removed
    window.close()
    window.deleteLater()
    qapp.processEvents()


def _running(window, monkeypatch, operation):
    monkeypatch.setattr(window.controller, "running_operation", lambda p: operation)
    monkeypatch.setattr(window.controller, "paused_operation", lambda p: "")


def test_stopping_a_first_install_asks_and_keep_stops_it(rig, monkeypatch):
    window, asked, answer, cancelled, removed = rig
    _running(window, monkeypatch, "install")
    window._on_cancel()
    assert asked == [("Wuthering Waves", True)]
    assert cancelled == ["wuwa"] and removed == []


def test_keep_downloading_changes_nothing(rig, monkeypatch):
    window, asked, answer, cancelled, removed = rig
    _running(window, monkeypatch, "install")
    answer["choice"] = "continue"
    window._on_cancel()
    assert cancelled == [] and removed == []


def test_remove_stops_then_opens_the_remove_dialog_with_the_files_ticked(rig, monkeypatch):
    window, asked, answer, cancelled, removed = rig
    _running(window, monkeypatch, "install")
    answer["choice"] = "remove"
    window._on_cancel()
    assert cancelled == ["wuwa"]
    assert removed == []  # not while the worker still runs
    window._on_cancelled("wuwa", "install")
    assert removed == [("wuwa", frozenset({"install_dir", "prefix"}))]
    window._on_cancelled("wuwa", "install")
    assert len(removed) == 1  # once


def test_an_update_is_asked_about_without_the_remove_choice(rig, monkeypatch):
    window, asked, answer, cancelled, removed = rig
    _running(window, monkeypatch, "update")
    window._on_cancel()
    assert asked == [("Wuthering Waves", False)]
    assert cancelled == ["wuwa"]


@pytest.mark.parametrize("operation", ["launch", "login", "verify", "status"])
def test_other_operations_stop_without_a_question(rig, monkeypatch, operation):
    window, asked, answer, cancelled, removed = rig
    _running(window, monkeypatch, operation)
    window._on_cancel()
    assert asked == [] and cancelled == ["wuwa"]


def test_a_paused_install_is_asked_about_too_and_the_downloads_page_uses_the_same_question(
    rig, monkeypatch
):
    window, asked, answer, cancelled, removed = rig
    monkeypatch.setattr(window.controller, "running_operation", lambda p: "")
    monkeypatch.setattr(window.controller, "paused_operation", lambda p: "install")
    window.downloads.cancel_requested.emit("wuwa")
    assert asked == [("Wuthering Waves", True)]
    assert cancelled == ["wuwa"]


def test_the_real_question_names_the_choices(qapp, monkeypatch):
    from PyQt6.QtWidgets import QMessageBox

    seen: dict[str, object] = {}

    def fake_exec(box):
        seen["buttons"] = [b.text() for b in box.buttons()]
        seen["text"] = box.text() + " " + box.informativeText()
        seen["default"] = box.defaultButton().text()
        return 0

    monkeypatch.setattr(QMessageBox, "exec", fake_exec)
    real = main_window_mod._REAL_ASK_CANCEL_CHOICE
    assert real(None, "Wuthering Waves", True) == "continue"  # nothing clicked
    assert "stays on disk" in seen["text"]
    assert seen["default"] == "Keep downloading"
    assert any("remove" in b.lower() for b in seen["buttons"])
    real(None, "Wuthering Waves", False)
    assert not any("remove" in b.lower() for b in seen["buttons"])
