# SPDX-License-Identifier: GPL-3.0-or-later
"""#1031: the Library grid lists games in the same order as the sidebar.

Profiles keep their config insertion order, which used to be the Library grid's order.
The sidebar sorts by running, then installed, then the rest, each alphabetical; the
grid uses the same order, except that a running game stays with the installed ones (a
card does not jump when its game is started from it).
"""

from __future__ import annotations

import os

os.environ.setdefault("QT_QPA_PLATFORM", "offscreen")

import pytest

try:  # pragma: no cover - exercised only when PyQt6 is missing
    from PyQt6.QtWidgets import QApplication
except Exception:  # noqa: BLE001
    pytest.skip("PyQt6 is not importable in this interpreter", allow_module_level=True)

from nadare.core import artwork
from nadare.core.backends import Status
from nadare.gui.library import LibraryPage
from nadare.gui.sidebar import SidebarGames


@pytest.fixture(scope="module")
def qapp():
    return QApplication.instance() or QApplication([])


@pytest.fixture(autouse=True)
def no_real_art(monkeypatch):
    monkeypatch.setattr(artwork, "fetch", lambda *args, **kwargs: None)


def _widgets(qapp):
    library = LibraryPage()
    sidebar = SidebarGames()
    return library, sidebar


def _profiles():
    # Deliberately not the sidebar's order: Zebra, Alpha, Middle would sort A, M, Z.
    return {
        "zeta": {"title": "Zebra", "backend": "wine"},
        "alpha": {"title": "Alpha", "backend": "wine"},
        "middle": {"title": "Middle", "backend": "wine"},
    }


def test_the_grid_matches_the_sidebar_before_any_status_is_known(qapp):
    library, sidebar = _widgets(qapp)
    profiles = _profiles()
    try:
        library.refresh(profiles)
        sidebar.set_profiles(profiles)

        assert list(library._cards) == sidebar.profiles()
    finally:
        library.deleteLater()
        sidebar.deleteLater()


def test_the_grid_matches_the_sidebar_after_statuses_arrive(qapp):
    library, sidebar = _widgets(qapp)
    profiles = _profiles()
    statuses = {
        "zeta": Status("installed"),
        "alpha": Status("running", running=True),
        "middle": Status("not_installed"),
    }
    try:
        library.refresh(profiles)
        sidebar.set_profiles(profiles)
        for name, status in statuses.items():
            sidebar.set_status(name, status)
            library.set_state(name, status)

        assert sidebar.profiles() == ["alpha", "zeta", "middle"]
        assert list(library._cards) == sidebar.profiles()
    finally:
        library.deleteLater()
        sidebar.deleteLater()


def test_starting_a_game_does_not_move_its_card(qapp):
    library, sidebar = _widgets(qapp)
    profiles = _profiles()
    try:
        library.refresh(profiles)
        sidebar.set_profiles(profiles)
        for name in ("zeta", "alpha"):
            sidebar.set_status(name, Status("installed"))
            library.set_state(name, Status("installed"))
        library.set_state("middle", Status("not_installed"))
        before = list(library._cards)
        cards = dict(library._cards)
        assert before == ["alpha", "zeta", "middle"]

        running = Status("running", running=True)
        sidebar.set_status("zeta", running)
        library.set_state("zeta", running)

        assert sidebar.profiles()[0] == "zeta"  # the sidebar still lists it first
        assert list(library._cards) == before
        assert library._cards == cards  # no rebuild: the same card objects
    finally:
        library.deleteLater()
        sidebar.deleteLater()


def test_a_finished_install_moves_the_card_into_the_installed_group(qapp):
    library, _sidebar = _widgets(qapp)
    profiles = _profiles()
    try:
        library.refresh(profiles)
        for name in profiles:
            library.set_state(name, Status("not_installed"))
        assert list(library._cards) == ["alpha", "middle", "zeta"]
        library.set_state("zeta", Status("installed"))
        assert list(library._cards) == ["zeta", "alpha", "middle"]
    finally:
        library.deleteLater()
        _sidebar.deleteLater()
