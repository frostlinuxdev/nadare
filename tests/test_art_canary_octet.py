# SPDX-License-Identifier: GPL-3.0-or-later
# Copyright (C) 2026 Nadare contributors
"""The art canary called live art dead: Netmarble (Mongil) and STOVE (CZN) serve their images as
``application/octet-stream`` / ``binary/octet-stream``. The app sniffs the bytes and shows them, so
the canary must too, or a game served that way turns the heartbeat "down" for nothing."""

from __future__ import annotations

import http.server
import importlib.util
import threading
from pathlib import Path

import pytest

SCRIPT = Path(__file__).resolve().parents[1] / "scripts" / "check_art_index.py"
PNG = b"\x89PNG\r\n\x1a\n" + b"\0" * 49_992
WEBP = b"RIFF\0\0\0\0WEBPVP8 " + b"\0" * 49_984
HTML = b"<!doctype html><html>" + b" " * 49_979


def _canary():
    spec = importlib.util.spec_from_file_location("art_index_canary_octet", SCRIPT)
    module = importlib.util.module_from_spec(spec)
    assert spec.loader is not None
    spec.loader.exec_module(module)
    module.BLIND.clear()
    return module


@pytest.fixture
def server():
    routes = {
        "/png": ("application/octet-stream", PNG),
        "/webp": ("binary/octet-stream", WEBP),
        "/html": ("application/octet-stream", HTML),
        "/html-typed": ("text/html", PNG),
    }

    class Handler(http.server.BaseHTTPRequestHandler):
        def log_message(self, *args):
            pass

        def _route(self):
            kind, body = routes.get(self.path, ("text/plain", b""))
            status, sent = 200, body
            ranged = self.headers.get("Range")
            if ranged and self.command == "GET":
                end = int(ranged.split("-")[1])
                sent = body[: end + 1]
                status = 206
            self.send_response(status if body else 404)
            self.send_header("Content-Type", kind)
            self.send_header("Content-Length", str(len(sent)))
            if status == 206:
                self.send_header("Content-Range", f"bytes 0-{len(sent) - 1}/{len(body)}")
            self.end_headers()
            if self.command != "HEAD":
                self.wfile.write(sent)

        do_HEAD = _route
        do_GET = _route

    httpd = http.server.ThreadingHTTPServer(("127.0.0.1", 0), Handler)
    threading.Thread(
        target=httpd.serve_forever, kwargs={"poll_interval": 0.01}, daemon=True
    ).start()
    yield f"http://127.0.0.1:{httpd.server_port}"
    httpd.shutdown()


@pytest.mark.parametrize("path", ["/png", "/webp"])
def test_an_image_served_as_octet_stream_works(server, path):
    canary = _canary()
    answer = canary._head(server + path)
    assert canary._works(answer), answer
    assert answer[2] == 50_000, "the real size is kept"


def test_an_octet_stream_that_is_not_an_image_still_does_not_work(server):
    canary = _canary()
    assert not canary._works(canary._head(server + "/html"))


def test_a_text_type_is_never_sniffed(server):
    canary = _canary()
    assert not canary._works(canary._head(server + "/html-typed"))
