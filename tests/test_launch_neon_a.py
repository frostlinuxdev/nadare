# SPDX-License-Identifier: GPL-3.0-or-later
"""Steam launch evidence and cancellation before a queued launch starts."""

import threading

import pytest
from PyQt6.QtWidgets import QMessageBox

from nadare.backends.steam import SteamBackend
from nadare.core import backends
from nadare.core.backends import BackendContext, GameBackend, Status
from nadare.core.config import Config
from nadare.gui.main_window import MainWindow
from nadare.gui.workers import wait_detached


def test_steam_pid_callback_uses_stop_matcher(tmp_path, monkeypatch):
    config = Config.load(tmp_path / "config.json")
    config.add_profile("g", backend="steam", steam_app_id="123456")
    backend = SteamBackend(BackendContext("g", config.profile("g"), config))
    calls = []

    def matching(app_id):
        calls.append(app_id)
        return [4242]

    monkeypatch.setattr(backend, "_matching_pids", matching)
    assert backend.game_pids()() == [4242]
    assert calls == ["123456"]


@pytest.mark.parametrize("app_id", ["", "bad-id"])
def test_invalid_steam_profile_has_safe_pid_callback(tmp_path, app_id):
    config = Config.load(tmp_path / "config.json")
    config.add_profile("g", backend="steam", steam_app_id=app_id)
    backend = SteamBackend(BackendContext("g", config.profile("g"), config))
    assert backend.game_pids()() == []


def test_sheet_stop_with_queued_launch_prevents_launch(tmp_path, monkeypatch, qtbot):
    entered = threading.Event()
    release = threading.Event()
    launched = threading.Event()

    class SlowBackend(GameBackend):
        kind = "slow-launch-test"

        def status(self):
            entered.set()
            release.wait(5)
            return Status("installed")

        def install(self):
            pass

        def update(self):
            pass

        def stop(self):
            pass

        def launch(self):
            launched.set()
            return 0

    monkeypatch.setitem(backends._REGISTRY, SlowBackend.kind, SlowBackend)
    config = Config.load(tmp_path / "config.json")
    config.add_profile("g", backend=SlowBackend.kind, title="Slow game")
    window = MainWindow(config)
    qtbot.addWidget(window)
    busy = []
    monkeypatch.setattr(QMessageBox, "information", lambda *args: busy.append(args))
    try:
        window.show()
        if not entered.is_set():
            window._start("g", "status", silent=True)
        qtbot.waitUntil(entered.is_set)
        assert window._start("g", "launch")
        sheet = window._launch_sheet
        assert sheet.isVisible()
        sheet.stop.click()
        release.set()
        qtbot.waitUntil(lambda: not window.controller.is_busy("g"))
        # Also drain the detached background worker before tearing down the window.
        qtbot.wait(100)
        assert not launched.is_set()
        assert busy == []
        assert window._launch_sheet is None
    finally:
        release.set()
        window.close()
        assert wait_detached()
