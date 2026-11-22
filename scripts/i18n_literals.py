#!/usr/bin/env python3
# SPDX-License-Identifier: GPL-3.0-or-later
# Copyright (C) 2026 Nadare contributors
"""Ratchet against new untranslated GUI strings (#1602 part 2).

The scan is AST-based. It counts string literals passed directly as text to
the Qt calls that show something: the first positional argument of the usual
widget constructors and setters, and the title and text of the
``QMessageBox`` static dialogs. Empty strings and strings without a letter
are ignored.

``scripts/i18n_literals_baseline.json`` records the count per file when the
ratchet came in. A file may keep or lower its count; growing it, or adding a
file with literals, fails. Lower a stale baseline with::

    scripts/i18n_literals.py --update-baseline

The check itself never rewrites the baseline.
"""

from __future__ import annotations

import argparse
import ast
import json
import sys
from pathlib import Path

ROOT = Path(__file__).resolve().parents[1]
GUI_DIR = Path("src") / "nadare" / "gui"
BASELINE = Path("scripts") / "i18n_literals_baseline.json"

WIDGET_CONSTRUCTORS = frozenset(
    {
        "QLabel",
        "QPushButton",
        "QCheckBox",
        "QRadioButton",
        "QGroupBox",
        "QAction",
        "QToolButton",
    }
)
TEXT_SETTERS = frozenset(
    {
        "setText",
        "setToolTip",
        "setPlaceholderText",
        "setWindowTitle",
        "setStatusTip",
        "setTitle",
        "addItem",
    }
)
MESSAGE_BOX_DIALOGS = frozenset({"information", "warning", "critical", "question"})


def _text_literal(node: ast.AST | None) -> str | None:
    if isinstance(node, ast.Constant) and isinstance(node.value, str):
        text = node.value
        if text and any(char.isalpha() for char in text):
            return text
    return None


def _call_name(node: ast.AST) -> str | None:
    if isinstance(node, ast.Name):
        return node.id
    if isinstance(node, ast.Attribute):
        return node.attr
    return None


def literals_in_file(path: Path) -> list[tuple[int, str]]:
    """``(line, literal)`` for every untranslated text literal in ``path``."""
    try:
        tree = ast.parse(path.read_text(encoding="utf-8"), filename=str(path))
    except (OSError, SyntaxError, UnicodeError) as exc:
        raise ValueError(f"cannot scan {path}: {exc}") from exc
    found: list[tuple[int, str]] = []
    for node in ast.walk(tree):
        if not isinstance(node, ast.Call):
            continue
        name = _call_name(node.func)
        arguments: list[ast.AST] = []
        if name in WIDGET_CONSTRUCTORS or name in TEXT_SETTERS:
            arguments = node.args[:1]
        elif (
            name in MESSAGE_BOX_DIALOGS
            and isinstance(node.func, ast.Attribute)
            and _call_name(node.func.value) == "QMessageBox"
        ):
            arguments = node.args[1:3]
        for argument in arguments:
            text = _text_literal(argument)
            if text is not None:
                found.append((argument.lineno, text))
    return sorted(found)


def literals(root: Path) -> dict[str, list[tuple[int, str]]]:
    """Literals per file under ``root``, keyed by a root-relative path."""
    root = Path(root)
    folder = root / GUI_DIR
    out: dict[str, list[tuple[int, str]]] = {}
    if not folder.is_dir():
        return out
    for path in sorted(folder.rglob("*.py")):
        found = literals_in_file(path)
        if found:
            out[path.relative_to(root).as_posix()] = found
    return out


def read_baseline(path: Path) -> dict[str, int]:
    path = Path(path)
    if not path.is_file():
        return {}
    raw = json.loads(path.read_text(encoding="utf-8"))
    if not isinstance(raw, dict):
        raise ValueError(f"{path} is not a JSON object of file counts")
    return {str(name): int(count) for name, count in raw.items()}


def _detail(name: str, found: list[tuple[int, str]]) -> list[str]:
    return [f'  {name}:{line}: "{text}"' for line, text in found]


def check(
    current: dict[str, list[tuple[int, str]]], baseline: dict[str, int]
) -> list[str]:
    """Fatal problems: a file grew, or a new file has literals."""
    problems: list[str] = []
    for name, found in sorted(current.items()):
        known = baseline.get(name)
        if known is None:
            problems.append(
                f"{name}: new file has {len(found)} untranslated literal(s); "
                "wrap them in tr() and add the keys to "
                "src/nadare/assets/locales/en.json"
            )
            problems.extend(_detail(name, found))
        elif len(found) > known:
            problems.append(
                f"{name}: {known} -> {len(found)} untranslated literal(s); "
                "wrap the new ones in tr() and add the keys to "
                "src/nadare/assets/locales/en.json"
            )
            problems.extend(_detail(name, found))
    return problems


def hints(
    current: dict[str, list[tuple[int, str]]], baseline: dict[str, int]
) -> list[str]:
    """Non-fatal notes: a file shrank, so the baseline can be lowered."""
    out: list[str] = []
    for name, known in sorted(baseline.items()):
        count = len(current.get(name, []))
        if count < known:
            out.append(
                f"{name}: {count} untranslated literal(s) now (baseline {known}); "
                "lower the baseline with --update-baseline"
            )
    return out


def main(argv: list[str] | None = None) -> int:
    parser = argparse.ArgumentParser(description=__doc__.splitlines()[0])
    parser.add_argument("--root", type=Path, default=ROOT, help="tree to scan")
    parser.add_argument(
        "--baseline",
        type=Path,
        default=None,
        help=f"file counts to compare with (default: <root>/{BASELINE.as_posix()})",
    )
    parser.add_argument(
        "--update-baseline",
        action="store_true",
        help="rewrite the baseline from the current tree",
    )
    args = parser.parse_args(argv)
    baseline_path = args.baseline or Path(args.root) / BASELINE

    try:
        current = literals(args.root)
        baseline = read_baseline(baseline_path)
    except (OSError, ValueError) as exc:
        print(f"i18n literals: {exc}", file=sys.stderr)
        return 2

    if args.update_baseline:
        counts = {name: len(found) for name, found in sorted(current.items())}
        baseline_path.parent.mkdir(parents=True, exist_ok=True)
        baseline_path.write_text(
            json.dumps(counts, indent=2, sort_keys=True) + "\n", encoding="utf-8"
        )
        print(f"baseline: {sum(counts.values())} literal(s) in {len(counts)} file(s)")
        return 0

    problems = check(current, baseline)
    for line in problems:
        print(line)
    if problems:
        print("i18n literals: new untranslated text; see the lines above")
        return 1
    for line in hints(current, baseline):
        print(line)
    total = sum(len(found) for found in current.values())
    print(f"i18n literals: {total} literal(s) in {len(current)} file(s)")
    return 0


if __name__ == "__main__":
    raise SystemExit(main())
