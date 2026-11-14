# SPDX-License-Identifier: GPL-3.0-or-later
"""W1-2: a default-library profile's folder and manifest follow the profile key."""

from __future__ import annotations

import os
from pathlib import Path

os.environ.setdefault("QT_QPA_PLATFORM", "offscreen")

import pytest
from PyQt6.QtWidgets import QApplication

from nadare.core import gamefolders, libraries, paths
from nadare.core.config import Config
from nadare.gui.add_game import AddGameDialog


@pytest.fixture(scope="module")
def qapp():
    return QApplication.instance() or QApplication([])


def test_a_chosen_profile_name_is_the_default_library_folder(tmp_path, qapp, monkeypatch):
    root = tmp_path / "profiles"
    monkeypatch.setattr(paths, "profiles_dir", lambda: root)
    config = Config.load(tmp_path / "config.json")
    dialog = AddGameDialog(config, data_root=root)
    try:
        dialog.set_backend("direct-bd2")
        dialog.set_profile_name("my-bd2")
        name = dialog.create_profile()
        assert name == "my-bd2", name
        profile = config.profiles[name]
        # One layout: <library>/<game folder>/install (never a folder inside the game folder).
        home = Path(profile["install_dir"]).parent
        want = (root / "my-bd2").resolve()
        assert home.resolve() == want, (home, want, profile.get("install_dir"))
        manifest = root / "my-bd2" / libraries.MANIFEST_NAME
        assert manifest.is_file(), (manifest, sorted(p.name for p in root.iterdir()))
        place = gamefolders.placement(config, name)
        assert place.folder == want, (place.folder, want)
    finally:
        dialog.deleteLater()
        qapp.processEvents()
