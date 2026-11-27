# SPDX-License-Identifier: GPL-3.0-or-later
"""Tests for #1853 fetch-retry leftovers (items A, B, D, E).

All socket fixtures bind to 127.0.0.1 only; no external network is ever touched.
"""

from __future__ import annotations

import errno
import http.client
import json
import socket
import threading
import time
import urllib.error
from http.server import BaseHTTPRequestHandler, ThreadingHTTPServer
from pathlib import Path

import pytest

from nadare.core import directdl, download, sophon, sources, transfer
from nadare.core.events import Cancel, Cancelled
from nadare.core.sources import r1999

FIX = Path(__file__).parent / "fixtures" / "catalog"
URL = "https://cdn.example.invalid/api/index.json"


# --------------------------------------------------------------------------- helpers
def _exc_chain(exc: BaseException) -> list[BaseException]:
    chain: list[BaseException] = []
    seen: set[int] = set()
    stack: list[BaseException | None] = [exc]
    while stack:
        cur = stack.pop()
        if cur is None or id(cur) in seen:
            continue
        seen.add(id(cur))
        chain.append(cur)
        stack.append(cur.__cause__)
        stack.append(cur.__context__)
        if isinstance(cur, urllib.error.URLError) and isinstance(cur.reason, BaseException):
            stack.append(cur.reason)
    return chain


def _assert_no_secrets(exc: BaseException) -> None:
    forbidden = ("u:p", "p@", "q=1", "#frag", "frag")
    for cur in _exc_chain(exc):
        texts = [str(cur)]
        for attr in ("url", "filename", "filename2", "msg", "strerror"):
            val = getattr(cur, attr, None)
            if isinstance(val, str):
                texts.append(val)
        reason = getattr(cur, "reason", None)
        if isinstance(reason, str):
            texts.append(reason)
        for text in texts:
            for secret in forbidden:
                assert secret not in text, f"{secret!r} leaked in {type(cur).__name__}: {text!r}"


def _unreachable(url: str, inner: BaseException) -> download.DownloadError:
    exc = download.DownloadError(f"cannot reach {url}: {inner}")
    exc.__cause__ = urllib.error.URLError(inner)
    return exc


class RawServer:
    """Minimal raw 127.0.0.1 TCP server executing a script of handler functions."""

    def __init__(self, handlers):
        self._handlers = list(handlers)
        self.received: list[bytes] = []
        self._server = socket.socket()
        self._server.setsockopt(socket.SOL_SOCKET, socket.SO_REUSEADDR, 1)
        self._server.bind(("127.0.0.1", 0))
        self._server.listen(8)
        self._server.settimeout(5)
        self.port = self._server.getsockname()[1]
        self._stop = threading.Event()
        self._thread = threading.Thread(target=self._serve, daemon=True)
        self._thread.start()

    @property
    def url(self) -> str:
        return f"http://127.0.0.1:{self.port}"

    def _serve(self) -> None:
        while not self._stop.is_set() and self._handlers:
            try:
                conn, _addr = self._server.accept()
            except OSError:
                return
            handler = self._handlers.pop(0)
            threading.Thread(target=self._run_one, args=(conn, handler), daemon=True).start()
        self._server.close()

    def _run_one(self, conn: socket.socket, handler) -> None:
        with conn:
            conn.settimeout(5)
            try:
                raw = b""
                while b"\r\n\r\n" not in raw:
                    chunk = conn.recv(65536)
                    if not chunk:
                        break
                    raw += chunk
                head, _, body = raw.partition(b"\r\n\r\n")
                need = 0
                for line in head.split(b"\r\n")[1:]:
                    if line.lower().startswith(b"content-length:"):
                        need = int(line.split(b":", 1)[1].strip())
                while len(body) < need:
                    chunk = conn.recv(65536)
                    if not chunk:
                        break
                    body += chunk
                full = head + b"\r\n\r\n" + body if b"\r\n\r\n" in raw else raw
                self.received.append(full)
                handler(conn, full)
            except OSError:
                pass

    def close(self) -> None:
        self._stop.set()
        self._server.close()


