# SPDX-License-Identifier: GPL-3.0-or-later
"""Round-5 review of the round-4 pause and console fixes (b23).

- A removal that deleted part of a tree was taken for "refused, nothing deleted".
- A Remove that failed as busy dropped RESUME from the page.
- The controller still dropped a paused entry when Update was queued behind a background probe,
  and for Log in / Log out / Remember me, which do not touch the game's files.
- A library added while Find games was busy was never scanned.
- A user's Retry that failed with the same words left no line in OUTPUT.
"""

from __future__ import annotations

import os

os.environ.setdefault("QT_QPA_PLATFORM", "offscreen")

import pytest

try:  # pragma: no cover
    from PyQt6.QtWidgets import QApplication
except Exception:  # noqa: BLE001
    pytest.skip("PyQt6 is not importable in this interpreter", allow_module_level=True)

from nadare.core import removal
from nadare.core.backends import Event
from nadare.core.config import Config
from nadare.gui import main_window
from nadare.gui.game_view import GameView
from nadare.gui.main_window import MainWindow
from nadare.gui.workers import BackendController


@pytest.fixture(scope="module")
def qapp():
    return QApplication.instance() or QApplication([])


@pytest.fixture
def window(tmp_path, qapp, monkeypatch):
    config = Config.load(tmp_path / "config.json")
    config.data["check_updates"] = False
    config.data["check_notices"] = False
    config.add_profile("g", backend="stove-czn", title="G")
    win = MainWindow(config)
    win.controller.start = lambda profile, op, payload=None: True
    shown: list[str] = []
    monkeypatch.setattr(main_window.QMessageBox, "warning", lambda *a, **k: shown.append(a[2]))
    monkeypatch.setattr(main_window.QMessageBox, "critical", lambda *a, **k: shown.append(a[2]))
    win.shown = shown
    yield win
    win.close()
    win.deleteLater()
    qapp.processEvents()


def _paused_page(window):
    window.open_game("g")
    window.controller._paused["g"] = ("install", None)
    window.game.set_paused("install")
    window._busy["g"] = "remove"
    window.game.set_busy("remove")


# ---- 1. what removal reports -----------------------------------------------------------------
def test_removal_marks_a_refusal_before_any_deletion(tmp_path, monkeypatch):
    for key in ("DATA", "CONFIG", "STATE", "CACHE"):
        monkeypatch.setenv(f"XDG_{key}_HOME", str(tmp_path / key.lower()))
    cfg = Config.load(tmp_path / "config.json")
    cfg.add_profile("g", backend="wine", prefix=str(tmp_path / "p"))
    monkeypatch.setattr(removal, "_running_problem", lambda config, name: "G is running")
    result = removal.remove_game(cfg, "g", applications_dir=tmp_path / "apps", save=False)
    assert result.profile_kept and result.refused


def test_a_partial_deletion_is_not_called_nothing_deleted(window):
    _paused_page(window)
    partial = removal.RemovalResult(profile="g", profile_kept=True)
    partial.warnings.append("/games/g/locked could not be removed; remove it by hand")
    window._on_game_removed("g", partial)  # not refused: it deleted what it could
    assert not window.controller.is_paused("g")


def test_an_up_front_refusal_keeps_resume(window):
    _paused_page(window)
    refused = removal.RemovalResult(profile="g", profile_kept=True, refused=True)
    refused.warnings.append("G is running")
    window._on_game_removed("g", refused)
    assert window.controller.is_paused("g")
    assert window.game.primary.text() == "RESUME"


# ---- 2. Remove failed as busy ---------------------------------------------------------------
def test_a_remove_that_fails_as_busy_keeps_resume_on_the_page(window):
    _paused_page(window)
    from nadare.core import process

    window._on_remove_failed("g", process.OPERATION_BUSY.format(name="g"))
    assert window.controller.is_paused("g")
    assert window.game.primary.text() == "RESUME"
    assert not window.game.update_btn.isEnabled()


# ---- 3/4. the controller keeps a paused entry -----------------------------------------------
class _Busy:
    operation = "status"

    def isRunning(self):  # noqa: N802 - Qt name
        return True

    def cancel(self):
        pass


def test_update_is_refused_even_while_a_background_probe_runs(qapp):
    controller = BackendController(None)
    controller._paused["g"] = ("install", None)
    controller._workers["g"] = _Busy()
    assert controller.start("g", "update") is False
    assert controller.paused_operation("g") == "install"


@pytest.mark.parametrize("operation", ["login", "logout", "set_remember"])
def test_sign_in_operations_wait_while_a_download_is_paused(qapp, operation):
    """Round 6: letting them run beside a paused entry broke Cancel and the page state."""
    controller = BackendController(None)
    controller._paused["g"] = ("install", None)
    assert controller.start("g", operation) is False
    assert controller.paused_operation("g") == "install"


