# SPDX-License-Identifier: GPL-3.0-or-later
"""apply_theme does not re-set an unchanged style sheet (CI segfault in setStyleSheet, #1024).

Every GUI test module styles the shared QApplication in its fixture; each setStyleSheet
re-polishes every widget still alive in that worker, and one of those calls crashed Qt in CI
(``theme.py apply_theme`` -> ``setStyleSheet``). Setting the same text again changes nothing,
so it is skipped; a changed sheet (a desktop text-size change) is still applied.
"""

from __future__ import annotations

import pytest

from nadare.gui import theme

# This file restyles the whole app: end earlier tests' windows first (#1024).
pytestmark = pytest.mark.usefixtures("restyle_safe")


class FakeApp:
    def __init__(self):
        self.sheet = ""
        self.sets = 0

    def styleSheet(self):
        return self.sheet

    def setStyleSheet(self, text):
        self.sets += 1
        self.sheet = text


def test_the_same_sheet_is_set_once(monkeypatch):
    monkeypatch.setattr(theme, "_font_scale", lambda: 1.0)
    app = FakeApp()
    theme.apply_theme(app)
    theme.apply_theme(app)
    theme.apply_theme(app)
    assert app.sets == 1
    assert app.sheet == theme.stylesheet()


def test_a_changed_sheet_is_applied(monkeypatch):
    monkeypatch.setattr(theme, "_font_scale", lambda: 1.0)
    app = FakeApp()
    theme.apply_theme(app)
    monkeypatch.setattr(theme, "_font_scale", lambda: 1.5)
    theme.apply_theme(app)
    assert app.sets == 2


def test_an_object_without_stylesheet_getter_still_gets_the_sheet():
    class Bare:
        def __init__(self):
            self.text = None

        def setStyleSheet(self, text):
            self.text = text

    target = Bare()
    theme.apply_theme(target)
    assert target.text