class LocalPostServer:
    """In-process localhost HTTP server supporting GET and POST with programmable replies."""

    def __init__(self, responder):
        self._responder = responder
        self.calls: list[tuple[str, str, dict[str, str], bytes]] = []
        outer = self

        class Handler(BaseHTTPRequestHandler):
            protocol_version = "HTTP/1.1"

            def log_message(self, *args):
                pass

            def _handle(self, method: str) -> None:
                length = int(self.headers.get("Content-Length") or "0")
                body = self.rfile.read(length) if length > 0 else b""
                hdrs = {k.lower(): v for k, v in self.headers.items()}
                outer.calls.append((method, self.path, hdrs, body))
                status, resp_headers, resp_body = outer._responder(method, self.path, hdrs, body)
                self.send_response(status)
                for k, v in resp_headers.items():
                    self.send_header(k, v)
                if "Content-Length" not in resp_headers and "content-length" not in {
                    k.lower() for k in resp_headers
                }:
                    self.send_header("Content-Length", str(len(resp_body)))
                self.end_headers()
                if resp_body:
                    self.wfile.write(resp_body)

            def do_GET(self) -> None:
                self._handle("GET")

            def do_POST(self) -> None:
                self._handle("POST")

        self.httpd = ThreadingHTTPServer(("127.0.0.1", 0), Handler)
        self.port = self.httpd.server_address[1]
        self.url = f"http://127.0.0.1:{self.port}"
        threading.Thread(
            target=self.httpd.serve_forever, kwargs={"poll_interval": 0.01}, daemon=True
        ).start()

    def close(self) -> None:
        self.httpd.shutdown()
        self.httpd.server_close()


# --------------------------------------------------------------------------- A: scrubbing
def test_direct_error_scrubs_userinfo_query_and_fragment():
    exc = directdl.DirectError("failed at https://u:p@host/x?q=1#frag")
    assert str(exc) == "failed at https://host/x"
    _assert_no_secrets(exc)


def test_redirect_to_secret_url_scrubbed_in_direct_error_and_causes():
    target = "https://u:p@host/x?q=1#frag"

    def respond(method, path, headers, body):
        return 302, {"Location": target}, b""

    server = LocalPostServer(respond)
    try:
        with pytest.raises(download.RedirectRefused) as dl_err:
            download.http_get(
                server.url + "/start?q=1#frag",
                redirect_allowed=lambda u: False,
            )
        _assert_no_secrets(dl_err.value)
        assert "https://host/x" in str(dl_err.value)

        with pytest.raises(directdl.DirectError) as caught:
            sources.default_fetch(
                server.url + "/start?q=1#frag",
                redirect_allowed=lambda u: False,
            )
    finally:
        server.close()

    _assert_no_secrets(caught.value)
    assert "https://host/x" in str(caught.value)


def test_followed_redirect_http_error_scrubs_cause_chain():
    def respond(method, path, headers, body):
        if path.startswith("/start-404"):
            return 302, {"Location": f"http://127.0.0.1:{server.port}/x?q=1#frag"}, b""
        return 404, {}, b"not found"

    server = LocalPostServer(respond)
    try:
        # Followed redirect ending in HTTP 404 (HTTPError carries redirected url/filename)
        with pytest.raises(directdl.DirectError) as status_err:
            sources.default_fetch(server.url + "/start-404")
        assert status_err.value.__cause__ is not None
        assert isinstance(status_err.value.__cause__.__cause__, urllib.error.HTTPError)
        _assert_no_secrets(status_err.value)
    finally:
        server.close()


def test_inner_cause_with_secret_url_is_scrubbed_in_download_and_direct_error(monkeypatch):
    secret_url = "https://u:p@host/x?q=1#frag"

    class Opener:
        def open(self, req, timeout=None):
            raise urllib.error.URLError(
                OSError(errno.EINVAL, f"bad {secret_url}", secret_url),
                secret_url,
            )

    monkeypatch.setattr(download.urllib.request, "build_opener", lambda *a, **k: Opener())
    monkeypatch.setattr(download, "DEFAULT_RETRIES", 1)

    with pytest.raises(directdl.DirectError) as caught:
        sources.default_fetch(secret_url)

    assert caught.value.__cause__ is not None
    assert caught.value.__cause__.__cause__ is not None
    _assert_no_secrets(caught.value)
    assert "https://host/x" in str(caught.value)


