# SPDX-License-Identifier: GPL-3.0-or-later
"""Wuthering Waves resource quality (SD / HD / UHD): the bundle index and its packs."""

from __future__ import annotations

import json

import pytest

from nadare.core import directdl as dd
from nadare.core.sources import kuro, wuwa

ROOT = "https://cdn-high.example/"
INDEX_URL = (
    "https://prod-alicdn-gamestarter.kurogame.com/launcher/game/"
    f"50004_{wuwa.LAUNCHER_APP_KEY}/G153/official/index.json"
)


def _pack(name: str, files: list[tuple[str, int]]) -> tuple[dict, bytes]:
    entry = {
        "version": "3.7.2",
        "indexFile": f"launcher/G153/3.7.2/{name}/indexFile.json",
        "indexFileMd5": "0" * 32,
        "baseUrl": "launcher/G153/3.7.2/zip/",
        "size": str(sum(size for _p, size in files)),
    }
    body = json.dumps(
        {"resource": [{"dest": d, "md5": f"{i:032x}", "size": s} for i, (d, s) in enumerate(files)]}
    ).encode()
    return entry, body


PACKS = {
    "common": [("Wuthering Waves.exe", 10), ("Client/Content/Paks/pakchunk0.pak", 100)],
    "sd": [("Client/Content/SD/pakchunk70-SD.pak", 20)],
    "hd": [("Client/Content/HD/pakchunk70-HD.pak", 45)],
    "uhd": [("Client/Content/UHD/pakchunk70-UHD.pak", 66)],
}


def _routes() -> dict[str, bytes]:
    packs, routes = {}, {}
    for name, files in PACKS.items():
        entry, body = _pack(name, files)
        packs[name] = entry
        routes[ROOT + entry["indexFile"]] = body
    index = {
        "cdnList": [{"P": 1, "url": "https://cdn-low.example/"}, {"P": 9, "url": ROOT}],
        "resourcePacks": packs,
        "bundles": {
            "SD": {"resourcePacks": ["common", "sd"]},
            "HD": {"resourcePacks": ["common", "hd"]},
            "UHD": {"resourcePacks": ["common", "uhd"]},
        },
    }
    routes[INDEX_URL] = json.dumps(index).encode()
    return routes


def _fetch(routes):
    def fetch(url, headers=None):
        try:
            return routes[url]
        except KeyError:
            raise dd.DirectError(f"HTTP 404 for {url}") from None

    return fetch


@pytest.mark.parametrize(
    ("tier", "extra"),
    [("hd", "HD"), ("sd", "SD"), ("uhd", "UHD"), ("UHD", "UHD"), (" Hd ", "HD")],
)
def test_a_tier_is_the_common_pack_plus_its_own(tier, extra):
    plan = wuwa.resolve(_fetch(_routes()), quality=tier)
    paths = [f.path for f in plan.files]
    assert paths[:2] == [p for p, _ in PACKS["common"]]
    assert paths[2:] == [p for p, _ in PACKS[extra.lower()]]
    assert plan.version == "3.7.2" and plan.exe == "Wuthering Waves.exe"
    assert plan.game_tag == "wuwa"


def test_files_are_fetched_from_the_fastest_cdn_under_the_pack_base():
    plan = wuwa.resolve(_fetch(_routes()), quality="hd")
    first = plan.files[1]
    assert first.url == ROOT + "launcher/G153/3.7.2/zip/Client/Content/Paks/pakchunk0.pak"
    assert first.size == 100 and len(first.md5) == 32


def test_the_default_tier_is_hd():
    plan = wuwa.resolve(_fetch(_routes()))
    assert any("Content/HD/" in f.path for f in plan.files)
    assert not any("Content/UHD/" in f.path or "Content/SD/" in f.path for f in plan.files)


def test_an_unknown_tier_says_which_exist():
    with pytest.raises(dd.DirectError, match="SD, HD, UHD|HD, SD, UHD|UHD"):
        kuro.resolve_bundle(
            INDEX_URL, game_tag="wuwa", exe="x.exe", tier="ultra", fetch=_fetch(_routes())
        )


def test_hd_falls_back_to_the_old_index_when_the_new_one_is_gone(monkeypatch):
    sentinel = dd.Plan(game_tag="wuwa", version="old")
    monkeypatch.setattr(wuwa.kuro, "resolve_with_tokens", lambda **kw: sentinel)
    assert wuwa.resolve(_fetch({}), quality="hd") is sentinel


