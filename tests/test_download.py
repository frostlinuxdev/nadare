# SPDX-License-Identifier: GPL-3.0-or-later
"""Unit tests for nadare.core.download.

No sockets are used: ``http_open`` is monkeypatched with an in-memory fake that
can emulate Range/206 responses, plain 200 responses and transient failures.
"""

from __future__ import annotations

import hashlib
from pathlib import Path

import pytest

from nadare.core import download as dl


class FakeResponse:
    """Minimal stand-in for the object returned by ``urllib.request.urlopen``."""

    def __init__(self, body: bytes, status: int = 200, headers: dict[str, str] | None = None):
        self._body = body
        self.status = status
        self.headers = headers or {}
        self._pos = 0

    def getcode(self) -> int:
        return self.status

    def read(self, n: int = -1) -> bytes:
        if n is None or n < 0:
            data = self._body[self._pos :]
            self._pos = len(self._body)
            return data
        data = self._body[self._pos : self._pos + n]
        self._pos += len(data)
        return data

    def __enter__(self) -> FakeResponse:
        return self

    def __exit__(self, *exc: object) -> bool:
        return False


def _fake_http(body: bytes, calls: list[dict[str, str]] | None = None):
    def _open(url: str, *, headers=None, timeout=dl.DEFAULT_TIMEOUT):
        if calls is not None:
            calls.append(dict(headers or {}))
        return FakeResponse(body, headers={"Content-Length": str(len(body))})

    return _open


def _sha256(data: bytes) -> str:
    return hashlib.sha256(data).hexdigest()


# ---------------------------------------------------------------------------
# hashing / verification
# ---------------------------------------------------------------------------
def test_hash_file_digests(tmp_path: Path) -> None:
    path = tmp_path / "x.bin"
    path.write_bytes(b"hello world")
    digests = dl.hash_file(path, ("sha256", "sha512"))
    assert digests["sha256"] == hashlib.sha256(b"hello world").hexdigest()
    assert digests["sha512"] == hashlib.sha512(b"hello world").hexdigest()


def test_verify_file_checks_size_and_hashes(tmp_path: Path) -> None:
    path = tmp_path / "x.bin"
    path.write_bytes(b"payload")

    assert dl.verify_file(path, size=7) is True
    assert dl.verify_file(path, size=8) is False
    assert dl.verify_file(path, sha256=_sha256(b"payload")) is True
    assert dl.verify_file(path, sha256=_sha256(b"other")) is False
    assert dl.verify_file(path) is True  # no constraints -> exists is enough
    assert dl.verify_file(tmp_path / "missing") is False


def test_parse_content_range() -> None:
    assert dl._parse_content_range("bytes 100-199/1000") == (100, 199, 1000)
    assert dl._parse_content_range("bytes 0-9/*") == (0, 9, -1)
    assert dl._parse_content_range("items 0-9/10") is None
    assert dl._parse_content_range("garbage") is None


# ---------------------------------------------------------------------------
# download: happy path / checksum / size
# ---------------------------------------------------------------------------
def test_download_ok(tmp_path: Path, monkeypatch: pytest.MonkeyPatch) -> None:
    body = b"nadare" * 500
    monkeypatch.setattr(dl, "http_open", _fake_http(body))
    dest = tmp_path / "game.bin"

    result = dl.download(
        "http://example.invalid/game", dest, expected_size=len(body), expected_sha256=_sha256(body)
    )

    assert dest.read_bytes() == body
    assert result.size == len(body)
    assert result.sha256 == _sha256(body)
    assert result.resumed is False
    assert not (tmp_path / "game.bin.part").exists()


def test_download_bad_checksum_raises_and_cleans_up(
    tmp_path: Path, monkeypatch: pytest.MonkeyPatch
) -> None:
    body = b"corrupted payload"
    monkeypatch.setattr(dl, "http_open", _fake_http(body))
    dest = tmp_path / "game.bin"

    with pytest.raises(dl.IntegrityError):
        dl.download("http://example.invalid/game", dest, expected_sha256="0" * 64)

    assert not dest.exists()
    assert not (tmp_path / "game.bin.part").exists()


