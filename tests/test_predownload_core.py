# SPDX-License-Identifier: GPL-3.0-or-later
"""Predownload (HoYo Sophon): fetch the next version's chunks ahead, use them on update day.

A predownload only writes into its own cache folder; the installed game is never touched, so
it stays playable. The update that follows takes verified chunks from the cache before the
network. Everything here is offline (a local HTTP server and canned API replies).
"""

from __future__ import annotations

import copy
import json
from pathlib import Path

import pytest

zstandard = pytest.importorskip("zstandard")

from test_sophon import FIXTURES, Build, FakeFetch  # noqa: E402

from nadare.core import directdl as dd  # noqa: E402
from nadare.core import diskspace, sophon  # noqa: E402
from nadare.core.backends import BackendError  # noqa: E402
from nadare.core.events import Cancel, Cancelled  # noqa: E402
from nadare.core.sources import hoyo  # noqa: E402


@pytest.fixture
def servers():
    made = []

    def make(build: Build):
        made.append(build.serve())
        return made[-1]

    yield make
    for server in made:
        server.close()


def _snapshot(root: Path) -> dict[str, bytes]:
    return {
        p.relative_to(root).as_posix(): p.read_bytes()
        for p in sorted(root.rglob("*"))
        if p.is_file()
    }


def _old_and_new():
    old = b"".join(bytes([i]) * 100 for i in range(1, 6))  # five distinct 100-byte chunks
    new = old[:200] + b"\xaa" * 100 + old[300:] + b"\xbb" * 60  # one chunk changed + a new tail
    return {"f.bin": old, "keep.bin": b"k" * 150}, {
        "f.bin": new,
        "keep.bin": b"k" * 150,
        "sub/added.bin": b"n" * 120,
    }


def _installed(tmp_path: Path, files: dict[str, bytes]) -> Path:
    root = tmp_path / "game"
    for name, data in files.items():
        (root / name).parent.mkdir(parents=True, exist_ok=True)
        (root / name).write_bytes(data)
    return root


# --------------------------------------------------------------- sophon.predownload_package
def test_predownload_fetches_only_new_chunks_and_never_touches_the_game(servers, tmp_path):
    old, new = _old_and_new()
    root = _installed(tmp_path, old)
    before = _snapshot(root)
    build = Build(new, chunk_size=100)
    servers(build)
    cache = tmp_path / "cache"
    pkg = build.package()
    fetched = sophon.predownload_package(pkg, root, cache, workers=1)
    # f.bin: the changed chunk + the 60-byte tail; added.bin: two chunks; keep.bin: nothing.
    assert len(build.chunk_requests()) == 4
    assert fetched == sum(len(build.objects[r.split("?")[0]]) for r in build.chunk_requests())
    assert _snapshot(root) == before  # the installed game is exactly as it was
    assert sophon.predownload_ready(pkg, cache)


def test_predownload_resumes_without_fetching_again(servers, tmp_path):
    old, new = _old_and_new()
    root = _installed(tmp_path, old)
    build = Build(new, chunk_size=100)
    servers(build)
    cache = tmp_path / "cache"
    sophon.predownload_package(build.package(), root, cache, workers=1)
    first = len(build.chunk_requests())
    assert sophon.predownload_package(build.package(), root, cache, workers=1) == 0
    assert len(build.chunk_requests()) == first


def test_update_after_a_predownload_downloads_no_chunks(servers, tmp_path):
    old, new = _old_and_new()
    root = _installed(tmp_path, old)
    build = Build(new, chunk_size=100)
    servers(build)
    cache = tmp_path / "cache"
    sophon.predownload_package(build.package(), root, cache, workers=1)
    before = len(build.chunk_requests())
    sophon.sync_package(build.package(), root, workers=1, chunk_cache=cache)
    assert len(build.chunk_requests()) == before  # every chunk came from the cache
    for name, data in new.items():
        assert (root / name).read_bytes() == data


def test_a_damaged_cached_chunk_is_downloaded_again(servers, tmp_path):
    old, new = _old_and_new()
    root = _installed(tmp_path, old)
    build = Build(new, chunk_size=100)
    servers(build)
    cache = tmp_path / "cache"
    sophon.predownload_package(build.package(), root, cache, workers=1)
    cached = sorted(p for p in cache.iterdir() if not p.name.endswith(".ready"))
    assert cached
    cached[0].write_bytes(b"garbage")
    before = len(build.chunk_requests())
    sophon.sync_package(build.package(), root, workers=1, chunk_cache=cache)
    assert len(build.chunk_requests()) == before + 1
    for name, data in new.items():
        assert (root / name).read_bytes() == data


