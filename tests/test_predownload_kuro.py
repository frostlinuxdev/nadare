# SPDX-License-Identifier: GPL-3.0-or-later
"""Predownload for Kuro games (Wuthering Waves, Punishing: Gray Raven) (#1273).

During a predownload window Kuro's ``index.json`` carries a ``predownload`` block shaped like
the current release: on the bundle index (launcher 3.0) it has its own ``cdnList``,
``resourcePacks`` (with ``patchConfig``) and ``resourceLevels`` (tier -> pack names); on the
older single-list index it has a ``config`` like ``default.config`` and borrows the default CDN
list. The shapes here are cut down from a real WuWa 3.7.2 -> 3.8.0 window (2026-11-10).

A Kuro predownload fetches what the publisher's own update from the installed version
downloads (its patch list: ``.krpdiff`` patches plus whole files) into
``<game>/.nadare/predownload/<version>/files/<md5>``. The update that follows takes them from
that cache before the network. All offline.
"""

from __future__ import annotations

import copy
import hashlib
import json
import os
from pathlib import Path

import pytest
from _backend_helpers import make_context, write_exe
from _krpdiff import build

from nadare.backends import direct
from nadare.core import directdl as dd
from nadare.core import kuropatch
from nadare.core.sources import kuro, pgr, wuwa

CDN = "https://cdn-a.example/"
PRE_CDN = "https://cdn-pre.example/"
BUNDLE = "https://index.example/G153/official/index.json"


def md5(data: bytes) -> str:
    return hashlib.md5(data).hexdigest()


def _list(files: dict[str, bytes]) -> bytes:
    return json.dumps(
        {"resource": [{"dest": p, "md5": md5(d), "size": len(d)} for p, d in files.items()]}
    ).encode()


def _pack(version: str, folder: str, name: str, patches=()) -> dict:
    return {
        "version": version,
        "indexFile": f"{folder}/{name}/indexFile.json",
        "indexFileMd5": "0" * 32,
        "baseUrl": f"{folder}/zip/",
        "size": 1,
        "unCompressSize": 1,
        "patchType": "patch",
        "patchConfig": list(patches),
    }


CURRENT = {"common": {"Client/a.pak": b"a-372"}, "hd": {"Client/HD/h.pak": b"h-372"}}
NEXT = {
    "common": {"Client/a.pak": b"a-380", "Client/new.pak": b"n-380"},
    "hd": {"Client/HD/h.pak": b"h-380"},
    "uhd": {"Client/UHD/u.pak": b"u-380"},
}


def _bundle_index(*, predownload: bool = True) -> dict:
    cur = "game/3.7.2/c"
    nxt = "game/3.8.0/n"
    index = {
        "cdnList": [{"url": CDN, "P": 10, "K1": 1, "K2": 1}],
        "resourcePacks": {name: _pack("3.7.2", cur, name) for name in ("common", "hd", "uhd")},
        "bundles": {
            "HD": {"resourcePacks": ["common", "hd"], "config": {}},
            "UHD": {"resourcePacks": ["common", "uhd"], "config": {}},
        },
        "config": {"predownloadSwitch": 1},
        "predownload": None,
    }
    if predownload:
        index["predownload"] = {
            "version": "3.8.0",
            "cdnList": [
                {"url": "https://slow.example/", "P": 1, "K1": 1, "K2": 1},
                {"url": PRE_CDN, "P": 50, "K1": 1, "K2": 1},
            ],
            "resourceLevel": "HD",
            "resourceLevels": {
                "HD": {"version": "3.8.0", "resourcePacks": ["common", "hd"]},
                "UHD": {"version": "3.8.0", "resourcePacks": ["common", "uhd"]},
            },
            "resourcePacks": {
                name: _pack(
                    "3.8.0", nxt, name,
                    [{"version": "3.7.2", "indexFile": f"{nxt}/{name}/3.7.2/indexFile.json",
                      "baseUrl": f"{nxt}/{name}/3.7.2/resources", "size": 7}],
                )
                for name in ("common", "hd", "uhd")
            },
            "config": {"predownloadSwitch": 1},
        }
    return index


