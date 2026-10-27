# SPDX-License-Identifier: GPL-3.0-or-later
"""Tests for nadare.core.directdl, against a real local HTTP server."""

from __future__ import annotations

import hashlib
import io
import threading
import time
import zipfile
import zlib
from pathlib import Path

import pytest
from _httpserver import Server as _Server

from nadare.core import directdl as dd
from nadare.core.events import Cancel, Cancelled, Event


@pytest.fixture
def server():
    made: list[_Server] = []

    def make(objects):
        s = _Server(objects)
        made.append(s)
        return s

    yield make
    for s in made:
        s.close()


def md5(b: bytes) -> str:
    return hashlib.md5(b).hexdigest()


def crc32(b: bytes) -> str:
    return f"{zlib.crc32(b) & 0xFFFFFFFF:08X}"


def test_merge_tree_rejects_symlinked_destination_parent_and_target(tmp_path):
    source = tmp_path / "source"
    dest = tmp_path / "dest"
    outside = tmp_path / "outside"
    source.mkdir()
    dest.mkdir()
    outside.mkdir()
    outside_file = outside / "save"
    outside_file.write_bytes(b"USERDATA")

    (source / "link").mkdir()
    (source / "link" / "save").write_bytes(b"attacker")
    (dest / "link").symlink_to(outside, target_is_directory=True)
    with pytest.raises(dd.DirectError, match="symlink|outside|unsafe"):
        dd._merge_tree(source, dest)
    assert outside_file.read_bytes() == b"USERDATA"

    (dest / "link").unlink()
    (source / "link").rename(source / "link-dir")
    (source / "save").write_bytes(b"attacker")
    (dest / "save").symlink_to(outside_file)
    with pytest.raises(dd.DirectError, match="symlink|outside|unsafe"):
        dd._merge_tree(source, dest)
    assert outside_file.read_bytes() == b"USERDATA"


def test_failed_direct_download_preserves_existing_file_and_cleans_stage(tmp_path, monkeypatch):
    target = tmp_path / "game.bin"
    old = b"OLD-GAME-DATA"
    target.write_bytes(old)
    rf = dd.RemoteFile("game.bin", "https://example.invalid/game.bin", 4)

    def fail(*args, **kwargs):
        raise dd.DownloadError("simulated CDN failure")

    monkeypatch.setattr(dd, "download", fail)
    with pytest.raises(dd.DirectError, match="download failed"):
        dd._download_remote(rf, target, emit=None, cancel=None)

    assert target.read_bytes() == old
    assert not (tmp_path / "game.bin.nadare-new").exists()
    assert not (tmp_path / "game.bin.nadare-new.part").exists()


def test_digest_mismatch_preserves_existing_file_and_cleans_stage(tmp_path, monkeypatch):
    target = tmp_path / "game.bin"
    old = b"OLD-GAME-DATA"
    target.write_bytes(old)
    rf = dd.RemoteFile("game.bin", "https://example.invalid/game.bin", 4, md5=md5(b"GOOD"))

    def write_bad(_url, dest, **kwargs):
        Path(dest).write_bytes(b"EVIL")

    monkeypatch.setattr(dd, "download", write_bad)
    with pytest.raises(dd.DirectError, match="hash mismatch"):
        dd._download_remote(rf, target, emit=None, cancel=None)

    assert target.read_bytes() == old
    assert not (tmp_path / "game.bin.nadare-new").exists()
    assert not (tmp_path / "game.bin.nadare-new.part").exists()


def test_verified_download_replaces_existing_file(tmp_path, monkeypatch):
    target = tmp_path / "game.bin"
    old = b"OLD-GAME-DATA"
    target.write_bytes(old)
    old_inode = target.stat().st_ino
    good = b"GOOD"
    rf = dd.RemoteFile("game.bin", "https://example.invalid/game.bin", len(good), md5=md5(good))

    def write_good(_url, dest, **kwargs):
        Path(dest).write_bytes(good)

    monkeypatch.setattr(dd, "download", write_good)
    dd._download_remote(rf, target, emit=None, cancel=None)

    assert target.read_bytes() == good
    assert target.stat().st_ino != old_inode
    assert not (tmp_path / "game.bin.nadare-new").exists()


