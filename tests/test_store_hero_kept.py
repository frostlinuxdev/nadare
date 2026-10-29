# SPDX-License-Identifier: GPL-3.0-or-later
"""The Store detail page keeps its key art when only the installed state changes (edge hunt #3)."""

from __future__ import annotations

import os

os.environ.setdefault("QT_QPA_PLATFORM", "offscreen")

import pytest

try:
    from PyQt6.QtGui import QColor, QPixmap
    from PyQt6.QtWidgets import QApplication
except Exception:  # pragma: no cover - optional GUI dependencies
    pytest.skip("PyQt6 is not importable in this interpreter", allow_module_level=True)

from nadare.core.storecatalog import StoreEntry, entries
from nadare.gui.store_detail import StoreDetail


@pytest.fixture(scope="module")
def qapp():
    app = QApplication.instance() or QApplication([])
    yield app


def _entry(kind: str, **changes) -> StoreEntry:
    base = next(item for item in entries() if item.kind == kind)
    values = {field: getattr(base, field) for field in StoreEntry.__dataclass_fields__}
    values.update(changes)
    return StoreEntry(**values)


def _art() -> QPixmap:
    pixmap = QPixmap(1200, 400)
    pixmap.fill(QColor(200, 40, 40))
    return pixmap


def test_an_installed_state_refresh_keeps_the_art(qapp):
    detail = StoreDetail(_entry("direct-pgr"))
    detail.set_hero(_art())
    detail.set_entry(_entry("direct-pgr", installed=True))
    assert detail._hero is not None, "the build did not change, so the art stays"


def test_the_same_build_refreshed_twice_still_has_its_art(qapp):
    detail = StoreDetail(_entry("direct-pgr"))
    detail.set_hero(_art())
    for installed in (True, False, True):
        detail.set_entry(_entry("direct-pgr", installed=installed))
    assert detail._hero is not None


def test_switching_to_another_build_resets_the_art(qapp):
    detail = StoreDetail(_entry("direct-pgr"))
    detail.set_hero(_art())
    detail.set_entry(_entry("launcher-nikke"))
    assert detail._hero is None, "another build has its own art, fetched afresh"


def test_the_first_entry_starts_without_art(qapp):
    assert StoreDetail(_entry("direct-pgr"))._hero is None
