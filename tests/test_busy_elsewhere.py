# SPDX-License-Identifier: GPL-3.0-or-later
"""Another process holding a game's operation lock is shown as busy, not as broken.

User sim r3b05 F2 and r3b07 F2: while `nadare install` (or another window) was installing a
game, `nadare status` said "not installed", `nadare verify` said "integrity check failed ...
Run: nadare install", and the game page offered INSTALL or RESUME. Every one of those advices
ends in "Another Nadare window or command is installing, updating or removing ...".
"""

from __future__ import annotations

import argparse
import os

os.environ.setdefault("QT_QPA_PLATFORM", "offscreen")

import pytest

from nadare import app
from nadare.core import process
from nadare.core.backends import BackendError, Status
from nadare.core.config import Config

INCOMPLETE = Status(
    state="not_installed",
    message="installation incomplete; click RESUME to finish",
    data={"incomplete": True},
)


@pytest.fixture
def env(tmp_path, monkeypatch):
    monkeypatch.setenv("XDG_DATA_HOME", str(tmp_path / "data"))
    monkeypatch.setenv("XDG_CONFIG_HOME", str(tmp_path / "config"))
    monkeypatch.setenv("XDG_STATE_HOME", str(tmp_path / "state"))
    monkeypatch.setenv("XDG_CACHE_HOME", str(tmp_path / "cache"))
    config = Config.load(tmp_path / "config.json")
    config.add_profile("ark", backend="wine", title="Arknights")
    return config


class _Backend:
    verified = False

    def requires_notice_ack(self) -> bool:
        return False

    def status(self) -> Status:
        return INCOMPLETE

    def verify(self):
        _Backend.verified = True
        return {"ok": False, "missing": ["prefix", "launcher"]}


def _run(config, monkeypatch, command):
    monkeypatch.setattr(app, "create_backend", lambda *a, **k: _Backend())
    events = []
    args = argparse.Namespace(command=command)
    code = app._run_backend_command(args, config, "ark", None, events.append)
    return code, events


def test_cli_status_says_another_process_is_working_on_it(env, monkeypatch, capsys):
    with process.OperationLock("ark"):
        code, events = _run(env, monkeypatch, "status")
    out = capsys.readouterr().out
    assert code == 0
    assert "another Nadare window or command is installing, updating or removing it" in out
    # The final line must not send the user to Resume or Install.
    assert "RESUME" not in events[-1].message
    assert events[-1].data.get("busy_elsewhere") is True


def test_cli_status_is_unchanged_without_the_lock(env, monkeypatch, capsys):
    _code, events = _run(env, monkeypatch, "status")
    assert "another Nadare" not in capsys.readouterr().out
    assert events[-1].message == INCOMPLETE.message
    assert "busy_elsewhere" not in events[-1].data


def test_cli_verify_waits_instead_of_recommending_install(env, monkeypatch):
    _Backend.verified = False
    with process.OperationLock("ark"):
        with pytest.raises(BackendError, match="Another Nadare window or command"):
            _run(env, monkeypatch, "verify")
    assert _Backend.verified is False


# ---- game page -------------------------------------------------------------------------

try:
    from PyQt6.QtWidgets import QApplication
except ImportError:  # pragma: no cover
    QApplication = None

needs_qt = pytest.mark.skipif(QApplication is None, reason="PyQt6 is not importable")


@pytest.fixture(scope="module")
def qapp():
    return QApplication.instance() or QApplication([])


@needs_qt
def test_the_game_page_says_busy_and_offers_no_resume(qapp):
    from nadare.gui.game_view import GameView

    view = GameView()
    try:
        view.set_profile("ark", {"title": "Arknights", "backend": "wine"})
        view.set_busy_elsewhere(True)
        view.set_status(INCOMPLETE)
        assert "Another Nadare window or command" in view.detail_label.text()
        assert "RESUME" not in view.detail_label.text()
        assert not view.primary.isEnabled()
        assert not view.verify.isEnabled()
        # Once it is done the page is the normal one again.
        view.set_busy_elsewhere(False)
        assert view.primary.isEnabled()
        assert view.primary.text() == "RESUME"
        # The next game never inherits it.
        view.set_busy_elsewhere(True)
        view.set_profile("bd2", {"title": "Brown Dust 2", "backend": "wine"})
        view.set_status(INCOMPLETE)
        assert view.primary.isEnabled()
    finally:
        view.deleteLater()
        qapp.processEvents()


@needs_qt
def test_the_window_checks_the_lock_on_each_status(qapp, env, monkeypatch):
    from nadare.gui.main_window import MainWindow

    window = MainWindow(env)
    monkeypatch.setattr(window.controller, "start", lambda *a, **k: True)
    try:
        window.open_game("ark")
        with process.OperationLock("ark"):
            window._on_result("ark", "status", INCOMPLETE)
            assert "Another Nadare window or command" in window.game.detail_label.text()
            assert not window.game.primary.isEnabled()
        window._on_result("ark", "status", INCOMPLETE)
        assert window.game.primary.isEnabled()
    finally:
        window.close()
        window.deleteLater()
        qapp.processEvents()


@needs_qt
def test_this_windows_own_predownload_is_not_another_process(qapp, env, monkeypatch):
    from nadare.gui.main_window import MainWindow

    window = MainWindow(env)
    monkeypatch.setattr(window.controller, "start", lambda *a, **k: True)
    monkeypatch.setattr(window.predownloads, "is_busy", lambda profile: profile == "ark")
    try:
        window.open_game("ark")
        with process.OperationLock("ark"):
            window._on_result("ark", "status", INCOMPLETE)
            assert "Another Nadare window or command" not in window.game.detail_label.text()
    finally:
        window.close()
        window.deleteLater()
        qapp.processEvents()