def make_zip(files: dict[str, bytes], stored: bool = False) -> bytes:
    buf = io.BytesIO()
    with zipfile.ZipFile(
        buf, "w", zipfile.ZIP_STORED if stored else zipfile.ZIP_DEFLATED
    ) as z:
        for name, data in files.items():
            z.writestr(name, data)
    return buf.getvalue()


def test_files_download_verify_and_skip_on_second_run(server, tmp_path):
    a, b = b"alpha" * 100, b"bravo" * 50
    s = server({"/a.bin": a, "/sub/b.bin": b})
    plan = dd.Plan(
        "T",
        "1",
        files=(
            dd.RemoteFile("a.bin", s.url + "/a.bin", len(a), md5=md5(a)),
            dd.RemoteFile("sub/b.bin", s.url + "/sub/b.bin", len(b), crc32=crc32(b)),
        ),
    )
    assert dd.sync(plan, tmp_path) == {"downloaded": 2, "skipped": 0}
    assert (tmp_path / "a.bin").read_bytes() == a
    assert (tmp_path / "sub/b.bin").read_bytes() == b
    n = len(s.requests)
    assert dd.sync(plan, tmp_path) == {"downloaded": 0, "skipped": 2}
    assert len(s.requests) == n  # nothing fetched again
    assert not list(tmp_path.rglob("*.part"))


def test_a_file_without_a_declared_size_may_exceed_the_default_cap(server, tmp_path, monkeypatch):
    """Aether Gazer's API reports size 0 for an 8.9 GB zip; the HEAD size must be used."""
    from nadare.core import download

    body = b"z" * 5000
    s = server({"/big.bin": body})
    monkeypatch.setattr(download, "MAX_DOWNLOAD_BYTES", 1000)  # stands in for the 8 GiB cap
    plan = dd.Plan("T", "1", files=(dd.RemoteFile("big.bin", s.url + "/big.bin", md5=md5(body)),))
    assert dd.sync(plan, tmp_path) == {"downloaded": 1, "skipped": 0}
    assert (tmp_path / "big.bin").read_bytes() == body


def test_downloads_check_free_space_first(server, tmp_path, monkeypatch):
    from nadare.core import diskspace
    from nadare.core.backends import BackendError

    s = server({"/a": b"1" * 100})
    seen: list[int] = []

    def fake_check(path, needed, margin=0.05):
        seen.append(needed)
        raise BackendError("no room", code=4)

    monkeypatch.setattr(diskspace, "check_space", fake_check)
    plan = dd.Plan("T", "1", files=(dd.RemoteFile("a", s.url + "/a", 100),))
    with pytest.raises(BackendError, match="no room"):  # the friendly error passes through
        dd.sync(plan, tmp_path)
    assert seen == [100]


def test_corrupted_file_is_refetched(server, tmp_path):
    a = b"x" * 1000
    s = server({"/a.bin": a})
    plan = dd.Plan("T", "1", files=(dd.RemoteFile("a.bin", s.url + "/a.bin", len(a), md5=md5(a)),))
    dd.sync(plan, tmp_path)
    (tmp_path / "a.bin").write_bytes(b"y" * 1000)  # same size, wrong content
    files, archives = dd.plan_missing(plan, tmp_path)
    assert [f.path for f in files] == ["a.bin"] and archives == []
    assert dd.sync(plan, tmp_path) == {"downloaded": 1, "skipped": 0}
    assert (tmp_path / "a.bin").read_bytes() == a


