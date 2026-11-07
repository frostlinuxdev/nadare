# SPDX-License-Identifier: GPL-3.0-or-later
"""An archive update removes the files the publisher dropped, and only those (#854)."""

from __future__ import annotations

import hashlib
import io
import zipfile

import pytest
from _httpserver import Server as _Server

from nadare.core import directdl as dd


def make_zip(files: dict[str, bytes]) -> bytes:
    buffer = io.BytesIO()
    with zipfile.ZipFile(buffer, "w", zipfile.ZIP_DEFLATED) as archive:
        for name, data in files.items():
            archive.writestr(name, data)
    return buffer.getvalue()


def md5(data: bytes) -> str:
    return hashlib.md5(data).hexdigest()


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


def _plan(srv, version, name, blob, path, *, files=()):
    archive = dd.Archive(
        name, dd.RemoteFile(f"{name}.zip", srv.url + path, len(blob), md5=md5(blob))
    )
    return archive, version


def _two_versions(server, tmp_path, v1, v2):
    z1, z2 = make_zip(v1), make_zip(v2)
    srv = server({"/g1.zip": z1, "/g2.zip": z2})
    first, _ = _plan(srv, "1", "g", z1, "/g1.zip")
    second, _ = _plan(srv, "2", "g", z2, "/g2.zip")
    dd.sync(dd.Plan("T", "1", archives=(first,)), tmp_path)
    return srv, second


def test_a_file_the_new_version_dropped_is_removed(server, tmp_path):
    _srv, second = _two_versions(
        server, tmp_path, {"game.exe": b"v1", "old.dll": b"gone"}, {"game.exe": b"v2"}
    )
    dd.sync(dd.Plan("T", "2", archives=(second,)), tmp_path)
    assert (tmp_path / "game.exe").read_bytes() == b"v2"
    assert not (tmp_path / "old.dll").exists()


def test_files_that_were_never_the_archives_are_kept(server, tmp_path):
    _srv, second = _two_versions(
        server, tmp_path, {"game.exe": b"v1", "old.dll": b"gone"}, {"game.exe": b"v2"}
    )
    (tmp_path / "save.dat").write_bytes(b"mine")
    (tmp_path / "mods").mkdir()
    (tmp_path / "mods" / "extra.dll").write_bytes(b"mine too")
    dd.sync(dd.Plan("T", "2", archives=(second,)), tmp_path)
    assert (tmp_path / "save.dat").read_bytes() == b"mine"
    assert (tmp_path / "mods" / "extra.dll").read_bytes() == b"mine too"


def test_a_dropped_file_the_user_changed_is_kept(server, tmp_path):
    _srv, second = _two_versions(
        server, tmp_path, {"game.exe": b"v1", "old.dll": b"as shipped"}, {"game.exe": b"v2"}
    )
    (tmp_path / "old.dll").write_bytes(b"edited by the user")
    dd.sync(dd.Plan("T", "2", archives=(second,)), tmp_path)
    assert (tmp_path / "old.dll").read_bytes() == b"edited by the user"


def test_a_file_another_archive_still_has_is_kept(server, tmp_path):
    zg1 = make_zip({"game.exe": b"v1", "shared.dll": b"s"})
    zg2 = make_zip({"game.exe": b"v2"})
    zd = make_zip({"shared.dll": b"s", "dlc.pak": b"d"})
    srv = server({"/g1.zip": zg1, "/g2.zip": zg2, "/d.zip": zd})
    game1, _ = _plan(srv, "1", "g", zg1, "/g1.zip")
    game2, _ = _plan(srv, "2", "g", zg2, "/g2.zip")
    dlc, _ = _plan(srv, "1", "d", zd, "/d.zip")
    dd.sync(dd.Plan("T", "1", archives=(game1, dlc)), tmp_path)
    dd.sync(dd.Plan("T", "2", archives=(game2, dlc)), tmp_path)
    assert (tmp_path / "shared.dll").read_bytes() == b"s"
    assert (tmp_path / "dlc.pak").read_bytes() == b"d"


def test_a_loose_file_in_the_new_plan_is_kept(server, tmp_path):
    z1, z2 = make_zip({"game.exe": b"v1", "cfg.ini": b"a"}), make_zip({"game.exe": b"v2"})
    srv = server({"/g1.zip": z1, "/g2.zip": z2, "/cfg.ini": b"a"})
    first, _ = _plan(srv, "1", "g", z1, "/g1.zip")
    second, _ = _plan(srv, "2", "g", z2, "/g2.zip")
    dd.sync(dd.Plan("T", "1", archives=(first,)), tmp_path)
    loose = dd.RemoteFile("cfg.ini", srv.url + "/cfg.ini", 1, md5=md5(b"a"))
    dd.sync(dd.Plan("T", "2", files=(loose,), archives=(second,)), tmp_path)
    assert (tmp_path / "cfg.ini").read_bytes() == b"a"


def test_a_failed_update_removes_nothing(server, tmp_path):
    z1 = make_zip({"game.exe": b"v1", "old.dll": b"gone"})
    bad = b"not a zip"
    srv = server({"/g1.zip": z1, "/g2.zip": bad})
    first, _ = _plan(srv, "1", "g", z1, "/g1.zip")
    broken = dd.Archive("g", dd.RemoteFile("g.zip", srv.url + "/g2.zip", len(bad), md5=md5(bad)))
    dd.sync(dd.Plan("T", "1", archives=(first,)), tmp_path)
    with pytest.raises(dd.DirectError):
        dd.sync(dd.Plan("T", "2", archives=(broken,)), tmp_path)
    assert (tmp_path / "old.dll").read_bytes() == b"gone"


def test_a_symlink_where_a_dropped_file_was_is_left_alone(server, tmp_path):
    _srv, second = _two_versions(
        server, tmp_path, {"game.exe": b"v1", "old.dll": b"gone"}, {"game.exe": b"v2"}
    )
    outside = tmp_path.parent / "outside.txt"
    outside.write_bytes(b"precious")
    (tmp_path / "old.dll").unlink()
    (tmp_path / "old.dll").symlink_to(outside)
    dd.sync(dd.Plan("T", "2", archives=(second,)), tmp_path)
    assert outside.read_bytes() == b"precious"
    assert (tmp_path / "old.dll").is_symlink()


def test_the_second_sync_has_nothing_left_to_do(server, tmp_path):
    _srv, second = _two_versions(
        server, tmp_path, {"game.exe": b"v1", "old.dll": b"gone"}, {"game.exe": b"v2"}
    )
    plan = dd.Plan("T", "2", archives=(second,))
    dd.sync(plan, tmp_path)
    assert dd.sync(plan, tmp_path) == {"downloaded": 0, "skipped": 1}
