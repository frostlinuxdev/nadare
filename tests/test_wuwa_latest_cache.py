# SPDX-License-Identifier: GPL-3.0-or-later
"""Wuthering Waves keeps one published-version cache per resource quality (#975)."""

from __future__ import annotations

from pathlib import Path

from _backend_helpers import make_context

from nadare.backends.direct import BrownDust2Backend, WutheringWavesBackend


def _wuwa(tmp_path: Path, quality: str | None, name: str):
    profile = {} if quality is None else {"wuwa_quality": quality}
    return WutheringWavesBackend(make_context(tmp_path, WutheringWavesBackend.kind, profile, name))


def test_each_quality_has_its_own_latest_version_cache(tmp_path):
    sd, hd, uhd = (_wuwa(tmp_path, q, f"wuwa-{q}") for q in ("sd", "hd", "uhd"))
    sd._write_latest("3.7.2")
    hd._write_latest("3.7.2+hd-3.7.3")
    assert sd._read_latest()["version"] == "3.7.2"
    assert hd._read_latest()["version"] == "3.7.2+hd-3.7.3"
    assert uhd._read_latest() is None  # nothing was cached for UHD: it must not borrow another
    assert len({sd._latest_path(), hd._latest_path(), uhd._latest_path()}) == 3


def test_profiles_with_the_same_quality_share_the_cache(tmp_path):
    first, second = _wuwa(tmp_path, "hd", "one"), _wuwa(tmp_path, "hd", "two")
    first._write_latest("3.7.3")
    assert second._read_latest()["version"] == "3.7.3"
    assert _wuwa(tmp_path, None, "default")._latest_path() == first._latest_path()  # HD default


def test_an_unknown_quality_reads_the_default_cache(tmp_path):
    assert _wuwa(tmp_path, "ultra", "x")._latest_path() == _wuwa(tmp_path, "hd", "y")._latest_path()


def test_a_custom_download_token_does_not_share_the_cache(tmp_path):
    plain = _wuwa(tmp_path, "hd", "plain")
    profile = {"wuwa_quality": "hd", "kuro_token": "SECRET123"}
    custom = WutheringWavesBackend(make_context(tmp_path, WutheringWavesBackend.kind, profile, "c"))
    assert plain._latest_path() != custom._latest_path()
    assert "SECRET123" not in str(custom._latest_path())  # the token is never part of a name


def test_other_games_keep_their_cache_name(tmp_path):
    bd2 = BrownDust2Backend(make_context(tmp_path, BrownDust2Backend.kind, {}, "bd2"))
    assert bd2._latest_path().name == f"{bd2.game_tag}.json"
