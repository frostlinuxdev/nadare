# SPDX-License-Identifier: GPL-3.0-or-later
"""#1207 b185: repro from the final b23 round (failed on b23 41e118a), kept as a test."""

from __future__ import annotations

import os

import pytest
from PyQt6.QtWidgets import QApplication

from nadare.gui.logs_dialog import LogsDialog


@pytest.fixture(scope="module")
def qapp():
    yield QApplication.instance() or QApplication([])


def test_a_rotated_log_that_is_larger_is_read_again(qapp, tmp_path):
    path = tmp_path / "G-launch.log"
    path.write_text("first\n", encoding="utf-8")
    dlg = LogsDialog(["G"], profile="G", operation="launch", directory=tmp_path, follow=False)
    assert "first" in dlg.view.toPlainText()

    replacement = tmp_path / "replacement.tmp"
    replacement.write_text("a much longer replacement line\n", encoding="utf-8")
    stamp = os.stat(path).st_mtime_ns + 1_000_000
    os.utime(replacement, ns=(stamp, stamp))
    os.replace(replacement, path)  # rotation: same name, new inode, larger file

    dlg._tick()

    shown = dlg.view.toPlainText()
    assert "first" not in shown, "old text kept and the rotated file's tail appended"
    assert shown.strip() == "a much longer replacement line"
