#!/usr/bin/env python3
# SPDX-License-Identifier: GPL-3.0-or-later
# Copyright (C) 2026 Nadare contributors
"""Micro-benchmark for the parallel Yostar sync (no internet).

Serves 500 x 256 KiB in-memory files from a local thread and prints the sync
throughput for a single worker versus the default six.  Run from the repo root:

    python scripts/bench_sync.py
"""

from __future__ import annotations

import http.server
import os
import socketserver
import sys
import tempfile
import threading
import time
import urllib.parse
from pathlib import Path

_ROOT = Path(__file__).resolve().parents[1]
sys.path.insert(0, str(_ROOT / "src"))

from nadare.core import yostar  # noqa: E402

FILE_COUNT = 500
FILE_SIZE = 256 * 1024
CHUNK = 64 * 1024
# Loopback has ~0 RTT, so without a little latency the benchmark measures the
# Python test server's own throughput rather than the connection setup the
# parallel path removes.  5 ms models a plausible WAN round trip.
LATENCY = 0.005


class _Handler(http.server.BaseHTTPRequestHandler):
    protocol_version = "HTTP/1.1"
    files: dict[str, bytes] = {}

    def log_message(self, *args: object) -> None:
        pass

    def do_GET(self) -> None:
        time.sleep(LATENCY)
        name = urllib.parse.urlparse(self.path).path.lstrip("/")
        data = self.files[name]
        range_header = self.headers.get("Range")
        if range_header:
            start = int(range_header.split("=")[1].split("-")[0])
            body = data[start:]
            self.send_response(206)
            self.send_header("Content-Range", f"bytes {start}-{len(data) - 1}/{len(data)}")
        else:
            body = data
            self.send_response(200)
        self.send_header("Content-Length", str(len(body)))
        self.end_headers()
        for offset in range(0, len(body), CHUNK):
            self.wfile.write(body[offset : offset + CHUNK])


def main() -> int:
    files = {f"file/{i:04d}.bin": os.urandom(FILE_SIZE) for i in range(FILE_COUNT)}
    _Handler.files = files
    server = socketserver.ThreadingTCPServer(("127.0.0.1", 0), _Handler)
    server.daemon_threads = True
    threading.Thread(target=server.serve_forever, daemon=True).start()
    base_url = f"http://127.0.0.1:{server.server_address[1]}"
    manifest = yostar.Manifest(
        game_tag="bench",
        version="1",
        base_url=base_url,
        generated_at="",
        files=tuple(
            yostar.ManifestFile(path, len(data), yostar.crc64(data))
            for path, data in files.items()
        ),
    )
    total_mib = FILE_COUNT * FILE_SIZE / (1024 * 1024)
    try:
        for workers in (1, 6):
            with tempfile.TemporaryDirectory() as tmp:
                started = time.perf_counter()
                yostar.sync(manifest, tmp, workers=workers)
                elapsed = time.perf_counter() - started
            print(f"workers={workers}: {total_mib / elapsed:6.1f} MiB/s ({elapsed:.2f}s)")
    finally:
        server.shutdown()
        server.server_close()
    return 0


if __name__ == "__main__":
    raise SystemExit(main())
