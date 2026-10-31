# SPDX-License-Identifier: GPL-3.0-or-later
"""#770: a game waiting for an install slot says so, and the message survives a revisit."""

from __future__ import annotations

import os
from pathlib import Path

os.environ.setdefault("QT_QPA_PLATFORM", "offscreen")

import pytest

try:
    from PyQt6.QtWidgets import QApplication
except Exception:  # pragma: no cover - optional GUI dependencies
    pytest.skip("PyQt6 is not importable in this interpreter", allow_module_level=True)

from nadare.core import artwork
from nadare.core.config import Config
from nadare.gui.main_window import MainWindow
from nadare.gui.workers import BackendController


class FakeWorker:
    def __init__(self, operation: str) -> None:
        self.operation = operation
        self.payload = None

    def isRunning(self) -> bool:  # noqa: N802 - Qt naming
        return True

    def cancel(self) -> None:
        pass

    def setParent(self, _parent) -> None:  # noqa: N802 - Qt naming
        pass

    def wait(self, _ms: int = 0) -> bool:
        return False


@pytest.fixture(scope="module")
def qapp():
    return QApplication.instance() or QApplication([])


@pytest.fixture(autouse=True)
def no_real_art(monkeypatch):
    monkeypatch.setattr(artwork, "fetch", lambda *args, **kwargs: None)


def _window(tmp_path: Path, monkeypatch) -> MainWindow:
    def fake_spawn(self, profile, operation, payload=None):
        if operation in ("status", "news", "media", "check_update"):
            return True  # background probes (started while the window is built) do not matter
        self._workers[profile] = FakeWorker(operation)
        self.busy.emit(profile, operation, True)
        return True

    monkeypatch.setattr(BackendController, "_spawn", fake_spawn)
    config = Config.load(tmp_path / "config.json")
    config.add_profile("a", backend="direct-pgr", title="A")
    config.add_profile("b", backend="direct-pgr", title="B")
    config.data["max_concurrent_installs"] = 1
    config.save()
    return MainWindow(config)


def _close(window, qapp) -> None:
    # Fake workers must not look like a running install: closing would ask to confirm quitting.
    window.controller._workers.clear()
    window.close()
    window.deleteLater()
    qapp.processEvents()


def test_a_waiting_game_says_it_is_waiting(tmp_path, qapp, monkeypatch):
    window = _window(tmp_path, monkeypatch)
    try:
        window.controller.start("a", "install")
        window.open_game("b")
        window.controller.start("b", "install")
        assert "Waiting" in window.game.step_label.text()
        assert window.controller.is_waiting("b")
    finally:
        _close(window, qapp)


def test_the_waiting_message_comes_back_after_leaving_and_returning(tmp_path, qapp, monkeypatch):
    window = _window(tmp_path, monkeypatch)
    try:
        window.controller.start("a", "install")
        window.open_game("b")
        window.controller.start("b", "install")
        window.open_game("a")
        assert "Waiting" not in window.game.step_label.text()
        window.open_game("b")
        assert "Waiting" in window.game.step_label.text()
    finally:
        _close(window, qapp)


def test_the_message_goes_when_the_slot_frees_up(tmp_path, qapp, monkeypatch):
    window = _window(tmp_path, monkeypatch)
    try:
        window.controller.start("a", "install")
        window.open_game("b")
        window.controller.start("b", "install")
        worker = window.controller._workers["a"]
        window.controller._on_succeeded("a", "install", 0, worker)
        assert not window.controller.is_waiting("b")
        assert "Waiting" not in window.game.step_label.text()
        assert "b" not in window._waiting
    finally:
        _close(window, qapp)


def test_cancelling_a_waiting_game_clears_the_message(tmp_path, qapp, monkeypatch):
    window = _window(tmp_path, monkeypatch)
    try:
        window.controller.start("a", "install")
        window.open_game("b")
        window.controller.start("b", "install")
        window.controller.cancel("b")
        assert "b" not in window._waiting
        window.open_game("a")
        window.open_game("b")
        assert "Waiting" not in window.game.step_label.text()
    finally:
        _close(window, qapp)