# --------------------------------------------------------------------------- B: post_fetch
@pytest.mark.parametrize(
    "failure",
    [
        download.HTTPStatusError("HTTP 503 for " + URL, status=503, url=URL),
        download.HTTPStatusError("HTTP 500 for " + URL, status=500, url=URL),
        download.HTTPStatusError("HTTP 429 for " + URL, status=429, url=URL),
        _unreachable(URL, TimeoutError("timed out")),
        _unreachable(URL, ConnectionResetError(104, "Connection reset by peer")),
        _unreachable(URL, ConnectionAbortedError(103, "aborted")),
        http.client.IncompleteRead(b"half", 10),
        TimeoutError("mid-body timeout"),
    ],
)
def test_post_fetch_does_not_retry_post_send_failures_by_default(monkeypatch, failure):
    calls: list[str] = []
    waits: list[float] = []

    def fake_post(url, body, **kwargs):
        calls.append(url)
        raise failure

    monkeypatch.setattr(download, "http_post", fake_post)
    monkeypatch.setattr(download, "wait", lambda s, c: waits.append(s))
    monkeypatch.setattr(download, "DEFAULT_RETRIES", 3)

    with pytest.raises(directdl.DirectError):
        sources.post_fetch(URL, b'{"a": 1}')
    assert len(calls) == 1
    assert waits == []


@pytest.mark.parametrize(
    "inner",
    [
        ConnectionRefusedError(111, "Connection refused"),
        socket.gaierror(socket.EAI_AGAIN, "Temporary failure in name resolution"),
        OSError(errno.ENETUNREACH, "Network is unreachable"),
        OSError(errno.EHOSTUNREACH, "No route to host"),
    ],
)
def test_post_fetch_retries_pre_send_failures_by_default(monkeypatch, inner):
    calls: list[str] = []
    waits: list[float] = []

    def fake_post(url, body, **kwargs):
        calls.append(url)
        if len(calls) == 1:
            raise _unreachable(url, inner)
        return b"ok"

    monkeypatch.setattr(download, "http_post", fake_post)
    monkeypatch.setattr(download, "wait", lambda s, c: waits.append(s))
    monkeypatch.setattr(download, "DEFAULT_RETRIES", 3)

    assert sources.post_fetch(URL, b'{"a": 1}') == b"ok"
    assert len(calls) == 2
    assert len(waits) == 1


def test_post_fetch_retries_before_send_connect_timeout_when_marked(monkeypatch):
    calls: list[str] = []
    waits: list[float] = []

    def fake_post(url, body, **kwargs):
        calls.append(url)
        if len(calls) == 1:
            exc = _unreachable(url, TimeoutError("connect timed out"))
            exc.before_send = True  # type: ignore[attr-defined]
            raise exc
        return b"ok"

    monkeypatch.setattr(download, "http_post", fake_post)
    monkeypatch.setattr(download, "wait", lambda s, c: waits.append(s))

    assert sources.post_fetch(URL, b'{"a": 1}') == b"ok"
    assert len(calls) == 2
    assert len(waits) == 1


@pytest.mark.parametrize(
    "failure",
    [
        download.HTTPStatusError("HTTP 503 for " + URL, status=503, url=URL),
        http.client.IncompleteRead(b"half", 10),
        _unreachable(URL, TimeoutError("timed out")),
    ],
)
def test_post_fetch_idempotent_true_retries_post_send_failures(monkeypatch, failure):
    calls: list[str] = []
    waits: list[float] = []

    def fake_post(url, body, **kwargs):
        calls.append(url)
        if len(calls) == 1:
            raise failure
        return b"ok"

    monkeypatch.setattr(download, "http_post", fake_post)
    monkeypatch.setattr(download, "wait", lambda s, c: waits.append(s))

    assert sources.post_fetch(URL, b'{"a": 1}', idempotent=True) == b"ok"
    assert len(calls) == 2
    assert len(waits) == 1


