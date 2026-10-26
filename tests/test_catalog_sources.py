# SPDX-License-Identifier: GPL-3.0-or-later
"""Tests for nadare.core.sources.*, run against recorded publisher responses."""

from __future__ import annotations

import gzip
import json
from pathlib import Path

import pytest

from nadare.core import directdl as dd
from nadare.core.sources import aethergazer, bd2, endfield, gfl2, hoyo, mongil, wuwa

FIX = Path(__file__).parent / "fixtures" / "catalog"


def fx(name: str) -> bytes:
    return (FIX / name).read_bytes()


class FakeFetch:
    """Serves ``routes`` (exact URL -> bytes) and records every call."""

    def __init__(self, routes: dict[str, bytes]):
        self.routes = routes
        self.calls: list[tuple[str, dict[str, str]]] = []

    def __call__(self, url, headers=None):
        self.calls.append((url, dict(headers or {})))
        if url not in self.routes:
            raise AssertionError(f"unexpected fetch: {url}")
        return self.routes[url]


def all_paths_safe(plan: dd.Plan) -> None:
    for f in plan.files:
        dd.safe_relative_path(f.path)
    for a in plan.archives:
        dd.safe_relative_path(a.name)
        dd.safe_relative_path(a.source.path)
        for p in a.parts:
            dd.safe_relative_path(p.path)


# ---------------------------------------------------------------- Brown Dust 2
def test_bd2_plan():
    base = "https://pc.bd2.pmang.cloud/browndust2starter/games/10000001"
    f = FakeFetch({base + "/game.json": fx("bd2_game.json")})
    plan = bd2.resolve(f)
    meta = json.loads(fx("bd2_game.json"))
    assert plan.game_tag == "bd2"
    assert plan.version == meta["last_version"] == "20261023000"
    assert plan.exe == "BrownDust II.exe"
    assert plan.files == ()
    (arc,) = plan.archives
    assert arc.unwrap == ("zip", "zip")
    assert arc.inner_crc32 == meta["last_file"]["tied_crc"] == "736BBB9D"
    assert arc.source.url == base + meta["last_file"]["file"]
    assert arc.source.size is None  # the manifest's file_size is not the real size
    assert f.calls[0][0] == base + "/game.json"
    all_paths_safe(plan)


# --------------------------------------------------------------- Aether Gazer
def test_aethergazer_plan():
    url = "https://us-api.yongshigame.com/game-website-server/pass/sdk/get_update_version?gameId=1"
    f = FakeFetch({url: fx("ag_get_update_version.json")})
    plan = aethergazer.resolve(f)
    info = json.loads(fx("ag_get_update_version.json"))["data"]["gameUpdateInfo"]
    assert plan.game_tag == "aethergazer"
    assert plan.version == info["currentVersion"] == "293"
    assert plan.exe == "AetherGazer.exe"
    (arc,) = plan.archives
    assert arc.unwrap == ("zip",)
    assert arc.source.url == info["downloadUrl"]
    # the build's md5 is the 32 hex digits before ".zip" in the file name
    assert arc.source.md5 == "b03ab844d2f9c0359577d7200d0fb67e"
    all_paths_safe(plan)


def test_aethergazer_rejects_an_error_response():
    url = "https://us-api.yongshigame.com/game-website-server/pass/sdk/get_update_version?gameId=1"
    f = FakeFetch({url: b'{"errorCode":"1","data":null}'})
    with pytest.raises(dd.DirectError):
        aethergazer.resolve(f)


# ------------------------------------------------------------ Mongil: Star Dive
def test_mongil_plan():
    url = "https://apis.netmarble.com/cpplauncher/api/game/monster2/builds?buildCode=A"
    f = FakeFetch({url: fx("mongil_builds.json")})
    plan = mongil.resolve(f)
    b = json.loads(fx("mongil_builds.json"))["data"]
    assert plan.game_tag == "mongil"
    assert plan.version == b["buildVersion"] == "1.04.00"
    assert plan.exe == "STARDIVE.exe"
    (arc,) = plan.archives
    assert arc.source.url == b["buildDownloadUrl"]
    assert arc.source.md5.lower() == b["buildFileHash"].lower()
    assert arc.source.size == b["buildFileSizeByte"]
    hdrs = f.calls[0][1]
    assert hdrs["X-NM-LAUNCHER-CH"] == "ypWjRL2aNi" and hdrs["X-NM-LAUNCHER-LANG"] == "en"
    all_paths_safe(plan)


