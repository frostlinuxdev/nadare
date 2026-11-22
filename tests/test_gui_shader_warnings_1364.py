# SPDX-License-Identifier: GPL-3.0-or-later
# Copyright (C) 2026 Nadare contributors
"""#1364: the Disk space card's shader cache note and warning lines.

The "Shader caches" heading always explains that the caches are safe to delete.
A total warning sits under the heading; a game over 1 GiB carries its warning on
its own rows.
"""

from __future__ import annotations

import os
from pathlib import Path

os.environ.setdefault("QT_QPA_PLATFORM", "offscreen")

import pytest

try:  # pragma: no cover - exercised only when PyQt6 is missing
    from PyQt6.QtWidgets import QApplication
except Exception:  # noqa: BLE001
    pytest.skip("PyQt6 is not importable in this interpreter", allow_module_level=True)

from nadare.core import storagecard
from nadare.gui.disk_space_panel import DiskSpacePanel

GAME_TEXT = "Large: over 1 GB. Clearing is safe; the game may stutter while it rebuilds."
NOTE_TEXT = "Safe to delete. Games rebuild them, and may stutter for a while after."


@pytest.fixture(scope="module")
def qapp():
    app = QApplication.instance()
    if app is None:
        app = QApplication([])
    yield app


def _row(tmp_path: Path, *, warning: str = "") -> storagecard.ShaderCache:
    return storagecard.ShaderCache(
        key="vkd3d:g",
        label="VKD3D · Game",
        profile="g",
        folders=(tmp_path / "vkd3d",),
        size=2 * 1024**3,
        running=False,
        warning=warning,
    )


def test_the_heading_always_carries_the_safe_to_delete_note(qapp, tmp_path):
    panel = DiskSpacePanel()
    try:
        panel._fill_report(storagecard.Report(shaders=(_row(tmp_path),)))
        assert panel.shader_note.text() == NOTE_TEXT
        assert panel.shader_note.isVisibleTo(panel)
    finally:
        panel.deleteLater()


def test_a_total_warning_is_shown_under_the_heading(qapp, tmp_path):
    panel = DiskSpacePanel()
    warning = "Large: over 8 GiB of shader caches. Clearing is safe."
    try:
        panel._fill_report(
            storagecard.Report(shaders=(_row(tmp_path),), shader_warning=warning)
        )
        assert panel.shader_warning.text() == warning
        assert panel.shader_warning.isVisibleTo(panel)
    finally:
        panel.deleteLater()


def test_no_total_warning_leaves_that_line_hidden(qapp, tmp_path):
    panel = DiskSpacePanel()
    try:
        panel._fill_report(storagecard.Report(shaders=(_row(tmp_path),)))
        assert panel.shader_warning.text() == ""
        assert not panel.shader_warning.isVisibleTo(panel)
    finally:
        panel.deleteLater()


def test_a_large_game_carries_its_warning_on_its_row(qapp, tmp_path):
    panel = DiskSpacePanel()
    try:
        panel._fill_report(storagecard.Report(shaders=(_row(tmp_path, warning=GAME_TEXT),)))
        assert GAME_TEXT in panel.shader_rows[0].label.text()
    finally:
        panel.deleteLater()


def test_an_unmeasured_report_hides_the_total_warning(qapp, tmp_path):
    panel = DiskSpacePanel()
    try:
        panel._fill_report(
            storagecard.Report(shaders=(_row(tmp_path),), shader_warning="Large: over 8 GiB.")
        )
        (tmp_path / "vkd3d").mkdir()
        assert panel.shader_warning.isVisibleTo(panel)

        panel._fill_report(None)

        assert panel.shader_warning.text() == ""
        assert not panel.shader_warning.isVisibleTo(panel)
    finally:
        panel.deleteLater()
