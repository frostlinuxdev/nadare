# SPDX-License-Identifier: GPL-3.0-or-later
"""#1602 items 3 and 6: plural forms, and locale-aware numbers and dates."""

from __future__ import annotations

import json
from datetime import datetime
from pathlib import Path

import pytest

from nadare.core import diskspace, i18n
from nadare.gui import i18n as gui_i18n


@pytest.fixture(autouse=True)
def isolated_locale_state(monkeypatch):
    monkeypatch.setattr(i18n, "_current", "en")
    monkeypatch.setattr(i18n, "_cache", {})
    monkeypatch.setattr(gui_i18n, "_qt_translator", None)


def _write_locale(directory: Path, code: str, data: dict) -> None:
    directory.mkdir(parents=True, exist_ok=True)
    (directory / f"{code}.json").write_text(json.dumps(data), encoding="utf-8")


def _catalogues(tmp_path: Path, monkeypatch, *codes: str) -> Path:
    directory = tmp_path / "locales"
    for code in codes:
        _write_locale(directory, code, {})
    monkeypatch.setattr(i18n, "locale_dir", lambda: directory)
    return directory


CATEGORIES = {"one": "one", "few": "few", "many": "many", "other": "other"}


@pytest.mark.parametrize("code", ["en", "de", "nl", "sv", "it", "es", "pt"])
@pytest.mark.parametrize(("n", "expected"), [(0, "other"), (1, "one"), (2, "other")])
def test_one_is_the_only_singular_in_these_languages(
    tmp_path, monkeypatch, code, n, expected
) -> None:
    _write_locale(tmp_path, code, {"items": CATEGORIES})
    monkeypatch.setattr(i18n, "locale_dir", lambda: tmp_path)
    assert i18n.translate_plural("items", n, locale=code) == expected


@pytest.mark.parametrize("code", ["fr", "pt_BR"])
@pytest.mark.parametrize(("n", "expected"), [(0, "one"), (1, "one"), (2, "other"), (5, "other")])
def test_french_counts_zero_as_singular(tmp_path, monkeypatch, code, n, expected) -> None:
    _write_locale(tmp_path, code, {"items": CATEGORIES})
    monkeypatch.setattr(i18n, "locale_dir", lambda: tmp_path)
    assert i18n.translate_plural("items", n, locale=code) == expected


@pytest.mark.parametrize("code", ["ja", "ko", "zh"])
@pytest.mark.parametrize("n", [0, 1, 2, 5])
def test_no_singular_category_in_these_languages(tmp_path, monkeypatch, code, n) -> None:
    _write_locale(tmp_path, code, {"items": CATEGORIES})
    monkeypatch.setattr(i18n, "locale_dir", lambda: tmp_path)
    assert i18n.translate_plural("items", n, locale=code) == "other"


@pytest.mark.parametrize("code", ["ru", "uk", "pl"])
@pytest.mark.parametrize(
    ("n", "expected"),
    [
        (0, "many"),
        (1, "one"),
        (2, "few"),
        (5, "many"),
        (11, "many"),
        (21, "one"),
        (22, "few"),
        (25, "many"),
    ],
)
def test_slavic_integer_categories(tmp_path, monkeypatch, code, n, expected) -> None:
    _write_locale(tmp_path, code, {"items": CATEGORIES})
    monkeypatch.setattr(i18n, "locale_dir", lambda: tmp_path)
    assert i18n.translate_plural("items", n, locale=code) == expected


def test_unknown_language_falls_back_to_other(tmp_path, monkeypatch) -> None:
    _write_locale(tmp_path, "xx", {"items": CATEGORIES})
    monkeypatch.setattr(i18n, "locale_dir", lambda: tmp_path)
    assert i18n.translate_plural("items", 1, locale="xx") == "other"


def test_plural_falls_back_to_the_other_form(tmp_path, monkeypatch) -> None:
    _write_locale(tmp_path, "ru", {"items": {"other": "many things"}})
    monkeypatch.setattr(i18n, "locale_dir", lambda: tmp_path)
    assert i18n.translate_plural("items", 2, locale="ru") == "many things"


def test_plural_falls_back_to_english(tmp_path, monkeypatch) -> None:
    _write_locale(tmp_path, "en", {"items": {"one": "one thing", "other": "many things"}})
    _write_locale(tmp_path, "ru", {})
    monkeypatch.setattr(i18n, "locale_dir", lambda: tmp_path)
    # The English string is used, so the English rule picks "other" for 2.
    assert i18n.translate_plural("items", 2, locale="ru") == "many things"


def test_plural_falls_back_to_the_key_with_n(tmp_path, monkeypatch) -> None:
    monkeypatch.setattr(i18n, "locale_dir", lambda: tmp_path)
    assert i18n.translate_plural("{n} missing things", 5, locale="en") == "5 missing things"