def test_a_damaged_cached_chunk_is_fetched_again_by_a_resumed_predownload(servers, tmp_path):
    old, new = _old_and_new()
    root = _installed(tmp_path, old)
    build = Build(new, chunk_size=100)
    servers(build)
    cache = tmp_path / "cache"
    sophon.predownload_package(build.package(), root, cache, workers=1)
    victim = sorted(p for p in cache.iterdir() if not p.name.endswith(".ready"))[0]
    victim.write_bytes(b"garbage")
    before = len(build.chunk_requests())
    assert sophon.predownload_package(build.package(), root, cache, workers=1) > 0
    assert len(build.chunk_requests()) == before + 1
    assert victim.read_bytes() != b"garbage"


def test_sync_without_a_cache_is_unchanged(servers, tmp_path):
    old, new = _old_and_new()
    root = _installed(tmp_path, old)
    build = Build(new, chunk_size=100)
    servers(build)
    sophon.sync_package(build.package(), root, workers=1)
    assert len(build.chunk_requests()) == 4
    assert (root / "sub/added.bin").read_bytes() == new["sub/added.bin"]


def test_a_cache_from_another_build_is_ignored_safely(servers, tmp_path):
    """A cache file whose bytes are not the chunk the manifest names is never written in."""
    old, new = _old_and_new()
    root = _installed(tmp_path, old)
    build = Build(new, chunk_size=100)
    servers(build)
    cache = tmp_path / "cache"
    cache.mkdir()
    # Plant valid zstd data under every chunk name, but with the wrong content.
    for obj in build.objects:
        if obj.startswith("/chunks/"):
            (cache / obj.rsplit("/", 1)[1]).write_bytes(
                zstandard.ZstdCompressor().compress(b"\x00" * 100)
            )
    sophon.sync_package(build.package(), root, workers=1, chunk_cache=cache)
    for name, data in new.items():
        assert (root / name).read_bytes() == data


def test_an_unsafe_chunk_name_is_refused_and_nothing_leaves_the_cache(servers, tmp_path):
    build = Build({"f.bin": b"z" * 100}, chunk_size=100)
    good = next(k for k in build.objects if k.startswith("/chunks/")).rsplit("/", 1)[1]
    raw = build.manifest_raw.replace(good.encode(), b"../../../../evil_" + b"x" * (len(good) - 17))
    build.objects["/manifests/m1"] = zstandard.ZstdCompressor().compress(raw)
    servers(build)
    root = _installed(tmp_path, {})
    root.mkdir(parents=True, exist_ok=True)
    cache = tmp_path / "deep" / "er" / "cache"
    server = build.server
    with pytest.raises(sophon.SophonError, match="unsafe chunk name"):
        sophon.predownload_package(build.package(), root, cache, workers=1)
    assert server.requests == ["/manifests/m1"]  # refused before any chunk was asked for
    assert not any(p.name.startswith("evil") for p in tmp_path.rglob("*"))
    assert not sophon.predownload_ready(build.package(), cache)


def test_predownload_checks_free_space_before_downloading(servers, tmp_path, monkeypatch):
    old, new = _old_and_new()
    root = _installed(tmp_path, old)
    build = Build(new, chunk_size=100)
    servers(build)

    def no_space(path, needed, margin=0.05):
        raise BackendError("not enough free space")

    monkeypatch.setattr(diskspace, "check_space", no_space)
    with pytest.raises(BackendError):
        sophon.predownload_package(build.package(), root, tmp_path / "cache", workers=1)
    assert build.chunk_requests() == []


def test_a_cancelled_predownload_is_not_ready(servers, tmp_path):
    old, new = _old_and_new()
    root = _installed(tmp_path, old)
    build = Build(new, chunk_size=100)
    servers(build)
    cancel = Cancel()
    cancel.cancel()
    with pytest.raises(Cancelled):
        sophon.predownload_package(build.package(), root, tmp_path / "cache", cancel=cancel)
    assert not sophon.predownload_ready(build.package(), tmp_path / "cache")


def test_ready_needs_the_same_manifest(servers, tmp_path):
    old, new = _old_and_new()
    root = _installed(tmp_path, old)
    build = Build(new, chunk_size=100)
    servers(build)
    cache = tmp_path / "cache"
    pkg = build.package()
    assert not sophon.predownload_ready(pkg, cache)
    sophon.predownload_package(pkg, root, cache, workers=1)
    other = sophon.SophonPackage(pkg.name, pkg.manifest_url, pkg.chunk_url, "m2", pkg.size)
    assert sophon.predownload_ready(pkg, cache) and not sophon.predownload_ready(other, cache)


