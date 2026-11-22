# SPDX-License-Identifier: GPL-3.0-or-later
"""The art-index refresher: current Steam assets in, portrait cover + wide art out."""

from __future__ import annotations

import importlib.util
import json
from pathlib import Path

import pytest

REPO = Path(__file__).resolve().parents[1]
SCRIPT = REPO / "scripts" / "refresh_art_index.py"
SEED = REPO / "docs" / "art-index.seed.json"

PORTRAIT = "https://cdn2.unrealengine.com/portrait.jpg"
WIDE = "https://cdn2.unrealengine.com/wide.jpg"
STEM = "https://shared.akamai.steamstatic.com/store_item_assets/steam/apps/3513350/"


def _refresher():
    spec = importlib.util.spec_from_file_location("art_index_refresh", SCRIPT)
    module = importlib.util.module_from_spec(spec)
    assert spec.loader is not None
    spec.loader.exec_module(module)
    return module


def _entry(url, orientation, width, height):
    return {"url": url, "orientation": orientation, "width": width, "height": height}


def _assets():
    return {
        "asset_url_format": "steam/apps/3513350/${FILENAME}?t=1794439536",
        "library_capsule_2x": "aaa/library_capsule_2x.jpg",
        "library_capsule": "aaa/library_capsule.jpg",
        "library_hero_2x": "bbb/library_hero_2x.jpg",
        "library_hero": "bbb/library_hero.jpg",
        "main_capsule_2x": "ccc/capsule_616x353_2x.jpg",
    }


def test_steam_art_becomes_the_portrait_cover_and_wide_hero():
    refresh = _refresher()
    base = {
        "schema": 1,
        "updated": "2026-10-01",
        "games": {
            "direct-wuwa": {
                "cover": [_entry(WIDE, "wide", 1920, 620)],
            }
        },
    }
    doc = refresh.build_document(base, {"direct-wuwa": _assets()}, date="2026-11-13")

    games = doc["games"]["direct-wuwa"]
    assert doc["updated"] == "2026-11-13"
    assert games["cover"][0] == {
        "url": STEM + "aaa/library_capsule_2x.jpg?t=1794439536",
        "orientation": "portrait",
        "width": 600,
        "height": 900,
    }
    assert games["wide"][0] == {
        "url": STEM + "bbb/library_hero_2x.jpg?t=1794439536",
        "orientation": "wide",
        "width": 3840,
        "height": 1240,
    }
    assert len(games["cover"]) <= refresh.artindex.MAX_CANDIDATES
    assert len(games["wide"]) <= refresh.artindex.MAX_CANDIDATES


def test_other_games_keep_their_art_with_portrait_covers_first_and_wide_copied():
    refresh = _refresher()
    base = {
        "schema": 1,
        "updated": "2026-10-01",
        "games": {
            "direct-genshin": {
                "cover": [
                    _entry(WIDE, "wide", 2560, 1440),
                    _entry(PORTRAIT, "portrait", 1200, 1600),
                ]
            }
        },
    }
    doc = refresh.build_document(base, {}, date="2026-11-13")
    entry = doc["games"]["direct-genshin"]
    assert [c["orientation"] for c in entry["cover"]] == ["portrait", "wide"]
    # The wide list is the landscape art so the store tile and game page hero keep a 16:9 crop.
    assert [c["url"] for c in entry["wide"]] == [WIDE]


def test_the_seed_is_already_normalized_so_a_refresh_keeps_it_identical():
    refresh = _refresher()
    seed = json.loads(SEED.read_text(encoding="utf-8"))
    assert refresh.build_document(seed, {}, date=seed["updated"]) == seed


def test_steam_kinds_includes_games_whose_index_may_use_steam():
    kinds = _refresher().steam_kinds()
    assert kinds["direct-wuwa"] == 3513350
    assert kinds["direct-zzz"] == 4162040
    assert kinds["stellasora-yostar"] == 4813850
    assert kinds["direct-nte"] == 4508340


def test_check_reports_a_change_without_writing(tmp_path, capsys, monkeypatch):
    refresh = _refresher()
    seed = tmp_path / "art.json"
    seed.write_text(
        json.dumps(
            {"schema": 1, "updated": "2026-10-01", "games": {"direct-wuwa": {"cover": []}}}
        ),
        encoding="utf-8",
    )
    monkeypatch.setattr(
        refresh,
        "fetch_steam_assets",
        lambda kinds: {"direct-wuwa": _assets()},
    )
    before = seed.read_bytes()
    assert refresh.main(["--seed", str(seed), "--check"]) == 1
    assert seed.read_bytes() == before
    assert "direct-wuwa" in capsys.readouterr().out


def test_check_is_zero_when_the_document_is_up_to_date(tmp_path, monkeypatch):
    refresh = _refresher()
    seed = tmp_path / "art.json"
    base = {
        "schema": 1,
        "updated": "2026-11-13",
        "games": {
            "direct-wuwa": {
                "cover": [_entry(STEM + "aaa/library_capsule_2x.jpg", "portrait", 600, 900)]
            }
        },
    }
    # The expected output of a refresh with no new assets.
    expected = refresh.build_document(base, {}, date="2026-11-13")
    seed.write_text(json.dumps(expected), encoding="utf-8")
    monkeypatch.setattr(refresh, "fetch_steam_assets", lambda kinds: {})
    assert refresh.main(["--seed", str(seed), "--check"]) == 0


@pytest.mark.parametrize("problem", [None, {"schema": 2, "games": {}}, []])
def test_a_broken_seed_is_a_clear_error(tmp_path, capsys, problem):
    refresh = _refresher()
    seed = tmp_path / "art.json"
    seed.write_text("not json" if problem is None else json.dumps(problem), encoding="utf-8")
    assert refresh.main(["--seed", str(seed), "--check"]) == 2
    assert "cannot read" in capsys.readouterr().err