def test_post_fetch_real_localhost_does_not_retry_after_server_receives_post(monkeypatch):
    monkeypatch.setattr(download, "wait", lambda s, c: None)
    monkeypatch.setattr(download, "DEFAULT_RETRIES", 3)

    # 1. Server receives POST and drops the connection (post-send failure)
    raw_server = RawServer([lambda conn, req: conn.close(), lambda conn, req: conn.close()])
    try:
        with pytest.raises(directdl.DirectError):
            sources.post_fetch(raw_server.url + "/submit", b"once")
        assert len(raw_server.received) == 1
    finally:
        raw_server.close()

    # 2. Server returns 503 on first POST, 200 on second: default does NOT retry; idempotent DOES
    attempts = {"n": 0}

    def respond(method, path, headers, body):
        attempts["n"] += 1
        if attempts["n"] in (1, 2):
            return 503, {"Retry-After": "0"}, b"busy"
        return 200, {}, b"done"

    server = LocalPostServer(respond)
    try:
        with pytest.raises(directdl.DirectError):
            sources.post_fetch(server.url + "/submit", b"once")
        assert len(server.calls) == 1

        assert sources.post_fetch(server.url + "/query", b"safe", idempotent=True) == b"done"
        assert len(server.calls) == 3  # 1 (above) + 2 (503 then 200)
    finally:
        server.close()


def test_post_fetch_real_localhost_retries_connection_refused_before_send(monkeypatch):
    waits: list[float] = []
    monkeypatch.setattr(download, "wait", lambda s, c: waits.append(s))
    monkeypatch.setattr(download, "DEFAULT_RETRIES", 2)

    sock = socket.socket()
    sock.bind(("127.0.0.1", 0))
    closed_port = sock.getsockname()[1]
    sock.close()

    with pytest.raises(directdl.DirectError):
        sources.post_fetch(f"http://127.0.0.1:{closed_port}/submit", b"once")
    assert len(waits) == 1


# --------------------------------------------------------------------------- D: real transport
def test_real_transport_inflight_stop_via_fetch_cancel_contextvar():
    stop_server = threading.Event()

    def stall(conn, req):
        conn.sendall(b"HTTP/1.0 200 OK\r\nContent-Length: 1000\r\n\r\n")
        stop_server.wait(5)

    server = RawServer([stall])
    token = Cancel()
    timer = threading.Timer(0.25, token.cancel)
    timer.start()
    started = time.monotonic()
    try:
        with sources.fetch_cancel(token):
            with pytest.raises(Cancelled):
                sources.default_fetch(server.url + "/index.json")
        assert time.monotonic() - started < 1.5
    finally:
        timer.cancel()
        stop_server.set()
        server.close()


def test_real_transport_http_post_roundtrip_and_inflight_stop():
    def respond(method, path, headers, body):
        if path == "/busy":
            return 503, {"Retry-After": "4"}, b"later"
        if path == "/big":
            return 200, {}, b"0123456789"
        return (
            200,
            {"Content-Type": "application/json"},
            json.dumps(
                {"method": method, "lang": headers.get("lang"), "echo": body.decode()}
            ).encode(),
        )

    server = LocalPostServer(respond)
    try:
        reply = download.http_post(
            server.url + "/echo",
            b"hello-post",
            headers={"lang": "en", "Content-Type": "text/plain"},
            timeout=5,
        )
        assert json.loads(reply) == {"method": "POST", "lang": "en", "echo": "hello-post"}

        with pytest.raises(download.HTTPStatusError) as status_err:
            download.http_post(server.url + "/busy", b"{}", timeout=5)
        assert status_err.value.status == 503
        assert status_err.value.retry_after == 4.0

        with pytest.raises(download.DownloadError, match="too large"):
            download.http_post(server.url + "/big", b"{}", max_bytes=4, timeout=5)
    finally:
        server.close()

    stop_server = threading.Event()

    def stall_post(conn, req):
        assert req.startswith(b"POST ")
        conn.sendall(b"HTTP/1.0 200 OK\r\nContent-Length: 1000\r\n\r\n")
        stop_server.wait(5)

    slow = RawServer([stall_post])
    token = Cancel()
    timer = threading.Timer(0.25, token.cancel)
    timer.start()
    started = time.monotonic()
    try:
        with pytest.raises(Cancelled):
            download.http_post(slow.url + "/slow", b"body", cancel=token, timeout=15)
        assert time.monotonic() - started < 1.5
    finally:
        timer.cancel()
        stop_server.set()
        slow.close()


