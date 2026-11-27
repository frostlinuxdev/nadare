# SPDX-License-Identifier: GPL-3.0-or-later
"""Follow-ups of the 2026-11-26 fetch scrutiny on the #1816 index-fetch retries.

Every test here fails on the tree that shipped #1816: a ``Retry-After`` of NaN/inf/negative
skipped the backoff, the final error text carried the query string, a Stop during the last
allowed attempt was reported as a network error, Stop could not end an in-flight body read,
Reverse: 1999 bypassed the shared retry/cancel/wrapping path, and the resumed-``.part``
digest after promotion ran without the cancel token.

The socket tests use 127.0.0.1 only and short timeouts; nothing leaves the host.
"""

from __future__ import annotations

import hashlib
import http.client
import math
import socket
import threading
import time
import urllib.error
from pathlib import Path

import pytest
from _httpserver import Server

from nadare.core import directdl, download, sources
from nadare.core.events import Cancel, Cancelled
from nadare.core.sources import r1999

URL = "https://cdn.example.invalid/index.json?appcode=SEKRIT&token=deadbeefcafe#frag"
SAFE = "https://cdn.example.invalid/index.json"


# --------------------------------------------------------------------------- helpers
def unreachable(url: str, inner: BaseException) -> download.DownloadError:
    """The error ``http_open`` raises: ``DownloadError`` caused by ``URLError(inner)``."""
    exc = download.DownloadError(f"cannot reach {url}: {inner}")
    exc.__cause__ = urllib.error.URLError(inner)
    return exc


def status_error(
    url: str, status: int, retry_after: float | None = None
) -> download.HTTPStatusError:
    return download.HTTPStatusError(
        f"HTTP {status} for {url}", status=status, retry_after=retry_after, url=url
    )


class FakeGet:
    """A scripted ``download.http_get``: each item is bytes or an exception."""

    def __init__(self, script):
        self.script = list(script)
        self.calls = []

    def __call__(self, url, **kwargs):
        self.calls.append((url, kwargs))
        item = self.script.pop(0) if self.script else b""
        if isinstance(item, BaseException):
            raise item
        return item


class FakeWait:
    def __init__(self):
        self.calls = []

    def __call__(self, seconds, cancel):
        self.calls.append((seconds, cancel))


@pytest.fixture()
def fake_get(monkeypatch):
    def install(script):
        fake = FakeGet(script)
        monkeypatch.setattr(download, "http_get", fake)
        return fake

    return install


@pytest.fixture()
def fake_wait(monkeypatch):
    fake = FakeWait()
    monkeypatch.setattr(download, "wait", fake)
    return fake


@pytest.fixture()
def no_jitter(monkeypatch):
    monkeypatch.setattr("random.random", lambda: 0.0)


class SlowServer:
    """A raw 127.0.0.1 server: headers at once, then one body byte every ``gap`` s.

    ``stall=True`` sends the headers and then nothing at all, like a tarpit.
    """

    def __init__(self, *, payload: int = 100, gap: float = 0.05, stall: bool = False):
        self.payload = payload
        self.gap = gap
        self.stall = stall
        self.stop = threading.Event()
        self.peer_gone = threading.Event()
        self._server = socket.socket()
        self._server.setsockopt(socket.SOL_SOCKET, socket.SO_REUSEADDR, 1)
        self._server.bind(("127.0.0.1", 0))
        self._server.listen(4)
        self._server.settimeout(10)
        self.port = self._server.getsockname()[1]
        threading.Thread(target=self._serve, daemon=True).start()

    @property
    def url(self) -> str:
        return f"http://127.0.0.1:{self.port}/index.json"

    def _serve(self) -> None:
        try:
            conn, _addr = self._server.accept()
        except OSError:
            return
        with conn:
            try:
                conn.recv(65536)
                conn.sendall(
                    b"HTTP/1.0 200 OK\r\nContent-Length: %d\r\n\r\n" % self.payload
                )
                if self.stall:
                    while not self.stop.wait(0.05):
                        pass
                else:
                    for _ in range(self.payload):
                        if self.stop.wait(self.gap):
                            return
                        conn.sendall(b"x")
                self.peer_gone.wait(2)
            except OSError:
                self.peer_gone.set()  # the client closed the socket

    def close(self) -> None:
        self.stop.set()
        self._server.close()


