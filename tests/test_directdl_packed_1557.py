# SPDX-License-Identifier: GPL-3.0-or-later
"""#1557: direct downloads can fetch a file the server sends compressed (gzip).

Chaos Zero Nightmare's patch CDN serves every client file as ``<chunk>.gz``; the manifest lists
the md5 and size of the compressed bytes and of the file itself. ``RemoteFile.packed`` carries
the first pair; the ``RemoteFile``'s own size/md5 stay the unpacked file's, so every check of
the installed tree (``plan_missing``, verify, the present-file shortcut) is the usual one.
"""

from __future__ import annotations

import gzip
import hashlib

import pytest
from _httpserver import Server

from nadare.core import directdl as dd
from nadare.core.events import Cancel, Cancelled

DATA = hashlib.shake_256(b"czn client binary").digest(300_000)
PACKED_MTIME = 1_700_000_000
PACKED = gzip.compress(DATA, mtime=PACKED_MTIME)


def md5(blob: bytes) -> str:
    return hashlib.md5(blob).hexdigest()


@pytest.fixture
def cdn():
    made: list[Server] = []

    def make(objects):
        server = Server(objects)
        made.append(server)
        return server

    yield make
    for server in made:
        server.close()


def _file(url: str, *, data: bytes = DATA, packed: bytes = PACKED, **over) -> dd.RemoteFile:
    pk = dd.Packed(
        "gzip",
        size=over.pop("packed_size", len(packed)),
        md5=over.pop("packed_md5", md5(packed)),
    )
    return dd.RemoteFile(
        over.pop("path", "bin/game.dll"),
        url,
        over.pop("size", len(data)),
        md5=over.pop("md5", md5(data)),
        packed=pk,
        **over,
    )


def _plan(*files: dd.RemoteFile) -> dd.Plan:
    return dd.Plan("CZN", "57", files=files)


def test_a_packed_file_is_downloaded_unpacked_and_checked(cdn, tmp_path):
    srv = cdn({"/v57/1.gz": PACKED})
    summary = dd.sync(_plan(_file(srv.url + "/v57/1.gz")), tmp_path)
    assert summary == {"downloaded": 1, "skipped": 0}
    assert (tmp_path / "bin/game.dll").read_bytes() == DATA
    # Nothing of the staging is left: the compressed copy, its .part, the unpacked .part.
    assert sorted(p.name for p in (tmp_path / "bin").iterdir()) == ["game.dll"]


def test_an_installed_packed_file_that_matches_is_not_fetched(cdn, tmp_path):
    (tmp_path / "bin").mkdir()
    (tmp_path / "bin/game.dll").write_bytes(DATA)
    srv = cdn({})  # any request would 404
    summary = dd.sync(_plan(_file(srv.url + "/v57/1.gz")), tmp_path)
    assert summary["downloaded"] == 0
    assert srv.requests == []


def test_plan_missing_checks_the_unpacked_file_not_the_packed_bytes(tmp_path):
    (tmp_path / "bin").mkdir()
    (tmp_path / "bin/game.dll").write_bytes(DATA)
    plan = _plan(_file("http://unused/1.gz"))
    assert dd.plan_missing(plan, tmp_path) == ([], [])
    (tmp_path / "bin/game.dll").write_bytes(DATA[:-1] + b"!")
    assert [rf.path for rf in dd.plan_missing(plan, tmp_path)[0]] == ["bin/game.dll"]


def test_wrong_packed_bytes_are_refused_and_the_old_file_kept(cdn, tmp_path):
    (tmp_path / "bin").mkdir()
    (tmp_path / "bin/game.dll").write_bytes(b"installed build")
    # The same file inside, other packed bytes (another gzip header): still not what the
    # manifest lists, so it is refused before it is unpacked.
    bad = gzip.compress(DATA, mtime=PACKED_MTIME + 1)
    assert gzip.decompress(bad) == DATA and bad != PACKED
    srv = cdn({"/v57/1.gz": bad})
    rf = _file(srv.url + "/v57/1.gz", packed_size=len(bad))  # size right, md5 wrong
    with pytest.raises(dd.DirectError, match=r"hash mismatch .*\(as downloaded\)"):
        dd.sync(_plan(rf), tmp_path)
    assert (tmp_path / "bin/game.dll").read_bytes() == b"installed build"
    assert sorted(p.name for p in (tmp_path / "bin").iterdir()) == ["game.dll"]


