# SPDX-License-Identifier: GPL-3.0-or-later
"""UI polish: card art without the baked news banner, bundled font, radio styling."""

from __future__ import annotations

import os
import time

os.environ.setdefault("QT_QPA_PLATFORM", "offscreen")

import pytest

try:  # pragma: no cover
    from PyQt6.QtGui import QColor, QFontDatabase, QImage
    from PyQt6.QtWidgets import QApplication
except Exception:  # noqa: BLE001
    pytest.skip("PyQt6 is not importable", allow_module_level=True)

from nadare.gui import images, theme

# This file restyles the whole app: end earlier tests' windows first (#1024).
pytestmark = pytest.mark.usefixtures("restyle_safe")


@pytest.fixture(scope="module")
def qapp():
    app = QApplication.instance() or QApplication([])
    yield app


def _two_tone(path, top=0x2060C0, bottom=0xE02020, w=80, h=60):
    img = QImage(w, h, QImage.Format.Format_RGB32)
    img.fill(top)
    for y in range(int(h * 0.70), h):
        for x in range(w):
            img.setPixel(x, y, bottom | 0xFF000000)
    img.save(str(path))


def test_banner_trim_only_for_news_thumbnails():
    assert images.banner_trim_for("/c/chaos-zero-nightmare-news-16cf1e7c615e192c-thumb.png") > 0
    assert images.banner_trim_for("/c/chaos-zero-nightmare-official-hero.jpg") == 0
    assert images.banner_trim_for("/c/mycover.png") == 0
    assert images.banner_trim_for(None) == 0


def test_decode_scaled_drops_the_bottom_strip(tmp_path, qapp):
    src = tmp_path / "t.png"
    _two_tone(src)
    full = images.decode_scaled(str(src), 40, 30, crop=False)
    trimmed = images.decode_scaled(str(src), 40, 30, crop=False, trim_bottom=0.30)
    assert full is not None and trimmed is not None
    # the red strip is gone from the trimmed image
    bottom_row = [QColor(trimmed.pixel(x, trimmed.height() - 1)) for x in range(trimmed.width())]
    assert all(c.red() < 0x80 for c in bottom_row)
    full_bottom = QColor(full.pixel(0, full.height() - 1))
    assert full_bottom.red() > 0xC0


def test_load_card_pixmap_trims_news_thumbs_only(tmp_path, qapp):
    news = tmp_path / "chaos-zero-nightmare-news-abcdef0123456789-thumb.png"
    plain = tmp_path / "cover.png"
    _two_tone(news)
    _two_tone(plain)
    assert theme.load_card_pixmap([str(news)]).height() == int(60 * 0.70)
    assert theme.load_card_pixmap([str(plain)]).height() == 60
    assert theme.load_card_pixmap([None, str(tmp_path / "missing.png"), str(plain)]).height() == 60


def test_image_loader_passes_the_trim_only_for_covers(tmp_path, qapp):
    news = tmp_path / "chaos-zero-nightmare-news-abcdef0123456789-thumb.png"
    _two_tone(news)
    calls = []

    def fake_decode(path, w, h, *, crop=True, trim_bottom=0.0):
        calls.append(trim_bottom)
        return QImage(w, h, QImage.Format.Format_RGB32)

    loader = images.ImageLoader(decode=fake_decode)
    got = []
    loader.request(str(news), 20, 20, got.append, cover=True)
    loader.request(str(news), 30, 30, got.append, cover=False)
    # Decoding runs on a worker thread: wait on a deadline, not a fixed number
    # of event-loop spins (which can finish before the thread does).
    deadline = time.monotonic() + 10.0
    while len(got) < 2 and time.monotonic() < deadline:
        qapp.processEvents()
        time.sleep(0.01)
    assert len(got) == 2
    assert sorted(calls) == [0.0, images.NEWS_BANNER_TRIM]


def test_inter_is_bundled_and_registered(qapp):
    assert (theme.FONTS_DIR / "Inter.ttf").is_file()
    assert (theme.FONTS_DIR / "OFL.txt").is_file()
    theme.apply_theme(qapp)
    assert "Inter" in QFontDatabase.families()


def test_stylesheet_styles_radio_buttons_with_the_accent():
    css = theme.STYLESHEET
    assert "QRadioButton::indicator:checked" in css
    block = css.split("QRadioButton::indicator:checked", 1)[1].split("}", 1)[0]
    assert theme.ACCENT in block


def test_dialog_primary_buttons_do_not_use_page_sized_padding():
    css = theme.STYLESHEET
    assert "QDialog QPushButton#Primary" in css


def test_image_loader_falls_back_when_the_first_existing_file_does_not_decode(tmp_path, qapp):
    """#918: a damaged custom background used to hide every valid cover behind it."""
    broken, good = tmp_path / "custom.png", tmp_path / "cover.png"
    broken.write_bytes(b"not an image")
    good.write_bytes(b"also stand-in bytes")

    def fake_decode(path, w, h, *, crop=True, trim_bottom=0.0, fit=False):
        if str(path) == str(broken):
            return None
        return QImage(w, h, QImage.Format.Format_RGB32)

    loader = images.ImageLoader(decode=fake_decode)
    got: list = []
    loader.request([str(broken), str(good)], 20, 20, got.append)
    deadline = time.monotonic() + 10.0
    while not got and time.monotonic() < deadline:
        qapp.processEvents()
        time.sleep(0.01)
    assert len(got) == 1 and got[0] is not None


def test_image_loader_reports_none_once_every_candidate_fails(tmp_path, qapp):
    only = tmp_path / "custom.png"
    only.write_bytes(b"x")
    loader = images.ImageLoader(decode=lambda *a, **k: None)
    got: list = []
    loader.request([str(only)], 20, 20, got.append)
    deadline = time.monotonic() + 10.0
    while not got and time.monotonic() < deadline:
        qapp.processEvents()
        time.sleep(0.01)
    assert got == [None]