def test_plan_missing_checks_in_parallel_and_preserves_plan_order(tmp_path, monkeypatch):
    good = b"good bytes"
    (tmp_path / "good.bin").write_bytes(good)
    (tmp_path / "small.bin").write_bytes(b"x")
    (tmp_path / "wrong-hash.bin").write_bytes(b"bad bytes!")
    plan = dd.Plan(
        "T",
        "1",
        files=(
            dd.RemoteFile(
                "missing.bin", "https://example.invalid/missing", len(good), md5=md5(good)
            ),
            dd.RemoteFile("good.bin", "https://example.invalid/good", len(good), md5=md5(good)),
            dd.RemoteFile("small.bin", "https://example.invalid/small", len(good), md5=md5(good)),
            dd.RemoteFile(
                "wrong-hash.bin", "https://example.invalid/wrong-hash", len(good), md5=md5(good)
            ),
        ),
    )
    thread_names = set()
    original = dd._verify_remote

    def observed(*args, **kwargs):
        thread_names.add(threading.current_thread().name)
        time.sleep(0.01)
        return original(*args, **kwargs)

    monkeypatch.setattr(dd, "_verify_remote", observed)
    missing, archives = dd.plan_missing(plan, tmp_path, check_workers=3)

    assert [rf.path for rf in missing] == ["missing.bin", "small.bin", "wrong-hash.bin"]
    assert archives == []
    assert len(thread_names) > 1
    assert len(thread_names) <= 3
    assert not any(t.name.startswith("nadare-plan-missing") for t in threading.enumerate())


def test_cancelled_sync_skips_plan_missing_workers(tmp_path, monkeypatch):
    cancel = Cancel()
    cancel.cancel()
    monkeypatch.setattr(
        dd, "plan_missing", lambda *a, **k: pytest.fail("preflight ran after cancel")
    )
    with pytest.raises(Cancelled):
        dd.sync(dd.Plan("T", "1"), tmp_path, cancel=cancel)
    assert not any(t.name.startswith("nadare-plan-missing") for t in threading.enumerate())


@pytest.mark.parametrize("field", ["md5", "crc32"])
def test_hash_mismatch_raises_and_leaves_no_file(server, tmp_path, field):
    s = server({"/a.bin": b"real"})
    bad = {"md5": "0" * 32, "crc32": "00000000"}[field]
    rf = dd.RemoteFile("a.bin", s.url + "/a.bin", 4, **{field: bad})
    with pytest.raises(dd.DirectError):
        dd.sync(dd.Plan("T", "1", files=(rf,)), tmp_path)
    assert not (tmp_path / "a.bin").exists()
    assert not list(tmp_path.rglob("*.part"))


@pytest.mark.parametrize("bad", ["../evil", "/abs", "a/../../evil", "", "a\\..\\evil"])
def test_unsafe_paths_rejected_before_any_request(server, tmp_path, bad):
    s = server({"/a.bin": b"x"})
    plan = dd.Plan("T", "1", files=(dd.RemoteFile(bad, s.url + "/a.bin", 1),))
    with pytest.raises(dd.DirectError):
        dd.sync(plan, tmp_path)
    assert s.requests == []
    with pytest.raises(dd.DirectError):
        dd.safe_relative_path(bad)


def test_safe_relative_path_normalises():
    assert dd.safe_relative_path("a/./b//c.txt") == "a/b/c.txt"


def test_headers_are_sent(server, tmp_path):
    s = server({"/a.bin": b"hi"})
    rf = dd.RemoteFile("a.bin", s.url + "/a.bin", 2, headers=(("Referer", "https://x.example/"),))
    dd.sync(dd.Plan("T", "1", files=(rf,)), tmp_path)
    assert s.headers[0]["referer"] == "https://x.example/"


def test_zip_archive_extracts_and_is_skipped_next_time(server, tmp_path):
    z = make_zip({"game.exe": b"MZ", "data/x.bin": b"123"})
    s = server({"/g.zip": z})
    arc = dd.Archive("g", dd.RemoteFile("g.zip", s.url + "/g.zip", len(z), md5=md5(z)))
    plan = dd.Plan("T", "7", archives=(arc,), exe="game.exe")
    assert dd.sync(plan, tmp_path) == {"downloaded": 1, "skipped": 0}
    assert (tmp_path / "game.exe").read_bytes() == b"MZ"
    assert (tmp_path / "data/x.bin").read_bytes() == b"123"
    assert not (tmp_path / dd.DL_DIR / "g.zip").exists()  # archive removed after unwrap
    assert not (tmp_path / dd.DL_DIR).exists()  # no empty cache dir left behind
    n = len(s.requests)
    assert dd.plan_missing(plan, tmp_path) == ([], [])
    assert dd.sync(plan, tmp_path) == {"downloaded": 0, "skipped": 1}
    assert len(s.requests) == n