# ------------------------------------------------------------ Wuthering Waves
def _wuwa_fetch(gz_index: bool, gz_list: bool = False):
    idx_url = (
        "https://prod-alicdn-gamestarter.kurogame.com/launcher/game/G153/"
        "50004_obOHXFrFanqsaIEOmuKroCcbZkQRBC7c/index.json"
    )
    index = json.loads(fx("wuwa_index.json"))
    cfg = index["default"]["config"]
    # highest-P CDN wins (aws, P=7276)
    root = "https://hw-pcdownload-aws.aki-game.net/"
    body = fx("wuwa_index.json")
    lst = fx("wuwa_indexFile.json")
    return (
        FakeFetch(
            {
                idx_url: gzip.compress(body) if gz_index else body,
                root + cfg["indexFile"]: gzip.compress(lst) if gz_list else lst,
            }
        ),
        root,
        cfg,
    )


@pytest.mark.parametrize("gz", [False, True])
def test_wuwa_plan(gz):
    f, root, cfg = _wuwa_fetch(gz, gz)
    plan = wuwa.resolve(f)
    res = json.loads(fx("wuwa_indexFile.json"))["resource"]
    assert plan.game_tag == "wuwa"
    assert plan.version == cfg["version"] == "3.7.1"
    assert plan.exe == "Wuthering Waves.exe"
    assert plan.archives == ()
    assert len(plan.files) == len(res)
    first = plan.files[0]
    assert first.path == res[0]["dest"] == "Wuthering Waves.exe"
    assert first.md5 == res[0]["md5"] and first.size == res[0]["size"]
    assert first.url == root + cfg["baseUrl"] + "Wuthering%20Waves.exe"
    all_paths_safe(plan)


def test_wuwa_rejects_a_traversal_dest():
    f, root, cfg = _wuwa_fetch(False)
    bad = json.loads(fx("wuwa_indexFile.json"))
    bad["resource"][0]["dest"] = "../evil.dll"
    f.routes[root + cfg["indexFile"]] = json.dumps(bad).encode()
    with pytest.raises(dd.DirectError):
        wuwa.resolve(f)


# ------------------------------------------------------------------ GFL2
def test_gfl2_plan():
    cdn = "https://gf2-us-cdn.sunborngame.com/prod/"
    flex = "https://gf2-launcher-us.sunborngame.com/flexible_config"
    ver_url = cdn + "AssetBundles_Windows/28279/Version.txt"
    f = FakeFetch({flex: fx("gfl2_flexible_config.json"), ver_url: fx("gfl2_Version.txt")})
    plan = gfl2.resolve(f)
    assert plan.game_tag == "gfl2"
    assert plan.version == "2.7.4330.14886.28279"
    assert plan.exe == "GF2_Exilium.exe"
    (arc,) = plan.archives
    assert arc.source.url == cdn + "package/PCClient/2.7.4330/GF2_Exilium_Origin.zip"
    assert arc.unwrap == ("zip",)
    lines = [ln.strip() for ln in fx("gfl2_Version.txt").decode().splitlines() if "#" in ln]
    data_dir = "GF2_Exilium_Data/LocalCache/Data/"
    bundles = [x for x in plan.files if x.path.startswith(data_dir + "AssetBundles_Windows/")]
    assert len(bundles) == len(lines)
    name, md5, size, ver = lines[0].split("#")
    first = bundles[0]
    assert first.path == data_dir + "AssetBundles_Windows/" + name.removeprefix("{0}/")
    assert first.url == cdn + f"AssetBundles_Windows/{ver}/" + name.removeprefix("{0}/")
    assert first.md5.lower() == md5.lower() and first.size == int(size)
    # the manifest itself is installed as Version_D.txt, pinned to what was fetched
    (vd,) = [x for x in plan.files if x.path == data_dir + "Version_D.txt"]
    assert vd.url == ver_url and vd.size == len(fx("gfl2_Version.txt")) and vd.md5
    all_paths_safe(plan)


