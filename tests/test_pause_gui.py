# SPDX-License-Identifier: GPL-3.0-or-later
"""Pause and resume for downloads (issue #538): the game page and the window."""

from __future__ import annotations

import os

os.environ.setdefault("QT_QPA_PLATFORM", "offscreen")

import pytest

try:  # pragma: no cover - exercised only when PyQt6 is missing
    from PyQt6.QtWidgets import QApplication
except Exception:  # noqa: BLE001
    pytest.skip("PyQt6 is not importable in this interpreter", allow_module_level=True)

from nadare.core.config import Config
from nadare.gui.game_view import GameView
from nadare.gui.main_window import MainWindow


@pytest.fixture(scope="module")
def qapp():
    app = QApplication.instance()
    if app is None:
        app = QApplication([])
    yield app


@pytest.fixture
def view(qapp):
    page = GameView()
    page.set_profile("pgr", {"backend": "direct-pgr", "title": "PGR"})
    yield page
    page.deleteLater()


@pytest.fixture
def window(tmp_path, qapp):
    config = Config.load(tmp_path / "config.json")
    config.add_profile("czn", backend="stove-czn", title="CZN")
    config.add_profile("other", backend="stove-czn", title="Other")
    win = MainWindow(config)
    win.controller.start = lambda profile, op, payload=None: True
    yield win
    win.close()


@pytest.mark.parametrize("operation", ["install", "update", "repair"])
def test_pause_is_offered_while_a_pausable_operation_runs(view, operation):
    view.set_busy(operation)
    assert not view.pause_button.isHidden()
    assert view.pause_button.isEnabled()


@pytest.mark.parametrize("operation", ["launch", "verify", "login", "remove", "status"])
def test_pause_is_not_offered_for_anything_else(view, operation):
    view.set_busy(operation)
    assert view.pause_button.isHidden()


def test_pause_is_hidden_when_idle(view):
    assert view.pause_button.isHidden()
    view.set_busy("install")
    view.finish("install", 0)
    assert view.pause_button.isHidden()


def test_clicking_pause_asks_for_a_pause_not_a_cancel(view):
    seen = []
    view.pause_requested.connect(lambda: seen.append("pause"))
    view.cancel_requested.connect(lambda: seen.append("cancel"))
    view.set_busy("install")
    view.pause_button.click()
    assert seen == ["pause"]


def test_paused_shows_resume_and_hides_pause(view):
    view.set_busy("install")
    view.set_paused("install")
    assert view.primary.text() == "RESUME"
    assert view.primary.isEnabled()
    assert view.pause_button.isHidden()
    assert view.is_paused() is True


def test_clicking_resume_asks_to_resume_and_starts_nothing_else(view):
    seen = []
    view.resume_requested.connect(lambda: seen.append("resume"))
    view.action_requested.connect(lambda op: seen.append(op))
    view.set_busy("update")
    view.set_paused("update")
    view._primary_grace_until = 0.0
    view.primary.click()
    assert seen == ["resume"]


def test_a_new_operation_clears_the_paused_state(view):
    view.set_paused("install")
    view.set_busy("repair")
    assert view.is_paused() is False
    assert view.primary.text() == "CANCEL"


def test_pause_is_not_offered_where_partial_downloads_are_not_kept(qapp):
    page = GameView()
    page.set_profile("czn", {"backend": "stove-czn", "title": "CZN"})
    page.set_busy("install")
    assert page.pause_button.isHidden()
    page.deleteLater()


def test_pause_is_offered_for_stella_sora(qapp):
    page = GameView()
    page.set_profile("ss", {"backend": "stellasora-yostar", "title": "SS"})
    page.set_busy("update")
    assert not page.pause_button.isHidden()
    page.deleteLater()


@pytest.mark.parametrize("ending", ["cancelled", "finish", "fail"])
def test_ending_an_operation_clears_the_paused_state(view, ending, monkeypatch):
    monkeypatch.setattr(view, "show_error", lambda *args, **kwargs: None)
    view.set_paused("install")
    if ending == "cancelled":
        view.cancelled()
    elif ending == "finish":
        view.finish("install", 0)
    else:
        view.fail("boom")
    assert view.is_paused() is False


def test_the_window_forwards_pause_and_resume(window):
    calls = []
    window.controller.pause = lambda profile: calls.append(("pause", profile)) or True
    window.controller.resume = lambda profile: calls.append(("resume", profile)) or True
    window.open_game("czn")
    window.game.pause_requested.emit()
    window.game.resume_requested.emit()
    assert calls == [("pause", "czn"), ("resume", "czn")]


def test_a_paused_signal_for_the_current_game_shows_resume(window):
    window.open_game("czn")
    window._on_busy("czn", "install", True)
    window.controller.paused.emit("czn", "install")
    assert window.game.primary.text() == "RESUME"
    assert "czn" not in window._busy


