# SPDX-License-Identifier: GPL-3.0-or-later
"""Patch a module-level name of the main window wherever its code uses it (#1248).

MainWindow's methods live in ``gui/main_window.py`` and the mixins in ``gui/main_window_parts/``;
each module looks names up in its own globals. A test that replaces, say, ``AccountNoticeDialog``
means every main-window use of it, so this patches every one of those modules that has the name.
"""

from __future__ import annotations

import importlib
import pkgutil


def patch_main_window(monkeypatch, name: str, value) -> None:
    from nadare.gui import main_window, main_window_parts

    modules = [main_window] + [
        importlib.import_module(f"{main_window_parts.__name__}.{info.name}")
        for info in pkgutil.iter_modules(main_window_parts.__path__)
    ]
    hits = [module for module in modules if name in vars(module)]
    assert hits, f"no main-window module defines {name!r}"
    for module in hits:
        monkeypatch.setattr(module, name, value)