def _bundle_fetch(index: dict, seen: list[str] | None = None):
    served = {BUNDLE: json.dumps(index).encode()}
    for name, files in CURRENT.items():
        served[f"{CDN}game/3.7.2/c/{name}/indexFile.json"] = _list(files)
    for name, files in NEXT.items():
        served[f"{PRE_CDN}game/3.8.0/n/{name}/indexFile.json"] = _list(files)

    def fetch(url, headers=None):
        if seen is not None:
            seen.append(url)
        try:
            return served[url]
        except KeyError:
            raise dd.DirectError(f"404 {url}") from None

    return fetch


# --------------------------------------------------------------- bundle index (WuWa)
def test_bundle_predownload_is_the_next_version_from_its_own_cdn():
    fetch = _bundle_fetch(_bundle_index())
    plan = kuro.resolve_bundle(
        BUNDLE, game_tag="wuwa", exe="W.exe", tier="hd", fetch=fetch, predownload=True
    )
    assert plan is not None
    assert plan.version == "3.8.0"
    by_path = {rf.path: rf for rf in plan.files}
    assert set(by_path) == {"Client/a.pak", "Client/new.pak", "Client/HD/h.pak"}
    assert by_path["Client/a.pak"].md5 == md5(b"a-380")
    # The predownload block's fastest CDN, under that pack's own baseUrl.
    assert by_path["Client/HD/h.pak"].url == f"{PRE_CDN}game/3.8.0/n/zip/Client/HD/h.pak"
    info = plan.extra[kuropatch.EXTRA_KEY]
    assert info["root"] == PRE_CDN
    assert set(info["packs"]) == {"common", "hd"}
    assert info["packs"]["hd"][0]["version"] == "3.7.2"


def test_bundle_predownload_follows_the_chosen_quality():
    fetch = _bundle_fetch(_bundle_index())
    plan = kuro.resolve_bundle(
        BUNDLE, game_tag="wuwa", exe="W.exe", tier="uhd", fetch=fetch, predownload=True
    )
    assert {rf.path for rf in plan.files} == {"Client/a.pak", "Client/new.pak", "Client/UHD/u.pak"}


def test_bundle_without_a_window_has_no_predownload():
    for value in (None, {}, "nope"):
        index = _bundle_index()
        index["predownload"] = value
        assert kuro.resolve_bundle(
            BUNDLE, game_tag="wuwa", exe="W.exe", tier="hd",
            fetch=_bundle_fetch(index), predownload=True,
        ) is None
    index = _bundle_index()
    del index["predownload"]
    assert kuro.resolve_bundle(
        BUNDLE, game_tag="wuwa", exe="W.exe", tier="hd",
        fetch=_bundle_fetch(index), predownload=True,
    ) is None


def test_bundle_predownload_without_its_own_cdn_list_uses_the_index_one():
    index = _bundle_index()
    del index["predownload"]["cdnList"]
    served = _bundle_fetch(index)

    def fetch(url, headers=None):  # the next version's lists, on the main CDN this time
        return served(url.replace(CDN + "game/3.8.0", PRE_CDN + "game/3.8.0"))

    plan = kuro.resolve_bundle(
        BUNDLE, game_tag="wuwa", exe="W.exe", tier="hd", fetch=fetch, predownload=True
    )
    assert plan.extra[kuropatch.EXTRA_KEY]["root"] == CDN


def test_bundle_predownload_without_resource_levels_uses_the_bundles():
    index = _bundle_index()
    del index["predownload"]["resourceLevels"]
    plan = kuro.resolve_bundle(
        BUNDLE, game_tag="wuwa", exe="W.exe", tier="hd",
        fetch=_bundle_fetch(index), predownload=True,
    )
    assert {rf.path for rf in plan.files} == {"Client/a.pak", "Client/new.pak", "Client/HD/h.pak"}


