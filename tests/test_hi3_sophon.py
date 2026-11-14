# SPDX-License-Identifier: GPL-3.0-or-later
"""Honkai Impact 3rd on Sophon chunk manifests, like Genshin, HSR and ZZZ (#1273).

Hi3's Sophon build is two manifests: ``game`` (executables, ~1.3 GiB) and ``asb`` (asset bundles,
~33 GiB), with no audio. Checked 2026-11-12 against 9.1.0: together they list exactly the files
of the 7z Nadare installed before (1103 files, same paths and sizes), so an existing install is
only verified chunk by chunk, never downloaded again or removed. Sophon also brings predownload.
Fixtures are the live replies of 2026-11-12.
"""

from __future__ import annotations

import json
import urllib.parse
from pathlib import Path

import pytest
from _backend_helpers import make_context, write_exe

from nadare.backends import direct
from nadare.core import directdl as dd
from nadare.core import sophon
from nadare.core.sources import hoyo

FIXTURES = Path(__file__).parent / "fixtures" / "catalog"
BRANCHES_JSON = json.loads((FIXTURES / "hoyo_getGameBranches_hi3.json").read_text())
BUILD_JSON = json.loads((FIXTURES / "hoyo_getBuild_hi3.json").read_text())
GAME_ID = "5TIVvvcwtM"
BRANCHES = (
    "https://sg-hyp-api.hoyoverse.com/hyp/hyp-connect/api/getGameBranches"
    f"?launcher_id=VYTpXlbWo8&game_ids%5B%5D={GAME_ID}"
)


def _build_url(branch: dict, name: str = "main") -> str:
    return (
        "https://sg-public-api.hoyoverse.com/downloader/sophon_chunk/api/getBuild?"
        + f"branch={name}&package_id={branch['package_id']}&password={branch['password']}"
    )


class FakeFetch:
    def __init__(self, routes):
        self.routes = routes
        self.calls: list[str] = []

    def __call__(self, url, headers=None):
        self.calls.append(url)
        if url not in self.routes:
            raise AssertionError(f"unexpected fetch: {url}")
        return self.routes[url]


def _fetch(branches=None, build=None, pre_build=None):
    branches = branches or BRANCHES_JSON
    main = branches["data"]["game_branches"][0]["main"]
    routes = {
        BRANCHES: json.dumps(branches).encode(),
        _build_url(main): json.dumps(build or BUILD_JSON).encode(),
    }
    pre = branches["data"]["game_branches"][0].get("pre_download")
    if pre:
        routes[_build_url(pre, pre.get("branch") or "predownload")] = json.dumps(
            pre_build or BUILD_JSON
        ).encode()
    return FakeFetch(routes)


def test_fixture_is_the_real_two_manifest_build():
    assert [m["matching_field"] for m in BUILD_JSON["data"]["manifests"]] == ["game", "asb"]
    assert urllib.parse.quote(GAME_ID) == GAME_ID


def test_hi3_resolves_to_the_game_and_asset_manifests():
    fetch = _fetch()
    plan = hoyo.resolve("bh3_global", fetch)
    assert plan.game_tag == "hi3" and plan.exe == "BH3.exe" and plan.version == "9.1.0"
    assert not plan.archives and not plan.files
    game, assets = plan.sophon
    assert game.name == "hi3-9.1.0" and assets.name == "hi3-9.1.0-asb"
    manifests = {m["matching_field"]: m for m in BUILD_JSON["data"]["manifests"]}
    assert game.manifest_id == manifests["game"]["manifest"]["id"]
    assert assets.manifest_id == manifests["asb"]["manifest"]["id"]
    assert assets.size == int(manifests["asb"]["stats"]["uncompressed_size"])
    assert all("getGamePackages" not in url for url in fetch.calls)


def test_hi3_ignores_the_audio_language():
    plan = hoyo.resolve("bh3_global", _fetch(), language="ja-jp")
    assert [p.name for p in plan.sophon] == ["hi3-9.1.0", "hi3-9.1.0-asb"]


def test_hi3_without_its_asset_manifest_is_refused():
    build = json.loads(json.dumps(BUILD_JSON))
    build["data"]["manifests"] = [
        m for m in build["data"]["manifests"] if m["matching_field"] != "asb"
    ]
    with pytest.raises(dd.DirectError):
        hoyo.resolve("bh3_global", _fetch(build=build))


