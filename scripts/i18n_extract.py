#!/usr/bin/env python3
# SPDX-License-Identifier: GPL-3.0-or-later
# Copyright (C) 2026 Nadare contributors
"""Report catalogue keys that the source uses, and shipped keys nothing uses.

The source is scanned read-only for the first (string) argument of the existing
translate calls: ``<i18n module>.translate(...)``, ``<i18n module>.tr(...)`` and
helpers imported from an i18n module (``translate``, ``tr``, ``_``). Plural helpers
(``translate_plural``, ``tr_n``, ``_n``) use the same key resolution and
not-checkable rules. ``--check`` also validates plural/plain catalogue shapes. The scan is
static, so a key built at run time or passed through a ``Translator`` instance
cannot be found; such calls are skipped, not guessed.

A bare name is followed to a module-level constant when that name is bound
exactly once in the module to a string literal (parentheses and implicit
concatenation included). Any other non-literal argument is listed as
"not checkable: file:line" and fails ``--check``; put ``# i18n: dynamic`` on the
call when the key really is built at run time.

Usage::

    scripts/i18n_extract.py                 # report only
    scripts/i18n_extract.py --check         # exit 1 when a used key is missing
    scripts/i18n_extract.py --src DIR --catalogue FILE

The script never edits the catalogue: unused keys are reported, not removed.
"""

from __future__ import annotations

import argparse
import ast
import json
import sys
from pathlib import Path
from typing import NamedTuple

ROOT = Path(__file__).resolve().parents[1]
DEFAULT_SRC = ROOT / "src"
DEFAULT_CATALOGUE = ROOT / "src" / "nadare" / "assets" / "locales" / "en.json"

#: Method names on an imported i18n module.
TRANSLATE_ATTRS = frozenset({"translate", "tr", "_"})
#: Names that may be imported straight from an i18n module.
TRANSLATE_HELPERS = frozenset({"translate", "tr", "_"})
#: Comment that marks a translate call whose key is built at run time.
DYNAMIC_MARKER = "# i18n: dynamic"
PLURAL_ATTRS = frozenset({"translate_plural", "tr_n", "_n"})
PLURAL_HELPERS = frozenset({"translate_plural", "tr_n", "_n"})
_MISSING = object()


class Uncheckable(NamedTuple):
    """A translate call whose key the static scan cannot see."""

    path: Path
    line: int

    def __str__(self) -> str:
        return f"{self.path}:{self.line}"


def _i18n_bindings(tree: ast.AST) -> tuple[set[str], set[str], set[str]]:
    """Names bound to an i18n module, its translate helpers and its plural helpers."""
    modules: set[str] = set()
    helpers: set[str] = set()
    plural_helpers: set[str] = set()
    for node in ast.walk(tree):
        if isinstance(node, ast.Import):
            for alias in node.names:
                if alias.name.split(".")[-1] == "i18n":
                    modules.add(alias.asname or alias.name)
        elif isinstance(node, ast.ImportFrom):
            module = node.module or ""
            for alias in node.names:
                name = alias.asname or alias.name
                if alias.name == "i18n":
                    modules.add(name)
                elif module.split(".")[-1] == "i18n":
                    if alias.name in PLURAL_HELPERS:
                        helpers.add(name)
                        plural_helpers.add(name)
                    elif alias.name in TRANSLATE_HELPERS:
                        helpers.add(name)
    return modules, helpers, plural_helpers


def _string_literal(node: ast.AST) -> str | None:
    """The value of a plain string literal (implicit concatenation included)."""
    if isinstance(node, ast.Constant) and isinstance(node.value, str):
        return node.value
    return None


