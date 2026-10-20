# SPDX-License-Identifier: GPL-3.0-or-later
"""Integration tests for the parallel Yostar sync, against a local HTTP server.

No internet is involved: a threaded ``http.server`` with Range support stands in
for the Yostar CDN and records concurrency / connection reuse.
"""

from __future__ import annotations

import http.server
import os
import socketserver
import threading
import time
import urllib.parse
from pathlib import Path

import pytest

from nadare.core import yostar
from nadare.core.events import Cancel, Cancelled

_CHUNK = 32 * 1024


class _State:
    def __init__(self, files: dict[str, bytes]) -> None:
        self.files = files
        self.lock = threading.Lock()
        self.connections = 0
        self.requests = 0
        self.inflight = 0
        self.max_inflight = 0
        self.range_requests = 0
        self.corrupt_once: set[str] = set()
        self.delay = 0.0


def _make_server(files: dict[str, bytes], *, delay: float = 0.0, corrupt_once=()):
    state = _State(files)
    state.delay = delay
    state.corrupt_once = set(corrupt_once)

    class Handler(http.server.BaseHTTPRequestHandler):
        protocol_version = "HTTP/1.1"

        def log_message(self, *args: object) -> None:  # silence test output
            pass

        def setup(self) -> None:
            with state.lock:
                state.connections += 1
            super().setup()

        def do_GET(self) -> None:
            with state.lock:
                state.requests += 1
                state.inflight += 1
                state.max_inflight = max(state.max_inflight, state.inflight)
            try:
                self._serve(state)
            except (BrokenPipeError, ConnectionResetError):
                pass
            finally:
                with state.lock:
                    state.inflight -= 1

        def _serve(self, state: _State) -> None:
            name = urllib.parse.urlparse(self.path).path.lstrip("/")
            data = state.files.get(name)
            if data is None:
                self.send_response(404)
                self.send_header("Content-Length", "0")
                self.end_headers()
                return
            range_header = self.headers.get("Range")
            if range_header:
                start = int(range_header.split("=")[1].split("-")[0])
                with state.lock:
                    state.range_requests += 1
                body = data[start:]
                self.send_response(206)
                self.send_header(
                    "Content-Range", f"bytes {start}-{len(data) - 1}/{len(data)}"
                )
            else:
                body = bytearray(data)
                with state.lock:
                    corrupt = name in state.corrupt_once
                    state.corrupt_once.discard(name)
                if corrupt and body:
                    body[0] ^= 0xFF
                body = bytes(body)
                self.send_response(200)
            self.send_header("Content-Length", str(len(body)))
            self.send_header("Content-Type", "application/octet-stream")
            self.end_headers()
            for offset in range(0, len(body), _CHUNK):
                self.wfile.write(body[offset : offset + _CHUNK])
                self.wfile.flush()
                if state.delay:
                    time.sleep(state.delay)

    server = socketserver.ThreadingTCPServer(("127.0.0.1", 0), Handler)
    server.daemon_threads = True
    thread = threading.Thread(target=server.serve_forever, daemon=True)
    thread.start()
    return server, state


@pytest.fixture
def local_server():
    servers: list[socketserver.ThreadingTCPServer] = []

    def start(files, **kwargs):
        server, state = _make_server(files, **kwargs)
        servers.append(server)
        return server, state

    yield start
    for server in servers:
        server.shutdown()
        server.server_close()


def _base_url(server) -> str:
    host, port = server.server_address[0], server.server_address[1]
    return f"http://{host}:{port}"


def _manifest(files: dict[str, bytes], base_url: str) -> yostar.Manifest:
    return yostar.Manifest(
        game_tag="t",
        version="1",
        base_url=base_url,
        generated_at="",
        files=tuple(
            yostar.ManifestFile(path, len(data), yostar.crc64(data))
            for path, data in files.items()
        ),
    )


def _random_files(count: int, size: int) -> dict[str, bytes]:
    return {f"small/{i:02d}.bin": os.urandom(size + i) for i in range(count)}


def test_parallel_downloads_and_verifies(local_server, tmp_path: Path) -> None:
    files = _random_files(40, 8 * 1024)
    files["large/a.bin"] = os.urandom(1_500_000)
    files["large/b.bin"] = os.urandom(1_200_000)
    server, state = local_server(files, delay=0.001)
    manifest = _manifest(files, _base_url(server))

    summary = yostar.sync(manifest, tmp_path, workers=6)

    assert summary["downloaded"] == len(files)
    for path, data in files.items():
        assert (tmp_path / path).read_bytes() == data
    assert state.max_inflight > 1, "workers did not overlap requests"