def test_download_size_mismatch_raises(tmp_path: Path, monkeypatch: pytest.MonkeyPatch) -> None:
    body = b"short"
    monkeypatch.setattr(dl, "http_open", _fake_http(body))
    dest = tmp_path / "game.bin"

    with pytest.raises(dl.IntegrityError):
        dl.download("http://example.invalid/game", dest, expected_size=len(body) + 1)

    assert not dest.exists()


def test_download_is_atomic_dest_absent_on_failure(
    tmp_path: Path, monkeypatch: pytest.MonkeyPatch
) -> None:
    monkeypatch.setattr(dl, "http_open", _fake_http(b"x"))
    dest = tmp_path / "game.bin"
    with pytest.raises(dl.DownloadError):
        dl.download(
            "http://example.invalid/game", dest, expected_size=999, expected_sha256="0" * 64
        )
    assert not dest.exists()


# ---------------------------------------------------------------------------
# download: resume
# ---------------------------------------------------------------------------
def test_download_resumes_partial_file(tmp_path: Path, monkeypatch: pytest.MonkeyPatch) -> None:
    body = bytes(range(256)) * 8  # 2048 bytes, non-trivial
    half = len(body) // 2
    dest = tmp_path / "game.bin"
    part = tmp_path / "game.bin.part"
    part.write_bytes(body[:half])

    seen: list[dict[str, str]] = []

    def _open(url: str, *, headers=None, timeout=dl.DEFAULT_TIMEOUT):
        headers = dict(headers or {})
        seen.append(headers)
        offset = int(headers["Range"].split("=")[1].split("-")[0])
        assert offset == half
        return FakeResponse(
            body[offset:],
            status=206,
            headers={
                "Content-Length": str(len(body) - offset),
                "Content-Range": f"bytes {offset}-{len(body) - 1}/{len(body)}",
            },
        )

    monkeypatch.setattr(dl, "http_open", _open)
    result = dl.download(
        "http://example.invalid/game", dest, expected_size=len(body), expected_sha256=_sha256(body)
    )

    assert result.resumed is True
    assert dest.read_bytes() == body
    assert not part.exists()
    assert seen and "Range" in seen[0]


def test_download_restarts_when_server_ignores_range(
    tmp_path: Path, monkeypatch: pytest.MonkeyPatch
) -> None:
    body = b"restart-me" * 100
    dest = tmp_path / "game.bin"
    (tmp_path / "game.bin.part").write_bytes(body[:50])

    monkeypatch.setattr(dl, "http_open", _fake_http(body))
    result = dl.download(
        "http://example.invalid/game", dest, expected_size=len(body), expected_sha256=_sha256(body)
    )

    assert result.resumed is False
    assert dest.read_bytes() == body


def test_download_reuses_already_verified_part(
    tmp_path: Path, monkeypatch: pytest.MonkeyPatch
) -> None:
    body = b"already here"
    dest = tmp_path / "game.bin"
    (tmp_path / "game.bin.part").write_bytes(body)

    def _boom(*args: object, **kwargs: object) -> None:
        raise AssertionError("http_open must not be called for a verified part")

    monkeypatch.setattr(dl, "http_open", _boom)
    result = dl.download(
        "http://example.invalid/game", dest, expected_size=len(body), expected_sha256=_sha256(body)
    )

    assert result.resumed is True
    assert dest.read_bytes() == body
    assert not (tmp_path / "game.bin.part").exists()


# ---------------------------------------------------------------------------
# retries / cancellation
# ---------------------------------------------------------------------------
def test_download_retries_transient_error(tmp_path: Path, monkeypatch: pytest.MonkeyPatch) -> None:
    body = b"eventually"
    attempts = {"n": 0}

    def _flaky(url: str, *, headers=None, timeout=dl.DEFAULT_TIMEOUT):
        attempts["n"] += 1
        if attempts["n"] == 1:
            raise dl.DownloadError("transient")
        return FakeResponse(body, headers={"Content-Length": str(len(body))})

    monkeypatch.setattr(dl, "http_open", _flaky)
    result = dl.download("http://example.invalid/game", tmp_path / "game.bin", retries=2, backoff=0)

    assert result.size == len(body)
    assert attempts["n"] == 2