# --------------------------------------------------------------------------- 1. Retry-After
@pytest.mark.parametrize("raw", ["nan", "NaN", "-nan", "inf", "-inf", "Infinity", "-5", "-0.5"])
def test_a_non_finite_or_negative_retry_after_is_absent(raw):
    """The header is data from the network: a broken value means normal backoff."""
    assert download.parse_retry_after(raw) is None


@pytest.mark.parametrize(
    "raw,expected", [("0", 0.0), ("0.5", 0.5), ("5", 5.0), ("999999", 60.0), ("1e308", 60.0)]
)
def test_a_finite_retry_after_is_kept_and_capped(raw, expected):
    assert download.parse_retry_after(raw) == expected


@pytest.mark.parametrize("value", [float("nan"), float("inf"), float("-inf"), -1.0])
def test_a_non_finite_exception_retry_after_falls_back_to_the_schedule(no_jitter, value):
    """A retry_after set directly on the exception must not reach ``download.wait``."""
    exc = status_error(URL, 503, retry_after=value)
    delay = sources._fetch_delay(exc, 1)
    assert math.isfinite(delay)
    assert delay == 2.0  # the normal first backoff, no jitter


@pytest.mark.parametrize("value", [float("nan"), float("inf"), -4.0])
def test_a_raw_retry_after_attribute_is_ignored_by_the_schedule(no_jitter, value):
    """Any exception a source raises may carry one, not only ``HTTPStatusError``."""
    exc = download.DownloadError("cannot reach https://cdn.example.invalid/index.json: busy")
    exc.retry_after = value  # type: ignore[attr-defined]
    delay = sources._fetch_delay(exc, 1)
    assert math.isfinite(delay)
    assert delay == 2.0


def test_a_nan_retry_after_header_waits_the_normal_backoff(fake_get, fake_wait, no_jitter):
    get = fake_get([status_error(URL, 503, retry_after=download.parse_retry_after("nan")), b"ok"])
    assert sources.default_fetch(URL) == b"ok"
    assert len(get.calls) == 2
    (delay, _cancel), = fake_wait.calls
    assert math.isfinite(delay) and delay == 2.0


@pytest.mark.parametrize("value", [float("nan"), float("inf"), -3.0, 1e9])
def test_every_requested_wait_is_finite_and_within_the_cap(
    monkeypatch, fake_get, fake_wait, value
):
    monkeypatch.setattr(download, "DEFAULT_RETRIES", 3)
    fake_get([status_error(URL, 503, retry_after=value)] * 3)
    with pytest.raises(directdl.DirectError):
        sources.default_fetch(URL)
    assert len(fake_wait.calls) == 2
    for delay, _cancel in fake_wait.calls:
        assert math.isfinite(delay)
        assert 0.0 <= delay <= sources.MAX_FETCH_RETRY_AFTER


# --------------------------------------------------------------------------- 2. final text
@pytest.mark.parametrize(
    "failure",
    [
        lambda url: unreachable(url, TimeoutError("timed out")),
        lambda url: status_error(url, 503),
        lambda url: TimeoutError("raw socket timeout"),
    ],
)
def test_the_final_fetch_error_has_no_query_string_or_fragment(
    fake_get, fake_wait, no_jitter, failure
):
    fake_get([failure(URL)] * 3)
    with pytest.raises(directdl.DirectError) as caught:
        sources.default_fetch(URL)
    text = str(caught.value)
    assert "SEKRIT" not in text
    assert "deadbeefcafe" not in text
    assert "?" not in text and "#" not in text
    assert SAFE in text


