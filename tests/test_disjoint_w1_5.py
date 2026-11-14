# SPDX-License-Identifier: GPL-3.0-or-later
"""W1-5: a fresh NIKKE profile must not be born with a stray (ignored) install folder."""

from __future__ import annotations

import os

os.environ.setdefault("QT_QPA_PLATFORM", "offscreen")

import pytest
from PyQt6.QtWidgets import QApplication

from nadare.core import gamefolders, paths
from nadare.core.config import Config
from nadare.gui.add_game import AddGameDialog


@pytest.fixture(scope="module")
def qapp():
    return QApplication.instance() or QApplication([])


def test_a_fresh_nikke_profile_has_no_stray_folder(tmp_path, qapp, monkeypatch):
    root = tmp_path / "profiles"
    monkeypatch.setattr(paths, "profiles_dir", lambda: root)
    config = Config.load(tmp_path / "config.json")
    dialog = AddGameDialog(config, data_root=root, kind="launcher-nikke")
    try:
        name = dialog.create_profile()
        assert name == "nikke", name
        place = gamefolders.placement(config, name)
        assert place is not None and place.stray == {}, (config.profiles[name], place)
    finally:
        dialog.deleteLater()
        qapp.processEvents()