def test_log_in_and_out_are_off_while_a_download_is_paused(qapp):
    from nadare.core.backends import Status

    view = GameView()
    try:
        view.set_profile("g", {"backend": "stove-czn", "title": "G"})
        view.set_status(Status("installed", data={"logged_in": True}))
        assert view.login.isEnabled() and view.logout.isEnabled()
        view.set_paused("install")
        assert not view.login.isEnabled() and not view.logout.isEnabled()
        assert "paused" in view.login.toolTip().lower()
    finally:
        view.deleteLater()
        qapp.processEvents()


def test_remember_me_waits_for_a_paused_download(window, monkeypatch):
    started = []
    window.controller.start = lambda profile, op, payload=None: started.append(op) or True
    monkeypatch.setattr(main_window, "supports_remember_me", lambda kind: True)
    window.config.profiles["g"]["remember_me"] = True
    window.controller._paused["g"] = ("install", None)
    window.controller.is_paused = lambda profile: profile == "g"
    window._sync_remember("g")
    assert "set_remember" not in started
    assert "g" in window._remember_pending


def test_install_still_replaces_a_paused_entry(qapp, monkeypatch):  # resume() pops first
    controller = BackendController(None)
    controller._paused["g"] = ("install", None)
    monkeypatch.setattr(controller, "_start_or_wait", lambda *a, **k: True)
    assert controller.start("g", "install") is True
    assert not controller.is_paused("g")


# ---- 6. a user's retry ------------------------------------------------------------------------
def test_a_retry_the_user_asked_for_shows_its_failure_again(qapp):
    view = GameView()
    try:
        view.set_profile("g", {"backend": "stove-czn", "title": "G"})
        view.append_event(Event("error", "offline"))
        view.action_requested.emit("status")  # RETRY
        view.append_event(Event("error", "offline"))
        assert view.console.toPlainText().count("offline") == 2
    finally:
        view.deleteLater()
        qapp.processEvents()


# ---- 5. a library added while Find games is busy -------------------------------------------
def test_a_library_added_during_a_scan_is_scanned_afterwards(tmp_path, qapp, monkeypatch):
    import json
    import threading

    from PyQt6.QtCore import QThreadPool

    from nadare.core import libraries
    from nadare.gui.libraries_panel import LibrariesPanel

    for key in ("DATA", "CONFIG", "STATE", "CACHE"):
        monkeypatch.setenv(f"XDG_{key}_HOME", str(tmp_path / key.lower()))
    home = tmp_path / "home"
    home.mkdir(exist_ok=True)
    monkeypatch.setenv("HOME", str(home))
    cfg = Config.load(tmp_path / "config.json")
    first, second = home / "Slow", home / "Games"
    first.mkdir()
    game = second / libraries.NADARE_DIR / "Brown Dust 2"
    (game / "install").mkdir(parents=True)
    (game / libraries.MANIFEST_NAME).write_text(
        json.dumps(
            {
                "schema": 1,
                "backend": "direct-bd2",
                "title": "Brown Dust 2",
                "install": "install",
                "made_by_nadare": ["install"],
            }
        )
    )
    gate = threading.Event()
    real_scan = libraries.scan

    def scan(config, root):
        if root == first.resolve():
            gate.wait(10)
        return real_scan(config, root)

    monkeypatch.setattr(libraries, "scan", scan)
    asked: list = []
    panel = LibrariesPanel(choose=lambda items: asked.append(items) or [True] * len(items))
    panel.load(cfg)
    panel.add_folder(str(first))
    assert panel.searching
    panel.add_folder(str(second))
    gate.set()
    for _ in range(6):
        QThreadPool.globalInstance().waitForDone(10_000)
        qapp.processEvents()
    assert asked, "the second library's game was never offered"
    assert "bd2" in cfg.profiles
    panel.deleteLater()
    qapp.processEvents()


# ---- round 6: what removal touched, not what it hoped ---------------------------------------
def test_a_removal_that_refused_every_tree_is_a_refusal(tmp_path, monkeypatch):
    for key in ("DATA", "CONFIG", "STATE", "CACHE"):
        monkeypatch.setenv(f"XDG_{key}_HOME", str(tmp_path / key.lower()))
    cfg = Config.load(tmp_path / "config.json")
    cfg.add_profile("g", backend="wine", install_dir=str(tmp_path / "home"))
    monkeypatch.setattr(removal, "_running_problem", lambda config, name: None)
    monkeypatch.setenv("HOME", str(tmp_path / "home"))
    (tmp_path / "home").mkdir(exist_ok=True)
    result = removal.remove_game(
        cfg, "g", delete_trees=["install_dir"], applications_dir=tmp_path / "apps", save=False
    )
    assert result.profile_kept
    assert result.refused  # the only tree was unsafe (home): nothing was touched


def test_a_remove_that_failed_after_deleting_does_not_bring_resume_back(window):
    _paused_page(window)
    window._on_remove_failed("g", "Permission denied: '/home/u/.local/share/applications/x'")
    assert window.game.primary.text() != "RESUME"