def test_the_final_fetch_error_of_a_permanent_failure_has_no_query_string(fake_get, fake_wait):
    fake_get([status_error(URL, 404)])
    with pytest.raises(directdl.DirectError) as caught:
        sources.default_fetch(URL)
    assert str(caught.value) == f"HTTP 404 for {SAFE}"


def test_a_raw_read_error_is_wrapped_without_the_query_string(fake_get, fake_wait, no_jitter):
    """The cause is what a logged traceback shows: it must be clean too."""
    fake_get([TimeoutError("socket timed out")] * 3)
    with pytest.raises(directdl.DirectError) as caught:
        sources.default_fetch(URL)
    cause = caught.value.__cause__
    assert cause is not None
    assert str(cause) == f"cannot reach {SAFE}: socket timed out"


def test_http_open_connection_error_hides_the_query_string():
    with pytest.raises(download.DownloadError) as caught:
        download.http_open("http://127.0.0.1:1/index.json?token=SEKRIT#frag", timeout=5)
    text = str(caught.value)
    assert "SEKRIT" not in text and "?" not in text and "#" not in text
    assert text.startswith("cannot reach http://127.0.0.1:1/index.json: ")


def test_http_get_status_error_hides_the_query_string():
    server = Server({"/index.json?token=SEKRIT#frag": b"busy"})
    server.fail_next = 1
    server.fail_status = 503
    try:
        with pytest.raises(download.DownloadError) as caught:
            download.http_get(server.url + "/index.json?token=SEKRIT#frag", timeout=5)
    finally:
        server.close()
    text = str(caught.value)
    assert "SEKRIT" not in text and "?" not in text and "#" not in text
    assert text == f"HTTP 503 for {server.url}/index.json"


def test_http_get_size_cap_error_hides_the_query_string():
    server = Server({"/index.json?token=SEKRIT": b"0123456789"})
    try:
        with pytest.raises(download.DownloadError) as caught:
            download.http_get(server.url + "/index.json?token=SEKRIT", max_bytes=4, timeout=5)
    finally:
        server.close()
    text = str(caught.value)
    assert "SEKRIT" not in text and "?" not in text
    assert text == f"response too large (over 4 bytes) for {server.url}/index.json"


# --------------------------------------------------------------------------- 3. last attempt
@pytest.mark.parametrize("retries", [1, 3])
def test_a_stop_during_the_last_allowed_attempt_raises_cancelled(
    monkeypatch, fake_wait, no_jitter, retries
):
    monkeypatch.setattr(download, "DEFAULT_RETRIES", retries)
    token = Cancel()
    calls = []

    def get(url, **kwargs):
        calls.append(url)
        if len(calls) == retries:
            token.cancel()  # Stop arrives while the final failed attempt unwinds
        raise unreachable(url, TimeoutError("timed out"))

    monkeypatch.setattr(download, "http_get", get)
    with pytest.raises(Cancelled):
        sources.default_fetch(URL, cancel=token)
    assert len(calls) == retries
    assert len(fake_wait.calls) == retries - 1


# --------------------------------------------------------------------------- 4. in-flight stop
def test_the_fetch_hands_its_token_to_the_transport(fake_get, fake_wait):
    get = fake_get([b"ok"])
    token = Cancel()
    assert sources.default_fetch(URL, cancel=token) == b"ok"
    assert get.calls[0][1]["cancel"] is token


def test_http_get_binds_the_token_to_the_response(monkeypatch):
    """So ``transfer.Response`` checks it between reads (its ``cancel`` slot)."""

    class Resp:
        def __init__(self):
            self.cancel = None
            self._left = b"ok"

        def read(self, _amount=-1):
            block, self._left = self._left, b""
            return block

        def __enter__(self):
            return self

        def __exit__(self, *_exc):
            return False

    resp = Resp()
    monkeypatch.setattr(download, "http_open", lambda url, **kw: resp)
    token = Cancel()
    assert download.http_get(URL, cancel=token) == b"ok"
    assert resp.cancel is token