def test_bundle_predownload_refuses_plain_http_and_unsafe_paths():
    index = _bundle_index()
    index["predownload"]["cdnList"] = [{"url": "http://cdn.example/", "P": 9}]
    with pytest.raises(dd.DirectError):
        kuro.resolve_bundle(
            BUNDLE, game_tag="wuwa", exe="W.exe", tier="hd",
            fetch=_bundle_fetch(index), predownload=True,
        )
    index = _bundle_index()
    served = _bundle_fetch(index)

    def evil(url, headers=None):
        if url.endswith("3.8.0/n/hd/indexFile.json"):
            return _list({"../../etc/passwd": b"x"})
        return served(url)

    with pytest.raises(dd.DirectError):
        kuro.resolve_bundle(
            BUNDLE, game_tag="wuwa", exe="W.exe", tier="hd", fetch=evil, predownload=True
        )


def test_the_normal_bundle_plan_is_unchanged_by_a_window():
    plan = kuro.resolve_bundle(
        BUNDLE, game_tag="wuwa", exe="W.exe", tier="hd", fetch=_bundle_fetch(_bundle_index())
    )
    assert plan.version == "3.7.2"
    assert {rf.path for rf in plan.files} == {"Client/a.pak", "Client/HD/h.pak"}


# --------------------------------------------------------------- older single-list index
LEGACY = "https://index.example/G143/50015_tok/index.json"


def _legacy_index(*, predownload: bool = True) -> dict:
    index = {
        "default": {
            "version": "4.9.0",
            "cdnList": [{"url": CDN, "P": 10}],
            "config": {
                "version": "4.9.0", "baseUrl": "g/4.9.0/zip/",
                "indexFile": "g/4.9.0/indexFile.json", "patchConfig": [],
            },
        },
        "predownloadSwitch": 1,
    }
    if predownload:
        index["predownload"] = {
            "version": "4.10.0",
            "config": {
                "version": "4.10.0", "baseUrl": "g/4.10.0/zip/",
                "indexFile": "g/4.10.0/indexFile.json",
                "patchConfig": [{"version": "4.9.0", "indexFile": "g/4.10.0/p/4.9.0.json",
                                 "baseUrl": "g/4.10.0/p/4.9.0/"}],
            },
        }
    return index


def _legacy_fetch(index: dict):
    served = {
        LEGACY: json.dumps(index).encode(),
        f"{CDN}g/4.9.0/indexFile.json": _list({"PGR.exe": b"old"}),
        f"{CDN}g/4.10.0/indexFile.json": _list({"PGR.exe": b"new", "x.pak": b"x"}),
    }

    def fetch(url, headers=None):
        try:
            return served[url]
        except KeyError:
            raise dd.DirectError(f"404 {url}") from None

    return fetch


def test_legacy_predownload_reads_its_config_with_the_default_cdn():
    plan = kuro.resolve_game(
        LEGACY, game_tag="pgr", exe="PGR.exe", fetch=_legacy_fetch(_legacy_index()),
        predownload=True,
    )
    assert plan.version == "4.10.0"
    assert {rf.path: rf.url for rf in plan.files} == {
        "PGR.exe": f"{CDN}g/4.10.0/zip/PGR.exe",
        "x.pak": f"{CDN}g/4.10.0/zip/x.pak",
    }
    assert plan.extra[kuropatch.EXTRA_KEY]["packs"][""][0]["version"] == "4.9.0"


def test_legacy_without_a_window_has_no_predownload():
    fetch = _legacy_fetch(_legacy_index(predownload=False))
    assert kuro.resolve_game(
        LEGACY, game_tag="pgr", exe="PGR.exe", fetch=fetch, predownload=True
    ) is None
    # ...and the normal plan still reads the default.
    assert kuro.resolve_game(LEGACY, game_tag="pgr", exe="PGR.exe", fetch=fetch).version == "4.9.0"