def test_predownload_reports_download_progress(servers, tmp_path):
    old, new = _old_and_new()
    root = _installed(tmp_path, old)
    build = Build(new, chunk_size=100)
    servers(build)
    events = []
    total = sophon.predownload_package(
        build.package(), root, tmp_path / "cache", emit=events.append, workers=1
    )
    progress = [e for e in events if e.kind == "progress"]
    assert progress and progress[-1].data["stage"] == "download"
    assert progress[-1].data["done"] == progress[-1].data["total"] == total


# --------------------------------------------------------------- directdl
def test_directdl_predownload_and_sync_with_the_cache(servers, tmp_path):
    old, new = _old_and_new()
    root = _installed(tmp_path, old)
    build = Build(new, chunk_size=100)
    servers(build)
    plan = dd.Plan("T", "2", sophon=(build.package(),))
    cache = tmp_path / "cache"
    result = dd.predownload(plan, root, cache, workers=1)
    assert result["bytes"] > 0
    assert dd.predownload_ready(plan, cache)
    before = len(build.chunk_requests())
    dd.sync(plan, root, workers=1, chunk_cache=cache)
    assert len(build.chunk_requests()) == before
    assert (root / "f.bin").read_bytes() == new["f.bin"]


def test_directdl_predownload_refuses_a_plan_without_sophon(tmp_path):
    with pytest.raises(dd.DirectError):
        dd.predownload(dd.Plan("T", "2"), tmp_path, tmp_path / "cache")
    assert not dd.predownload_ready(dd.Plan("T", "2"), tmp_path / "cache")


def test_directdl_predownload_turns_sophon_errors_into_direct_errors(servers, tmp_path):
    build = Build({"f.bin": b"q" * 100}, chunk_size=100)
    build.objects["/manifests/m1"] = b"not zstd"
    servers(build)
    with pytest.raises(dd.DirectError):
        dd.predownload(dd.Plan("T", "2", sophon=(build.package(),)), tmp_path, tmp_path / "c")


# --------------------------------------------------------------- HoYo source
ZZZ_BRANCHES = (
    "https://sg-hyp-api.hoyoverse.com/hyp/hyp-connect/api/getGameBranches"
    "?launcher_id=VYTpXlbWo8&game_ids%5B%5D=U5hbdsT9W7"
)


def _zzz_branches(pre: dict | None) -> bytes:
    data = json.loads((FIXTURES / "hoyo_getGameBranches_zzz.json").read_bytes())
    data["data"]["game_branches"][0]["pre_download"] = pre
    return json.dumps(data).encode()


def _zzz_pre(**override) -> dict:
    data = json.loads((FIXTURES / "hoyo_getGameBranches_zzz.json").read_bytes())
    pre = copy.deepcopy(data["data"]["game_branches"][0]["main"])
    pre.update(
        tag="3.4.0", branch="predownload", package_id="PrePkg0001", password="PrePass0001"
    )
    pre.update(override)
    return pre


PRE_BUILD = (
    "https://sg-public-api.hoyoverse.com/downloader/sophon_chunk/api/getBuild"
    "?branch=predownload&package_id=PrePkg0001&password=PrePass0001"
)


def test_no_predownload_window_gives_none():
    f = FakeFetch({ZZZ_BRANCHES: _zzz_branches(None)})
    assert hoyo.resolve_predownload("nap_global", f) is None
    assert all("getBuild" not in u for u in f.calls)


def test_an_open_window_gives_the_next_version_from_the_predownload_branch():
    f = FakeFetch(
        {
            ZZZ_BRANCHES: _zzz_branches(_zzz_pre()),
            PRE_BUILD: (FIXTURES / "hoyo_getBuild_zzz.json").read_bytes(),
        }
    )
    plan = hoyo.resolve_predownload("nap_global", f)
    assert plan is not None
    assert plan.version == "3.4.0" and plan.game_tag == "zzz" and plan.exe == "ZenlessZoneZero.exe"
    game, audio = plan.sophon
    assert game.name == "zzz-3.4.0" and audio.name == "zzz-3.4.0-audio-en-us"
    assert PRE_BUILD in f.calls


def test_the_normal_plan_still_uses_main_while_a_window_is_open():
    main_build = (
        "https://sg-public-api.hoyoverse.com/downloader/sophon_chunk/api/getBuild"
        "?branch=main&package_id=qsoIbfMm4x&password=lh0OmNjbhG9x"
    )
    f = FakeFetch(
        {
            ZZZ_BRANCHES: _zzz_branches(_zzz_pre()),
            main_build: (FIXTURES / "hoyo_getBuild_zzz.json").read_bytes(),
        }
    )
    assert hoyo.resolve("nap_global", f).version == "3.3.0"


