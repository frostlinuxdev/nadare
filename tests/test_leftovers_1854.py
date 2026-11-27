# SPDX-License-Identifier: GPL-3.0-or-later
"""Leftovers of the 2-pack pipeline review (#1854)."""

from __future__ import annotations

import concurrent.futures
import errno
import gzip
import hashlib
import http.server
import io
import os
import stat
import threading
import time
import zipfile
from pathlib import Path

import pytest
from _httpserver import Server

from nadare.core import directdl as dd
from nadare.core import download as dl
from nadare.core.events import Cancel, Cancelled

SEVENZ = Path(__file__).parent / "fixtures" / "sevenz"


def _md5(data: bytes) -> str:
    return hashlib.md5(data).hexdigest()


def _make_zip(files: dict[str, bytes]) -> bytes:
    buf = io.BytesIO()
    with zipfile.ZipFile(buf, "w", compression=zipfile.ZIP_STORED) as zf:
        for name, body in files.items():
            info = zipfile.ZipInfo(name, date_time=(2026, 1, 1, 0, 0, 0))
            info.compress_type = zipfile.ZIP_STORED
            info.external_attr = (stat.S_IFREG | 0o644) << 16
            zf.writestr(info, body)
    return buf.getvalue()


@pytest.mark.parametrize("code", [errno.ENOSPC, errno.EDQUOT])
def test_a_full_disk_keeps_the_resumable_part_of_a_replacement(tmp_path, monkeypatch, code):
    target = tmp_path / "game.bin"
    target.write_bytes(b"OLD-GAME-DATA")
    rf = dd.RemoteFile("game.bin", "https://example.invalid/game.bin", 4)

    def fill_the_disk(_url, dest, **kwargs):
        part = dest.with_name(dest.name + ".part")
        part.write_bytes(b"AB")
        raise dd.DownloadError("not enough disk space") from OSError(code, "full")

    monkeypatch.setattr(dd, "download", fill_the_disk)
    with pytest.raises(dd.DirectError, match="download failed"):
        dd._download_remote(rf, target, emit=None, cancel=None)
    assert target.read_bytes() == b"OLD-GAME-DATA"
    assert (tmp_path / "game.bin.nadare-new.part").read_bytes() == b"AB"


# ---------------------------------------------------------------------------
# A. Item 3: streamed packs (zip and 7z) must honour keep
# ---------------------------------------------------------------------------


@pytest.mark.parametrize("with_middle_pack", [False, True])
def test_resumed_streamed_zip_keeps_later_packs_shared_member(tmp_path, with_middle_pack):
    """A resumed streamed zip (1-pack or 2-pack pipeline) must not overwrite a later pack's file."""
    pack_a = _make_zip({"shared.bin": b"A-STREAM" * 4, "only-a.bin": b"a"})
    pack_mid = _make_zip({"only-mid.bin": b"m"})
    pack_b = _make_zip({"shared.bin": b"B-LATER" * 5, "only-b.bin": b"b"})
    srv = Server({"/a.zip": pack_a, "/mid.zip": pack_mid, "/b.zip": pack_b}, keepalive=True)
    try:
        archive_a = dd.Archive(
            "a",
            dd.RemoteFile("a.zip", f"{srv.url}/a.zip", len(pack_a)),
            unwrap=("zip",),
            stream=True,
        )
        archive_mid = dd.Archive(
            "mid",
            dd.RemoteFile("mid.zip", f"{srv.url}/mid.zip", len(pack_mid), md5=_md5(pack_mid)),
        )
        archive_b = dd.Archive(
            "b",
            dd.RemoteFile("b.zip", f"{srv.url}/b.zip", len(pack_b), md5=_md5(pack_b)),
        )
        archives = (
            (archive_a, archive_mid, archive_b) if with_middle_pack else (archive_a, archive_b)
        )
        plan = dd.Plan("packpipe", "1.0", archives=archives)

        # Later pack b is already installed.
        dd.sync(dd.Plan("packpipe", "1.0", archives=(archive_b,)), tmp_path, workers=1)
        assert (tmp_path / "shared.bin").read_bytes() == b"B-LATER" * 5

        summary = dd.sync(plan, tmp_path, workers=2)
        assert summary == {"downloaded": 2 if with_middle_pack else 1, "skipped": 1}
        assert (tmp_path / "only-a.bin").read_bytes() == b"a"
        assert (tmp_path / "shared.bin").read_bytes() == b"B-LATER" * 5
        assert srv.ranges, "streamed zip did not use ranged reads"

        again = dd.sync(plan, tmp_path, workers=2)
        assert again == {"downloaded": 0, "skipped": len(archives)}
        assert (tmp_path / "shared.bin").read_bytes() == b"B-LATER" * 5
    finally:
        srv.close()


