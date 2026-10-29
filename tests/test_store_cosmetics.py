# SPDX-License-Identifier: GPL-3.0-or-later
"""Three Store cosmetics from the b17 edge hunt: a stretched hero, a huge dialog, a wrong count."""

from __future__ import annotations

import os

os.environ.setdefault("QT_QPA_PLATFORM", "offscreen")

import pytest

try:
    from PyQt6.QtGui import QColor, QPixmap
    from PyQt6.QtWidgets import QApplication
except Exception:  # pragma: no cover - optional GUI dependencies
    pytest.skip("PyQt6 is not importable in this interpreter", allow_module_level=True)

from nadare.core import artwork
from nadare.core.storecatalog import StoreEntry, entries
from nadare.gui.quick_install import QuickInstallDialog
from nadare.gui.store import StorePage
from nadare.gui.store_detail import StoreDetail


@pytest.fixture(scope="module")
def qapp():
    app = QApplication.instance() or QApplication([])
    yield app


@pytest.fixture(autouse=True)
def no_real_art(monkeypatch):
    monkeypatch.setattr(artwork, "fetch", lambda *a, **k: None)


@pytest.fixture
def dispose(qapp):
    """Delete every widget a test made, so none outlives the QApplication."""
    made: list = []
    yield made.append
    for widget in made:
        widget.close()
        widget.deleteLater()
    qapp.processEvents()


def _entry(kind: str) -> StoreEntry:
    return next(item for item in entries() if item.kind == kind)


# ---- the hero is drawn at the size of its label, never stretched ---------------------------
@pytest.mark.parametrize("size", [(880, 560), (1200, 900), (1000, 700)])
def test_the_hero_pixmap_matches_its_label(qapp, dispose, size):
    detail = StoreDetail(_entry("launcher-nikke"))  # has a notice, so the label is squeezed
    dispose(detail)
    pixmap = QPixmap(1200, 400)
    pixmap.fill(QColor(200, 40, 40))
    detail.set_hero(pixmap)
    detail.resize(*size)
    detail.show()
    qapp.processEvents()
    shown = detail.hero_label.pixmap()
    assert (shown.width(), shown.height()) == (
        detail.hero_label.width(),
        detail.hero_label.height(),
    )
    assert not detail.hero_label.hasScaledContents()


def test_the_hero_follows_a_resize(qapp, dispose):
    detail = StoreDetail(_entry("direct-pgr"))
    dispose(detail)
    detail.resize(1200, 900)
    detail.show()
    qapp.processEvents()
    first = detail.hero_label.pixmap().size()
    detail.resize(880, 560)
    qapp.processEvents()
    second = detail.hero_label.pixmap().size()
    assert second != first
    assert (second.width(), second.height()) == (
        detail.hero_label.width(),
        detail.hero_label.height(),
    )


# ---- a long install path does not blow the dialog up ----------------------------------------
def test_a_very_long_folder_is_shortened_in_the_middle_with_the_full_path_on_hover(
    qapp, dispose
):
    long_path = "/mnt/" + "/".join(["a-very-long-folder-name"] * 9) + "/game"
    dialog = QuickInstallDialog(
        "Game", notice="", describe=lambda chosen: (long_path, "10 GB free", "")
    )
    dispose(dialog)
    text = dialog.folder_label.text()
    assert "…" in text and len(text) < 140
    assert text.endswith("(10 GB free)") and "/game" in text
    assert text.startswith("Install folder: /mnt/")
    assert dialog.folder_label.toolTip() == long_path
    assert dialog.sizeHint().width() < 800


def test_a_short_folder_is_shown_whole_without_a_tooltip(qapp, dispose):
    dialog = QuickInstallDialog(
        "Game", notice="", describe=lambda chosen: ("/home/me/games/game", "10 GB free", "")
    )
    dispose(dialog)
    assert dialog.folder_label.text() == "Install folder: /home/me/games/game (10 GB free)"
    assert dialog.folder_label.toolTip() == ""


# ---- a grouped card counts every build of the game, whatever the filter hides ---------------
def _gfl2_card(page: StorePage):
    return next(c for c in page._cards.values() if c.title_label.text() == "Girls' Frontline 2")


def test_the_version_count_ignores_the_tier_filter(qapp, dispose):
    page = StorePage()
    dispose(page)
    page.resize(1400, 900)
    page.show()
    qapp.processEvents()
    everything = _gfl2_card(page).delivery_label.text()
    assert "3 versions" in everything
    page.tier_filter.setCurrentText("Experimental")
    qapp.processEvents()
    assert "3 versions" in _gfl2_card(page).delivery_label.text()
