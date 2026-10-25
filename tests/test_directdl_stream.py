# SPDX-License-Identifier: GPL-3.0-or-later
"""Streaming zip installs: read the archive off the server, write members in place."""

from __future__ import annotations

import io
import os
import stat
import zipfile
from pathlib import Path

import pytest
from _httpserver import Server

from nadare.core import directdl as dd
from nadare.core.backends import BackendError
from nadare.core.events import Cancel, Cancelled, Event


@pytest.fixture
def server():
    made = []

    def make(objects):
        s = Server(objects)
        made.append(s)
        return s

    yield make
    for s in made:
        s.close()


def make_zip(files: dict[str, bytes], stored: set[str] = frozenset(), extra=None) -> bytes:
    buf = io.BytesIO()
    with zipfile.ZipFile(buf, "w", zipfile.ZIP_DEFLATED) as z:
        for name, data in files.items():
            info = zipfile.ZipInfo(name, date_time=(2026, 1, 1, 0, 0, 0))
            info.compress_type = zipfile.ZIP_STORED if name in stored else zipfile.ZIP_DEFLATED
            info.external_attr = (stat.S_IFREG | 0o644) << 16
            z.writestr(info, data)
        for name, attr in (extra or {}).items():
            info = zipfile.ZipInfo(name, date_time=(2026, 1, 1, 0, 0, 0))
            info.external_attr = attr << 16
            z.writestr(info, b"target")
    return buf.getvalue()


def files(n=12):
    out = {f"data/part{i}.bin": os.urandom(30_000) if i % 3 == 0 else bytes([i]) * 30_000 for i in range(n)}
    out["readme.txt"] = b"hello\n" * 50
    out["empty.bin"] = b""
    out["deep/er/still/f.dat"] = os.urandom(7_000)
    return out


def plan(url, version="1", size=None, parts=(), workers_note=None):
    src = dd.RemoteFile("g.zip", url, size)
    return dd.Plan("T", version, archives=(dd.Archive("g", src, parts=parts, unwrap=("zip",), stream=True),))


def tree(root: Path) -> dict[str, bytes]:
    return {
        p.relative_to(root).as_posix(): p.read_bytes()
        for p in sorted(root.rglob("*"))
        if p.is_file() and p.relative_to(root).parts[0] not in (".nadare", "xdg")
    }


def test_stream_install_writes_the_members_and_never_stores_the_archive(server, tmp_path):
    f = files()
    z = make_zip(f, stored={"data/part1.bin"})
    s = server({"/g.zip": z})
    assert dd.sync(plan(s.url + "/g.zip", size=len(z)), tmp_path) == {"downloaded": 1, "skipped": 0}
    assert tree(tmp_path) == f
    assert not (tmp_path / dd.DL_DIR).exists()
    assert s.ranges and all(p == "/g.zip" for p, _, _ in s.ranges)
    assert not [q for q in s.requests if q == "/g.zip" and not s.ranges]  # only ranged GETs
    assert s.bytes_served <= len(z) + 2 * dd_block()  # about the archive, read once


def dd_block() -> int:
    from nadare.core.rangefile import DEFAULT_BLOCK

    return DEFAULT_BLOCK


def test_second_sync_of_the_same_plan_does_nothing(server, tmp_path):
    z = make_zip(files())
    s = server({"/g.zip": z})
    p = plan(s.url + "/g.zip", size=len(z))
    dd.sync(p, tmp_path)
    n = len(s.requests)
    assert dd.sync(p, tmp_path) == {"downloaded": 0, "skipped": 1}
    assert len(s.requests) == n


def test_an_update_fetches_only_the_changed_members(server, tmp_path):
    f1 = files(20)
    z1 = make_zip(f1)
    f2 = dict(f1)
    f2["data/part4.bin"] = os.urandom(30_000)  # changed
    f2["new/added.bin"] = os.urandom(5_000)  # added
    z2 = make_zip(f2)
    s = server({"/v1.zip": z1, "/v2.zip": z2})
    dd.sync(plan(s.url + "/v1.zip", "1", len(z1)), tmp_path)
    before = {k: (tmp_path / k).stat().st_mtime_ns for k in f1}
    served = s.bytes_served
    dd.sync(plan(s.url + "/v2.zip", "2", len(z2)), tmp_path)
    assert tree(tmp_path) == f2
    changed = {k for k in f1 if (tmp_path / k).stat().st_mtime_ns != before[k]}
    assert changed == {"data/part4.bin"}  # the unchanged files were not even rewritten
    v2_ranges = [r for r in s.ranges if r[0] == "/v2.zip"]
    fetched_members = sum(e - a + 1 for _, a, e in v2_ranges)
    assert fetched_members < len(z2)  # far less than the whole archive was read
    assert s.bytes_served - served < len(z2)


