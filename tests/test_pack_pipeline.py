# SPDX-License-Identifier: GPL-3.0-or-later
# Copyright (C) 2026 Nadare contributors
"""Pack download/unpack pipeline tests (#1817): overlap, ordering, bounds, cancel, resume."""

from __future__ import annotations

import errno
import hashlib
import http.client
import http.server
import io
import os
import tempfile
import threading
import time
import zipfile
from pathlib import Path

import pytest
from _httpserver import Server

from nadare.core import directdl as dd
from nadare.core import diskspace
from nadare.core import download as dl
from nadare.core.backends import BackendError
from nadare.core.events import Cancel, Cancelled, Event


def _md5(data: bytes) -> str:
    return hashlib.md5(data).hexdigest()


def _make_zip(files: dict[str, bytes]) -> bytes:
    buf = io.BytesIO()
    with zipfile.ZipFile(buf, "w", compression=zipfile.ZIP_STORED) as zf:
        for name, body in files.items():
            zf.writestr(name, body)
    return buf.getvalue()


class _KeepAliveServer:
    """Minimal localhost HTTP/1.1 server supporting keep-alive and optional Range requests."""

    def __init__(
        self,
        routes: dict[str, bytes],
        *,
        support_range: bool = True,
        before_get=None,
    ) -> None:
        self.routes = routes
        self.support_range = support_range
        self.before_get = before_get
        self.requests: list[tuple[str, str | None]] = []
        self.connections = 0
        self._lock = threading.Lock()
        parent = self

        class Handler(http.server.BaseHTTPRequestHandler):
            protocol_version = "HTTP/1.1"

            def log_message(self, *args: object) -> None:
                pass

            def setup(self) -> None:
                super().setup()
                with parent._lock:
                    parent.connections += 1

            def do_HEAD(self) -> None:
                body = parent.routes.get(self.path)
                if body is None:
                    self.send_response(404)
                    self.send_header("Content-Length", "0")
                    self.end_headers()
                    return
                self.send_response(200)
                if parent.support_range:
                    self.send_header("Accept-Ranges", "bytes")
                self.send_header("Content-Length", str(len(body)))
                self.end_headers()

            def do_GET(self) -> None:
                body = parent.routes.get(self.path)
                range_hdr = self.headers.get("Range")
                if parent.before_get is not None:
                    parent.before_get(self.path, range_hdr)
                with parent._lock:
                    parent.requests.append((self.path, range_hdr))
                if body is None:
                    self.send_response(404)
                    self.send_header("Content-Length", "0")
                    self.end_headers()
                    return
                start, end = 0, len(body) - 1
                partial = False
                if parent.support_range and range_hdr and range_hdr.startswith("bytes="):
                    spec = range_hdr.removeprefix("bytes=")
                    a, _, b = spec.partition("-")
                    start = int(a)
                    end = int(b) if b else len(body) - 1
                    partial = True
                chunk = body[start : end + 1]
                self.send_response(206 if partial else 200)
                if parent.support_range:
                    self.send_header("Accept-Ranges", "bytes")
                if partial:
                    self.send_header("Content-Range", f"bytes {start}-{end}/{len(body)}")
                self.send_header("Content-Length", str(len(chunk)))
                self.end_headers()
                self.wfile.write(chunk)

        self._server = http.server.ThreadingHTTPServer(("127.0.0.1", 0), Handler)
        self._thread = threading.Thread(target=self._server.serve_forever, daemon=True)
        self._thread.start()
        self.url = f"http://127.0.0.1:{self._server.server_port}"

    def close(self) -> None:
        self._server.shutdown()
        self._server.server_close()
        self._thread.join(timeout=5)


def _zip_plan(blobs: dict[str, bytes], base_url: str = "https://example.invalid") -> dd.Plan:
    archives = tuple(
        dd.Archive(
            name=name,
            source=dd.RemoteFile(name, f"{base_url}/{name}", len(data), md5=_md5(data)),
            unwrap=("zip",),
        )
        for name, data in blobs.items()
    )
    return dd.Plan("packpipe", "1.0", archives=archives)


def test_ten_packs_overlap_download_and_unpack_time(tmp_path, monkeypatch):
    """10 packs with download time T and unpack time U finish near 10*max(T,U), not 10*(T+U)."""
    count = 10
    dl_time = 0.15  # long enough that per-pack overhead on a loaded CI host does not decide it
    unpack_time = 0.15
    blobs = {
        f"p{i:02}.zip": _make_zip({f"f{i:02}.bin": f"data-{i}".encode()}) for i in range(count)
    }
    plan = _zip_plan(blobs)
    real_unwrap = dd._unwrap
    overlaps = 0
    unpacking_now = threading.Event()
    lock = threading.Lock()

    def fake_download(rf, path, *, emit=None, cancel=None, pool=None, redirect_allowed=None):
        nonlocal overlaps
        if unpacking_now.is_set():
            with lock:
                overlaps += 1
        time.sleep(dl_time)
        if unpacking_now.is_set():
            with lock:
                overlaps += 1
        path.parent.mkdir(parents=True, exist_ok=True)
        path.write_bytes(blobs[rf.path])

    def slow_unwrap(archive, source, dest, dl_dir, cancel=None, changed=None):
        unpacking_now.set()
        try:
            time.sleep(unpack_time)
            return real_unwrap(archive, source, dest, dl_dir, cancel)
        finally:
            unpacking_now.clear()

    monkeypatch.setattr(dd, "_download_remote", fake_download)
    monkeypatch.setattr(dd, "_unwrap", slow_unwrap)

    # workers=1 simulates a single saturated network link (1 pack downloaded at a time)
    # overlapped with the separate unpack thread -> ~10*max(T,U) + min(T,U).
    started = time.monotonic()
    result = dd.sync(plan, tmp_path, workers=1)
    elapsed = time.monotonic() - started

    assert result == {"downloaded": count, "skipped": 0}
    assert overlaps > 0, "download of pack N+1 never overlapped with unpack of pack N"
    sequential = count * (dl_time + unpack_time)
    ideal = count * max(dl_time, unpack_time) + min(dl_time, unpack_time)
    # Below the sequential time, not a fraction of it: a loaded CI runner took 2.56s of 3.0s
    # three times on 11-27. The overlap count above is the pipeline proof; this bound only
    # catches a pipeline that went fully sequential.
    assert elapsed < sequential, (
        f"pipeline took {elapsed:.3f}s (expected ~{ideal:.3f}s, sequential is {sequential:.3f}s)"
    )
    for i in range(count):
        assert (tmp_path / f"f{i:02}.bin").read_bytes() == f"data-{i}".encode()


def test_unpack_order_matches_plan_even_when_next_pack_downloads_first(tmp_path, monkeypatch):
    """Pack 1 finishes downloading before Pack 0, yet Pack 0 still unpacks first on one thread."""
    blobs = {
        "p0.zip": _make_zip({"shared.bin": b"from-pack-0", "only0.bin": b"0"}),
        "p1.zip": _make_zip({"shared.bin": b"from-pack-1", "only1.bin": b"1"}),
        "p2.zip": _make_zip({"shared.bin": b"from-pack-2", "only2.bin": b"2"}),
    }
    plan = _zip_plan(blobs)
    real_unwrap = dd._unwrap
    p1_downloaded = threading.Event()
    download_finish_order: list[str] = []
    unpack_order: list[str] = []
    unpack_threads: set[int] = set()
    concurrent_unpacks = 0
    active_unpacks = 0
    lock = threading.Lock()

    def fake_download(rf, path, *, emit=None, cancel=None, pool=None, redirect_allowed=None):
        if rf.path == "p0.zip":
            # Force p1 to finish downloading well before p0 does so an out-of-order unpacker
            # would pick p1 while p0 is still downloading.
            assert p1_downloaded.wait(timeout=3.0), "p1 did not download concurrently with p0"
            time.sleep(0.02)
        path.parent.mkdir(parents=True, exist_ok=True)
        path.write_bytes(blobs[rf.path])
        with lock:
            download_finish_order.append(rf.path)
        if rf.path == "p1.zip":
            p1_downloaded.set()

    def tracking_unwrap(archive, source, dest, dl_dir, cancel=None, changed=None):
        nonlocal active_unpacks, concurrent_unpacks
        with lock:
            active_unpacks += 1
            concurrent_unpacks = max(concurrent_unpacks, active_unpacks)
            unpack_threads.add(threading.get_ident())
            unpack_order.append(archive.name)
        try:
            time.sleep(0.01)
            return real_unwrap(archive, source, dest, dl_dir, cancel)
        finally:
            with lock:
                active_unpacks -= 1

    monkeypatch.setattr(dd, "_download_remote", fake_download)
    monkeypatch.setattr(dd, "_unwrap", tracking_unwrap)

    dd.sync(plan, tmp_path, workers=2)

    assert download_finish_order.index("p1.zip") < download_finish_order.index("p0.zip")
    assert unpack_order == ["p0.zip", "p1.zip", "p2.zip"]
    assert concurrent_unpacks == 1
    assert len(unpack_threads) == 1
    assert (tmp_path / "shared.bin").read_bytes() == b"from-pack-2"


def test_never_more_than_two_packs_on_disk_at_once(tmp_path, monkeypatch):
    """Download runs ahead by at most 1 pack: peak packs on disk is 2, never 3."""
    blobs = {f"p{i}.zip": _make_zip({f"f{i}.bin": bytes([i]) * 64}) for i in range(6)}
    plan = _zip_plan(blobs)
    real_unwrap = dd._unwrap
    dl_dir = tmp_path / dd.DL_DIR
    peak_files = 0
    in_flight_tracked = 0
    peak_tracked = 0
    lock = threading.Lock()
    pack0_unpacking = threading.Event()
    pack1_written = threading.Event()

    def sample_disk() -> None:
        nonlocal peak_files
        if dl_dir.is_dir():
            archives_on_disk = [p for p in dl_dir.glob("*.zip") if p.is_file()]
            with lock:
                peak_files = max(peak_files, len(archives_on_disk))

    def fake_download(rf, path, *, emit=None, cancel=None, pool=None, redirect_allowed=None):
        nonlocal in_flight_tracked, peak_tracked
        with lock:
            in_flight_tracked += 1
            peak_tracked = max(peak_tracked, in_flight_tracked)
        path.parent.mkdir(parents=True, exist_ok=True)
        path.write_bytes(blobs[rf.path])
        sample_disk()
        if rf.path == "p1.zip":
            pack1_written.set()

    def slow_unwrap(archive, source, dest, dl_dir_, cancel=None, changed=None):
        nonlocal in_flight_tracked
        if archive.name == "p0.zip":
            pack0_unpacking.set()
            assert pack1_written.wait(timeout=3.0), "pack 1 did not download while pack 0 unpacked"
            # Give a buggy unbounded downloader time to wrongly pull pack 2 onto disk.
            time.sleep(0.05)
        sample_disk()
        try:
            return real_unwrap(archive, source, dest, dl_dir_, cancel)
        finally:
            sample_disk()
            with lock:
                in_flight_tracked -= 1

    monkeypatch.setattr(dd, "_download_remote", fake_download)
    monkeypatch.setattr(dd, "_unwrap", slow_unwrap)

    dd.sync(plan, tmp_path, workers=4)

    assert peak_files == 2, f"expected peak 2 packs on disk, saw {peak_files}"
    assert peak_tracked == 2, f"expected peak 2 tracked packs, saw {peak_tracked}"


