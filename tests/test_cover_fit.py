# SPDX-License-Identifier: GPL-3.0-or-later
"""Card and tile art fills its slot with a centre crop, without blurred letterboxing."""

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


def test_wide_art_fills_a_portrait_card(tmp_path, qapp):
    """Wide key art is centre-cropped to fill the card, not letterboxed over a blur."""
    src = _striped(tmp_path / "wide.png", 800, 400)  # 2:1
    out = images.decode_scaled(src, 234, 300, crop=True)
    assert (out.width(), out.height()) == (234, 300)
    middle = out.height() // 2
    # Neither strip survives the crop: the card shows the middle of the art, edge to edge.
    assert QColor.fromRgba(out.pixel(1, middle)).red() < 200, "the left edge was cropped away"
    assert QColor.fromRgba(out.pixel(out.width() - 2, middle)).blue() < 200, "and so was the right"
    centre = QColor.fromRgba(out.pixel(out.width() // 2, middle))
    assert abs(centre.red() - 120) < 12 and centre.alpha() == 255


def test_wide_art_is_not_drawn_over_a_dim_blurred_copy(tmp_path, qapp):
    """The old letterbox dimmed a blurred copy behind the art; the card is now the art."""
    src = _striped(tmp_path / "wide.png", 800, 400)
    out = images.decode_scaled(src, 234, 300, crop=True)
    for y in (2, out.height() - 3):
        pixel = QColor.fromRgba(out.pixel(out.width() // 2, y))
        assert abs(pixel.red() - 120) < 12, "no darkened bar above or below the art"
        assert pixel.alpha() == 255


def test_portrait_art_is_still_cropped_to_fill(tmp_path, qapp):
    src = _striped(tmp_path / "tall.png", 400, 600)  # 2:3, same shape as the card
    out = images.decode_scaled(src, 234, 351, crop=True)
    assert (out.width(), out.height()) == (234, 351)
    # No bars: the art fills the card, so the strip is on the very first column.
    assert QColor.fromRgba(out.pixel(1, out.height() // 2)).red() > 200


def test_near_square_art_is_still_cropped(tmp_path, qapp):
    src = _striped(tmp_path / "square.png", 500, 500)
    out = images.decode_scaled(src, 234, 351, crop=True)
    assert QColor.fromRgba(out.pixel(1, out.height() // 2)).red() < 200


def test_small_square_targets_are_never_letterboxed(tmp_path, qapp):
    """Sidebar icons and the like ask for small square crops of whatever the art is."""
    src = _striped(tmp_path / "wide.png", 800, 400)
    out = images.decode_scaled(src, 32, 32, crop=True)
    assert (out.width(), out.height()) == (32, 32)
    assert QColor.fromRgba(out.pixel(1, 16)).red() < 200


def test_contain_mode_is_unchanged(tmp_path, qapp):
    src = _striped(tmp_path / "wide.png", 800, 400)
    out = images.decode_scaled(src, 200, 200, crop=False)
    assert (out.width(), out.height()) == (200, 100)


def test_the_bottom_trim_still_applies_before_fitting(tmp_path, qapp):
    src = _banded(tmp_path / "wide.png", 800, 400)
    full = images.decode_scaled(src, 234, 300, crop=True)
    trimmed = images.decode_scaled(src, 234, 300, crop=True, trim_bottom=0.3)
    assert (trimmed.width(), trimmed.height()) == (234, 300)
    # Both keep the reddish top edge; cutting the bottom 30% takes the blue edge with it.
    assert QColor.fromRgba(full.pixel(117, 1)).red() > 200
    assert QColor.fromRgba(full.pixel(117, 298)).blue() > 200
    assert QColor.fromRgba(trimmed.pixel(117, 298)).blue() < 200


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


def test_a_cinemascope_tile_is_filled_not_letterboxed(tmp_path, qapp):
    """A 3:1 key art on a 16:9 store tile is cropped, the way the portrait card is."""
    src = _striped(tmp_path / "cinemascope.png", 1200, 400)
    out = images.decode_scaled(src, 330, 186, crop=True)
    assert (out.width(), out.height()) == (330, 186)
    middle = out.height() // 2
    assert QColor.fromRgba(out.pixel(1, middle)).red() < 200, "the strip was cropped away"
    assert QColor.fromRgba(out.pixel(165, 2)).alpha() == 255, "no blurred bar above the art"


def test_a_portrait_poster_on_a_tile_is_cropped_too(tmp_path, qapp):
    src = _banded(tmp_path / "tall.png", 400, 600)
    out = images.decode_scaled(src, 330, 186, crop=True)
    assert QColor.fromRgba(out.pixel(out.width() // 2, 1)).red() < 200
    assert QColor.fromRgba(out.pixel(1, out.height() // 2)).alpha() == 255


def test_transparent_art_keeps_its_transparency_when_the_card_is_cropped(tmp_path, qapp):
    """A logo-style PNG has no bars to fill any more: the card behind it shows through."""
    image = QImage(800, 400, QImage.Format.Format_ARGB32)
    image.fill(QColor(0, 0, 0, 0))
    painter = QPainter(image)
    painter.fillRect(300, 150, 200, 100, QColor(255, 0, 0, 255))
    painter.end()
    path = tmp_path / "logo.png"
    assert image.save(str(path))
    out = images.decode_scaled(str(path), 234, 300, crop=True)
    assert QColor.fromRgba(out.pixel(2, 2)).alpha() == 0
    assert QColor.fromRgba(out.pixel(117, 150)).red() > 200