def test_plural_formats_n_and_other_values(tmp_path, monkeypatch) -> None:
    _write_locale(
        tmp_path,
        "en",
        {"{n} games in {place}": {"one": "{n} game in {place}", "other": "{n} games in {place}"}},
    )
    monkeypatch.setattr(i18n, "locale_dir", lambda: tmp_path)
    assert (
        i18n.translate_plural("{n} games in {place}", 1, locale="en", place="Library")
        == "1 game in Library"
    )
    assert (
        i18n.translate_plural("{n} games in {place}", 3, locale="en", place="Library")
        == "3 games in Library"
    )


def test_plain_translate_returns_the_other_form(tmp_path, monkeypatch) -> None:
    _write_locale(tmp_path, "en", {"items": {"one": "{n} item", "other": "{n} items"}})
    monkeypatch.setattr(i18n, "locale_dir", lambda: tmp_path)
    assert i18n.translate("items", "en") == "{n} items"


def test_gui_tr_n_uses_the_shared_plural_service(tmp_path, monkeypatch) -> None:
    _write_locale(tmp_path, "en", {"{n} games": {"one": "{n} game", "other": "{n} games"}})
    monkeypatch.setattr(i18n, "locale_dir", lambda: tmp_path)
    assert gui_i18n.tr_n("{n} games", 1) == "1 game"
    assert gui_i18n.tr_n("{n} games", 4) == "4 games"


def test_shipped_plural_entries_render_their_other_form() -> None:
    catalogue = json.loads((i18n.locale_dir() / "en.json").read_text(encoding="utf-8"))
    for key, value in catalogue.items():
        expected = value["other"] if isinstance(value, dict) else value
        assert i18n.translate(key) == expected


# ---------------------------------------------------------------------------
# Locale-aware numbers, sizes and dates
# ---------------------------------------------------------------------------


def _select(tmp_path: Path, monkeypatch, code: str) -> None:
    directory = _catalogues(tmp_path, monkeypatch, "en", "de", "fr")
    _write_locale(directory, code, {})
    i18n.set_locale(code)


def test_format_number_english_and_german(tmp_path, monkeypatch) -> None:
    _select(tmp_path, monkeypatch, "en")
    assert gui_i18n.format_number(1234.5, 1) == "1,234.5"
    assert gui_i18n.format_number(1.5) == "1.5"
    assert gui_i18n.format_number(1536, 0) == "1,536"

    i18n.set_locale("de")
    assert gui_i18n.format_number(1234.5, 1) == "1.234,5"
    assert gui_i18n.format_number(1.5) == "1,5"
    assert gui_i18n.format_number(1536, 0) == "1.536"


def test_format_size_is_locale_aware(tmp_path, monkeypatch) -> None:
    _select(tmp_path, monkeypatch, "en")
    assert gui_i18n.format_size(0) == "0 B"
    assert gui_i18n.format_size(512) == "512 B"
    assert gui_i18n.format_size(1536) == "1.5 KiB"
    assert gui_i18n.format_size(5 << 20) == "5 MiB"
    assert gui_i18n.format_size(319 << 30) == "319 GiB"

    i18n.set_locale("de")
    assert gui_i18n.format_size(1536) == "1,5 KiB"
    assert gui_i18n.format_size(5 << 20) == "5 MiB"


@pytest.mark.parametrize("size", [1000, 1023, 1000 << 10, (1023 << 10) + 500, 1010 << 20])
def test_format_size_never_groups_four_digits(tmp_path, monkeypatch, size) -> None:
    # English must stay byte-identical to the shared formatter; no locale groups a size.
    _select(tmp_path, monkeypatch, "en")
    assert gui_i18n.format_size(size) == diskspace.human_size(size)
    i18n.set_locale("de")
    assert gui_i18n.format_size(size) == diskspace.human_size(size)


def test_format_datetime_english_keeps_the_iso_order(tmp_path, monkeypatch) -> None:
    _select(tmp_path, monkeypatch, "en")
    assert gui_i18n.format_datetime(datetime(2026, 11, 19, 14, 30)) == "2026-11-19 14:30"


def test_format_datetime_german_uses_day_first(tmp_path, monkeypatch) -> None:
    _select(tmp_path, monkeypatch, "de")
    assert gui_i18n.format_datetime(datetime(2026, 11, 19, 14, 30)) == "19.11.2026 14:30"


def test_converted_gui_call_sites_stay_identical_in_english(tmp_path, monkeypatch) -> None:
    from nadare.gui import logs_dialog, runners_dialog

    _select(tmp_path, monkeypatch, "en")
    for size in (0, 700, 1536, 5 << 20, int(1.5 * (1 << 30)), 88 << 30):
        expected = diskspace.human_size(size)
        assert gui_i18n.format_size(size) == expected
        assert logs_dialog._human_size(size) == expected
        assert runners_dialog.human_size(size) == expected

    stamp = datetime(2026, 11, 19, 14, 30).timestamp()
    assert logs_dialog._when(stamp) == datetime.fromtimestamp(stamp).strftime("%Y-%m-%d %H:%M")