def test_resumed_streamed_7z_keeps_later_packs_shared_member(tmp_path):
    """A resumed streamed 7z must not overwrite a later installed pack's shared member."""
    blob_7z = (SEVENZ / "nonsolid.7z").read_bytes()
    pack_mid = _make_zip({"only-mid.bin": b"m"})
    pack_b = _make_zip({"hello.txt": b"LATER-PACK-HELLO", "only-b.bin": b"b"})
    srv = Server({"/a.7z": blob_7z, "/mid.zip": pack_mid, "/b.zip": pack_b}, keepalive=True)
    try:
        archive_a = dd.Archive(
            "a",
            dd.RemoteFile("a.7z", f"{srv.url}/a.7z", len(blob_7z)),
            unwrap=("7z",),
            stream=True,
        )
        archive_mid = dd.Archive(
            "mid",
            dd.RemoteFile("mid.zip", f"{srv.url}/mid.zip", len(pack_mid), md5=_md5(pack_mid)),
        )
        archive_b = dd.Archive(
            "b",
            dd.RemoteFile("b.zip", f"{srv.url}/b.zip", len(pack_b), md5=_md5(pack_b)),
        )
        plan = dd.Plan("packpipe", "1.0", archives=(archive_a, archive_mid, archive_b))

        dd.sync(dd.Plan("packpipe", "1.0", archives=(archive_b,)), tmp_path, workers=1)
        assert (tmp_path / "hello.txt").read_bytes() == b"LATER-PACK-HELLO"

        summary = dd.sync(plan, tmp_path, workers=2)
        assert summary == {"downloaded": 2, "skipped": 1}
        assert (tmp_path / "hello.txt").read_bytes() == b"LATER-PACK-HELLO"
        expected_sub = (SEVENZ / "expected/sub/data.bin").read_bytes()
        assert (tmp_path / "sub/data.bin").read_bytes() == expected_sub
        assert srv.ranges, "streamed 7z did not use ranged reads"

        again = dd.sync(plan, tmp_path, workers=2)
        assert again == {"downloaded": 0, "skipped": 3}
        assert (tmp_path / "hello.txt").read_bytes() == b"LATER-PACK-HELLO"
    finally:
        srv.close()


# ---------------------------------------------------------------------------
# B. Item 4.2: directslices.matches at older version
# ---------------------------------------------------------------------------


def test_later_member_owners_counts_older_version_slices_matched_by_directslices(tmp_path):
    """A later slices pack skipped via directslices.matches at an older version keeps its files."""
    shared_b = b"SLICE-SHARED-B" * 3
    only_b = b"SLICE-ONLY-B"
    slice_blob = shared_b + only_b
    pack_a = _make_zip({"shared.bin": b"PACK-A-OLD", "only-a.bin": b"a"})
    srv = Server({"/a.zip": pack_a, "/b.pak": slice_blob}, keepalive=True)
    try:
        archive_b = dd.Archive(
            "b",
            dd.RemoteFile("b.pak", f"{srv.url}/b.pak", len(slice_blob), md5=_md5(slice_blob)),
            unwrap=("slices",),
            members=(
                dd.Member("shared.bin", 0, len(shared_b), md5=_md5(shared_b)),
                dd.Member("only-b.bin", len(shared_b), len(only_b), md5=_md5(only_b)),
            ),
        )
        archive_a = dd.Archive(
            "a",
            dd.RemoteFile("a.zip", f"{srv.url}/a.zip", len(pack_a), md5=_md5(pack_a)),
        )

        # Install pack b at version 1.0.
        dd.sync(dd.Plan("packpipe", "1.0", archives=(archive_b,)), tmp_path, workers=1)
        assert (tmp_path / "shared.bin").read_bytes() == shared_b

        # Version 2.0 adds pack a before unchanged pack b. verify=False skips pack b via
        # directslices.matches without rewriting b.done's version to 2.0.
        plan_v2 = dd.Plan("packpipe", "2.0", archives=(archive_a, archive_b))
        owners = dd._later_member_owners(plan_v2, archive_a, tmp_path, installing=(archive_a,))
        assert "shared.bin" in owners

        summary = dd.sync(plan_v2, tmp_path, workers=2, verify=False)
        assert summary == {"downloaded": 1, "skipped": 1}
        assert (tmp_path / "only-a.bin").read_bytes() == b"a"
        assert (tmp_path / "shared.bin").read_bytes() == shared_b
    finally:
        srv.close()


# ---------------------------------------------------------------------------
# C. Item 5: _check_cache_path_clash catches .nadare-new.part* and dir prefixes
# ---------------------------------------------------------------------------