def test_pgr_predownload_uses_the_token_chain(monkeypatch):
    seen = []

    def fake(url, **kwargs):
        seen.append((url, kwargs.get("predownload")))
        return None

    monkeypatch.setattr(kuro, "resolve_game", fake)
    assert pgr.resolve_predownload(fetch=lambda u, h=None: b"{}") is None
    assert seen and all(flag is True for _, flag in seen)
    assert "G143/50015_" in seen[0][0]


# --------------------------------------------------------------- WuWa source
def test_wuwa_predownload_asks_the_bundle_index_with_the_quality(monkeypatch):
    asked = {}

    def fake_bundle(url, **kwargs):
        asked.update(kwargs, url=url)
        return "PLAN"

    monkeypatch.setattr(kuro, "resolve_bundle", fake_bundle)
    assert wuwa.resolve_predownload(quality="uhd") == "PLAN"
    assert asked["url"] == wuwa.BUNDLE_INDEX_URL
    assert asked["tier"] == "uhd" and asked["predownload"] is True


def test_wuwa_predownload_falls_back_to_the_older_index_for_hd_only(monkeypatch):
    def broken(url, **kwargs):
        raise dd.DirectError("bundle index gone")

    legacy = []
    monkeypatch.setattr(kuro, "resolve_bundle", broken)
    monkeypatch.setattr(
        kuro, "resolve_with_tokens",
        lambda **kwargs: legacy.append(kwargs) or "LEGACY",
    )
    assert wuwa.resolve_predownload(quality="hd") == "LEGACY"
    assert legacy[0]["predownload"] is True
    with pytest.raises(dd.DirectError):
        wuwa.resolve_predownload(quality="sd")


def test_wuwa_predownload_none_means_no_window(monkeypatch):
    monkeypatch.setattr(kuro, "resolve_bundle", lambda url, **kwargs: None)
    monkeypatch.setattr(
        kuro, "resolve_with_tokens", lambda **kwargs: pytest.fail("no fallback on None")
    )
    assert wuwa.resolve_predownload() is None


# --------------------------------------------------------------- patch list: what to fetch
OLD_A = bytes(range(256)) * 64
NEW_A = OLD_A[:4000] + b"CHANGED" + OLD_A[4000:]
WHOLE = b"whole-new-exe" * 100
PATCH_ROOT = "https://cdn.example/"


@pytest.fixture
def kgame(tmp_path, monkeypatch):
    """An installed 3.7.2 (one patched file, one whole-file change, one unchanged file)."""
    root = tmp_path / "install"
    (root / "Client").mkdir(parents=True)
    (root / "Client" / "a.pak").write_bytes(OLD_A)
    (root / "Client" / "game.exe").write_bytes(b"old-exe")
    (root / "Client" / "same.pak").write_bytes(b"same")
    patch = build(
        [("Client/a.pak", OLD_A)], [("Client/a.pak", NEW_A)],
        [("old", 0, 4000), ("new", b"CHANGED"), ("old", 4000, len(OLD_A) - 4000)],
    )
    listing = {
        "resource": [
            {"dest": "g0.krpdiff", "md5": md5(patch), "size": len(patch)},
            {"dest": "Client/game.exe", "md5": md5(WHOLE), "size": len(WHOLE),
             "fromFolder": "zip/"},
        ],
        "groupInfos": [{
            "dest": "g0.krpdiff",
            "srcFiles": [{"dest": "Client/a.pak", "md5": md5(OLD_A), "size": len(OLD_A)}],
            "dstFiles": [{"dest": "Client/a.pak", "md5": md5(NEW_A), "size": len(NEW_A)}],
        }],
        "deleteFiles": [],
    }
    lists = {f"{PATCH_ROOT}p/3.7.2/index.json": json.dumps(listing).encode()}
    blobs = {
        f"{PATCH_ROOT}p/3.7.2/res/g0.krpdiff": patch,
        "https://x/game.exe": WHOLE,
        "https://x/a.pak": NEW_A,
        "https://x/same.pak": b"same",
    }
    downloads: list[str] = []

    def fetch(url, headers=None):
        return lists[url]

    def download(rf, path, *, emit=None, cancel=None, pool=None):
        downloads.append(rf.url)
        data = blobs[rf.url]
        Path(path).parent.mkdir(parents=True, exist_ok=True)
        Path(path).write_bytes(data)

    monkeypatch.setattr(dd, "_download_remote", download)
    plan = dd.Plan(
        game_tag="wuwa", version="3.8.0", exe="Client/game.exe",
        files=(
            dd.RemoteFile("Client/a.pak", "https://x/a.pak", len(NEW_A), md5(NEW_A)),
            dd.RemoteFile("Client/game.exe", "https://x/game.exe", len(WHOLE), md5(WHOLE)),
            dd.RemoteFile("Client/same.pak", "https://x/same.pak", 4, md5(b"same")),
        ),
        extra={kuropatch.EXTRA_KEY: {"root": PATCH_ROOT, "packs": {"common": [
            {"version": "3.7.2", "indexFile": "p/3.7.2/index.json", "baseUrl": "p/3.7.2/res"},
        ]}}},
    )
    return {"root": root, "plan": plan, "fetch": fetch, "downloads": downloads,
            "patch": patch, "cache": tmp_path / "cache"}