def test_new_version_refetches_the_archive(server, tmp_path):
    z1, z2 = make_zip({"v.txt": b"1"}), make_zip({"v.txt": b"2"})
    s = server({"/g1.zip": z1, "/g2.zip": z2})
    mk = lambda ver, z, p: dd.Plan(  # noqa: E731
        "T", ver, archives=(dd.Archive("g", dd.RemoteFile("g.zip", s.url + p, len(z), md5=md5(z))),)
    )
    dd.sync(mk("1", z1, "/g1.zip"), tmp_path)
    assert (tmp_path / "v.txt").read_bytes() == b"1"
    assert dd.sync(mk("2", z2, "/g2.zip"), tmp_path) == {"downloaded": 1, "skipped": 0}
    assert (tmp_path / "v.txt").read_bytes() == b"2"


def test_nested_zip_with_inner_crc(server, tmp_path):
    inner = make_zip({"BrownDust II.exe": b"MZ", "d/f": b"z"}, stored=True)
    outer = make_zip({"20261023000.tied": inner})
    s = server({"/p.dat": outer})
    arc = dd.Archive(
        "p",
        dd.RemoteFile("p.dat", s.url + "/p.dat", len(outer), crc32=crc32(outer)),
        unwrap=("zip", "zip"),
        inner_crc32=crc32(inner),
    )
    dd.sync(dd.Plan("T", "1", archives=(arc,)), tmp_path)
    assert (tmp_path / "BrownDust II.exe").read_bytes() == b"MZ"
    assert (tmp_path / "d/f").read_bytes() == b"z"


def test_nested_zip_bad_inner_crc_writes_nothing(server, tmp_path):
    inner = make_zip({"game.exe": b"MZ"}, stored=True)
    outer = make_zip({"x.tied": inner})
    s = server({"/p.dat": outer})
    arc = dd.Archive(
        "p",
        dd.RemoteFile("p.dat", s.url + "/p.dat", len(outer), md5=md5(outer)),
        unwrap=("zip", "zip"),
        inner_crc32="DEADBEEF",
    )
    with pytest.raises(dd.DirectError):
        dd.sync(dd.Plan("T", "1", archives=(arc,)), tmp_path)
    assert not (tmp_path / "game.exe").exists()


def test_split_archive_parts_are_joined(server, tmp_path):
    z = make_zip({"game.exe": b"MZ" * 500, "x": b"1" * 3000})
    cut = len(z) // 3
    chunks = [z[:cut], z[cut : 2 * cut], z[2 * cut :]]
    objs = {f"/g.zip.00{i + 1}": c for i, c in enumerate(chunks)}
    s = server(objs)
    parts = tuple(
        dd.RemoteFile(f"g.zip.00{i + 1}", s.url + f"/g.zip.00{i + 1}", len(c), md5=md5(c))
        for i, c in enumerate(chunks)
    )
    arc = dd.Archive("g", dd.RemoteFile("g.zip", "", len(z), md5=md5(z)), parts=parts)
    dd.sync(dd.Plan("T", "1", archives=(arc,)), tmp_path)
    assert (tmp_path / "game.exe").read_bytes() == b"MZ" * 500
    assert sorted(s.requests) == sorted(objs)