@pytest.mark.parametrize("branch", ["../main", "pre download", "main&x=1", ""])
def test_a_strange_branch_name_is_refused(branch):
    pre = _zzz_pre(branch=branch)
    if branch == "":
        # An empty branch falls back to the documented "predownload" name.
        f = FakeFetch(
            {
                ZZZ_BRANCHES: _zzz_branches(pre),
                PRE_BUILD: (FIXTURES / "hoyo_getBuild_zzz.json").read_bytes(),
            }
        )
        assert hoyo.resolve_predownload("nap_global", f).version == "3.4.0"
        return
    f = FakeFetch({ZZZ_BRANCHES: _zzz_branches(pre)})
    with pytest.raises(dd.DirectError):
        hoyo.resolve_predownload("nap_global", f)


def test_a_predownload_for_the_same_version_as_main_is_not_offered():
    f = FakeFetch({ZZZ_BRANCHES: _zzz_branches(_zzz_pre(tag="3.3.0"))})
    assert hoyo.resolve_predownload("nap_global", f) is None


def test_honkai_impact_checks_its_branch_and_finds_no_window():
    url = (
        "https://sg-hyp-api.hoyoverse.com/hyp/hyp-connect/api/getGameBranches"
        "?launcher_id=VYTpXlbWo8&game_ids%5B%5D=5TIVvvcwtM"
    )
    f = FakeFetch({url: (FIXTURES / "hoyo_getGameBranches_hi3.json").read_bytes()})
    assert hoyo.resolve_predownload("bh3_global", f) is None
    assert f.calls == [url]


@pytest.mark.parametrize("tag", ["..", "../x", "3.4/0", ".hidden", "", "a" * 40])
def test_a_strange_version_tag_is_refused(tag):
    f = FakeFetch({ZZZ_BRANCHES: _zzz_branches(_zzz_pre(tag=tag))})
    with pytest.raises(dd.DirectError):
        hoyo.resolve_predownload("nap_global", f)


@pytest.mark.parametrize("name", [".hidden_chunk", "has space"])
def test_odd_chunk_names_without_a_slash_are_refused_too(servers, tmp_path, name):
    build = Build({"f.bin": b"z" * 100}, chunk_size=100)
    good = next(k for k in build.objects if k.startswith("/chunks/")).rsplit("/", 1)[1]
    bad = (name + "x" * len(good))[: len(good)]
    raw = build.manifest_raw.replace(good.encode(), bad.encode())
    build.objects["/manifests/m1"] = zstandard.ZstdCompressor().compress(raw)
    servers(build)
    with pytest.raises(sophon.SophonError, match="unsafe chunk name"):
        sophon.predownload_package(build.package(), tmp_path / "game", tmp_path / "c", workers=1)


def test_a_link_in_the_cache_is_never_read(servers, tmp_path):
    """Even a link to the right bytes is skipped: the cache holds plain files only."""
    old, new = _old_and_new()
    root = _installed(tmp_path, old)
    build = Build(new, chunk_size=100)
    servers(build)
    cache = tmp_path / "cache"
    sophon.predownload_package(build.package(), root, cache, workers=1)
    elsewhere = tmp_path / "elsewhere"
    elsewhere.mkdir()
    victim = sorted(p for p in cache.iterdir() if not p.name.endswith(".ready"))[0]
    victim.rename(elsewhere / victim.name)
    victim.symlink_to(elsewhere / victim.name)
    before = len(build.chunk_requests())
    sophon.sync_package(build.package(), root, workers=1, chunk_cache=cache)
    assert len(build.chunk_requests()) == before + 1
    assert (root / "f.bin").read_bytes() == new["f.bin"]


def test_a_cancel_after_the_last_chunk_still_leaves_it_not_ready(servers, tmp_path):
    old, new = _old_and_new()
    root = _installed(tmp_path, old)
    build = Build(new, chunk_size=100)
    servers(build)
    cancel = Cancel()

    def cancel_when_done(event):
        data = event.data or {}
        if event.kind == "progress" and data.get("done") == data.get("total"):
            cancel.cancel()

    with pytest.raises(Cancelled):
        sophon.predownload_package(
            build.package(), root, tmp_path / "cache", emit=cancel_when_done, cancel=cancel,
            workers=1,
        )
    assert not sophon.predownload_ready(build.package(), tmp_path / "cache")
