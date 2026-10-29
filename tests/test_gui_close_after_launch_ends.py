# SPDX-License-Identifier: GPL-3.0-or-later
"""A hidden launcher must quit once its last launch worker ends, however it ended."""

from __future__ import annotations

import os

os.environ.setdefault("QT_QPA_PLATFORM", "offscreen")

import pytest

try:  # pragma: no cover
    from PyQt6.QtWidgets import QApplication
except Exception:  # noqa: BLE001
    pytest.skip("PyQt6 is not importable in this interpreter", allow_module_level=True)

from nadare.core.config import Config
from nadare.gui import main_window as mw
from nadare.gui.main_window import MainWindow


@pytest.fixture(scope="module")
def qapp():
    app = QApplication.instance()
    if app is None:
        app = QApplication([])
    yield app


class _FakeWorker:
    def __init__(self, operation: str) -> None:
        self.operation = operation
        self.cancelled = False
        self.waited = False

    def isRunning(self) -> bool:  # noqa: N802 - Qt naming
        return not self.cancelled

    def cancel(self) -> None:
        self.cancelled = True

    def wait(self, ms: int) -> bool:  # noqa: N802 - Qt naming
        self.waited = True
        return True


def _window(tmp_path, qapp):
    window = MainWindow(Config.load(tmp_path / "config.json"))
    window.show()
    qapp.processEvents()
    return window


def _close(window, qapp):
    window.close()
    qapp.processEvents()


def _teardown(window, qapp):
    window.controller._workers.clear()
    window._quit_after_games = False
    QApplication.setQuitOnLastWindowClosed(True)
    window.deleteLater()
    qapp.processEvents()


def _closed_with_launch(tmp_path, qapp, monkeypatch):
    window = _window(tmp_path, qapp)
    window.controller._workers["game"] = _FakeWorker("launch")
    quits = []
    monkeypatch.setattr(mw.QApplication, "quit", staticmethod(lambda: quits.append(1)))
    _close(window, qapp)
    assert quits == []
    assert window._quit_after_games
    return window, quits


def test_failed_launch_after_close_quits(tmp_path, qapp, monkeypatch):
    monkeypatch.setattr(mw.QMessageBox, "warning", lambda *a, **k: 0)
    window, quits = _closed_with_launch(tmp_path, qapp, monkeypatch)
    try:
        window.controller.failed.emit("game", "launch", "the game could not start")
        qapp.processEvents()
        assert quits == [1]
    finally:
        _teardown(window, qapp)


def test_cancelled_launch_after_close_quits(tmp_path, qapp, monkeypatch):
    window, quits = _closed_with_launch(tmp_path, qapp, monkeypatch)
    try:
        window.controller.cancelled.emit("game", "launch")
        qapp.processEvents()
        assert quits == [1]
    finally:
        _teardown(window, qapp)


def test_successful_launch_quits_even_before_the_thread_stops(tmp_path, qapp, monkeypatch):
    window, quits = _closed_with_launch(tmp_path, qapp, monkeypatch)
    game = window.controller._workers["game"]
    try:
        # A QThread is still isRunning() while its succeeded slot is delivered.
        assert game.isRunning()
        window.controller.busy.emit("game", "launch", False)
        qapp.processEvents()
        assert quits == [1]
    finally:
        _teardown(window, qapp)


def test_close_does_not_quit_while_the_launch_still_runs(tmp_path, qapp, monkeypatch):
    window, quits = _closed_with_launch(tmp_path, qapp, monkeypatch)
    try:
        assert window.controller.launching_profiles() == ["game"]
        window.controller.busy.emit("game", "status", False)
        qapp.processEvents()
        assert quits == []
        assert window._quit_after_games
    finally:
        _teardown(window, qapp)
