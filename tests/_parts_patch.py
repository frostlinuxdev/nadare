# SPDX-License-Identifier: GPL-3.0-or-later
"""Patch a module-level name wherever a split class's code uses it (#1248).

A big class split into mixins lives in ``<module>.py`` plus ``<module>_parts/``; each module looks
names up in its own globals. A test that replaces a name of the module means every use of it, so
this patches the module and every part module that has the name.
"""

from __future__ import annotations

import importlib
import pkgutil


def patch_everywhere(monkeypatch, module, name: str, value) -> None:
    modules = [module]
    try:
        parts = importlib.import_module(f"{module.__name__}_parts")
    except ImportError:
        parts = None
    if parts is not None:
        modules += [
            importlib.import_module(f"{parts.__name__}.{info.name}")
            for info in pkgutil.iter_modules(parts.__path__)
        ]
    hits = [m for m in modules if name in vars(m)]
    assert hits, f"no module of {module.__name__} defines {name!r}"
    for m in hits:
        monkeypatch.setattr(m, name, value)
