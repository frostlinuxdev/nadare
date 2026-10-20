"""Import-direction guard: core <- backends <- gui/app; nothing reaches back."""

from __future__ import annotations

import ast
from pathlib import Path

SRC = Path(__file__).resolve().parents[1] / "src" / "nadare"


def _imports(path: Path) -> set[str]:
    """Absolute ``nadare.*`` module names imported anywhere in ``path``."""
    pkg = ".".join(["nadare", *path.relative_to(SRC).parent.parts])
    found: set[str] = set()
    for node in ast.walk(ast.parse(path.read_text())):
        if isinstance(node, ast.Import):
            found.update(a.name for a in node.names)
        elif isinstance(node, ast.ImportFrom):
            if node.level:
                base = pkg.split(".")
                base = base[: len(base) - (node.level - 1)]
                module = ".".join([*base, node.module] if node.module else base)
            else:
                module = node.module or ""
            found.add(module)
            found.update(f"{module}.{a.name}" for a in node.names)
    return {m for m in found if m.startswith("nadare")}


def _violations(folder: str, forbidden: tuple[str, ...]) -> list[str]:
    bad = []
    for path in sorted((SRC / folder).rglob("*.py")):
        for mod in _imports(path):
            if mod.startswith(forbidden):
                bad.append(f"{path.relative_to(SRC)} imports {mod}")
    return bad


def test_core_does_not_import_backends_gui_or_app():
    assert _violations("core", ("nadare.backends", "nadare.gui", "nadare.app")) == []


def test_backends_do_not_import_gui_or_app():
    assert _violations("backends", ("nadare.gui", "nadare.app")) == []


def test_gui_does_not_import_a_concrete_backend():
    assert _violations("gui", ("nadare.backends",)) == []


def test_no_backend_imports_private_names_from_a_sibling_backend():
    """Shared helpers must be public: ``from .wine import _x`` is a smell."""
    bad = []
    for path in sorted((SRC / "backends").rglob("*.py")):
        tree = ast.parse(path.read_text())
        for node in ast.walk(tree):
            if isinstance(node, ast.ImportFrom) and node.level and node.module:
                # Same-package siblings (``.wine``) and the parent package (``..core``).
                if node.module.startswith("core") or node.level > 1:
                    continue
                for alias in node.names:
                    if alias.name.startswith("_") and not alias.name.startswith("__"):
                        bad.append(
                            f"{path.relative_to(SRC)}: from .{node.module} import {alias.name}"
                        )
    assert bad == []
