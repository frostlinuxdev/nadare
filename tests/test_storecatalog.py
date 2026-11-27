# SPDX-License-Identifier: GPL-3.0-or-later
# Copyright (C) 2026 Nadare contributors

from __future__ import annotations

import os
import re
import subprocess
import sys
from types import SimpleNamespace

import pytest

from nadare.core import backends, diskspace, steamstore
from nadare.core.directkinds import (
    CATALOG_KINDS,
    DIRECT_LABEL,
    HELD_BACK_KINDS,
    TIER_EXPERIMENTAL,
    TIER_HELP,
    TIER_VERIFIED,
)
from nadare.core.diskspace import human_size
from nadare.core.storecatalog import StoreEntry, entries

#: The older backends that are not in directkinds but are in the Add Game wizard.
LEGACY_KINDS = {
    "stove-czn",
    "fgo-lepton",
    "arknights-yostar",
    "stellasora-yostar",
    "hbr-yostar",
    "azurpromilia-jp-yostar",
}


def test_entries_cover_catalog_once_and_sort_verified_first() -> None:
    result = entries()
    kinds = [entry.kind for entry in result]
    assert len(kinds) == len(set(kinds))
    steam_tiles = {kind for kind in kinds if steamstore.app_id_of(kind)}
    assert steam_tiles  # Steam games are tiles of their own (see test_steamstore.py)
    assert set(kinds) - steam_tiles == (set(CATALOG_KINDS) | LEGACY_KINDS) - set(
        backends.unavailable()
    ) - HELD_BACK_KINDS
    assert [entry.tier == TIER_VERIFIED for entry in result] == sorted(
        [entry.tier == TIER_VERIFIED for entry in result], reverse=True
    )
    for tier in (TIER_VERIFIED, TIER_EXPERIMENTAL):
        # Builds of one game sit together under the game's name; their order is declared.
        names = [(entry.group or entry.title) for entry in result if entry.tier == tier]
        assert names == sorted(names, key=str.casefold)


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
    assert result["direct-pgr"].has_art is True
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
    from nadare.gui.add_game import available_catalog

    wizard = {game.kind for game in available_catalog()}
    # Steam games are Store-only tiles: the Store adds them with the Steam set-up checklist.
    assert {e.kind for e in entries() if not steamstore.app_id_of(e.kind)} == wizard


def test_older_games_get_their_tiers_and_delivery_labels() -> None:
    by_kind = {entry.kind: entry for entry in entries()}
    for kind in ("stove-czn", "arknights-yostar", "stellasora-yostar"):
        assert by_kind[kind].tier == TIER_VERIFIED
        assert by_kind[kind].tier_help == TIER_HELP[TIER_VERIFIED]
    assert by_kind["stove-czn"].has_art is True  # a fixed official share image
    assert by_kind["arknights-yostar"].has_art is True  # newest news image
    assert by_kind["stellasora-yostar"].has_art is True
    assert by_kind["stove-czn"].title == "Chaos Zero Nightmare"
    assert by_kind["stove-czn"].delivery_label == DIRECT_LABEL
    assert by_kind["arknights-yostar"].title == "Arknights"
    assert by_kind["arknights-yostar"].delivery_label == DIRECT_LABEL
    assert by_kind["stellasora-yostar"].title == "Stella Sora"
    assert by_kind["stellasora-yostar"].delivery_label == DIRECT_LABEL
    if "fgo-lepton" in by_kind:
        assert by_kind["fgo-lepton"].title == "Fate/Grand Order"
        assert by_kind["fgo-lepton"].tier == TIER_EXPERIMENTAL
        assert by_kind["fgo-lepton"].tier_help == TIER_HELP[TIER_EXPERIMENTAL]
        assert by_kind["fgo-lepton"].delivery_label == "Android container"


@pytest.mark.parametrize(
    "kind",
    [
        "arknights-yostar",
        "stellasora-yostar",
        "hbr-yostar",
        "azurpromilia-jp-yostar",
    ],
)
def test_every_yostar_store_row_shows_its_installed_size(kind) -> None:
    """HBR and Azur Promilia are direct Yostar games now: their cards carry a size like the rest."""
    entry = next(item for item in entries() if item.kind == kind)
    expected = backends.install_size_hint(kind)
    assert expected and expected > 0
    assert entry.size_bytes == expected
    assert human_size(entry.size_bytes).endswith("GiB")  # one formatter, binary units


_SIZE_IN_TEXT = re.compile(
    r"\d+(?:\.\d+)?\s*(?:B|KiB|MiB|GiB|TiB|KB|MB|GB|TB)\b"
)


