# SPDX-License-Identifier: GPL-3.0-or-later
"""Library cards keep their state pill whole (b23-rc2 click-through: "NOT INSTALLEI" on the
NIKKE card, whose "Publisher's launcher" line took the room), and a retired kind reads as one."""

from __future__ import annotations

import os

os.environ.setdefault("QT_QPA_PLATFORM", "offscreen")

import pytest

try:
    from PyQt6.QtWidgets import QApplication
except ImportError:  # pragma: no cover
    pytest.skip("PyQt6 is not importable", allow_module_level=True)

from nadare.gui.library import GameCard, backend_label


@pytest.fixture(scope="module")
def qapp():
    return QApplication.instance() or QApplication([])


@pytest.mark.parametrize("state", ["not_installed", "error", "installed", "running"])
def test_the_state_pill_is_never_cut(qapp, state):
    card = GameCard("nikke", "NIKKE: Goddess of Victory", "launcher-nikke")
    try:
        card.set_state(state)
        card.show()
        qapp.processEvents()
        if state == "installed":
            assert not card.badge.isVisible()  # a ready game shows no pill
        else:
            assert card.badge.isVisible()
            text = card.badge.fontMetrics().horizontalAdvance(card.badge.text())
            assert card.badge.contentsRect().width() >= text
        assert card.meta_label.toolTip() == backend_label("launcher-nikke")
    finally:
        card.deleteLater()


def test_a_retired_kind_has_a_plain_label():
    assert backend_label("launcher-nte") == "Old launcher (retired)"
    assert backend_label("launcher-gfl2-haoplay") == "Old launcher (retired)"
