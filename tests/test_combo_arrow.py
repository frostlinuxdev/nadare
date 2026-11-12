# SPDX-License-Identifier: GPL-3.0-or-later
"""Drop-down arrows are an icon: Qt draws the CSS border-triangle trick as a small "=" bar."""

from __future__ import annotations

import re
from pathlib import Path

from nadare.gui import theme


def test_the_combo_arrow_is_an_svg_that_ships_with_nadare():
    sheet = theme.stylesheet()
    block = sheet[sheet.index("QComboBox::down-arrow") :]
    block = block[: block.index("}")]
    match = re.search(r"image:\s*url\([\"']?([^)\"']+)[\"']?\)", block)  # quoted since #1207 b245
    assert match, block
    icon = Path(match.group(1))
    assert icon.is_file() and icon.suffix == ".svg"
    assert "assets/icons" in icon.as_posix()
    assert "border-top" not in block  # the triangle trick is gone
