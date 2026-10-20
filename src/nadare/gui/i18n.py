# SPDX-License-Identifier: GPL-3.0-or-later
# Copyright (C) 2026 Nadare contributors
"""A tiny gettext-flavoured translator backed by ``assets/locales/*.json``.

Keys are the English source strings. A missing key or locale falls back to the
key itself, so the UI is always usable even with no catalogue.
"""

from __future__ import annotations

import json
from functools import cache
from pathlib import Path

LOCALES_DIR = Path(__file__).resolve().parent.parent / "assets" / "locales"
DEFAULT_LANGUAGE = "en"

_current = DEFAULT_LANGUAGE


def available() -> list[str]:
    """Locale codes with a catalogue, falling back to ``["en"]``."""
    codes: list[str] = []
    try:
        for path in sorted(LOCALES_DIR.glob("*.json")):
            codes.append(path.stem)
    except OSError:
        pass
    if DEFAULT_LANGUAGE not in codes:
        codes.insert(0, DEFAULT_LANGUAGE)
    return codes


@cache
def _catalogue(language: str) -> dict[str, str]:
    # Only a code that resolves to a real catalogue may build a path: a crafted
    # config language (e.g. ``../../etc/passwd``) must never escape LOCALES_DIR
    # into an arbitrary JSON file (06-12).
    if language not in available():
        return {}
    path = LOCALES_DIR / f"{language}.json"
    try:
        data = json.loads(path.read_text(encoding="utf-8"))
    except (OSError, ValueError):
        return {}
    if not isinstance(data, dict):
        return {}
    return {str(key): str(value) for key, value in data.items()}


def set_language(language: str) -> None:
    global _current
    _current = language or DEFAULT_LANGUAGE


def language() -> str:
    return _current


def tr(text: str, **values: object) -> str:
    """Translate ``text``; ``{name}`` placeholders are formatted if given."""
    translated = _catalogue(_current).get(text, text)
    if values:
        try:
            return translated.format(**values)
        except (KeyError, IndexError, ValueError):
            return translated
    return translated


__all__ = ["available", "set_language", "language", "tr", "LOCALES_DIR"]
