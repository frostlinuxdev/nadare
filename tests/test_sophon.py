# SPDX-License-Identifier: GPL-3.0-or-later
"""Tests for nadare.core.sophon (HoYo's chunked downloads) and the HoYo source that uses it."""

from __future__ import annotations

import hashlib
import json
from pathlib import Path

import pytest
import zstandard

from _httpserver import Server as _Server
from nadare.core import directdl as dd
from nadare.core import sophon
from nadare.core.events import Cancel, Cancelled
from nadare.core.sources import hoyo

FIXTURES = Path(__file__).parent / "fixtures" / "catalog"


# --------------------------------------------------------------- protobuf + fixture builders
def varint(n: int) -> bytes:
    out = bytearray()
    while True:
        b = n & 0x7F
        n >>= 7
        if n:
            out.append(b | 0x80)
        else:
            out.append(b)
            return bytes(out)


def field_varint(num: int, value: int) -> bytes:
    return varint(num << 3) + varint(value)


def field_bytes(num: int, value: bytes | str) -> bytes:
    raw = value.encode() if isinstance(value, str) else value
    return varint((num << 3) | 2) + varint(len(raw)) + raw


def md5(b: bytes) -> str:
    return hashlib.md5(b).hexdigest()


class Build:
    """A fake Sophon build: files cut into chunks, a manifest, and a server holding both."""

    def __init__(self, files: dict[str, bytes], chunk_size: int = 100, dirs: tuple[str, ...] = ()):
        self.files = files
        self.objects: dict[str, bytes] = {}
        entries = b""
        for d in dirs:
            entries += field_bytes(1, field_bytes(1, d) + field_varint(3, 64))
        for name, data in files.items():
            chunks = b""
            for off in range(0, max(len(data), 1), chunk_size):
                part = data[off : off + chunk_size]
                if not part:
                    continue
                comp = zstandard.ZstdCompressor().compress(part)
                cname = f"{md5(part)[:16]}_{md5(part)}"
                self.objects["/chunks/" + cname] = comp
                chunks += field_bytes(
                    2,
                    field_bytes(1, cname)
                    + field_bytes(2, md5(part))
                    + field_varint(3, off)
                    + field_varint(4, len(comp))
                    + field_varint(5, len(part))
                    + field_varint(6, 12345)
                    + field_bytes(7, md5(comp)),
                )
            entries += field_bytes(
                1,
                field_bytes(1, name) + chunks + field_varint(4, len(data)) + field_bytes(5, md5(data)),
            )
        self.manifest_raw = entries
        self.objects["/manifests/m1"] = zstandard.ZstdCompressor().compress(entries)
        self.server: _Server | None = None

    def serve(self) -> _Server:
        self.server = _Server(self.objects)
        return self.server

    def package(self, name: str = "game") -> sophon.SophonPackage:
        assert self.server is not None
        return sophon.SophonPackage(
            name=name,
            manifest_url=self.server.url + "/manifests/m1",
            chunk_url=self.server.url + "/chunks",
            manifest_id="m1",
            size=sum(len(v) for v in self.files.values()),
        )

    def chunk_requests(self) -> list[str]:
        assert self.server is not None
        return [r for r in self.server.requests if "/chunks/" in r]


@pytest.fixture
def servers():
    made: list[_Server] = []

    def make(build: Build) -> _Server:
        s = build.serve()
        made.append(s)
        return s

    yield make
    for s in made:
        s.close()


def plan_for(build: Build, version: str = "1") -> dd.Plan:
    return dd.Plan("T", version, sophon=(build.package(),))


# --------------------------------------------------------------- manifest parsing
def test_parse_manifest_reads_files_chunks_and_skips_directories():
    build = Build({"a.bin": b"x" * 250, "sub/b.bin": b"y" * 40}, chunk_size=100, dirs=("sub",))
    entries = sophon.parse_manifest(build.manifest_raw)
    assert [e.path for e in entries] == ["a.bin", "sub/b.bin"]  # the directory entry is dropped
    a = entries[0]
    assert a.size == 250 and a.md5 == md5(b"x" * 250)
    assert [(c.offset, c.size) for c in a.chunks] == [(0, 100), (100, 100), (200, 50)]
    assert a.chunks[0].md5 == md5(b"x" * 100) and a.chunks[0].csize > 0


def test_parse_manifest_rejects_unsafe_paths_and_garbage():
    evil = field_bytes(1, field_bytes(1, "../escape") + field_varint(4, 0) + field_bytes(5, md5(b"")))
    with pytest.raises(sophon.SophonError):
        sophon.parse_manifest(evil)
    with pytest.raises(sophon.SophonError):
        sophon.parse_manifest(b"\xff\xff\xff\xff\xff\xff\xff\xff\xff\xff\xff")