def test_split_archive_is_never_joined_on_disk(server, tmp_path, monkeypatch):
    """Parts are read as one virtual file, so peak disk stays at ~1x the archive."""
    z = make_zip({"game.exe": b"MZ" * 500, "x": b"1" * 3000})
    cut = len(z) // 2
    chunks = [z[:cut], z[cut:]]
    s = server({f"/g.zip.00{i + 1}": c for i, c in enumerate(chunks)})
    parts = tuple(
        dd.RemoteFile(f"g.zip.00{i + 1}", s.url + f"/g.zip.00{i + 1}", len(c), md5=md5(c))
        for i, c in enumerate(chunks)
    )
    arc = dd.Archive("g", dd.RemoteFile("g.zip", "", len(z), md5=md5(z)), parts=parts)
    seen: list[bool] = []
    real = dd._extract_zip_to

    def spy(source, target):
        seen.append((tmp_path / dd.DL_DIR / "g.zip").exists())
        return real(source, target)

    monkeypatch.setattr(dd, "_extract_zip_to", spy)
    dd.sync(dd.Plan("T", "1", archives=(arc,)), tmp_path)
    assert seen == [False]  # no joined copy existed when extraction ran
    assert (tmp_path / "game.exe").read_bytes() == b"MZ" * 500
    assert not list((tmp_path / dd.DL_DIR).glob("g.zip*"))  # parts cleaned up afterwards


def test_split_archive_whole_archive_hash_is_checked_without_joining(server, tmp_path):
    z = make_zip({"game.exe": b"MZ"})
    cut = len(z) // 2
    chunks = [z[:cut], z[cut:]]
    s = server({"/a": chunks[0], "/b": chunks[1]})
    parts = (
        dd.RemoteFile("p.001", s.url + "/a", len(chunks[0]), md5=md5(chunks[0])),
        dd.RemoteFile("p.002", s.url + "/b", len(chunks[1]), md5=md5(chunks[1])),
    )
    arc = dd.Archive("g", dd.RemoteFile("g.zip", "", len(z), md5="0" * 32), parts=parts)
    with pytest.raises(dd.DirectError):
        dd.sync(dd.Plan("T", "1", archives=(arc,)), tmp_path)
    assert not (tmp_path / "game.exe").exists()


def _pack(pieces: dict[str, bytes]):
    """A fake pack: junk header, then each piece back to back. Returns (bytes, members)."""
    blob = b"PACKHEADER" + b"\x00" * 20
    members = []
    for name, data in pieces.items():
        blob += b"entry-record-header"  # per-entry header, skipped by 'offset'
        members.append(dd.Member(name, len(blob), len(data), md5=md5(data)))
        blob += data
    return blob, tuple(members)


def test_slices_cut_files_out_of_a_downloaded_pack(server, tmp_path):
    pieces = {"Client/a.sig": b"A" * 500, "Client/sub/b.utoc": b"B" * 70, "c.bin": b"C" * 3}
    blob, members = _pack(pieces)
    s = server({"/pack": blob})
    arc = dd.Archive(
        "pak-1",
        dd.RemoteFile("pak-1", s.url + "/pack", len(blob), md5=md5(blob)),
        unwrap=("slices",),
        members=members,
    )
    plan = dd.Plan("T", "1", archives=(arc,))
    assert dd.sync(plan, tmp_path) == {"downloaded": 1, "skipped": 0}
    for name, data in pieces.items():
        assert (tmp_path / name).read_bytes() == data
    assert not (tmp_path / dd.DL_DIR).exists()  # the pack itself is deleted afterwards
    n = len(s.requests)
    assert dd.sync(plan, tmp_path) == {"downloaded": 0, "skipped": 1}
    assert len(s.requests) == n


def test_slices_bad_member_md5_writes_nothing(server, tmp_path):
    blob, members = _pack({"x.bin": b"X" * 100, "y.bin": b"Y" * 100})
    bad = (members[0], dd.Member("y.bin", members[1].offset, members[1].size, md5="0" * 32))
    s = server({"/pack": blob})
    arc = dd.Archive(
        "p", dd.RemoteFile("p", s.url + "/pack", len(blob), md5=md5(blob)),
        unwrap=("slices",), members=bad,
    )
    with pytest.raises(dd.DirectError):
        dd.sync(dd.Plan("T", "1", archives=(arc,)), tmp_path)
    assert not (tmp_path / "x.bin").exists() and not (tmp_path / "y.bin").exists()