def test_a_cancelled_token_never_reads_the_body(monkeypatch):
    reads = []

    class Resp:
        cancel = None

        def read(self, amount=-1):
            reads.append(amount)
            return b""

        def __enter__(self):
            return self

        def __exit__(self, *_exc):
            return False

    monkeypatch.setattr(download, "http_open", lambda url, **kw: Resp())
    token = Cancel()
    token.cancel()
    with pytest.raises(Cancelled):
        download.http_get(URL, cancel=token)
    assert reads == []


def test_a_read_error_after_stop_is_reported_as_cancelled(monkeypatch):
    token = Cancel()

    class Resp:
        cancel = None

        def read(self, amount=-1):
            token.cancel()  # Stop lands just as the socket is shut down
            raise OSError(104, "Connection reset by peer")

        def __enter__(self):
            return self

        def __exit__(self, *_exc):
            return False

    monkeypatch.setattr(download, "http_open", lambda url, **kw: Resp())
    with pytest.raises(Cancelled):
        download.http_get(URL, cancel=token)





def test_stop_ends_a_slow_drip_body_at_once():
    server = SlowServer(payload=200, gap=0.05)  # 10 s if it were allowed to finish
    token = Cancel()
    threading.Timer(0.3, token.cancel).start()
    started = time.monotonic()
    try:
        with pytest.raises(Cancelled):
            sources.default_fetch(server.url, cancel=token)
        elapsed = time.monotonic() - started
        assert elapsed < 1.0, f"Stop took {elapsed:.2f}s"
        assert server.peer_gone.wait(2.0), "the slow response was not closed"
    finally:
        server.close()


def test_stop_ends_a_stalled_body_at_once():
    server = SlowServer(stall=True)
    token = Cancel()
    threading.Timer(0.3, token.cancel).start()
    started = time.monotonic()
    try:
        with pytest.raises(Cancelled):
            download.http_get(server.url, cancel=token, timeout=30)
        elapsed = time.monotonic() - started
        assert elapsed < 1.0, f"Stop took {elapsed:.2f}s"
    finally:
        server.close()


def test_the_http_stack_closes_a_stalled_response_on_stop():
    """Through ``http_get``'s own token: after Stop the server sees the connection go."""
    server = SlowServer(payload=1000, gap=0.05)
    token = Cancel()
    threading.Timer(0.3, token.cancel).start()
    try:
        with pytest.raises(Cancelled):
            download.http_get(server.url, cancel=token, timeout=30)
        assert server.peer_gone.wait(2.0)
    finally:
        server.close()


# --------------------------------------------------------------------------- 5. Reverse: 1999
def _no_http_open(*_a, **_k):
    raise AssertionError("r1999 must use the shared retry path, not http_open")


def test_r1999_post_retries_a_transient_failure(monkeypatch, fake_wait, no_jitter):
    body = b'{"gameId": "60001"}'
    calls = []

    def post(url, data, **kwargs):
        calls.append((url, data, kwargs))
        if len(calls) == 1:
            raise unreachable(url, TimeoutError("timed out"))
        return b'{"code": 200, "data": {}}'

    monkeypatch.setattr(download, "http_open", _no_http_open)
    monkeypatch.setattr(download, "http_post", post, raising=False)
    assert r1999.default_post(r1999.API + "/version", body, {"lang": "en"}) == (
        b'{"code": 200, "data": {}}'
    )
    assert len(calls) == 2
    assert calls[0][1] == body
    assert calls[0][2]["headers"] == {"lang": "en"}
    guard = calls[0][2]["redirect_allowed"]
    assert guard(r1999.API + "/resource") and not guard("https://evil.test/x")
    assert len(fake_wait.calls) == 1


def test_r1999_post_retries_a_mid_body_read_error(monkeypatch, fake_wait, no_jitter):
    calls = []

    def post(url, data, **kwargs):
        calls.append(url)
        if len(calls) == 1:
            raise http.client.IncompleteRead(b"half", 10)
        return b"{}"

    monkeypatch.setattr(download, "http_open", _no_http_open)
    monkeypatch.setattr(download, "http_post", post, raising=False)
    assert r1999.default_post(r1999.API + "/version", b"{}", {}) == b"{}"
    assert len(calls) == 2


