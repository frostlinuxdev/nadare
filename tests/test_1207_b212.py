# SPDX-License-Identifier: GPL-3.0-or-later
"""An unreadable default library must not raise out of QuickInstallDialog's constructor."""
from __future__ import annotations

import os

os.environ.setdefault("QT_QPA_PLATFORM", "offscreen")

import pytest

pytest.importorskip("PyQt6.QtWidgets")
from PyQt6.QtWidgets import QApplication

from nadare.gui.quick_install import QuickInstallDialog


@pytest.fixture(scope="module")
def qapp():
    return QApplication.instance() or QApplication([])


def test_an_unreadable_default_library_does_not_escape_the_constructor(qapp):
    def describe(_chosen):
        raise PermissionError(13, "Permission denied")

    # _use_folder() refuses this same OSError for a picked folder; the first
    # describe(None) in __init__ has no such guard.
    dialog = QuickInstallDialog("Game", notice="", describe=describe)
    try:
        assert dialog.chosen_folder == ""
    finally:
        dialog.deleteLater()