def test_cache_path_clash_catches_nadare_new_parts_and_dir_prefixes():
    """_check_cache_path_clash refuses .nadare-new.part* and .part/.nadare-new dir prefixes."""

    def archive(name: str, path: str) -> dd.Archive:
        return dd.Archive(name, dd.RemoteFile(path, f"https://example.invalid/{path}", 10))

    def refused_both_orders(first: str, second: str) -> None:
        for a, b in ((first, second), (second, first)):
            plan = dd.Plan("packpipe", "1.0", archives=(archive("p0", a), archive("p1", b)))
            with pytest.raises(dd.DirectError, match="clash"):
                dd._validate_plan(plan)

    refused_both_orders("a.zip", "a.zip.nadare-new.part")
    refused_both_orders("a.zip", "a.zip.nadare-new.part-0")
    refused_both_orders("a.zip", "a.zip.nadare-new.part-0.done")
    refused_both_orders("a.zip", "a.zip.nadare-new.part.ranges.json")
    refused_both_orders("a.zip", "a.zip.part/sub.zip")
    refused_both_orders("a.zip", "a.zip.nadare-new/sub.zip")
    refused_both_orders("a.zip", "a.zip.nadare-new.part/sub.zip")
    refused_both_orders("a.zip", f"a.zip{dd.PACKED_SUFFIX}")
    refused_both_orders("a.zip", f"a.zip{dd.PACKED_SUFFIX}.part")
    refused_both_orders("a.zip", f"a.zip{dd.PACKED_SUFFIX}.part-0")
    refused_both_orders("a.zip", f"a.zip{dd.PACKED_SUFFIX}.part-0.done")
    refused_both_orders("a.zip", f"a.zip{dd.PACKED_SUFFIX}.part.ranges.json")
    refused_both_orders("a.zip", f"a.zip{dd.PACKED_SUFFIX}/sub.zip")
    refused_both_orders("a.zip", f"a.zip{dd.PACKED_SUFFIX}.part/sub.zip")

    # Sibling folders that do not collide with derived files still validate.
    dd._validate_plan(
        dd.Plan(
            "packpipe",
            "1.0",
            archives=(archive("p0", "dir/a.zip"), archive("p1", "dir.part/b.zip")),
        )
    )


# ---------------------------------------------------------------------------
# D. Item 7: symlink re-check across remote_size, _verify_remote, retries, _download_packed
# ---------------------------------------------------------------------------


def test_symlink_recheck_after_remote_size(tmp_path, monkeypatch):
    """A symlink planted while remote_size() runs must be caught before any write."""
    body = _make_zip({"a.bin": b"A" * 10})
    srv = Server({"/p0.zip": body}, keepalive=True)
    outside = tmp_path / "outside"
    outside.mkdir()
    inst = tmp_path / "inst"
    try:
        plan = dd.Plan(
            "packpipe",
            "1.0",
            archives=(
                dd.Archive(
                    "p0",
                    dd.RemoteFile("sub/p0.zip", f"{srv.url}/p0.zip", size=None, md5=_md5(body)),
                ),
            ),
        )

        def planting_remote_size(*args, **kwargs):
            (outside / "p0.zip").write_bytes(body)
            (inst / dd.DL_DIR / "sub").symlink_to(outside, target_is_directory=True)
            return len(body)

        verify_calls: list[Path] = []
        real_verify = dd._verify_remote

        def spy_verify(path, rf, **kwargs):
            verify_calls.append(path)
            return real_verify(path, rf, **kwargs)

        monkeypatch.setattr(dd, "remote_size", planting_remote_size)
        monkeypatch.setattr(dd, "_verify_remote", spy_verify)
        with pytest.raises(dd.DirectError, match="is a symlink"):
            dd.sync(plan, inst, workers=1)
    finally:
        srv.close()

    assert verify_calls == [], "reached _verify_remote after symlink planted in remote_size()"
    assert srv.requests == []
    assert not (inst / "a.bin").exists()


def test_symlink_recheck_after_verify_remote(tmp_path, monkeypatch):
    """A symlink planted during _verify_remote() must be caught before staging a replacement."""
    body = _make_zip({"a.bin": b"A" * 10})
    srv = Server({"/p0.zip": body}, keepalive=True)
    outside = tmp_path / "outside"
    outside.mkdir()
    inst = tmp_path / "inst"
    sub = inst / dd.DL_DIR / "sub"
    sub.mkdir(parents=True)
    (sub / "p0.zip").write_bytes(b"stale-bad-bytes")
    real_verify = dd._verify_remote
    planted = False
    try:
        plan = dd.Plan(
            "packpipe",
            "1.0",
            archives=(
                dd.Archive(
                    "p0",
                    dd.RemoteFile("sub/p0.zip", f"{srv.url}/p0.zip", len(body), md5=_md5(body)),
                ),
            ),
        )

        def planting_verify(path, rf, **kwargs):
            nonlocal planted
            ok = real_verify(path, rf, **kwargs)
            if not planted and path == sub / "p0.zip":
                planted = True
                os.rename(sub, Path(str(sub) + ".real"))
                sub.symlink_to(outside, target_is_directory=True)
            return ok

        monkeypatch.setattr(dd, "_verify_remote", planting_verify)
        monkeypatch.setattr(
            dd, "download", lambda *a, **k: pytest.fail("download() called after symlink planted")
        )
        with pytest.raises(dd.DirectError, match="is a symlink"):
            dd.sync(plan, inst, workers=1)
    finally:
        srv.close()

    assert planted
    assert list(outside.iterdir()) == []
    assert not (inst / "a.bin").exists()


