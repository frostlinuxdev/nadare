# SPDX-License-Identifier: GPL-3.0-or-later
# ruff: noqa: F811 - tests take the fixtures imported below as arguments
"""Small things the 2026-11-20 re-check on a real KDE Wayland desktop showed.

* While installing, a long pack file name (no spaces) widened the game page past the window,
  so CANCEL and the button row were cut off on the right.
* A game waiting in line said INSTALLING.
* The number fields' up/down arrows were two dots.
* The status line under the title was hard to read over light key art.
* The install question could be maximised, and then stretched badly.
"""

from __future__ import annotations

from _gui_smoke_helpers import (  # noqa: F401 - helpers and pytest fixtures, found by name
    _collect_garbage_between_tests,
    qapp,
)
from PyQt6.QtWidgets import QGraphicsDropShadowEffect

LONG_PACK = (
    "installing pack 1 of 1: AetherGazer_jp_us_jp_us_prod_293_21_9_windows_limit_sdk_release_"
    "W12000000_B03AB844D2F9C035E1A7.zip"
)


def test_a_long_pack_name_does_not_widen_the_game_page(qapp):
    from PyQt6.QtWidgets import QWidget

    from nadare.core.events import Event
    from nadare.gui.game_view import GameView

    view = GameView()
    try:
        view.set_profile("sample", {})
        view.resize(700, 900)
        view.show()
        view.set_busy("install")
        view.append_event(Event("step", LONG_PACK))
        qapp.processEvents()
        assert "AetherGazer" in view.step_label.text()
        too_wide = [
            w.objectName() or type(w).__name__
            for w in view.findChildren(QWidget)
            if w.isVisible() and w.minimumSizeHint().width() > 700
        ]
        assert too_wide == []
    finally:
        view.deleteLater()


def test_a_game_waiting_in_line_does_not_say_installing(qapp):
    from nadare.gui.game_view import GameView

    view = GameView()
    try:
        view.set_profile("sample", {})
        view.set_waiting("install", ahead=0, can_now=True)
        assert view.badge.text() == "WAITING"
        view.set_busy("install")
        assert view.badge.text() == "INSTALLING"
    finally:
        view.deleteLater()


def test_number_fields_draw_real_arrows():
    from nadare.gui import theme

    sheet = theme.stylesheet()
    assert "QAbstractSpinBox::up-arrow" in sheet and "QAbstractSpinBox::down-arrow" in sheet
    assert "chevron-up.svg" in sheet and theme._UP_ARROW_ICON.is_file()


def test_the_hero_status_line_has_a_shadow_for_light_art(qapp):
    from nadare.gui.game_view import GameView

    view = GameView()
    try:
        for label in (view.detail_label, view.backend_label, view.version_label):
            assert isinstance(label.graphicsEffect(), QGraphicsDropShadowEffect)
    finally:
        view.deleteLater()


def test_the_install_question_cannot_be_maximised(qapp):
    from PyQt6.QtCore import Qt

    from nadare.gui.quick_install import QuickInstallDialog

    dialog = QuickInstallDialog(
        "Game", notice="", describe=lambda chosen: ("/home/me/games/game", "10 GB free", "")
    )
    try:
        assert not dialog.windowFlags() & Qt.WindowType.WindowMaximizeButtonHint
        dialog.show()
        qapp.processEvents()
        assert dialog.minimumSize() == dialog.maximumSize()
        assert dialog.width() >= 520
    finally:
        dialog.deleteLater()
