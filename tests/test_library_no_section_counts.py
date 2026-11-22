# SPDX-License-Identifier: GPL-3.0-or-later
"""Library section headers carry only their title (user 2026-11-21: the counts were useless).

No "N games", "N pinned" or "last played ..." text on the right of CONTINUE PLAYING, FAVOURITES,
INSTALLED/LIBRARY or NOT INSTALLED.
"""

from __future__ import annotations

import os

os.environ.setdefault("QT_QPA_PLATFORM", "offscreen")

import pytest

try:  # pragma: no cover - exercised only when PyQt6 is missing
    from PyQt6.QtWidgets import QApplication, QLabel
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


def _header_texts(section):
    """Every non-empty label text in the section's header row (not inside its cards)."""
    texts = []
    for label in section.findChildren(QLabel):
        if label.text() and label.parent() is section:
            texts.append(label.text())
    return texts


def test_section_headers_show_only_their_title(page, qapp):
    page.resize(1600, 1000)
    profiles = {
        "a": {
            "title": "Alpha",
            "backend": "wine",
            "last_played": "2026-11-21T10:00:00+00:00",
            "favourite": True,
        },
        "b": {"title": "Bravo", "backend": "wine"},
        "c": {"title": "Charlie", "backend": "wine"},
    }
    page.refresh(profiles)
    page.set_state("a", Status("installed"))
    page.set_state("b", Status("installed"))
    page.set_state("c", Status("not_installed"))
    page.show()
    qapp.processEvents()
    sections = {
        "recent": page.recent_section,
        "favourites": page.favourites_section,
        "grid": page.grid_section,
        "rest": page.not_installed_section,
    }
    for name, section in sections.items():
        texts = _header_texts(section)
        assert texts == [section.title_label.text()], (name, texts)
    page.hide()
