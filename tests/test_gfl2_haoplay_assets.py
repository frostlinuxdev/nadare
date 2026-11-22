# SPDX-License-Identifier: GPL-3.0-or-later
"""The Haoplay build installs the game data too, from Haoplay's own CDN (#752)."""

from __future__ import annotations

import hashlib
import json
from pathlib import Path

from nadare.core import directdl as dd
from nadare.core.sources import gfl2

FIXTURES = Path(__file__).parent / "fixtures" / "catalog"
FLEX_URL = "https://gf2-launcher-intl.haoplay.com/flexible_config"
CDN = "https://gf2-intl-cdn.17996cdn.com/prod/"
DATA_DIR = "GF2_Exilium_Data/LocalCache/Data/"
VERSION_URL = CDN + "AssetBundles_Windows/28279/Version.txt"


def _config(*, client_version: str | None = None, asset_version: str | None = None) -> bytes:
    doc = json.loads((FIXTURES / "gfl2_haoplay_flexible_config.json").read_text())
    holder = doc["data"]["flexible_config"]
    plug = json.loads(holder["nexon_plug"])
    if client_version is not None:
        plug["game_client_version"] = client_version
    if asset_version is not None:
        plug["ab_resource_version"] = asset_version
    holder["nexon_plug"] = json.dumps(plug)
    return json.dumps(doc).encode()


def _fetch(*, client_version: str | None = None, asset_version: str | None = None):
    version = asset_version or "2.0.3936.14886.28279"
    resource = version.rsplit(".", 1)[-1]
    routes = {
        FLEX_URL: _config(client_version=client_version, asset_version=asset_version),
        CDN + f"AssetBundles_Windows/{resource}/Version.txt": (
            FIXTURES / "gfl2_haoplay_Version.txt"
        ).read_bytes(),
    }

    def fetch(url, headers=None):
        return routes[url]

    return fetch


def test_the_haoplay_plan_lists_the_client_and_the_game_data():
    plan = gfl2.resolve_haoplay(_fetch())
    assert plan.game_tag == "gfl2-haoplay"
    assert plan.version == "2.0.3936.14886.28279+client-2.0.3936"
    assert plan.exe == "GF2_Exilium.exe"

    (arc,) = plan.archives
    assert arc.source.url == CDN + "package/PCClient/2.0.3936/GF2_Exilium_Origin.zip"

    lines = [
        line.strip()
        for line in (FIXTURES / "gfl2_haoplay_Version.txt").read_text().splitlines()
        if "#" in line
    ]
    bundles = [f for f in plan.files if f.path.startswith(DATA_DIR + "AssetBundles_Windows/")]
    assert len(bundles) == len(lines)
    by_path = {f.path: f for f in bundles}
    for line in lines:
        name, md5, size, ver = line.split("#")
        rel = name.removeprefix("{0}/")
        remote = by_path[DATA_DIR + "AssetBundles_Windows/" + rel]
        assert remote.url == CDN + f"AssetBundles_Windows/{ver}/{rel}"
        assert remote.md5.lower() == md5.lower() and remote.size == int(size)

    # Every asset comes from Haoplay's host, never from Darkwinter's Sunborn CDN.
    assert all(f.url.startswith(CDN) for f in plan.files)
    assert not any("sunborngame" in f.url for f in plan.files)

    # The manifest itself is installed where the Darkwinter adapter writes it.
    data = (FIXTURES / "gfl2_haoplay_Version.txt").read_bytes()
    (version_d,) = [f for f in plan.files if f.path == DATA_DIR + "Version_D.txt"]
    assert version_d.url == VERSION_URL
    assert version_d.size == len(data) and version_d.md5 == hashlib.md5(data).hexdigest()

    for f in plan.files:
        dd.safe_relative_path(f.path)


def test_a_new_asset_version_alone_changes_the_plan_identity():
    old = gfl2.resolve_haoplay(_fetch())
    new = gfl2.resolve_haoplay(_fetch(asset_version="2.0.3936.14886.29999"))

    assert old.version != new.version
    assert new.version == "2.0.3936.14886.29999+client-2.0.3936"
    # only the game data moved; the client archive did not
    assert old.archives[0].source.url == new.archives[0].source.url


def test_a_new_client_version_alone_changes_the_plan_identity():
    old = gfl2.resolve_haoplay(_fetch())
    new = gfl2.resolve_haoplay(_fetch(client_version="2.0.3937"))

    assert old.version != new.version
    assert new.version == "2.0.3936.14886.28279+client-2.0.3937"
    assert old.files == new.files  # the game data did not change
    assert old.archives[0].source.url != new.archives[0].source.url