def test_download_cancellation(tmp_path: Path, monkeypatch: pytest.MonkeyPatch) -> None:
    from nadare.core.events import Cancel, Cancelled

    body = b"z" * 100
    cancel = Cancel()

    class CancellingResponse(FakeResponse):
        def read(self, n: int = -1) -> bytes:
            cancel.cancel()
            return super().read(n)

    def _open(url: str, *, headers=None, timeout=dl.DEFAULT_TIMEOUT):
        return CancellingResponse(body, headers={"Content-Length": str(len(body))})

    monkeypatch.setattr(dl, "http_open", _open)
    dest = tmp_path / "game.bin"
    with pytest.raises(Cancelled):
        dl.download("http://example.invalid/game", dest, cancel=cancel, retries=1)

    assert not dest.exists()
    # the partial bytes stay so the next install resumes instead of restarting
    assert (tmp_path / "game.bin.part").exists()


def test_download_network_failure_keeps_resumable_part(
    tmp_path: Path, monkeypatch: pytest.MonkeyPatch
) -> None:
    body = b"y" * 100

    class DyingResponse(FakeResponse):
        def read(self, n: int = -1) -> bytes:
            if self._pos >= 40:
                raise OSError("connection reset")
            return super().read(min(n, 40) if n > 0 else 40)

    monkeypatch.setattr(
        dl,
        "http_open",
        lambda url, *, headers=None, timeout=dl.DEFAULT_TIMEOUT: DyingResponse(
            body, headers={"Content-Length": str(len(body))}
        ),
    )
    monkeypatch.setattr(dl, "_sleep", lambda *a, **k: None)
    dest = tmp_path / "game.bin"
    with pytest.raises(dl.DownloadError):
        dl.download("http://example.invalid/game", dest, retries=2)

    assert not dest.exists()
    assert (tmp_path / "game.bin.part").exists()


def test_download_without_content_length(tmp_path: Path, monkeypatch: pytest.MonkeyPatch) -> None:
    """A chunked response (no Content-Length) must still complete."""
    body = b"chunked body with no length"

    def _open(url: str, *, headers=None, timeout=dl.DEFAULT_TIMEOUT):
        return FakeResponse(body, status=200, headers={})

    monkeypatch.setattr(dl, "http_open", _open)
    dest = tmp_path / "game.bin"
    result = dl.download("http://example.invalid/game", dest, retries=1, backoff=0)

    assert result.size == len(body)
    assert dest.read_bytes() == body


# ---------------------------------------------------------------------------
# http_open error translation (no network)
# ---------------------------------------------------------------------------
def test_http_open_translates_urlerror(monkeypatch: pytest.MonkeyPatch) -> None:
    import urllib.error

    class _Opener:
        def open(self, *args: object, **kwargs: object) -> None:
            raise urllib.error.URLError("nope")

    monkeypatch.setattr(dl.urllib.request, "build_opener", lambda *a, **k: _Opener())
    with pytest.raises(dl.DownloadError):
        dl.http_get("http://example.invalid/x")


def test_http_get_routes_through_the_safe_redirect_handler(
    monkeypatch: pytest.MonkeyPatch,
) -> None:
    """L6 residual: http_get must not follow redirects with urllib defaults."""
    captured: dict[str, object] = {}

    class _Opener:
        def open(self, req: object, timeout: int | None = None) -> FakeResponse:
            captured["req"] = req
            return FakeResponse(b"ok", headers={"Content-Length": "2"})

    def _build(*handlers: object) -> _Opener:
        captured["handlers"] = handlers
        return _Opener()

    monkeypatch.setattr(dl.urllib.request, "build_opener", _build)
    guard = lambda url: True  # noqa: E731
    assert dl.http_get("http://example.invalid/x", redirect_allowed=guard) == b"ok"
    handlers = captured["handlers"]
    assert isinstance(handlers[0], dl._SafeRedirectHandler)  # type: ignore[index]
    assert handlers[0]._redirect_allowed is guard  # type: ignore[union-attr]


def test_safe_redirect_handler_drops_credentials_across_origin() -> None:
    import urllib.request

    handler = dl._SafeRedirectHandler()
    req = urllib.request.Request(
        "https://good.example/a",
        headers={"Authorization": "Bearer x", "Cookie": "s=1", "X-Keep": "1"},
    )
    new = handler.redirect_request(req, None, 302, "Found", {}, "https://other.example/b")
    assert new is not None
    lowered = {k.lower(): v for k, v in new.headers.items()}
    assert "authorization" not in lowered
    assert "cookie" not in lowered
    assert lowered["x-keep"] == "1"


