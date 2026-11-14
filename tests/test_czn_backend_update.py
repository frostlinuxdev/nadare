# SPDX-License-Identifier: GPL-3.0-or-later
# ruff: noqa: F811 - tests take the fixtures imported below as arguments
"""czn.update: pure helpers, latest-version resolution, the update entry point, the public index
(split out of test_backends_czn.py, #1248)."""

from __future__ import annotations

import gzip
import hashlib
import http.client
import http.server
import json
import os
import socket
import threading
import time
import tracemalloc
import urllib.parse
from pathlib import Path

import pytest
from _czn_test_helpers import (  # noqa: F401  # noqa: F401 - pytest fixtures, found by name
    _manifest_files,
    _no_runner_downloads,
    _no_vcredist_network,
    _no_webview2_index,
    _reset_czn_process_state,
    _write_matching,
)

from nadare.backends.czn import update as czn_update

# ---------------------------------------------------------------------------
# czn.update — pure helpers
# ---------------------------------------------------------------------------




def _chunk_server(body: bytes, *, interrupt_first_at: int | None = None):
    requests: list[str | None] = []

    class Handler(http.server.BaseHTTPRequestHandler):
        protocol_version = "HTTP/1.1"

        def do_GET(self):  # noqa: N802
            range_header = self.headers.get("Range")
            requests.append(range_header)
            if interrupt_first_at and len(requests) == 1:
                self.send_response(200)
                self.send_header("Content-Length", str(len(body)))
                self.end_headers()
                self.wfile.write(body[:interrupt_first_at])
                self.wfile.flush()
                self.close_connection = True
                try:
                    self.connection.shutdown(socket.SHUT_RDWR)
                except OSError:
                    pass
                self.connection.close()
                return
            start = 0
            if range_header:
                start = int(range_header.removeprefix("bytes=").removesuffix("-"))
                self.send_response(206)
                self.send_header("Content-Range", f"bytes {start}-{len(body) - 1}/{len(body)}")
            else:
                self.send_response(200)
            self.send_header("Content-Length", str(len(body) - start))
            self.end_headers()
            for offset in range(start, len(body), 1 << 18):
                self.wfile.write(body[offset : offset + (1 << 18)])

        def log_message(self, _format, *_args):  # noqa: ANN002, ANN003
            return

    server = http.server.ThreadingHTTPServer(("127.0.0.1", 0), Handler)
    thread = threading.Thread(target=server.serve_forever, daemon=True)
    thread.start()
    return server, thread, f"http://127.0.0.1:{server.server_port}", requests


class _LocalHttpResponse:
    def __init__(self, connection: http.client.HTTPConnection, response) -> None:
        self._connection = connection
        self._response = response
        self._content: bytes | None = None
        self.status_code = response.status
        self.headers = response.headers

    def iter_content(self, chunk_size: int):
        while chunk := self._response.read1(chunk_size):
            yield chunk

    @property
    def content(self) -> bytes:
        if self._content is None:
            self._content = self._response.read()
        return self._content

    def close(self) -> None:
        self._response.close()
        self._connection.close()


class _LocalHttpSession:
    def get(self, url: str, *, headers: dict, timeout: float, stream: bool = False):
        parsed = urllib.parse.urlsplit(url)
        connection = http.client.HTTPConnection(parsed.hostname, parsed.port, timeout=timeout)
        connection.request("GET", parsed.path, headers=headers)
        return _LocalHttpResponse(connection, connection.getresponse())


