# SPDX-License-Identifier: GPL-3.0-or-later
"""The library grid keeps its columns after a game is removed (#197)."""

from __future__ import annotations

import os
import time
import types

os.environ.setdefault("QT_QPA_PLATFORM", "offscreen")

import pytest

try:  # pragma: no cover - exercised only when PyQt6 is missing
    from PyQt6.QtWidgets import QApplication
except Exception:  # noqa: BLE001
    pytest.skip("PyQt6 is not importable in this interpreter", allow_module_level=True)

from nadare.core.config import Config
from nadare.gui.main_window import MainWindow
from nadare.gui.theme import apply_theme


@pytest.fixture(scope="module")
def qapp():
    app = QApplication.instance()
    if app is None:
        app = QApplication([])
    yield app


def _pump(app, seconds=0.3):
    end = time.time() + seconds
    while time.time() < end:
        app.processEvents()


def test_removing_a_game_does_not_collapse_the_grid_to_one_column(tmp_path, qapp):
    apply_theme(qapp)
    config = Config.load(tmp_path / "config.json")
    for name in ("alpha", "beta", "gamma", "delta", "doomed"):
        config.add_profile(name, backend="arknights-yostar", title=name.title())
    window = MainWindow(config)
    window.controller.start = lambda profile, op, payload=None: True
    window.resize(1256, 700)
    window.show()
    window.show_library()
    _pump(qapp)

    window.open_game("doomed")  # removal runs from the game page: library hidden
    _pump(qapp, 0.1)
    window._on_game_removed("doomed", types.SimpleNamespace(profile_kept=False, warnings=[]))
    _pump(qapp)

    cards = list(window.library._cards.values())
    assert len(cards) == 4
    assert len({card.x() for card in cards}) >= 2, "cards stacked in a single column"
    window.close()
