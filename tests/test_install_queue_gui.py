# SPDX-License-Identifier: GPL-3.0-or-later
"""A waiting game can move up the line or take the running install's place (no dragging)."""

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
    for name in ("a", "b", "c"):
        config.add_profile(name, backend="direct-pgr", title=name.upper())
    config.add_profile("w", backend="wine", title="W")
    config.save()
    return MainWindow(config)


def _close(window, qapp) -> None:
    window.controller._workers.clear()
    window.close()
    window.deleteLater()
    qapp.processEvents()


def _shown(window, button) -> bool:
    return button.isVisibleTo(window.game)


def test_a_waiting_game_offers_both_buttons_when_two_are_waiting(tmp_path, qapp, monkeypatch):
    window = _window(tmp_path, monkeypatch)
    try:
        for name in ("a", "b", "c"):
            window.open_game(name)
            window.controller.start(name, "install")
        # c is second in line: it can move up, and a running direct download can pause.
        assert _shown(window, window.game.queue_next_btn)
        assert _shown(window, window.game.queue_now_btn)
        assert "1 install ahead" in window.game.step_label.text()
        window.open_game("b")
        # b is first: moving up means nothing, but taking the place does.
        assert not _shown(window, window.game.queue_next_btn)
        assert _shown(window, window.game.queue_now_btn)
        assert "Waiting" in window.game.step_label.text()
        assert "ahead" not in window.game.step_label.text()
    finally:
        _close(window, qapp)


def test_download_next_moves_the_game_up_the_line(tmp_path, qapp, monkeypatch):
    window = _window(tmp_path, monkeypatch)
    try:
        for name in ("a", "b", "c"):
            window.open_game(name)
            window.controller.start(name, "install")
        window.game.queue_next_btn.click()
        assert window.controller.queue_order() == ["c", "b"]
        assert not _shown(window, window.game.queue_next_btn)  # c is first now
        assert "ahead" not in window.game.step_label.text()
    finally:
        _close(window, qapp)


def test_download_now_pauses_the_running_install(tmp_path, qapp, monkeypatch):
    window = _window(tmp_path, monkeypatch)
    try:
        for name in ("a", "b"):
            window.open_game(name)
            window.controller.start(name, "install")
        window.game.queue_now_btn.click()
        assert "a" in window.controller._pausing
        assert window.controller.queue_order() == ["b"]
    finally:
        _close(window, qapp)


def test_download_now_is_hidden_when_the_running_install_cannot_pause(
    tmp_path, qapp, monkeypatch
):
    window = _window(tmp_path, monkeypatch)
    try:
        window.open_game("w")
        window.controller.start("w", "install")  # a Wine installer does not resume from disk
        window.open_game("b")
        window.controller.start("b", "install")
        assert "Waiting" in window.game.step_label.text()
        assert not _shown(window, window.game.queue_now_btn)
    finally:
        _close(window, qapp)


def test_the_buttons_go_when_the_slot_frees_up(tmp_path, qapp, monkeypatch):
    window = _window(tmp_path, monkeypatch)
    try:
        window.controller.start("a", "install")
        window.open_game("b")
        window.controller.start("b", "install")
        assert _shown(window, window.game.queue_now_btn)
        window.controller._on_succeeded("a", "install", 0, window.controller._workers["a"])
        assert not _shown(window, window.game.queue_now_btn)
        assert not _shown(window, window.game.queue_next_btn)
    finally:
        _close(window, qapp)


def test_the_position_follows_the_line(tmp_path, qapp, monkeypatch):
    window = _window(tmp_path, monkeypatch)
    try:
        for name in ("a", "b", "c"):
            window.open_game(name)
            window.controller.start(name, "install")
        assert "1 install ahead" in window.game.step_label.text()
        window.controller.cancel("b")
        assert "ahead" not in window.game.step_label.text()
        assert not _shown(window, window.game.queue_next_btn)
    finally:
        _close(window, qapp)


def test_a_running_install_shows_no_queue_buttons(tmp_path, qapp, monkeypatch):
    window = _window(tmp_path, monkeypatch)
    try:
        window.open_game("a")
        window.controller.start("a", "install")
        assert not window.controller.is_waiting("a")
        assert not _shown(window, window.game.queue_next_btn)
        assert not _shown(window, window.game.queue_now_btn)
    finally:
        _close(window, qapp)
