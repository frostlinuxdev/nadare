# SPDX-License-Identifier: GPL-3.0-or-later
# Copyright (C) 2026 Nadare contributors
"""The process-wide JSON catalogue and locale service for CLI and GUI.

Locales live in ``src/nadare/assets/locales/<code>.json`` as a flat object. The
default is English and the fallback chain is: requested locale -> English -> the
key itself, so a missing string degrades to something readable rather than an
exception. Values may use ``str.format`` placeholders. A value is either a plain
string or, for plurals, an object of CLDR categories (``one``/``few``/``many``/
``other``); its key is the English ``other`` form.

A successful language save in Settings changes subsequent translation calls
immediately, including backend messages. Existing widgets keep their rendered
text until recreated (or the application is restarted); there is no live widget
retranslation. Only successfully loaded catalogues are cached; failed loads are
retried on the next lookup.
"""

from __future__ import annotations

import json
import os
from collections.abc import Mapping
from pathlib import Path
from typing import Any

DEFAULT_LOCALE = "en"
LOCALE_ENV = "NADARE_LANG"

_current = DEFAULT_LOCALE
_cache: dict[str, dict[str, Any]] = {}


def locale_dir() -> Path:
    """Directory containing the shipped locale JSON files."""
    return Path(__file__).resolve().parent.parent / "assets" / "locales"


def available_locales() -> list[str]:
    try:
        codes = sorted(
            p.stem for p in locale_dir().glob("*.json") if p.is_file() and _safe_code(p.stem)
        )
    except OSError:
        codes = []
    if DEFAULT_LOCALE not in codes:
        codes.insert(0, DEFAULT_LOCALE)
    return codes


def _safe_code(code: str) -> bool:
    return bool(code) and code not in (".", "..") and not any(c in code for c in "/\\\0")


def _clean_value(value: Any) -> Any:
    """Keep a plural object; stringify every other JSON value like before."""
    if isinstance(value, dict):
        return {str(category): str(text) for category, text in value.items()}
    return str(value)


def _load(locale: str) -> dict[str, Any]:
    cached = _cache.get(locale)
    if cached is not None:
        return cached
    # Validate before building a path, including for explicit/bound translators.
    # This retains the GUI loader's requirement for a known catalogue code.
    if not _safe_code(locale) or locale not in available_locales():
        return {}
    path = locale_dir() / f"{locale}.json"
    try:
        raw = json.loads(path.read_text(encoding="utf-8"))
    except (OSError, ValueError):
        # A momentarily unreadable/malformed locale file must not be cached for
        # the life of the process; a later call retries the disk.
        return {}
    if not isinstance(raw, dict):
        return {}
    data = {str(k): _clean_value(v) for k, v in raw.items()}
    _cache[locale] = data
    return data


def normalise_locale(value: str) -> str:
    """Reduce a locale name to a catalogue code; ``""`` when it names no language.

    ``de_DE.UTF-8@euro`` -> ``de_DE``; ``C``/``POSIX`` mean English, and unsafe
    values (path separators, NUL) are rejected so they never reach a path lookup.
    """
    code = value.strip()
    if not code:
        return ""
    code = code.split("@", 1)[0].split(".", 1)[0].replace("-", "_")
    if code in ("C", "POSIX"):
        return DEFAULT_LOCALE
    return code if _safe_code(code) else ""


def resolve_locale(
    setting: str | None = None, environ: Mapping[str, str] | None = None
) -> str:
    """The UI language for startup, from the Nadare setting or the desktop.

    Order: the Nadare setting, then ``LC_ALL``,
    ``LC_MESSAGES``, ``LANG`` and the first entry of ``LANGUAGE``. A full code
    wins when its catalogue exists, then its language part; otherwise ``en``.
    The same function serves AppImage, Flatpak and source runs.
    """
    env = os.environ if environ is None else environ
    language_list = env.get("LANGUAGE") or ""
    sources = (
        setting,
        env.get("LC_ALL"),
        env.get("LC_MESSAGES"),
        env.get("LANG"),
        language_list.split(":", 1)[0],
    )
    available = available_locales()
    for raw in sources:
        if not isinstance(raw, str):
            continue
        code = normalise_locale(raw)
        if not code:
            continue
        for candidate in (code, code.split("_", 1)[0]):
            if candidate and candidate in available:
                return candidate
    return DEFAULT_LOCALE