def test_cancel_during_overlap_stops_both_and_stays_resumable(tmp_path, monkeypatch):
    """Cancel while pack 0 unpacks and pack 1 downloads stops both and keeps resumable state."""
    blobs = {
        "p0.zip": _make_zip({"f0.bin": b"zero" * 100}),
        "p1.zip": _make_zip({"f1.bin": b"one" * 100}),
        "p2.zip": _make_zip({"f2.bin": b"two" * 100}),
    }
    srv = _KeepAliveServer({f"/{name}": data for name, data in blobs.items()})
    try:
        plan = _zip_plan(blobs, base_url=srv.url)
        real_unwrap = dd._unwrap
        real_download = dd._download_remote
        cancel = Cancel()
        p0_in_unpack = threading.Event()
        p1_in_download = threading.Event()
        seen_downloads: list[str] = []
        p1_saw_cancel = threading.Event()
        p0_saw_cancel = threading.Event()
        p1_half = len(blobs["p1.zip"]) // 2

        def first_download(rf, path, *, emit=None, cancel=None, pool=None, redirect_allowed=None):
            seen_downloads.append(rf.path)
            path.parent.mkdir(parents=True, exist_ok=True)
            if rf.path == "p0.zip":
                path.write_bytes(blobs[rf.path])
                return
            if rf.path == "p1.zip":
                # Write partial .part bytes and wait until pack 0 is unpacking, then observe cancel.
                part = path.with_name(path.name + ".part")
                part.write_bytes(blobs[rf.path][:p1_half])
                p1_in_download.set()
                assert p0_in_unpack.wait(timeout=3.0), "p0 never entered unpack while p1 downloaded"
                deadline = time.monotonic() + 3.0
                while time.monotonic() < deadline:
                    if cancel is not None and cancel.cancelled:
                        p1_saw_cancel.set()
                        cancel.raise_if_cancelled()
                    time.sleep(0.005)
                raise AssertionError("p1 download never observed cancel")
            raise AssertionError(f"pack {rf.path} should never have started before cancel")

        def first_unwrap(archive, source, dest, dl_dir, cancel=None, changed=None):
            if archive.name == "p0.zip":
                p0_in_unpack.set()
                assert p1_in_download.wait(timeout=3.0), (
                    "p1 never started downloading during p0 unpack"
                )
                cancel.cancel()
                if cancel is not None and cancel.cancelled:
                    p0_saw_cancel.set()
                    cancel.raise_if_cancelled()
            return real_unwrap(archive, source, dest, dl_dir, cancel)

        monkeypatch.setattr(dd, "_download_remote", first_download)
        monkeypatch.setattr(dd, "_unwrap", first_unwrap)

        with pytest.raises(Cancelled):
            dd.sync(plan, tmp_path, cancel=cancel, workers=2)

        assert p0_saw_cancel.is_set()
        assert p1_saw_cancel.is_set()
        assert "p2.zip" not in seen_downloads
        # Resumable state remains in .nadare-dl: p0 verified archive + p1 .part file.
        assert (tmp_path / dd.DL_DIR / "p0.zip").is_file()
        assert (tmp_path / dd.DL_DIR / "p1.zip.part").is_file()

        # Second run uses the real downloader against localhost HTTP: p0 is reused from cache
        # (never requested), p1 resumes its .part with Range, and p2 downloads normally.
        monkeypatch.setattr(dd, "_download_remote", real_download)
        monkeypatch.setattr(dd, "_unwrap", real_unwrap)

        dd.sync(plan, tmp_path, workers=2)
        requested_paths = [p for p, _ in srv.requests]
        assert "/p0.zip" not in requested_paths
        p1_ranges = [r for p, r in srv.requests if p == "/p1.zip"]
        assert p1_ranges and p1_ranges[0] == f"bytes={p1_half}-"
        assert (tmp_path / "f0.bin").read_bytes() == b"zero" * 100
        assert (tmp_path / "f1.bin").read_bytes() == b"one" * 100
        assert (tmp_path / "f2.bin").read_bytes() == b"two" * 100
    finally:
        srv.close()


def test_unpack_error_in_pack_n_cancels_pack_n_plus_1(tmp_path, monkeypatch):
    """Unpack failure in pack 0 cancels pack 1's download and raises pack 0's error."""
    blobs = {
        "p0.zip": _make_zip({"f0.bin": b"zero"}),
        "p1.zip": _make_zip({"f1.bin": b"one"}),
        "p2.zip": _make_zip({"f2.bin": b"two"}),
    }
    plan = _zip_plan(blobs)
    p1_started = threading.Event()
    p1_cancelled = threading.Event()
    started: list[str] = []

    def fake_download(rf, path, *, emit=None, cancel=None, pool=None, redirect_allowed=None):
        started.append(rf.path)
        path.parent.mkdir(parents=True, exist_ok=True)
        if rf.path == "p0.zip":
            path.write_bytes(blobs[rf.path])
            return
        if rf.path == "p1.zip":
            p1_started.set()
            deadline = time.monotonic() + 3.0
            while time.monotonic() < deadline:
                if cancel is not None and cancel.cancelled:
                    p1_cancelled.set()
                    cancel.raise_if_cancelled()
                time.sleep(0.005)
            raise AssertionError("p1 download was NOT cancelled when p0 unpack failed")
        raise AssertionError(f"pack {rf.path} should not start")

    def failing_unwrap(archive, source, dest, dl_dir, cancel=None, changed=None):
        assert archive.name == "p0.zip"
        assert p1_started.wait(timeout=3.0), "p1 never overlapped with p0 unpack"
        raise dd.DirectError("corrupt archive p0")

    monkeypatch.setattr(dd, "_download_remote", fake_download)
    monkeypatch.setattr(dd, "_unwrap", failing_unwrap)

    with pytest.raises(dd.DirectError, match="corrupt archive p0"):
        dd.sync(plan, tmp_path, workers=2)

    assert p1_cancelled.is_set(), "unpack error in pack 0 did not cancel pack 1"
    assert "p2.zip" not in started
    # Corrupt pack 0 is removed; pack 1 was not deleted as corrupt.
    assert not (tmp_path / dd.DL_DIR / "p0.zip").exists()


def test_unpack_error_in_pack_n_keeps_already_verified_pack_n_plus_1(tmp_path, monkeypatch):
    """If pack 1 finished before pack 0 failed unpack, pack 1 stays cached for resume."""
    blobs = {
        "p0.zip": _make_zip({"f0.bin": b"zero"}),
        "p1.zip": _make_zip({"f1.bin": b"one"}),
    }
    plan = _zip_plan(blobs)
    p1_ready = threading.Event()

    def fake_download(rf, path, *, emit=None, cancel=None, pool=None, redirect_allowed=None):
        path.parent.mkdir(parents=True, exist_ok=True)
        path.write_bytes(blobs[rf.path])
        if rf.path == "p1.zip":
            p1_ready.set()

    def failing_unwrap(archive, source, dest, dl_dir, cancel=None, changed=None):
        assert p1_ready.wait(timeout=3.0), "p1 did not finish downloading during p0 unpack"
        raise dd.DirectError("corrupt p0")

    monkeypatch.setattr(dd, "_download_remote", fake_download)
    monkeypatch.setattr(dd, "_unwrap", failing_unwrap)

    with pytest.raises(dd.DirectError, match="corrupt p0"):
        dd.sync(plan, tmp_path, workers=2)

    assert (tmp_path / dd.DL_DIR / "p1.zip").read_bytes() == blobs["p1.zip"]


def test_free_space_check_asks_room_for_two_packs_and_the_staging_copy(tmp_path, monkeypatch):
    """With 2+ packs, the pre-flight asks for two archives in flight and one staging copy.

    While pack N unpacks, its archive, its unpacked staging copy and pack N+1 are all on disk.
    """
    size = 10_000
    blobs = {
        "p0.zip": _make_zip({"a.bin": b"a" * size}),
        "p1.zip": _make_zip({"b.bin": b"b" * size}),
        "p2.zip": _make_zip({"c.bin": b"c" * size}),
    }
    plan = _zip_plan(blobs)
    s0, s1, s2 = len(blobs["p0.zip"]), len(blobs["p1.zip"]), len(blobs["p2.zip"])
    expected = max(s0 + s1 + s0, s1 + s2 + s1)
    # Enough room for 1 pack + 5% margin, NOT enough for the true peak.
    one_pack_room = int(max(s0, s1, s2) * 1.2)
    assert one_pack_room < expected

    asked: list[int] = []
    real_check = diskspace.check_space

    def recording_check(path, needed_bytes, margin=diskspace.DEFAULT_MARGIN):
        asked.append(needed_bytes)
        return real_check(path, needed_bytes, margin=margin)

    monkeypatch.setattr(diskspace, "free_bytes", lambda _path: one_pack_room)
    monkeypatch.setattr(diskspace, "check_space", recording_check)
    monkeypatch.setattr(
        dd, "_download_remote", lambda *a, **k: pytest.fail("download started without 2-pack room")
    )

    with pytest.raises(BackendError, match="Not enough disk space"):
        dd.sync(plan, tmp_path, workers=2)

    assert asked and asked[0] == expected


def test_pack_progress_counts_both_packs_monotonically_within_total(tmp_path, monkeypatch):
    """Overlapping packs add into one bar that never goes backwards and never exceeds total."""
    blobs = {
        "p0.zip": _make_zip({"a.bin": b"a" * 2000}),
        "p1.zip": _make_zip({"b.bin": b"b" * 2000}),
    }
    plan = _zip_plan(blobs)
    s0, s1 = len(blobs["p0.zip"]), len(blobs["p1.zip"])
    total = s0 + s1
    p0_half = threading.Event()
    p1_half = threading.Event()

    def fake_download(rf, path, *, emit=None, cancel=None, pool=None, redirect_allowed=None):
        size = len(blobs[rf.path])
        half = size // 2
        emit(
            Event(
                "progress",
                f"download: {half}/{size}",
                progress=half / size,
                data={"stage": "download", "done": half, "total": size},
            )
        )
        if rf.path == "p0.zip":
            p0_half.set()
            assert p1_half.wait(timeout=3.0), "p1 never reported progress while p0 was halfway"
        else:
            assert p0_half.wait(timeout=3.0), "p0 never reached halfway"
            p1_half.set()
        path.parent.mkdir(parents=True, exist_ok=True)
        path.write_bytes(blobs[rf.path])
        emit(
            Event(
                "progress",
                f"download: {size}/{size}",
                progress=1.0,
                data={"stage": "download", "done": size, "total": size},
            )
        )

    monkeypatch.setattr(dd, "_download_remote", fake_download)
    events: list[Event] = []
    dd.sync(plan, tmp_path, emit=events.append, workers=2)

    progress = [
        e for e in events if e.kind == "progress" and isinstance(e.data, dict)
        and e.data.get("stage") == "download"
    ]
    dones = [e.data["done"] for e in progress]
    totals = {e.data["total"] for e in progress}
    assert totals == {total}
    assert dones == sorted(dones), f"progress went backwards: {dones}"
    assert max(dones) <= total
    assert dones[-1] == total
    # When both packs are halfway, combined done MUST equal s0//2 + s1//2 (not s0 + s1//2!).
    assert (s0 // 2 + s1 // 2) in dones, (
        f"expected combined halfway byte count {s0 // 2 + s1 // 2} in {dones}"
    )
    steps = [e.message for e in events if e.kind == "step" and "installing pack" in e.message]
    assert steps == ["installing pack 1 of 2: p0.zip", "installing pack 2 of 2: p1.zip"]