def _module_constants(tree: ast.Module) -> dict[str, str]:
    """Names bound exactly once, at module level, to a string literal."""
    bindings: dict[str, list[str | None]] = {}

    def record(name: str, value: str | None) -> None:
        bindings.setdefault(name, []).append(value)

    def bind_target(target: ast.AST, value: str | None) -> None:
        if isinstance(target, ast.Name):
            record(target.id, value)
        elif isinstance(target, ast.Starred):
            bind_target(target.value, None)
        elif isinstance(target, (ast.Tuple, ast.List)):
            for element in target.elts:
                bind_target(element, None)

    def walk(statements: list[ast.stmt]) -> None:
        for node in statements:
            if isinstance(node, (ast.FunctionDef, ast.AsyncFunctionDef, ast.ClassDef)):
                record(node.name, None)
            elif isinstance(node, ast.Import):
                for alias in node.names:
                    record(alias.asname or alias.name.split(".")[0], None)
            elif isinstance(node, ast.ImportFrom):
                for alias in node.names:
                    if alias.name != "*":
                        record(alias.asname or alias.name, None)
            elif isinstance(node, ast.Assign):
                value = _string_literal(node.value)
                for target in node.targets:
                    bind_target(target, value)
            elif isinstance(node, ast.AnnAssign):
                if node.value is not None:
                    bind_target(node.target, _string_literal(node.value))
            elif isinstance(node, ast.AugAssign):
                bind_target(node.target, None)
            elif isinstance(node, (ast.For, ast.AsyncFor, ast.While)):
                if not isinstance(node, ast.While):
                    bind_target(node.target, None)
                walk(node.body)
                walk(node.orelse)
            elif isinstance(node, (ast.With, ast.AsyncWith)):
                for item in node.items:
                    if item.optional_vars is not None:
                        bind_target(item.optional_vars, None)
                walk(node.body)
            elif isinstance(node, ast.If):
                walk(node.body)
                walk(node.orelse)
            elif isinstance(node, ast.Try):
                walk(node.body)
                walk(node.orelse)
                walk(node.finalbody)
                for handler in node.handlers:
                    if handler.name:
                        record(handler.name, None)
                    walk(handler.body)

    walk(tree.body)
    return {
        name: values[0]
        for name, values in bindings.items()
        if len(values) == 1 and values[0] is not None
    }


def _bound_names(node: ast.AST) -> set[str]:
    """Every name bound in a function, lambda or comprehension body.

    Deliberately over-approximate: a name stored anywhere inside is treated as
    shadowing the module constant, so a wrong key is reported, never guessed.
    """
    names: set[str] = set()
    if isinstance(node, (ast.FunctionDef, ast.AsyncFunctionDef, ast.Lambda)):
        arguments = node.args
        for arg in (
            *arguments.posonlyargs,
            *arguments.args,
            *arguments.kwonlyargs,
            arguments.vararg,
            arguments.kwarg,
        ):
            if arg is not None:
                names.add(arg.arg)
    for child in ast.walk(node):
        if isinstance(child, ast.Name) and isinstance(child.ctx, ast.Store):
            names.add(child.id)
        elif isinstance(child, (ast.FunctionDef, ast.AsyncFunctionDef, ast.ClassDef)):
            names.add(child.name)
        elif isinstance(child, ast.Import):
            for alias in child.names:
                names.add(alias.asname or alias.name.split(".")[0])
        elif isinstance(child, ast.ImportFrom):
            for alias in child.names:
                if alias.name != "*":
                    names.add(alias.asname or alias.name)
        elif isinstance(child, ast.ExceptHandler) and child.name:
            names.add(child.name)
        elif isinstance(child, (ast.Global, ast.Nonlocal)):
            names.update(child.names)
    return names


def _call_key(call: ast.Call, constants: dict[str, str]) -> tuple[str | None, bool]:
    """``(key, resolvable)`` for one translate call's first argument."""
    first = call.args[0] if call.args else next(
        (kw.value for kw in call.keywords if kw.arg in {"key", "text"}), None
    )
    if first is None:
        return None, True
    literal = _string_literal(first)
    if literal is not None:
        return literal, True
    if isinstance(first, ast.Name) and first.id in constants:
        return constants[first.id], True
    return None, False


def _has_dynamic_marker(lines: list[str], call: ast.Call) -> bool:
    last = min(getattr(call, "end_lineno", call.lineno), len(lines))
    return any(DYNAMIC_MARKER in lines[line - 1] for line in range(call.lineno, last + 1))


