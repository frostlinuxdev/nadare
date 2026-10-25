# SPDX-License-Identifier: GPL-3.0-or-later
"""Integrity, cancel/resume, repair and locking regressions for directdl."""

from __future__ import annotations

import fcntl
import hashlib
import io
import json
import os
import zipfile

import pytest
from _httpserver import Server

from nadare.core import directdl as dd
from nadare.core import paths
from nadare.core.events import Cancel, Cancelled


@pytest.fixture
def server():
    made: list[Server] = []

    def make(objects):
        s = Server(objects)
        made.append(s)
        return s

    yield make
    for s in made:
        s.close()


def md5(b: bytes) -> str:
    return hashlib.md5(b).hexdigest()


def make_zip(files: dict[str, bytes]) -> bytes:
    buf = io.BytesIO()
    with zipfile.ZipFile(buf, "w", zipfile.ZIP_DEFLATED) as z:
        for name, data in files.items():
            z.writestr(name, data)
    return buf.getvalue()


def test_a_cancelled_download_keeps_its_part_and_the_next_sync_resumes(server, tmp_path):
    body = b"x" * (3 << 20)
    s = server({"/a.bin": body})
    rf = dd.RemoteFile("a.bin", s.url + "/a.bin", len(body), md5=md5(body))
    plan = dd.Plan("T", "1", files=(rf,))
    cancel = Cancel()

    def stop_after_first_chunk(event):
        if event.kind == "progress":
            cancel.cancel()

    with pytest.raises(Cancelled):
        dd.sync(plan, tmp_path, emit=stop_after_first_chunk, cancel=cancel)

    part = tmp_path / "a.bin.part"
    assert part.is_file()
    assert 0 < part.stat().st_size < len(body)

    assert dd.sync(plan, tmp_path) == {"downloaded": 1, "skipped": 0}
    assert (tmp_path / "a.bin").read_bytes() == body
    assert not part.exists()
    assert any(req[0] == "/a.bin" for req in s.ranges)  # the second run resumed


def test_a_hash_mismatch_download_leaves_no_file_or_part(server, tmp_path):
    s = server({"/a.bin": b"real"})
    rf = dd.RemoteFile("a.bin", s.url + "/a.bin", 4, md5="0" * 32)
    with pytest.raises(dd.DirectError):
        dd.sync(dd.Plan("T", "1", files=(rf,)), tmp_path)
    assert not (tmp_path / "a.bin").exists()
    assert not list(tmp_path.rglob("*.part"))


def test_a_deleted_member_makes_the_archive_missing_again(server, tmp_path):
    z = make_zip({"game.exe": b"MZ", "data/x.bin": b"123"})
    s = server({"/g.zip": z})
    arc = dd.Archive("g", dd.RemoteFile("g.zip", s.url + "/g.zip", len(z), md5=md5(z)))
    plan = dd.Plan("T", "1", archives=(arc,))

    dd.sync(plan, tmp_path)
    record_path = tmp_path / dd.STATE_DIR / "g.done"
    record = json.loads(record_path.read_text(encoding="utf-8"))
    assert record["members"] == ["data/x.bin", "game.exe"]

    (tmp_path / "data/x.bin").unlink()
    assert dd.plan_missing(plan, tmp_path) == ([], [arc])
    assert not dd._archive_done(plan, arc, tmp_path)


def test_an_old_done_record_without_members_is_still_accepted(server, tmp_path):
    z = make_zip({"game.exe": b"MZ", "data/x.bin": b"123"})
    s = server({"/g.zip": z})
    arc = dd.Archive("g", dd.RemoteFile("g.zip", s.url + "/g.zip", len(z), md5=md5(z)))
    plan = dd.Plan("T", "1", archives=(arc,))

    dd.sync(plan, tmp_path)
    record_path = tmp_path / dd.STATE_DIR / "g.done"
    record = json.loads(record_path.read_text(encoding="utf-8"))
    record.pop("members")
    record_path.write_text(json.dumps(record), encoding="utf-8")

    (tmp_path / "data/x.bin").unlink()
    assert dd.plan_missing(plan, tmp_path) == ([], [])


def test_sync_refuses_to_run_while_another_sync_holds_the_lock(tmp_path):
    root = tmp_path / "install"
    key = hashlib.sha256(str(root.resolve()).encode()).hexdigest()[:16]
    lock_path = paths.state_dir() / "locks" / f"directdl-{key}.lock"
    lock_path.parent.mkdir(parents=True, exist_ok=True)
    fd = os.open(lock_path, os.O_RDWR | os.O_CREAT, 0o600)
    try:
        fcntl.flock(fd, fcntl.LOCK_EX | fcntl.LOCK_NB)
        with pytest.raises(dd.DirectError, match="another install or update is already running"):
            dd.sync(dd.Plan("T", "1"), root)
    finally:
        os.close(fd)

    assert dd.sync(dd.Plan("T", "1"), root) == {"downloaded": 0, "skipped": 0}


def test_a_full_disk_while_unpacking_keeps_the_verified_archive(server, tmp_path, monkeypatch):
    archive = make_zip({"game.exe": b"MZ" + b"y" * 4096})
    s = server({"/g.zip": archive})
    source = dd.RemoteFile("g.zip", s.url + "/g.zip", len(archive), md5=md5(archive))
    plan = dd.Plan("T", "1", archives=(dd.Archive("g", source, unwrap=("zip",)),))

    def disk_full(*args, **kwargs):
        raise OSError(28, "No space left on device")

    monkeypatch.setattr(dd.shutil, "copyfileobj", disk_full)
    with pytest.raises(dd.DirectError):
        dd.sync(plan, tmp_path)
    kept = [p for p in tmp_path.rglob("g.zip")]
    assert kept and kept[0].read_bytes() == archive

    monkeypatch.undo()
    requests_before = len(s.ranges)
    assert dd.sync(plan, tmp_path) == {"downloaded": 1, "skipped": 0}
    assert (tmp_path / "game.exe").read_bytes().startswith(b"MZ")
    assert len(s.ranges) == requests_before  # reused the kept archive, no refetch


def test_a_corrupt_archive_is_deleted_after_a_failed_unpack(server, tmp_path):
    bad = b"not a zip file at all" * 100
    s = server({"/g.zip": bad})
    source = dd.RemoteFile("g.zip", s.url + "/g.zip", len(bad), md5=md5(bad))
    plan = dd.Plan("T", "1", archives=(dd.Archive("g", source, unwrap=("zip",)),))
    with pytest.raises(dd.DirectError):
        dd.sync(plan, tmp_path)
    assert not list(tmp_path.rglob("g.zip"))