def test_gfl2_haoplay_global_plan_is_only_the_small_client_from_the_intl_cdn():
    cdn = "https://gf2-intl-cdn.17996cdn.com/prod/"
    flex = "https://gf2-launcher-intl.haoplay.com/flexible_config"
    f = FakeFetch({flex: fx("gfl2_haoplay_flexible_config.json")})  # nothing else is fetched
    plan = gfl2.resolve_haoplay(f)
    assert plan.game_tag == "gfl2-haoplay"
    assert plan.version == "2.0.3936.14886.28279+client-2.0.3936"
    assert plan.exe == "GF2_Exilium.exe"
    assert plan.files == ()  # the game downloads its own ~60 GB of resources in-game
    (arc,) = plan.archives
    assert arc.source.url == cdn + "package/PCClient/2.0.3936/GF2_Exilium_Origin.zip"
    all_paths_safe(plan)


# ------------------------------------------------------------ Endfield
def test_endfield_plan():
    url = "https://launcher.gryphline.com/api/game/get_latest?appcode=YDUTE5gscDZ229CW&channel=6&sub_channel=6"
    f = FakeFetch({url: fx("endfield_get_latest.json")})
    plan = endfield.resolve(f)
    d = json.loads(fx("endfield_get_latest.json"))
    packs = d["pkg"]["packs"]
    assert plan.game_tag == "endfield"
    assert plan.version == d["version"] == "1.6.4"
    assert plan.exe == "Endfield.exe"
    (arc,) = plan.archives
    assert len(arc.parts) == len(packs) == 59
    assert [p.url for p in arc.parts] == [p["url"] for p in packs]  # order preserved
    assert [p.md5 for p in arc.parts] == [p["md5"] for p in packs]
    assert arc.parts[0].path.endswith(".001") and arc.parts[-1].path.endswith(".059")
    assert arc.source.size == sum(int(p["package_size"]) for p in packs)
    assert arc.unwrap == ("zip",)
    all_paths_safe(plan)


# ------------------------------------------------------------ HoYo (zip titles)
HOYO_URL = "https://sg-hyp-api.hoyoverse.com/hyp/hyp-connect/api/getGamePackages?launcher_id=VYTpXlbWo8"


def test_hoyo_honkai_impact_is_a_single_7z_without_audio():
    f = FakeFetch({HOYO_URL: fx("hoyo_getGamePackages.json")})
    plan = hoyo.resolve("bh3_global", f)
    assert plan.game_tag == "hi3" and plan.exe == "BH3.exe" and plan.version == "9.1.0"
    (arc,) = plan.archives
    assert arc.unwrap == ("7z",) and not arc.parts
    assert arc.source.url.endswith("BH3_v9.1.0_af4349d71387.7z") and arc.source.md5
    assert arc.source.size == 27772773070
    all_paths_safe(plan)


def test_hoyo_unknown_biz():
    f = FakeFetch({HOYO_URL: fx("hoyo_getGamePackages.json")})
    with pytest.raises(dd.DirectError):
        hoyo.resolve("nope_global", f)