def test_r1999_post_obeys_a_cancelled_backend_token(monkeypatch):
    token = Cancel()
    token.cancel()
    calls = []

    def post(url, data, **kwargs):
        calls.append(url)
        return b"{}"

    monkeypatch.setattr(download, "http_open", _no_http_open)
    monkeypatch.setattr(download, "http_post", post, raising=False)
    with sources.fetch_cancel(token):
        with pytest.raises(Cancelled):
            r1999.default_post(r1999.API + "/version", b"{}", {})
    assert calls == []


def test_r1999_post_final_error_has_no_query_string(monkeypatch, fake_wait, no_jitter):
    url = "https://hotupdate-hw.sl916.com/diff-update/version?token=SEKRIT&appcode=deadbeefcafe"

    def post(target, data, **kwargs):
        raise unreachable(target, TimeoutError("timed out"))

    monkeypatch.setattr(download, "http_open", _no_http_open)
    monkeypatch.setattr(download, "http_post", post, raising=False)
    with pytest.raises(directdl.DirectError) as caught:
        r1999.default_post(url, b"{}", {})
    text = str(caught.value)
    assert "SEKRIT" not in text and "deadbeefcafe" not in text and "?" not in text
    assert "https://hotupdate-hw.sl916.com/diff-update/version" in text


# --------------------------------------------------------------------------- 6. resumed part
def _no_network(*_a, **_k):
    raise AssertionError("unexpected network")


def test_the_resumed_part_digest_gets_the_cancel_token(tmp_path, monkeypatch):
    payload = b"resumed bytes" * 16
    dest = tmp_path / "asset.bin"
    dest.with_name("asset.bin.part").write_bytes(payload)
    token = Cancel()
    seen: list[tuple[Path, object]] = []
    real_hash = download.hash_file

    def hashing(path, *args, **kwargs):
        seen.append((Path(path), kwargs.get("cancel")))
        return real_hash(path, *args, **kwargs)

    monkeypatch.setattr(download, "hash_file", hashing)
    monkeypatch.setattr(download, "http_open", _no_network)
    result = download.download(
        "https://invalid.example/asset",
        dest,
        expected_sha256=hashlib.sha256(payload).hexdigest(),
        cancel=token,
    )
    assert result.resumed and dest.read_bytes() == payload
    assert seen[-1][0] == dest, "the promotion digest is the last one"
    assert seen[-1][1] is token, "the promotion digest did not get the cancel token"


def test_every_url_in_a_download_error_loses_its_query():
    from nadare.core import download

    exc = download.DownloadError(
        "redirect to disallowed URL: https://evil.invalid/next?appcode=SEKRIT#f "
        "from https://user:pw@api.example.com/a?token=SEKRIT2"
    )
    assert "SEKRIT" not in str(exc) and "pw@" not in str(exc)
    assert "https://evil.invalid/next" in str(exc)


def test_a_stop_racing_tls_shutdown_is_cancelled_not_valueerror():
    import pytest as _pytest

    from nadare.core import download
    from nadare.core.events import Cancel, Cancelled

    cancel = Cancel()

    class Resp:
        def read(self, n):
            cancel.cancel()
            raise ValueError("Read on closed or unwrapped SSL socket.")

    with _pytest.raises(Cancelled):
        download._read_body(Resp(), 100, cancel)


def test_an_already_stopped_get_opens_no_connection(monkeypatch):
    import pytest as _pytest

    from nadare.core import download
    from nadare.core.events import Cancel, Cancelled

    opened = []
    monkeypatch.setattr(download, "http_open", lambda *a, **k: opened.append(a))
    cancel = Cancel()
    cancel.cancel()
    with _pytest.raises(Cancelled):
        download.http_get("https://example.invalid/x", cancel=cancel)
    assert opened == []