def test_safe_redirect_handler_keeps_credentials_same_origin() -> None:
    import urllib.request

    handler = dl._SafeRedirectHandler()
    req = urllib.request.Request("https://good.example/a", headers={"Authorization": "Bearer x"})
    new = handler.redirect_request(req, None, 302, "Found", {}, "https://good.example/b")
    assert new is not None
    assert {k.lower() for k in new.headers} >= {"authorization"}


def test_safe_redirect_handler_refuses_https_downgrade() -> None:
    import urllib.request

    handler = dl._SafeRedirectHandler()
    req = urllib.request.Request("https://good.example/a")
    with pytest.raises(dl.DownloadError):
        handler.redirect_request(req, None, 302, "Found", {}, "http://good.example/b")


def test_safe_redirect_handler_honours_redirect_allowed() -> None:
    import urllib.request

    req = urllib.request.Request("https://good.example/a")
    denied = dl._SafeRedirectHandler(lambda url: False)
    with pytest.raises(dl.DownloadError):
        denied.redirect_request(req, None, 302, "Found", {}, "https://other.example/b")

    seen: list[str] = []
    allowed = dl._SafeRedirectHandler(lambda url: seen.append(url) or True)
    new = allowed.redirect_request(req, None, 302, "Found", {}, "https://other.example/b")
    assert new is not None
    assert seen == ["https://other.example/b"]


class _TrackableFp:
    """Response stand-in recording whether it was drained and closed."""

    def __init__(self) -> None:
        self.read_called = False
        self.closed = False

    def read(self, *args: object) -> bytes:
        self.read_called = True
        return b""

    def close(self) -> None:
        self.closed = True


def test_safe_redirect_handler_closes_response_before_refusing() -> None:
    """N4: a refused redirect must not abandon the response socket."""
    import urllib.request

    handler = dl._SafeRedirectHandler()
    req = urllib.request.Request("https://good.example/a")
    fp = _TrackableFp()

    with pytest.raises(dl.DownloadError):
        handler.redirect_request(req, fp, 302, "Found", {}, "http://good.example/b")

    assert fp.read_called is True
    assert fp.closed is True


def test_safe_redirect_handler_translates_malformed_port() -> None:
    """N4: a malformed redirect port must surface as DownloadError, not ValueError."""
    import urllib.request

    handler = dl._SafeRedirectHandler()
    req = urllib.request.Request("https://good.example/a")
    with pytest.raises(dl.DownloadError, match="invalid port"):
        handler.redirect_request(req, None, 302, "Found", {}, "https://good.example:abc/b")


def test_split_url_rejects_malformed_port() -> None:
    with pytest.raises(dl.DownloadError, match="invalid port"):
        dl._split_url("https://good.example:abc/x")


# ---------------------------------------------------------------------------
# remote_size: redirect hardening (N1)
# ---------------------------------------------------------------------------
def test_remote_size_routes_through_the_safe_redirect_handler(
    monkeypatch: pytest.MonkeyPatch,
) -> None:
    captured: dict[str, object] = {}

    class _Opener:
        def open(self, req: object, timeout: int | None = None) -> FakeResponse:
            captured["req"] = req
            return FakeResponse(b"", headers={"Content-Length": "1234"})

    def _build(*handlers: object) -> _Opener:
        captured["handlers"] = handlers
        return _Opener()

    monkeypatch.setattr(dl.urllib.request, "build_opener", _build)
    guard = lambda url: False  # noqa: E731
    assert dl.remote_size("https://example.invalid/x", redirect_allowed=guard) == 1234
    handler = captured["handlers"][0]  # type: ignore[index]
    assert isinstance(handler, dl._SafeRedirectHandler)
    assert handler._redirect_allowed is guard  # type: ignore[union-attr]
    assert captured["req"].method == "HEAD"  # type: ignore[union-attr]