@pytest.mark.parametrize("tier", ["sd", "uhd"])
def test_a_tier_the_old_index_cannot_give_is_an_error_not_a_silent_hd(tier, monkeypatch):
    monkeypatch.setattr(
        wuwa.kuro, "resolve_with_tokens", lambda **kw: pytest.fail("must not fall back")
    )
    with pytest.raises(dd.DirectError):
        wuwa.resolve(_fetch({}), quality=tier)


def test_a_path_in_two_packs_is_refused():
    routes = _routes()
    PACKS_BAD = {"resource": [{"dest": "Wuthering Waves.exe", "md5": "1" * 32, "size": 1}]}
    routes[ROOT + "launcher/G153/3.7.2/hd/indexFile.json"] = json.dumps(PACKS_BAD).encode()
    with pytest.raises(dd.DirectError, match="more than one pack"):
        kuro.resolve_bundle(
            INDEX_URL, game_tag="wuwa", exe="x.exe", tier="hd", fetch=_fetch(routes)
        )


def test_a_traversal_dest_in_a_tier_pack_is_refused():
    routes = _routes()
    bad = {"resource": [{"dest": "../evil.dll", "md5": "1" * 32, "size": 1}]}
    routes[ROOT + "launcher/G153/3.7.2/uhd/indexFile.json"] = json.dumps(bad).encode()
    with pytest.raises(dd.DirectError):
        kuro.resolve_bundle(
            INDEX_URL, game_tag="wuwa", exe="x.exe", tier="uhd", fetch=_fetch(routes)
        )


def test_a_bundle_naming_a_missing_pack_is_refused():
    routes = _routes()
    index = json.loads(routes[INDEX_URL])
    index["bundles"]["HD"]["resourcePacks"] = ["common", "nope"]
    routes[INDEX_URL] = json.dumps(index).encode()
    with pytest.raises(dd.DirectError, match="nope"):
        kuro.resolve_bundle(
            INDEX_URL, game_tag="wuwa", exe="x.exe", tier="hd", fetch=_fetch(routes)
        )


def test_a_non_https_cdn_is_refused():
    routes = _routes()
    index = json.loads(routes[INDEX_URL])
    index["cdnList"] = [{"P": 9, "url": "http://cdn.example/"}]
    routes[INDEX_URL] = json.dumps(index).encode()
    with pytest.raises(dd.DirectError):
        kuro.resolve_bundle(
            INDEX_URL, game_tag="wuwa", exe="x.exe", tier="hd", fetch=_fetch(routes)
        )


def test_quality_values_are_normalised():
    assert wuwa.quality_of("UHD") == "uhd"
    assert wuwa.quality_of(" sd ") == "sd"
    assert wuwa.quality_of("") == "hd"
    assert wuwa.quality_of(None) == "hd"
    assert wuwa.quality_of("ultra") == "hd"


# ---- every selected pack takes part in the version (#905) ---------------------------------------
def _with_pack_version(routes, pack, version):
    index = json.loads(routes[INDEX_URL])
    index["resourcePacks"][pack]["version"] = version
    routes[INDEX_URL] = json.dumps(index).encode()


def test_packs_at_the_same_version_give_the_plain_version():
    assert wuwa.resolve(_fetch(_routes()), quality="hd").version == "3.7.2"


def test_a_tier_pack_that_moved_alone_changes_the_version():
    base = wuwa.resolve(_fetch(_routes()), quality="hd").version
    routes = _routes()
    _with_pack_version(routes, "hd", "3.7.3")
    moved = wuwa.resolve(_fetch(routes), quality="hd").version
    assert moved != base and "3.7.3" in moved and "3.7.2" in moved


def test_a_pack_of_another_tier_does_not_change_this_tiers_version():
    base = wuwa.resolve(_fetch(_routes()), quality="hd").version
    routes = _routes()
    _with_pack_version(routes, "uhd", "9.9.9")
    assert wuwa.resolve(_fetch(routes), quality="hd").version == base


def test_the_common_pack_moving_changes_every_tier():
    for tier in ("sd", "hd", "uhd"):
        base = wuwa.resolve(_fetch(_routes()), quality=tier).version
        routes = _routes()
        _with_pack_version(routes, "common", "3.8.0")
        assert wuwa.resolve(_fetch(routes), quality=tier).version != base
