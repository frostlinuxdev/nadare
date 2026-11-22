# SPDX-License-Identifier: GPL-3.0-or-later
"""#1621: the Library grid is split into Installed and Not installed.

Before any status is known the grid stays one "LIBRARY" section, so cards do not all start under
"Not installed" and jump. A running game counts as installed; an interrupted install does not.
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


@pytest.fixture(scope="module")
def qapp():
    return QApplication.instance() or QApplication([])


@pytest.fixture(autouse=True)
def no_real_art(monkeypatch):
    monkeypatch.setattr(artwork, "fetch", lambda *args, **kwargs: None)


@pytest.fixture
def page(qapp):
    page = LibraryPage()
    yield page
    page.wait_for_images()
    page.deleteLater()
    qapp.processEvents()


def _profiles():
    return {
        "zeta": {"title": "Zebra", "backend": "wine"},
        "alpha": {"title": "Alpha", "backend": "wine"},
        "middle": {"title": "Middle", "backend": "wine"},
    }


def _in(container, page):
    return [name for name, card in page._cards.items() if card.parent() is container]


def test_one_library_section_until_a_status_is_known(page):
    page.refresh(_profiles())
    assert page.grid_title.text() == "LIBRARY"
    assert page.not_installed_section.isHidden()
    assert _in(page._container, page) == ["alpha", "middle", "zeta"]


def test_installed_and_not_installed_are_two_sections(page):
    page.refresh(_profiles())
    page.set_state("zeta", Status("installed"))
    page.set_state("alpha", Status("not_installed"))
    page.set_state("middle", Status("running", running=True))

    assert page.grid_title.text() == "INSTALLED"
    assert _in(page._container, page) == ["middle", "zeta"]
    assert not page.not_installed_section.isHidden()
    assert _in(page._rest_container, page) == ["alpha"]
    # The first status alone splits the grid, even when it does not change the order.
    other = LibraryPage()
    try:
        other.refresh(_profiles())
        other.set_state("alpha", Status("installed"))
        assert other.grid_title.text() == "INSTALLED"
        assert _in(other._rest_container, other) == ["middle", "zeta"]
    finally:
        other.deleteLater()


def test_an_empty_installed_section_is_hidden(page):
    page.refresh(_profiles())
    for name in _profiles():
        page.set_state(name, Status("not_installed"))
    assert page.grid_section.isHidden()
    assert not page.not_installed_section.isHidden()
    assert _in(page._rest_container, page) == ["alpha", "middle", "zeta"]


def test_a_finished_install_moves_the_card_across(page):
    page.refresh(_profiles())
    for name in _profiles():
        page.set_state(name, Status("not_installed"))
    page.set_state("zeta", Status("installed"))
    assert _in(page._container, page) == ["zeta"]
    assert not page.grid_section.isHidden()
    assert _in(page._rest_container, page) == ["alpha", "middle"]


def test_search_hides_a_section_with_no_match(page):
    page.refresh(_profiles())
    page.set_state("zeta", Status("installed"))
    page.set_state("alpha", Status("not_installed"))
    page.set_state("middle", Status("not_installed"))
    page.search.setText("zeb")
    assert not page.grid_section.isHidden()
    assert page.not_installed_section.isHidden()
    page.search.setText("alp")
    assert page.grid_section.isHidden()
    assert not page.not_installed_section.isHidden()
    page.search.clear()
    assert not page.grid_section.isHidden() and not page.not_installed_section.isHidden()
