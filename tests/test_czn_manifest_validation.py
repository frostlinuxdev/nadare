# SPDX-License-Identifier: GPL-3.0-or-later
"""The CZN updater never commits an error page or a malformed manifest as an update (#904)."""

from __future__ import annotations

import json

import pytest

from nadare.backends.czn import update as czn_update
from nadare.core.yostar import ManifestError

GOOD = {
    "version_no": 11,
    "root_folder": "ChaosZeroNightmare",
    "execution": "ChaosZeroNightmare.exe",
    "files": ["x|D|bin", "x|F|a.bin|3|c0|0|4|0|0|0"],
}


@pytest.fixture
def game(tmp_path, monkeypatch):
    root = tmp_path / "prefix"
    game_dir = root / "drive_c/ProgramData/Smilegate/Games/ChaosZeroNightmare"
    (game_dir / "combinedata_manifest").mkdir(parents=True)
    upf = game_dir / "combinedata_manifest" / "GameManifest_X.upf"
    upf.write_text(json.dumps({"local_version": 10, "game_id": "X"}))
    cache = game_dir / "combinedata_manifest" / "nadare-manifest-X.json"
    cache.write_text(json.dumps({**GOOD, "version_no": 10}))
    monkeypatch.setattr(czn_update, "PREFIX", root)
    monkeypatch.setattr(czn_update, "GAME_DIR", game_dir)
    monkeypatch.setattr(czn_update, "UPF", upf)
    monkeypatch.setattr(czn_update, "MANIFEST_CACHE", cache)
    monkeypatch.setattr(czn_update, "STAT_CACHE", root / "stat.json")
    return upf, cache


@pytest.mark.parametrize(
    "bad",
    [
        {"error": "maintenance"},
        {},
        [],
        "oops",
        {"files": "not a list"},
        {"files": []},
        {"files": [1, 2]},
        {"files": ["a|F|x"], "version_no": "abc"},
    ],
)
def test_a_document_that_is_not_a_manifest_is_rejected(bad):
    with pytest.raises(ManifestError):
        czn_update.check_manifest(bad)


def test_a_real_manifest_passes():
    czn_update.check_manifest(GOOD)


@pytest.mark.parametrize("bad", [{"error": "maintenance"}, {"files": []}])
def test_an_update_to_an_error_page_changes_nothing(game, monkeypatch, capsys, bad):
    upf, cache = game
    before = (upf.read_text(), cache.read_text())
    monkeypatch.setattr(czn_update, "fetch_manifest", lambda version: bad)
    code = czn_update.main(["update", "--apply", "--version", "11"])
    assert code == 2
    assert "manifest rejected" in capsys.readouterr().out
    assert (upf.read_text(), cache.read_text()) == before


def test_verify_does_not_vouch_for_a_bad_cached_document(game, capsys):
    upf, cache = game
    cache.write_text(json.dumps({"error": "maintenance"}))
    czn_update.main(["verify"])
    assert '"checked": false' in capsys.readouterr().out.replace("False", "false").lower()
