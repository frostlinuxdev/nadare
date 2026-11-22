# SPDX-License-Identifier: GPL-3.0-or-later
# Copyright (C) 2026 Nadare contributors
"""UI naming wrappers around the shared :mod:`nadare.core.i18n` service.

Locale selection, validation, fallback, formatting and caching belong to core.
Settings changes affect future translation calls immediately; existing widgets
keep their text until recreated or restarted (no live retranslation).
"""

from __future__ import annotations

import re
from collections.abc import Callable
from datetime import datetime
from pathlib import Path
from typing import Any

from ..core import diskspace, i18n

DEFAULT_LANGUAGE = i18n.DEFAULT_LOCALE

#: Kept alive for the process: Qt does not take ownership of an installed
#: translator, so dropping the Python reference would silently undo it.
_qt_translator: Any = None


def available() -> list[str]:
    """Locale codes with a catalogue, falling back to ``["en"]``."""
    return i18n.available_locales()


def set_language(language: str | None) -> None:
    """Switch to ``language``; an unset one follows the desktop, as at startup."""
    i18n.set_locale(language or resolve_locale(None))


def language() -> str:
    return i18n.get_locale()


def resolve_locale(setting: str | None = None) -> str:
    """The UI language for startup, from the setting or the desktop."""
    return i18n.resolve_locale(setting)


def tr(text: str, **values: object) -> str:
    """Translate ``text``; ``{name}`` placeholders are formatted if given."""
    return i18n.translate(text, **values)  # i18n: dynamic (the caller's key)


def tr_n(text: str, n: float, **values: object) -> str:
    """Translate a plural ``text`` for ``n`` (``{n}`` is always available)."""
    return i18n.translate_plural(text, n, **values)  # i18n: dynamic (the caller's key)


def _qlocale() -> Any:
    from PyQt6.QtCore import QLocale

    return QLocale(language())


def format_number(value: float, decimals: int | None = None, *, group: bool = True) -> str:
    """``value`` with the UI language's decimal and grouping marks (none if ``group=False``)."""
    locale = _qlocale()
    if not group:
        locale.setNumberOptions(locale.numberOptions() | locale.NumberOption.OmitGroupSeparator)
    if decimals is None:
        number = float(value)
        if number.is_integer():
            return locale.toString(int(number))
        text = repr(number)
        if "e" in text or "E" in text:
            return locale.toString(number, "g", 15)
        decimals = len(text.split(".", 1)[1])
    return locale.toString(float(value), "f", max(0, int(decimals)))


def format_size(size: float) -> str:
    """:func:`nadare.core.diskspace.human_size` with a locale-aware number.

    Never grouped: a size has at most four digits, and ``1,000 KiB`` reads like a decimal.
    """
    value, decimals, unit = diskspace.human_size_parts(size)
    return f"{format_number(value, decimals, group=False)} {unit}"


def format_datetime(dt: datetime, short: bool = True) -> str:
    """``dt`` in the UI language's order; short keeps the full year."""
    from PyQt6.QtCore import QDateTime, QLocale

    locale = _qlocale()
    # A Python datetime is a date first: wrap it so Qt does not drop the time.
    moment = QDateTime(dt)
    if not short:
        return locale.toString(moment, QLocale.FormatType.LongFormat)
    if language().split("_", 1)[0].lower() == DEFAULT_LANGUAGE:
        # English keeps the ISO order Nadare has always shown.
        pattern = "yyyy-MM-dd HH:mm"
    else:
        pattern = locale.dateTimeFormat(QLocale.FormatType.ShortFormat)
        pattern = re.sub(r"(?<!y)yy(?!y)", "yyyy", pattern)
    return locale.toString(moment, pattern)


def _qt_translations_dir() -> Path:
    """The folder where this PyQt6 build keeps its own ``.qm`` translations."""
    from PyQt6.QtCore import QLibraryInfo

    return Path(QLibraryInfo.path(QLibraryInfo.LibraryPath.TranslationsPath))


def _load_qt_translator(code: str, path: Path) -> Any:
    """Read one ``.qm`` file, or ``None`` when Qt rejects it."""
    from PyQt6.QtCore import QTranslator

    translator = QTranslator()
    return translator if translator.load(str(path)) else None


def install_qt_translations(
    application: Any,
    language: str | None = None,
    *,
    loader: Callable[[str, Path], Any] | None = None,
    translations_dir: Path | None = None,
) -> bool:
    """Load Qt's own texts (buttons, standard dialogs) for the UI language.

    Called once at GUI start, before widgets exist. English and a missing
    ``qtbase_<code>.qm`` both stay silently English instead of failing.
    """
    global _qt_translator
    if application is None or _qt_translator is not None:
        return False
    language = language or i18n.get_locale()
    code = i18n.normalise_locale(language)
    if not code or code.split("_", 1)[0] == DEFAULT_LANGUAGE:
        return False
    directory = Path(translations_dir) if translations_dir is not None else _qt_translations_dir()
    load = loader or _load_qt_translator
    for candidate in (code, code.split("_", 1)[0]):
        path = directory / f"qtbase_{candidate}.qm"
        if not path.is_file():
            continue
        translator = load(candidate, path)
        if translator is not None:
            application.installTranslator(translator)
            _qt_translator = translator
            return True
    return False


__all__ = [
    "available",
    "format_datetime",
    "format_number",
    "format_size",
    "install_qt_translations",
    "language",
    "resolve_locale",
    "set_language",
    "tr",
    "tr_n",
]
