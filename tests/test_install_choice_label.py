# SPDX-License-Identifier: GPL-3.0-or-later
"""#990 point 2: the game page says which resource quality the game uses (WuWa SD/HD/UHD)."""

from __future__ import annotations

import os

os.environ.setdefault("QT_QPA_PLATFORM", "offscreen")

import pytest

from nadare.core import backends


@pytest.mark.parametrize(
    ("profile", "expected"),
    [
        ({}, "Resource quality: HD"),  # the default
        ({"wuwa_quality": "uhd"}, "Resource quality: UHD"),
        ({"extra": {"wuwa_quality": "sd"}}, "Resource quality: SD"),
        ({"wuwa_quality": "nonsense"}, "Resource quality: HD"),
    ],
)
def test_the_label_names_the_chosen_quality(profile, expected):
    assert backends.install_choice_label("direct-wuwa", profile) == expected


@pytest.mark.parametrize("kind", ["direct-pgr", "wine", "no-such-kind", ""])
def test_games_without_a_choice_have_no_label(kind):
    assert backends.install_choice_label(kind, {}) == ""


def test_the_game_page_shows_it_for_wuwa_only():
    pytest.importorskip("PyQt6.QtWidgets")
    from PyQt6.QtWidgets import QApplication

    from nadare.gui.game_view import GameView

    app = QApplication.instance() or QApplication([])
    page = GameView()
    try:
        page.set_profile("w", {"backend": "direct-wuwa", "title": "WuWa", "wuwa_quality": "sd"})
        assert not page.choice_label.isHidden()
        assert page.choice_label.text() == "Resource quality: SD"
        page.set_profile("p", {"backend": "direct-pgr", "title": "PGR"})
        assert page.choice_label.isHidden()
    finally:
        page.deleteLater()
        app.processEvents()
