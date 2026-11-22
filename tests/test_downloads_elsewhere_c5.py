# SPDX-License-Identifier: GPL-3.0-or-later
"""C5 (conflict review 11-21, r3b05 F2): Downloads shows a game another Nadare window or
command is working on, not "Nothing downloading" and not a Resume that must fail."""

from __future__ import annotations

import os
from pathlib import Path

os.environ.setdefault("QT_QPA_PLATFORM", "offscreen")

import pytest

try:
    from PyQt6.QtWidgets import QApplication
except Exception:  # pragma: no cover - optional GUI dependencies
    pytest.skip("PyQt6 is not importable in this interpreter", allow_module_level=True)

from nadare.core import artwork, process
from nadare.core.config import Config
from nadare.gui.downloads_page import DownloadRow, DownloadRowWidget
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


def _start(window, *names):
    for name in names:
        window.controller.start(name, "install")


def _held_elsewhere(monkeypatch, *names):
    monkeypatch.setattr(process, "operation_in_progress", lambda profile: profile in names)


def test_an_elsewhere_row_says_so_and_offers_no_buttons(qapp):
    widget = DownloadRowWidget("b")
    widget.apply(DownloadRow("b", "B", "elsewhere", "elsewhere"))
    assert "Another Nadare window or command" in widget.status_label.text()
    for button in (
        widget.pause_btn,
        widget.resume_btn,
        widget.next_btn,
        widget.now_btn,
        widget.cancel_btn,
    ):
        assert not button.isVisibleTo(widget)
    assert not widget.bar.isVisibleTo(widget)
    widget.deleteLater()


def test_a_cli_download_is_listed_not_nothing_downloading(tmp_path, qapp, monkeypatch):
    window = _window(tmp_path, monkeypatch)
    try:
        _held_elsewhere(monkeypatch, "b")
        window.show_downloads()
        assert window.downloads.row_profiles() == ["b"]
        assert not window.downloads.empty_title.isVisibleTo(window.downloads)
        assert "Another Nadare window" in window.downloads.row("b").status_label.text()
    finally:
        _close(window, qapp)


def test_a_paused_row_taken_over_elsewhere_has_no_resume(tmp_path, qapp, monkeypatch):
    window = _window(tmp_path, monkeypatch)
    try:
        _start(window, "a", "b")
        window.show_downloads()
        window.downloads.row("b").pause_btn.click()
        assert window.controller.is_paused("b")
        _held_elsewhere(monkeypatch, "a", "b")  # a: our own worker holds it
        window.show_downloads()
        assert window.downloads.row_profiles() == ["a", "b"]
        row = window.downloads.row("b")
        assert not row.resume_btn.isVisibleTo(window.downloads)
        assert "Another Nadare window" in row.status_label.text()
        # Our own running install keeps its row and its buttons.
        assert window.downloads.row("a").pause_btn.isVisibleTo(window.downloads)
    finally:
        _close(window, qapp)


def test_the_open_page_notices_a_cli_download_on_the_status_tick(tmp_path, qapp, monkeypatch):
    window = _window(tmp_path, monkeypatch)
    try:
        window.show_downloads()
        assert window.downloads.row_profiles() == []
        _held_elsewhere(monkeypatch, "c")
        monkeypatch.setattr(window, "isVisible", lambda: True)
        window._on_status_timer()
        assert window.downloads.row_profiles() == ["c"]
        _held_elsewhere(monkeypatch)
        window._on_status_timer()
        assert window.downloads.row_profiles() == []
    finally:
        _close(window, qapp)
