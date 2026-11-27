# SPDX-License-Identifier: GPL-3.0-or-later
"""#1601 parts 4+5: the library cards and the theme stylesheet live in their own modules.

The split is move-only: every moved name is still reachable through
``nadare.gui.library`` / ``nadare.gui.theme`` as the *same object*, the stylesheet
bytes are unchanged, and both old modules are back under 1000 lines.
"""

from __future__ import annotations

import hashlib
import os
import subprocess
import sys
from pathlib import Path

os.environ.setdefault("QT_QPA_PLATFORM", "offscreen")

import pytest

try:  # pragma: no cover - exercised only when PyQt6 is missing
    from PyQt6.QtWidgets import QApplication  # noqa: F401
except Exception:  # noqa: BLE001
    pytest.skip("PyQt6 is not importable in this interpreter", allow_module_level=True)

# theme first: theme_qss reads theme's constants at import time.
from nadare.gui import library, library_cards, theme, theme_qss

ROOT = Path(__file__).resolve().parent.parent

#: sha256 of ``nadare.gui.theme.STYLESHEET`` on the tree before the stylesheet moved.
# sha256 of the pre-split STYLESHEET with the absolute icon folder replaced by "<ICONS>"
# (the urls embed the checkout path, which differs between machines).
STYLESHEET_SHA256 = "49729ad73f226b7c131d5d6ca5a8950eacbc81162850416d13901e99c30bd2c3"

MOVED_TO_CARDS = (
    "FlowLayout",
    "TileLayout",
    "CoverLabel",
    "GameCard",
    "AddGameCard",
    "RecentTile",
    "CARD_SCALE",
    "NEW_DAYS",
    "RECENT_MAX",
    "TILE_WIDTH",
    "TILE_ART_H",
    "TILE_HEIGHT",
    "CARD_WIDTH",
    "CARD_ART_H",
    "CARD_HEIGHT",
    "_FOCUS_RING",
    "_BACKEND_LABELS",
    "backend_label",
    "card_width_for",
    "tile_width_for",
    "_tile_text",
    "_name_clear_of_play",
    "_elide_name",
    "_place_badge",
    "_paint_scrim",
    "_drop_shadow",
    "_parse_stamp",
    "_ago",
    "_is_new",
)

MOVED_TO_QSS = (
    "STYLESHEET",
    "_qss_url",
    "_CHECK_ICON",
    "_CHECK_IMAGE",
    "_ARROW_ICON",
    "_ARROW_IMAGE",
    "_UP_ARROW_ICON",
    "_UP_ARROW_IMAGE",
)


def test_library_defines_the_card_names():
    missing = [name for name in MOVED_TO_CARDS if not hasattr(library_cards, name)]
    assert missing == []


def test_library_reexports_the_card_names_as_the_same_objects():
    missing = [name for name in MOVED_TO_CARDS if not hasattr(library, name)]
    assert missing == []
    different = [
        name
        for name in MOVED_TO_CARDS
        if getattr(library, name) is not getattr(library_cards, name)
    ]
    assert different == []


def test_library_cards_does_not_import_the_library_module():
    """No circular import: importing the cards alone must not pull in LibraryPage."""
    result = subprocess.run(
        [
            sys.executable,
            "-c",
            "import sys; import nadare.gui.library_cards; "
            "print('nadare.gui.library' in sys.modules)",
        ],
        cwd=ROOT,
        env={**os.environ, "QT_QPA_PLATFORM": "offscreen"},
        capture_output=True,
        text=True,
        timeout=120,
    )
    assert result.returncode == 0, result.stderr
    assert result.stdout.strip() == "False"


def test_theme_qss_defines_the_stylesheet_names():
    missing = [name for name in MOVED_TO_QSS if not hasattr(theme_qss, name)]
    assert missing == []


def test_theme_reexports_the_stylesheet_names_as_the_same_objects():
    missing = [name for name in MOVED_TO_QSS if not hasattr(theme, name)]
    assert missing == []
    different = [
        name for name in MOVED_TO_QSS if getattr(theme, name) is not getattr(theme_qss, name)
    ]
    assert different == []


def test_stylesheet_bytes_are_unchanged():
    assert theme.STYLESHEET == theme_qss.STYLESHEET
    icons = str(Path(theme.__file__).resolve().parent.parent / "assets" / "icons")
    assert icons in theme.STYLESHEET
    portable = theme.STYLESHEET.replace(icons, "<ICONS>")
    digest = hashlib.sha256(portable.encode("utf-8")).hexdigest()
    assert digest == STYLESHEET_SHA256


def test_both_old_modules_are_under_1000_lines():
    sizes = {
        module.__name__: len(Path(module.__file__).read_text(encoding="utf-8").splitlines())
        for module in (library, theme)
    }
    too_long = {name: lines for name, lines in sizes.items() if lines >= 1000}
    assert too_long == {}


def test_theme_qss_imports_first_in_a_fresh_interpreter():
    """Importing theme_qss before theme must work (no import cycle)."""
    import subprocess
    import sys

    code = (
        "import nadare.gui.theme_qss as q, nadare.gui.theme as t;"
        "assert t.STYLESHEET is q.STYLESHEET and t._qss_url is q._qss_url"
    )
    run = subprocess.run([sys.executable, "-c", code], capture_output=True, text=True)
    assert run.returncode == 0, run.stderr
