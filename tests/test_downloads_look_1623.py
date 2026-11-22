# SPDX-License-Identifier: GPL-3.0-or-later
"""Downloads page: each row shows the game's cover, and the empty page says what to do (#1623)."""

from __future__ import annotations

import os

os.environ.setdefault("QT_QPA_PLATFORM", "offscreen")

import pytest

try:
    from PyQt6.QtGui import QColor, QImage
    from PyQt6.QtWidgets import QApplication
except Exception:  # pragma: no cover - optional GUI dependencies
    pytest.skip("PyQt6 is not importable in this interpreter", allow_module_level=True)

from nadare.gui.downloads_page import DownloadRow, DownloadsPage


@pytest.fixture(scope="module")
def qapp():
    return QApplication.instance() or QApplication([])


@pytest.fixture
def page(qapp):
    widget = DownloadsPage()
    widget.resize(900, 600)
    widget.show()
    yield widget
    widget.close()
    widget.deleteLater()
    qapp.processEvents()


def _cover(tmp_path, name="cover.png", color="red"):
    path = tmp_path / name
    image = QImage(60, 80, QImage.Format.Format_RGB32)
    image.fill(QColor(color))
    assert image.save(str(path))
    return str(path)


def _row(profile="a", art=(), **kw):
    return DownloadRow(
        profile=profile,
        title=profile.upper(),
        operation="install",
        state="running",
        fraction=0.3,
        art=tuple(art),
        **kw,
    )


def _center_color(widget):
    pixmap = widget.thumb.pixmap()
    assert pixmap is not None and not pixmap.isNull()
    image = pixmap.toImage()
    return image.pixelColor(image.width() // 2, image.height() // 2)


def test_a_row_shows_a_placeholder_before_any_cover(page):
    page.set_rows([_row("a")])
    widget = page.row("a")
    assert widget.thumb.isVisibleTo(page)
    assert widget.thumb.pixmap() is not None and not widget.thumb.pixmap().isNull()


def test_the_cover_is_decoded_off_the_gui_thread_and_shown(page, tmp_path):
    page.set_rows([_row("a", art=[_cover(tmp_path)])])
    page.wait_for_art()
    QApplication.processEvents()
    assert _center_color(page.row("a")).red() > 200


def test_a_missing_file_falls_through_to_the_next_candidate(page, tmp_path):
    good = _cover(tmp_path, color="blue")
    page.set_rows([_row("a", art=[str(tmp_path / "gone.png"), good])])
    page.wait_for_art()
    QApplication.processEvents()
    assert _center_color(page.row("a")).blue() > 200


def test_an_unchanged_row_does_not_decode_again(page, tmp_path, monkeypatch):
    art = [_cover(tmp_path)]
    page.set_rows([_row("a", art=art)])
    page.wait_for_art()
    calls = []
    real = page._images.request
    monkeypatch.setattr(page._images, "request", lambda *a, **k: calls.append(a) or real(*a, **k))
    page.set_rows([_row("a", art=art, detail="Unpacking")])  # progress ticks re-apply rows
    assert calls == []


def test_the_empty_page_has_a_heading_and_a_hint(page):
    page.set_rows([])
    assert page.empty_title.isVisibleTo(page)
    assert page.empty_title.text() == "Nothing downloading"
    assert "Store" in page.empty_label.text()
    page.set_rows([_row("a")])
    assert not page.empty_title.isVisibleTo(page)
    assert not page.empty_label.isVisibleTo(page)


def test_the_window_gives_each_row_the_librarys_cover_candidates(monkeypatch):
    from nadare.gui import main_window as mw

    seen = []
    monkeypatch.setattr(mw, "profile_art_candidates", lambda p: seen.append(p) or ["/x.png"])

    class _W:
        config = type("C", (), {"profiles": {"g": {"title": "G"}, "bad": "x"}})()

    assert mw.MainWindow._download_art(_W(), "g") == ("/x.png",)
    assert mw.MainWindow._download_art(_W(), "bad") == ()
    assert mw.MainWindow._download_art(_W(), "gone") == ()
    assert seen == [{"title": "G"}]