def test_resume_after_pack_n_done_and_pack_n_plus_1_half_downloaded(tmp_path):
    """Interrupted run with pack 0 done and pack 1 partial reuses pack 1's finished parts."""
    z0 = _make_zip({"a.bin": b"pack0-data" * 500})
    z1 = _make_zip({"b.bin": b"pack1-data" * 4000})
    z2 = _make_zip({"c.bin": b"pack2-data" * 500})
    cut = len(z1) // 3
    p1_pieces = [z1[:cut], z1[cut : cut * 2], z1[cut * 2 :]]
    routes = {
        "/p0.zip": z0,
        "/p1.0": p1_pieces[0],
        "/p1.1": p1_pieces[1],
        "/p1.2": p1_pieces[2],
        "/p2.zip": z2,
    }
    srv = _KeepAliveServer(routes)
    try:
        parts = tuple(
            dd.RemoteFile(
                f"p1.{i}",
                srv.url + f"/p1.{i}",
                len(piece),
                sha256=hashlib.sha256(piece).hexdigest(),
            )
            for i, piece in enumerate(p1_pieces)
        )
        p0_rf = dd.RemoteFile("p0.zip", srv.url + "/p0.zip", len(z0), md5=_md5(z0))
        p2_rf = dd.RemoteFile("p2.zip", srv.url + "/p2.zip", len(z2), md5=_md5(z2))
        plan = dd.Plan(
            "packpipe",
            "1.0",
            archives=(
                dd.Archive("p0", p0_rf),
                dd.Archive(
                    "p1",
                    dd.RemoteFile("p1.zip", srv.url + "/p1.0", len(z1), md5=_md5(z1)),
                    parts=parts,
                ),
                dd.Archive("p2", p2_rf),
            ),
        )

        # Simulate interrupted run: p0 finished (.done written), p1 part 0 already verified in
        # .nadare-dl and part 1 half-downloaded in .part, with p2 also pending so resume runs
        # through _sync_archives / download_part (not the 1-pack shortcut).
        dd.sync(dd.Plan("packpipe", "1.0", archives=(plan.archives[0],)), tmp_path, workers=2)
        dl_dir = tmp_path / dd.DL_DIR
        dl_dir.mkdir(parents=True, exist_ok=True)
        (dl_dir / "p1.0").write_bytes(p1_pieces[0])
        (dl_dir / "p1.1.part").write_bytes(p1_pieces[1][: len(p1_pieces[1]) // 2])

        before = len(srv.requests)
        dd.sync(plan, tmp_path, workers=2)
        after = srv.requests[before:]

        # Pack 0 was already done; pack 1 part 0 was already verified -> neither requested.
        requested_paths = [p for p, _ in after]
        assert "/p0.zip" not in requested_paths
        assert "/p1.0" not in requested_paths
        assert "/p2.zip" in requested_paths
        # Part 1 resumed with Range instead of restarting from 0.
        p1_1_ranges = [r for p, r in after if p == "/p1.1"]
        assert p1_1_ranges and p1_1_ranges[0] == f"bytes={len(p1_pieces[1]) // 2}-"
        assert (tmp_path / "a.bin").read_bytes() == b"pack0-data" * 500
        assert (tmp_path / "b.bin").read_bytes() == b"pack1-data" * 4000
        assert (tmp_path / "c.bin").read_bytes() == b"pack2-data" * 500
    finally:
        srv.close()


def test_packs_reuse_one_keepalive_connection_pool(tmp_path):
    """Single-source packs share one keep-alive HttpClientPool across all packs."""
    blobs = {f"p{i}.zip": _make_zip({f"f{i}.bin": f"body-{i}".encode() * 20}) for i in range(6)}
    srv = _KeepAliveServer({f"/{name}": data for name, data in blobs.items()})
    try:
        plan = _zip_plan(blobs, base_url=srv.url)
        dd.sync(plan, tmp_path, workers=2)
        assert len(srv.requests) == 6
        assert srv.connections <= 2, f"expected <=2 keep-alive connections, got {srv.connections}"
    finally:
        srv.close()


def test_multipart_packs_have_no_tail_gap_across_boundary(tmp_path, monkeypatch):
    """While pack 0's slow last part finishes, a free worker starts pack 1's parts immediately."""
    z0 = _make_zip({"a.bin": b"alpha" * 200})
    z1 = _make_zip({"b.bin": b"beta" * 200})
    p0_a, p0_b = z0[: len(z0) // 2], z0[len(z0) // 2 :]
    p1_a, p1_b = z1[: len(z1) // 2], z1[len(z1) // 2 :]
    part_data = {"p0.0": p0_a, "p0.1": p0_b, "p1.0": p1_a, "p1.1": p1_b}

    plan = dd.Plan(
        "packpipe",
        "1.0",
        archives=(
            dd.Archive(
                "p0",
                dd.RemoteFile("p0.zip", "https://example.invalid/p0", len(z0), md5=_md5(z0)),
                parts=(
                    dd.RemoteFile("p0.0", "https://example.invalid/p0.0", len(p0_a)),
                    dd.RemoteFile("p0.1", "https://example.invalid/p0.1", len(p0_b)),
                ),
            ),
            dd.Archive(
                "p1",
                dd.RemoteFile("p1.zip", "https://example.invalid/p1", len(z1), md5=_md5(z1)),
                parts=(
                    dd.RemoteFile("p1.0", "https://example.invalid/p1.0", len(p1_a)),
                    dd.RemoteFile("p1.1", "https://example.invalid/p1.1", len(p1_b)),
                ),
            ),
        ),
    )

    # Set when any of pack 1's parts starts. Starting before pack 0's tail thread runs counts too
    # (under CI load it can): either way pack 1 did not wait for that tail.
    p1_started = threading.Event()

    def fake_download(rf, path, *, emit=None, cancel=None, pool=None, redirect_allowed=None):
        path.parent.mkdir(parents=True, exist_ok=True)
        if rf.path == "p0.1":
            # Hold pack 0's tail part until pack 1 has started on the other worker.
            assert p1_started.wait(timeout=10.0), (
                "pack 1 did not start while pack 0's tail part was still in flight"
            )
        elif rf.path in ("p1.0", "p1.1"):
            p1_started.set()
        path.write_bytes(part_data[rf.path])

    monkeypatch.setattr(dd, "_download_remote", fake_download)
    dd.sync(plan, tmp_path, workers=2)
    assert p1_started.is_set()
    assert (tmp_path / "a.bin").read_bytes() == b"alpha" * 200
    assert (tmp_path / "b.bin").read_bytes() == b"beta" * 200


def test_fresh_md5_download_hashes_inline_without_post_download_reads(tmp_path, monkeypatch):
    """Fresh download hashes sha256/sha512/md5 inline without post-download reads."""
    body = _make_zip({"a.bin": b"inline-hash" * 500})
    srv = _KeepAliveServer({"/p.zip": body, "/bad.zip": body + b"corrupt"})
    try:
        monkeypatch.setattr(
            dl, "hash_file", lambda *a, **k: pytest.fail("hash_file re-read fresh download")
        )
        monkeypatch.setattr(
            dd, "_verify_remote", lambda *a, **k: pytest.fail("_verify_remote re-read fresh md5")
        )
        good_plan = dd.Plan(
            "packpipe",
            "1.0",
            archives=(
                dd.Archive(
                    "p", dd.RemoteFile("p.zip", srv.url + "/p.zip", len(body), md5=_md5(body))
                ),
            ),
        )
        dd.sync(good_plan, tmp_path / "ok", workers=2)
        assert (tmp_path / "ok" / "a.bin").read_bytes() == b"inline-hash" * 500

        # A bad MD5 is still rejected immediately without needing _verify_remote.
        bad_plan = dd.Plan(
            "packpipe",
            "1.0",
            archives=(
                dd.Archive(
                    "bad",
                    dd.RemoteFile(
                        "bad.zip",
                        srv.url + "/bad.zip",
                        len(body) + 7,
                        md5=_md5(body),  # wrong md5
                    ),
                ),
            ),
        )
        with pytest.raises(dd.DirectError, match="hash mismatch"):
            dd.sync(bad_plan, tmp_path / "bad", workers=2)
    finally:
        srv.close()


# ---------------------------------------------------------------------------
# Fix round 1 tests (NEON-REVIEW.md items 1-9, 11-15)
# ---------------------------------------------------------------------------


def test_error_in_pack_n_plus_1_while_pack_n_downloads_never_unpacks_or_sets_changed(
    tmp_path, monkeypatch
):
    """Error in pack 1 while pack 0 downloads (3-pack plan) never unpacks pack 0 or sets changed."""
    blobs = {
        "p0.zip": _make_zip({"f0.bin": b"real-p0"}),
        "p1.zip": _make_zip({"f1.bin": b"real-p1"}),
        "p2.zip": _make_zip({"f2.bin": b"real-p2"}),
    }
    plan = _zip_plan(blobs)
    # Plant a stale unverified archive for p0 so a buggy premature unpack would actually unpack
    # unverified bytes (or, when absent, raise FileNotFoundError masking p1's real error).
    dl_dir = tmp_path / dd.DL_DIR
    dl_dir.mkdir(parents=True, exist_ok=True)
    (dl_dir / "p0.zip").write_bytes(_make_zip({"stale.bin": b"UNVERIFIED"}))

    p0_started = threading.Event()
    unpacked: list[str] = []
    real_unwrap = dd._unwrap

    def fake_download(rf, path, *, emit=None, cancel=None, pool=None, redirect_allowed=None):
        if rf.path == "p0.zip":
            p0_started.set()
            deadline = time.monotonic() + 3.0
            while time.monotonic() < deadline:
                if cancel is not None and cancel.cancelled:
                    cancel.raise_if_cancelled()
                time.sleep(0.005)
            raise AssertionError("p0 was never cancelled after p1 failed")
        if rf.path == "p1.zip":
            assert p0_started.wait(timeout=3.0), "p0 did not start before p1"
            # Let feed advance to pack 2 and park on in_flight so feed's finally runs on cancel.
            time.sleep(0.03)
            raise dd.DirectError("p1 download boom")
        raise AssertionError(f"unexpected pack started: {rf.path}")

    def tracking_unwrap(archive, source, dest, dl_dir_, cancel=None, changed=None):
        unpacked.append(archive.name)
        return real_unwrap(archive, source, dest, dl_dir_, cancel)

    monkeypatch.setattr(dd, "_download_remote", fake_download)
    monkeypatch.setattr(dd, "_unwrap", tracking_unwrap)

    changed = threading.Event()
    with pytest.raises(dd.DirectError, match="p1 download boom"):
        dd.sync(plan, tmp_path, workers=2, changed=changed)

    assert unpacked == [], f"unverified pack was unpacked prematurely: {unpacked}"
    assert not changed.is_set(), "changed was set even though nothing should have unpacked"
    assert not (tmp_path / "stale.bin").exists()


def test_error_in_pack_n_plus_1_with_missing_pack_n_raises_real_error_not_fnf(
    tmp_path, monkeypatch
):
    """When pack 1 fails before pack 0 writes, pack 1's error wins over FileNotFoundError."""
    blobs = {
        "p0.zip": _make_zip({"f0.bin": b"real-p0"}),
        "p1.zip": _make_zip({"f1.bin": b"real-p1"}),
        "p2.zip": _make_zip({"f2.bin": b"real-p2"}),
    }
    plan = _zip_plan(blobs)
    p0_started = threading.Event()

    def fake_download(rf, path, *, emit=None, cancel=None, pool=None, redirect_allowed=None):
        if rf.path == "p0.zip":
            p0_started.set()
            deadline = time.monotonic() + 3.0
            while time.monotonic() < deadline:
                if cancel is not None and cancel.cancelled:
                    cancel.raise_if_cancelled()
                time.sleep(0.005)
            raise AssertionError("p0 was never cancelled")
        if rf.path == "p1.zip":
            assert p0_started.wait(timeout=3.0)
            time.sleep(0.03)
            raise dd.DirectError("p1 network failure")
        raise AssertionError(f"unexpected pack started: {rf.path}")

    monkeypatch.setattr(dd, "_download_remote", fake_download)

    changed = threading.Event()
    with pytest.raises(dd.DirectError, match="p1 network failure"):
        dd.sync(plan, tmp_path, workers=2, changed=changed)
    assert not changed.is_set()


def test_error_in_feed_while_pack_n_downloads_never_unpacks_or_sets_changed(
    tmp_path, monkeypatch
):
    """An exception raised inside feed on pack 1 never lets unfinished pack 0 unpack."""
    blobs = {
        "p0.zip": _make_zip({"f0.bin": b"real-p0"}),
        "p1.zip": _make_zip({"f1.bin": b"real-p1"}),
        "p2.zip": _make_zip({"f2.bin": b"real-p2"}),
    }
    plan = _zip_plan(blobs)
    p0_started = threading.Event()
    unpacked: list[str] = []

    def fake_download(rf, path, *, emit=None, cancel=None, pool=None, redirect_allowed=None):
        if rf.path == "p0.zip":
            p0_started.set()
            deadline = time.monotonic() + 3.0
            while time.monotonic() < deadline:
                if cancel is not None and cancel.cancelled:
                    cancel.raise_if_cancelled()
                time.sleep(0.005)
            raise AssertionError("p0 was never cancelled")
        raise AssertionError(f"unexpected download: {rf.path}")

    def failing_pin(archive):
        if archive.name == "p1.zip":
            assert p0_started.wait(timeout=3.0), "p0 did not start before feed processed p1"
            raise dd.DirectError("feed pin error on p1")
        return None

    monkeypatch.setattr(dd, "_download_remote", fake_download)
    monkeypatch.setattr(dd, "_pin_for", failing_pin)
    monkeypatch.setattr(dd, "_unwrap", lambda a, *args, **kw: unpacked.append(a.name) or [])

    changed = threading.Event()
    with pytest.raises(dd.DirectError, match="feed pin error on p1"):
        dd.sync(plan, tmp_path, workers=2, changed=changed)

    assert unpacked == []
    assert not changed.is_set()


def test_ranged_packs_reuse_connections_without_leaking_sockets(tmp_path, monkeypatch):
    """Many ranged packs (> RANGE_THRESHOLD) bound total TCP connections and tracked sockets."""
    payload = bytes(range(256)) * 256  # 64 KiB each
    blobs = {f"r{i}.zip": _make_zip({f"f{i}.bin": payload}) for i in range(6)}
    srv = _KeepAliveServer({f"/{name}": data for name, data in blobs.items()})
    peak_pool_all = 0
    pool_instances: list[dl.HttpClientPool] = []
    real_conn = dl.HttpClientPool._connection

    def tracking_connection(self, *args, **kwargs):
        nonlocal peak_pool_all
        if self not in pool_instances:
            pool_instances.append(self)
        conn = real_conn(self, *args, **kwargs)
        with self._lock:
            peak_pool_all = max(peak_pool_all, len(self._all))
        return conn

    try:
        monkeypatch.setattr(dl, "RANGE_THRESHOLD", 4096)
        monkeypatch.setattr(dl.HttpClientPool, "_connection", tracking_connection)
        plan = _zip_plan(blobs, base_url=srv.url)
        dd.sync(plan, tmp_path, workers=4)

        # Verify range requests were actually used (206 probes + byte ranges).
        range_headers = [r for _, r in srv.requests if r is not None]
        assert any(r == "bytes=0-0" for r in range_headers), "ranged path was not exercised"
        # With workers=4 (range_workers=2) and 2 packs in flight, at most ~4-6 connections should
        # serve all 6 packs (buggy un-recycled threads open >=12 connections and leak them in _all).
        assert srv.connections <= 6, (
            f"expected <=6 connections across 6 ranged packs, got {srv.connections}"
        )
        assert peak_pool_all <= 6, (
            f"HttpClientPool._all leaked connections across ranged packs: peak={peak_pool_all}"
        )
        for pool in pool_instances:
            assert pool._all == []
        for i in range(6):
            assert (tmp_path / f"f{i}.bin").read_bytes() == payload
    finally:
        srv.close()


def test_ranged_download_hashes_inline_and_resumed_fallback_checks_md5(tmp_path, monkeypatch):
    """Ranged fresh downloads hash inline without re-reading; resumed fallback still checks md5."""
    payload = bytes(range(256)) * 128  # 32 KiB
    body = _make_zip({"a.bin": payload})
    bad_body = body + b"corrupt"
    srv = _KeepAliveServer({"/r.zip": body, "/bad.zip": bad_body})
    try:
        with monkeypatch.context() as mp:
            mp.setattr(dl, "RANGE_THRESHOLD", 4096)
            mp.setattr(
                dl,
                "hash_file",
                lambda *a, **k: pytest.fail("hash_file re-read fresh ranged download"),
            )
            mp.setattr(
                dd,
                "_verify_remote",
                lambda *a, **k: pytest.fail("_verify_remote re-read fresh ranged md5"),
            )
            good_plan = dd.Plan(
                "packpipe",
                "1.0",
                archives=(
                    dd.Archive(
                        "r", dd.RemoteFile("r.zip", srv.url + "/r.zip", len(body), md5=_md5(body))
                    ),
                ),
            )
            dd.sync(good_plan, tmp_path / "ok", workers=4)
            assert (tmp_path / "ok" / "a.bin").read_bytes() == payload
            assert any(r == "bytes=0-0" for _, r in srv.requests), "ranged path was not taken"

            bad_plan = dd.Plan(
                "packpipe",
                "1.0",
                archives=(
                    dd.Archive(
                        "bad",
                        dd.RemoteFile(
                            "bad.zip", srv.url + "/bad.zip", len(bad_body), md5=_md5(body)
                        ),
                    ),
                ),
            )
            with pytest.raises(dd.DirectError, match="hash mismatch"):
                dd.sync(bad_plan, tmp_path / "bad", workers=4)
    finally:
        srv.close()

    # Resumed .part fallback (full cached .part, partial HTTP 206 resume, and corrupt prefix md5).
    srv2 = _KeepAliveServer({"/r.zip": body})
    try:
        dest = tmp_path / "resumed" / "r.zip"
        dest.parent.mkdir(parents=True, exist_ok=True)
        dest.with_name("r.zip.part").write_bytes(body)
        res = dl.download(srv2.url + "/r.zip", dest, expected_size=len(body), resume=True)
        assert res.resumed is True
        assert res.md5 == _md5(body)

        # Partial HTTP 206 resume (_download_once resumed=True -> hash_file fallback computes md5).
        half = len(body) // 2
        dest2 = tmp_path / "resumed2" / "r.zip"
        dest2.parent.mkdir(parents=True, exist_ok=True)
        dest2.with_name("r.zip.part").write_bytes(body[:half])
        res2 = dl.download(srv2.url + "/r.zip", dest2, expected_size=len(body), resume=True)
        assert res2.resumed is True
        assert res2.md5 == _md5(body)
        assert any(r == f"bytes={half}-" for _, r in srv2.requests)

        # Corrupt resumed .part prefix is caught by MD5 verification via _download_remote.
        corrupt_root = tmp_path / "resumed_corrupt"
        dl_dir = corrupt_root / dd.DL_DIR
        dl_dir.mkdir(parents=True, exist_ok=True)
        corrupt_prefix = bytes([body[0] ^ 0xFF]) + body[1:half]
        (dl_dir / "r.zip.part").write_bytes(corrupt_prefix)
        rf = dd.RemoteFile("r.zip", srv2.url + "/r.zip", len(body), md5=_md5(body))
        with pytest.raises(dd.DirectError, match="hash mismatch"):
            dd._download_remote(rf, dl_dir / "r.zip", emit=None, cancel=None)
    finally:
        srv2.close()


def test_pack_progress_never_emits_backwards_under_concurrent_interleave(monkeypatch):
    """Forcing thread B to update and emit while thread A is mid-emit never emits backwards."""
    archives = (
        dd.Archive("p0", dd.RemoteFile("p0.zip", "https://example.invalid/p0.zip", 1000)),
        dd.Archive("p1", dd.RemoteFile("p1.zip", "https://example.invalid/p1.zip", 1000)),
    )
    events: list[Event] = []
    t0_in_format = threading.Event()
    t1_emitted = threading.Event()
    real_human_size = diskspace.human_size

    def tracking_emit(ev: Event) -> None:
        events.append(ev)
        if ev.kind == "progress" and isinstance(ev.data, dict) and ev.data.get("done") == 1000:
            t1_emitted.set()

    def interleaved_human_size(n: int) -> str:
        if n == 500:
            t0_in_format.set()
            # If _PackProgress released its lock before formatting/emitting, thread 1 runs here,
            # emits 1000, sets t1_emitted immediately, and thread 0 then emits 500 -> backwards!
            t1_emitted.wait(timeout=0.05)
        return real_human_size(n)

    monkeypatch.setattr(diskspace, "human_size", interleaved_human_size)
    progress = dd._PackProgress(archives, tracking_emit)
    sink0 = progress.sink(0)
    sink1 = progress.sink(1)

    def run_t0() -> None:
        sink0(
            Event(
                "progress",
                "download: 500 / 1000",
                progress=0.5,
                data={"stage": "download", "done": 500, "total": 1000},
            )
        )

    def run_t1() -> None:
        assert t0_in_format.wait(timeout=2.0)
        sink1(
            Event(
                "progress",
                "download: 500 / 1000",
                progress=0.5,
                data={"stage": "download", "done": 500, "total": 1000},
            )
        )

    t0 = threading.Thread(target=run_t0)
    t1 = threading.Thread(target=run_t1)
    t0.start()
    t1.start()
    t0.join(timeout=5)
    t1.join(timeout=5)

    dones = [e.data["done"] for e in events if e.kind == "progress"]
    assert dones == [500, 1000], f"progress emitted out of order: {dones}"


def test_symlinked_dl_dir_and_subdir_refused_before_space_check(tmp_path, monkeypatch):
    """_check_work_dirs and symlink guards run before diskspace.check_space."""
    outside = tmp_path / "outside"
    outside.mkdir()
    (outside / "p0.zip").write_bytes(b"x" * 10_000)

    blobs = {
        "p0.zip": _make_zip({"a.bin": b"a" * 5000}),
        "p1.zip": _make_zip({"b.bin": b"b" * 5000}),
    }
    space_checked: list[int] = []
    monkeypatch.setattr(diskspace, "free_bytes", lambda _p: 0)
    monkeypatch.setattr(
        diskspace,
        "check_space",
        lambda path, need, **kw: space_checked.append(need) or (_ for _ in ()).throw(
            BackendError("Not enough disk space")
        ),
    )

    # Case 1: .nadare-dl itself is a symlink -> DirectError, check_space never called.
    inst1 = tmp_path / "inst1"
    inst1.mkdir()
    (inst1 / dd.DL_DIR).symlink_to(outside)
    with pytest.raises(dd.DirectError, match="symlink"):
        dd.sync(_zip_plan(blobs), inst1, workers=2)
    assert space_checked == [], "diskspace.check_space ran before _check_work_dirs"

    # Case 2: intermediate folder inside .nadare-dl is a symlink -> DirectError before check_space.
    inst2 = tmp_path / "inst2"
    (inst2 / dd.DL_DIR).mkdir(parents=True)
    (inst2 / dd.DL_DIR / "sub").symlink_to(outside)
    nested_plan = dd.Plan(
        "packpipe",
        "1.0",
        archives=(
            dd.Archive(
                "p0",
                dd.RemoteFile("sub/p0.zip", "https://example.invalid/p0.zip", 10_000),
            ),
            dd.Archive(
                "p1",
                dd.RemoteFile("sub/p1.zip", "https://example.invalid/p1.zip", 10_000),
            ),
        ),
    )
    with pytest.raises(dd.DirectError, match="symlink"):
        dd.sync(nested_plan, inst2, workers=2)
    assert space_checked == [], "diskspace.check_space ran before symlink guard on subdir"

    # Case 3: .nadare (STATE_DIR) is a symlink -> _check_work_dirs refuses before check_space.
    inst3 = tmp_path / "inst3"
    inst3.mkdir()
    (inst3 / dd.STATE_DIR).symlink_to(outside)
    with pytest.raises(dd.DirectError, match="symlink"):
        dd.sync(_zip_plan(blobs), inst3, workers=2)
    assert space_checked == [], "diskspace.check_space ran before _check_work_dirs on STATE_DIR"


def test_duplicate_archive_cache_paths_are_rejected(tmp_path):
    """Plans where two archives share a .nadare-dl cache path are rejected by validation."""
    z0 = _make_zip({"a.bin": b"a"})
    z1 = _make_zip({"b.bin": b"b"})

    dup_source = dd.Plan(
        "packpipe",
        "1.0",
        archives=(
            dd.Archive("p0", dd.RemoteFile("same.zip", "https://example.invalid/0", len(z0))),
            dd.Archive("p1", dd.RemoteFile("same.zip", "https://example.invalid/1", len(z1))),
        ),
    )
    with pytest.raises(dd.DirectError, match="duplicate archive cache path"):
        dd.sync(dup_source, tmp_path, workers=2)

    dup_part = dd.Plan(
        "packpipe",
        "1.0",
        archives=(
            dd.Archive(
                "p0",
                dd.RemoteFile("p0.zip", "https://example.invalid/p0", len(z0)),
                parts=(dd.RemoteFile("shared.0", "https://example.invalid/a", len(z0)),),
            ),
            dd.Archive(
                "p1",
                dd.RemoteFile("p1.zip", "https://example.invalid/p1", len(z1)),
                parts=(dd.RemoteFile("shared.0", "https://example.invalid/b", len(z1)),),
            ),
        ),
    )
    with pytest.raises(dd.DirectError, match="duplicate archive cache path"):
        dd.sync(dup_part, tmp_path, workers=2)

    source_vs_part = dd.Plan(
        "packpipe",
        "1.0",
        archives=(
            dd.Archive("p0", dd.RemoteFile("collide.bin", "https://example.invalid/0", len(z0))),
            dd.Archive(
                "p1",
                dd.RemoteFile("p1.zip", "https://example.invalid/p1", len(z1)),
                parts=(dd.RemoteFile("collide.bin", "https://example.invalid/b", len(z1)),),
            ),
        ),
    )
    with pytest.raises(dd.DirectError, match="duplicate archive cache path"):
        dd.sync(source_vs_part, tmp_path, workers=2)


def test_cancel_aborts_hash_parts_between_blocks_and_reaches_from_sync(tmp_path, monkeypatch):
    """_hash_parts honours cancel between 1 MiB blocks and receives cancel from _sync_archives."""
    chunk = b"A" * (1 << 20)  # 1 MiB
    p0 = tmp_path / "part0"
    p1 = tmp_path / "part1"
    p0.write_bytes(chunk * 2)  # 2 MiB
    p1.write_bytes(chunk)  # 1 MiB
    rf = dd.RemoteFile("big.zip", "https://example.invalid/big.zip", 3 << 20, md5="0" * 32)
    cancel = Cancel()
    blocks_hashed = 0
    real_new = hashlib.new

    class _CountingHasher:
        def __init__(self, inner):
            self._inner = inner

        def update(self, data: bytes) -> None:
            nonlocal blocks_hashed
            blocks_hashed += 1
            self._inner.update(data)
            if blocks_hashed == 1:
                cancel.cancel()

        def hexdigest(self) -> str:
            return self._inner.hexdigest()

    with monkeypatch.context() as mp:
        mp.setattr(hashlib, "new", lambda alg: _CountingHasher(real_new(alg)))
        with pytest.raises(Cancelled):
            dd._hash_parts([p0, p1], rf, "big.zip", cancel=cancel)
    assert blocks_hashed == 1, f"expected _hash_parts to stop after 1 block, hashed {blocks_hashed}"

    # Verify _sync_archives passes cancel into _hash_parts.
    seen_cancel: list[object] = []
    z0 = _make_zip({"a.bin": b"alpha"})
    z1 = _make_zip({"b.bin": b"beta"})
    part_blobs = {
        "p0.0": z0[: len(z0) // 2],
        "p0.1": z0[len(z0) // 2 :],
        "p1.0": z1[: len(z1) // 2],
        "p1.1": z1[len(z1) // 2 :],
    }
    plan = dd.Plan(
        "packpipe",
        "1.0",
        archives=(
            dd.Archive(
                "p0",
                dd.RemoteFile("p0.zip", "https://example.invalid/p0", len(z0), md5=_md5(z0)),
                parts=(
                    dd.RemoteFile("p0.0", "https://example.invalid/p0.0", len(part_blobs["p0.0"])),
                    dd.RemoteFile("p0.1", "https://example.invalid/p0.1", len(part_blobs["p0.1"])),
                ),
            ),
            dd.Archive(
                "p1",
                dd.RemoteFile("p1.zip", "https://example.invalid/p1", len(z1), md5=_md5(z1)),
                parts=(
                    dd.RemoteFile("p1.0", "https://example.invalid/p1.0", len(part_blobs["p1.0"])),
                    dd.RemoteFile("p1.1", "https://example.invalid/p1.1", len(part_blobs["p1.1"])),
                ),
            ),
        ),
    )
    user_cancel = Cancel()
    real_hash_parts = dd._hash_parts

    def spy_hash_parts(paths, rf_, name, cancel=None):
        seen_cancel.append(cancel)
        user_cancel.cancel()
        return real_hash_parts(paths, rf_, name, cancel=cancel)

    def fake_dl(rf_, path, *, emit=None, cancel=None, pool=None, redirect_allowed=None):
        path.parent.mkdir(parents=True, exist_ok=True)
        path.write_bytes(part_blobs[rf_.path])

    monkeypatch.setattr(dd, "_download_remote", fake_dl)
    monkeypatch.setattr(dd, "_hash_parts", spy_hash_parts)
    with pytest.raises(Cancelled):
        dd.sync(plan, tmp_path / "inst", cancel=user_cancel, workers=2)
    assert seen_cancel and seen_cancel[0] is not None


def test_no_spurious_finished_progress_after_cancel(tmp_path, monkeypatch):
    """Cancel arriving right as download finishes must not emit pack_progress.finished()."""
    blobs = {
        "p0.zip": _make_zip({"a.bin": b"a" * 500}),
        "p1.zip": _make_zip({"b.bin": b"b" * 500}),
    }
    plan = _zip_plan(blobs)
    cancel = Cancel()
    events: list[Event] = []

    def fake_dl(rf, path, *, emit=None, cancel=None, pool=None, redirect_allowed=None):
        path.parent.mkdir(parents=True, exist_ok=True)
        path.write_bytes(blobs[rf.path])
        # Simulate user cancel arriving right after download finishes (before finished() runs).
        user_cancel_ref.cancel()

    user_cancel_ref = cancel
    monkeypatch.setattr(dd, "_download_remote", fake_dl)
    with pytest.raises(Cancelled):
        dd.sync(plan, tmp_path, emit=events.append, cancel=cancel, workers=1)

    download_progress = [
        e for e in events
        if e.kind == "progress" and isinstance(e.data, dict) and e.data.get("stage") == "download"
    ]
    assert download_progress == [], f"spurious post-cancel progress emitted: {download_progress}"


def test_pack_space_needed_respects_streamed_barrier_inflight_cap_and_cached_bytes(
    tmp_path, monkeypatch
):
    """_pack_space_needed separates runs across streamed barriers and uses _MAX_INFLIGHT_PACKS."""
    dl_dir = tmp_path / dd.DL_DIR
    dl_dir.mkdir()
    a0 = dd.Archive("p0", dd.RemoteFile("p0.zip", "https://example.invalid/0", 1000))
    streamed = dd.Archive(
        "s1", dd.RemoteFile("s1.zip", "https://example.invalid/s1", 5000), unwrap=("zip",),
        stream=True,
    )
    a1 = dd.Archive("p1", dd.RemoteFile("p1.zip", "https://example.invalid/1", 1100))
    a2 = dd.Archive("p2", dd.RemoteFile("p2.zip", "https://example.invalid/2", 1200))

    # Streamed archive drains in-flight packs, so p0 and p2 are never on disk together; each
    # still unpacks into a staging copy of its own size (#1854 item 6).
    assert dd._pack_space_needed([a0, streamed, a2], dl_dir) == 2400

    # Contiguous non-streamed packs use a sliding window of _MAX_INFLIGHT_PACKS, plus the
    # staging copy of the earlier pack of each window while it unpacks.
    assert dd._pack_space_needed([a0, a1, a2], dl_dir) == 3400
    with monkeypatch.context() as mp:
        mp.setattr(dd, "_MAX_INFLIGHT_PACKS", 3)
        assert dd._pack_space_needed([a0, a1, a2], dl_dir) == 4300

    # Cached verified file and partial .part bytes are subtracted; a pack already on disk
    # still needs its staging copy when it unpacks, and so does the last pack (#1854 item 6).
    (dl_dir / "p0.zip").write_bytes(b"x" * 1000)
    (dl_dir / "p1.zip.part").write_bytes(b"y" * 550)
    assert dd._pack_space_needed([a0, a1], dl_dir) == 1650


def test_pack_space_needed_counts_the_staging_copy_of_a_large_last_pack(tmp_path):
    dl_dir = tmp_path / dd.DL_DIR
    dl_dir.mkdir()
    small = [dd.Archive(f"p{i}", dd.RemoteFile(f"p{i}.zip", f"https://example.invalid/{i}", 10))
             for i in range(2)]
    big = dd.Archive("big", dd.RemoteFile("big.zip", "https://example.invalid/big", 1000))
    # While the 1000-byte pack unpacks alone, it and its unpacked copy are both on disk.
    assert dd._pack_space_needed([*small, big], dl_dir) == 2000


def test_single_pack_checks_space_once_and_uses_sync_archive(tmp_path, monkeypatch):
    """Single-pack installs do not double-check disk space and route through _sync_archive."""
    body = _make_zip({"a.bin": b"hello" * 200})
    plan = _zip_plan({"p0.zip": body})
    space_calls: list[int] = []
    sync_archive_calls = 0
    real_check = diskspace.check_space
    real_sync_archive = dd._sync_archive

    def spy_check(path, needed, margin=diskspace.DEFAULT_MARGIN):
        space_calls.append(needed)
        return real_check(path, needed, margin=margin)

    def spy_sync_archive(*a, **kw):
        nonlocal sync_archive_calls
        sync_archive_calls += 1
        return real_sync_archive(*a, **kw)

    def fake_dl(url, destination, **kw):
        destination.parent.mkdir(parents=True, exist_ok=True)
        destination.write_bytes(body)
        return dl.DownloadResult(destination, len(body), "", "", md5=_md5(body))

    monkeypatch.setattr(diskspace, "check_space", spy_check)
    monkeypatch.setattr(dd, "_sync_archive", spy_sync_archive)
    monkeypatch.setattr(dd, "download", fake_dl)

    dd.sync(plan, tmp_path, workers=2)
    assert len(space_calls) == 1, f"expected 1 check_space call on single pack, got {space_calls}"
    assert sync_archive_calls == 1


def test_streamed_and_mixed_plans_do_not_emit_synthetic_download_progress(tmp_path):
    """Streamed archives emit stage='install' only; mixed plans keep download total accurate."""
    z0 = _make_zip({"f0.bin": b"zero" * 200})
    z1 = _make_zip({"f1.bin": b"one" * 300})
    z2 = _make_zip({"f2.bin": b"two" * 200})
    srv = _KeepAliveServer({"/p0.zip": z0, "/p1.zip": z1, "/p2.zip": z2})
    try:
        # Streamed-only 2-pack plan: ZERO stage="download" events.
        stream_plan = dd.Plan(
            "packpipe",
            "1.0",
            archives=(
                dd.Archive(
                    "p0", dd.RemoteFile("p0.zip", srv.url + "/p0.zip", len(z0)), stream=True
                ),
                dd.Archive(
                    "p1", dd.RemoteFile("p1.zip", srv.url + "/p1.zip", len(z1)), stream=True
                ),
            ),
        )
        stream_events: list[Event] = []
        dd.sync(stream_plan, tmp_path / "streamed", emit=stream_events.append, workers=2)
        dl_prog = [
            e for e in stream_events
            if e.kind == "progress" and isinstance(e.data, dict)
            and e.data.get("stage") == "download"
        ]
        assert dl_prog == [], f"unexpected synthetic download progress on streamed plan: {dl_prog}"
        assert (tmp_path / "streamed" / "f0.bin").read_bytes() == b"zero" * 200
        assert (tmp_path / "streamed" / "f1.bin").read_bytes() == b"one" * 300

        # Mixed [non-streamed, streamed, non-streamed] plan: download bar counts non-streamed only.
        mixed_plan = dd.Plan(
            "packpipe",
            "1.0",
            archives=(
                dd.Archive(
                    "p0",
                    dd.RemoteFile("p0.zip", srv.url + "/p0.zip", len(z0), md5=_md5(z0)),
                ),
                dd.Archive(
                    "p1",
                    dd.RemoteFile("p1.zip", srv.url + "/p1.zip", len(z1)),
                    stream=True,
                ),
                dd.Archive(
                    "p2",
                    dd.RemoteFile("p2.zip", srv.url + "/p2.zip", len(z2), md5=_md5(z2)),
                ),
            ),
        )
        mixed_events: list[Event] = []
        dd.sync(mixed_plan, tmp_path / "mixed", emit=mixed_events.append, workers=2)
        mixed_dl = [
            e.data for e in mixed_events
            if e.kind == "progress" and isinstance(e.data, dict)
            and e.data.get("stage") == "download"
        ]
        assert mixed_dl
        assert {d["total"] for d in mixed_dl} == {len(z0) + len(z2)}
        assert mixed_dl[-1]["done"] == len(z0) + len(z2)
        assert (tmp_path / "mixed" / "f0.bin").read_bytes() == b"zero" * 200
        assert (tmp_path / "mixed" / "f1.bin").read_bytes() == b"one" * 300
        assert (tmp_path / "mixed" / "f2.bin").read_bytes() == b"two" * 200
    finally:
        srv.close()


# ---------------------------------------------------------------------------
# Fix round 2 tests (NEON-REVIEW-2.md items 1-6)
# ---------------------------------------------------------------------------


def test_sync_archive_multipart_fallback_releases_shared_pool_sockets(tmp_path, monkeypatch):
    """Multi-part fallback in _sync_archive with a shared pool releases/closes worker sockets."""
    z0 = _make_zip({"f0.bin": b"alpha" * 1000})
    z1 = _make_zip({"f1.bin": b"beta" * 1000})

    def split4(blob: bytes) -> list[bytes]:
        q = len(blob) // 4
        return [blob[:q], blob[q : q * 2], blob[q * 2 : q * 3], blob[q * 3 :]]

    s0_parts = split4(z0)
    s1_parts = split4(z1)
    routes = {
        **{f"/s0.{i}": p for i, p in enumerate(s0_parts)},
        **{f"/s1.{i}": p for i, p in enumerate(s1_parts)},
    }
    # Server ignores Range -> streamed archives fall back to _sync_archive's multi-part executor.
    srv = _KeepAliveServer(routes, support_range=False)
    peak_by_pool: dict[int, int] = {}
    new_conns_by_pool: dict[int, int] = {}
    unreleased_after_sync_archive: list[int] = []
    shared_pools: list[dl.HttpClientPool] = []
    real_conn = dl.HttpClientPool._connection
    real_sync_archive = dd._sync_archive

    def tracking_connection(self, *args, **kwargs):
        conn = real_conn(self, *args, **kwargs)
        pid = id(self)
        with self._lock:
            if getattr(conn, "sock", None) is None:
                new_conns_by_pool[pid] = new_conns_by_pool.get(pid, 0) + 1
            peak_by_pool[pid] = max(peak_by_pool.get(pid, 0), len(self._all))
        return conn

    def spy_sync_archive(*args, **kwargs):
        pool = kwargs.get("pool")
        if pool is not None and pool not in shared_pools:
            shared_pools.append(pool)
        try:
            return real_sync_archive(*args, **kwargs)
        finally:
            if pool is not None:
                with pool._lock:
                    idle_count = sum(len(v) for v in pool._idle.values())
                    total_count = len(pool._all)
                unreleased_after_sync_archive.append(total_count - idle_count)

    try:
        monkeypatch.setattr(dl.HttpClientPool, "_connection", tracking_connection)
        monkeypatch.setattr(dd, "_sync_archive", spy_sync_archive)

        def mk_streamed_multipart(name: str, blob: bytes, pieces: list[bytes]) -> dd.Archive:
            parts = tuple(
                dd.RemoteFile(f"{name}.{i}", srv.url + f"/{name}.{i}", len(piece))
                for i, piece in enumerate(pieces)
            )
            return dd.Archive(
                name,
                dd.RemoteFile(f"{name}.zip", srv.url + f"/{name}.0", len(blob), md5=_md5(blob)),
                parts=parts,
                stream=True,
            )

        plan = dd.Plan(
            "packpipe",
            "1.0",
            archives=(
                mk_streamed_multipart("s0", z0, s0_parts),
                mk_streamed_multipart("s1", z1, s1_parts),
            ),
        )
        dd.sync(plan, tmp_path / "ok", workers=4)

        assert len(shared_pools) == 1
        shared_id = id(shared_pools[0])
        assert unreleased_after_sync_archive == [0, 0], (
            f"sockets trapped in dead worker threads after _sync_archive: "
            f"{unreleased_after_sync_archive}"
        )
        assert new_conns_by_pool[shared_id] <= 4, (
            f"expected <=4 sockets opened in shared pool across two 4-part fallbacks, "
            f"opened {new_conns_by_pool[shared_id]}"
        )
        # 2 Range probes from _stream_zip_archive + <=4 shared-pool sockets (reused across s1).
        assert srv.connections <= 6, (
            f"expected <=6 total connections (2 probes + <=4 pooled), got {srv.connections}"
        )
        assert peak_by_pool[shared_id] <= 4, (
            f"shared pool leaked sockets: peak_all={peak_by_pool[shared_id]}"
        )
        assert shared_pools[0]._all == []
        assert (tmp_path / "ok" / "f0.bin").read_bytes() == b"alpha" * 1000
        assert (tmp_path / "ok" / "f1.bin").read_bytes() == b"beta" * 1000

        # Error path: a failing part closes (does not recycle) its worker socket in shared_pool.
        shared_pool = dl.HttpClientPool()
        try:
            bad_archive = dd.Archive(
                "bad",
                dd.RemoteFile("bad.zip", srv.url + "/s0.0", len(z0)),
                parts=(
                    dd.RemoteFile(
                        "s0.0", srv.url + "/s0.0", len(s0_parts[0]), md5="0" * 32
                    ),
                ),
            )
            bad_plan = dd.Plan("packpipe", "1.0", archives=(bad_archive,))
            with pytest.raises(dd.DirectError, match="hash mismatch"):
                dd._sync_archive(
                    bad_plan,
                    bad_archive,
                    tmp_path / "err",
                    emit=None,
                    cancel=None,
                    workers=2,
                    pool=shared_pool,
                )
            with shared_pool._lock:
                idle_count = sum(len(v) for v in shared_pool._idle.values())
                assert shared_pool._all == [] and idle_count == 0, (
                    f"failed multi-part worker kept or leaked socket: "
                    f"all={len(shared_pool._all)} idle={idle_count}"
                )
        finally:
            shared_pool.close()
    finally:
        srv.close()


def test_streamed_archive_fallback_keeps_progress_monotonic_within_total_in_mixed_plan(
    tmp_path, monkeypatch
):
    """When a streamed archive falls back to full download, progress stays monotonic <= total."""
    z0 = _make_zip({"f0.bin": b"zero" * 4000})
    z1 = _make_zip({"f1.bin": b"one" * 5000})
    z2 = _make_zip({"f2.bin": b"two" * 4000})
    # Server ignores Range -> streamed archives fall back to full download.
    srv = _KeepAliveServer({"/p0.zip": z0, "/s1.zip": z1, "/p2.zip": z2}, support_range=False)
    try:
        # Small chunk size ensures multiple progress events per archive so backwards jumps show up.
        real_download = dd.download
        monkeypatch.setattr(dd, "download", lambda *a, **kw: real_download(*a, chunk=4096, **kw))

        mixed_plan = dd.Plan(
            "packpipe",
            "1.0",
            archives=(
                dd.Archive(
                    "p0",
                    dd.RemoteFile("p0.zip", srv.url + "/p0.zip", len(z0), md5=_md5(z0)),
                ),
                dd.Archive(
                    "s1",
                    dd.RemoteFile("s1.zip", srv.url + "/s1.zip", len(z1)),
                    stream=True,
                ),
                dd.Archive(
                    "p2",
                    dd.RemoteFile("p2.zip", srv.url + "/p2.zip", len(z2), md5=_md5(z2)),
                ),
            ),
        )
        events: list[Event] = []
        dd.sync(mixed_plan, tmp_path / "mixed", emit=events.append, workers=2)
        dl_events = [
            e
            for e in events
            if e.kind == "progress"
            and isinstance(e.data, dict)
            and e.data.get("stage") == "download"
        ]
        assert dl_events
        dones = [e.data["done"] for e in dl_events]
        fractions = [e.progress for e in dl_events]
        assert dones == sorted(dones), f"mixed fallback progress went backwards: {dones}"
        assert fractions == sorted(fractions), (
            f"mixed fallback fraction went backwards: {fractions}"
        )
        assert all(0 <= e.data["done"] <= e.data["total"] for e in dl_events)
        assert all(0.0 <= e.progress <= 1.0 for e in dl_events)
        assert dl_events[-1].data["done"] == dl_events[-1].data["total"]
        assert dl_events[-1].progress == 1.0
        assert (tmp_path / "mixed" / "f0.bin").read_bytes() == b"zero" * 4000
        assert (tmp_path / "mixed" / "f1.bin").read_bytes() == b"one" * 5000
        assert (tmp_path / "mixed" / "f2.bin").read_bytes() == b"two" * 4000

        # Streamed-only plan where both streamed archives fall back to full download.
        both_streamed = dd.Plan(
            "packpipe",
            "1.0",
            archives=(
                dd.Archive(
                    "s0",
                    dd.RemoteFile("p0.zip", srv.url + "/p0.zip", len(z0)),
                    stream=True,
                ),
                dd.Archive(
                    "s1",
                    dd.RemoteFile("s1.zip", srv.url + "/s1.zip", len(z1)),
                    stream=True,
                ),
            ),
        )
        s_events: list[Event] = []
        dd.sync(both_streamed, tmp_path / "both_streamed", emit=s_events.append, workers=2)
        s_dl = [
            e
            for e in s_events
            if e.kind == "progress"
            and isinstance(e.data, dict)
            and e.data.get("stage") == "download"
        ]
        assert s_dl
        s_dones = [e.data["done"] for e in s_dl]
        s_fracs = [e.progress for e in s_dl]
        assert s_dones == sorted(s_dones), f"streamed fallback went backwards: {s_dones}"
        assert s_fracs == sorted(s_fracs), f"streamed fallback fraction went backwards: {s_fracs}"
        assert all(0 <= e.data["done"] <= e.data["total"] for e in s_dl)
        assert s_dl[-1].data["done"] == s_dl[-1].data["total"] == len(z0) + len(z1)
    finally:
        srv.close()


def test_background_pack_verify_step_does_not_replace_foreground_header(tmp_path, monkeypatch):
    """Pack N+1's background 'Checking the download…' step must not overwrite Pack N's header."""
    payload = bytes(range(256)) * 128  # 32 KiB
    z0 = _make_zip({"f0.bin": payload})
    z1 = _make_zip({"f1.bin": payload})
    z2 = _make_zip({"f2.bin": payload})
    p0_in_unpack = threading.Event()
    p1_verified = threading.Event()
    p2_foreground = threading.Event()
    events: list[Event] = []
    real_unwrap = dd._unwrap

    def gate_requests(path: str, range_hdr: str | None) -> None:
        if path == "/p1.zip" and range_hdr != "bytes=0-0":
            # Keep Pack 1's range transfers until Pack 0 is actually unpacking, so Pack 1's
            # progress + 'Checking the download…' deterministically land during Pack 0's unpack.
            p0_in_unpack.wait(timeout=5.0)
        elif path == "/p2.zip" and range_hdr != "bytes=0-0":
            # Hold Pack 2 until it becomes the foreground pack so its own verify step must show.
            p2_foreground.wait(timeout=5.0)

    srv = _KeepAliveServer(
        {"/p0.zip": z0, "/p1.zip": z1, "/p2.zip": z2}, before_get=gate_requests
    )

    def record_event(ev: Event) -> None:
        events.append(ev)
        if ev.kind == "log" and "verified p1.zip" in (ev.message or ""):
            p1_verified.set()
        if ev.kind == "step" and ev.message == "installing pack 3 of 3: p2.zip":
            p2_foreground.set()

    def held_unwrap(archive, source, dest, dl_dir, cancel=None, changed=None):
        if archive.name == "p0.zip":
            p0_in_unpack.set()
            # Hold Pack 0 inside unpack until Pack 1 has finished its ranged download and join/hash
            # (where _download_ranges emits Event("step", "Checking the download…")).
            assert p1_verified.wait(timeout=5.0), "p1 did not finish verifying while p0 unpacked"
        return real_unwrap(archive, source, dest, dl_dir, cancel)

    try:
        monkeypatch.setattr(dl, "RANGE_THRESHOLD", 4096)
        monkeypatch.setattr(dd, "_unwrap", held_unwrap)
        plan = _zip_plan({"p0.zip": z0, "p1.zip": z1, "p2.zip": z2}, base_url=srv.url)
        dd.sync(plan, tmp_path, emit=record_event, workers=4)

        # Verify ranged path really ran for all packs.
        for path in ("/p0.zip", "/p1.zip", "/p2.zip"):
            assert any(p == path and r == "bytes=0-0" for p, r in srv.requests)

        step_messages = [e.message for e in events if e.kind == "step"]
        unpack0_idx = step_messages.index("Unpacking p0.zip…")
        install2_idx = step_messages.index("installing pack 2 of 3: p1.zip")
        install3_idx = step_messages.index("installing pack 3 of 3: p2.zip")
        unpack2_idx = step_messages.index("Unpacking p2.zip…")
        # Foreground Pack 0 DID forward its own verify step before unpacking.
        assert "Checking the download…" in step_messages[:unpack0_idx], (
            f"foreground pack 0 verify step missing: {step_messages}"
        )
        # Background Pack 1's verify step must NOT appear while Pack 0 is unpacking.
        between = step_messages[unpack0_idx + 1 : install2_idx]
        assert "Checking the download…" not in between, (
            f"background pack 1 step overwrote pack 0 header: {between}"
        )
        # Once Pack 2 becomes foreground, its own verify step IS forwarded.
        p2_steps = step_messages[install3_idx + 1 : unpack2_idx]
        assert "Checking the download…" in p2_steps, (
            f"foreground pack 2 verify step missing after foreground switch: {step_messages}"
        )

        # Progress from BOTH packs still flowed (and advanced past len(z0) while p0 unpacked).
        unpack0_ev_idx = next(
            i for i, e in enumerate(events) if e.kind == "step" and e.message == "Unpacking p0.zip…"
        )
        install2_ev_idx = next(
            i
            for i, e in enumerate(events)
            if e.kind == "step" and e.message == "installing pack 2 of 3: p1.zip"
        )
        dl_during_unpack0 = [
            e.data["done"]
            for e in events[unpack0_ev_idx:install2_ev_idx]
            if e.kind == "progress"
            and isinstance(e.data, dict)
            and e.data.get("stage") == "download"
        ]
        assert any(d > len(z0) for d in dl_during_unpack0), (
            f"pack 1 download progress did not advance while pack 0 unpacked: {dl_during_unpack0}"
        )
        all_dl_dones = [
            e.data["done"]
            for e in events
            if e.kind == "progress"
            and isinstance(e.data, dict)
            and e.data.get("stage") == "download"
        ]
        assert all_dl_dones[-1] == len(z0) + len(z1) + len(z2)

        # Feed the exact event stream through GameView.append_event (what the GUI sees).
        os.environ.setdefault("QT_QPA_PLATFORM", "offscreen")
        from PyQt6.QtWidgets import QApplication

        from nadare.gui.game_view import GameView, breakable

        app = QApplication.instance() or QApplication([])
        page = GameView()
        try:
            page.set_profile("sample", {"backend": "direct-strinova", "title": "Sample"})
            page.set_busy("install")
            in_unpack0 = False
            for ev in events:
                if ev.kind == "step" and ev.message == "installing pack 2 of 3: p1.zip":
                    in_unpack0 = False
                page.append_event(ev)
                if ev.kind == "step" and ev.message == "Unpacking p0.zip…":
                    in_unpack0 = True
                if in_unpack0:
                    assert page._operation_detail == breakable("Unpacking p0.zip…"), (
                        f"GUI header replaced during p0 unpack: {page._operation_detail!r}"
                    )
        finally:
            page._timer.stop()
            page.deleteLater()
            app.processEvents()
    finally:
        srv.close()


def test_http_client_pool_retry_preserves_healthy_idle_siblings_and_opened_flag(monkeypatch):
    """Pool retry drops only the failed socket, keeps healthy idle siblings, and tracks opened."""
    srv = _KeepAliveServer({"/ok": b"hello"})
    try:
        pool = dl.HttpClientPool()
        try:
            url = srv.url + "/ok"
            key = ("http", "127.0.0.1", srv._server.server_port)

            # Worker thread establishes a healthy keep-alive socket and releases it to _idle.
            def prime_idle() -> None:
                with pool.open(url) as resp:
                    assert resp.read() == b"hello"
                pool.release_thread(reuse=True)

            worker = threading.Thread(target=prime_idle)
            worker.start()
            worker.join(timeout=5)
            with pool._lock:
                assert len(pool._idle.get(key, [])) == 1
                healthy_sibling = pool._idle[key][0]
            assert healthy_sibling.sock is not None

            # Main thread has a stale socket in _local whose request fails on first try.
            class _StaleConn:
                sock = object()

                def __init__(self) -> None:
                    self.closed = False

                def request(self, *args, **kwargs):
                    raise http.client.RemoteDisconnected("peer closed")

                def close(self) -> None:
                    self.closed = True

            stale = _StaleConn()
            pool._store()[key] = stale
            with pool._lock:
                pool._all.append(stale)

            with dl.transfer.measure(2, None):
                with pool.open(url) as resp:
                    assert resp.read() == b"hello"
                opened_count = dl.current_transfer().opened

            assert stale.closed is True
            assert opened_count == 1, (
                f"expected retry to report 1 opened socket, got {opened_count}"
            )
            # Healthy sibling remained untouched and open in _idle!
            with pool._lock:
                assert healthy_sibling in pool._idle.get(key, []), (
                    "retry dropped healthy sibling from _idle"
                )
            assert healthy_sibling.sock is not None, "retry closed healthy sibling socket"
            assert pool._store().get(key) is not healthy_sibling

            # If retry receives an already-connected socket (sock not None), opened is False.
            class _WarmRetryConn:
                def __init__(self, real_sock) -> None:
                    self.sock = real_sock
                    self.calls = 0

                def request(self, *args, **kwargs):
                    self.calls += 1
                    if self.calls == 1:
                        raise http.client.RemoteDisconnected("stale")

                def getresponse(self):
                    class _Resp:
                        status = 200
                        headers = {"Content-Length": "2"}

                        def __init__(self) -> None:
                            self._done = False

                        def read(self, n=-1):
                            if self._done:
                                return b""
                            self._done = True
                            return b"ok"

                        def isclosed(self):
                            return self._done

                        def getheader(self, name, default=None):
                            return self.headers.get(name, default)

                        def close(self):
                            self._done = True

                    return _Resp()

                def close(self):
                    pass

            warm_pool = dl.HttpClientPool()
            try:
                warm_conn = _WarmRetryConn(object())
                monkeypatch.setattr(warm_pool, "_connection", lambda *a, **k: warm_conn)
                with dl.transfer.measure(2, None):
                    with warm_pool.open("http://127.0.0.1:9/x") as resp:
                        assert resp.read() == b"ok"
                    warm_opened = dl.current_transfer().opened
                assert warm_opened == 0, (
                    f"opened reported True for an already-connected retry socket: {warm_opened}"
                )
            finally:
                warm_pool.close()
        finally:
            pool.close()
    finally:
        srv.close()


def test_multipart_two_inflight_packs_do_not_oversubscribe_range_workers(tmp_path, monkeypatch):
    """concurrent_files accounts for parts across 2 in-flight packs so range_workers <= workers."""
    z = _make_zip({"a.bin": b"abc" * 200})
    p_a, p_b = z[: len(z) // 2], z[len(z) // 2 :]

    def mk_multipart(name: str) -> dd.Archive:
        return dd.Archive(
            name,
            dd.RemoteFile(f"{name}.zip", f"https://example.invalid/{name}", len(z), md5=_md5(z)),
            parts=(
                dd.RemoteFile(f"{name}.0", f"https://example.invalid/{name}.0", len(p_a)),
                dd.RemoteFile(f"{name}.1", f"https://example.invalid/{name}.1", len(p_b)),
            ),
        )

    def observe_range_workers(archives: tuple[dd.Archive, ...], workers: int) -> int:
        seen: list[int] = []
        plan = dd.Plan("packpipe", "1.0", archives=archives)

        def fake_dl(rf, path, *, emit=None, cancel=None, pool=None, redirect_allowed=None):
            m = dl.current_transfer()
            assert m is not None
            seen.append(m.range_workers)
            path.parent.mkdir(parents=True, exist_ok=True)
            if rf.path.endswith(".0"):
                path.write_bytes(p_a)
            elif rf.path.endswith(".1"):
                path.write_bytes(p_b)
            else:
                path.write_bytes(z)

        monkeypatch.setattr(dd, "_download_remote", fake_dl)
        dd.sync(plan, tmp_path / f"w{workers}_{len(archives)}_{archives[0].name}", workers=workers)
        assert seen
        return seen[0]

    # 2 multi-part packs x 2 parts = 4 concurrent parts -> with workers=4, range_workers MUST be 1.
    assert observe_range_workers((mk_multipart("a0"), mk_multipart("a1")), workers=4) == 1
    # Same 4 concurrent parts with workers=8 -> range_workers == 2 (4 * 2 == 8).
    assert observe_range_workers((mk_multipart("b0"), mk_multipart("b1")), workers=8) == 2
    # Single 2-part pack with workers=4 -> range_workers == 2.
    assert observe_range_workers((mk_multipart("c0"),), workers=4) == 2
    # 1 single-source + 1 two-part pack = 3 concurrent tasks -> workers=4 gives range_workers=1.
    single = dd.Archive(
        "d0", dd.RemoteFile("d0.zip", "https://example.invalid/d0.zip", len(z), md5=_md5(z))
    )
    assert observe_range_workers((single, mk_multipart("d1")), workers=4) == 1
    # Streamed barrier separates two 2-part packs so only 2 parts run at once -> range_workers == 2.
    streamed = dd.Archive(
        "s1",
        dd.RemoteFile("s1.zip", "https://example.invalid/s1.zip", len(z)),
        stream=True,
    )
    monkeypatch.setattr(dd, "_stream_zip_archive", lambda *a, **k: None)
    assert observe_range_workers((mk_multipart("e0"), streamed, mk_multipart("e2")), workers=4) == 2


def test_single_pack_install_keeps_pack_progress_wrapping(tmp_path, monkeypatch):
    """1-pack installs still wrap emit in _PackProgress (normalizes total, guards retries)."""
    z0 = _make_zip({"a.bin": b"hello" * 200})
    planned = len(z0)
    plan = _zip_plan({"p0.zip": z0})

    # Case 1: Server omits Content-Length (total=-1) and reports a mismatched total, then retries
    # (done drops backwards on refetch_clean=True). _PackProgress normalizes and stays monotonic.
    def fake_dl(rf, path, *, emit=None, cancel=None, pool=None, redirect_allowed=None):
        assert emit is not None
        half = planned // 2
        # Missing Content-Length (total=-1) -> normalized to planned size.
        emit(
            Event(
                "progress",
                f"download: {half}",
                progress=None,
                data={"stage": "download", "done": half, "total": -1},
            )
        )
        # Simulated clean retry (done restarts below previous high-water mark) -> must not go back.
        emit(
            Event(
                "progress",
                f"download: {half // 2} / {planned * 2}",
                progress=(half // 2) / (planned * 2),
                data={"stage": "download", "done": half // 2, "total": planned * 2},
            )
        )
        # Mismatched Content-Length (2x planned) -> scaled to planned size.
        emit(
            Event(
                "progress",
                f"download: {planned * 2} / {planned * 2}",
                progress=1.0,
                data={"stage": "download", "done": planned * 2, "total": planned * 2},
            )
        )
        path.parent.mkdir(parents=True, exist_ok=True)
        path.write_bytes(z0)

    monkeypatch.setattr(dd, "_download_remote", fake_dl)
    events: list[Event] = []
    dd.sync(plan, tmp_path, emit=events.append, workers=2)

    dl_events = [
        e
        for e in events
        if e.kind == "progress"
        and isinstance(e.data, dict)
        and e.data.get("stage") == "download"
    ]
    assert dl_events
    dones = [e.data["done"] for e in dl_events]
    totals = {e.data["total"] for e in dl_events}
    assert totals == {planned}, f"expected normalized total {planned}, got {totals}"
    assert dones == sorted(dones), f"1-pack retry went backwards: {dones}"
    assert dones == [planned // 2, planned], f"unexpected 1-pack progress sequence: {dones}"
    assert all(e.progress is not None and 0.0 <= e.progress <= 1.0 for e in dl_events)
    assert dl_events[-1].progress == 1.0


# --- 2026-11-26 night scrutiny follow-up: correctness, accounting, hardening ---------------


def _tree_bytes(root: Path) -> int:
    total = 0
    for dirpath, _dirnames, filenames in os.walk(root):
        for name in filenames:
            try:
                total += os.stat(os.path.join(dirpath, name)).st_size
            except OSError:
                pass
    return total


def test_resumed_sync_keeps_the_later_packs_shared_member(tmp_path):
    """A pack installed on resume must not undo a later pack already on disk.

    Packs are applied in plan order, so a later pack's copy of a shared path wins. Skipping
    that later pack as already done must not change the installed tree
    (sim-pack finding F2, ``repro_shared.py``)."""
    pack_a = _make_zip({"shared.bin": b"A" * 10, "only-a.bin": b"a"})
    pack_b = _make_zip({"shared.bin": b"B" * 20, "only-b.bin": b"b"})
    srv = _KeepAliveServer({"/a.zip": pack_a, "/b.zip": pack_b})
    try:
        archive_a = dd.Archive(
            "a", dd.RemoteFile("a.zip", f"{srv.url}/a.zip", len(pack_a), md5=_md5(pack_a))
        )
        archive_b = dd.Archive(
            "b", dd.RemoteFile("b.zip", f"{srv.url}/b.zip", len(pack_b), md5=_md5(pack_b))
        )
        plan = dd.Plan("packpipe", "1.0", archives=(archive_a, archive_b))

        # An earlier sync installed only pack b (a stop before a, as repro_shared.py sets up).
        dd.sync(dd.Plan("packpipe", "1.0", archives=(archive_b,)), tmp_path, workers=1)
        assert (tmp_path / "shared.bin").read_bytes() == b"B" * 20

        summary = dd.sync(plan, tmp_path, workers=2)
        assert summary == {"downloaded": 1, "skipped": 1}
        assert (tmp_path / "only-a.bin").read_bytes() == b"a"
        assert (tmp_path / "only-b.bin").read_bytes() == b"b"
        assert (tmp_path / "shared.bin").read_bytes() == b"B" * 20, (
            "the earlier pack overwrote the later pack's shared member"
        )

        # Pack a's receipt must not claim a file it did not install, or every later sync
        # would reinstall it forever.
        again = dd.sync(plan, tmp_path, workers=2)
        assert again == {"downloaded": 0, "skipped": 2}
        assert (tmp_path / "shared.bin").read_bytes() == b"B" * 20
    finally:
        srv.close()


def test_fetched_bytes_counts_only_what_was_really_downloaded(tmp_path):
    """A verified pack reused from .nadare-dl after a stop was not fetched now (#1830)."""
    pack_a = _make_zip({"a.bin": b"a" * 100})
    pack_b = _make_zip({"b.bin": b"b" * 200})
    srv = Server({"/a.zip": pack_a, "/b.zip": pack_b}, keepalive=True)
    try:
        plan = dd.Plan(
            "packpipe",
            "1.0",
            archives=(
                dd.Archive(
                    "a", dd.RemoteFile("a.zip", f"{srv.url}/a.zip", len(pack_a), md5=_md5(pack_a))
                ),
                dd.Archive(
                    "b", dd.RemoteFile("b.zip", f"{srv.url}/b.zip", len(pack_b), md5=_md5(pack_b))
                ),
            ),
        )
        (tmp_path / dd.DL_DIR).mkdir()
        # What a stop between download and unpack leaves behind.
        (tmp_path / dd.DL_DIR / "a.zip").write_bytes(pack_a)

        summary = dd.sync(plan, tmp_path, workers=2)

        assert "/a.zip" not in srv.requests, "pack a was expected to come from the cache"
        assert srv.bytes_served == len(pack_b)
        assert summary.fetched_bytes == len(pack_b), (
            f"fetched_bytes {summary.fetched_bytes} != {len(pack_b)} really transferred"
        )
    finally:
        srv.close()


def test_derived_cache_names_and_prefix_collisions_are_refused(tmp_path):
    """Plan validation reserves the cache names Nadare derives from a declared path.

    ``download()`` derives ``<path>.part``, ``<path>.nadare-new`` and range pieces
    ``<path>.part-N`` / ``<path>.part-N.done`` / ``<path>.part.ranges.json``; staging folders
    (``extract-*``, ``slices-*``, ``outer-*``) and ``stream`` are Nadare's own (SEC F1,
    Neon 1). Two packs whose names collide only through those derived names used to pass
    validation and then fail or mix bytes."""

    def archive(name: str, path: str) -> dd.Archive:
        return dd.Archive(name, dd.RemoteFile(path, f"https://example.invalid/{path}", 10))

    def refused(first: str, second: str) -> None:
        plan = dd.Plan(
            "packpipe", "1.0", archives=(archive("p0", first), archive("p1", second))
        )
        with pytest.raises(dd.DirectError, match="clash"):
            dd._validate_plan(plan)

    refused("a.zip", "a.zip.part")
    refused("a.zip", "a.zip.nadare-new")
    refused("a.zip", "a.zip.part-0")
    refused("a.zip", "a.zip.part-0.done")
    refused("a.zip", "a.zip.part.ranges.json")
    # A file and a folder of the same name cannot both exist in .nadare-dl.
    refused("dir", "dir/a.zip")
    refused("a.zip", "a.zip/sub.zip")
    # The same collision between two parts of one archive.
    in_one = dd.Plan(
        "packpipe",
        "1.0",
        archives=(
            dd.Archive(
                "p0",
                dd.RemoteFile("a.zip", "https://example.invalid/a", 10),
                parts=(
                    dd.RemoteFile("chunk.0", "https://example.invalid/b", 10),
                    dd.RemoteFile("chunk.0.part", "https://example.invalid/c", 10),
                ),
            ),
        ),
    )
    with pytest.raises(dd.DirectError, match="clash"):
        dd._validate_plan(in_one)

    # Nadare's own folders under .nadare-dl are not a publisher's to use.
    for reserved in ("extract-tmp/x.zip", "slices-tmp/x.zip", "outer-tmp/x.zip", "stream/x.zip"):
        plan = dd.Plan("packpipe", "1.0", archives=(archive("p0", reserved),))
        with pytest.raises(dd.DirectError, match="Nadare keeps"):
            dd._validate_plan(plan)

    # Two distinct names still validate.
    dd._validate_plan(
        dd.Plan("packpipe", "1.0", archives=(archive("p0", "a.zip"), archive("p1", "b.zip")))
    )


def test_symlink_planted_before_a_download_is_refused(tmp_path, monkeypatch):
    """A link planted under .nadare-dl after planning must not take a write outside (SEC F3)."""
    body = _make_zip({"a.bin": b"A" * 10})
    srv = _KeepAliveServer({"/p0.zip": body})
    outside = tmp_path / "outside"
    outside.mkdir()
    try:
        plan = dd.Plan(
            "packpipe",
            "1.0",
            archives=(
                dd.Archive(
                    "p0",
                    dd.RemoteFile(
                        "sub/p0.zip", f"{srv.url}/p0.zip", len(body), md5=_md5(body)
                    ),
                ),
            ),
        )
        real = dd._download_remote

        def planting(rf, path, *, emit=None, cancel=None, pool=None, redirect_allowed=None):
            sub = tmp_path / dd.DL_DIR / "sub"
            if not sub.exists():
                sub.symlink_to(outside)
            return real(
                rf, path, emit=emit, cancel=cancel, pool=pool,
                redirect_allowed=redirect_allowed,
            )

        monkeypatch.setattr(dd, "_download_remote", planting)
        with pytest.raises(dd.DirectError, match="symlink"):
            dd.sync(plan, tmp_path, workers=1)
    finally:
        srv.close()

    assert list(outside.iterdir()) == [], "the download wrote through a planted symlink"
    assert not (tmp_path / "a.bin").exists()


def test_symlink_planted_before_a_cache_unlink_cannot_delete_outside(tmp_path, monkeypatch):
    """A link planted after the download must not let the cache cleanup delete outside (F3)."""
    body = _make_zip({"a.bin": b"A" * 10})
    srv = _KeepAliveServer({"/p0.zip": body})
    outside = tmp_path / "outside"
    outside.mkdir()
    decoy = outside / "p0.zip"
    decoy.write_bytes(b"the user's own file, not Nadare's")
    try:
        plan = dd.Plan(
            "packpipe",
            "1.0",
            archives=(
                dd.Archive(
                    "p0",
                    dd.RemoteFile(
                        "sub/p0.zip", f"{srv.url}/p0.zip", len(body), md5=_md5(body)
                    ),
                ),
            ),
        )
        real = dd._download_remote
        dl_dir = tmp_path / dd.DL_DIR

        def planting(rf, path, *, emit=None, cancel=None, pool=None, redirect_allowed=None):
            result = real(
                rf, path, emit=emit, cancel=cancel, pool=pool,
                redirect_allowed=redirect_allowed,
            )
            sub = dl_dir / "sub"
            os.rename(sub, Path(str(sub) + ".real"))
            sub.symlink_to(outside)
            return result

        monkeypatch.setattr(dd, "_download_remote", planting)
        with pytest.raises(dd.DirectError, match="symlink"):
            dd.sync(plan, tmp_path, workers=1)
    finally:
        srv.close()

    assert decoy.exists(), "a cache cleanup unlinked a file outside the install"
    assert not (tmp_path / "a.bin").exists()


def test_cache_cleanup_does_not_unlink_through_a_planted_symlink(tmp_path):
    """The cleanup itself refuses a link, even if one was planted after the unpack check."""
    outside = tmp_path / "outside"
    outside.mkdir()
    decoy = outside / "p0.zip"
    decoy.write_bytes(b"the user's own file, not Nadare's")
    dl_dir = tmp_path / dd.DL_DIR
    dl_dir.mkdir()
    (dl_dir / "sub").symlink_to(outside)
    cached = dl_dir / "sub" / "p0.zip"

    dd._remove_cache_file(cached)

    assert decoy.exists(), "a cache cleanup unlinked a file outside the install"


def test_cancel_while_a_pack_stalls_in_the_pool_returns_quickly(tmp_path, monkeypatch):
    """Stop closes the pool's sockets before waiting for the download workers (Neon 2)."""
    small = _make_zip({"p0.bin": b"x" * 100})
    big = 8 << 20  # body the stalled server never finishes sending
    stalled = threading.Event()
    release = threading.Event()

    class Handler(http.server.BaseHTTPRequestHandler):
        protocol_version = "HTTP/1.1"

        def log_message(self, *args: object) -> None:
            pass

        def do_GET(self) -> None:
            if self.path == "/p0.zip":
                self.send_response(200)
                self.send_header("Content-Length", str(len(small)))
                self.end_headers()
                self.wfile.write(small)
                return
            self.send_response(200)
            self.send_header("Content-Length", str(big))
            self.end_headers()
            self.wfile.write(b"z" * 1024)
            self.wfile.flush()
            stalled.set()
            release.wait(10)  # a tarpit: no more bytes, no close
            self.close_connection = True

    httpd = http.server.ThreadingHTTPServer(("127.0.0.1", 0), Handler)
    thread = threading.Thread(target=httpd.serve_forever, daemon=True)
    thread.start()
    url = f"http://127.0.0.1:{httpd.server_port}"
    real_download = dd.download

    def short_timeout(*args, **kwargs):
        kwargs.setdefault("timeout", 2)  # the stalled socket timeout, not the 60 s default
        return real_download(*args, **kwargs)

    monkeypatch.setattr(dd, "download", short_timeout)
    plan = dd.Plan(
        "packpipe",
        "1.0",
        archives=(
            dd.Archive(
                "p0", dd.RemoteFile("p0.zip", f"{url}/p0.zip", len(small), md5=_md5(small))
            ),
            dd.Archive("p1", dd.RemoteFile("p1.zip", f"{url}/p1.zip", big)),
        ),
    )
    cancel = Cancel()

    def stopper() -> None:
        assert stalled.wait(5), "the second pack never started downloading"
        time.sleep(0.1)
        cancel.cancel()

    threading.Thread(target=stopper, daemon=True).start()
    began = time.monotonic()
    try:
        with pytest.raises(Cancelled):
            dd.sync(plan, tmp_path, cancel=cancel, workers=2)
    finally:
        release.set()
        httpd.shutdown()
        httpd.server_close()
        thread.join(timeout=5)
    elapsed = time.monotonic() - began
    assert elapsed < 1.5, f"Stop took {elapsed:.1f}s while a pack was stalled in the pool"


def test_space_check_counts_the_unpack_staging_copy(tmp_path, monkeypatch):
    """The pre-flight number covers the archives in flight plus the unpack staging copy (F2)."""
    size = 1 << 20
    plan = dd.Plan(
        "packpipe",
        "1.0",
        archives=tuple(
            dd.Archive(
                f"p{index}",
                dd.RemoteFile(f"p{index}.zip", f"https://example.invalid/{index}", size),
                unwrap=("zip",),
            )
            for index in range(2)
        ),
    )
    asked: list[int] = []
    monkeypatch.setattr(diskspace, "check_space", lambda path, need, **kw: asked.append(need))
    p1_written = threading.Event()
    measured: list[int] = []

    def fake_download(rf, path, *, emit=None, cancel=None, pool=None, redirect_allowed=None):
        path.parent.mkdir(parents=True, exist_ok=True)
        path.write_bytes(b"\0" * size)
        if rf.path == "p1.zip":
            p1_written.set()

    def fake_unwrap(archive, source, dest, dl_dir, cancel=None, changed=None):
        if archive.name == "p0":
            assert p1_written.wait(3), "pack 1 did not download while pack 0 unpacked"
            staging = Path(tempfile.mkdtemp(prefix="extract-", dir=dl_dir))
            (staging / "f0.bin").write_bytes(b"\0" * size)
            measured.append(_tree_bytes(tmp_path))
            os.replace(staging / "f0.bin", dest / "f0.bin")
            os.rmdir(staging)
            return ["f0.bin"]
        (dest / "f1.bin").write_bytes(b"\0" * size)
        return ["f1.bin"]

    monkeypatch.setattr(dd, "_download_remote", fake_download)
    monkeypatch.setattr(dd, "_unwrap", fake_unwrap)
    dd.sync(plan, tmp_path, workers=2)

    assert measured and 3 * size <= measured[0] < 3 * size + 4096, measured
    assert asked and asked[0] >= measured[0], (
        f"asked for {asked} while the install peaked at {measured[0]} bytes"
    )


@pytest.mark.parametrize("packs", [1, 2])
def test_enospc_during_a_pack_download_keeps_the_resumable_part(tmp_path, monkeypatch, packs):
    """A full disk is not a corrupt archive: the .part stays so the next run resumes it."""
    blobs = {f"p{i}.zip": _make_zip({f"f{i}.bin": bytes([65 + i]) * 200}) for i in range(packs)}
    plan = dd.Plan(
        "packpipe",
        "1.0",
        archives=tuple(
            dd.Archive(
                f"p{index}",
                dd.RemoteFile(f"p{index}.zip", f"https://example.invalid/{index}", len(body)),
            )
            for index, body in enumerate(blobs.values())
        ),
    )
    failing = f"p{packs - 1}.zip"

    def full_disk(url, destination, **kwargs):
        destination = Path(destination)
        if destination.name != failing:
            destination.parent.mkdir(parents=True, exist_ok=True)
            destination.write_bytes(blobs[destination.name])
            return
        part = destination.with_name(destination.name + ".part")
        part.parent.mkdir(parents=True, exist_ok=True)
        part.write_bytes(blobs[failing][: len(blobs[failing]) // 2])
        raise dl.DownloadError("not enough disk space") from OSError(
            errno.ENOSPC, "No space left on device"
        )

    monkeypatch.setattr(dd, "download", full_disk)
    with pytest.raises(dd.DirectError, match="download failed"):
        dd.sync(plan, tmp_path, workers=2)

    assert (tmp_path / dd.DL_DIR / f"{failing}.part").is_file(), (
        "the resumable .part was deleted when the disk was full"
    )
    assert not (tmp_path / dd.DL_DIR / failing).exists()


def test_a_damaged_multi_part_zip_is_a_direct_error_not_a_raw_valueerror(tmp_path):
    """A truncated split pack makes zipfile seek before the start of the joined parts;
    that must read as a damaged archive, not "negative seek position" (sim-pack 1126)."""
    data = _make_zip({"a.bin": b"a" * 300, "b.bin": b"b" * 300})[10:]
    half = len(data) // 2
    first, second = tmp_path / "p.zip.001", tmp_path / "p.zip.002"
    first.write_bytes(data[:half])
    second.write_bytes(data[half:])
    joined = dd._JoinedFile([first, second])
    try:
        with pytest.raises(dd.DirectError):
            dd._extract_zip_to(joined, tmp_path / "out", None)
    finally:
        joined.close()


# ---- Neon r1 on packsec (2026-11-26) ------------------------------------------------------
def test_a_symlinked_download_cache_itself_is_refused(tmp_path):
    """Replacing .nadare-dl itself with a link must not send writes or unlinks outside."""
    outside = tmp_path / "outside"
    outside.mkdir()
    (outside / "p0.zip").write_bytes(b"user file")
    dest = tmp_path / "game"
    dest.mkdir()
    (dest / dd.DL_DIR).symlink_to(outside, target_is_directory=True)
    with pytest.raises(dd.DirectError):
        dd._refuse_cache_symlinks(dest / dd.DL_DIR / "p0.zip", dest / dd.DL_DIR)
    dd._remove_cache_file(dest / dd.DL_DIR / "p0.zip")
    assert (outside / "p0.zip").read_bytes() == b"user file"


def test_a_closed_pool_opens_no_new_connection():
    """Stop closes the pool; a worker woken by that must not dial the stalled server again."""
    pool = dl.HttpClientPool()
    pool.close()
    with pytest.raises(OSError):
        pool._connection("http", "127.0.0.1", 9, 1)
    assert pool._all == []


def test_a_later_pack_this_sync_reinstalls_does_not_keep_its_old_members(tmp_path):
    """b.done from an older b build must not stop a from writing a path b no longer ships."""
    pack_a = _make_zip({"shared.bin": b"A" * 10})
    pack_b = _make_zip({"other.bin": b"b"})
    a = dd.Archive("a", dd.RemoteFile("a.zip", "https://x.invalid/a.zip", len(pack_a)))
    b = dd.Archive("b", dd.RemoteFile("b.zip", "https://x.invalid/b.zip", len(pack_b)))
    plan = dd.Plan("packpipe", "1.0", archives=(a, b))
    (tmp_path / "shared.bin").write_bytes(b"B" * 10)
    dd._write_done(plan, b, tmp_path, ["shared.bin"])
    assert "shared.bin" in dd._later_member_owners(plan, a, tmp_path, installing=())
    assert dd._later_member_owners(plan, a, tmp_path, installing=(b,)) == {}
