# SPDX-License-Identifier: GPL-3.0-or-later
"""A typed import prefix must be an existing folder, not a typo or a file."""

from __future__ import annotations

import os

os.environ.setdefault("QT_QPA_PLATFORM", "offscreen")

from nadare.gui.add_game import detect_import


def test_a_prefix_that_is_not_an_existing_folder_is_not_adopted(tmp_path):
    not_a_folder = tmp_path / "not-a-folder"
    not_a_folder.write_text("x", encoding="utf-8")
    missing = tmp_path / "missing"

    for candidate in (not_a_folder, missing):
        info = detect_import("arknights-yostar", None, str(candidate))
        assert info["prefix"] == "", candidate
        assert info["notes"] == [], candidate