def test_a_wrong_unpacked_md5_is_refused_and_the_old_file_kept(cdn, tmp_path):
    (tmp_path / "bin").mkdir()
    (tmp_path / "bin/game.dll").write_bytes(b"installed build")
    srv = cdn({"/v57/1.gz": PACKED})
    rf = _file(srv.url + "/v57/1.gz", md5="0" * 32)
    with pytest.raises(dd.DirectError, match="hash mismatch"):
        dd.sync(_plan(rf), tmp_path)
    assert (tmp_path / "bin/game.dll").read_bytes() == b"installed build"
    assert sorted(p.name for p in (tmp_path / "bin").iterdir()) == ["game.dll"]


def test_unpacking_stops_at_the_declared_size(cdn, tmp_path):
    """A small packed file that expands without end (a bomb) is cut off at the file's size."""
    bomb = gzip.compress(b"\0" * (8 << 20))
    srv = cdn({"/v57/1.gz": bomb})
    rf = _file(srv.url + "/v57/1.gz", packed=bomb, size=4096, md5=md5(b"\0" * 4096))
    with pytest.raises(dd.DirectError, match="larger than"):
        dd.sync(_plan(rf), tmp_path)
    assert not (tmp_path / "bin/game.dll").exists()
    assert list((tmp_path / "bin").iterdir()) == []


def test_corrupt_gzip_is_a_clean_error(cdn, tmp_path):
    junk = b"not gzip at all" * 10
    srv = cdn({"/v57/1.gz": junk})
    rf = _file(srv.url + "/v57/1.gz", packed=junk)
    with pytest.raises(dd.DirectError, match="cannot unpack"):
        dd.sync(_plan(rf), tmp_path)
    assert list((tmp_path / "bin").iterdir()) == []


def test_a_partial_packed_download_is_resumed(cdn, tmp_path):
    (tmp_path / "bin").mkdir()
    half = len(PACKED) // 2
    (tmp_path / f"bin/game.dll{dd.PACKED_SUFFIX}.part").write_bytes(PACKED[:half])
    srv = cdn({"/v57/1.gz": PACKED})
    dd.sync(_plan(_file(srv.url + "/v57/1.gz")), tmp_path)
    assert (tmp_path / "bin/game.dll").read_bytes() == DATA
    assert srv.ranges == [("/v57/1.gz", half, len(PACKED) - 1)]
    assert srv.bytes_served == len(PACKED) - half


def test_a_complete_packed_copy_left_by_a_killed_run_is_used(cdn, tmp_path):
    (tmp_path / "bin").mkdir()
    (tmp_path / f"bin/game.dll{dd.PACKED_SUFFIX}").write_bytes(PACKED)
    srv = cdn({})  # a download would 404
    dd.sync(_plan(_file(srv.url + "/v57/1.gz")), tmp_path)
    assert (tmp_path / "bin/game.dll").read_bytes() == DATA
    assert srv.requests == []


def test_a_stale_packed_copy_is_fetched_again(cdn, tmp_path):
    (tmp_path / "bin").mkdir()
    (tmp_path / f"bin/game.dll{dd.PACKED_SUFFIX}").write_bytes(b"x" * len(PACKED))
    srv = cdn({"/v57/1.gz": PACKED})
    dd.sync(_plan(_file(srv.url + "/v57/1.gz")), tmp_path)
    assert (tmp_path / "bin/game.dll").read_bytes() == DATA
    assert srv.requests == ["/v57/1.gz"]


