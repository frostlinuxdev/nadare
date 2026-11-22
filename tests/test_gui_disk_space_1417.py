# SPDX-License-Identifier: GPL-3.0-or-later
"""#1417 phases 2c and 2e: the Disk space card in Settings (all games).

Unused runners are counted there (the launch prompt was the only place before), and the
folders of removed games are listed for review, never deleted without a tick and a Yes.
"""

from __future__ import annotations

import os
import time
from pathlib import Path

os.environ.setdefault("QT_QPA_PLATFORM", "offscreen")

import pytest

try:  # pragma: no cover - exercised only when PyQt6 is missing
    from PyQt6.QtWidgets import QApplication
except Exception:  # noqa: BLE001
    pytest.skip("PyQt6 is not importable in this interpreter", allow_module_level=True)

from nadare.core import paths, runners
from nadare.core.config import Config
from nadare.gui import disk_space_panel
from nadare.gui.disk_space_panel import DiskSpacePanel, ProfileFoldersDialog
from nadare.gui.settings import SettingsPage


@pytest.fixture(scope="module")
def qapp():
    app = QApplication.instance()
    if app is None:
        app = QApplication([])
    yield app


@pytest.fixture
def runner_root(tmp_path, monkeypatch):
    folder = tmp_path / "runners"
    folder.mkdir()
    monkeypatch.setattr(runners.paths, "runners_dir", lambda: folder)
    monkeypatch.setattr(runners, "steam_runners", lambda: [])
    return folder


@pytest.fixture
def profiles_root() -> Path:
    folder = paths.profiles_dir()
    folder.mkdir(parents=True, exist_ok=True)
    return folder


def _runner(root: Path, name: str, payload: int) -> Path:
    d = root / name
    d.mkdir()
    (d / "proton").write_text("#!/bin/sh\n")
    (d / "blob").write_bytes(b"x" * payload)
    return d


def _orphan(root: Path, name: str, payload: int) -> Path:
    d = root / name / "prefix" / "drive_c"
    d.mkdir(parents=True)
    (d / "save.dat").write_bytes(b"s" * payload)
    return root / name


def _wait(qapp, predicate, seconds=15.0):
    end = time.time() + seconds
    while time.time() < end:
        qapp.processEvents()
        if predicate():
            return True
        time.sleep(0.02)
    return False


def _config(tmp_path) -> Config:
    return Config.load(tmp_path / "config.json")


# ---- the card ---------------------------------------------------------------------------------
def test_the_card_counts_unused_runners_but_never_the_pin_or_one_a_game_names(
    qapp, tmp_path, runner_root, profiles_root
):
    _runner(runner_root, "dwproton-11.0-17-x86_64", 5000)  # the pin
    _runner(runner_root, "dwproton-11.0-13-x86_64", 3000)  # unused
    _runner(runner_root, "GE-Proton10-1", 2000)  # unused (GE-Proton11-7 is GE's pin)
    named = _runner(runner_root, "dwproton-11.0-15-x86_64", 7000)  # a game names it
    config = _config(tmp_path)
    config.add_profile("g", backend="wine", runner=str(named))
    panel = DiskSpacePanel()
    try:
        panel.load(config)
        panel.refresh()
        assert _wait(qapp, lambda: panel.runners_found is not None)
        assert sorted(p.name for p, _s in panel.runners_found) == [
            "GE-Proton10-1",
            "dwproton-11.0-13-x86_64",
        ]
        assert panel.runners_label.text().startswith("Unused runners · ")
        assert disk_space_panel.diskspace.human_size(3010 + 2010) in panel.runners_label.text()
    finally:
        panel.deleteLater()


def test_the_card_says_when_no_runner_is_unused(qapp, tmp_path, runner_root, profiles_root):
    _runner(runner_root, "dwproton-11.0-17-x86_64", 100)
    panel = DiskSpacePanel()
    try:
        panel.load(_config(tmp_path))
        panel.refresh()
        assert _wait(qapp, lambda: panel.runners_found is not None)
        assert panel.runners_found == []
        assert panel.runners_label.text() == "Unused runners · 0 B"
    finally:
        panel.deleteLater()


def test_the_card_lists_folders_of_removed_games(qapp, tmp_path, runner_root, profiles_root):
    _orphan(profiles_root, "gone", 400)
    _orphan(profiles_root, "kept", 100)
    config = _config(tmp_path)
    config.add_profile("kept", backend="wine")
    panel = DiskSpacePanel()
    try:
        panel.load(config)
        panel.refresh()
        assert _wait(qapp, lambda: panel.folders_found is not None)
        assert [f.path.name for f in panel.folders_found] == ["gone"]
        assert panel.folders_label.text().startswith("Folders of removed games · ")
        assert panel.review_button.isEnabled()
    finally:
        panel.deleteLater()


