# SPDX-License-Identifier: GPL-3.0-or-later
"""Add Game never makes a drive root, the home folder or another game's folder the prefix (#851)."""

from __future__ import annotations

import os

os.environ.setdefault("QT_QPA_PLATFORM", "offscreen")

import pytest

from nadare.core.config import Config
from nadare.gui import add_game
from nadare.gui.add_game import AddGameDialog


@pytest.fixture(scope="module")
def qapp():
    from PyQt6.QtWidgets import QApplication

    return QApplication.instance() or QApplication([])


@pytest.fixture
def home(tmp_path, monkeypatch):
    folder = tmp_path / "home" / "user"
    folder.mkdir(parents=True)
    monkeypatch.setenv("HOME", str(folder))
    return folder


def _dialog(tmp_path, qapp):
    instance = AddGameDialog(
        Config.load(tmp_path / "config.json"), data_root=tmp_path / "data", kind="wine"
    )
    instance.show()
    qapp.processEvents()
    instance.location_toggle.click()
    instance.prefix_toggle.click()
    return instance


def _nested(d, parent) -> str:
    title = add_game._folder_name(d._selected_game().title)
    return str(parent / "Nadare" / title / "prefix")


def test_a_drive_root_chosen_as_the_prefix_gets_a_nadare_folder_inside(
    tmp_path, qapp, home, monkeypatch
):
    drive = tmp_path / "drive"
    drive.mkdir()
    real = os.path.ismount
    monkeypatch.setattr(os.path, "ismount", lambda p: str(p) == str(drive.resolve()) or real(p))
    d = _dialog(tmp_path, qapp)
    try:
        d.set_prefix(drive)
        name = d.create_profile()
        assert name
        assert d.config.profile(name)["prefix"] == _nested(d, drive)
        assert str(drive) not in (d.config.profile(name)["prefix"],)
    finally:
        d.close()


@pytest.mark.parametrize("bad", ["home", "Documents"])
def test_home_and_personal_folders_get_a_nadare_folder_inside(tmp_path, qapp, home, bad):
    target = home if bad == "home" else home / bad
    target.mkdir(exist_ok=True)
    d = _dialog(tmp_path, qapp)
    try:
        d.set_prefix(target)
        name = d.create_profile()
        assert d.config.profile(name)["prefix"] == _nested(d, target)
    finally:
        d.close()


def test_another_games_prefix_is_refused_with_its_name(tmp_path, qapp, home):
    other = home / "Games" / "Other" / "prefix"
    other.mkdir(parents=True)
    d = _dialog(tmp_path, qapp)
    try:
        d.config.add_profile("other", backend="wine", title="Other", prefix=str(other))
        d.set_prefix(other)
        assert d.create_profile() is None
        assert "already used by Other" in d.error_label.text()
    finally:
        d.close()


def test_a_normal_custom_prefix_is_kept_exactly(tmp_path, qapp, home):
    custom = home / "Games" / "mine" / "prefix"
    d = _dialog(tmp_path, qapp)
    try:
        d.set_prefix(custom)
        name = d.create_profile()
        assert d.config.profile(name)["prefix"] == str(custom)
    finally:
        d.close()


def test_the_default_location_is_unchanged(tmp_path, qapp, home):
    d = _dialog(tmp_path, qapp)
    try:
        d.prefix_toggle.click()
        name = d.create_profile()
        assert d.config.profile(name)["prefix"] == str(tmp_path / "data" / name / "prefix")
    finally:
        d.close()