class TestUpdateHelpers:
    def test_parse_files(self):
        manifest = _manifest_files(
            "0|D|/somedir",
            "1|F|/a.bin|3|chunk0|gz|11|22|" + "a" * 32 + "|" + "b" * 32 + "|x",
            "bad",
        )
        rows = list(czn_update.parse_files(manifest))
        assert rows[0] == ("D", "/somedir", None, None, None, None, None)
        kind, path, vf, ci, md5u, md5p, usize = rows[1]
        assert (kind, path, vf, ci, usize) == ("F", "/a.bin", "3", "chunk0", 11)
        assert (md5u, md5p) == ("a" * 32, "b" * 32)
        assert len(rows) == 2  # "bad" row skipped

    def test_plan_matches_and_missing(self, monkeypatch, tmp_path):
        game_dir = tmp_path / "game"
        monkeypatch.setattr(czn_update, "GAME_DIR", game_dir)
        data = b"hello"
        md5 = _write_matching(game_dir / "a.bin", data)
        manifest = _manifest_files(
            f"0|F|/a.bin|3|c0|g|{len(data)}|9|{md5}|{'d' * 32}|x",
            "1|F|/missing.bin|3|c1|g|5|9|" + "e" * 32 + "|" + "f" * 32 + "|x",
            "2|D|/newdir",
            "3|F|/vcredist/x.bin|3|c|g|1|1|" + "0" * 32 + "|" + "0" * 32 + "|x",
        )
        to_download, dirs = czn_update.plan(manifest)
        assert [p for p, *_ in to_download] == ["/missing.bin"]
        assert dirs == ["/newdir"]

    def test_plan_skips_rehash_for_unchanged_verified_file(self, monkeypatch, tmp_path):
        game_dir = tmp_path / "game"
        cache = tmp_path / "verified-stat.json"
        monkeypatch.setattr(czn_update, "GAME_DIR", game_dir)
        monkeypatch.setattr(czn_update, "STAT_CACHE", cache, raising=False)
        data = b"verified client binary"
        md5 = _write_matching(game_dir / "a.bin", data)
        manifest = _manifest_files(
            f"0|F|/a.bin|3|c0|g|{len(data)}|9|{md5}|{'d' * 32}|x"
        )
        original_hash = czn_update._md5_file
        hashed: list[Path] = []

        def counting_hash(path):
            hashed.append(path)
            return original_hash(path)

        monkeypatch.setattr(czn_update, "_md5_file", counting_hash)
        assert czn_update.plan(manifest)[0] == []
        assert hashed == [game_dir / "a.bin"]
        assert czn_update.plan(manifest)[0] == []
        assert hashed == [game_dir / "a.bin"]

    def test_plan_rehashes_and_catches_changed_file(self, monkeypatch, tmp_path):
        game_dir = tmp_path / "game"
        cache = tmp_path / "verified-stat.json"
        monkeypatch.setattr(czn_update, "GAME_DIR", game_dir)
        monkeypatch.setattr(czn_update, "STAT_CACHE", cache, raising=False)
        data = b"verified client binary"
        md5 = _write_matching(game_dir / "a.bin", data)
        manifest = _manifest_files(
            f"0|F|/a.bin|3|c0|g|{len(data)}|9|{md5}|{'d' * 32}|x"
        )
        assert czn_update.plan(manifest)[0] == []
        time.sleep(0.05)  # kernels with coarse timestamps (< 6.13) tick every few ms
        (game_dir / "a.bin").write_bytes(b"modified client binary")
        changed, _dirs = czn_update.plan(manifest)
        assert [row[0] for row in changed] == ["/a.bin"]
        assert json.loads(cache.read_text())["files"] == {}

    def test_plan_rehashes_same_size_file_even_if_mtime_is_restored(
        self, monkeypatch, tmp_path
    ):
        game_dir = tmp_path / "game"
        monkeypatch.setattr(czn_update, "GAME_DIR", game_dir)
        monkeypatch.setattr(
            czn_update, "STAT_CACHE", tmp_path / "verified-stat.json", raising=False
        )
        original = b"trusted bytes"
        md5 = _write_matching(game_dir / "a.bin", original)
        path = game_dir / "a.bin"
        manifest = _manifest_files(
            f"0|F|/a.bin|3|c0|g|{len(original)}|9|{md5}|{'d' * 32}|x"
        )
        assert czn_update.plan(manifest)[0] == []
        before = path.stat()
        time.sleep(0.05)  # coarse-timestamp kernels: the write must land in a later tick
        path.write_bytes(b"tampered data")
        os.utime(path, ns=(before.st_atime_ns, before.st_mtime_ns))

        changed, _dirs = czn_update.plan(manifest)
        assert [row[0] for row in changed] == ["/a.bin"]
        assert json.loads((tmp_path / "verified-stat.json").read_text())["files"] == {}

    def test_plan_invalidates_stat_fingerprint_when_manifest_hash_changes(
        self, monkeypatch, tmp_path
    ):
        game_dir = tmp_path / "game"
        monkeypatch.setattr(czn_update, "GAME_DIR", game_dir)
        monkeypatch.setattr(
            czn_update, "STAT_CACHE", tmp_path / "verified-stat.json", raising=False
        )
        data = b"verified client binary"
        md5 = _write_matching(game_dir / "a.bin", data)
        manifest = _manifest_files(
            f"0|F|/a.bin|3|c0|g|{len(data)}|9|{md5}|{'d' * 32}|x"
        )
        assert czn_update.plan(manifest)[0] == []
        changed_manifest = _manifest_files(
            f"0|F|/a.bin|3|c0|g|{len(data)}|9|{'0' * 32}|{'d' * 32}|x"
        )
        changed, _dirs = czn_update.plan(changed_manifest)
        assert [row[0] for row in changed] == ["/a.bin"]
        assert json.loads((tmp_path / "verified-stat.json").read_text())["files"] == {}

    def test_plan_rejects_traversal_file(self, monkeypatch, tmp_path):
        game_dir = tmp_path / "game"
        game_dir.mkdir()
        monkeypatch.setattr(czn_update, "GAME_DIR", game_dir)
        manifest = _manifest_files(
            "0|F|/../evil.bin|3|c0|g|5|9|" + "a" * 32 + "|" + "b" * 32 + "|x"
        )
        with pytest.raises(czn_update.ManifestError):
            czn_update.plan(manifest)
        assert not (tmp_path / "evil.bin").exists()

    def test_plan_rejects_traversal_dir(self, monkeypatch, tmp_path):
        game_dir = tmp_path / "game"
        game_dir.mkdir()
        monkeypatch.setattr(czn_update, "GAME_DIR", game_dir)
        with pytest.raises(czn_update.ManifestError):
            czn_update.plan(_manifest_files("0|D|/../evil"))
        assert not (tmp_path / "evil").exists()

    def test_plan_rejects_absolute_and_drive_paths(self, monkeypatch, tmp_path):
        game_dir = tmp_path / "game"
        game_dir.mkdir()
        monkeypatch.setattr(czn_update, "GAME_DIR", game_dir)
        for bad in ("C:/evil.bin", "//evil.bin", r"\evil.bin"):
            manifest = _manifest_files(
                "0|F|" + bad + "|3|c0|g|5|9|" + "a" * 32 + "|" + "b" * 32 + "|x"
            )
            with pytest.raises(czn_update.ManifestError):
                czn_update.plan(manifest)

    def test_local_version(self, monkeypatch, tmp_path):
        upf = tmp_path / "GameManifest.upf"
        upf.write_text(json.dumps({"local_version": 42100}))
        monkeypatch.setattr(czn_update, "UPF", upf)
        assert czn_update.local_version() == 42100

    def test_local_version_missing_is_zero(self, monkeypatch, tmp_path):
        monkeypatch.setattr(czn_update, "UPF", tmp_path / "nope.upf")
        assert czn_update.local_version() == 0

    def test_game_running_false_for_absent_dir(self, monkeypatch, tmp_path):
        monkeypatch.setattr(czn_update, "GAME_DIR", tmp_path / "nope")
        assert czn_update.game_running() is False

    def test_parse_files_rejects_a_malformed_size(self):
        """FRESH-4: a non-numeric usize is a ManifestError, not a ValueError."""
        manifest = _manifest_files(
            "0|F|/a.bin|3|c0|g|abc|9|" + "a" * 32 + "|" + "b" * 32 + "|x"
        )
        with pytest.raises(czn_update.ManifestError):
            list(czn_update.parse_files(manifest))

    def test_cmdline_match_ignores_an_incidental_substring(self):
        """L2: the path only counts at an argv-token boundary."""
        prefix = "z:\\home\\game\\bin\\"
        assert not czn_update._cmdline_has_exe(
            b"editor\0/home/user/notes-z:\\home\\game\\bin\\x", prefix
        )
        assert czn_update._cmdline_has_exe(
            b"Z:\\home\\game\\bin\\shield.exe\0--launch", prefix
        )


