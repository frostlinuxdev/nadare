# SPDX-License-Identifier: GPL-3.0-or-later
"""A game that is being installed, updated or repaired says so in the Library.

User sim r2 item 5 (and r1 #5/#6), seen again on the b28 tip 2026-11-21: while Arknights
downloaded, its Library card read NOT INSTALLED, the same as a game nobody had touched. The
game page and Downloads knew; the Library only saw the status probe ("not installed yet").
"""

from __future__ import annotations

import os

os.environ.setdefault("QT_QPA_PLATFORM", "offscreen")

import pytest

try:
    from PyQt6.QtWidgets import QApplication
except ImportError:  # pragma: no cover
    pytest.skip("PyQt6 is not importable", allow_module_level=True)

from nadare.core.backends import Status
from nadare.core.config import Config
from nadare.gui import library
from nadare.gui.main_window import MainWindow


@pytest.fixture(scope="module")
def qapp():
    return QApplication.instance() or QApplication([])


@pytest.fixture
def page(qapp):
    page = library.LibraryPage()
    yield page
    page.wait_for_images()
    page.close()
    page.deleteLater()
    qapp.processEvents()


NOT_INSTALLED = Status(state="not_installed", version=None, running=False, message="")


def _badge(page, name):
    card = page._cards[name]
    return card.badge.text() if card._has_state_line else ""


def test_an_install_shows_installing_over_the_not_installed_probe(page):
    page.refresh({"ark": {"title": "Arknights", "backend": "direct"}})
    page.set_state("ark", NOT_INSTALLED)
    assert _badge(page, "ark") == "NOT INSTALLED"

    page.set_busy("ark", "installing")
    assert _badge(page, "ark") == "INSTALLING"
    # A status probe during the download still says "not installed yet": the card keeps INSTALLING.
    page.set_state("ark", NOT_INSTALLED)
    assert _badge(page, "ark") == "INSTALLING"

    page.set_busy("ark", None)
    assert _badge(page, "ark") == "NOT INSTALLED"


def test_the_busy_state_survives_a_rebuild_and_reaches_pinned_and_recent(page):
    profiles = {
        "ark": {
            "title": "Arknights",
            "backend": "direct",
            "favourite": True,
            "last_played": "2026-11-20T10:00:00+00:00",
        },
        "bd2": {"title": "Brown Dust 2", "backend": "direct"},
    }
    page.refresh(profiles)
    page.set_busy("ark", "updating")
    page._shown = None  # force the full rebuild a config change would do
    page.refresh(profiles)
    assert _badge(page, "ark") == "UPDATING"
    fav = page._favourite_cards["ark"]
    assert fav._has_state_line and fav.badge.text() == "UPDATING"
    tile = next(t for t in page.recent_tiles if t.profile == "ark")
    assert tile.badge.text() == "UPDATING"
    assert _badge(page, "bd2") == ""  # the other game is untouched


def test_a_removed_game_forgets_its_busy_state(page):
    page.refresh({"ark": {"title": "Arknights"}})
    page.set_busy("ark", "installing")
    page.refresh({})
    page.refresh({"ark": {"title": "Arknights"}})
    assert _badge(page, "ark") != "INSTALLING"


def _window(tmp_path):
    config = Config.load(tmp_path / "config.json")
    config.add_profile("alpha", backend="wine", title="Alpha")
    return MainWindow(config)


@pytest.mark.parametrize(
    ("operation", "label"),
    [("install", "INSTALLING"), ("update", "UPDATING"), ("repair", "REPAIRING")],
)
def test_the_window_marks_the_card_while_the_operation_runs(tmp_path, qapp, operation, label):
    window = _window(tmp_path)
    try:
        window.library.set_state("alpha", NOT_INSTALLED)
        window._on_busy("alpha", operation, True)
        assert _badge(window.library, "alpha") == label
        window._on_busy("alpha", operation, False)
        assert _badge(window.library, "alpha") == "NOT INSTALLED"
    finally:
        window.close()
        window.deleteLater()
        qapp.processEvents()


def test_a_queued_install_reads_waiting_and_a_launch_changes_nothing(tmp_path, qapp):
    window = _window(tmp_path)
    try:
        window.library.set_state("alpha", NOT_INSTALLED)
        window._on_queued("alpha", "install")
        assert _badge(window.library, "alpha") == "WAITING"
        window._on_busy("alpha", "install", True)
        assert _badge(window.library, "alpha") == "INSTALLING"
        window._on_busy("alpha", "install", False)
        # Background checks and launches keep their own states (PLAYING comes from the probe).
        window._on_busy("alpha", "status", True)
        window._on_busy("alpha", "launch", True)
        assert _badge(window.library, "alpha") == "NOT INSTALLED"
    finally:
        window.close()
        window.deleteLater()
        qapp.processEvents()