def _install_redirect_probe(monkeypatch: pytest.MonkeyPatch, location: str) -> dict:
    """Fake opener that runs the real handler's redirect logic exactly once."""
    state: dict[str, object] = {}

    class _Opener:
        def open(self, req: object, timeout: int | None = None) -> FakeResponse:
            state["redirect"] = state["handler"].redirect_request(  # type: ignore[union-attr]
                req, None, 302, "Found", {}, location
            )
            return FakeResponse(b"", headers={"Content-Length": "7"})

    def _build(*handlers: object) -> _Opener:
        state["handler"] = handlers[0]
        return _Opener()

    monkeypatch.setattr(dl.urllib.request, "build_opener", _build)
    return state


def test_remote_size_drops_credentials_across_origin(
    monkeypatch: pytest.MonkeyPatch,
) -> None:
    state = _install_redirect_probe(monkeypatch, "https://other.example/x")

    assert (
        dl.remote_size("https://good.example/x", headers={"Authorization": "Bearer secret"})
        == 7
    )
    redirected = state["redirect"]
    assert redirected is not None
    assert "authorization" not in {k.lower() for k in redirected.headers}  # type: ignore[union-attr]


def test_remote_size_refuses_https_downgrade(monkeypatch: pytest.MonkeyPatch) -> None:
    _install_redirect_probe(monkeypatch, "http://good.example/x")
    # The handler refuses the downgrade; an unknown size is not an error.
    assert dl.remote_size("https://good.example/x") is None


# ---------------------------------------------------------------------------
# HttpClientPool: redirect safety
# ---------------------------------------------------------------------------
class _FakeStreamResponse:
    """Stand-in for ``http.client.HTTPResponse`` used by ``HttpClientPool``."""

    def __init__(self, status: int, headers: dict[str, str], body: bytes):
        self.status = status
        self.headers = headers
        self._body = body
        self._pos = 0

    def getheader(self, name: str, default: str | None = None) -> str | None:
        return self.headers.get(name, default)

    def read(self, n: int = -1) -> bytes:
        if n is None or n < 0:
            data = self._body[self._pos :]
            self._pos = len(self._body)
            return data
        data = self._body[self._pos : self._pos + n]
        self._pos += len(data)
        return data

    def close(self) -> None:
        pass


class _FakeConn:
    def __init__(self, responses: list[tuple[int, dict[str, str], bytes]]):
        self._responses = list(responses)
        self.sent: list[tuple[str, dict[str, str]]] = []

    def request(self, method: str, path: str, headers: dict[str, str] | None = None) -> None:
        self.sent.append((path, dict(headers or {})))

    def getresponse(self) -> _FakeStreamResponse:
        status, headers, body = self._responses.pop(0)
        return _FakeStreamResponse(status, headers, body)

    def close(self) -> None:
        pass


def test_http_client_pool_drops_credentials_across_hosts(
    monkeypatch: pytest.MonkeyPatch,
) -> None:
    pool = dl.HttpClientPool()
    conn = _FakeConn(
        [
            (302, {"Location": "https://other.example/steal"}, b""),
            (200, {"Content-Length": "2"}, b"ok"),
        ]
    )
    monkeypatch.setattr(pool, "_connection", lambda *a: conn)

    resp = pool.open(
        "https://good.example/a",
        headers={"Authorization": "Bearer x", "X-Keep": "1"},
    )
    resp.close()

    assert conn.sent[0][1]["Authorization"] == "Bearer x"
    assert "Authorization" not in conn.sent[1][1]
    assert conn.sent[1][1]["X-Keep"] == "1"


def test_http_client_pool_refuses_https_to_http_downgrade(
    monkeypatch: pytest.MonkeyPatch,
) -> None:
    pool = dl.HttpClientPool()
    conn = _FakeConn([(302, {"Location": "http://good.example/b"}, b"")])
    monkeypatch.setattr(pool, "_connection", lambda *a: conn)

    with pytest.raises(dl.DownloadError):
        pool.open("https://good.example/a")


def test_http_client_pool_translates_malformed_redirect_port(
    monkeypatch: pytest.MonkeyPatch,
) -> None:
    """N4: a bad Location port must not escape as a raw ValueError."""
    pool = dl.HttpClientPool()
    conn = _FakeConn([(302, {"Location": "https://good.example:abc/b"}, b"")])
    monkeypatch.setattr(pool, "_connection", lambda *a: conn)

    with pytest.raises(dl.DownloadError, match="invalid port"):
        pool.open("https://good.example/a")