def test_resume_refetches_only_missing_or_damaged_files(server, tmp_path):
    f = files(10)
    z = make_zip(f)
    s = server({"/g.zip": z})
    for name in ("data/part0.bin", "data/part1.bin", "readme.txt"):  # already installed
        (tmp_path / name).parent.mkdir(parents=True, exist_ok=True)
        (tmp_path / name).write_bytes(f[name])
    (tmp_path / "data/part2.bin").write_bytes(b"truncated")  # damaged
    dd.sync(plan(s.url + "/g.zip", size=len(z)), tmp_path)
    assert tree(tmp_path) == f


def test_a_corrupt_member_fails_and_leaves_no_partial_file(server, tmp_path):
    f = {"a.bin": os.urandom(40_000), "b.bin": os.urandom(40_000)}
    z = bytearray(make_zip(f, stored={"a.bin", "b.bin"}))
    with zipfile.ZipFile(io.BytesIO(bytes(z))) as zf:
        info = zf.getinfo("b.bin")
    z[info.header_offset + 30 + len(info.filename) + 100] ^= 0xFF  # inside b.bin's data
    s = server({"/g.zip": bytes(z)})
    with pytest.raises(dd.DirectError):
        dd.sync(plan(s.url + "/g.zip", size=len(z)), tmp_path)
    assert not (tmp_path / "b.bin").exists()
    assert not list(tmp_path.rglob("*.part"))


@pytest.mark.parametrize(
    "bad",
    [{"../evil.txt": b"x"}, {"/abs.txt": b"x"}, {"ok/../../evil2": b"x"}],
)
def test_hostile_member_names_write_nothing(server, tmp_path, bad):
    z = make_zip({"good.txt": b"good", **bad})
    s = server({"/g.zip": z})
    dest = tmp_path / "install"
    with pytest.raises(dd.DirectError):
        dd.sync(plan(s.url + "/g.zip", size=len(z)), dest)
    assert not (tmp_path / "evil.txt").exists() and not (tmp_path / "evil2").exists()
    assert not (dest / "good.txt").exists()  # nothing half-installed


def test_symlink_members_are_refused(server, tmp_path):
    z = make_zip({"good.txt": b"good"}, extra={"link": stat.S_IFLNK | 0o777})
    s = server({"/g.zip": z})
    with pytest.raises(dd.DirectError, match="(?i)symlink"):
        dd.sync(plan(s.url + "/g.zip", size=len(z)), tmp_path)
    assert not (tmp_path / "good.txt").exists()


def test_free_space_is_checked_before_any_member_is_fetched(server, tmp_path, monkeypatch):
    from nadare.core import diskspace

    z = make_zip(files())
    s = server({"/g.zip": z})
    seen = []

    def fake(path, needed, margin=0.05):
        seen.append(needed)
        raise BackendError("no room", code=4)

    monkeypatch.setattr(diskspace, "check_space", fake)
    with pytest.raises(BackendError, match="no room"):
        dd.sync(plan(s.url + "/g.zip", size=len(z)), tmp_path)
    assert seen and seen[0] >= sum(len(v) for v in files().values()) * 0.5
    assert s.bytes_served < len(z)  # only the central directory was read


