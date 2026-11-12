# SPDX-License-Identifier: GPL-3.0-or-later
"""Asset urls must be quoted: an unquoted Qt CSS url() ends at the first space."""

from __future__ import annotations

import importlib.util
import re
import shutil
from pathlib import Path

import pytest

try:  # pragma: no cover - only when PyQt6 is missing
    from PyQt6.QtGui import QColor  # noqa: F401
except Exception:  # noqa: BLE001
    pytest.skip("PyQt6 is not importable in this interpreter", allow_module_level=True)

from nadare.gui import theme


def _load_theme_from(path: Path):
    spec = importlib.util.spec_from_file_location("nadare_theme_with_space", path)
    module = importlib.util.module_from_spec(spec)
    spec.loader.exec_module(module)
    return module


def test_asset_urls_are_quoted_for_a_path_with_a_space(tmp_path):
    root = tmp_path / "Nadare app" / "nadare"
    gui = root / "gui"
    icons = root / "assets" / "icons"
    gui.mkdir(parents=True)
    icons.mkdir(parents=True)
    copied = gui / "theme.py"
    shutil.copy2(theme.__file__, copied)
    for name in ("check.svg", "chevron-down.svg"):
        (icons / name).write_text("<svg xmlns='http://www.w3.org/2000/svg'/>", encoding="utf-8")

    module = _load_theme_from(copied)
    urls = re.findall(r"image:\s*url\(([^)]*)\)", module.stylesheet())
    assert urls, "the stylesheet lost its icon urls"
    unquoted = [url for url in urls if url.strip()[:1] not in "\"'"]
    assert not unquoted, f"unquoted url() breaks on a path with a space: {unquoted}"
