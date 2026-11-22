# SPDX-License-Identifier: GPL-3.0-or-later
"""#1474 (GUI half): removing the other resource qualities is a controller operation.

It used to run as a bare pool task the controller never saw: an Update could be admitted while
the tiers were being deleted, the page did not show it busy, and quitting did not warn. Now it
goes through ``BackendController`` like an install: one operation per profile, the cross-process
operation lock, visible as busy, and counted as mutating at quit.
"""

from __future__ import annotations

import threading
import time

import pytest

pytest.importorskip("PyQt6.QtWidgets")

from PyQt6.QtWidgets import QApplication, QMessageBox  # noqa: E402

from nadare.backends.direct import WutheringWavesBackend  # noqa: E402
from nadare.core.config import Config  # noqa: E402
from nadare.core.process import OperationLock  # noqa: E402
from nadare.gui import workers  # noqa: E402
from nadare.gui.main_window import MainWindow  # noqa: E402

TIERS = [("uhd", "/nonexistent/UHD", 300)]


@pytest.fixture()
def app():
    return QApplication.instance() or QApplication([])


@pytest.fixture()
def window(tmp_path, app, monkeypatch):
    for kind in ("warning", "information", "critical"):
        monkeypatch.setattr(QMessageBox, kind, lambda *a, **k: None)
    config = Config.load(tmp_path / "config.json")
    config.profiles["w"] = {"backend": "direct-wuwa", "title": "WuWa",
                            "install_dir": str(tmp_path / "wuwa"), "wuwa_quality": "sd"}
    win = MainWindow(config)
    monkeypatch.setattr(win, "_ask_remove_qualities", lambda *a: True)
    yield win
    win.close()
    win.deleteLater()
    app.processEvents()


def _pump(app, until, timeout=10.0) -> None:
    end = time.monotonic() + timeout
    while time.monotonic() < end:
        app.processEvents()
        if until():
            return
        time.sleep(0.01)
    raise AssertionError("timed out")


def _run(app, window, tiers=TIERS) -> list[tuple[str, str]]:
    """Offer the cleanup and wait for the controller to report its end (it may queue behind
    the status probe the window starts on its own)."""
    ended: list[tuple[str, str]] = []
    window.controller.succeeded.connect(
        lambda p, op, code: op == "remove_qualities" and ended.append(("ok", ""))
    )
    window.controller.failed.connect(
        lambda p, op, msg: op == "remove_qualities" and ended.append(("failed", msg))
    )
    window._offer_quality_cleanup("w", tiers)
    _pump(app, lambda: bool(ended))
    app.processEvents()
    return ended


def test_the_cleanup_is_a_controller_operation(app, window, monkeypatch):
    gate = threading.Event()
    entered = threading.Event()

    def slow(self, only=None):
        entered.set()
        gate.wait(10)
        return (300, [])

    monkeypatch.setattr(WutheringWavesBackend, "remove_other_qualities", slow)
    window._offer_quality_cleanup("w", TIERS)
    _pump(app, entered.is_set)
    try:
        assert window.controller.running_operation("w") == "remove_qualities"
        assert window.controller.is_busy("w")
        assert "w" in window.controller.mutating_profiles()  # quitting warns
        assert window.controller.start("w", "update") is False  # no Update under the delete
    finally:
        gate.set()
    _pump(app, lambda: not window.controller.running_operation("w") == "remove_qualities")
    _pump(app, lambda: "w" not in window.controller.mutating_profiles())


def test_an_update_running_first_refuses_the_cleanup(app, window, monkeypatch):
    removed: list[str] = []
    monkeypatch.setattr(WutheringWavesBackend, "remove_other_qualities",
                        lambda self, only=None: removed.append("x") or (0, []))
    gate = threading.Event()
    entered = threading.Event()
    monkeypatch.setattr(
        WutheringWavesBackend, "update", lambda self: entered.set() or gate.wait(10)
    )
    assert window.controller.start("w", "update")
    _pump(app, entered.is_set)
    try:
        window._offer_quality_cleanup("w", TIERS)
    finally:
        gate.set()
    end = time.monotonic() + 10
    while window.controller.is_busy("w") and time.monotonic() < end:
        app.processEvents()
        time.sleep(0.01)
    assert removed == []


def test_another_instance_holding_the_game_refuses_the_cleanup(app, window, monkeypatch):
    removed: list[str] = []
    monkeypatch.setattr(WutheringWavesBackend, "remove_other_qualities",
                        lambda self, only=None: removed.append("x") or (0, []))
    failures: list[tuple[str, str]] = []
    window.controller.failed.connect(lambda p, op, msg: failures.append((op, msg)))
    lock = OperationLock("w")
    lock.acquire()
    try:
        window._offer_quality_cleanup("w", TIERS)
        end = time.monotonic() + 10
        while not failures and time.monotonic() < end:
            app.processEvents()
            time.sleep(0.01)
    finally:
        lock.release()
    assert removed == []
    assert failures and failures[0][0] == "remove_qualities"


def test_only_the_confirmed_tiers_go_to_the_backend(app, window, monkeypatch):
    seen: list[object] = []
    monkeypatch.setattr(WutheringWavesBackend, "remove_other_qualities",
                        lambda self, only=None: seen.append(list(only or [])) or (300, []))
    _run(app, window, [("uhd", "/x", 300), ("hd", "/y", 200)])
    assert seen == [["uhd", "hd"]]


def test_the_result_is_reported_on_the_page(app, window, monkeypatch):
    lines: list[str] = []
    monkeypatch.setattr(window, "_set_side_status", lines.append)
    monkeypatch.setattr(WutheringWavesBackend, "remove_other_qualities",
                        lambda self, only=None: (300, ["one folder was kept"]))
    _run(app, window)
    text = " ".join(lines)
    assert "Removed the UHD files" in text and "one folder was kept" in text


def test_the_worker_refuses_a_cleanup_without_its_snapshot(tmp_path):
    config = Config.load(tmp_path / "config.json")
    config.profiles["w"] = {"backend": "direct-wuwa"}
    worker = workers.BackendWorker(config, "w", "remove_qualities", payload=["uhd"])
    failures: list[str] = []
    worker.failed.connect(failures.append)
    worker.run()
    assert failures and "snapshot" in failures[0].lower()
