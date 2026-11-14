# SPDX-License-Identifier: GPL-3.0-or-later
import os

os.environ.setdefault("QT_QPA_PLATFORM", "offscreen")

import pytest

try:
    from PyQt6.QtWidgets import QApplication
except Exception:  # pragma: no cover - optional GUI dependencies
    pytest.skip("PyQt6 is not importable in this interpreter", allow_module_level=True)

from nadare.core import artwork
from nadare.gui import library


@pytest.fixture(scope="module")
def qapp():
    return QApplication.instance() or QApplication([])


@pytest.fixture(autouse=True)
def no_real_art(monkeypatch):
    monkeypatch.setattr(artwork, "fetch", lambda *args, **kwargs: None)


def test_a_failed_rebuild_is_not_remembered_as_done(qapp, monkeypatch):
    page = library.LibraryPage()
    real_card = library.GameCard
    calls = {"n": 0}

    class Boom(real_card):
        def __init__(self, *args, **kwargs):
            calls["n"] += 1
            if calls["n"] == 1:
                raise RuntimeError("transient Qt failure")
            super().__init__(*args, **kwargs)

    try:
        monkeypatch.setattr(library, "GameCard", Boom)
        profiles = {"a": {"title": "A", "backend": "wine"}}
        with pytest.raises(RuntimeError):
            page.refresh(profiles)
        page.refresh(profiles)
        assert "a" in page._cards
    finally:
        page.deleteLater()
        qapp.processEvents()
