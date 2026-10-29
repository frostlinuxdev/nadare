# SPDX-License-Identifier: GPL-3.0-or-later
"""Wide key art is shown whole on a portrait card instead of being centre-cropped."""

from __future__ import annotations

import os

os.environ.setdefault("QT_QPA_PLATFORM", "offscreen")

import pytest

try:  # pragma: no cover - only when PyQt6 is missing
    from PyQt6.QtGui import QColor, QImage, QPainter
except Exception:  # noqa: BLE001
    pytest.skip("PyQt6 is not importable in this interpreter", allow_module_level=True)

from nadare.gui import images


def _striped(path, width, height):
    """Grey picture with a red strip on its far left and a blue strip on its far right."""
    image = QImage(width, height, QImage.Format.Format_RGB32)
    image.fill(QColor(120, 120, 120))
    painter = QPainter(image)
    painter.fillRect(0, 0, width // 20, height, QColor(255, 0, 0))
    painter.fillRect(width - width // 20, 0, width // 20, height, QColor(0, 0, 255))
    painter.end()
    assert image.save(str(path))
    return str(path)


def test_wide_art_is_kept_whole_on_a_portrait_card(tmp_path, qapp):
    src = _striped(tmp_path / "wide.png", 800, 400)  # 2:1
    out = images.decode_scaled(src, 234, 300, crop=True)
    assert (out.width(), out.height()) == (234, 300)
    middle = out.height() // 2
    left, right = QColor(out.pixel(1, middle)), QColor(out.pixel(out.width() - 2, middle))
    assert left.red() > 200 and left.blue() < 80, "the left edge of the art must be visible"
    assert right.blue() > 200 and right.red() < 80, "the right edge of the art must be visible"


def test_the_bars_around_wide_art_are_filled_not_transparent(tmp_path, qapp):
    src = _striped(tmp_path / "wide.png", 800, 400)
    out = images.decode_scaled(src, 234, 300, crop=True)
    for y in (2, out.height() - 3):
        assert QColor(out.pixel(out.width() // 2, y)).alpha() == 255


def test_portrait_art_is_still_cropped_to_fill(tmp_path, qapp):
    src = _striped(tmp_path / "tall.png", 400, 600)  # 2:3, same shape as the card
    out = images.decode_scaled(src, 234, 351, crop=True)
    assert (out.width(), out.height()) == (234, 351)
    # No bars: the art fills the card, so the strip is on the very first column.
    assert QColor(out.pixel(1, out.height() // 2)).red() > 200


def test_near_square_art_is_still_cropped(tmp_path, qapp):
    src = _striped(tmp_path / "square.png", 500, 500)
    out = images.decode_scaled(src, 234, 351, crop=True)
    assert QColor(out.pixel(1, out.height() // 2)).red() < 200, "square art keeps the old crop"


def test_small_square_targets_are_never_letterboxed(tmp_path, qapp):
    """Sidebar icons and the like ask for small square crops of whatever the art is."""
    src = _striped(tmp_path / "wide.png", 800, 400)
    out = images.decode_scaled(src, 32, 32, crop=True)
    assert (out.width(), out.height()) == (32, 32)
    assert QColor(out.pixel(1, 16)).red() < 200


def test_contain_mode_is_unchanged(tmp_path, qapp):
    src = _striped(tmp_path / "wide.png", 800, 400)
    out = images.decode_scaled(src, 200, 200, crop=False)
    assert (out.width(), out.height()) == (200, 100)


def test_the_bottom_trim_still_applies_before_fitting(tmp_path, qapp):
    src = _striped(tmp_path / "wide.png", 800, 400)
    full = images.decode_scaled(src, 234, 300, crop=True)
    trimmed = images.decode_scaled(src, 234, 300, crop=True, trim_bottom=0.3)
    assert (trimmed.width(), trimmed.height()) == (234, 300)
    assert full != trimmed


def _banded(path, width, height):
    """Grey picture with a red strip along its top edge and a blue one along its bottom edge."""
    image = QImage(width, height, QImage.Format.Format_RGB32)
    image.fill(QColor(120, 120, 120))
    painter = QPainter(image)
    painter.fillRect(0, 0, width, height // 20, QColor(255, 0, 0))
    painter.fillRect(0, height - height // 20, width, height // 20, QColor(0, 0, 255))
    painter.end()
    assert image.save(str(path))
    return str(path)


def test_portrait_art_is_shown_whole_on_a_landscape_card_when_asked(tmp_path, qapp):
    src = _banded(tmp_path / "tall.png", 400, 600)
    out = images.decode_scaled(src, 330, 186, crop=True, fit=True)
    assert (out.width(), out.height()) == (330, 186)
    middle = out.width() // 2
    assert QColor(out.pixel(middle, 1)).red() > 200, "the top edge of the art must be visible"
    assert QColor(out.pixel(middle, out.height() - 2)).blue() > 200, "and the bottom edge"
    assert QColor(out.pixel(2, out.height() // 2)).alpha() == 255, "the side bars are filled"


def test_without_the_flag_a_landscape_target_still_crops(tmp_path, qapp):
    src = _banded(tmp_path / "tall.png", 400, 600)
    out = images.decode_scaled(src, 330, 186, crop=True)
    assert QColor(out.pixel(out.width() // 2, 1)).red() < 200, "centre crop loses the top strip"


def test_wide_art_fills_a_landscape_card_without_bars(tmp_path, qapp):
    src = _striped(tmp_path / "wide.png", 1600, 900)  # 16:9, same shape as the card
    out = images.decode_scaled(src, 330, 186, crop=True, fit=True)
    assert QColor(out.pixel(1, out.height() // 2)).red() > 200
    assert QColor(out.pixel(out.width() - 2, out.height() // 2)).blue() > 200


def test_the_loader_passes_fit_through_and_caches_it_separately(tmp_path, qapp):
    src = _banded(tmp_path / "tall.png", 400, 600)
    loader = images.ImageLoader()
    got = {}
    loader.request([src], 330, 186, lambda p: got.setdefault("plain", p), cover=True)
    loader.request([src], 330, 186, lambda p: got.setdefault("fit", p), cover=True, fit=True)
    loader.wait()
    assert got["plain"] is not None and got["fit"] is not None
    assert got["plain"].toImage() != got["fit"].toImage()


def test_the_bars_stay_opaque_even_when_the_art_has_transparent_pixels(tmp_path, qapp):
    """A logo-style PNG must not let the page show through the bars."""
    image = QImage(800, 400, QImage.Format.Format_ARGB32)
    image.fill(QColor(0, 0, 0, 0))
    painter = QPainter(image)
    painter.fillRect(300, 150, 200, 100, QColor(255, 0, 0, 255))
    painter.end()
    path = tmp_path / "logo.png"
    assert image.save(str(path))
    out = images.decode_scaled(str(path), 234, 300, crop=True)
    for x, y in ((2, 2), (117, 10), (117, 290), (230, 150), (2, 290)):
        assert QColor(out.pixel(x, y)).alpha() == 255, (x, y)