@pytest.mark.parametrize(
    "member",
    [
        dd.Member("../evil", 0, 1),
        dd.Member("/abs", 0, 1),
        dd.Member("ok", 10_000_000, 5),  # runs past the end of the pack
        dd.Member("ok", 0, -1),
        dd.Member("ok", -1, 5),
    ],
)
def test_slices_reject_unsafe_or_out_of_range_members(server, tmp_path, member):
    blob = b"0123456789" * 10
    s = server({"/pack": blob})
    arc = dd.Archive(
        "p", dd.RemoteFile("p", s.url + "/pack", len(blob), md5=md5(blob)),
        unwrap=("slices",), members=(member,),
    )
    with pytest.raises(dd.DirectError):
        dd.sync(dd.Plan("T", "1", archives=(arc,)), tmp_path)
    assert not list(tmp_path.glob("ok")) and not (tmp_path.parent / "evil").exists()


def test_slices_without_members_is_an_error(server, tmp_path):
    s = server({"/pack": b"abc"})
    arc = dd.Archive("p", dd.RemoteFile("p", s.url + "/pack", 3), unwrap=("slices",))
    with pytest.raises(dd.DirectError):
        dd.sync(dd.Plan("T", "1", archives=(arc,)), tmp_path)


def test_split_pack_parts_can_also_be_sliced(server, tmp_path):
    blob, members = _pack({"f.bin": b"F" * 200})
    cut = len(blob) // 2
    s = server({"/a": blob[:cut], "/b": blob[cut:]})
    parts = (
        dd.RemoteFile("p.001", s.url + "/a", cut, md5=md5(blob[:cut])),
        dd.RemoteFile("p.002", s.url + "/b", len(blob) - cut, md5=md5(blob[cut:])),
    )
    arc = dd.Archive(
        "p", dd.RemoteFile("p", "", len(blob), md5=md5(blob)), parts=parts,
        unwrap=("slices",), members=members,
    )
    dd.sync(dd.Plan("T", "1", archives=(arc,)), tmp_path)
    assert (tmp_path / "f.bin").read_bytes() == b"F" * 200


SEVENZ = Path(__file__).parent / "fixtures" / "sevenz"


def _tree(root: Path) -> dict[str, bytes]:
    return {
        p.relative_to(root).as_posix(): p.read_bytes()
        for p in sorted(root.rglob("*"))
        if p.is_file() and p.relative_to(root).parts[0] not in (".nadare", "xdg")
    }


def test_7z_archive_is_extracted_and_skipped_next_time(server, tmp_path):
    blob = (SEVENZ / "lzma2.7z").read_bytes()
    s = server({"/g.7z": blob})
    arc = dd.Archive(
        "g", dd.RemoteFile("g.7z", s.url + "/g.7z", len(blob), md5=md5(blob)), unwrap=("7z",)
    )
    plan = dd.Plan("T", "1", archives=(arc,))
    assert dd.sync(plan, tmp_path) == {"downloaded": 1, "skipped": 0}
    assert {k: v for k, v in _tree(tmp_path).items() if not k.startswith(".nadare")} == _tree(
        SEVENZ / "expected"
    )
    assert not (tmp_path / dd.DL_DIR).exists()
    n = len(s.requests)
    assert dd.sync(plan, tmp_path) == {"downloaded": 0, "skipped": 1}
    assert len(s.requests) == n