class TestResolveLatest:
    def test_index_at_installed_cannot_suppress_updates(self, monkeypatch):
        """NEW-3: an unsigned index must not pin latest at the installed version."""
        monkeypatch.setattr(czn_update, "indexed_version", lambda: 10)
        monkeypatch.setattr(czn_update, "_head_ok", lambda url: True)
        probed: dict[str, int] = {}

        def fake_latest(installed):
            probed["installed"] = installed
            return 12

        monkeypatch.setattr(czn_update, "latest_version", fake_latest)
        assert czn_update.resolve_latest(10) == (12, "probe")
        assert probed == {"installed": 10}

    def test_index_strictly_newer_than_installed_is_used(self, monkeypatch):
        monkeypatch.setattr(czn_update, "indexed_version", lambda: 11)
        monkeypatch.setattr(czn_update, "_head_ok", lambda url: True)

        def _boom(installed):
            raise AssertionError("must not probe when the index is newer")

        monkeypatch.setattr(czn_update, "latest_version", _boom)
        assert czn_update.resolve_latest(10) == (11, "index")


class TestUpdateMain:
    def test_check_without_game_dir_is_offline(self, monkeypatch, tmp_path, capsys):
        monkeypatch.setattr(czn_update, "GAME_DIR", tmp_path / "missing")
        monkeypatch.setattr(czn_update, "PREFIX", tmp_path / "prefix")
        code = czn_update.main(["check"])
        assert code == 5
        assert "game dir not found" in capsys.readouterr().out

    def test_dry_run_up_to_date(self, monkeypatch, tmp_path, capsys):
        game_dir = tmp_path / "game"
        game_dir.mkdir()
        monkeypatch.setattr(czn_update, "GAME_DIR", game_dir)
        monkeypatch.setattr(czn_update, "resolve_latest", lambda installed: (10, "probe"))
        monkeypatch.setattr(czn_update, "fetch_manifest", lambda v: _manifest_files("0|D|/adir"))
        code = czn_update.main(["check"])
        out = capsys.readouterr().out
        assert code == 0
        assert "up to date" in out

    def test_apply_up_to_date_still_bumps_the_version_marker(self, monkeypatch, tmp_path):
        game_dir = tmp_path / "game"
        game_dir.mkdir()
        upf = tmp_path / "upf.json"
        upf.write_text(json.dumps({"local_version": 55, "game_id": "STOVE_CHAOSZERO"}))
        monkeypatch.setattr(czn_update, "GAME_DIR", game_dir)
        monkeypatch.setattr(czn_update, "UPF", upf)
        monkeypatch.setattr(czn_update, "resolve_latest", lambda installed: (56, "index"))
        manifest = _manifest_files("0|D|/adir")
        manifest["version_no"] = 56
        monkeypatch.setattr(czn_update, "fetch_manifest", lambda v: manifest)
        assert czn_update.main(["check"]) == 0
        assert json.loads(upf.read_text())["local_version"] == 55  # dry run writes nothing
        assert czn_update.main(["update", "--apply"]) == 0
        assert json.loads(upf.read_text())["local_version"] == 56

    def test_apply_checks_disk_space_for_total(self, monkeypatch, tmp_path, capsys):
        game_dir = tmp_path / "game"
        game_dir.mkdir()
        monkeypatch.setattr(czn_update, "GAME_DIR", game_dir)
        monkeypatch.setattr(czn_update, "resolve_latest", lambda installed: (10, "probe"))
        monkeypatch.setattr(czn_update, "fetch_manifest", lambda v: _manifest_files("0|D|/adir"))
        monkeypatch.setattr(
            czn_update,
            "plan",
            lambda manifest, **_kw: ([
                ("/a.bin", "3", "c0", "u" * 32, "p" * 32, 1234),
                ("/b.bin", "3", "c0", "u" * 32, "p" * 32, 66),
            ], []),
        )
        monkeypatch.setattr(czn_update, "game_running", lambda: False)
        monkeypatch.setattr(czn_update, "download_file", lambda *a, **k: 1)
        monkeypatch.setattr(czn_update, "UPF", tmp_path / "upf.json")
        calls: list[tuple[Path, int]] = []
        monkeypatch.setattr(
            czn_update.diskspace,
            "check_space",
            lambda path, needed, margin=0.05: calls.append((Path(path), needed)),
        )
        assert czn_update.main(["update", "--apply"]) == 0
        assert calls == [(game_dir, 1300)]

    def test_download_file_writes_bytes(self, monkeypatch, tmp_path):
        game_dir = tmp_path / "game"
        monkeypatch.setattr(czn_update, "GAME_DIR", game_dir)

        def fake_fetch(_path, _vf, _ci, _md5u, _md5p, _usize, _packed_tmp, output_tmp):
            output_tmp.write_bytes(b"payload")
            return len(b"payload")

        monkeypatch.setattr(czn_update, "_fetch_verified_to_file", fake_fetch)
        n = czn_update.download_file("/sub/file.bin", "3", "c0", "u", "p")
        assert n == len(b"payload")
        assert (game_dir / "sub/file.bin").read_bytes() == b"payload"

    def test_download_resumes_a_truncated_chunk_with_range(self, monkeypatch, tmp_path):
        payload = hashlib.shake_256(b"resume test").digest(4 << 20)
        packed = gzip.compress(payload)
        server, thread, base, requests = _chunk_server(
            packed, interrupt_first_at=1 << 20
        )
        game_dir = tmp_path / "game"
        monkeypatch.setattr(czn_update, "_SESS", _LocalHttpSession())
        monkeypatch.setattr(czn_update, "BASE", base)
        monkeypatch.setattr(czn_update, "GAME_DIR", game_dir)
        monkeypatch.setattr(czn_update.time, "sleep", lambda _seconds: None)
        try:
            size = czn_update.download_file(
                "/sub/file.bin",
                "3",
                "chunk",
                hashlib.md5(payload).hexdigest(),
                hashlib.md5(packed).hexdigest(),
                len(payload),
            )
        finally:
            server.shutdown()
            server.server_close()
            thread.join(timeout=2)
        assert size == len(payload)
        assert (game_dir / "sub/file.bin").read_bytes() == payload
        assert requests == [None, f"bytes={1 << 20}-"]
        assert not (game_dir / "sub/file.bin.part").exists()
        assert not (game_dir / "sub/file.bin.part.gz").exists()

    def test_download_stream_memory_stays_below_chunk_size(self, monkeypatch, tmp_path):
        payload = hashlib.shake_256(b"memory test").digest(16 << 20)
        packed = gzip.compress(payload)
        server, thread, base, _requests = _chunk_server(packed)
        game_dir = tmp_path / "game"
        monkeypatch.setattr(czn_update, "_SESS", _LocalHttpSession())
        monkeypatch.setattr(czn_update, "BASE", base)
        monkeypatch.setattr(czn_update, "GAME_DIR", game_dir)
        tracemalloc.start()
        try:
            size = czn_update.download_file(
                "/large.bin",
                "3",
                "large",
                hashlib.md5(payload).hexdigest(),
                hashlib.md5(packed).hexdigest(),
                len(payload),
            )
            _current, peak = tracemalloc.get_traced_memory()
        finally:
            tracemalloc.stop()
            server.shutdown()
            server.server_close()
            thread.join(timeout=2)
        assert size == len(payload)
        assert (game_dir / "large.bin").stat().st_size == len(payload)
        assert peak < 10 << 20

    def test_apply_rejects_a_traversal_manifest_and_writes_nothing(
        self, monkeypatch, tmp_path, capsys
    ):
        game_dir = tmp_path / "game"
        game_dir.mkdir()
        monkeypatch.setattr(czn_update, "GAME_DIR", game_dir)
        monkeypatch.setattr(czn_update, "PREFIX", tmp_path / "prefix")
        monkeypatch.setattr(czn_update, "UPF", tmp_path / "upf.json")
        monkeypatch.setattr(czn_update, "resolve_latest", lambda installed: (10, "probe"))
        monkeypatch.setattr(
            czn_update,
            "fetch_manifest",
            lambda v: _manifest_files(
                "0|F|/../evil.bin|3|c0|g|5|9|" + "a" * 32 + "|" + "b" * 32 + "|x"
            ),
        )
        assert czn_update.main(["update", "--apply"]) == 2
        assert "rejected" in capsys.readouterr().out
        assert not (tmp_path / "evil.bin").exists()

    def test_malformed_manifest_size_is_reported_without_traceback(
        self, monkeypatch, tmp_path, capsys
    ):
        """FRESH-4: a bad usize surfaces through the documented error path."""
        game_dir = tmp_path / "game"
        game_dir.mkdir()
        monkeypatch.setattr(czn_update, "GAME_DIR", game_dir)
        monkeypatch.setattr(czn_update, "PREFIX", tmp_path / "prefix")
        monkeypatch.setattr(czn_update, "resolve_latest", lambda installed: (10, "probe"))
        monkeypatch.setattr(
            czn_update,
            "fetch_manifest",
            lambda v: _manifest_files(
                "0|F|/a.bin|3|c0|g|abc|9|" + "a" * 32 + "|" + "b" * 32 + "|x"
            ),
        )
        assert czn_update.main(["update", "--apply"]) == 2
        assert "rejected" in capsys.readouterr().out