def test_split_archives_stream_too(server, tmp_path):
    f = files()
    z = make_zip(f)
    cut = [0, len(z) // 3, 2 * len(z) // 3, len(z)]
    objs = {f"/p{i}": z[cut[i] : cut[i + 1]] for i in range(3)}
    s = server(objs)
    parts = tuple(
        dd.RemoteFile(f"g.zip.00{i + 1}", s.url + f"/p{i}", cut[i + 1] - cut[i]) for i in range(3)
    )
    p = dd.Plan(
        "T", "1",
        archives=(dd.Archive("g", dd.RemoteFile("g.zip", "", len(z)), parts=parts, unwrap=("zip",), stream=True),),
    )
    dd.sync(p, tmp_path)
    assert tree(tmp_path) == f
    assert not (tmp_path / dd.DL_DIR).exists()


@pytest.mark.parametrize("workers", [1, 4])
def test_workers_give_the_same_result(server, tmp_path, workers):
    f = files(40)
    z = make_zip(f)
    s = server({"/g.zip": z})
    dd.sync(plan(s.url + "/g.zip", size=len(z)), tmp_path, workers=workers)
    assert tree(tmp_path) == f


def test_cancel_stops_a_stream_install(server, tmp_path):
    z = make_zip(files(30))
    s = server({"/g.zip": z})
    cancel = Cancel()
    cancel.cancel()
    with pytest.raises(Cancelled):
        dd.sync(plan(s.url + "/g.zip", size=len(z)), tmp_path, cancel=cancel)
    assert not list(tmp_path.rglob("*.part"))


def test_progress_is_reported(server, tmp_path):
    z = make_zip(files())
    s = server({"/g.zip": z})
    seen: list[Event] = []
    dd.sync(plan(s.url + "/g.zip", size=len(z)), tmp_path, emit=seen.append)
    assert any(e.kind == "progress" for e in seen)


def test_the_stream_flag_is_ignored_for_other_archive_kinds(server, tmp_path):
    blob = (Path(__file__).parent / "fixtures" / "sevenz" / "lzma2.7z").read_bytes()
    s = server({"/g.7z": blob})
    arc = dd.Archive("g", dd.RemoteFile("g.7z", s.url + "/g.7z", len(blob)), unwrap=("7z",), stream=True)
    dd.sync(dd.Plan("T", "1", archives=(arc,)), tmp_path)
    assert (tmp_path / "hello.txt").is_file()


def test_a_server_without_range_support_falls_back_to_a_normal_install(server, tmp_path):
    f = files(6)
    z = make_zip(f)
    s = server({"/g.zip": z})
    s.ignore_range = True
    src = dd.RemoteFile("g.zip", s.url + "/g.zip", len(z))
    import hashlib

    src = dd.RemoteFile("g.zip", s.url + "/g.zip", len(z), md5=hashlib.md5(z).hexdigest())
    arc = dd.Archive("g", src, unwrap=("zip",), stream=True)
    dd.sync(dd.Plan("T", "1", archives=(arc,)), tmp_path)
    assert tree(tmp_path) == f


# ------------------------------------------------------------------ 7z
SEVENZ = Path(__file__).parent / "fixtures" / "sevenz"


def tree7(root: Path) -> dict[str, bytes]:
    return tree(root)


def plan7(url, size, parts=(), md5=None):
    src = dd.RemoteFile("g.7z", url, size, md5=md5)
    return dd.Plan("T", "1", archives=(dd.Archive("g", src, parts=parts, unwrap=("7z",), stream=True),))


def test_a_7z_streams_without_storing_the_archive(server, tmp_path):
    blob = (SEVENZ / "lzma2.7z").read_bytes()
    s = server({"/g.7z": blob})
    assert dd.sync(plan7(s.url + "/g.7z", len(blob)), tmp_path)["downloaded"] == 1
    assert tree7(tmp_path) == tree7(SEVENZ / "expected")
    assert not (tmp_path / dd.DL_DIR).exists()
    assert s.ranges and not [q for q in s.requests if q == "/g.7z" and not s.ranges]


def test_a_7z_resume_skips_whole_folders_that_are_already_installed(server, tmp_path):
    blob = (SEVENZ / "nonsolid.7z").read_bytes()  # one folder per file
    s = server({"/g.7z": blob})
    expected = tree7(SEVENZ / "expected")
    for name, data in expected.items():  # everything is there except two files
        if name in ("sub/data.bin", "hello.txt"):
            continue
        (tmp_path / name).parent.mkdir(parents=True, exist_ok=True)
        (tmp_path / name).write_bytes(data)
    (tmp_path / "big.bin").write_bytes(b"damaged")  # same name, wrong content
    # One worker: this checks the skip logic byte for byte. With several workers each reader
    # fetches this 6 KB archive as one 64 KB block, which says nothing about skipping.
    dd.sync(plan7(s.url + "/g.7z", len(blob)), tmp_path, workers=1)
    assert tree7(tmp_path) == expected
    # the two missing files and the damaged one were decoded; the rest was never read
    assert s.bytes_served < len(blob) * 2 // 3 + 4096 * 2


def test_a_7z_that_is_fully_installed_fetches_only_its_header(server, tmp_path):
    blob = (SEVENZ / "nonsolid.7z").read_bytes()
    s = server({"/g.7z": blob})
    for name, data in tree7(SEVENZ / "expected").items():
        (tmp_path / name).parent.mkdir(parents=True, exist_ok=True)
        (tmp_path / name).write_bytes(data)
    dd.sync(plan7(s.url + "/g.7z", len(blob)), tmp_path)
    assert s.bytes_served < 4096 * 2  # headers only


def test_a_hostile_7z_writes_nothing_when_streamed(server, tmp_path):
    blob = (SEVENZ / "traversal.7z").read_bytes()
    s = server({"/g.7z": blob})
    dest = tmp_path / "install"
    with pytest.raises(dd.DirectError):
        dd.sync(plan7(s.url + "/g.7z", len(blob)), dest)
    assert not (tmp_path / "yy.txt").exists()
    assert not [p for p in dest.rglob("*") if p.is_file() and not p.name.endswith(".done")]


def test_7z_volumes_stream_as_one_archive(server, tmp_path):
    parts_b = [(SEVENZ / "split.7z.001").read_bytes(), (SEVENZ / "split.7z.002").read_bytes()]
    s = server({"/a": parts_b[0], "/b": parts_b[1]})
    parts = (
        dd.RemoteFile("g.7z.001", s.url + "/a", len(parts_b[0])),
        dd.RemoteFile("g.7z.002", s.url + "/b", len(parts_b[1])),
    )
    arc = dd.Archive("g", dd.RemoteFile("g.7z", "", sum(map(len, parts_b))), parts=parts, unwrap=("7z",), stream=True)
    dd.sync(dd.Plan("T", "1", archives=(arc,)), tmp_path)
    assert tree7(tmp_path) == tree7(SEVENZ / "expected")
    assert not (tmp_path / dd.DL_DIR).exists()


def test_a_7z_checks_free_space_before_decoding(server, tmp_path, monkeypatch):
    from nadare.core import diskspace

    blob = (SEVENZ / "lzma2.7z").read_bytes()
    s = server({"/g.7z": blob})
    seen = []

    def fake(path, needed, margin=0.05):
        seen.append(needed)
        raise BackendError("no room", code=4)

    monkeypatch.setattr(diskspace, "check_space", fake)
    with pytest.raises(BackendError, match="no room"):
        dd.sync(plan7(s.url + "/g.7z", len(blob)), tmp_path)
    assert seen and seen[0] == sum(len(v) for v in tree7(SEVENZ / "expected").values())
    assert not [
        p
        for p in tmp_path.rglob("*")
        if p.is_file() and p.relative_to(tmp_path).parts[0] != "xdg"
    ]  # nothing was decoded or written


def test_a_7z_without_range_support_falls_back_to_a_normal_install(server, tmp_path):
    import hashlib

    blob = (SEVENZ / "lzma2.7z").read_bytes()
    s = server({"/g.7z": blob})
    s.ignore_range = True
    dd.sync(plan7(s.url + "/g.7z", len(blob), md5=hashlib.md5(blob).hexdigest()), tmp_path)
    assert tree7(tmp_path) == tree7(SEVENZ / "expected")


def test_a_7z_stream_can_be_cancelled(server, tmp_path):
    blob = (SEVENZ / "lzma2.7z").read_bytes()
    s = server({"/g.7z": blob})
    cancel = Cancel()
    cancel.cancel()
    with pytest.raises(Cancelled):
        dd.sync(plan7(s.url + "/g.7z", len(blob)), tmp_path, cancel=cancel)


@pytest.mark.parametrize("workers", [1, 4])
def test_a_multi_block_7z_installs_the_same_in_parallel(server, tmp_path, workers):
    """Independent solid blocks are decoded by several workers, each over its own connection."""
    from test_sevenz_parallel import CONTENT

    blob = (SEVENZ / "multiblock.7z").read_bytes()
    s = server({"/g.7z": blob})
    assert dd.sync(plan7(s.url + "/g.7z", len(blob)), tmp_path, workers=workers)["downloaded"] == 1
    assert {k: v for k, v in tree7(tmp_path).items() if not k.startswith(".nadare")} == CONTENT
    assert s.ranges and all(p == "/g.7z" for p, _, _ in s.ranges)
    assert not (tmp_path / dd.DL_DIR).exists()
    # a second sync fetches nothing
    before = len(s.ranges)
    assert dd.sync(plan7(s.url + "/g.7z", len(blob)), tmp_path, workers=workers)["downloaded"] == 0
    assert len(s.ranges) == before