def test_symlink_recheck_across_download_retries(tmp_path, monkeypatch):
    """A symlink planted during a download() retry backoff must be refused before attempt 2."""
    body = _make_zip({"a.bin": b"A" * 10})
    outside = tmp_path / "outside"
    outside.mkdir()
    inst = tmp_path / "inst"
    attempts = 0

    class Handler(http.server.BaseHTTPRequestHandler):
        protocol_version = "HTTP/1.1"

        def log_message(self, *args: object) -> None:
            pass

        def do_GET(self) -> None:
            nonlocal attempts
            attempts += 1
            if attempts == 1:
                self.send_response(503)
                self.send_header("Content-Length", "0")
                self.end_headers()
                return
            self.send_response(200)
            self.send_header("Content-Length", str(len(body)))
            self.end_headers()
            self.wfile.write(body)

    httpd = http.server.ThreadingHTTPServer(("127.0.0.1", 0), Handler)
    thread = threading.Thread(target=httpd.serve_forever, daemon=True)
    thread.start()
    url = f"http://127.0.0.1:{httpd.server_port}/p0.zip"
    planted = False

    def plant_during_backoff(_seconds, _cancel):
        nonlocal planted
        if not planted:
            planted = True
            sub = inst / dd.DL_DIR / "sub"
            if sub.exists():
                os.rename(sub, Path(str(sub) + ".real"))
            sub.symlink_to(outside, target_is_directory=True)

    monkeypatch.setattr(dl, "_sleep", plant_during_backoff)
    try:
        plan = dd.Plan(
            "packpipe",
            "1.0",
            archives=(
                dd.Archive("p0", dd.RemoteFile("sub/p0.zip", url, len(body), md5=_md5(body))),
            ),
        )
        with pytest.raises(dd.DirectError, match="is a symlink"):
            dd.sync(plan, inst, workers=1)
    finally:
        httpd.shutdown()
        httpd.server_close()
        thread.join(timeout=5)

    assert planted
    assert attempts == 1, "attempt 2 dialed after symlink was planted during retry backoff"
    assert list(outside.iterdir()) == []
    assert not (inst / "a.bin").exists()

    # Also verify a symlink planted during the HTTP round-trip (after _download_once started,
    # right before open(part, mode)) is refused before writing to outside.
    inst2 = tmp_path / "inst2"
    outside2 = tmp_path / "outside2"
    outside2.mkdir()
    planted_in_get = False

    class PlantOnGetHandler(http.server.BaseHTTPRequestHandler):
        protocol_version = "HTTP/1.1"

        def log_message(self, *args: object) -> None:
            pass

        def do_GET(self) -> None:
            nonlocal planted_in_get
            if not planted_in_get:
                planted_in_get = True
                sub = inst2 / dd.DL_DIR / "sub"
                if sub.exists():
                    os.rename(sub, Path(str(sub) + ".real"))
                sub.symlink_to(outside2, target_is_directory=True)
            self.send_response(200)
            self.send_header("Content-Length", str(len(body)))
            self.end_headers()
            self.wfile.write(body)

    httpd2 = http.server.ThreadingHTTPServer(("127.0.0.1", 0), PlantOnGetHandler)
    thread2 = threading.Thread(target=httpd2.serve_forever, daemon=True)
    thread2.start()
    try:
        plan2 = dd.Plan(
            "packpipe",
            "1.0",
            archives=(
                dd.Archive(
                    "p0",
                    dd.RemoteFile(
                        "sub/p0.zip",
                        f"http://127.0.0.1:{httpd2.server_port}/p0.zip",
                        len(body),
                        md5=_md5(body),
                    ),
                ),
            ),
        )
        with pytest.raises(dd.DirectError, match="is a symlink"):
            dd.sync(plan2, inst2, workers=1)
    finally:
        httpd2.shutdown()
        httpd2.server_close()
        thread2.join(timeout=5)

    assert planted_in_get
    assert list(outside2.iterdir()) == []
    assert not (inst2 / "a.bin").exists()