def test_a_paused_game_shows_resume_when_you_come_back_to_it(window):
    window.open_game("other")
    window._on_busy("czn", "install", True)
    window.controller.paused.emit("czn", "install")
    window.controller._paused["czn"] = ("install", None)
    window.open_game("czn")
    assert window.game.primary.text() == "RESUME"


def test_a_paused_signal_for_another_game_does_not_touch_this_page(window):
    window.open_game("czn")
    window.controller.paused.emit("other", "install")
    assert window.game.is_paused() is False


def test_pausing_does_not_count_as_a_running_install_when_quitting(window):
    window.controller._paused["czn"] = ("install", None)
    assert window.controller.mutating_profiles() == []


@pytest.mark.parametrize("operation", ["status", "news", "media", "check_update"])
def test_a_background_refresh_finishing_keeps_the_page_paused(view, operation):
    view.set_paused("install")
    view.finish(operation, 0)
    assert view.is_paused() is True
    assert view.primary.text() == "RESUME"


def test_switching_games_clears_the_paused_state(view):
    view.set_paused("install")
    view.set_profile("bd2", {"backend": "direct-bd2", "title": "BD2"})
    assert view.is_paused() is False
    assert view.primary.text() != "RESUME"


def test_a_status_refresh_succeeding_keeps_resume_on_the_window(window):
    window.open_game("czn")
    window.controller._paused["czn"] = ("install", None)
    window.game.set_paused("install")
    window._on_succeeded("czn", "status", 0)
    assert window.game.primary.text() == "RESUME"


def test_the_pause_button_is_an_icon_not_a_word(view):
    """A symbol everyone reads, in any language; the name stays for tooltips and screen readers."""
    button = view.pause_button
    assert button.text() == ""
    assert not button.icon().isNull()
    assert button.toolTip()
    assert button.accessibleName() == "Pause"


def test_removing_a_game_forgets_its_paused_operation(window):
    from types import SimpleNamespace

    window.controller._paused["other"] = ("install", None)
    assert window.controller.is_paused("other")
    window._on_game_removed("other", SimpleNamespace(profile_kept=False, warnings=[]))
    assert not window.controller.is_paused("other")


def test_a_paused_download_can_be_cancelled_from_its_page(view):
    seen = []
    view.cancel_requested.connect(lambda: seen.append("cancel"))
    view.resume_requested.connect(lambda: seen.append("resume"))
    view.set_busy("install")
    view.set_paused("install")
    assert not view.cancel_paused_button.isHidden()
    view.cancel_paused_button.click()
    assert seen == ["cancel"]


@pytest.mark.parametrize("operation", ["install", "update"])
def test_cancel_paused_is_only_offered_while_paused(view, operation):
    assert view.cancel_paused_button.isHidden()
    view.set_busy(operation)
    assert view.cancel_paused_button.isHidden()  # running: the primary already reads CANCEL
    view.set_paused(operation)
    assert not view.cancel_paused_button.isHidden()
    view.cancelled()
    assert view.cancel_paused_button.isHidden()


def test_the_button_that_ends_a_paused_download_says_the_files_stay(view):
    view.set_busy("install")
    view.set_paused("install")
    assert view.cancel_paused_button.text() == "Stop download"
    assert "stays on disk" in view.cancel_paused_button.toolTip()


def _no_boxes(monkeypatch) -> list[str]:
    from nadare.gui import main_window

    shown: list[str] = []
    monkeypatch.setattr(
        main_window.QMessageBox, "warning", lambda *a, **k: shown.append(str(a[2]))
    )
    return shown


def test_a_refused_removal_keeps_the_paused_download(window, monkeypatch):
    """Remove refused before anything was deleted (game running, profile locked): RESUME stays."""
    from nadare.core.removal import RemovalResult

    shown = _no_boxes(monkeypatch)
    window.open_game("other")
    window.controller._paused["other"] = ("install", None)
    window.game.set_paused("install")
    window._busy["other"] = "remove"
    window.game.set_busy("remove")
    refused = RemovalResult(profile="other", profile_kept=True, refused=True)
    refused.warnings.append("Other is running. Stop it first, then remove it.")
    window._on_game_removed("other", refused)
    assert window.controller.is_paused("other")
    assert "other" in window.config.profiles
    assert "other" not in window._busy
    assert window._current_profile == "other"  # still on the game page, not the Library
    assert window.game.primary.text() == "RESUME"
    assert shown and "running" in shown[0]


def test_a_removal_that_deleted_files_still_forgets_the_pause(window, tmp_path, monkeypatch):
    """Some files went before the refusal: nothing may resume into a half-deleted folder."""
    from nadare.core.removal import RemovalResult

    _no_boxes(monkeypatch)
    window.controller._paused["other"] = ("install", None)
    partial = RemovalResult(profile="other", profile_kept=True)
    partial.trees_removed.append(tmp_path / "prefix")
    partial.warnings.append("not deleting install_dir: outside")
    window._on_game_removed("other", partial)
    assert not window.controller.is_paused("other")
    assert "other" in window.config.profiles
