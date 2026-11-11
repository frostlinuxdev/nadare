# SPDX-License-Identifier: GPL-3.0-or-later
"""A background check that fails the same way again does not repeat its line (b23 round 4).

The rc4 click-through of a retired launcher game showed the same message once per status
check in the game's OUTPUT box.
"""

from __future__ import annotations

import os

os.environ.setdefault("QT_QPA_PLATFORM", "offscreen")

import pytest

try:  # pragma: no cover
    from PyQt6.QtWidgets import QApplication
except Exception:  # noqa: BLE001
    pytest.skip("PyQt6 is not importable in this interpreter", allow_module_level=True)

from nadare.core.backends import Event
from nadare.gui.game_view import GameView


@pytest.fixture(scope="module")
def qapp():
    return QApplication.instance() or QApplication([])


@pytest.fixture
def view(qapp):
    page = GameView()
    page.set_profile("nte", {"backend": "launcher-nte", "title": "NTE"})
    yield page
    page.deleteLater()
    qapp.processEvents()


def _count(view: GameView, text: str) -> int:
    return view.console.toPlainText().count(text)


def test_the_same_background_error_is_written_once(view):
    message = "This launcher version is retired. Remove it and install NTE from the Store."
    for _ in range(4):
        view.append_event(Event("error", message))
    assert _count(view, "is retired") == 1


def test_a_different_line_in_between_lets_it_show_again(view):
    view.append_event(Event("error", "boom"))
    view.append_line("launched", "status")
    view.append_event(Event("error", "boom"))
    assert _count(view, "boom") == 2


def test_errors_during_an_operation_are_all_kept(view):
    view.set_busy("install")
    view.append_event(Event("error", "retrying"))
    view.append_event(Event("error", "retrying"))
    assert _count(view, "retrying") == 2


def test_another_game_starts_with_a_clean_slate(view):
    view.append_event(Event("error", "same words"))
    view.set_profile("other", {"backend": "direct-bd2", "title": "Other"})
    view.append_event(Event("error", "same words"))
    assert _count(view, "same words") == 1  # the cleared console is not "the line above"