def test_prepare_lists_the_whole_files_of_the_patch(kgame):
    patches = kuropatch.prepare(kgame["plan"], "3.7.2", kgame["fetch"])
    assert patches.found is True
    assert patches.wholes == ("Client/game.exe",)
    assert [g.patch.path for g in patches.groups] == ["g0.krpdiff"]


def test_prepare_for_an_unknown_version_found_nothing(kgame):
    patches = kuropatch.prepare(kgame["plan"], "3.6.0", kgame["fetch"])
    assert patches.found is False and patches.wholes == ()


def test_prepare_found_needs_every_pack(kgame):
    plan = kgame["plan"]
    extra = copy.deepcopy(plan.extra)
    extra[kuropatch.EXTRA_KEY]["packs"]["hd"] = [{"version": "3.7.0", "indexFile": "x",
                                                  "baseUrl": "x"}]
    plan = dd.Plan(game_tag="wuwa", version="3.8.0", files=plan.files, extra=extra)
    assert kuropatch.prepare(plan, "3.7.2", kgame["fetch"]).found is False


def test_predownload_files_are_the_patches_and_the_plans_whole_files(kgame):
    patches = kuropatch.prepare(kgame["plan"], "3.7.2", kgame["fetch"])
    files = kuropatch.predownload_files(patches, kgame["plan"])
    assert [rf.url for rf in files] == [
        f"{PATCH_ROOT}p/3.7.2/res/g0.krpdiff", "https://x/game.exe",
    ]


def test_a_whole_file_the_plan_does_not_list_is_not_fetched(kgame):
    patches = kuropatch.prepare(kgame["plan"], "3.7.2", kgame["fetch"])
    plan = dd.Plan(game_tag="wuwa", version="3.8.0", files=kgame["plan"].files[:1],
                   extra=kgame["plan"].extra)
    assert [rf.path for rf in kuropatch.predownload_files(patches, plan)] == ["g0.krpdiff"]


# --------------------------------------------------------------- directdl: the file cache
def _snapshot(root: Path) -> dict[str, bytes]:
    return {p.relative_to(root).as_posix(): p.read_bytes()
            for p in sorted(root.rglob("*")) if p.is_file()}


