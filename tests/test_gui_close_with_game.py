# SPDX-License-Identifier: GPL-3.0-or-later
"""Closing the window must never kill a running game (issue #20)."""

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
from nadare.gui.workers import BackendController


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


def test_close_during_game_hides_and_keeps_game_running(tmp_path, qapp, monkeypatch):
    window = _window(tmp_path, qapp)
    game, probe = _FakeWorker("launch"), _FakeWorker("status")
    window.controller._workers.update({"game": game, "other": probe})
    asked = []
    monkeypatch.setattr(mw.QMessageBox, "warning", lambda *a, **k: asked.append(a) or 0)
    try:
        _close(window, qapp)
        assert not window.isVisible()
        assert not game.cancelled and not game.waited  # the game is untouched
        assert probe.cancelled  # background probes are still stopped
        assert window._quit_after_games
        assert QApplication.quitOnLastWindowClosed() is False
        assert asked == []  # no scary dialog for a running game
    finally:
        _teardown(window, qapp)


def test_quits_when_the_game_exits_after_close(tmp_path, qapp, monkeypatch):
    window = _window(tmp_path, qapp)
    window.controller._workers["game"] = _FakeWorker("launch")
    quits = []
    monkeypatch.setattr(mw.QApplication, "quit", staticmethod(lambda: quits.append(1)))
    try:
        _close(window, qapp)
        assert quits == []  # game still running
        window.controller._workers.clear()  # the game exited
        window.controller.busy.emit("game", "launch", False)
        qapp.processEvents()
        assert quits == [1]
    finally:
        _teardown(window, qapp)


def test_reopened_window_does_not_quit_after_game(tmp_path, qapp, monkeypatch):
    window = _window(tmp_path, qapp)
    window.controller._workers["game"] = _FakeWorker("launch")
    quits = []
    monkeypatch.setattr(mw.QApplication, "quit", staticmethod(lambda: quits.append(1)))
    try:
        _close(window, qapp)
        window.show()  # second start / activation brings it back
        qapp.processEvents()
        assert QApplication.quitOnLastWindowClosed() is True
        window.controller._workers.clear()
        window.controller.busy.emit("game", "launch", False)
        qapp.processEvents()
        assert quits == []
        assert window.isVisible()
    finally:
        _teardown(window, qapp)


def test_close_without_game_still_closes(tmp_path, qapp):
    window = _window(tmp_path, qapp)
    try:
        _close(window, qapp)
        assert not window.isVisible()
        assert not window._quit_after_games
    finally:
        _teardown(window, qapp)


def test_install_still_warns_and_game_is_not_named(tmp_path, qapp, monkeypatch):
    window = _window(tmp_path, qapp)
    window.controller._workers.update(
        {"playing": _FakeWorker("launch"), "inst": _FakeWorker("install")}
    )
    seen = []

    def warn(parent, title, text, *a):
        seen.append(text)
        return mw.QMessageBox.StandardButton.Cancel

    monkeypatch.setattr(mw.QMessageBox, "warning", warn)
    try:
        _close(window, qapp)
        assert window.isVisible()  # user cancelled the close
        assert len(seen) == 1 and "inst" in seen[0] and "playing" not in seen[0]
        assert not window.controller._workers["playing"].cancelled
    finally:
        _teardown(window, qapp)


def test_controller_shutdown_keep_launches(tmp_path, qapp):
    controller = BackendController(Config.load(tmp_path / "config.json"))
    game, probe = _FakeWorker("launch"), _FakeWorker("news")
    controller._workers.update({"g": game, "p": probe})
    assert controller.launching_profiles() == ["g"]
    controller.shutdown(wait_ms=10, keep_launches=True)
    assert probe.cancelled and not game.cancelled
    controller.shutdown(wait_ms=10)
    assert game.cancelled
    controller._workers.clear()


def test_does_not_quit_while_a_queued_remember_me_worker_runs(tmp_path, qapp, monkeypatch):
    """r3: the pending Remember-me worker started when the game exits must not
    be cancelled by the background close."""
    window = _window(tmp_path, qapp)
    window.controller._workers["game"] = _FakeWorker("launch")
    quits = []
    monkeypatch.setattr(mw.QApplication, "quit", staticmethod(lambda: quits.append(1)))
    try:
        _close(window, qapp)
        window.controller._workers.clear()  # the game exited ...
        window.controller._workers["game"] = _FakeWorker("set_remember")  # ... a worker started
        window.controller.busy.emit("game", "launch", False)
        qapp.processEvents()
        assert quits == []
        window.controller._workers.clear()  # it finished
        window.controller.busy.emit("game", "set_remember", False)
        qapp.processEvents()
        assert quits == [1]
    finally:
        _teardown(window, qapp)
