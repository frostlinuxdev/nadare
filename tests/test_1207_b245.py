# SPDX-License-Identifier: GPL-3.0-or-later
"""Asset urls must be quoted: an unquoted Qt CSS url() ends at the first space."""

from __future__ import annotations

import importlib
import re
import shutil
import sys
from pathlib import Path

import pytest

try:  # pragma: no cover - only when PyQt6 is missing
    from PyQt6.QtGui import QColor  # noqa: F401
except Exception:  # noqa: BLE001
    pytest.skip("PyQt6 is not importable in this interpreter", allow_module_level=True)

from nadare.gui import theme, theme_qss

PACKAGE = "nadare_theme_with_space"


def _load_theme_package(parent: Path):
    """Import the copied ``PACKAGE.gui.theme`` from ``parent`` and return it."""
    sys.path.insert(0, str(parent))
    try:
        module = importlib.import_module(f"{PACKAGE}.gui.theme")
        module.STYLESHEET  # noqa: B018 - load the copied theme_qss while it is importable
        return module
    finally:
        sys.path.remove(str(parent))
        for name in [key for key in sys.modules if key == PACKAGE or key.startswith(f"{PACKAGE}.")]:
            del sys.modules[name]


def test_asset_urls_are_quoted_for_a_path_with_a_space(tmp_path):
    package = tmp_path / "Nadare app" / PACKAGE
    gui = package / "gui"
    icons = package / "assets" / "icons"
    gui.mkdir(parents=True)
    icons.mkdir(parents=True)
    (package / "__init__.py").write_text("", encoding="utf-8")
    (gui / "__init__.py").write_text("", encoding="utf-8")
    # The stylesheet lives in theme_qss since #1601; the copy needs both modules.
    shutil.copy2(theme.__file__, gui / "theme.py")
    shutil.copy2(theme_qss.__file__, gui / "theme_qss.py")
    for name in ("check.svg", "chevron-down.svg"):
        (icons / name).write_text("<svg xmlns='http://www.w3.org/2000/svg'/>", encoding="utf-8")

    module = _load_theme_package(package.parent)
    urls = re.findall(r"image:\s*url\(([^)]*)\)", module.stylesheet())
    assert urls, "the stylesheet lost its icon urls"
    unquoted = [url for url in urls if url.strip()[:1] not in "\"'"]
    assert not unquoted, f"unquoted url() breaks on a path with a space: {unquoted}"