def test_review_is_off_when_no_folder_is_left(qapp, tmp_path, runner_root, profiles_root):
    panel = DiskSpacePanel()
    try:
        panel.load(_config(tmp_path))
        panel.refresh()
        assert _wait(qapp, lambda: panel.folders_found is not None)
        assert not panel.review_button.isEnabled()
    finally:
        panel.deleteLater()


def test_a_recovered_settings_file_checks_nothing(qapp, tmp_path, runner_root, profiles_root):
    # A recovered config has no trustworthy list of games: every folder would look orphaned.
    _orphan(profiles_root, "czn", 100)
    config = _config(tmp_path)
    config.notice = "config was damaged and recovered"
    panel = DiskSpacePanel()
    try:
        panel.load(config)
        panel.refresh()
        qapp.processEvents()
        assert panel.folders_found is None and panel.runners_found is None
        assert not panel.review_button.isEnabled()
        assert "recovered" in panel.folders_label.text()
    finally:
        panel.deleteLater()


# ---- the review dialog ------------------------------------------------------------------------
def _dialog(config: Config, folders) -> ProfileFoldersDialog:
    return ProfileFoldersDialog(config, folders)


def test_nothing_is_ticked_and_delete_is_off_at_first(qapp, tmp_path, profiles_root):
    _orphan(profiles_root, "gone", 10)
    config = _config(tmp_path)
    dialog = _dialog(config, disk_space_panel.profilefolders.find({}, []))
    try:
        assert [box.isChecked() for box in dialog.boxes] == [False]
        assert not dialog.delete_button.isEnabled()
        assert "saves" in dialog.note.text()
        dialog.boxes[0].setChecked(True)
        assert dialog.delete_button.isEnabled()
    finally:
        dialog.deleteLater()


def test_delete_removes_only_the_ticked_folders_after_a_yes(
    qapp, tmp_path, profiles_root, monkeypatch
):
    a = _orphan(profiles_root, "a", 10)
    b = _orphan(profiles_root, "b", 10)
    config = _config(tmp_path)
    asked = []
    monkeypatch.setattr(
        ProfileFoldersDialog, "_confirm", lambda self, items: asked.append(items) or True
    )
    dialog = _dialog(config, disk_space_panel.profilefolders.find({}, []))
    try:
        dialog.boxes[0].setChecked(True)  # "a"
        dialog._delete_ticked()
        assert _wait(qapp, lambda: dialog.finished_deleting)
        assert len(asked) == 1 and [f.path.name for f in asked[0]] == ["a"]
        assert not a.exists()
        assert b.exists()
    finally:
        dialog.deleteLater()


def test_no_keeps_every_folder(qapp, tmp_path, profiles_root, monkeypatch):
    a = _orphan(profiles_root, "a", 10)
    config = _config(tmp_path)
    monkeypatch.setattr(ProfileFoldersDialog, "_confirm", lambda self, items: False)
    dialog = _dialog(config, disk_space_panel.profilefolders.find({}, []))
    try:
        dialog.boxes[0].setChecked(True)
        dialog._delete_ticked()
        qapp.processEvents()
        assert not dialog.finished_deleting
        assert a.exists()
    finally:
        dialog.deleteLater()


def test_a_game_added_after_the_list_was_shown_keeps_its_folder(
    qapp, tmp_path, profiles_root, monkeypatch
):
    a = _orphan(profiles_root, "a", 10)
    config = _config(tmp_path)
    monkeypatch.setattr(ProfileFoldersDialog, "_confirm", lambda self, items: True)
    dialog = _dialog(config, disk_space_panel.profilefolders.find({}, []))
    try:
        config.add_profile("a", backend="wine")  # the game came back meanwhile
        dialog.boxes[0].setChecked(True)
        dialog._delete_ticked()
        assert _wait(qapp, lambda: dialog.finished_deleting)
        assert a.exists()
        assert "kept" in dialog.status.text()
    finally:
        dialog.deleteLater()


# ---- Settings ---------------------------------------------------------------------------------
def test_settings_all_games_tab_has_the_card_and_measures_it(
    qapp, tmp_path, runner_root, profiles_root
):
    _orphan(profiles_root, "gone", 10)
    page = SettingsPage()
    try:
        page.load_app_only(_config(tmp_path))
        assert _wait(qapp, lambda: page.disk_space_panel.folders_found is not None)
        assert [f.path.name for f in page.disk_space_panel.folders_found] == ["gone"]
    finally:
        page.deleteLater()


def test_a_games_settings_do_not_measure_until_the_all_games_tab_opens(
    qapp, tmp_path, runner_root, profiles_root
):
    config = _config(tmp_path)
    config.add_profile("g", backend="wine")
    page = SettingsPage()
    try:
        page.load(config, "g")
        qapp.processEvents()
        assert page.disk_space_panel.folders_found is None
        page.tabs.setCurrentIndex(page._app_tab)
        assert _wait(qapp, lambda: page.disk_space_panel.folders_found is not None)
    finally:
        page.deleteLater()