def test_real_transport_r1999_resolve_and_redirect_guard(monkeypatch):
    version_doc = (FIX / "r1999_version.json").read_bytes()
    resource_doc = (FIX / "r1999_resource.json").read_bytes()
    version_calls = {"n": 0}

    def respond(method, path, headers, body):
        if path == "/diff-update/version":
            version_calls["n"] += 1
            if version_calls["n"] == 1:
                return 503, {"Retry-After": "0"}, b"busy"
            return 200, {"Content-Type": "application/json"}, version_doc
        if path == "/diff-update/resource":
            return 200, {"Content-Type": "application/json"}, resource_doc
        if path == "/diff-update/redir":
            return 302, {"Location": "https://evil.example.invalid/x"}, b""
        return 404, {}, b""

    server = LocalPostServer(respond)
    monkeypatch.setattr(r1999, "API", server.url + "/diff-update")
    try:
        plan = r1999.resolve()
        assert plan.game_tag == "r1999"
        assert plan.version == "3.9.1"
        assert len(plan.archives) == 3
        assert [(m, p) for m, p, _, _ in server.calls] == [
            ("POST", "/diff-update/version"),
            ("POST", "/diff-update/version"),
            ("POST", "/diff-update/resource"),
        ]
        assert json.loads(server.calls[0][3])["gameId"] == "60001"
        assert json.loads(server.calls[2][3])["targetVersion"] == "3.9.1"

        with pytest.raises(directdl.DirectError, match="disallowed"):
            r1999.default_post(server.url + "/diff-update/redir", b"{}", {})
    finally:
        server.close()


# --------------------------------------------------------------------------- E: early EOF / short
def test_transfer_response_raises_on_early_eof_after_blocks():
    def truncated(conn, req):
        conn.sendall(b"HTTP/1.1 200 OK\r\nContent-Length: 100\r\n\r\n" + b"a" * 40)
        conn.close()

    server = RawServer([truncated, truncated])
    try:
        # Unbounded read() after collecting initial blocks
        with download.http_open(server.url + "/short", timeout=5) as resp:
            with pytest.raises(http.client.IncompleteRead):
                resp.read()

        # Bounded read() in two steps: first succeeds, second hits early EOF
        with download.http_open(server.url + "/short", timeout=5) as resp:
            assert resp.read(20) == b"a" * 20
            with pytest.raises(http.client.IncompleteRead):
                resp.read(80)
    finally:
        server.close()

    # Stub response with Content-Length header returning blocks then EOF early
    class StubShort:
        status = 200
        headers = {"Content-Length": "100"}

        def __init__(self):
            self._chunks = [b"x" * 20, b"y" * 20, b""]

        def read(self, amount=-1):
            return self._chunks.pop(0) if self._chunks else b""

    wrapped = transfer.Response(StubShort(), "http://127.0.0.1/stub")
    with pytest.raises(http.client.IncompleteRead):
        wrapped.read()


def test_http_get_post_and_default_fetch_reject_short_body_vs_content_length(monkeypatch):
    def truncated(conn, req):
        conn.sendall(b"HTTP/1.1 200 OK\r\nContent-Length: 100\r\n\r\n" + b"a" * 40)
        conn.close()

    def complete(conn, req):
        conn.sendall(b"HTTP/1.1 200 OK\r\nContent-Length: 100\r\n\r\n" + b"b" * 100)
        conn.close()

    server = RawServer([truncated, truncated, truncated, complete])
    monkeypatch.setattr(download, "wait", lambda s, c: None)
    try:
        with pytest.raises(download.DownloadError) as get_err:
            download.http_get(server.url + "/get", timeout=5)
        assert isinstance(get_err.value.__cause__, http.client.IncompleteRead)

        with pytest.raises(download.DownloadError) as post_err:
            download.http_post(server.url + "/post", b"{}", timeout=5)
        assert isinstance(post_err.value.__cause__, http.client.IncompleteRead)

        # Retryable through default_fetch: attempt 1 truncated, attempt 2 full
        assert sources.default_fetch(server.url + "/retry") == b"b" * 100
    finally:
        server.close()

    # Non-transfer.Response stub with Content-Length mismatch
    class PlainShort:
        status = 200
        headers = {"Content-Length": "100"}

        def __init__(self):
            self._left = b"z" * 10

        def read(self, amount=-1):
            data, self._left = self._left, b""
            return data

        def __enter__(self):
            return self

        def __exit__(self, *exc):
            return False

    monkeypatch.setattr(download, "http_open", lambda url, **kw: PlainShort())
    with pytest.raises(download.DownloadError) as stub_err:
        download.http_get("http://127.0.0.1/plain")
    assert isinstance(stub_err.value.__cause__, http.client.IncompleteRead)