def _declared_size_strings(kind: str) -> set[str]:
    """``human_size`` of every size ``kind`` declares: its hints and its ``*_BYTES`` constants."""
    cls = backends.get(kind)
    sizes: set[int] = set()
    for base in cls.__mro__:
        for name, value in vars(base).items():
            if name.endswith("_BYTES") and isinstance(value, int):
                sizes.add(value)
    module = sys.modules[cls.__module__]
    for name, value in vars(module).items():
        if name.endswith("_BYTES") and isinstance(value, int):
            sizes.add(value)
    for name in ("download_size_hint", "install_size_hint", "install_space_hint"):
        value = getattr(cls, name, None)
        if isinstance(value, int):
            sizes.add(value)
    space = getattr(cls, "install_space_hint", None)
    if isinstance(space, int):
        # A keep-free figure is the space check's own need: the peak plus its margin.
        sizes.add(diskspace.free_needed(space))
    return {human_size(size) for size in sizes}


def _notice_kinds_with_a_size() -> list[str]:
    kinds = []
    for kind in backends.kinds():
        notice = backends.page_notice(kind)
        if notice and _SIZE_IN_TEXT.search(notice[0]):
            kinds.append(kind)
    return kinds


@pytest.mark.parametrize("kind", _notice_kinds_with_a_size())
def test_every_size_in_a_page_notice_is_a_declared_hint_in_one_unit(kind) -> None:
    """One unit everywhere: each page size is ``human_size`` of a declared hint."""
    text = backends.page_notice(kind)[0]
    declared = _declared_size_strings(kind)
    found = _SIZE_IN_TEXT.findall(text)
    assert found, kind
    for token in found:
        assert token in declared, (kind, token, sorted(declared))
    assert not re.search(r"\d\s*GB\b", text), kind
    # The Store card shows the figure the Store sheet's download line names.
    entry = next(item for item in entries() if item.kind == kind)
    assert backends.install_choice(kind) is None
    assert entry.size_bytes == backends.download_size_hint(kind)


def test_games_with_a_separate_download_declare_it() -> None:
    """DNA (old peak), Endfield and ToF download a different figure than they install."""
    for kind in ("direct-dna", "direct-endfield", "direct-tof"):
        cls = backends.get(kind)
        assert cls.download_size_hint is not None, kind
        assert cls.download_size_hint != cls.install_size_hint, kind
    # DNA used to store its unpacking peak in install_size_hint.
    dna = backends.get("direct-dna")
    assert dna.install_space_hint > dna.install_size_hint


@pytest.mark.parametrize(
    "helper, attribute",
    [
        (backends.download_size_hint, "download_size_hint"),
        (backends.install_space_hint, "install_space_hint"),
    ],
)
def test_a_declared_zero_hint_is_kept_not_treated_as_unset(monkeypatch, helper, attribute):
    monkeypatch.setattr(
        backends,
        "get",
        lambda kind: SimpleNamespace(install_size_hint=5, **{attribute: 0}),
    )
    assert helper("any-kind") == 0


@pytest.mark.parametrize(
    "helper, attribute",
    [
        (backends.download_size_hint, "download_size_hint"),
        (backends.install_space_hint, "install_space_hint"),
    ],
)
def test_an_undeclared_hint_falls_back_to_the_install_size(monkeypatch, helper, attribute):
    monkeypatch.setattr(backends, "get", lambda kind: SimpleNamespace(install_size_hint=5))
    assert helper("any-kind") == 5


@pytest.mark.parametrize(
    "helper",
    [backends.download_size_hint, backends.install_space_hint],
)
def test_an_unknown_kind_has_no_hint(monkeypatch, helper):
    def unknown(kind):
        raise backends.BackendError("no such backend")

    monkeypatch.setattr(backends, "get", unknown)
    assert helper("no-such-kind") is None


def test_older_games_follow_the_same_filters_and_availability(monkeypatch) -> None:
    assert [entry.kind for entry in entries(query="chaos")] == ["stove-czn"]
    verified = {entry.kind for entry in entries(tier=TIER_VERIFIED)}
    assert {"stove-czn", "arknights-yostar", "stellasora-yostar"} <= verified
    assert "fgo-lepton" not in verified
    monkeypatch.setattr(backends, "unavailable", lambda: {"fgo-lepton", "stove-czn"})
    kinds = {entry.kind for entry in entries()}
    assert not {"fgo-lepton", "stove-czn"} & kinds
    assert {
        "arknights-yostar",
        "stellasora-yostar",
        "hbr-yostar",
        "azurpromilia-jp-yostar",
    } <= kinds