# --------------------------------------------------------------- sync through directdl
def test_fresh_install_writes_every_file_and_is_idempotent(servers, tmp_path):
    files = {"a.bin": bytes(range(256)) * 3, "sub/b.bin": b"bravo" * 70, "empty.bin": b""}
    build = Build(files, chunk_size=100)
    servers(build)
    plan = plan_for(build)
    assert dd.sync(plan, tmp_path)["downloaded"] == 1
    for name, data in files.items():
        assert (tmp_path / name).read_bytes() == data
    assert not list(tmp_path.rglob("*.part"))
    first = len(build.chunk_requests())
    assert first == 8 + 4  # 768/100 -> 8 chunks, 350/100 -> 4 chunks
    dd.sync(plan, tmp_path)
    assert len(build.chunk_requests()) == first  # nothing fetched again


def test_only_the_damaged_chunk_is_fetched_again(servers, tmp_path):
    data = b"".join(bytes([i]) * 100 for i in range(1, 6))  # five distinct 100-byte chunks
    build = Build({"f.bin": data}, chunk_size=100)
    servers(build)
    plan = plan_for(build)
    dd.sync(plan, tmp_path)
    before = len(build.chunk_requests())
    bad = bytearray(data)
    bad[250] ^= 0xFF  # inside the third chunk
    (tmp_path / "f.bin").write_bytes(bytes(bad))
    dd.sync(plan, tmp_path)
    assert (tmp_path / "f.bin").read_bytes() == data
    assert len(build.chunk_requests()) - before == 1


def test_update_reuses_unchanged_chunks_and_resizes_the_file(servers, tmp_path):
    old = b"".join(bytes([i]) * 100 for i in range(1, 5))  # 400 bytes
    new = old[:200] + b"\xaa" * 100 + old[300:] + b"\xbb" * 60  # one chunk changed, 60 added
    (tmp_path / "f.bin").write_bytes(old)
    build = Build({"f.bin": new}, chunk_size=100)
    servers(build)
    dd.sync(plan_for(build, "2"), tmp_path)
    assert (tmp_path / "f.bin").read_bytes() == new
    assert len(build.chunk_requests()) == 2  # the changed chunk and the new tail only


def test_shrinking_file_is_truncated(servers, tmp_path):
    (tmp_path / "f.bin").write_bytes(b"z" * 1000)
    build = Build({"f.bin": b"z" * 100}, chunk_size=100)
    servers(build)
    dd.sync(plan_for(build), tmp_path)
    assert (tmp_path / "f.bin").read_bytes() == b"z" * 100


def test_a_corrupt_chunk_from_the_server_fails_the_install(servers, tmp_path):
    build = Build({"f.bin": b"q" * 150}, chunk_size=100)
    name = next(k for k in build.objects if k.startswith("/chunks/"))
    build.objects[name] = zstandard.ZstdCompressor().compress(b"WRONG" * 20)  # not the declared md5
    servers(build)
    with pytest.raises(dd.DirectError):
        dd.sync(plan_for(build), tmp_path, workers=1)
    assert not (tmp_path / ".nadare" / "sophon-game.done").exists()


def test_cancel_stops_without_marking_done(servers, tmp_path):
    build = Build({"f.bin": b"c" * 1000}, chunk_size=100)
    servers(build)
    cancel = Cancel()
    cancel.cancel()
    with pytest.raises(Cancelled):
        dd.sync(plan_for(build), tmp_path, cancel=cancel)
    assert not (tmp_path / ".nadare" / "sophon-game.done").exists()


def test_plan_missing_uses_the_done_marker(servers, tmp_path):
    build = Build({"f.bin": b"m" * 120}, chunk_size=100)
    servers(build)
    plan = plan_for(build)
    assert dd.plan_missing(plan, tmp_path) == ([], [])  # sophon packages are not archives/files...
    assert dd.sophon_missing(plan, tmp_path) == [plan.sophon[0]]
    dd.sync(plan, tmp_path)
    assert dd.sophon_missing(plan, tmp_path) == []
    other = dd.Plan("T", "2", sophon=(build.package(),))
    object.__setattr__(other.sophon[0], "manifest_id", "m2")
    assert dd.sophon_missing(other, tmp_path) == [other.sophon[0]]  # a new build id is missing again


def test_unsafe_manifest_path_never_writes_outside(servers, tmp_path):
    build = Build({"ok.bin": b"1" * 10}, chunk_size=100)
    evil = field_bytes(1, field_bytes(1, "../x.bin") + field_varint(4, 0) + field_bytes(5, md5(b"")))
    build.objects["/manifests/m1"] = zstandard.ZstdCompressor().compress(evil)
    servers(build)
    with pytest.raises(dd.DirectError):
        dd.sync(plan_for(build), tmp_path / "game")
    assert not (tmp_path / "x.bin").exists()