def test_leftover_packed_staging_is_removed_after_a_finished_sync(cdn, tmp_path):
    """A .nadare-gz (or its .part) of a file the plan no longer needs is Nadare's own litter."""
    (tmp_path / "bin").mkdir()
    (tmp_path / "bin/game.dll").write_bytes(DATA)
    (tmp_path / f"bin/old.dll{dd.PACKED_SUFFIX}").write_bytes(b"left by a killed run")
    (tmp_path / f"bin/old2.dll{dd.PACKED_SUFFIX}.part").write_bytes(b"half")
    (tmp_path / "bin/notes.gz").write_bytes(b"the player's own")
    srv = cdn({})
    dd.sync(_plan(_file(srv.url + "/v57/1.gz")), tmp_path)
    assert sorted(p.name for p in (tmp_path / "bin").iterdir()) == ["game.dll", "notes.gz"]


def test_cancel_during_unpack_keeps_the_packed_copy_for_the_next_run(cdn, tmp_path, monkeypatch):
    srv = cdn({"/v57/1.gz": PACKED})
    cancel = Cancel()
    real = dd._unpack

    def cancelling(*args, **kwargs):
        cancel.cancel()
        return real(*args, **kwargs)

    monkeypatch.setattr(dd, "_unpack", cancelling)
    with pytest.raises(Cancelled):
        dd.sync(_plan(_file(srv.url + "/v57/1.gz")), tmp_path, cancel=cancel)
    assert (tmp_path / f"bin/game.dll{dd.PACKED_SUFFIX}").read_bytes() == PACKED
    assert not (tmp_path / "bin/game.dll").exists()
    monkeypatch.setattr(dd, "_unpack", real)
    srv.requests.clear()
    dd.sync(_plan(_file(srv.url + "/v57/1.gz")), tmp_path)
    assert (tmp_path / "bin/game.dll").read_bytes() == DATA
    assert srv.requests == []


def test_headers_are_sent_with_the_packed_request(cdn, tmp_path):
    srv = cdn({"/v57/1.gz": PACKED})
    rf = _file(srv.url + "/v57/1.gz", headers=(("caller-id", "LAUNCHER"),))
    dd.sync(_plan(rf), tmp_path)
    assert srv.headers[-1]["caller-id"] == "LAUNCHER"


def test_an_unknown_codec_is_refused_before_anything_is_written(tmp_path):
    rf = dd.RemoteFile(
        "bin/game.dll", "http://unused/1.zst", 1, md5="0" * 32, packed=dd.Packed("zstd")
    )
    with pytest.raises(dd.DirectError, match="zstd"):
        dd.sync(_plan(rf), tmp_path / "game")
    assert not (tmp_path / "game").exists()


def test_progress_counts_the_bytes_on_the_wire(cdn, tmp_path):
    srv = cdn({"/v57/1.gz": PACKED})
    seen: list[tuple[int, int]] = []

    def emit(event):
        data = event.data if isinstance(event.data, dict) else {}
        if event.kind == "progress":
            seen.append((data.get("done"), data.get("total")))

    dd.sync(_plan(_file(srv.url + "/v57/1.gz")), tmp_path, emit=emit)
    assert seen and seen[-1] == (len(PACKED), len(PACKED))


def test_sync_without_its_own_lock_runs_under_the_callers(cdn, tmp_path):
    """A caller that holds the install lock over more than the sync (CZN: plan, sync, version
    marker) passes ``lock=False``; another sync still finds the folder busy."""
    srv = cdn({"/v57/1.gz": PACKED})
    with dd.install_lock(tmp_path):
        with pytest.raises(dd.InstallBusy):
            dd.sync(_plan(_file(srv.url + "/v57/1.gz")), tmp_path)
        assert srv.requests == []
        dd.sync(_plan(_file(srv.url + "/v57/1.gz")), tmp_path, lock=False)
    assert (tmp_path / "bin/game.dll").read_bytes() == DATA


def test_two_packed_files_in_parallel(cdn, tmp_path):
    other = b"second file" * 1000
    other_packed = gzip.compress(other)
    srv = cdn({"/v57/1.gz": PACKED, "/v56/2.gz": other_packed})
    files = (
        _file(srv.url + "/v57/1.gz"),
        _file(srv.url + "/v56/2.gz", path="bin/other.dll", data=other, packed=other_packed),
    )
    dd.sync(_plan(*files), tmp_path, workers=4)
    assert (tmp_path / "bin/game.dll").read_bytes() == DATA
    assert (tmp_path / "bin/other.dll").read_bytes() == other
