# SPDX-License-Identifier: GPL-3.0-or-later
"""Helpers for the hostile-download chaos tests (not a test module)."""

from __future__ import annotations

import hashlib
import threading
from http.server import BaseHTTPRequestHandler, ThreadingHTTPServer


class ObjectServer:
    """A tiny loopback object server for hostile download probes."""

    def __init__(self, objects: dict[str, bytes]) -> None:
        self.objects = objects
        self.requests: list[str] = []
        outer = self

        class Handler(BaseHTTPRequestHandler):
            protocol_version = "HTTP/1.1"

            def log_message(self, *args):  # noqa: D401 - silence
                pass

            def do_GET(self):  # noqa: N802
                outer.requests.append(self.path)
                body = outer.objects.get(self.path)
                if body is None:
                    self.send_response(404)
                    self.send_header("Content-Length", "0")
                    self.end_headers()
                    return
                self.send_response(200)
                self.send_header("Content-Length", str(len(body)))
                self.end_headers()
                self.wfile.write(body)

        self.httpd = ThreadingHTTPServer(("127.0.0.1", 0), Handler)
        self.url = f"http://127.0.0.1:{self.httpd.server_address[1]}"
        threading.Thread(target=self.httpd.serve_forever, daemon=True).start()

    def close(self) -> None:
        self.httpd.shutdown()
        self.httpd.server_close()


# ---- the smallest Sophon manifest builder needed by the collision test ---------
def varint(n: int) -> bytes:
    out = bytearray()
    while True:
        b = n & 0x7F
        n >>= 7
        if n:
            out.append(b | 0x80)
        else:
            out.append(b)
            return bytes(out)


def field_varint(num: int, value: int) -> bytes:
    return varint(num << 3) + varint(value)


def field_bytes(num: int, value: bytes | str) -> bytes:
    raw = value.encode() if isinstance(value, str) else value
    return varint((num << 3) | 2) + varint(len(raw)) + raw


def md5(data: bytes) -> str:
    return hashlib.md5(data).hexdigest()


class SophonBuild:
    """One-file Sophon build: a zstd manifest and its chunk objects."""

    def __init__(self, files: dict[str, bytes], chunk_size: int = 100) -> None:
        import zstandard

        self.objects: dict[str, bytes] = {}
        entries = b""
        for name, data in files.items():
            chunks = b""
            for off in range(0, max(len(data), 1), chunk_size):
                part = data[off : off + chunk_size]
                if not part:
                    continue
                comp = zstandard.ZstdCompressor().compress(part)
                cname = f"{md5(part)[:16]}_{md5(part)}"
                self.objects["/chunks/" + cname] = comp
                chunks += field_bytes(
                    2,
                    field_bytes(1, cname)
                    + field_bytes(2, md5(part))
                    + field_varint(3, off)
                    + field_varint(4, len(comp))
                    + field_varint(5, len(part))
                    + field_varint(6, 12345)
                    + field_bytes(7, md5(comp)),
                )
            entries += field_bytes(
                1,
                field_bytes(1, name)
                + chunks
                + field_varint(4, len(data))
                + field_bytes(5, md5(data)),
            )
        self.objects["/manifests/m1"] = zstandard.ZstdCompressor().compress(entries)
        self.server: ObjectServer | None = None

    def serve(self) -> ObjectServer:
        self.server = ObjectServer(self.objects)
        return self.server

    def package(self, size: int) -> object:
        from nadare.core import sophon

        assert self.server is not None
        return sophon.SophonPackage(
            name="game",
            manifest_url=self.server.url + "/manifests/m1",
            chunk_url=self.server.url + "/chunks",
            manifest_id="m1",
            size=size,
        )
