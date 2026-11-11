# SPDX-License-Identifier: GPL-3.0-or-later
"""An empty expected catalog folder is not an installed game."""

from __future__ import annotations

import os

os.environ.setdefault("QT_QPA_PLATFORM", "offscreen")

from nadare.gui.add_game import detect_import


def test_an_empty_catalog_layout_is_not_imported_as_installed(tmp_path):
    prefix = tmp_path / "prefix"
    (prefix / "drive_c" / "YostarGames" / "Arknights_EN").mkdir(parents=True)

    info = detect_import("arknights-yostar", None, str(prefix))

    assert info["install_dir"] == ""