def test_zip_sources_stream_and_the_nested_one_does_not():
    base = "https://pc.bd2.pmang.cloud/browndust2starter/games/10000001"
    assert not any(
        a.stream
        for a in bd2.resolve(FakeFetch({base + "/game.json": fx("bd2_game.json")})).archives
    )

    ag = "https://us-api.yongshigame.com/game-website-server/pass/sdk/get_update_version?gameId=1"
    assert all(
        a.stream
        for a in aethergazer.resolve(FakeFetch({ag: fx("ag_get_update_version.json")})).archives
    )
    mg = "https://apis.netmarble.com/cpplauncher/api/game/monster2/builds?buildCode=A"
    assert all(a.stream for a in mongil.resolve(FakeFetch({mg: fx("mongil_builds.json")})).archives)
    ef = "https://launcher.gryphline.com/api/game/get_latest?appcode=YDUTE5gscDZ229CW&channel=6&sub_channel=6"
    assert all(
        a.stream for a in endfield.resolve(FakeFetch({ef: fx("endfield_get_latest.json")})).archives
    )
    hy = FakeFetch({HOYO_URL: fx("hoyo_getGamePackages.json")})
    # The other HoYo titles now use Sophon chunk manifests (see test_sophon.py).
    assert all(a.stream for a in hoyo.resolve("bh3_global", hy).archives)


# ------------------------------------------------------------------ Punishing: Gray Raven
PGR_TOKEN = "LWdk9D2Ep9mpJmqBZZkcPBU2YNraEWBQ"
PGR_INDEX = f"https://prod-alicdn-gamestarter.kurogame.com/launcher/game/G143/50015_{PGR_TOKEN}/index.json"


def pgr_routes(token=PGR_TOKEN):
    from nadare.core.sources import pgr

    index = json.loads(fx("pgr_index.json"))
    cfg = index["default"]["config"]
    best = max(index["default"]["cdnList"], key=lambda c: int(c["P"]))["url"]
    return pgr.index_url(token), {
        pgr.index_url(token): fx("pgr_index.json"),
        best + cfg["indexFile"]: fx("pgr_indexFile.json"),
    }, best, cfg


def test_pgr_plan():
    from nadare.core.sources import pgr

    url, routes, root, cfg = pgr_routes()
    assert url == PGR_INDEX
    f = FakeFetch(routes)
    plan = pgr.resolve(f)
    res = json.loads(fx("pgr_indexFile.json"))["resource"]
    assert plan.game_tag == "pgr" and plan.exe == "PGR.exe"
    assert plan.version == cfg["version"] == "4.8.0"
    assert plan.archives == () and len(plan.files) == len(res)
    first = plan.files[0]
    assert (first.path, first.md5, first.size) == (res[0]["dest"], res[0]["md5"], res[0]["size"])
    assert first.url == root + cfg["baseUrl"] + first.path  # PGR.exe has nothing to escape
    assert f.calls[0][0] == PGR_INDEX
    all_paths_safe(plan)


def test_pgr_token_can_be_overridden():
    from nadare.core.sources import pgr

    url, routes, _root, _cfg = pgr_routes("OVERRIDETOKEN0123456789abcdefghij")
    assert "50015_OVERRIDETOKEN0123456789abcdefghij" in url
    assert (
        pgr.resolve(FakeFetch(routes), token="OVERRIDETOKEN0123456789abcdefghij").version == "4.8.0"
    )
    for bad in ("", "has space", "../x", "a" * 80, "tok/en"):
        with pytest.raises(dd.DirectError):
            pgr.index_url(bad)


def test_a_refused_index_is_explained_as_a_rotated_token():
    from nadare.core.sources import pgr

    def refuse(url, headers=None):
        raise dd.DirectError("HTTP 403 for " + url)

    with pytest.raises(dd.DirectError, match="(?i)token") as ei:
        pgr.resolve(refuse)
    assert "403" not in str(ei.value) or "token" in str(ei.value).lower()


def test_default_fetch_reports_download_errors_as_direct_errors(monkeypatch):
    from nadare.core import download
    from nadare.core import sources as src

    def boom(url, headers=None, **kw):
        raise download.DownloadError("HTTP 404 for x")

    monkeypatch.setattr(download, "http_get", boom)
    with pytest.raises(dd.DirectError, match="404"):
        src.default_fetch("https://example.invalid/x")


def test_pgr_streams_nothing_and_is_a_plain_file_list():
    from nadare.core.sources import pgr

    _u, routes, _r, _c = pgr_routes()
    assert not any(a.stream for a in pgr.resolve(FakeFetch(routes)).archives)