def test_7z_split_volumes_are_read_without_joining(server, tmp_path, monkeypatch):
    chunks = [(SEVENZ / "split.7z.001").read_bytes(), (SEVENZ / "split.7z.002").read_bytes()]
    s = server({"/a": chunks[0], "/b": chunks[1]})
    parts = (
        dd.RemoteFile("g.7z.001", s.url + "/a", len(chunks[0]), md5=md5(chunks[0])),
        dd.RemoteFile("g.7z.002", s.url + "/b", len(chunks[1]), md5=md5(chunks[1])),
    )
    arc = dd.Archive(
        "g", dd.RemoteFile("g.7z", "", sum(map(len, chunks))), parts=parts, unwrap=("7z",)
    )
    seen: list[bool] = []
    from nadare.core import sevenz

    real = sevenz.extract

    def spy(fh, dest, **kw):
        seen.append((tmp_path / dd.DL_DIR / "g.7z").exists())
        return real(fh, dest, **kw)

    monkeypatch.setattr(sevenz, "extract", spy)
    dd.sync(dd.Plan("T", "1", archives=(arc,)), tmp_path)
    assert seen == [False]
    assert (tmp_path / "hello.txt").is_file() and (tmp_path / "sub/deep/text.txt").is_file()


def test_a_hostile_7z_writes_nothing(server, tmp_path):
    blob = (SEVENZ / "traversal.7z").read_bytes()
    s = server({"/g.7z": blob})
    arc = dd.Archive(
        "g", dd.RemoteFile("g.7z", s.url + "/g.7z", len(blob), md5=md5(blob)), unwrap=("7z",)
    )
    dest = tmp_path / "install"
    with pytest.raises(dd.DirectError):
        dd.sync(dd.Plan("T", "1", archives=(arc,)), dest)
    assert not (tmp_path / "yy.txt").exists()
    assert not [p for p in dest.rglob("*") if p.is_file() and not p.name.endswith(".done")]


def test_zip_with_traversal_member_is_rejected(server, tmp_path):
    buf = io.BytesIO()
    with zipfile.ZipFile(buf, "w") as z:
        z.writestr("ok.txt", b"ok")
        z.writestr("../evil.txt", b"bad")
    z = buf.getvalue()
    s = server({"/g.zip": z})
    arc = dd.Archive("g", dd.RemoteFile("g.zip", s.url + "/g.zip", len(z), md5=md5(z)))
    dest = tmp_path / "install"
    with pytest.raises(dd.DirectError):
        dd.sync(dd.Plan("T", "1", archives=(arc,)), dest)
    assert not (tmp_path / "evil.txt").exists()
    assert not (dest / "ok.txt").exists()  # nothing half-extracted


def test_http_404_is_a_direct_error(server, tmp_path):
    s = server({})
    rf = dd.RemoteFile("a.bin", s.url + "/missing", 1)
    with pytest.raises(dd.DirectError):
        dd.sync(dd.Plan("T", "1", files=(rf,)), tmp_path)


def test_cancel_stops_the_sync(server, tmp_path):
    s = server({"/a": b"1", "/b": b"2"})
    cancel = Cancel()
    cancel.cancel()
    plan = dd.Plan(
        "T",
        "1",
        files=(dd.RemoteFile("a", s.url + "/a", 1), dd.RemoteFile("b", s.url + "/b", 1)),
    )
    with pytest.raises(Cancelled):
        dd.sync(plan, tmp_path, cancel=cancel)


def test_progress_events_are_emitted(server, tmp_path):
    s = server({"/a": b"1" * 10})
    seen: list[Event] = []
    dd.sync(
        dd.Plan("T", "1", files=(dd.RemoteFile("a", s.url + "/a", 10),)), tmp_path, emit=seen.append
    )
    assert any(e.kind in ("progress", "step", "log") for e in seen)


@pytest.mark.parametrize("asked,used", [(0, 1), (6, 6), (48, 48), (500, dd.MAX_WORKERS)])
def test_sync_uses_the_requested_number_of_workers_up_to_the_cap(
    server, tmp_path, monkeypatch, asked, used
):
    """A game with tens of thousands of small files needs far more than 16 connections."""
    seen = []
    real = dd.ThreadPoolExecutor

    def recording(max_workers=None, **kw):
        seen.append(max_workers)
        return real(max_workers=max_workers, **kw)

    monkeypatch.setattr(dd, "ThreadPoolExecutor", recording)
    data = b"x" * 64
    s = server({"/f.bin": data})
    rf = dd.RemoteFile("f.bin", s.url + "/f.bin", len(data), md5=md5(data))
    plan = dd.Plan("T", "1", files=(rf,))
    assert dd.sync(plan, tmp_path, workers=asked) == {"downloaded": 1, "skipped": 0}
    assert seen == [used]
    assert dd.MAX_WORKERS >= 32


