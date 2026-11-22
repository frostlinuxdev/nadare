# SPDX-License-Identifier: GPL-3.0-or-later
"""A Settings save never brings back a game another process removed (user sim r3b05 F1).

Steps that showed it on 2026-11-21: open a game's Settings, change its name, run
``nadare remove -p <game> --yes`` in a terminal, then press Save changes. The profile came back
under the new name: the three-way merge keeps a profile "changed here" even when the disk no
longer has it. A save of one game's settings now refuses when that game was on disk when this
window loaded it and is gone now, and says so.
"""

from __future__ import annotations

import os

os.environ.setdefault("QT_QPA_PLATFORM", "offscreen")

import pytest

from nadare.core.config import Config, ProfileRemovedElsewhere


def _two_windows(tmp_path):
    path = tmp_path / "config.json"
    first = Config.load(path)
    first.add_profile("ag", backend="wine", title="Aether Gazer")
    first.save()
    gui = Config.load(path)  # the window, loaded before the removal
    cli = Config.load(path)
    cli.remove_profile("ag")
    cli.save()
    return path, gui


def test_editing_a_profile_removed_elsewhere_is_refused(tmp_path):
    path, gui = _two_windows(tmp_path)
    with pytest.raises(ProfileRemovedElsewhere):
        with gui.edit_settings("ag") as draft:
            draft.profile["title"] = "Concurrency rename"
    assert "ag" not in Config.load(path).profiles
    # The live data is untouched, as for any failed edit.
    assert gui.profiles["ag"]["title"] == "Aether Gazer"


def test_editing_a_profile_still_on_disk_saves(tmp_path):
    path = tmp_path / "config.json"
    first = Config.load(path)
    first.add_profile("ag", backend="wine", title="Aether Gazer")
    first.add_profile("bd2", backend="wine", title="Brown Dust 2")
    first.save()
    gui = Config.load(path)
    cli = Config.load(path)
    cli.remove_profile("bd2")  # another game going away does not block this one
    cli.save()
    with gui.edit_settings("ag") as draft:
        draft.profile["title"] = "Renamed"
    on_disk = Config.load(path).profiles
    assert on_disk["ag"]["title"] == "Renamed"
    assert "bd2" not in on_disk


def test_app_settings_save_is_not_blocked_by_a_removed_game(tmp_path):
    path, gui = _two_windows(tmp_path)
    with gui.edit_settings() as draft:
        draft.settings["check_updates"] = False
    saved = Config.load(path)
    assert saved.data["check_updates"] is False
    assert "ag" not in saved.profiles


def test_a_later_plain_save_is_not_blocked(tmp_path):
    """The refusal belongs to that one edit; it must not stick to the Config."""
    path, gui = _two_windows(tmp_path)
    with pytest.raises(ProfileRemovedElsewhere):
        with gui.edit_settings("ag") as draft:
            draft.profile["title"] = "x"
    gui.data["check_updates"] = False
    gui.save()
    assert Config.load(path).data["check_updates"] is False


# ---- Settings page ------------------------------------------------------------------------

try:
    from PyQt6.QtWidgets import QApplication, QMessageBox
except ImportError:  # pragma: no cover
    QApplication = None

needs_qt = pytest.mark.skipif(QApplication is None, reason="PyQt6 is not importable")


@pytest.fixture(scope="module")
def qapp():
    return QApplication.instance() or QApplication([])


@needs_qt
def test_the_settings_page_says_so_and_does_not_resurrect(tmp_path, qapp, monkeypatch):
    from nadare.gui.settings import SettingsPage

    path, gui = _two_windows(tmp_path)
    told: list[str] = []
    monkeypatch.setattr(
        QMessageBox, "information", staticmethod(lambda _p, _t, text, *a, **k: told.append(text))
    )
    monkeypatch.setattr(
        QMessageBox, "critical", staticmethod(lambda _p, _t, text, *a, **k: told.append(text))
    )
    page = SettingsPage()
    cancelled: list[bool] = []
    saved: list[str] = []
    page.cancelled.connect(lambda: cancelled.append(True))
    page.saved.connect(saved.append)
    try:
        page.load(gui, "ag")
        page.title.setText("Concurrency rename")
        page.save()
        assert "ag" not in Config.load(path).profiles
        assert saved == []
        assert cancelled == [True]
        assert told and "removed" in told[0].lower()
    finally:
        page.deleteLater()
        qapp.processEvents()
