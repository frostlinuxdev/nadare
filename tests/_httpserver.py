# SPDX-License-Identifier: GPL-3.0-or-later
"""A tiny in-process HTTP server for download tests (not a test module)."""

from __future__ import annotations

import threading
from http.server import BaseHTTPRequestHandler, ThreadingHTTPServer


class Server:
    def __init__(self, objects: dict[str, bytes], *, keepalive: bool = False):
        self.objects = objects
        #: TCP connections accepted so far (a keep-alive client keeps this low).
        self.connections = 0
        self.requests: list[str] = []
        self.headers: list[dict[str, str]] = []
        #: ``(path, start, end)`` of every ranged GET, and the bytes sent for GETs.
        self.ranges: list[tuple[str, int, int]] = []
        self.bytes_served = 0
        #: Answer 200 with the whole body even when a Range is asked for.
        self.ignore_range = False
        #: The next N GETs are answered 503 (a flaky CDN).
        self.fail_next = 0
        outer = self

        class H(BaseHTTPRequestHandler):
            protocol_version = "HTTP/1.1" if keepalive else "HTTP/1.0"

            def setup(self):
                outer.connections += 1
                super().setup()

            def log_message(self, *a):  # silence
                pass

            def do_HEAD(self):
                outer.requests.append("HEAD " + self.path)
                body = outer.objects.get(self.path)
                if body is None:
                    self.send_response(404)
                    self.send_header("Content-Length", "0")
                    self.end_headers()
                    return
                self.send_response(200)
                self.send_header("Content-Length", str(len(body)))
                self.end_headers()

            def do_GET(self):
                outer.requests.append(self.path)
                outer.headers.append({k.lower(): v for k, v in self.headers.items()})
                if outer.fail_next > 0:
                    outer.fail_next -= 1
                    self.send_response(503)
                    self.send_header("Content-Length", "0")
                    self.end_headers()
                    return
                body = outer.objects.get(self.path)
                if body is None:
                    self.send_response(404)
                    self.send_header("Content-Length", "0")
                    self.end_headers()
                    return
                rng = self.headers.get("Range")
                if rng and rng.startswith("bytes=") and not outer.ignore_range:
                    first, _, last = rng[6:].partition("-")
                    start = int(first)
                    end = min(int(last), len(body) - 1) if last else len(body) - 1
                    part = body[start : end + 1]
                    outer.ranges.append((self.path, start, end))
                    self.send_response(206)
                    self.send_header("Content-Range", f"bytes {start}-{end}/{len(body)}")
                    self.send_header("Content-Length", str(len(part)))
                    self.end_headers()
                    # Count before writing: the client may finish reading (and a test may
                    # assert on the counter) before this thread runs the next line.
                    outer.bytes_served += len(part)
                    self.wfile.write(part)
                    return
                self.send_response(200)
                self.send_header("Content-Length", str(len(body)))
                self.end_headers()
                outer.bytes_served += len(body)
                self.wfile.write(body)

        self.httpd = ThreadingHTTPServer(("127.0.0.1", 0), H)
        self.url = f"http://127.0.0.1:{self.httpd.server_address[1]}"
        threading.Thread(target=self.httpd.serve_forever, daemon=True).start()

    def close(self):
        self.httpd.shutdown()
        self.httpd.server_close()
