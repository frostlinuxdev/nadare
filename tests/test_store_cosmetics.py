# SPDX-License-Identifier: GPL-3.0-or-later
"""Three Store cosmetics from the b17 edge hunt: a stretched hero, a huge dialog, a wrong count."""

from __future__ import annotations

import os

os.environ.setdefault("QT_QPA_PLATFORM", "offscreen")

import pytest

try:
    from PyQt6.QtWidgets import QApplication
except Exception:  # pragma: no cover - optional GUI dependencies
    pytest.skip("PyQt6 is not importable in this interpreter", allow_module_level=True)

from nadare.core import artwork
from nadare.gui.quick_install import QuickInstallDialog
from nadare.gui.store import StorePage


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


# ---- the hero is drawn at the size of its label, never stretched ---------------------------
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


def test_the_version_count_ignores_the_filters(qapp, dispose):
    page = StorePage()
    dispose(page)
    page.resize(1400, 900)
    page.show()
    qapp.processEvents()
    assert "2 versions" in _gfl2_card(page).delivery_label.text()
    # (Both GFL2 builds are Verified since the launcher kinds were retired, so a tier filter
    # cannot hide one of them any more; the search below is the case that can.)
    # A search that keeps one build still shows the whole game, not that build alone.
    page.tier_filter.setCurrentText("All tiers")
    page.search.setText("haoplay")
    qapp.processEvents()
    assert "2 versions" in _gfl2_card(page).delivery_label.text()


# ---- the unofficial notice is shown whole, not clipped under its heading -------------------
def test_quick_install_shows_every_wrapped_line_of_the_notice(qapp, dispose):
    from PyQt6.QtWidgets import QLabel

    from nadare.core.backends import UNOFFICIAL_NOTICE

    dialog = QuickInstallDialog(
        "Genshin Impact",
        notice=UNOFFICIAL_NOTICE,
        describe=lambda chosen: (
            "/home/me/.local/share/nadare/profiles/genshin-impact/install",
            "306 GiB free",
            "Installing Genshin Impact downloads about 94 GiB and needs about as much "
            "free space (306 GiB free).",
        ),
    )
    dispose(dialog)
    dialog.show()
    qapp.processEvents()
    for label in dialog.findChildren(QLabel):
        if label.isVisible() and label.wordWrap():
            need = label.heightForWidth(label.width())
            assert label.height() >= need, (label.text()[:40], label.height(), need)