def test_older_versions_of_each_package_are_told_apart():
    game, assets = hoyo.resolve("bh3_global", _fetch()).sophon
    import re

    assert re.fullmatch(game.supersedes, "hi3-9.0.0")
    assert not re.fullmatch(game.supersedes, "hi3-9.0.0-asb")
    assert re.fullmatch(assets.supersedes, "hi3-9.0.0-asb")
    assert not re.fullmatch(assets.supersedes, "hi3-9.0.0")


def test_the_other_hoyo_games_still_need_their_audio():
    # Genshin/HSR/ZZZ are unchanged: an unknown language is still refused (see test_sophon.py).
    assert "hkrpg_global" in hoyo._SOPHON_GAME_IDS
    assert hoyo._SOPHON_GAME_IDS["bh3_global"] == GAME_ID


def test_hi3_predownload_reads_the_pre_download_branch():
    branches = json.loads(json.dumps(BRANCHES_JSON))
    entry = branches["data"]["game_branches"][0]
    entry["pre_download"] = dict(entry["main"], tag="9.2.0", branch="predownload",
                                 package_id="PrePkg0001", password="PrePass0001")
    pre_build = json.loads(json.dumps(BUILD_JSON))
    pre_build["data"]["tag"] = "9.2.0"
    plan = hoyo.resolve_predownload("bh3_global", _fetch(branches, pre_build=pre_build))
    assert plan is not None and plan.version == "9.2.0"
    assert [p.name for p in plan.sophon] == ["hi3-9.2.0", "hi3-9.2.0-asb"]


def test_hi3_without_a_window_has_no_predownload():
    assert hoyo.resolve_predownload("bh3_global", _fetch()) is None


def test_the_hi3_backend_offers_predownload():
    branches = json.loads(json.dumps(BRANCHES_JSON))
    entry = branches["data"]["game_branches"][0]
    entry["pre_download"] = dict(entry["main"], tag="9.2.0", branch="predownload",
                                 package_id="PrePkg0001", password="PrePass0001")
    plan = direct.Hi3Backend.predownload_factory(_fetch(branches))
    assert plan is not None and plan.version == "9.2.0"
    assert direct.Hi3Backend.predownload_factory(_fetch()) is None


def test_an_archive_install_moving_to_sophon_loses_no_file(tmp_path, monkeypatch):
    """The old 7z install leaves a .done record that owned every file; the Sophon sync must
    not treat those files as dropped (it verifies them in place instead)."""
    root = tmp_path / "hi3"
    (root / "BH3_Data").mkdir(parents=True)
    (root / "BH3.exe").write_bytes(b"exe")
    (root / "BH3_Data" / "data.unity3d").write_bytes(b"assets")
    state = root / dd.STATE_DIR
    state.mkdir()
    (state / "hi3-9.0.0.pack.done").write_text(json.dumps({
        "version": "9.0.0", "game": "hi3", "slot": None,
        "url": "https://autopatchglb.honkaiimpact3.com/x/BH3_v9.0.0.7z",
        "members": ["BH3.exe", "BH3_Data/data.unity3d"],
    }))
    synced = []
    monkeypatch.setattr(
        sophon, "sync_package",
        lambda pkg, dest, **kwargs: synced.append(pkg.name),
    )
    plan = hoyo.resolve("bh3_global", _fetch())
    dd.sync(plan, root)
    assert synced == ["hi3-9.1.0", "hi3-9.1.0-asb"]
    assert (root / "BH3.exe").read_bytes() == b"exe"
    assert (root / "BH3_Data" / "data.unity3d").read_bytes() == b"assets"


def test_the_hi3_backend_plan_is_sophon(tmp_path, monkeypatch):
    monkeypatch.setattr(hoyo, "default_fetch", _fetch())
    backend = direct.Hi3Backend(
        make_context(tmp_path, "direct-hi3", {"prefix": str(tmp_path / "pfx")})
    )
    write_exe(backend.direct_target(), "MZ")
    plan = backend.plan()
    assert [p.name for p in plan.sophon] == ["hi3-9.1.0", "hi3-9.1.0-asb"]