def _scan_file(path: Path) -> tuple[set[str], set[str], list[Uncheckable]]:
    try:
        source = path.read_text(encoding="utf-8")
        tree = ast.parse(source, filename=str(path))
    except (OSError, SyntaxError, UnicodeError) as exc:
        raise ValueError(f"cannot scan {path}: {exc}") from exc
    lines = source.splitlines()
    constants = _module_constants(tree)
    modules, helpers, plural_helpers = _i18n_bindings(tree)
    keys: set[str] = set()
    plural: set[str] = set()
    uncheckable: set[Uncheckable] = set()

    def walk(node: ast.AST, shadowed: frozenset[str]) -> None:
        if isinstance(node, (ast.FunctionDef, ast.AsyncFunctionDef)):
            for decorator in node.decorator_list:
                walk(decorator, shadowed)
            for default in (*node.args.defaults, *node.args.kw_defaults):
                if default is not None:
                    walk(default, shadowed)
            inner = shadowed | _bound_names(node)
            for statement in node.body:
                walk(statement, inner)
            return
        if isinstance(node, ast.Lambda):
            for default in (*node.args.defaults, *node.args.kw_defaults):
                if default is not None:
                    walk(default, shadowed)
            inner = shadowed | _bound_names(node)
            walk(node.body, inner)
            return
        if isinstance(node, ast.ClassDef):
            for child in (*node.decorator_list, *node.bases, *node.keywords):
                walk(child, shadowed)
            for statement in node.body:
                walk(statement, shadowed)
            return
        if isinstance(node, (ast.ListComp, ast.SetComp, ast.DictComp, ast.GeneratorExp)):
            inner = shadowed | _bound_names(node)
            for generator in node.generators:
                walk(generator.iter, shadowed)
                walk(generator.target, shadowed)
                for condition in generator.ifs:
                    walk(condition, inner)
            if isinstance(node, ast.DictComp):
                walk(node.key, inner)
                walk(node.value, inner)
            else:
                walk(node.elt, inner)
            return
        if isinstance(node, ast.Call):
            func = node.func
            is_translate = False
            is_plural = False
            if isinstance(func, ast.Attribute):
                if func.attr in TRANSLATE_ATTRS | PLURAL_ATTRS:
                    is_plural = func.attr in PLURAL_ATTRS
                    receiver = func.value
                    parts = []
                    while isinstance(receiver, ast.Attribute):
                        parts.append(receiver.attr)
                        receiver = receiver.value
                    if isinstance(receiver, ast.Name):
                        parts.append(receiver.id)
                        is_translate = ".".join(reversed(parts)) in modules
            elif isinstance(func, ast.Name):
                is_translate = func.id in helpers
                is_plural = func.id in plural_helpers
            if is_translate:
                first = node.args[0] if node.args else next(
                    (kw.value for kw in node.keywords if kw.arg in {"key", "text"}), None
                )
                locally_bound = isinstance(first, ast.Name) and first.id in shadowed
                key, resolvable = _call_key(node, constants)
                if locally_bound:
                    resolvable = False
                if resolvable:
                    if key is not None:
                        (plural if is_plural else keys).add(key)
                elif not _has_dynamic_marker(lines, node):
                    uncheckable.add(Uncheckable(path, node.lineno))
        for child in ast.iter_child_nodes(node):
            walk(child, shadowed)

    walk(tree, frozenset())
    return keys, plural, sorted(uncheckable, key=lambda item: (str(item.path), item.line))


def _scan(src: Path) -> tuple[set[str], set[str], list[Uncheckable]]:
    """Keys the source passes to translate calls, and calls it cannot check."""
    if not Path(src).is_dir():
        raise ValueError(f"{src} is not a source directory")
    keys: set[str] = set()
    plural: set[str] = set()
    uncheckable: list[Uncheckable] = []
    for path in sorted(Path(src).rglob("*.py")):
        found, found_plural, unclear = _scan_file(path)
        plural |= found_plural
        keys |= found
        uncheckable.extend(unclear)
    return keys, plural, uncheckable


