# SPDX-License-Identifier: GPL-3.0-or-later
"""Snowbreak: Containment Zone, installed straight from Seasun's public manifest."""

from __future__ import annotations

import json
from pathlib import Path

import pytest

from nadare.core import directdl as dd
from nadare.core.sources import snowbreak

FIX = Path(__file__).parent / "fixtures" / "catalog"
BASE = "https://snowbreak-dl.amazingseasuncdn.com/c34e2650e07e397c7e55e46b3266572c/PC/updates/"


def _manifest() -> dict:
    return json.loads((FIX / "snowbreak_manifest.json").read_bytes())


class Fetch:
    def __init__(self, body: bytes):
        self.body = body
        self.urls: list[str] = []

    def __call__(self, url, headers=None):
        self.urls.append(url)
        return self.body


def _resolve(manifest: dict) -> dd.Plan:
    return snowbreak.resolve(Fetch(json.dumps(manifest).encode()))


def test_plan_lists_every_file_by_md5():
    manifest = _manifest()
    fetch = Fetch(json.dumps(manifest).encode())
    plan = snowbreak.resolve(fetch)
    assert fetch.urls == [BASE + "manifest.json"]
    assert plan.game_tag == "snowbreak"
    assert plan.version == manifest["version"]
    assert plan.exe == "Game/Binaries/Win64/Game.exe"
    assert plan.archives == ()
    assert len(plan.files) == len(manifest["paks"])
    for rf, pak in zip(plan.files, manifest["paks"], strict=True):
        assert rf.path == pak["name"]
        assert rf.md5 == pak["hash"]
        assert rf.size == pak["sizeInBytes"]
        # content-addressed: the object is named by its MD5 under pathOffset
        assert rf.url == BASE + "assets/" + pak["hash"]
        dd.safe_relative_path(rf.path)


def test_the_exe_must_be_in_the_manifest():
    manifest = _manifest()
    manifest["paks"] = [p for p in manifest["paks"] if not p["name"].endswith("Game.exe")]
    with pytest.raises(dd.DirectError, match="Game.exe"):
        _resolve(manifest)


@pytest.mark.parametrize(
    "field,value",
    [
        ("name", "../outside.dll"),
        ("name", "/etc/passwd"),
        ("name", ""),
        ("hash", "not-an-md5"),
        ("hash", "../" + "0" * 30),
        ("sizeInBytes", -1),
        ("sizeInBytes", "12"),
        ("sizeInBytes", True),
    ],
)
def test_a_bad_entry_is_refused(field, value):
    manifest = _manifest()
    # not paks[0]: that is Game.exe, whose absence would raise for its own reason
    victim = next(p for p in manifest["paks"] if not p["name"].endswith("Game.exe"))
    victim[field] = value
    with pytest.raises(dd.DirectError):
        _resolve(manifest)


def test_a_file_listed_twice_is_refused():
    manifest = _manifest()
    manifest["paks"].append(dict(manifest["paks"][0]))
    with pytest.raises(dd.DirectError, match="twice"):
        _resolve(manifest)


@pytest.mark.parametrize("offset", ["../x", "/abs", "a/../../b", ""])
def test_an_unsafe_path_offset_is_refused(offset):
    manifest = _manifest()
    manifest["pathOffset"] = offset
    with pytest.raises(dd.DirectError):
        _resolve(manifest)


@pytest.mark.parametrize(
    "body",
    [b"not json", b"[]", json.dumps({"version": "1"}).encode(), json.dumps({"paks": []}).encode()],
)
def test_a_broken_manifest_is_refused(body):
    with pytest.raises(dd.DirectError):
        snowbreak.resolve(Fetch(body))


def test_backend_is_a_catalog_direct_kind():
    from nadare.backends.direct import SnowbreakBackend
    from nadare.core import directkinds, profilenames

    assert "direct-snowbreak" in directkinds.DIRECT_KINDS
    assert directkinds.tier_of("direct-snowbreak") == directkinds.TIER_EXPERIMENTAL
    assert SnowbreakBackend.kind == "direct-snowbreak"
    assert SnowbreakBackend.plan_factory is not None
    assert profilenames.SHORT_NAMES["direct-snowbreak"] == "snowbreak"


def test_play_passes_the_seasun_channel(tmp_path):
    """The game picks its account SDK channel from the command line, like the
    community launcher does; without it the in-game sign-in has no channel."""
    from nadare.backends.direct import SnowbreakBackend

    assert "-ChannelID=seasun" in SnowbreakBackend.snowbreak_args
    # without it the PC client stops at "global shader library is missing" (ES3.1-only shaders)
    assert "-FeatureLevelES31" in SnowbreakBackend.snowbreak_args


def test_a_profile_launch_args_replaces_ours():
    from nadare.backends.direct import SnowbreakBackend

    class Fake(SnowbreakBackend):
        def __init__(self, args):
            self._args = args

        def _extra_args(self, key):
            return list(self._args)

    assert Fake([]).launch_extra_args() == list(SnowbreakBackend.snowbreak_args)
    assert Fake(["-dx11"]).launch_extra_args() == []


def test_a_removed_branch_falls_through_to_the_next(monkeypatch):
    old, new = "a" * 32, "b" * 32
    monkeypatch.setattr(snowbreak, "BRANCHES", (old, new))
    body = json.dumps(_manifest()).encode()
    seen = []

    def fetch(url, headers=None):
        seen.append(url)
        if old in url:
            raise dd.DirectError("HTTP 403")
        return body

    plan = snowbreak.resolve(fetch)
    assert [u.split("/")[3] for u in seen] == [old, new]
    assert all(f"/{new}/PC/updates/assets/" in f.url for f in plan.files)


def test_no_branch_answering_is_a_clear_error(monkeypatch):
    monkeypatch.setattr(snowbreak, "BRANCHES", ("a" * 32,))

    def fetch(url, headers=None):
        raise dd.DirectError("HTTP 403")

    with pytest.raises(dd.DirectError, match="no known download branch"):
        snowbreak.resolve(fetch)


@pytest.mark.parametrize("branch", ["../x", "A" * 32, "a" * 31, ""])
def test_a_bad_branch_is_refused(branch):
    with pytest.raises(dd.DirectError):
        snowbreak.base_of(branch)


def test_snowbreak_downloads_with_more_connections_1611():
    """#1611: Seasun's CDN caps each connection near 1.6 MB/s (measured 2026-11-18: 1 connection
    1.66 MB/s, 8 connections 1.6 each, 32 connections 48 MB/s in total). Sized by file count alone,
    Snowbreak's 1,701 files got 16 connections (about 25 MB/s); the plan asks for 32."""
    plan = _resolve(_manifest())
    assert dd.plan_workers(plan) == snowbreak.WORKERS == 32


def test_a_plan_without_a_hint_is_sized_by_file_count():
    files = tuple(dd.RemoteFile(f"f{i}", f"https://x/{i}", 1) for i in range(10))
    assert dd.plan_workers(dd.Plan("t", "1", files=files)) == dd.default_workers(10)


@pytest.mark.parametrize("hint", [0, -3, "32", True, 10_000])
def test_a_bad_worker_hint_is_ignored_or_capped(hint):
    files = tuple(dd.RemoteFile(f"f{i}", f"https://x/{i}", 1) for i in range(10))
    workers = dd.plan_workers(dd.Plan("t", "1", files=files, extra={"workers": hint}))
    if hint == 10_000:
        assert workers == dd.MAX_WORKERS
    else:
        assert workers == dd.default_workers(10)
