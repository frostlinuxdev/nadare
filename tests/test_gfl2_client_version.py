# SPDX-License-Identifier: GPL-3.0-or-later
"""A client-only update of the Darkwinter build changes its plan version too (#866)."""

from __future__ import annotations

import json
from pathlib import Path

from nadare.core import directdl as dd
from nadare.core.sources import gfl2

FIXTURES = Path(__file__).parent / "fixtures" / "catalog"
CDN = "https://gf2-us-cdn.sunborngame.com/prod/"
FLEX = "https://gf2-launcher-us.sunborngame.com/flexible_config"


def _fetch(client_version: str | None = None):
    flexible = json.loads((FIXTURES / "gfl2_flexible_config.json").read_text())
    if client_version is not None:
        holder = flexible["data"]["flexible_config"]
        plug = json.loads(holder["nexon_plug"])
        plug["game_client_version"] = client_version
        holder["nexon_plug"] = json.dumps(plug)
    routes = {
        FLEX: json.dumps(flexible).encode(),
        CDN + "AssetBundles_Windows/28279/Version.txt": (
            FIXTURES / "gfl2_Version.txt"
        ).read_bytes(),
    }

    def fetch(url, headers=None):
        return routes[url]

    return fetch


def test_a_client_only_change_changes_the_plan_version():
    old = gfl2.resolve(_fetch())
    new = gfl2.resolve(_fetch("2.7.9999"))
    assert old.version != new.version
    assert new.version.endswith("+client-2.7.9999")
    assert old.archives[0].source.url != new.archives[0].source.url


def test_the_version_carries_both_the_asset_and_the_client_version():
    plan = gfl2.resolve(_fetch())
    assert plan.version == "2.7.4330.14886.28279+client-2.7.4330"


def test_haoplay_keeps_its_version_format():
    flexible = (FIXTURES / "gfl2_haoplay_flexible_config.json").read_bytes()

    def fetch(url, headers=None):
        return flexible

    assert gfl2.resolve_haoplay(fetch).version == "2.0.3936.14886.28279+client-2.0.3936"


# ---- an install made before the change is not re-downloaded for it ----------------------
def _done_record(tmp_path: Path, archive: dd.Archive, version: str) -> None:
    state = tmp_path / dd.STATE_DIR
    state.mkdir(parents=True)
    record = {"version": version, "url": archive.source.url, "members": []}
    (state / f"{archive.name}.done").write_text(json.dumps(record))


def test_an_old_record_with_the_same_client_archive_still_counts_as_done(tmp_path):
    plan = gfl2.resolve(_fetch())
    _done_record(tmp_path, plan.archives[0], "2.7.4330.14886.28279")  # the old version string
    assert dd._archive_done(plan, plan.archives[0], tmp_path)


def test_an_old_record_for_another_client_archive_is_not_done(tmp_path):
    old = gfl2.resolve(_fetch())
    _done_record(tmp_path, old.archives[0], "2.7.4330.14886.28279")
    new = gfl2.resolve(_fetch("2.7.9999"))
    assert not dd._archive_done(new, new.archives[0], tmp_path)


def test_a_record_of_a_different_asset_version_is_still_not_done(tmp_path):
    plan = gfl2.resolve(_fetch())
    _done_record(tmp_path, plan.archives[0], "2.7.4330.14886.11111")
    assert not dd._archive_done(plan, plan.archives[0], tmp_path)
