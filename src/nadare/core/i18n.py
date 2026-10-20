# SPDX-License-Identifier: GPL-3.0-or-later
# Copyright (C) 2026 Nadare contributors
"""A tiny JSON translation loader.

Locales live in ``src/nadare/assets/locales/<code>.json`` as a flat object. The
default is English and the fallback chain is: requested locale -> English -> the
key itself, so a missing string degrades to something readable rather than an
exception. Values may use ``str.format`` placeholders.
"""

from __future__ import annotations

import json
import os
from pathlib import Path
from typing import Any

DEFAULT_LOCALE = "en"
LOCALE_ENV = "NADARE_LANG"

_current = DEFAULT_LOCALE
_cache: dict[str, dict[str, str]] = {}


def locale_dir() -> Path:
    """Directory containing the shipped locale JSON files."""
    return Path(__file__).resolve().parent.parent / "assets" / "locales"


def available_locales() -> list[str]:
    d = locale_dir()
    if not d.is_dir():
        return [DEFAULT_LOCALE]
    codes = sorted(p.stem for p in d.glob("*.json") if p.is_file())
    if DEFAULT_LOCALE not in codes:
        codes.insert(0, DEFAULT_LOCALE)
    return codes


def _load(locale: str) -> dict[str, str]:
    cached = _cache.get(locale)
    if cached is not None:
        return cached
    path = locale_dir() / f"{locale}.json"
    try:
        raw = json.loads(path.read_text(encoding="utf-8"))
    except (OSError, ValueError):
        # A momentarily unreadable/malformed locale file must not be cached for
        # the life of the process; a later call retries the disk.
        return {}
    if not isinstance(raw, dict):
        return {}
    data = {str(k): str(v) for k, v in raw.items()}
    _cache[locale] = data
    return data


def set_locale(locale: str | None) -> str:
    """Set the active locale; returns the one actually selected."""
    global _current
    chosen = (locale or os.environ.get(LOCALE_ENV) or DEFAULT_LOCALE).split(".")[0]
    if chosen not in available_locales():
        chosen = DEFAULT_LOCALE
    _current = chosen
    return _current


def get_locale() -> str:
    return _current


def translate(key: str, locale: str | None = None, **fmt: Any) -> str:
    """Look ``key`` up, falling back to English and then to the key itself."""
    locales = [locale] if locale else [_current, DEFAULT_LOCALE]
    if DEFAULT_LOCALE not in locales:
        locales.append(DEFAULT_LOCALE)
    text: str | None = None
    for code in locales:
        if code is None:
            continue
        text = _load(code).get(key)
        if text is not None:
            break
    if text is None:
        text = key
    if fmt:
        try:
            text = text.format(**fmt)
        except (KeyError, IndexError, ValueError):
            pass
    return text


#: Shorthand mirroring gettext's ``_``.
_ = translate


class Translator:
    """Bound translator, handy when a backend carries its own locale."""

    def __init__(self, locale: str | None = None) -> None:
        self.locale = locale or get_locale()

    def __call__(self, key: str, **fmt: Any) -> str:
        return translate(key, self.locale, **fmt)