def test_file_predownload_fills_the_cache_by_md5_and_leaves_the_game(kgame):
    patches = kuropatch.prepare(kgame["plan"], "3.7.2", kgame["fetch"])
    files = kuropatch.predownload_files(patches, kgame["plan"])
    before = _snapshot(kgame["root"])
    result = dd.predownload(kgame["plan"], kgame["root"], kgame["cache"], files=files)
    assert _snapshot(kgame["root"]) == before
    assert dd.cached_file(kgame["cache"], files[0]).read_bytes() == kgame["patch"]
    assert dd.cached_file(kgame["cache"], files[1]).read_bytes() == WHOLE
    assert dd.cached_file(kgame["cache"], files[1]) == kgame["cache"] / "files" / md5(WHOLE)
    assert result["files"] == 2 and result["bytes"] == len(kgame["patch"]) + len(WHOLE)
    assert dd.predownload_ready(kgame["plan"], kgame["cache"]) is True


def test_file_predownload_without_a_list_fetches_what_differs(kgame):
    dd.predownload(kgame["plan"], kgame["root"], kgame["cache"])
    assert sorted(kgame["downloads"]) == ["https://x/a.pak", "https://x/game.exe"]


def test_a_second_predownload_fetches_nothing_again(kgame):
    dd.predownload(kgame["plan"], kgame["root"], kgame["cache"])
    kgame["downloads"].clear()
    dd.predownload(kgame["plan"], kgame["root"], kgame["cache"])
    assert kgame["downloads"] == []


def test_not_ready_before_it_finished_or_for_another_version(kgame):
    assert dd.predownload_ready(kgame["plan"], kgame["cache"]) is False
    dd.predownload(kgame["plan"], kgame["root"], kgame["cache"])
    other = dd.Plan(game_tag="wuwa", version="3.9.0", files=kgame["plan"].files)
    assert dd.predownload_ready(other, kgame["cache"]) is False


def test_a_file_without_md5_cannot_be_cached(kgame):
    plan = dd.Plan(game_tag="wuwa", version="3.8.0",
                   files=(dd.RemoteFile("Client/x", "https://x/same.pak", 4),))
    with pytest.raises(dd.DirectError):
        dd.predownload(plan, kgame["root"], kgame["cache"], files=list(plan.files))
    bad = dd.RemoteFile("Client/x", "https://x/same.pak", 4, "../../etc")
    with pytest.raises(dd.DirectError):
        dd.cached_file(kgame["cache"], bad)


def test_sync_takes_cached_files_before_the_network(kgame):
    dd.predownload(kgame["plan"], kgame["root"], kgame["cache"])
    kgame["downloads"].clear()
    dd.sync(kgame["plan"], kgame["root"], chunk_cache=kgame["cache"])
    assert kgame["downloads"] == []
    assert (kgame["root"] / "Client" / "a.pak").read_bytes() == NEW_A
    assert (kgame["root"] / "Client" / "game.exe").read_bytes() == WHOLE


def test_sync_ignores_a_damaged_cached_file(kgame):
    dd.predownload(kgame["plan"], kgame["root"], kgame["cache"])
    rf = kgame["plan"].files[1]
    dd.cached_file(kgame["cache"], rf).write_bytes(b"X" * len(WHOLE))
    kgame["downloads"].clear()
    dd.sync(kgame["plan"], kgame["root"], chunk_cache=kgame["cache"])
    assert kgame["downloads"] == ["https://x/game.exe"]
    assert (kgame["root"] / "Client" / "game.exe").read_bytes() == WHOLE


def test_sync_never_follows_a_link_in_the_cache(kgame, tmp_path):
    rf = kgame["plan"].files[1]
    elsewhere = tmp_path / "elsewhere"
    elsewhere.write_bytes(WHOLE)
    link = kgame["cache"] / "files" / md5(WHOLE)
    link.parent.mkdir(parents=True)
    os.symlink(elsewhere, link)
    dd.sync(kgame["plan"], kgame["root"], chunk_cache=kgame["cache"])
    assert "https://x/game.exe" in kgame["downloads"]
    assert elsewhere.read_bytes() == WHOLE  # never moved away
    assert not (kgame["root"] / "Client" / "game.exe").is_symlink()
    assert rf.md5  # (fixture sanity)