def test_symlink_recheck_in_download_packed(tmp_path, monkeypatch):
    """_download_packed must re-check cache symlinks before unpacking into the target folder."""
    raw = _make_zip({"a.bin": b"PACKED-DATA" * 20})
    gz_body = gzip.compress(raw)
    srv = Server({"/p0.zip.gz": gz_body}, keepalive=True)
    outside = tmp_path / "outside"
    outside.mkdir()
    inst = tmp_path / "inst"
    sub = inst / dd.DL_DIR / "sub"
    target = sub / "p0.zip"
    rf = dd.RemoteFile(
        "sub/p0.zip",
        f"{srv.url}/p0.zip.gz",
        len(raw),
        md5=_md5(raw),
        packed=dd.Packed(codec="gzip", size=len(gz_body), md5=_md5(gz_body)),
    )
    plan = dd.Plan("packpipe", "1.0", archives=(dd.Archive("p0", rf),))
    real_verify = dd._verify_remote
    planted = False

    def plant_after_gz_verify(path, item, **kwargs):
        nonlocal planted
        ok = real_verify(path, item, **kwargs)
        if not planted and path == target.with_name(target.name + dd.PACKED_SUFFIX):
            planted = True
            os.rename(sub, Path(str(sub) + ".real"))
            (outside / (target.name + dd.PACKED_SUFFIX)).write_bytes(gz_body)
            sub.symlink_to(outside, target_is_directory=True)
        return ok

    monkeypatch.setattr(dd, "_verify_remote", plant_after_gz_verify)
    try:
        with pytest.raises(dd.DirectError, match="is a symlink"):
            dd.sync(plan, inst, workers=1)
    finally:
        srv.close()

    assert planted
    assert not (outside / target.name).exists()
    assert not (outside / (target.name + ".nadare-new.part")).exists()
    assert not (inst / "a.bin").exists()


def test_download_packed_refuses_staged_symlink_before_verify(tmp_path, monkeypatch):
    """_download_packed must refuse a symlinked staged file before hashing/verifying it."""
    raw = _make_zip({"a.bin": b"PACKED-STAGED" * 20})
    gz_body = gzip.compress(raw)
    srv = Server({"/p0.zip.gz": gz_body}, keepalive=True)
    outside = tmp_path / "outside"
    outside.mkdir()
    outside_gz = outside / f"p0.zip{dd.PACKED_SUFFIX}"
    outside_gz.write_bytes(gz_body)
    inst = tmp_path / "inst"
    sub = inst / dd.DL_DIR / "sub"
    sub.mkdir(parents=True)
    staged = sub / f"p0.zip{dd.PACKED_SUFFIX}"
    staged.symlink_to(outside_gz)
    rf = dd.RemoteFile(
        "sub/p0.zip",
        f"{srv.url}/p0.zip.gz",
        len(raw),
        md5=_md5(raw),
        packed=dd.Packed(codec="gzip", size=len(gz_body), md5=_md5(gz_body)),
    )
    plan = dd.Plan("packpipe", "1.0", archives=(dd.Archive("p0", rf),))
    verified_paths: list[Path] = []
    real_verify = dd._verify_remote

    def spy_verify(path, item, **kwargs):
        verified_paths.append(path)
        return real_verify(path, item, **kwargs)

    monkeypatch.setattr(dd, "_verify_remote", spy_verify)
    try:
        with pytest.raises(dd.DirectError, match="is a symlink"):
            dd.sync(plan, inst, workers=1)
    finally:
        srv.close()

    assert staged not in verified_paths, "_verify_remote read through symlinked staged file"
    assert not (inst / "a.bin").exists()


def test_download_omits_before_write_for_download_ranges_when_none(tmp_path, monkeypatch):
    """download() must not pass before_write to _download_ranges when before_write is None."""
    body = b"R" * 8192
    monkeypatch.setattr(dl, "RANGE_THRESHOLD", 1024)
    called: list[bool] = []

    def stub_ranges(
        url,
        part,
        *,
        expected_size,
        workers,
        headers,
        timeout,
        emit,
        cancel,
        chunk,
        redirect_allowed,
        pool,
        digests_out=None,
    ):
        called.append(True)
        part.write_bytes(body)
        return False

    monkeypatch.setattr(dl, "_download_ranges", stub_ranges)
    dest = tmp_path / "big.bin"
    dl.download(
        "https://example.invalid/big.bin",
        dest,
        expected_size=len(body),
        range_workers=2,
    )
    assert called == [True]
    assert dest.read_bytes() == body