def test_sophon_fetch_bytes_retries_truncated_content_length(monkeypatch):
    def truncated(conn, req):
        conn.sendall(b"HTTP/1.1 200 OK\r\nContent-Length: 100\r\n\r\n" + b"a" * 40)
        conn.close()

    def complete(conn, req):
        conn.sendall(b"HTTP/1.1 200 OK\r\nContent-Length: 100\r\n\r\n" + b"c" * 100)
        conn.close()

    server = RawServer([truncated, complete])
    monkeypatch.setattr(sophon.time, "sleep", lambda _s: None)
    try:
        with download.HttpClientPool(timeout=5) as pool:
            assert sophon._fetch_bytes(pool, server.url + "/chunk", limit=1024) == b"c" * 100
    finally:
        server.close()


# --------------------------------------------------------------------------- review fixes
def test_public_text_leaves_ordinary_text_with_at_signs_alone():
    for text in (
        r"cannot write C:\Users\foo@bar\game.pak",
        "asset:icon@2x.png",
        "failed (see log@line 4)",
    ):
        assert download.public_text(text) == text


def test_an_early_eof_keeps_the_bytes_that_arrived_and_the_retry_resumes(tmp_path, monkeypatch):
    body = bytes(range(256)) * 4  # 1024 bytes

    def truncated(conn, req):
        conn.sendall(b"HTTP/1.1 200 OK\r\nContent-Length: 1024\r\n\r\n" + body[:600])
        conn.close()

    def rest(conn, req):
        assert b"range: bytes=600-" in req.lower()
        conn.sendall(
            b"HTTP/1.1 206 Partial Content\r\nContent-Length: 424\r\n"
            b"Content-Range: bytes 600-1023/1024\r\n\r\n" + body[600:]
        )
        conn.close()

    server = RawServer([truncated, rest])
    monkeypatch.setattr(download, "wait", lambda s, c: None)
    dest = tmp_path / "file.bin"
    try:
        download.download(server.url + "/f", dest, expected_size=1024, range_workers=1)
    finally:
        server.close()
    assert dest.read_bytes() == body
    assert b"range: bytes=600-" in server.received[1].lower()


def test_a_truncated_pooled_response_drops_its_connection():
    class Conn:
        closed = False

        def close(self):
            Conn.closed = True

    class Raw:
        status = 200
        headers: dict = {}
        length = 60  # http.client: body bytes still owed after an early EOF

        def isclosed(self):
            return True  # http.client closes fp at EOF even when the body was short

        def close(self):
            pass

    download.HttpResponse(Raw(), Conn()).close()
    assert Conn.closed


# --------------------------------------------------------------------------- rc6 mutation gaps
def test_plain_exception_args_in_the_chain_are_scrubbed(monkeypatch):
    """A ValueError (not URLError/OSError) carrying a secret URL is scrubbed through its args."""
    secret_url = "https://u:p@host/x?q=1#frag"

    class Opener:
        def open(self, req, timeout=None):
            raise ValueError(f"bad {secret_url}")

    monkeypatch.setattr(download, "_build_opener", lambda redirect_allowed: Opener())

    with pytest.raises(download.DownloadError) as caught:
        download.http_open("https://host/x")

    assert isinstance(caught.value.__cause__, ValueError)
    _assert_no_secrets(caught.value)
    assert "https://host/x" in caught.value.__cause__.args[0]


@pytest.mark.parametrize(
    ("sent", "inner", "expected"),
    [
        # The connection never completed: safe to send again, even for a timeout the
        # errno rules alone would not retry.
        (False, TimeoutError("timed out"), True),
        # The request went out: never "before send", even for an errno that usually is.
        (True, ConnectionRefusedError(errno.ECONNREFUSED, "refused"), False),
    ],
)
def test_http_open_marks_before_send_from_the_connection_state(monkeypatch, sent, inner, expected):
    class Opener:
        _nadare_send_state = {"tracked": True, "sent": sent}

        def open(self, req, timeout=None):
            raise inner

    monkeypatch.setattr(download, "_build_opener", lambda redirect_allowed: Opener())

    with pytest.raises(download.DownloadError) as caught:
        download.http_open(URL, data=b"{}")

    assert caught.value.before_send is expected
    assert sources._pre_send_error(caught.value) is expected
