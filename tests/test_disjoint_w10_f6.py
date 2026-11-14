# SPDX-License-Identifier: GPL-3.0-or-later
"""W10-F6: Add Game and Move disagree about a dangling symlink being a free folder."""
from __future__ import annotations

import os

os.environ.setdefault("QT_QPA_PLATFORM", "offscreen")

import pytest

try:
    from PyQt6.QtWidgets import QApplication
except Exception:  # noqa: BLE001
    pytest.skip("PyQt6 is not importable", allow_module_level=True)

from nadare.core import relink
from nadare.core.config import Config
from nadare.gui.add_game import AddGameDialog


@pytest.fixture(scope="module")
def qapp():
    return QApplication.instance() or QApplication([])


def test_add_game_skips_a_dangling_symlink_as_move_does(tmp_path, qapp):
    library = tmp_path / "Games"
    (library / "Nadare").mkdir(parents=True)
    link = library / "Nadare" / "Brown Dust 2"
    link.symlink_to(tmp_path / "not-mounted")
    assert not link.exists() and link.is_symlink()

    # Both rules asked about the same disk state (before Add Game creates its folder).
    moved_to = relink.free_home(library, "Brown Dust 2", {})
    config = Config.load(tmp_path / "config.json")
    dialog = AddGameDialog(config, data_root=tmp_path / "data")
    try:
        dialog.set_backend("direct-bd2")
        dialog.set_install_dir(library)
        created = dialog.create_profile()
        assert created, dialog.error_label.text()
        chosen = config.profile(created)["owned_dirs"][0]
    finally:
        dialog.deleteLater()
        qapp.processEvents()

    print("Add Game chose:", chosen)
    print("Move would choose:", moved_to)
    assert chosen == str(moved_to)
