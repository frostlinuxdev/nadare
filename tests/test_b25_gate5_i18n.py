# SPDX-License-Identifier: GPL-3.0-or-later
"""Gate 5: Slavic counts ending in 12–14 must use the many form."""

from __future__ import annotations

import json

import pytest

from nadare.core import i18n


@pytest.mark.parametrize("locale", ["ru", "uk", "pl"])
@pytest.mark.parametrize("count", [12, 13, 14, 112, 113, 114])
def test_teens_do_not_use_the_few_plural_form(tmp_path, monkeypatch, locale, count):
    catalogue = {"items": {"one": "one", "few": "few", "many": "many", "other": "other"}}
    (tmp_path / f"{locale}.json").write_text(json.dumps(catalogue), encoding="utf-8")
    monkeypatch.setattr(i18n, "locale_dir", lambda: tmp_path)
    monkeypatch.setattr(i18n, "_cache", {})
    assert i18n.translate_plural("items", count, locale=locale) == "many"
