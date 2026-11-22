# SPDX-License-Identifier: GPL-3.0-or-later
"""An Uninstall that a crash interrupted says so (user sim r3b01 F1, 2026-11-21).

Uninstall records the folders it is deleting (``removal_pending``) before the first one goes.
After a kill -9 mid-delete and a restart, the game read NOT INSTALLED / INSTALL with 3.7 GB still
on disk and nothing saying the Uninstall had not finished. The record was only shown in the
support report. The game page and ``nadare status`` now name the folders still there and where
to finish the job.
"""

from __future__ import annotations

import argparse
import os

os.environ.setdefault("QT_QPA_PLATFORM", "offscreen")

import pytest

from nadare import app
from nadare.core import pending
from nadare.core.backends import Status
from nadare.core.config import Config

NOT_INSTALLED = Status(state="not_installed", message="Arknights is not installed yet")


def test_removal_leftovers_lists_only_recorded_folders_still_on_disk(tmp_path):
    there = tmp_path / "install"
    there.mkdir()
    profile = {
        "removal_pending": {
            "install_dir": str(there),
            "prefix": str(tmp_path / "prefix-gone"),
        }
    }
    assert pending.removal_leftovers(profile) == [str(there)]


@pytest.mark.parametrize(
    "record",
    [None, {}, "x", ["a"], {"install_dir": ""}, {"install_dir": 3}, {"k": "a\x00b"}],
)
def test_removal_leftovers_ignores_missing_or_damaged_records(record):
    profile = {} if record is None else {"removal_pending": record}
    assert pending.removal_leftovers(profile) == []


def test_removal_leftovers_does_not_change_the_profile(tmp_path):
    (tmp_path / "install").mkdir()
    profile = {"removal_pending": {"install_dir": str(tmp_path / "install")}}
    before = repr(profile)
    pending.removal_leftovers(profile)
    assert repr(profile) == before


class _FakeBackend:
    def status(self):
        return NOT_INSTALLED


def _status_cli(tmp_path, monkeypatch, capsys, profile_extra):
    config = Config.load(tmp_path / "config.json")
    config.add_profile("ark", backend="direct", title="Arknights")
    config.profiles["ark"].update(profile_extra)
    monkeypatch.setattr(app, "create_backend", lambda *a, **k: _FakeBackend())
    args = argparse.Namespace(command="status")
    code = app._run_backend_command(args, config, "ark", None, lambda event: None)
    return code, capsys.readouterr().out


def test_cli_status_names_an_unfinished_uninstall(tmp_path, monkeypatch, capsys):
    left = tmp_path / "install"
    left.mkdir()
    code, out = _status_cli(
        tmp_path, monkeypatch, capsys, {"removal_pending": {"install_dir": str(left)}}
    )
    assert code == 0
    assert "ark: not installed" in out
    assert "uninstall did not finish" in out.lower()
    assert str(left) in out


def test_cli_status_is_quiet_without_a_record(tmp_path, monkeypatch, capsys):
    _code, out = _status_cli(tmp_path, monkeypatch, capsys, {})
    assert "uninstall" not in out.lower()


# ---- game page -----------------------------------------------------------------------------

try:
    from PyQt6.QtWidgets import QApplication
except ImportError:  # pragma: no cover
    QApplication = None

needs_qt = pytest.mark.skipif(QApplication is None, reason="PyQt6 is not importable")


@pytest.fixture(scope="module")
def qapp():
    return QApplication.instance() or QApplication([])


@needs_qt
def test_the_game_page_says_the_uninstall_did_not_finish(qapp, tmp_path):
    from nadare.gui.game_view import GameView

    view = GameView()
    try:
        view.set_profile("ark", {"title": "Arknights"})
        view.set_status(NOT_INSTALLED)
        view.set_unfinished_removal([str(tmp_path / "install")])
        text = view.detail_label.text()
        assert "Uninstall did not finish" in text
        assert str(tmp_path / "install") in text
        assert "Uninstall or remove" in text
        # The next game never inherits the notice.
        view.set_profile("bd2", {"title": "Brown Dust 2"})
        view.set_status(NOT_INSTALLED)
        assert "Uninstall did not finish" not in view.detail_label.text()
    finally:
        view.deleteLater()
        qapp.processEvents()


@needs_qt
def test_the_window_passes_the_record_to_the_game_page(qapp, tmp_path, monkeypatch):
    from nadare.gui.main_window import MainWindow

    left = tmp_path / "install"
    left.mkdir()
    config = Config.load(tmp_path / "config.json")
    config.add_profile("alpha", backend="wine", title="Alpha")
    config.profiles["alpha"]["removal_pending"] = {"install_dir": str(left)}
    window = MainWindow(config)
    monkeypatch.setattr(window.controller, "start", lambda *a, **k: True)
    try:
        window.open_game("alpha")
        window._on_result("alpha", "status", NOT_INSTALLED)
        assert "Uninstall did not finish" in window.game.detail_label.text()
        assert str(left) in window.game.detail_label.text()
    finally:
        window.close()
        window.deleteLater()
        qapp.processEvents()


def test_cli_status_says_nothing_once_the_game_is_installed_again(tmp_path, monkeypatch, capsys):
    """A reinstall into the same folder leaves the old record; an installed game is not
    an unfinished Uninstall."""
    left = tmp_path / "install"
    left.mkdir()
    monkeypatch.setattr(_FakeBackend, "status", lambda self: Status(state="installed"))
    _code, out = _status_cli(
        tmp_path, monkeypatch, capsys, {"removal_pending": {"install_dir": str(left)}}
    )
    assert "uninstall" not in out.lower()


@needs_qt
def test_the_game_page_drops_the_notice_for_an_installed_game(qapp, tmp_path):
    from nadare.gui.game_view import GameView

    view = GameView()
    try:
        view.set_profile("ark", {"title": "Arknights"})
        view.set_unfinished_removal([str(tmp_path / "install")])
        view.set_status(Status(state="installed"))
        assert "Uninstall did not finish" not in view.detail_label.text()
    finally:
        view.deleteLater()
        qapp.processEvents()