# ---------------------------------------------------------------------------
# update.indexed_version (public index parsing)
# ---------------------------------------------------------------------------

class _FakeResp:
    def __init__(self, body: bytes, status: int = 200) -> None:
        self.content = body
        self.status_code = status


class _FakeSession:
    def __init__(self, body: bytes, status: int = 200) -> None:
        self._resp = _FakeResp(body, status)

    def get(self, *args, **kwargs):  # noqa: ANN002, ANN003
        return self._resp


def test_indexed_version_accepts_matching_service(monkeypatch: pytest.MonkeyPatch) -> None:
    body = json.dumps({"service": "STOVE_CHAOSZERO", "latest": 77}).encode()
    monkeypatch.setenv("CZN_MANIFEST_INDEX", "https://example.test/latest.json")
    monkeypatch.setattr(czn_update, "_SESS", _FakeSession(body))
    assert czn_update.indexed_version() == 77


def test_indexed_version_rejects_wrong_service_or_non_https(
    monkeypatch: pytest.MonkeyPatch,
) -> None:
    body = json.dumps({"service": "OTHER", "latest": 77}).encode()
    monkeypatch.setenv("CZN_MANIFEST_INDEX", "https://example.test/latest.json")
    monkeypatch.setattr(czn_update, "_SESS", _FakeSession(body))
    assert czn_update.indexed_version() is None

    monkeypatch.setenv("CZN_MANIFEST_INDEX", "")
    assert czn_update.indexed_version() is None
