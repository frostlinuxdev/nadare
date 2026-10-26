# SPDX-License-Identifier: GPL-3.0-or-later
# Copyright (C) 2026 Nadare contributors

from __future__ import annotations

import os
import subprocess
import sys

from nadare.core import backends
from nadare.core.directkinds import (
    CATALOG_KINDS,
    DIRECT_LABEL,
    TIER_EXPERIMENTAL,
    TIER_HELP,
    TIER_VERIFIED,
)
from nadare.core.storecatalog import StoreEntry, entries

#: The four older backends that are not in directkinds but are in the Add Game wizard.
LEGACY_KINDS = {"stove-czn", "fgo-lepton", "arknights-yostar", "stellasora-yostar"}


def test_entries_cover_catalog_once_and_sort_verified_first() -> None:
    result = entries()
    kinds = [entry.kind for entry in result]
    assert len(kinds) == len(set(kinds))
    assert set(kinds) == (set(CATALOG_KINDS) | LEGACY_KINDS) - set(backends.unavailable())
    assert [entry.tier == TIER_VERIFIED for entry in result] == sorted(
        [entry.tier == TIER_VERIFIED for entry in result], reverse=True
    )
    for tier in (TIER_VERIFIED, TIER_EXPERIMENTAL):
        titles = [entry.title for entry in result if entry.tier == tier]
        assert titles == sorted(titles, key=str.casefold)


def test_entries_hide_hoyo_when_configured(monkeypatch) -> None:
    monkeypatch.setenv("NADARE_HIDE_HOYO", "1")
    result = entries()
    assert not {"direct-genshin", "direct-hsr", "direct-hi3", "direct-zzz"} & {
        entry.kind for entry in result
    }


def test_entries_hide_unavailable_backends(monkeypatch) -> None:
    monkeypatch.setattr(backends, "unavailable", lambda: {"direct-bd2"})
    assert "direct-bd2" not in {entry.kind for entry in entries()}


def test_entries_filter_query_and_tier() -> None:
    result = entries(query="gray", tier=TIER_VERIFIED)
    assert [entry.kind for entry in result] == ["direct-pgr"]
    assert entries(query="no such title") == []
    assert all(entry.tier == TIER_EXPERIMENTAL for entry in entries(tier=TIER_EXPERIMENTAL))


def test_entries_include_backend_facts_and_installed_marker(monkeypatch) -> None:
    real_size_hint = backends.install_size_hint
    monkeypatch.setattr(
        backends,
        "install_size_hint",
        lambda kind: None if kind == "direct-bd2" else real_size_hint(kind),
    )
    result = {entry.kind: entry for entry in entries(installed_kinds={"direct-pgr"})}
    assert result["direct-pgr"].installed is True
    assert result["direct-bd2"].installed is False
    assert result["direct-pgr"].size_bytes == backends.install_size_hint("direct-pgr")
    assert result["direct-bd2"].size_bytes is None
    assert result["direct-pgr"].art_folder == "pgr"
    assert isinstance(result["direct-pgr"], StoreEntry)


def test_storecatalog_does_not_import_qt() -> None:
    code = (
        "import sys; import nadare.core.storecatalog; "
        "assert not any(x.startswith(('PyQt', 'PySide')) for x in sys.modules)"
    )
    result = subprocess.run(
        [sys.executable, "-c", code],
        check=False,
        capture_output=True,
        text=True,
        env={**os.environ, "PYTHONPATH": os.path.abspath("src")},
    )
    assert result.returncode == 0, result.stderr


def test_store_lists_exactly_the_games_the_wizard_offers() -> None:
    from nadare.gui.add_game import ADVANCED_GROUP, available_catalog

    wizard = {game.kind for game in available_catalog() if game.group != ADVANCED_GROUP}
    assert {entry.kind for entry in entries()} == wizard


def test_older_games_get_their_tiers_and_delivery_labels() -> None:
    by_kind = {entry.kind: entry for entry in entries()}
    for kind in ("stove-czn", "arknights-yostar", "stellasora-yostar"):
        assert by_kind[kind].tier == TIER_VERIFIED
        assert by_kind[kind].tier_help == TIER_HELP[TIER_VERIFIED]
        assert by_kind[kind].art_folder is None
    assert by_kind["stove-czn"].title == "Chaos Zero Nightmare"
    assert by_kind["stove-czn"].delivery_label == DIRECT_LABEL
    assert by_kind["arknights-yostar"].title == "Arknights"
    assert by_kind["arknights-yostar"].delivery_label == "Publisher's installer"
    assert by_kind["stellasora-yostar"].title == "Stella Sora"
    assert by_kind["stellasora-yostar"].delivery_label == "Publisher's installer"
    if "fgo-lepton" in by_kind:
        assert by_kind["fgo-lepton"].title == "Fate/Grand Order"
        assert by_kind["fgo-lepton"].tier == TIER_EXPERIMENTAL
        assert by_kind["fgo-lepton"].tier_help == TIER_HELP[TIER_EXPERIMENTAL]
        assert by_kind["fgo-lepton"].delivery_label == "Android container"


def test_older_games_follow_the_same_filters_and_availability(monkeypatch) -> None:
    assert [entry.kind for entry in entries(query="chaos")] == ["stove-czn"]
    verified = {entry.kind for entry in entries(tier=TIER_VERIFIED)}
    assert {"stove-czn", "arknights-yostar", "stellasora-yostar"} <= verified
    assert "fgo-lepton" not in verified
    monkeypatch.setattr(backends, "unavailable", lambda: {"fgo-lepton", "stove-czn"})
    kinds = {entry.kind for entry in entries()}
    assert not {"fgo-lepton", "stove-czn"} & kinds
    assert {"arknights-yostar", "stellasora-yostar"} <= kinds
