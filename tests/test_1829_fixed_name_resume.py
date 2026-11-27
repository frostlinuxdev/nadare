# SPDX-License-Identifier: GPL-3.0-or-later
"""#1829: an update of fixed-name archives that stops part-way still removes what was dropped.

R1999's full package is package-00..19.7z under the same names every version. Each archive's
``.done`` record is replaced as soon as that archive is installed, and the dropped-file prune
runs at the end. Stopped in between, the RESUME read the new record of a finished archive, so
the files that archive dropped were never removed. The sync now keeps what the archives owned
when it started (``prune-pending-<game>.json``) until a sync of that game completes.
"""

from __future__ import annotations

import json
import time

import pytest
from test_directdl_prune import make_zip, md5, server  # noqa: F401 - fixture reuse

from nadare.core import directdl as dd


def _archive(srv, name, blob, path):
    return dd.Archive(
        name, dd.RemoteFile(f"{name}.zip", srv.url + path, len(blob), md5=md5(blob)), slot=name
    )


@pytest.fixture
def two_packages(server, tmp_path, monkeypatch):  # noqa: F811
    a1 = make_zip({"a/keep.bin": b"a1", "a/dropped.bin": b"dropped by v2"})
    b1 = make_zip({"b/keep.bin": b"b1"})
    a2 = make_zip({"a/keep.bin": b"a2"})
    b2 = make_zip({"b/keep.bin": b"b2"})
    srv = server({"/1/a": a1, "/1/b": b1, "/2/a": a2, "/2/b": b2})
    v1 = dd.Plan(
        "T",
        "1",
        archives=(_archive(srv, "pkg-00", a1, "/1/a"), _archive(srv, "pkg-01", b1, "/1/b")),
    )
    v2 = dd.Plan(
        "T",
        "2",
        archives=(_archive(srv, "pkg-00", a2, "/2/a"), _archive(srv, "pkg-01", b2, "/2/b")),
    )
    dd.sync(v1, tmp_path, workers=1)
    assert (tmp_path / "a/dropped.bin").is_file()
    broken_b = dd.Archive(
        "pkg-01",
        dd.RemoteFile("pkg-01.zip", srv.url + "/missing", len(b2), md5=md5(b2)),
        slot="pkg-01",
    )
    stopped = dd.Plan("T", "2", archives=(v2.archives[0], broken_b))
    # The 2-pack pipeline (#1817) downloads pkg-01 while pkg-00 unpacks, and a failed pkg-01
    # cancels pkg-00 when it is not installed yet (CI flake, 2026-11-26). These tests are about
    # "pkg-00 is installed, then pkg-01 fails", so the broken download waits for pkg-00.
    real = dd._download_remote

    def after_pkg00(rf, path, *args, **kwargs):
        if rf.url.endswith("/missing"):
            deadline = time.monotonic() + 10
            keep = tmp_path / "a/keep.bin"
            while time.monotonic() < deadline and keep.read_bytes() != b"a2":
                time.sleep(0.005)
        return real(rf, path, *args, **kwargs)

    monkeypatch.setattr(dd, "_download_remote", after_pkg00)
    return v2, stopped


def test_resume_after_a_stopped_update_removes_what_a_finished_archive_dropped(
    two_packages, tmp_path
):
    v2, stopped = two_packages
    with pytest.raises(dd.DirectError):
        dd.sync(stopped, tmp_path, workers=1)  # pkg-00 is installed, pkg-01 fails
    assert (tmp_path / "a/dropped.bin").is_file()  # nothing removed by a failed sync
    dd.sync(v2, tmp_path, workers=1)  # RESUME
    assert not (tmp_path / "a/dropped.bin").exists()
    assert (tmp_path / "a/keep.bin").read_bytes() == b"a2"


def test_the_pending_list_is_gone_after_a_complete_sync(two_packages, tmp_path):
    v2, stopped = two_packages
    with pytest.raises(dd.DirectError):
        dd.sync(stopped, tmp_path, workers=1)
    pending = tmp_path / dd.STATE_DIR / "prune-pending-T.json"
    assert "a/dropped.bin" in json.loads(pending.read_text())["files"]
    dd.sync(v2, tmp_path, workers=1)
    assert not pending.exists()


def test_a_pending_list_of_another_game_is_ignored(two_packages, tmp_path):
    v2, stopped = two_packages
    with pytest.raises(dd.DirectError):
        dd.sync(stopped, tmp_path, workers=1)
    pending = tmp_path / dd.STATE_DIR / "prune-pending-T.json"
    data = json.loads(pending.read_text())
    data["game"] = "other"
    pending.write_text(json.dumps(data))
    dd.sync(v2, tmp_path, workers=1)
    assert (tmp_path / "a/dropped.bin").is_file()  # the bug's old behaviour: kept, never wrong


def test_a_file_changed_since_is_kept_even_from_the_pending_list(two_packages, tmp_path):
    v2, stopped = two_packages
    with pytest.raises(dd.DirectError):
        dd.sync(stopped, tmp_path, workers=1)
    (tmp_path / "a/dropped.bin").write_bytes(b"DROPPED BY V2")
    dd.sync(v2, tmp_path, workers=1)
    assert (tmp_path / "a/dropped.bin").read_bytes() == b"DROPPED BY V2"