@pytest.mark.parametrize(
    "files,expected",
    [(0, 6), (80, 6), (200, 6), (201, 16), (2000, 16), (2001, 32), (47190, 32)],
)
def test_default_workers_scale_with_the_number_of_files(files, expected):
    assert dd.default_workers(files) == expected
    assert dd.default_workers(files) <= dd.MAX_WORKERS


def test_a_loose_file_under_a_symlinked_subfolder_is_refused(server, tmp_path):
    body = b"NEW-GAME-DATA"
    s = server({"/x.bin": body})
    outside = tmp_path / "user_docs"
    outside.mkdir()
    (outside / "x.bin").write_bytes(b"USER-OWNED FILE")
    dest = tmp_path / "install"
    dest.mkdir()
    (dest / "sub").symlink_to(outside)
    rf = dd.RemoteFile("sub/x.bin", s.url + "/x.bin", len(body), md5=md5(body))
    with pytest.raises(dd.DirectError, match="symlink"):
        dd.sync(dd.Plan("T", "1", files=(rf,)), dest)
    assert (outside / "x.bin").read_bytes() == b"USER-OWNED FILE"
    assert sorted(p.name for p in outside.iterdir()) == ["x.bin"]

# ---- b14 (#562): Cancel must also stop the scan of files already on disk -------------
class _CancelAfter(Cancel):
    """Reports a cancel once ``checks`` cancellation points have been passed."""

    def __init__(self, checks: int) -> None:
        super().__init__()
        self.left = checks
        self.seen = 0

    def raise_if_cancelled(self) -> None:
        self.seen += 1
        self.left -= 1
        if self.left < 0:
            self.cancel()
        super().raise_if_cancelled()


def _big_plan(tmp_path, count=3, size=10 << 20, **digests):
    files = []
    for index in range(count):
        data = bytes([index + 1]) * size
        (tmp_path / f"f{index}.bin").write_bytes(data)
        files.append(
            dd.RemoteFile(
                f"f{index}.bin",
                f"https://example.invalid/f{index}",
                size,
                md5=md5(data),
                **({"crc32": crc32(data)} if digests.get("crc") else {}),
            )
        )
    return dd.Plan("T", "1", files=tuple(files))


def test_plan_missing_stops_at_once_when_already_cancelled(tmp_path):
    plan = _big_plan(tmp_path)
    cancel = Cancel()
    cancel.cancel()
    with pytest.raises(Cancelled):
        dd.plan_missing(plan, tmp_path, cancel=cancel)


@pytest.mark.parametrize("crc", [False, True])
def test_hashing_a_big_file_stops_between_blocks(tmp_path, crc):
    plan = _big_plan(tmp_path, count=1, size=40 << 20, crc=crc)
    cancel = _CancelAfter(3)
    with pytest.raises(Cancelled):
        dd.plan_missing(plan, tmp_path, cancel=cancel)
    assert cancel.seen < 40  # gave up after a few of the 40 one-MiB blocks, not at the end


def test_sync_cancelled_during_the_scan_downloads_nothing_and_says_cancelled(tmp_path):
    plan = _big_plan(tmp_path, count=4, size=8 << 20)
    cancel = _CancelAfter(5)
    with pytest.raises(Cancelled):
        dd.sync(plan, tmp_path, cancel=cancel, check_workers=2)


def test_an_uncancelled_scan_still_verifies_good_and_bad_files(tmp_path):
    plan = _big_plan(tmp_path, count=2, size=2 << 20, crc=True)
    (tmp_path / "f1.bin").write_bytes(b"\x07" * (2 << 20))  # same size, wrong content
    missing, _ = dd.plan_missing(plan, tmp_path, cancel=Cancel())
    assert [rf.path for rf in missing] == ["f1.bin"]