# --------------------------------------------------------------- kuropatch.apply with a cache
def test_apply_uses_a_cached_patch(kgame):
    patches = kuropatch.prepare(kgame["plan"], "3.7.2", kgame["fetch"])
    dd.predownload(kgame["plan"], kgame["root"], kgame["cache"],
                   files=kuropatch.predownload_files(patches, kgame["plan"]))
    kgame["downloads"].clear()
    outcome = kuropatch.apply(patches, kgame["plan"], kgame["root"], cache=kgame["cache"])
    assert outcome.applied == 1 and outcome.downloaded_bytes == 0
    assert kgame["downloads"] == []
    assert (kgame["root"] / "Client" / "a.pak").read_bytes() == NEW_A


def test_apply_downloads_when_the_cached_patch_is_damaged(kgame):
    patches = kuropatch.prepare(kgame["plan"], "3.7.2", kgame["fetch"])
    group = patches.groups[0]
    cached = dd.cached_file(kgame["cache"], group.patch)
    cached.parent.mkdir(parents=True)
    cached.write_bytes(b"\0" * len(kgame["patch"]))
    outcome = kuropatch.apply(patches, kgame["plan"], kgame["root"], cache=kgame["cache"])
    assert outcome.applied == 1
    assert kgame["downloads"] == [group.patch.url]


# --------------------------------------------------------------- backends
def test_kuro_games_offer_predownload():
    assert direct.WutheringWavesBackend.predownload_factory is not None
    assert direct.PgrBackend.predownload_factory is not None


def _wuwa(tmp_path, monkeypatch, kgame, *, installed="3.7.2", pre=True, profile=None):
    plan = kgame["plan"]
    old = dd.Plan(game_tag="wuwa", version=installed, files=plan.files)
    asked: list[dict] = []
    monkeypatch.setattr(
        direct.WutheringWavesBackend, "plan_factory",
        staticmethod(lambda fetch=None, **kw: old),
    )

    def factory(fetch=None, **kwargs):
        asked.append(kwargs)
        return plan if pre else None

    monkeypatch.setattr(direct.WutheringWavesBackend, "predownload_factory", staticmethod(factory))
    monkeypatch.setattr(direct, "patch_list_fetch", kgame["fetch"])
    backend = direct.WutheringWavesBackend(
        make_context(tmp_path, "direct-wuwa", {
            "prefix": str(tmp_path / "pfx"), "install_dir": str(kgame["root"]),
            **(profile or {})})
    )
    write_exe(backend.direct_target(), "MZ")
    backend._write_version_file(installed, "wuwa")
    backend._write_latest(installed)
    monkeypatch.setattr(backend, "_ensure_default_runner", lambda strict=False: None, raising=False)
    monkeypatch.setattr(backend, "_ensure_direct_prefix", lambda: None, raising=False)
    return backend, asked


def test_the_wuwa_predownload_check_asks_with_the_profile_quality(tmp_path, monkeypatch, kgame):
    backend, asked = _wuwa(tmp_path, monkeypatch, kgame, profile={"wuwa_quality": "uhd"})
    backend.check_update()
    assert asked and asked[-1].get("quality") == "uhd"
    assert backend.status().data.get("predownload_version") == "3.8.0"


def test_wuwa_predownload_fetches_the_patch_list_into_the_cache(tmp_path, monkeypatch, kgame):
    backend, asked = _wuwa(tmp_path, monkeypatch, kgame)
    root = backend.install_dir()
    before = _snapshot(root)
    backend.predownload()
    assert asked[-1].get("quality") == "hd"
    assert sorted(kgame["downloads"]) == sorted(
        [f"{PATCH_ROOT}p/3.7.2/res/g0.krpdiff", "https://x/game.exe"]
    )
    cache = root / ".nadare" / "predownload" / "3.8.0"
    assert (cache / "files" / md5(WHOLE)).read_bytes() == WHOLE
    after = {k: v for k, v in _snapshot(root).items() if not k.startswith(".nadare/")}
    assert after == {k: v for k, v in before.items() if not k.startswith(".nadare/")}
    assert backend.status().data.get("predownload_ready") is True


