"""Offscreen tests for the built-in store detail view."""

from __future__ import annotations

import os
import socket

os.environ.setdefault("QT_QPA_PLATFORM", "offscreen")

import pytest

try:
    from PyQt6.QtCore import Qt
    from PyQt6.QtGui import QColor, QKeyEvent, QPixmap
    from PyQt6.QtWidgets import QApplication, QWidget
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


@pytest.mark.parametrize(
    ("kind", "fact"),
    [
        ("direct-pgr", "Installs straight from the publisher's servers, without their launcher."),
        ("launcher-nikke", "Runs the publisher's own launcher under Wine/Proton."),
        ("arknights-yostar", "Runs the publisher's installer under Wine/Proton."),
        ("fgo-lepton", "Runs in an Android container (Lepton)."),
    ],
)
def test_delivery_explanation_is_visible(qapp, kind, fact):
    widget = StoreDetail(_entry(kind))
    assert fact in widget.delivery_label.text()


def test_size_is_shown_and_omitted(qapp):
    widget = StoreDetail(_entry("direct-pgr", size_bytes=60 * 1024**3))
    assert "Install size: about 60 GiB" in widget.size_label.text()
    widget.set_entry(_entry("direct-pgr", size_bytes=None))
    assert widget.size_label.isHidden()


def test_notice_is_shown_and_cleared(qapp):
    noticed = _entry(
        "launcher-nikke",
        notice=("Publisher note", "https://example.invalid", "Details"),
    )
    widget = StoreDetail(noticed)
    assert widget.notice_label.text() == "Publisher note"
    assert widget.notice_link.text() == "Details"
    widget.set_entry(_entry("direct-pgr", notice=None))
    assert widget.notice_label.isHidden()
    assert widget.notice_link.isHidden()


def test_installed_button_note_and_signal(qapp):
    widget = StoreDetail(_entry("direct-pgr", installed=True))
    assert widget.install_button.text() == "Add another"
    assert widget.installed_note.text() == "Already in your library."
    received = []
    widget.install_requested.connect(received.append)
    widget.install_button.click()
    assert received == ["direct-pgr"]


def test_back_button_and_escape_emit(qapp):
    widget = StoreDetail(_entry("direct-pgr"))
    received = []
    widget.back_requested.connect(lambda: received.append(True))
    widget.back_button.click()
    QApplication.sendEvent(
        widget,
        QKeyEvent(
            QKeyEvent.Type.KeyPress,
            Qt.Key.Key_Escape,
            Qt.KeyboardModifier.NoModifier,
        ),
    )
    assert received == [True, True]
    assert widget.back_button.accessibleName() == "Back to the store"


def test_primary_button_has_initial_focus(qapp):
    widget = StoreDetail(_entry("direct-pgr"))
    widget.show()
    qapp.processEvents()
    assert widget.focusWidget() is widget.install_button
    widget.close()


def _centre_is_red(widget) -> bool:
    image = widget.hero_label.pixmap().toImage()
    colour = QColor(image.pixel(image.width() // 2, image.height() // 2))
    return colour.red() > 200 and colour.green() < 60 and colour.blue() < 60


def test_placeholder_and_supplied_hero(qapp):
    widget = StoreDetail(_entry("stove-czn"))
    assert widget.hero_label.pixmap() is not None
    assert not widget.hero_label.pixmap().isNull()
    assert not _centre_is_red(widget)
    pixmap = QPixmap(20, 10)
    pixmap.fill(Qt.GlobalColor.red)
    widget.set_hero(pixmap)
    assert _centre_is_red(widget)
    widget.set_hero(None)
    assert not _centre_is_red(widget)


def test_set_entry_drops_the_previous_hero(qapp):
    widget = StoreDetail(_entry("stove-czn"))
    pixmap = QPixmap(20, 10)
    pixmap.fill(Qt.GlobalColor.red)
    widget.set_hero(pixmap)
    assert _centre_is_red(widget)
    widget.set_entry(_entry("direct-pgr"))
    assert not _centre_is_red(widget)


@pytest.mark.parametrize("width", [520, 1400])
def test_layout_fits_width(qapp, width):
    widget = StoreDetail(_entry("launcher-nikke"))
    widget.resize(width, 800)
    widget.show()
    qapp.processEvents()
    # The real layout, not the widget's own size hints, must fit the width ...
    assert widget.layout().minimumSize().width() <= width
    # ... and no visible child may stick out of the right edge.
    for child in widget.findChildren(QWidget):
        if child.isVisible() and child.parent() is widget:
            assert child.geometry().right() <= width, child.objectName() or child.text()
    widget.close()


def test_construction_does_not_open_network(qapp, monkeypatch):
    def forbidden(*args, **kwargs):
        raise AssertionError("store detail attempted network access")

    monkeypatch.setattr(socket, "socket", forbidden)
    StoreDetail(_entry("direct-pgr"))