def test_keep_alive_reuses_connections(local_server, tmp_path: Path) -> None:
    files = _random_files(30, 2048)
    server, state = local_server(files)
    manifest = _manifest(files, _base_url(server))

    yostar.sync(manifest, tmp_path, workers=2)

    assert state.requests == len(files)
    assert state.connections < state.requests
    assert state.connections <= 2


def test_resume_uses_range_and_verifies(local_server, tmp_path: Path) -> None:
    payload = os.urandom(400_000)
    files = {"resume.bin": payload}
    (tmp_path / "resume.bin.part").write_bytes(payload[:150_000])
    server, state = local_server(files, delay=0.001)
    manifest = _manifest(files, _base_url(server))

    yostar.sync(manifest, tmp_path, workers=1)

    assert state.range_requests >= 1
    assert (tmp_path / "resume.bin").read_bytes() == payload


def test_corrupt_byte_is_retried(local_server, tmp_path: Path) -> None:
    payload = os.urandom(120_000)
    files = {"corrupt.bin": payload}
    server, state = local_server(files, corrupt_once={"corrupt.bin"})
    manifest = _manifest(files, _base_url(server))

    yostar.sync(manifest, tmp_path, workers=1, retries=2)

    assert (tmp_path / "corrupt.bin").read_bytes() == payload
    assert state.requests >= 2


def test_cancel_stops_quickly_and_keeps_part(local_server, tmp_path: Path) -> None:
    files = {f"c/{i}.bin": os.urandom(300_000) for i in range(20)}
    server, state = local_server(files, delay=0.02)
    manifest = _manifest(files, _base_url(server))
    cancel = Cancel()
    result: dict[str, BaseException] = {}

    def run() -> None:
        try:
            yostar.sync(manifest, tmp_path, workers=2, cancel=cancel)
        except BaseException as exc:  # noqa: BLE001 - captured for assertions
            result["error"] = exc

    worker = threading.Thread(target=run)
    worker.start()
    deadline = time.time() + 5
    while time.time() < deadline and state.requests < 1:
        time.sleep(0.01)
    time.sleep(0.15)
    started = time.time()
    cancel.cancel()
    worker.join(timeout=2)

    assert not worker.is_alive(), "sync did not stop within 2s of cancel"
    assert time.time() - started < 2
    assert isinstance(result.get("error"), Cancelled)
    assert any(p.name.endswith(".part") for p in tmp_path.rglob("*"))


def test_progress_events_carry_speed_and_eta(local_server, tmp_path: Path) -> None:
    files = {f"p/{i}.bin": os.urandom(400_000) for i in range(6)}
    server, state = local_server(files, delay=0.01)
    manifest = _manifest(files, _base_url(server))
    events: list = []

    yostar.sync(manifest, tmp_path, workers=2, emit=events.append)

    sync_events = [
        e for e in events if e.data.get("stage") == "sync" and "speed_bps" in e.data
    ]
    assert sync_events
    assert all("eta_s" in e.data for e in sync_events)
    assert sync_events[-1].data["done"] == sum(len(d) for d in files.values())
    assert sync_events[-1].progress == 1.0
    assert any(e.data["speed_bps"] > 0 for e in sync_events)


def test_progress_is_rate_limited(monkeypatch) -> None:
    clock = [0.0]
    monkeypatch.setattr(time, "monotonic", lambda: clock[0])
    events: list = []
    progress = yostar._SyncProgress(events.append, 1000.0, interval=0.25)
    for step in range(1000):
        clock[0] += 0.001
        progress.set("f", step + 1)
    progress.flush()
    assert events
    assert len(events) <= 6  # ~1s of work at a 250 ms cadence
    assert all("speed_bps" in e.data and "eta_s" in e.data for e in events)


def test_untrusted_per_file_host_is_rejected(local_server, tmp_path: Path, monkeypatch) -> None:
    payload = b"data"
    files = {"x.bin": payload}
    server, _state = local_server(files)
    manifest = yostar.Manifest(
        game_tag="t",
        version="1",
        base_url=_base_url(server),
        generated_at="",
        files=(yostar.ManifestFile("x.bin", len(payload), yostar.crc64(payload)),),
    )
    # Pretend the manifest points elsewhere so the per-file host check fails.
    monkeypatch.setattr(yostar, "file_url", lambda _m, _e: "https://evil.example/x.bin")
    with pytest.raises(yostar.ManifestError):
        yostar.sync(manifest, tmp_path, workers=1)


def test_plan_native_crc_in_pool(tmp_path: Path) -> None:
    files = {f"f/{i}.bin": os.urandom(16_384) for i in range(20)}
    for path, data in files.items():
        target = tmp_path / path
        target.parent.mkdir(parents=True, exist_ok=True)
        target.write_bytes(data)
    manifest = _manifest(files, "http://127.0.0.1:1")
    assert yostar.plan(manifest, tmp_path, workers=4) == []