def test_symlink_recheck_in_ranged_download_above_threshold(tmp_path, monkeypatch):
    """A symlink planted during a ranged download (> RANGE_THRESHOLD) is refused before write."""
    body = _make_zip({"a.bin": b"RANGE-DATA" * 1024})
    outside = tmp_path / "outside"
    outside.mkdir()
    inst = tmp_path / "inst"
    planted = False
    plant_lock = threading.Lock()
    span_requests = 0

    class RangePlantHandler(http.server.BaseHTTPRequestHandler):
        protocol_version = "HTTP/1.1"

        def log_message(self, *args: object) -> None:
            pass

        def do_GET(self) -> None:
            nonlocal planted, span_requests
            range_hdr = self.headers.get("Range", "")
            if range_hdr == "bytes=0-0":
                self.send_response(206)
                self.send_header("Accept-Ranges", "bytes")
                self.send_header("Content-Range", f"bytes 0-0/{len(body)}")
                self.send_header("Content-Length", "1")
                self.end_headers()
                self.wfile.write(body[:1])
                return
            spec = range_hdr.removeprefix("bytes=")
            start_s, _, end_s = spec.partition("-")
            start, end = int(start_s), int(end_s)
            chunk = body[start : end + 1]
            with plant_lock:
                span_requests += 1
                if not planted:
                    planted = True
                    sub = inst / dd.DL_DIR / "sub"
                    if sub.exists():
                        os.rename(sub, Path(str(sub) + ".real"))
                    sub.symlink_to(outside, target_is_directory=True)
            self.send_response(206)
            self.send_header("Accept-Ranges", "bytes")
            self.send_header("Content-Range", f"bytes {start}-{end}/{len(body)}")
            self.send_header("Content-Length", str(len(chunk)))
            self.end_headers()
            self.wfile.write(chunk)

    httpd = http.server.ThreadingHTTPServer(("127.0.0.1", 0), RangePlantHandler)
    thread = threading.Thread(target=httpd.serve_forever, daemon=True)
    thread.start()
    url = f"http://127.0.0.1:{httpd.server_port}/p0.zip"
    monkeypatch.setattr(dl, "RANGE_THRESHOLD", 4096)
    try:
        plan = dd.Plan(
            "packpipe",
            "1.0",
            archives=(
                dd.Archive("p0", dd.RemoteFile("sub/p0.zip", url, len(body), md5=_md5(body))),
            ),
        )
        with pytest.raises(dd.DirectError, match="is a symlink"):
            dd.sync(plan, inst, workers=2)
    finally:
        httpd.shutdown()
        httpd.server_close()
        thread.join(timeout=5)

    assert planted
    assert span_requests >= 1
    assert list(outside.iterdir()) == []
    assert not (inst / "a.bin").exists()


# ---------------------------------------------------------------------------
# E. Item 8: single-pack multi-part and ranged downloads close pool before wait on Stop
# ---------------------------------------------------------------------------


