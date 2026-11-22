# SPDX-License-Identifier: GPL-3.0-or-later
"""The presentation adapter retains launch-end and hidden-close behaviour."""
import pytest
from PyQt6.QtWidgets import QApplication

from nadare.core.config import Config
from nadare.gui.main_window import MainWindow


@pytest.fixture
def window(tmp_path, monkeypatch):
    app = QApplication.instance() or QApplication([])
    config = Config.load(tmp_path / "config.json")
    config.add_profile("alpha", backend="wine")
    monkeypatch.setattr(MainWindow, "_probe_all", lambda self: None)
    win = MainWindow(config)
    monkeypatch.setattr(win, "_start", lambda *a, **k: False)
    monkeypatch.setattr(win, "_notify_finished", lambda *a: None)
    monkeypatch.setattr(win, "_maybe_offer_runner_cleanup", lambda: None)
    monkeypatch.setattr(win, "_is_terms_failure", lambda *a: False)
    monkeypatch.setattr(win, "_mark_status_error", lambda *a: None)
    monkeypatch.setattr(win.game, "fail", lambda *a, **k: None)
    monkeypatch.setattr(win, "_confirm_close", lambda: True)
    yield win, app
    win._quit_after_games = False
    win.controller._launches.clear()
    win.controller._workers.clear()
    win.close()
    win.deleteLater()
    app.processEvents()
    QApplication.setQuitOnLastWindowClosed(True)


@pytest.mark.parametrize("terminal", ["succeeded", "failed", "cancelled", "busy_false"])
def test_launch_ended_and_background_close(window, monkeypatch, terminal):
    win, app = window
    win._current_profile = "alpha"
    win.controller._launches.add("alpha")
    win._on_busy("alpha", "launch", True)
    win._quit_after_games = True
    finished = []
    monkeypatch.setattr(win, "_finish_background_close", lambda: finished.append(True))
    if terminal == "busy_false":
        win._on_busy("alpha", "launch", False)
    elif terminal == "succeeded":
        win._on_succeeded("alpha", "launch", 0)
    elif terminal == "failed":
        win._on_failed("alpha", "launch", "failed")
    else:
        win._on_cancelled("alpha", "launch")
    app.processEvents()
    assert not win.controller.launching_profiles()
    assert finished == [True]
    assert "alpha" not in win._busy


def test_closing_during_launch_hides_and_preserves_game(window, monkeypatch):
    win, app = window
    win.controller._launches.add("alpha")
    shutdown = []
    monkeypatch.setattr(win.controller, "shutdown", lambda **kw: shutdown.append(kw))
    monkeypatch.setattr(win, "_notify_still_running", lambda: None)
    win.show()
    win.close()
    assert not win.isVisible()
    assert win._quit_after_games
    assert not win._closing
    assert shutdown == [{"keep_launches": True}]
    assert win.controller.launching_profiles() == ["alpha"]
    win._maybe_finish_background_close()
    app.processEvents()
    assert win._quit_after_games


def test_completion_while_viewing_another_profile_does_not_finish_its_page(window, monkeypatch):
    win, app = window
    win.config.add_profile("beta", backend="wine")
    win._on_busy("alpha", "install", True)
    win._on_busy("beta", "install", True)
    win.open_game("beta")
    finished = []
    monkeypatch.setattr(win.game, "finish", lambda *a: finished.append(a))
    win._on_succeeded("alpha", "install", 0)
    assert finished == []
    assert win._operations.view("alpha").busy is None
    assert win._operations.view("beta").busy == "install"


def test_paused_view_tracks_controller_shutdown(window):
    win, app = window
    win._on_busy("alpha", "install", True)
    win.controller._paused["alpha"] = ("install", None)
    win._on_paused("alpha", "install")
    win.open_game("alpha")
    assert win.game.primary.text() == "RESUME"
    win.controller.shutdown()
    win.controller.reopen()
    win.open_game("alpha")
    assert win.game.primary.text() != "RESUME"


def test_removed_profile_does_not_retain_operation_replay(window):
    from types import SimpleNamespace

    from nadare.core.events import Event

    win, app = window
    win._on_busy("alpha", "install", True)
    win._on_event("alpha", Event("log", "old profile"))
    win._on_game_removed("alpha", SimpleNamespace(profile_kept=False, warnings=[]))
    assert win._operations.view("alpha").events == ()