# --------------------------------------------------------------- the HoYo source
BRANCHES = (
    "https://sg-hyp-api.hoyoverse.com/hyp/hyp-connect/api/getGameBranches"
    "?launcher_id=VYTpXlbWo8&game_ids%5B%5D=4ziysqXOQ8"
)
BUILD = (
    "https://sg-public-api.hoyoverse.com/downloader/sophon_chunk/api/getBuild"
    "?branch=main&package_id=PePf9OoV54&password=E6RCs6eKqXNC"
)


class FakeFetch:
    def __init__(self, routes: dict[str, bytes]):
        self.routes = routes
        self.calls: list[str] = []

    def __call__(self, url, headers=None):
        self.calls.append(url)
        if url not in self.routes:
            raise AssertionError(f"unexpected fetch: {url}")
        return self.routes[url]


def hsr_fetch() -> FakeFetch:
    return FakeFetch(
        {
            BRANCHES: (FIXTURES / "hoyo_getGameBranches_hsr.json").read_bytes(),
            BUILD: (FIXTURES / "hoyo_getBuild_hsr.json").read_bytes(),
        }
    )


def test_hoyo_sophon_titles_use_the_live_build_not_the_stale_package_list():
    f = hsr_fetch()
    plan = hoyo.resolve("hkrpg_global", f)
    assert plan.game_tag == "hsr" and plan.exe == "StarRail.exe" and plan.version == "4.6.0"
    assert not plan.archives and not plan.files
    game, audio = plan.sophon
    assert game.name == "hsr-4.6.0" and audio.name == "hsr-4.6.0-audio-en-us"
    assert game.manifest_url.startswith("https://autopatchos.starrails.com/client/sophon/manifests/")
    assert game.manifest_url.endswith(
        "/manifest_edb916e5a1ca140c_65fc23eec5dce74324cada65dc595341"
    )
    assert game.manifest_id == "manifest_edb916e5a1ca140c_65fc23eec5dce74324cada65dc595341"
    assert game.chunk_url.startswith("https://autopatchos.starrails.com/client/sophon/chunks/")
    assert game.size == 101062718244 and audio.size == 15109809977
    assert all("getGamePackages" not in u for u in f.calls)


def test_hoyo_sophon_language_choice_and_unknown_language():
    ja = hoyo.resolve("hkrpg_global", hsr_fetch(), language="ja-jp")
    assert ja.sophon[1].name.endswith("audio-ja-jp")
    with pytest.raises(dd.DirectError):
        hoyo.resolve("hkrpg_global", hsr_fetch(), language="xx-xx")


def test_hoyo_sophon_rejects_encrypted_builds():
    build = json.loads((FIXTURES / "hoyo_getBuild_hsr.json").read_bytes())
    build["data"]["manifests"][0]["chunk_download"]["encryption"] = 1
    f = FakeFetch(
        {
            BRANCHES: (FIXTURES / "hoyo_getGameBranches_hsr.json").read_bytes(),
            BUILD: json.dumps(build).encode(),
        }
    )
    with pytest.raises(dd.DirectError):
        hoyo.resolve("hkrpg_global", f)


@pytest.mark.parametrize(
    "biz,tag,exe,version,game_id,package_id,password",
    [
        ("hk4e_global", "genshin", "GenshinImpact.exe", "7.1.0", "gopR6Cufr3", "ScSYQBFhu9", "bDL4JUHL625x"),
        ("nap_global", "zzz", "ZenlessZoneZero.exe", "3.3.0", "U5hbdsT9W7", "qsoIbfMm4x", "lh0OmNjbhG9x"),
    ],
)
def test_hoyo_sophon_genshin_and_zzz_install_only_game_and_the_chosen_audio(
    biz, tag, exe, version, game_id, package_id, password
):
    branches = (
        "https://sg-hyp-api.hoyoverse.com/hyp/hyp-connect/api/getGameBranches"
        f"?launcher_id=VYTpXlbWo8&game_ids%5B%5D={game_id}"
    )
    build = (
        "https://sg-public-api.hoyoverse.com/downloader/sophon_chunk/api/getBuild"
        f"?branch=main&package_id={package_id}&password={password}"
    )
    f = FakeFetch(
        {
            branches: (FIXTURES / f"hoyo_getGameBranches_{tag}.json").read_bytes(),
            build: (FIXTURES / f"hoyo_getBuild_{tag}.json").read_bytes(),
        }
    )
    plan = hoyo.resolve(biz, f)
    assert plan.game_tag == tag and plan.exe == exe and plan.version == version
    game, audio = plan.sophon  # ZZZ's numeric on-demand resource packs and "mini-*" audio are skipped
    assert game.name == f"{tag}-{version}" and audio.name == f"{tag}-{version}-audio-en-us"
    assert game.size > 10**10 and audio.size > 10**8