def test_stop_during_stalled_single_pack_multipart_returns_quickly(tmp_path, monkeypatch):
    """Stop on a stalled single-pack multi-part archive closes its pool before waiting."""
    z = _make_zip({"a.bin": b"alpha" * 200})
    part0 = z[: len(z) // 2]
    stalled = threading.Event()
    release = threading.Event()

    class Handler(http.server.BaseHTTPRequestHandler):
        protocol_version = "HTTP/1.1"

        def log_message(self, *args: object) -> None:
            pass

        def do_GET(self) -> None:
            if self.path == "/p.0":
                self.send_response(200)
                self.send_header("Content-Length", str(len(part0)))
                self.end_headers()
                self.wfile.write(part0)
                return
            self.send_response(200)
            self.send_header("Content-Length", str(8 << 20))
            self.end_headers()
            self.wfile.write(b"z" * 1024)
            self.wfile.flush()
            stalled.set()
            release.wait(10)
            self.close_connection = True

    httpd = http.server.ThreadingHTTPServer(("127.0.0.1", 0), Handler)
    thread = threading.Thread(target=httpd.serve_forever, daemon=True)
    thread.start()
    url = f"http://127.0.0.1:{httpd.server_port}"
    real_download = dd.download

    def short_timeout(*args, **kwargs):
        kwargs.setdefault("timeout", 4)
        return real_download(*args, **kwargs)

    monkeypatch.setattr(dd, "download", short_timeout)
    plan = dd.Plan(
        "packpipe",
        "1.0",
        archives=(
            dd.Archive(
                "p",
                dd.RemoteFile("p.zip", f"{url}/p.0", len(part0) + (8 << 20)),
                parts=(
                    dd.RemoteFile("p.0", f"{url}/p.0", len(part0)),
                    dd.RemoteFile("p.1", f"{url}/p.1", 8 << 20),
                ),
            ),
        ),
    )
    cancel = Cancel()

    def stopper() -> None:
        assert stalled.wait(5), "part 1 never reached the stalling handler"
        time.sleep(0.05)
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
    assert elapsed < 1.5, f"Stop took {elapsed:.2f}s on stalled single-pack multi-part"


def test_stop_during_stalled_ranged_download_returns_quickly(tmp_path, monkeypatch):
    """Stop on a stalled ranged download closes its pool before waiting for workers."""
    total_size = 16384
    stalled = threading.Event()
    release = threading.Event()

    class Handler(http.server.BaseHTTPRequestHandler):
        protocol_version = "HTTP/1.1"

        def log_message(self, *args: object) -> None:
            pass

        def do_GET(self) -> None:
            range_hdr = self.headers.get("Range", "")
            if range_hdr == "bytes=0-0":
                self.send_response(206)
                self.send_header("Accept-Ranges", "bytes")
                self.send_header("Content-Range", f"bytes 0-0/{total_size}")
                self.send_header("Content-Length", "1")
                self.end_headers()
                self.wfile.write(b"x")
                return
            spec = range_hdr.removeprefix("bytes=")
            start_s, _, end_s = spec.partition("-")
            start, end = int(start_s), int(end_s)
            span = end - start + 1
            self.send_response(206)
            self.send_header("Accept-Ranges", "bytes")
            self.send_header("Content-Range", f"bytes {start}-{end}/{total_size}")
            self.send_header("Content-Length", str(span))
            self.end_headers()
            self.wfile.write(b"z" * min(256, span))
            self.wfile.flush()
            stalled.set()
            release.wait(10)
            self.close_connection = True

    httpd = http.server.ThreadingHTTPServer(("127.0.0.1", 0), Handler)
    thread = threading.Thread(target=httpd.serve_forever, daemon=True)
    thread.start()
    url = f"http://127.0.0.1:{httpd.server_port}/big.bin"
    monkeypatch.setattr(dl, "RANGE_THRESHOLD", 4096)
    cancel = Cancel()

    def stopper() -> None:
        assert stalled.wait(5), "ranged worker never reached the stalling handler"
        time.sleep(0.05)
        cancel.cancel()

    threading.Thread(target=stopper, daemon=True).start()
    began = time.monotonic()
    try:
        with pytest.raises(Cancelled):
            dl.download(
                url,
                tmp_path / "big.bin",
                expected_size=total_size,
                timeout=4,
                cancel=cancel,
                range_workers=2,
            )
    finally:
        release.set()
        httpd.shutdown()
        httpd.server_close()
        thread.join(timeout=5)
    elapsed = time.monotonic() - began
    assert elapsed < 1.5, f"Stop took {elapsed:.2f}s on stalled ranged download"


def test_stop_polling_handles_submit_error_without_unbound_local(tmp_path, monkeypatch):
    """If executor.submit() raises, neither _sync_archive nor _download_ranges masks it."""
    z = _make_zip({"a.bin": b"alpha" * 200})
    half = len(z) // 2
    srv = Server({"/p.0": z[:half], "/p.1": z[half:], "/big.bin": b"B" * 8192}, keepalive=True)

    def boom_submit(self, fn, *args, **kwargs):
        raise RuntimeError("submit boom")

    monkeypatch.setattr(concurrent.futures.ThreadPoolExecutor, "submit", boom_submit)
    monkeypatch.setattr(dl, "RANGE_THRESHOLD", 1024)
    try:
        plan = dd.Plan(
            "packpipe",
            "1.0",
            archives=(
                dd.Archive(
                    "p",
                    dd.RemoteFile("p.zip", f"{srv.url}/p.0", len(z)),
                    parts=(
                        dd.RemoteFile("p.0", f"{srv.url}/p.0", half),
                        dd.RemoteFile("p.1", f"{srv.url}/p.1", len(z) - half),
                    ),
                ),
            ),
        )
        with pytest.raises(RuntimeError, match="submit boom"):
            dd.sync(plan, tmp_path / "inst", workers=2)

        with pytest.raises(RuntimeError, match="submit boom"):
            dl.download(
                f"{srv.url}/big.bin",
                tmp_path / "big.bin",
                expected_size=8192,
                range_workers=2,
            )
    finally:
        srv.close()


def test_stop_polling_uses_blocking_wait_when_cancel_is_none(tmp_path, monkeypatch):
    """wait_futures uses timeout=None when cancel is None and timeout=0.05 with a Cancel token."""
    z = _make_zip({"a.bin": b"alpha" * 200})
    half = len(z) // 2
    big = b"W" * 8192
    srv = Server({"/p.0": z[:half], "/p.1": z[half:], "/big.bin": big}, keepalive=True)
    real_wait = concurrent.futures.wait
    timeouts: list[float | None] = []

    def spy_wait(fs, timeout=None, return_when=concurrent.futures.ALL_COMPLETED):
        timeouts.append(timeout)
        return real_wait(fs, timeout=timeout, return_when=return_when)

    monkeypatch.setattr(dd, "wait_futures", spy_wait)
    monkeypatch.setattr(concurrent.futures, "wait", spy_wait)
    monkeypatch.setattr(dl, "RANGE_THRESHOLD", 1024)
    try:
        plan = dd.Plan(
            "packpipe",
            "1.0",
            archives=(
                dd.Archive(
                    "p",
                    dd.RemoteFile("p.zip", f"{srv.url}/p.0", len(z)),
                    parts=(
                        dd.RemoteFile("p.0", f"{srv.url}/p.0", half),
                        dd.RemoteFile("p.1", f"{srv.url}/p.1", len(z) - half),
                    ),
                ),
            ),
        )

        # Single-pack multi-part without cancel -> timeout=None.
        timeouts.clear()
        dd.sync(plan, tmp_path / "mp-none", cancel=None, workers=2)
        assert timeouts and all(t is None for t in timeouts)

        # Single-pack multi-part with cancel -> timeout=0.05.
        timeouts.clear()
        dd.sync(plan, tmp_path / "mp-cancel", cancel=Cancel(), workers=2)
        assert timeouts and all(t == 0.05 for t in timeouts)

        # Ranged download without cancel -> timeout=None.
        timeouts.clear()
        dl.download(
            f"{srv.url}/big.bin",
            tmp_path / "big-none.bin",
            expected_size=len(big),
            cancel=None,
            range_workers=2,
        )
        assert timeouts and all(t is None for t in timeouts)

        # Ranged download with cancel -> timeout=0.05.
        timeouts.clear()
        dl.download(
            f"{srv.url}/big.bin",
            tmp_path / "big-cancel.bin",
            expected_size=len(big),
            cancel=Cancel(),
            range_workers=2,
        )
        assert timeouts and all(t == 0.05 for t in timeouts)
    finally:
        srv.close()


def test_stop_polling_raises_in_submission_order(tmp_path, monkeypatch):
    """Simultaneously completed failed futures must raise in deterministic submission order."""
    z = _make_zip({"a.bin": b"alpha" * 200})
    half = len(z) // 2
    big = b"O" * 8192
    srv = Server({"/p.0": z[:half], "/p.1": z[half:], "/big.bin": big}, keepalive=True)
    real_wait = concurrent.futures.wait
    submitted: list[concurrent.futures.Future] = []
    real_submit = concurrent.futures.ThreadPoolExecutor.submit

    def tracking_submit(self, fn, *args, **kwargs):
        fut = real_submit(self, fn, *args, **kwargs)
        submitted.append(fut)
        return fut

    def reversed_wait(fs, timeout=None, return_when=concurrent.futures.ALL_COMPLETED):
        real_wait(fs, timeout=None, return_when=concurrent.futures.ALL_COMPLETED)
        # Return all completed futures in reverse submission order: iterating done_set directly
        # raises the last future's error, while submission-order iteration raises the first.
        ordered = [f for f in reversed(submitted) if f in fs]
        return ordered, set()

    monkeypatch.setattr(concurrent.futures.ThreadPoolExecutor, "submit", tracking_submit)
    monkeypatch.setattr(dd, "wait_futures", reversed_wait)
    monkeypatch.setattr(concurrent.futures, "wait", reversed_wait)
    monkeypatch.setattr(dl, "RANGE_THRESHOLD", 1024)
    try:
        # 1) _sync_archive multi-part: part 0 raises "part-0-err", part 1 raises "part-1-err".
        def failing_download_remote(rf, *args, **kwargs):
            if rf.path == "p.0":
                raise dd.DirectError("part-0-err")
            raise dd.DirectError("part-1-err")

        monkeypatch.setattr(dd, "_download_remote", failing_download_remote)
        plan = dd.Plan(
            "packpipe",
            "1.0",
            archives=(
                dd.Archive(
                    "p",
                    dd.RemoteFile("p.zip", f"{srv.url}/p.0", len(z)),
                    parts=(
                        dd.RemoteFile("p.0", f"{srv.url}/p.0", half),
                        dd.RemoteFile("p.1", f"{srv.url}/p.1", len(z) - half),
                    ),
                ),
            ),
        )
        submitted.clear()
        with pytest.raises(dd.DirectError, match="part-0-err"):
            dd.sync(plan, tmp_path / "mp", workers=2)

        # 2) _download_ranges: worker 0 raises _BeforeWriteRefused("range-0-err"),
        #    worker 1 raises _BeforeWriteRefused("range-1-err").
        submitted.clear()

        def failing_guard(target: Path) -> None:
            if target.name.endswith(".part-0"):
                raise dd.DirectError("range-0-err")
            if target.name.endswith(".part-1"):
                raise dd.DirectError("range-1-err")

        with pytest.raises(dd.DirectError, match="range-0-err"):
            dl.download(
                f"{srv.url}/big.bin",
                tmp_path / "big.bin",
                expected_size=len(big),
                range_workers=2,
                before_write=failing_guard,
            )
    finally:
        srv.close()
