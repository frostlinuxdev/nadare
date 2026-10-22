# SPDX-License-Identifier: GPL-3.0-or-later
"""Regression tests for GUI shutdown, cancel and verify lifecycle (impl2)."""

from __future__ import annotations

import os
import signal

os.environ.setdefault("QT_QPA_PLATFORM", "offscreen")

import pytest

try:  # pragma: no cover - exercised only when PyQt6 is missing
    from PyQt6.QtWidgets import QApplication
except Exception:  # noqa: BLE001
    pytest.skip("PyQt6 is not importable in this interpreter", allow_module_level=True)

from nadare.core.config import Config
from nadare.gui import workers
from nadare.gui.main_window import MainWindow
from nadare.gui.workers import BackendController


@pytest.fixture(scope="module")
def qapp():
    app = QApplication.instance()
    if app is None:
        app = QApplication([])
    yield app


class FakeWorker:
    """A running worker that can ignore Cancel, like a blocked network fetch."""

    def __init__(self, operation: str, *, honours_cancel: bool = False) -> None:
        self.operation = operation
        self.cancelled = False
        self._running = True
        self._honours_cancel = honours_cancel

    def isRunning(self) -> bool:  # noqa: N802 - Qt naming
        return self._running

    def cancel(self) -> None:
        self.cancelled = True
        if self._honours_cancel:
            self._running = False

    def finish(self) -> None:
        self._running = False

    def setParent(self, _parent) -> None:  # noqa: N802 - Qt naming
        pass

    def wait(self, _ms: int = 0) -> bool:
        return not self._running


def _controller(tmp_path) -> BackendController:
    config = Config.load(tmp_path / "config.json")
    config.add_profile("p", backend="wine", title="P")
    return BackendController(config)


def _window(tmp_path) -> MainWindow:
    config = Config.load(tmp_path / "config.json")
    config.add_profile("p", backend="wine", title="P")
    window = MainWindow(config)
    window.show()
    return window


def test_controller_refuses_new_work_after_shutdown(tmp_path, qapp):
    controller = _controller(tmp_path)
    spawned: list[tuple[str, object]] = []
    controller._spawn = lambda profile, operation, payload=None: spawned.append(
        (operation, payload)
    )

    controller.shutdown(wait_ms=0)

    assert controller.start("p", "status") is False
    controller._pending["p"] = ("install", None)
    controller._on_worker_finished("p", FakeWorker("status"))
    assert spawned == []
    assert "p" not in controller._pending


def test_closing_window_ignores_a_late_success_callback(tmp_path, qapp, monkeypatch):
    window = _window(tmp_path)
    started: list[tuple] = []
    monkeypatch.setattr(window, "_start", lambda *args, **kwargs: started.append(args) or True)
    try:
        window._closing = True
        window._on_succeeded("p", "install", 0)
        assert started == []
    finally:
        window.controller._workers.clear()
        window.close()
        window.deleteLater()
        qapp.processEvents()


def test_cancel_frees_the_slot_from_an_uncancellable_background_refresh(tmp_path, qapp):
    controller = _controller(tmp_path)
    news = FakeWorker("news")
    controller._workers["p"] = news
    spawned: list[tuple[str, object]] = []
    controller._spawn = lambda profile, operation, payload=None: spawned.append(
        (operation, payload)
    )
    cancelled: list[str] = []
    controller.cancelled.connect(lambda _profile, operation: cancelled.append(operation))

    try:
        assert controller.start("p", "install") is True
        assert controller._pending["p"] == ("install", None)

        controller.cancel("p")
        assert news.cancelled is True
        assert cancelled == ["install"]
        assert "p" not in controller._workers, "the blocked fetch must not hold the slot"
        assert news in workers._detached

        # A second click must not queue behind the cancelled (still-blocked) fetch.
        assert controller.start("p", "install") is True
        assert spawned == [("install", None)]
    finally:
        news.finish()
        controller._on_worker_finished("p", news)
        workers._detached.discard(news)


def test_verify_shows_a_disabled_button_not_cancel(tmp_path, qapp, monkeypatch):
    window = _window(tmp_path)
    monkeypatch.setattr(window.controller, "start", lambda *args, **kwargs: True)
    try:
        window.open_game("p")
        window._on_busy("p", "verify", True)
        assert window.game.primary.text() != "CANCEL"
        assert window.game.primary.isEnabled() is False

        window._on_busy("p", "install", True)
        assert window.game.primary.text() == "CANCEL"
        assert window.game.primary.isEnabled() is True
    finally:
        window.controller._workers.clear()
        window.close()
        window.deleteLater()
        qapp.processEvents()


def test_sigterm_is_routed_through_the_window_close(tmp_path, qapp, monkeypatch):
    from nadare.gui import _install_signal_handlers

    window = _window(tmp_path)
    requested: list[bool] = []
    monkeypatch.setattr(window, "_request_close_from_signal", lambda: requested.append(True))
    installed: dict[int, object] = {}
    monkeypatch.setattr(
        signal, "signal", lambda signum, handler: installed.setdefault(signum, handler)
    )
    try:
        _install_signal_handlers(window)
        assert signal.SIGTERM in installed and signal.SIGINT in installed

        installed[signal.SIGTERM](signal.SIGTERM, None)
        qapp.processEvents()
        assert requested == [True]
    finally:
        window.controller._workers.clear()
        window.close()
        window.deleteLater()
        qapp.processEvents()


def test_signal_close_stops_an_install_without_a_question(tmp_path, qapp, monkeypatch):
    from nadare.gui import main_window as mw

    window = _window(tmp_path)
    installing = FakeWorker("install", honours_cancel=True)
    window.controller._workers["p"] = installing
    asked: list[object] = []
    monkeypatch.setattr(mw.QMessageBox, "warning", lambda *args, **kwargs: asked.append(args))
    try:
        window._request_close_from_signal()
        qapp.processEvents()
        assert installing.cancelled is True
        assert asked == []
        assert not window.isVisible()
    finally:
        window.controller._workers.clear()
        window.deleteLater()
        qapp.processEvents()
