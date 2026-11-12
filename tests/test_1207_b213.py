# SPDX-License-Identifier: GPL-3.0-or-later
"""A failed refresh after a quality change must not keep the previous quality's numbers."""
from __future__ import annotations

import os
from types import SimpleNamespace

os.environ.setdefault("QT_QPA_PLATFORM", "offscreen")

import pytest

pytest.importorskip("PyQt6.QtWidgets")
from PyQt6.QtWidgets import QApplication

from nadare.gui.quick_install import QuickInstallDialog


@pytest.fixture(scope="module")
def qapp():
    return QApplication.instance() or QApplication([])


def test_a_failed_quality_refresh_does_not_keep_the_old_size(qapp):
    option = SimpleNamespace(
        label="Quality",
        choices=[("SD", "sd"), ("HD", "hd")],
        default="hd",
        help="",
    )
    state = {"fail": False}

    def describe(_chosen):
        if state["fail"]:
            raise PermissionError(13, "Permission denied", "/games")
        return "/games/install", "500 GB free", "downloads about 60 GiB"

    def on_choice(value):
        state["fail"] = value == "sd"

    dialog = QuickInstallDialog(
        "WuWa",
        notice="",
        describe=describe,
        choice=option,
        choice_sizes={"sd": 1 << 30, "hd": 2 << 30},
        on_choice=on_choice,
    )
    try:
        before = dialog.summary_label.text()
        dialog.choice_combo.setCurrentIndex(0)  # SD: the refresh raises OSError
        # Either the choice must not stay applied, or the shown numbers must change.
        assert dialog.choice_value == "hd" or dialog.summary_label.text() != before
    finally:
        dialog.deleteLater()