def test_wuwa_predownload_with_no_patch_for_the_install_fetches_what_differs(
    tmp_path, monkeypatch, kgame
):
    backend, _ = _wuwa(tmp_path, monkeypatch, kgame, installed="3.6.0")
    backend.predownload()
    assert sorted(kgame["downloads"]) == ["https://x/a.pak", "https://x/game.exe"]


def test_wuwa_update_after_a_predownload_downloads_nothing(tmp_path, monkeypatch, kgame):
    backend, _ = _wuwa(tmp_path, monkeypatch, kgame)
    backend.predownload()
    kgame["downloads"].clear()
    # Release day: the main index now gives 3.8.0.
    monkeypatch.setattr(
        direct.WutheringWavesBackend, "plan_factory",
        staticmethod(lambda fetch=None, **kw: kgame["plan"]),
    )
    monkeypatch.setattr(
        direct.WutheringWavesBackend, "predownload_factory",
        staticmethod(lambda fetch=None, **kw: None),
    )
    backend.update()
    assert kgame["downloads"] == []
    root = backend.install_dir()
    assert (root / "Client" / "a.pak").read_bytes() == NEW_A
    assert (root / "Client" / "game.exe").read_bytes() == WHOLE
    assert backend._read_version_file()["version"] == "3.8.0"
    assert not (root / ".nadare" / "predownload").exists()


def test_without_a_window_wuwa_predownload_is_refused(tmp_path, monkeypatch, kgame):
    from nadare.core.backends import BackendError

    backend, _ = _wuwa(tmp_path, monkeypatch, kgame, pre=False)
    with pytest.raises(BackendError):
        backend.predownload()
    assert kgame["downloads"] == []


def test_a_hotfix_released_instead_still_uses_and_then_removes_the_cache(
    tmp_path, monkeypatch, kgame
):
    """Kuro often ships x.y.1 days after x.y.0: whole files cached for 3.8.0 still count."""
    backend, _ = _wuwa(tmp_path, monkeypatch, kgame)
    backend.predownload()
    kgame["downloads"].clear()
    hotfix = dd.Plan(game_tag="wuwa", version="3.8.1", files=kgame["plan"].files,
                     extra=kgame["plan"].extra)
    monkeypatch.setattr(
        direct.WutheringWavesBackend, "plan_factory", staticmethod(lambda fetch=None, **kw: hotfix)
    )
    monkeypatch.setattr(
        direct.WutheringWavesBackend, "predownload_factory",
        staticmethod(lambda fetch=None, **kw: None),
    )
    backend.update()
    assert "https://x/game.exe" not in kgame["downloads"]  # taken from the 3.8.0 cache
    root = backend.install_dir()
    assert (root / "Client" / "game.exe").read_bytes() == WHOLE
    assert (root / "Client" / "a.pak").read_bytes() == NEW_A
    assert not (root / ".nadare" / "predownload").exists()


def test_a_repair_before_the_release_keeps_the_predownload(tmp_path, monkeypatch, kgame):
    backend, _ = _wuwa(tmp_path, monkeypatch, kgame)
    old = dd.Plan(game_tag="wuwa", version="3.7.2", files=(
        dd.RemoteFile("Client/same.pak", "https://x/same.pak", 4, md5(b"same")),))
    monkeypatch.setattr(
        direct.WutheringWavesBackend, "plan_factory", staticmethod(lambda fetch=None, **kw: old)
    )
    backend.predownload()
    cache = backend.install_dir() / ".nadare" / "predownload" / "3.8.0"
    before = _snapshot(cache)
    backend.update()  # same version: a repair
    assert _snapshot(cache) == before
    assert backend.status().data.get("predownload_ready") is True
