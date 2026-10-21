# SPDX-License-Identifier: GPL-3.0-or-later
"""Version and busy-status wording on the game page."""

from __future__ import annotations

import os

os.environ.setdefault("QT_QPA_PLATFORM", "offscreen")

import pytest

try:  # pragma: no cover - exercised only when PyQt6 is missing
    from PyQt6.QtWidgets import QApplication
except Exception:  # noqa: BLE001
    pytest.skip("PyQt6 is not importable in this interpreter", allow_module_level=True)

from nadare.core.backends import Status
from nadare.core.config import Config
from nadare.gui.game_view import GameView, _version_text
from nadare.gui.main_window import MainWindow


@pytest.fixture(scope="module")
def qapp():
    app = QApplication.instance()
    if app is None:
        app = QApplication([])
    yield app


@pytest.mark.parametrize(
    ("raw", "shown"),
    [("v55", "v55"), ("55", "v55"), ("041.2.0", "v041.2.0"), ("V2", "V2"), (None, ""), ("", "")],
)
def test_version_gets_exactly_one_v(raw, shown):
    assert _version_text(raw) == shown


def test_the_pill_row_does_not_double_the_v(qapp):
    view = GameView()
    view.set_profile("czn", {"backend": "stove-czn", "title": "CZN"})
    view.set_status(Status("installed", version="v55"))
    assert view.version_label.text() == "v55"
    view.deleteLater()


def test_saving_remember_me_reads_as_words(tmp_path, qapp):
    config = Config.load(tmp_path / "config.json")
    config.add_profile("czn", backend="stove-czn", title="CZN")
    window = MainWindow(config)
    window.controller.start = lambda profile, op, payload=None: True
    window.open_game("czn")
    window._on_busy("czn", "set_remember", True)
    assert window.side_status.text() == "Saving · czn"
    window._on_busy("czn", "verify", True)
    assert window.side_status.text() == "Verify · czn"
    window.close()
