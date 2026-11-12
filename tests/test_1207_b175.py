import os

os.environ.setdefault("QT_QPA_PLATFORM", "offscreen")

import pytest

try:
    from PyQt6.QtCore import QEvent, QPoint
    from PyQt6.QtWidgets import QApplication, QMenu
except Exception:  # pragma: no cover - optional GUI dependencies
    pytest.skip("PyQt6 is not importable in this interpreter", allow_module_level=True)

from nadare.core import artwork
from nadare.gui.library import LibraryPage


@pytest.fixture(scope="module")
def qapp():
    return QApplication.instance() or QApplication([])


@pytest.fixture(autouse=True)
def no_real_art(monkeypatch):
    monkeypatch.setattr(artwork, "fetch", lambda *args, **kwargs: None)


def test_repeated_right_clicks_do_not_accumulate_menus(qapp, monkeypatch):
    page = LibraryPage()
    try:
        page.refresh({"a": {"title": "A", "backend": "wine"}})
        monkeypatch.setattr(QMenu, "exec", lambda self, *args, **kwargs: None)
        for _ in range(3):
            page._cards["a"].customContextMenuRequested.emit(QPoint(1, 1))
            qapp.processEvents()
        qapp.sendPostedEvents(None, QEvent.Type.DeferredDelete)
        qapp.processEvents()
        assert page.findChildren(QMenu) == []
    finally:
        page.deleteLater()
        qapp.processEvents()