def test_hoyo_honkai_impact_still_uses_the_old_package_list():
    f = FakeFetch(
        {
            "https://sg-hyp-api.hoyoverse.com/hyp/hyp-connect/api/getGamePackages?launcher_id=VYTpXlbWo8": (
                FIXTURES / "hoyo_getGamePackages.json"
            ).read_bytes()
        }
    )
    plan = hoyo.resolve("bh3_global", f)
    assert plan.game_tag == "hi3" and plan.version == "9.1.0" and plan.archives and not plan.sophon


def test_a_flaky_cdn_is_retried(servers, tmp_path, monkeypatch):
    monkeypatch.setattr(sophon.time, "sleep", lambda s: None)
    build = Build({"f.bin": b"r" * 250}, chunk_size=100)
    s = servers(build)
    s.fail_next = 2  # the first two GETs (the manifest, then a chunk) answer 503
    dd.sync(plan_for(build), tmp_path, workers=1)
    assert (tmp_path / "f.bin").read_bytes() == b"r" * 250


# --------------------------------------------------------------- audit follow-ups
def test_verify_remote_reads_a_file_once_for_md5_and_crc32(tmp_path, monkeypatch):
    import zlib

    data = b"v" * 5000
    path = tmp_path / "f.bin"
    path.write_bytes(data)
    crc = f"{zlib.crc32(data) & 0xFFFFFFFF:08X}"
    rf = dd.RemoteFile("f.bin", "https://x/f", len(data), md5=md5(data), crc32=crc)

    def boom(_path):
        raise AssertionError("a second read pass for crc32")

    monkeypatch.setattr(dd, "_crc32_file", boom)
    assert dd._verify_remote(path, rf) is True
    bad = dd.RemoteFile("f.bin", "https://x/f", len(data), md5=md5(data), crc32="00000000")
    assert dd._verify_remote(path, bad) is False
    bad_md5 = dd.RemoteFile("f.bin", "https://x/f", len(data), md5="0" * 32, crc32=crc)
    assert dd._verify_remote(path, bad_md5) is False


def test_a_fresh_install_does_not_hash_the_zero_filled_files(servers, tmp_path, monkeypatch):
    build = Build({"f.bin": b"n" * 450}, chunk_size=100)
    servers(build)
    calls = []
    real = sophon._chunk_matches
    monkeypatch.setattr(sophon, "_chunk_matches", lambda fh, chunk: calls.append(1) or real(fh, chunk))
    dd.sync(plan_for(build), tmp_path)
    assert calls == []  # the file did not exist: every chunk is fetched without reading it back
    dd.sync(plan_for(build), tmp_path)
    assert len(calls) == 5  # a re-sync still verifies every chunk


def test_install_progress_is_throttled_but_ends_at_the_total(servers, tmp_path):
    build = Build({"f.bin": b"p" * 5000}, chunk_size=10)  # 500 chunks
    servers(build)
    events = []
    dd.sync(plan_for(build), tmp_path, emit=events.append, workers=1)
    progress = [e for e in events if e.kind == "progress"]
    assert 2 <= len(progress) < 120
    assert progress[-1].data["done"] == progress[-1].data["total"] == 5000


def test_sophon_plans_get_many_workers():
    pkg = sophon.SophonPackage("g", "https://x/m", "https://x/c", "m1", 1)
    assert dd.plan_workers(dd.Plan("T", "1", sophon=(pkg,))) >= 16
    assert dd.plan_workers(dd.Plan("T", "1")) == dd.default_workers(0)


def test_direct_backend_reports_an_unfinished_sophon_package_as_missing(tmp_path, monkeypatch):
    from _backend_helpers import make_context

    from nadare.backends import direct

    pkg = sophon.SophonPackage("g-1", "https://x/m", "https://x/c", "m1", 1)
    plan = dd.Plan("T", "1", sophon=(pkg,))
    monkeypatch.setattr(direct.GenshinBackend, "plan_factory", staticmethod(lambda fetch=None: plan))
    backend = direct.GenshinBackend(
        make_context(tmp_path, "direct-genshin", {"install_dir": str(tmp_path / "game")}, "g")
    )
    assert [e.path for e in backend._missing_entries(verify=False)] == ["g-1"]
    done = tmp_path / "game" / ".nadare"
    done.mkdir(parents=True)
    (done / "sophon-g-1.done").write_text("m1")
    assert backend._missing_entries(verify=False) == []