def test_http_client_pool_honours_redirect_allowed(
    monkeypatch: pytest.MonkeyPatch,
) -> None:
    pool = dl.HttpClientPool()
    conn = _FakeConn([(302, {"Location": "https://other.example/b"}, b"")])
    monkeypatch.setattr(pool, "_connection", lambda *a: conn)
    seen: list[str] = []

    def _deny(url: str) -> bool:
        seen.append(url)
        return False

    with pytest.raises(dl.DownloadError):
        pool.open("https://good.example/a", redirect_allowed=_deny)
    assert seen == ["https://other.example/b"]


# ---------------------------------------------------------------------------
# stream_download: hasher seeding on restart vs resume
# ---------------------------------------------------------------------------
class _NoResetHasher:
    """Digest-like object *without* ``reset()`` to catch stale-state corruption."""

    def __init__(self) -> None:
        self.data = b""

    def update(self, block: bytes) -> None:
        self.data += block


class _FakePool:
    def __init__(self, response: _FakeStreamResponse):
        self._response = response
        self.calls: list[tuple[str, dict[str, object]]] = []

    def open(self, url: str, **kwargs: object) -> dl.HttpResponse:
        self.calls.append((url, kwargs))
        return dl.HttpResponse(self._response)


def test_stream_download_200_restart_hashes_only_fresh_bytes(tmp_path: Path) -> None:
    body = b"fresh-body" * 10
    dest = tmp_path / "f.bin"
    (tmp_path / "f.bin.part").write_bytes(b"stale partial")
    resp = _FakeStreamResponse(200, {"Content-Length": str(len(body))}, body)
    hasher = _NoResetHasher()

    dl.stream_download(
        "https://x/y", dest, pool=_FakePool(resp), hasher=hasher, expected_size=len(body)
    )

    assert hasher.data == body


def test_stream_download_seeds_hasher_from_existing_part(tmp_path: Path) -> None:
    body = b"abcdefghij" * 6
    half = len(body) // 2
    dest = tmp_path / "f.bin"
    (tmp_path / "f.bin.part").write_bytes(body[:half])
    tail = body[half:]
    resp = _FakeStreamResponse(
        206,
        {
            "Content-Length": str(len(tail)),
            "Content-Range": f"bytes {half}-{len(body) - 1}/{len(body)}",
        },
        tail,
    )
    hasher = _NoResetHasher()

    dl.stream_download(
        "https://x/y", dest, pool=_FakePool(resp), hasher=hasher, expected_size=len(body)
    )

    assert hasher.data == body


def test_stream_download_forwards_redirect_guard(tmp_path: Path) -> None:
    resp = _FakeStreamResponse(200, {"Content-Length": "1"}, b"x")
    pool = _FakePool(resp)

    def _guard(url: str) -> bool:
        return True

    dl.stream_download("https://x/y", tmp_path / "f.bin", pool=pool, redirect_allowed=_guard)
    assert pool.calls[0][1]["redirect_allowed"] is _guard


# ---------------------------------------------------------------------------
# download: unverifiable part is not reused
# ---------------------------------------------------------------------------
def test_download_does_not_reuse_part_without_expectations(
    tmp_path: Path, monkeypatch: pytest.MonkeyPatch
) -> None:
    body = b"network body"
    dest = tmp_path / "game.bin"
    (tmp_path / "game.bin.part").write_bytes(b"stale partial")
    calls: list[dict[str, str]] = []
    monkeypatch.setattr(dl, "http_open", _fake_http(body, calls))

    result = dl.download("http://example.invalid/game", dest)

    assert calls, "an unverifiable part must be re-downloaded, not reused"
    assert result.size == len(body)
    assert dest.read_bytes() == body


# ---------------------------------------------------------------------------
# stream_download: a stale/complete .part must not wedge the install (HTTP 416)
# ---------------------------------------------------------------------------
class _SeqPool:
    def __init__(self, *responses: _FakeStreamResponse):
        self._responses = list(responses)
        self.calls: list[tuple[str, dict[str, object]]] = []

    def open(self, url: str, **kwargs: object) -> dl.HttpResponse:
        self.calls.append((url, kwargs))
        return dl.HttpResponse(self._responses.pop(0))


