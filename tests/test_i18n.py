# SPDX-License-Identifier: GPL-3.0-or-later
"""Unit tests for nadare.core.i18n."""

from __future__ import annotations

import json
from pathlib import Path

from nadare.core import i18n


def _write_locale(directory: Path, code: str, data: dict) -> None:
    directory.mkdir(parents=True, exist_ok=True)
    (directory / f"{code}.json").write_text(json.dumps(data), encoding="utf-8")


def test_translate_falls_back_to_english_then_key(tmp_path: Path, monkeypatch) -> None:
    monkeypatch.setattr(i18n, "locale_dir", lambda: tmp_path)
    i18n._cache.clear()
    _write_locale(tmp_path, "en", {"hello": "Hello"})

    assert i18n.translate("hello", "de") == "Hello"  # de missing -> English
    assert i18n.translate("absent", "de") == "absent"  # -> key itself


def test_translate_formats_kwargs(tmp_path: Path, monkeypatch) -> None:
    monkeypatch.setattr(i18n, "locale_dir", lambda: tmp_path)
    i18n._cache.clear()
    _write_locale(tmp_path, "en", {"greet": "Hi {name}"})
    assert i18n.translate("greet", "en", name="Ada") == "Hi Ada"


def test_transient_load_failure_is_not_cached(tmp_path: Path, monkeypatch) -> None:
    """L20: a missing/unreadable locale must be retried, not cached forever."""
    monkeypatch.setattr(i18n, "locale_dir", lambda: tmp_path)
    i18n._cache.clear()

    # No file yet: the key is returned and nothing is cached.
    assert i18n.translate("hello", "xx") == "hello"
    assert "xx" not in i18n._cache

    # The file becomes available; a later lookup must pick it up.
    _write_locale(tmp_path, "xx", {"hello": "hola"})
    assert i18n.translate("hello", "xx") == "hola"


def test_gui_catalogue_refuses_a_path_traversal_code(tmp_path, monkeypatch) -> None:
    """06-12: a crafted language code must not escape the locales directory."""
    from nadare.gui import i18n as gui_i18n

    locales = tmp_path / "locales"
    locales.mkdir()
    outside = tmp_path / "outside"
    outside.mkdir()
    _write_locale(outside, "secret", {"hello": "leaked"})

    monkeypatch.setattr(gui_i18n, "LOCALES_DIR", locales)
    gui_i18n._catalogue.cache_clear()
    try:
        assert gui_i18n._catalogue("../outside/secret") == {}
        assert gui_i18n._catalogue("not-a-locale") == {}

        # A real catalogue is still loaded.
        _write_locale(locales, "de", {"hello": "Hallo"})
        assert gui_i18n._catalogue("de") == {"hello": "Hallo"}
    finally:
        gui_i18n._catalogue.cache_clear()
