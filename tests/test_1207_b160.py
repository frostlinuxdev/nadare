# SPDX-License-Identifier: GPL-3.0-or-later
import os

os.environ.setdefault("QT_QPA_PLATFORM", "offscreen")

import pytest

try:
    from PyQt6.QtWidgets import QApplication
except Exception:  # pragma: no cover
    pytest.skip("PyQt6 is not importable", allow_module_level=True)

from nadare.gui.game_view import GameView


@pytest.fixture(scope="module")
def qapp():
    app = QApplication.instance()
    if app is None:
        app = QApplication([])
    yield app


def test_a_background_refresh_keeps_the_paused_message(qapp):
    view = GameView()
    try:
        view.set_profile("sample", {"title": "Sample"})
        view.set_paused("install")
        assert view.step_label.text().startswith("Paused")

        view.finish("status", 0)  # a successful background refresh

        assert view.is_paused(), "the download must still be resumable"
        assert view.step_label.text().startswith("Paused"), (
            "a background refresh erased the paused message"
        )
    finally:
        view.deleteLater()
        qapp.processEvents()