def test_stream_download_416_on_resume_restarts_from_scratch(tmp_path: Path) -> None:
    body = b"complete-file!" * 8
    dest = tmp_path / "f.bin"
    part = tmp_path / "f.bin.part"
    part.write_bytes(body)  # the whole file was already on disk when it was cancelled
    pool = _SeqPool(
        _FakeStreamResponse(416, {"Content-Range": f"bytes */{len(body)}"}, b""),
        _FakeStreamResponse(200, {"Content-Length": str(len(body))}, body),
    )
    hasher = _NoResetHasher()

    result = dl.stream_download(
        "https://x/y", dest, pool=pool, hasher=hasher, expected_size=len(body)
    )

    assert result.size == len(body)
    assert part.read_bytes() == body
    assert hasher.data == body  # no stale bytes mixed in
    assert len(pool.calls) == 2
    assert "Range" not in pool.calls[1][1].get("headers", {})


def test_stream_download_oversized_part_is_discarded_before_requesting(tmp_path: Path) -> None:
    body = b"abc" * 10
    dest = tmp_path / "f.bin"
    (tmp_path / "f.bin.part").write_bytes(body + b"EXTRA")
    pool = _SeqPool(_FakeStreamResponse(200, {"Content-Length": str(len(body))}, body))

    dl.stream_download("https://x/y", dest, pool=pool, expected_size=len(body))

    assert "Range" not in pool.calls[0][1].get("headers", {})
    assert (tmp_path / "f.bin.part").read_bytes() == body


def test_download_416_on_resume_restarts_the_file(
    tmp_path: Path, monkeypatch: pytest.MonkeyPatch
) -> None:
    import io

    body = b"runner-archive" * 5
    dest = tmp_path / "r.tar"
    (tmp_path / "r.tar.part").write_bytes(body)
    seen: list[str | None] = []

    class _Resp(io.BytesIO):
        status = 200
        headers = {"Content-Length": str(len(body))}

        def getcode(self) -> int:
            return 200

        def __enter__(self):
            return self

        def __exit__(self, *a):
            return False

    def fake_http_open(url, *, headers=None, timeout=0, redirect_allowed=None):
        seen.append((headers or {}).get("Range"))
        if (headers or {}).get("Range"):
            raise dl.DownloadError(f"HTTP 416 for {url}")
        return _Resp(body)

    monkeypatch.setattr(dl, "http_open", fake_http_open)
    dl._download_once(
        "https://x/r.tar",
        dest.with_name("r.tar.part"),
        headers=None,
        timeout=5,
        resume=True,
        emit=lambda e: None,
        cancel=None,
        chunk=1024,
    )
    assert seen == [f"bytes={len(body)}-", None]
    assert (tmp_path / "r.tar.part").read_bytes() == body


def test_sleep_never_passes_negative_duration(monkeypatch: pytest.MonkeyPatch) -> None:
    ticks = iter([0.0, 0.1, 5.0, 5.0, 5.0])
    monkeypatch.setattr(dl.time, "monotonic", lambda: next(ticks))
    seen: list[float] = []
    monkeypatch.setattr(dl.time, "sleep", lambda s: seen.append(s))
    dl._sleep(1, None)
    assert seen and all(s >= 0 for s in seen)


def test_download_fsyncs_destination_directory(
    tmp_path: Path, monkeypatch: pytest.MonkeyPatch
) -> None:
    import os
    import stat

    body = b"x" * 100
    monkeypatch.setattr(dl, "http_open", _fake_http(body))
    modes: list[bool] = []
    real = os.fsync

    def spy(fd):
        modes.append(stat.S_ISDIR(os.fstat(fd).st_mode))
        return real(fd)

    monkeypatch.setattr(dl.os, "fsync", spy)
    dl.download("http://example.invalid/g", tmp_path / "g.bin")
    assert True in modes