def scan(src: Path) -> tuple[set[str], list[Uncheckable]]:
    """Resolved plain and plural keys, and calls whose keys cannot be checked."""
    plain, plural, uncheckable = _scan(src)
    return plain | plural, uncheckable


def plural_keys(src: Path) -> set[str]:
    """Resolved keys passed to plural calls."""
    return _scan(src)[1]


def used_keys(src: Path) -> set[str]:
    """Every literal key passed to an i18n translate call under ``src``."""
    return scan(src)[0]


def catalogue_values(catalogue: Path) -> dict[str, object]:
    """Every shipped key and its raw value (a string or a plural object)."""
    raw = json.loads(Path(catalogue).read_text(encoding="utf-8"))
    if not isinstance(raw, dict):
        raise ValueError(f"{catalogue} is not a flat JSON object")
    return {str(key): value for key, value in raw.items()}


def catalogue_keys(catalogue: Path) -> set[str]:
    """Keys shipped in a flat JSON catalogue."""
    return set(catalogue_values(catalogue))


def compare(src: Path, catalogue: Path) -> tuple[list[str], list[str]]:
    """Return ``(missing, unused)`` key lists, each sorted."""
    used = used_keys(src)
    known = catalogue_keys(catalogue)
    return sorted(used - known), sorted(known - used)


def type_mismatches(src: Path, catalogue: Path) -> list[str]:
    """Calls whose catalogue value has the wrong shape for a plain or plural call."""
    plain, plural, _uncheckable = _scan(src)
    values = catalogue_values(catalogue)
    problems: list[str] = []
    for key in sorted(plural):
        value = values.get(key, _MISSING)
        if value is _MISSING:
            continue  # reported as a missing key
        if not isinstance(value, dict):
            problems.append(f"plural call on a plain string: {key}")
        elif not isinstance(value.get("other"), str):
            problems.append(f'plural key with no "other" form: {key}')
    for key in sorted(plain):
        value = values.get(key, _MISSING)
        if isinstance(value, dict) and not isinstance(value.get("other"), str):
            problems.append(f'plain call on a key with no "other" form: {key}')
    return problems


def main(argv: list[str] | None = None) -> int:
    parser = argparse.ArgumentParser(description=__doc__.splitlines()[0])
    parser.add_argument("--src", type=Path, default=DEFAULT_SRC, help="source tree to scan")
    parser.add_argument(
        "--catalogue", type=Path, default=DEFAULT_CATALOGUE, help="flat JSON catalogue"
    )
    parser.add_argument(
        "--check", action="store_true", help="exit 1 when a used key is missing"
    )
    args = parser.parse_args(argv)

    try:
        used, uncheckable = scan(args.src)
        known = catalogue_keys(args.catalogue)
        mismatches = type_mismatches(args.src, args.catalogue)
    except (OSError, ValueError) as exc:
        print(f"i18n extract: {exc}", file=sys.stderr)
        return 2
    missing = sorted(used - known)
    unused = sorted(known - used)

    print(f"keys used in {args.src} but missing from {args.catalogue}:")
    for key in missing:
        print(f"  - {key}")
    if not missing:
        print("  (none)")
    print(f"keys in {args.catalogue} that nothing uses (kept, never deleted):")
    for key in unused:
        print(f"  - {key}")
    if not unused:
        print("  (none)")
    if uncheckable:
        print("calls whose key cannot be checked (use a literal or '# i18n: dynamic'):")
        for item in uncheckable:
            print(f"  - not checkable: {item}")

    print("calls whose catalogue value has the wrong plural/plain shape:")
    for problem in mismatches:
        print(f"  - {problem}")
    if not mismatches:
        print("  (none)")

    if args.check and (missing or uncheckable or mismatches):
        if uncheckable or mismatches:
            print(
                f"i18n extract: {len(missing)} missing key(s), "
                f"{len(uncheckable)} call(s) not checkable, "
                f"{len(mismatches)} type mismatch(es)",
                file=sys.stderr,
            )
        else:
            print(f"i18n extract: {len(missing)} missing key(s)", file=sys.stderr)
        return 1
    return 0


if __name__ == "__main__":
    raise SystemExit(main())