def set_locale(locale: str | None) -> str:
    """Set the active locale; returns the one actually selected."""
    global _current
    chosen = locale or os.environ.get(LOCALE_ENV) or DEFAULT_LOCALE
    chosen = chosen.split(".")[0] if _safe_code(chosen) else DEFAULT_LOCALE
    if chosen not in available_locales():
        chosen = DEFAULT_LOCALE
    _current = chosen
    return _current


def get_locale() -> str:
    return _current


def _locale_chain(locale: str | None) -> list[str]:
    locales = [locale] if locale else [_current, DEFAULT_LOCALE]
    if DEFAULT_LOCALE not in locales:
        locales.append(DEFAULT_LOCALE)
    return locales


def _format(text: str, fmt: Mapping[str, Any]) -> str:
    if fmt:
        try:
            return text.format(**fmt)
        except (KeyError, IndexError, ValueError):
            pass
    return text


def _plain_text(value: Any) -> str | None:
    """A catalogue value as translatable text; plural objects use their ``other`` form."""
    if isinstance(value, str):
        return value
    if isinstance(value, Mapping):
        other = value.get("other")
        return other if isinstance(other, str) else None
    return None


def translate(key: str, locale: str | None = None, **fmt: Any) -> str:
    """Look ``key`` up, falling back to English and then to the key itself."""
    text: str | None = None
    for code in _locale_chain(locale):
        if code is None:
            continue
        text = _plain_text(_load(code).get(key))
        if text is not None:
            break
    if text is None:
        text = key
    return _format(text, fmt)


# +-- plurals ---------------------------------------------------------------

#: One is the only singular form.
_ONE_IF_ONE = frozenset({"en", "de", "nl", "sv", "it", "es", "pt"})
#: Zero and one share the singular form.
_ONE_IF_ZERO_OR_ONE = frozenset({"fr"})
#: No singular form at all.
_OTHER_ONLY = frozenset({"ja", "ko", "zh"})
#: CLDR's one/few/many integer rules.
_SLAVIC = frozenset({"ru", "uk", "pl"})


def _plural_category(locale: str, n: float) -> str:
    """The CLDR plural category this language uses for the integer ``n``."""
    code = normalise_locale(locale).lower()
    language = code.split("_", 1)[0]
    if code == "pt_br" or language in _ONE_IF_ZERO_OR_ONE:
        return "one" if n in (0, 1) else "other"
    if language in _OTHER_ONLY:
        return "other"
    if language in _SLAVIC:
        number = float(n)
        if not number.is_integer():
            return "other"
        i = abs(int(number))
        if i % 10 == 1 and i % 100 != 11:
            return "one"
        if 2 <= i % 10 <= 4 and not 12 <= i % 100 <= 14:
            return "few"
        return "many"
    if language in _ONE_IF_ONE:
        return "one" if n == 1 else "other"
    return "other"


def _plural_text(value: Any, locale: str, n: float) -> str | None:
    """Pick ``value``'s category, then ``other``, for ``locale``."""
    if isinstance(value, str):
        return value
    if isinstance(value, Mapping):
        text = value.get(_plural_category(locale, n))
        if not isinstance(text, str):
            text = value.get("other")
        return text if isinstance(text, str) else None
    return None


def translate_plural(key: str, n: float, locale: str | None = None, **values: Any) -> str:
    """Translate a plural ``key`` for ``n``; ``{n}`` is always available."""
    fmt = {"n": n, **values}
    for code in _locale_chain(locale):
        if code is None:
            continue
        text = _plural_text(_load(code).get(key), code, n)
        if text is not None:
            return _format(text, fmt)
    return _format(key, fmt)


#: Shorthand mirroring gettext's ``_``.
_ = translate
#: Shorthand for :func:`translate_plural`.
_n = translate_plural


class Translator:
    """Bound translator, handy when a backend carries its own locale."""

    def __init__(self, locale: str | None = None) -> None:
        self.locale = locale or get_locale()

    def __call__(self, key: str, **fmt: Any) -> str:
        return translate(key, self.locale, **fmt)