# ---------------------------------------------------------------------------
# resume: server claims a different start than the bytes already on disk (#84)
# ---------------------------------------------------------------------------
def test_download_rejects_server_resuming_at_wrong_offset(
    tmp_path: Path, monkeypatch: pytest.MonkeyPatch
) -> None:
    body = bytes(range(256)) * 8  # 2048 bytes, non-trivial
    half = len(body) // 2
    dest = tmp_path / "game.bin"
    part = tmp_path / "game.bin.part"
    part.write_bytes(body[:half])

    def _open(url: str, *, headers=None, timeout=dl.DEFAULT_TIMEOUT):
        headers = dict(headers or {})
        offset = int(headers["Range"].split("=")[1].split("-")[0])
        start = offset + 5  # the server lies about where the resume begins
        return FakeResponse(
            body[start:],
            status=206,
            headers={
                "Content-Length": str(len(body) - start),
                "Content-Range": f"bytes {start}-{len(body) - 1}/{len(body)}",
            },
        )

    monkeypatch.setattr(dl, "http_open", _open)
    with pytest.raises(dl.DownloadError, match="resumed at"):
        dl.download(
            "http://example.invalid/game",
            dest,
            expected_size=len(body),
            expected_sha256=_sha256(body),
            retries=1,
            backoff=0,
        )

    assert not dest.exists()
    assert part.read_bytes() == body[:half]  # the bad response added nothing


def test_stream_download_rejects_server_resuming_at_wrong_offset(tmp_path: Path) -> None:
    body = b"abcdefghij" * 6
    half = len(body) // 2
    dest = tmp_path / "f.bin"
    part = tmp_path / "f.bin.part"
    part.write_bytes(body[:half])
    start = half + 5  # the server lies about where the resume begins
    resp = _FakeStreamResponse(
        206,
        {
            "Content-Length": str(len(body) - start),
            "Content-Range": f"bytes {start}-{len(body) - 1}/{len(body)}",
        },
        body[start:],
    )

    with pytest.raises(dl.DownloadError, match="resumed at"):
        dl.stream_download("https://x/y", dest, pool=_FakePool(resp), expected_size=len(body))

    assert not dest.exists()
    assert part.read_bytes() == body[:half]  # untouched: no bytes were spliced in


def _serve(label: str, log: list):
    import http.server
    import threading

    class Handler(http.server.BaseHTTPRequestHandler):
        def do_GET(self):
            log.append((label, self.path, self.headers.get("Proxy-Authorization")))
            self.send_response(200)
            self.send_header("Content-Length", "2")
            self.end_headers()
            self.wfile.write(b"ok")

        def log_message(self, *args):
            pass

    server = http.server.ThreadingHTTPServer(("127.0.0.1", 0), Handler)
    threading.Thread(target=server.serve_forever, daemon=True).start()
    return server


def test_http_client_pool_goes_through_the_environment_proxy(
    monkeypatch: pytest.MonkeyPatch,
) -> None:
    log: list = []
    proxy = _serve("proxy", log)
    try:
        monkeypatch.setenv("http_proxy", f"http://user:pw@127.0.0.1:{proxy.server_port}")
        monkeypatch.delenv("no_proxy", raising=False)
        monkeypatch.delenv("NO_PROXY", raising=False)
        pool = dl.HttpClientPool()
        with pool.open("http://origin.invalid/file.bin") as resp:
            assert resp.read() == b"ok"
        pool.close()
    finally:
        proxy.shutdown()
    assert log == [("proxy", "http://origin.invalid/file.bin", "Basic dXNlcjpwdw==")]


def test_http_client_pool_skips_the_proxy_for_no_proxy_hosts(
    monkeypatch: pytest.MonkeyPatch,
) -> None:
    log: list = []
    proxy = _serve("proxy", log)
    origin = _serve("origin", log)
    try:
        monkeypatch.setenv("http_proxy", f"http://127.0.0.1:{proxy.server_port}")
        monkeypatch.setenv("no_proxy", "127.0.0.1")
        pool = dl.HttpClientPool()
        with pool.open(f"http://127.0.0.1:{origin.server_port}/b") as resp:
            assert resp.read() == b"ok"
        pool.close()
    finally:
        proxy.shutdown()
        origin.shutdown()
    assert log == [("origin", "/b", None)]


def test_http_client_pool_tunnels_https_through_the_proxy(
    monkeypatch: pytest.MonkeyPatch,
) -> None:
    monkeypatch.setenv("https_proxy", "http://127.0.0.1:3128")
    monkeypatch.delenv("no_proxy", raising=False)
    monkeypatch.delenv("NO_PROXY", raising=False)
    pool = dl.HttpClientPool()
    conn = pool._connection("https", "origin.invalid", 443, 5)
    assert (conn.host, conn.port) == ("127.0.0.1", 3128)
    assert (conn._tunnel_host, conn._tunnel_port) == ("origin.invalid", 443)
