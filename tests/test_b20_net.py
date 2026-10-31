# SPDX-License-Identifier: GPL-3.0-or-later
"""Regression tests for the b20 network/intake work (#769, #623, #612, #194)."""

from __future__ import annotations

import urllib.error
from pathlib import Path

import pytest

from nadare import app as nadare_app
from nadare.core import directdl as dd
from nadare.core import download as dl
from nadare.core import kurotokens, sophon, updatecheck, yostar
from nadare.core.sources import ungzip


def _http_error(code: int, retry_after: str | None = None) -> urllib.error.HTTPError:
    headers = {} if retry_after is None else {"Retry-After": retry_after}
    return urllib.error.HTTPError(
        "http://example.invalid/game", code, f"HTTP {code}", headers, None
    )


class _FailingOpener:
    def __init__(self, error: BaseException) -> None:
        self.error = error

    def open(self, *args: object, **kwargs: object) -> None:
        raise self.error


# ---------------------------------------------------------------------------
# #769 download retries
# ---------------------------------------------------------------------------
def test_http_open_carries_status_and_retry_after(monkeypatch: pytest.MonkeyPatch) -> None:
    monkeypatch.setattr(
        dl.urllib.request,
        "build_opener",
        lambda *a, **k: _FailingOpener(_http_error(429, "7")),
    )
    with pytest.raises(dl.HTTPStatusError) as info:
        dl.http_open("http://example.invalid/game")
    assert info.value.status == 429
    assert info.value.retry_after == 7


def test_retry_delay_is_jittered_bounded_and_varies() -> None:
    low = dl.retry_delay(2.0, 1, rand=lambda: 0.0)
    high = dl.retry_delay(2.0, 1, rand=lambda: 1.0)
    assert 2.0 <= low < high <= dl.MAX_BACKOFF
    assert low != high
    assert dl.retry_delay(30.0, 8, rand=lambda: 1.0) <= dl.MAX_BACKOFF


def test_download_does_not_retry_a_permanent_4xx(
    tmp_path: Path, monkeypatch: pytest.MonkeyPatch
) -> None:
    calls: list[str] = []

    def _open(url: str, **kwargs: object) -> None:
        calls.append(url)
        raise dl.HTTPStatusError("HTTP 404 for " + url, status=404, url=url)

    monkeypatch.setattr(dl, "http_open", _open)
    monkeypatch.setattr(dl, "_sleep", lambda *a, **k: None)
    with pytest.raises(dl.DownloadError) as info:
        dl.download("http://example.invalid/game", tmp_path / "game.bin", retries=3)
    assert len(calls) == 1
    assert "HTTP 404" in str(info.value)


@pytest.mark.parametrize("status", [408, 425])
def test_download_retries_a_timeout_or_too_early_4xx(
    tmp_path: Path, monkeypatch: pytest.MonkeyPatch, status: int
) -> None:
    """A 408/425 means "try again", unlike a 403/404; the old code retried every error."""
    body = b"payload"
    calls: list[str] = []

    def _open(url: str, **kwargs: object) -> object:
        calls.append(url)
        if len(calls) == 1:
            raise dl.HTTPStatusError(f"HTTP {status} for " + url, status=status, url=url)
        return _body_response(body)

    monkeypatch.setattr(dl, "http_open", _open)
    monkeypatch.setattr(dl, "_sleep", lambda *a, **k: None)
    dl.download("http://example.invalid/game", tmp_path / "game.bin", retries=3)
    assert len(calls) == 2


@pytest.mark.parametrize("status", [400, 401, 403, 404, 410])
def test_download_does_not_retry_these_4xx_either(
    tmp_path: Path, monkeypatch: pytest.MonkeyPatch, status: int
) -> None:
    calls: list[str] = []

    def _open(url: str, **kwargs: object) -> None:
        calls.append(url)
        raise dl.HTTPStatusError(f"HTTP {status} for " + url, status=status, url=url)

    monkeypatch.setattr(dl, "http_open", _open)
    monkeypatch.setattr(dl, "_sleep", lambda *a, **k: None)
    with pytest.raises(dl.DownloadError):
        dl.download("http://example.invalid/game", tmp_path / "game.bin", retries=3)
    assert len(calls) == 1


def test_download_retries_429_with_capped_retry_after(
    tmp_path: Path, monkeypatch: pytest.MonkeyPatch
) -> None:
    body = b"payload"
    calls: list[str] = []
    sleeps: list[float] = []

    def _open(url: str, **kwargs: object) -> object:
        calls.append(url)
        if len(calls) == 1:
            raise dl.HTTPStatusError(
                "HTTP 429 for " + url, status=429, retry_after=600, url=url
            )
        return _body_response(body)

    monkeypatch.setattr(dl, "http_open", _open)
    monkeypatch.setattr(dl, "_sleep", lambda seconds, cancel: sleeps.append(seconds))
    result = dl.download("http://example.invalid/game", tmp_path / "game.bin", retries=3)

    assert result.size == len(body)
    assert sleeps == [dl.RETRY_AFTER_MAX]


def test_download_backoff_is_jittered_between_attempts(
    tmp_path: Path, monkeypatch: pytest.MonkeyPatch
) -> None:
    sleeps: list[float] = []
    monkeypatch.setattr(
        dl,
        "http_open",
        lambda url, **kwargs: (_ for _ in ()).throw(OSError("connection reset")),
    )
    monkeypatch.setattr(dl, "_sleep", lambda seconds, cancel: sleeps.append(seconds))
    monkeypatch.setattr(dl.random, "random", iter([0.0, 1.0]).__next__)

    with pytest.raises(dl.DownloadError):
        dl.download("http://example.invalid/game", tmp_path / "game.bin", retries=3)

    assert len(sleeps) == 2
    assert all(0 < s <= dl.MAX_BACKOFF for s in sleeps)
    assert sleeps[0] != sleeps[1]


def _body_response(body: bytes) -> object:
    class _Resp:
        status = 200
        headers = {"Content-Length": str(len(body))}
        _pos = 0

        def read(self, n: int = -1) -> bytes:
            data = body[self._pos :] if n < 0 else body[self._pos : self._pos + n]
            self._pos += len(data)
            return data

        def __enter__(self) -> _Resp:
            return self

        def __exit__(self, *exc: object) -> bool:
            return False

    return _Resp()


# ---------------------------------------------------------------------------
# #769 Yostar retry pacing
# ---------------------------------------------------------------------------
def _manifest(tmp_path: Path, name: str = "x.bin") -> yostar.Manifest:
    return yostar.Manifest(
        source=str(tmp_path / "manifest.json"),
        base_url="https://cdn.yo-star.com/game/",
        game_tag="test",
        version="1.0",
        generated_at="2026-10-31T00:00:00Z",
        files=(yostar.ManifestFile(name, 1, 0),),
    )


def test_yostar_sync_entry_sleeps_between_attempts_and_keeps_message(
    tmp_path: Path, monkeypatch: pytest.MonkeyPatch
) -> None:
    manifest = _manifest(tmp_path)
    entry = manifest.files[0]
    sleeps: list[float] = []

    def _fail(*args: object, **kwargs: object) -> None:
        raise dl.DownloadError("cannot reach cdn: connection reset")

    monkeypatch.setattr(dl, "stream_download", _fail)
    progress = yostar._SyncProgress(lambda _event: None, 1)

    with pytest.raises(yostar.ManifestError) as info:
        yostar._sync_entry(
            entry,
            root=tmp_path,
            manifest=manifest,
            pool=object(),
            progress=progress,
            cancel=None,
            attempts=3,
            allowed_hosts=None,
            base_host="cdn.yo-star.com",
            chunk=1024,
            sleeper=sleeps.append,
        )

    assert len(sleeps) == 2
    assert all(0 < s <= yostar.RETRY_SLEEP_MAX for s in sleeps)
    assert "Could not download the game files" in str(info.value)


# ---------------------------------------------------------------------------
# #623 intake: gzip cap, CLI encoding, pinned update fallback, Sophon sizes,
# and a typed error for an install path that is a file
# ---------------------------------------------------------------------------
def test_ungzip_caps_decompressed_size() -> None:
    import gzip

    bomb = gzip.compress(b"a" * 4096)
    with pytest.raises(dd.DirectError):
        ungzip(bomb, limit=1024)
    assert ungzip(bomb, limit=8192) == b"a" * 4096


def test_ungzip_wraps_corrupt_stream_in_direct_error() -> None:
    with pytest.raises(dd.DirectError):
        ungzip(b"\x1f\x8bnot really gzip")


def test_cli_reconfigures_stdout_and_stderr_to_backslashreplace(
    monkeypatch: pytest.MonkeyPatch,
) -> None:
    class _Stream:
        def __init__(self) -> None:
            self.calls: list[dict[str, object]] = []

        def reconfigure(self, **kwargs: object) -> None:
            self.calls.append(kwargs)

    out, err = _Stream(), _Stream()
    monkeypatch.setattr(nadare_app.sys, "stdout", out)
    monkeypatch.setattr(nadare_app.sys, "stderr", err)

    nadare_app._make_output_encoding_safe()

    assert out.calls == [{"errors": "backslashreplace"}]
    assert err.calls == [{"errors": "backslashreplace"}]


def test_update_redirect_fallback_uses_the_pinned_ca_context(
    monkeypatch: pytest.MonkeyPatch,
) -> None:
    import urllib.request

    sentinel = object()
    seen: list[object] = []

    monkeypatch.setattr(updatecheck.tls, "ssl_context", lambda: sentinel)

    class _Opener:
        def open(self, *args: object, **kwargs: object) -> None:
            raise OSError("offline")

    def _build(*handlers: object) -> _Opener:
        seen.extend(handlers)
        return _Opener()

    monkeypatch.setattr(urllib.request, "build_opener", _build)
    updatecheck._github_latest_redirect("https://github.com/a/b/releases/latest", timeout=1)

    https = [h for h in seen if isinstance(h, urllib.request.HTTPSHandler)]
    assert https and https[0]._context is sentinel


def _varint(value: int) -> bytes:
    out = bytearray()
    while True:
        part = value & 0x7F
        value >>= 7
        if value:
            out.append(part | 0x80)
        else:
            out.append(part)
            return bytes(out)


def _field_bytes(number: int, value: bytes | str) -> bytes:
    raw = value.encode() if isinstance(value, str) else value
    return _varint((number << 3) | 2) + _varint(len(raw)) + raw


def _field_varint(number: int, value: int) -> bytes:
    return _varint(number << 3) + _varint(value)


def test_sophon_manifest_rejects_an_implausible_file_size() -> None:
    lying = _field_bytes(
        1,
        _field_bytes(1, "x.bin") + _field_varint(4, 1 << 40),
    )
    with pytest.raises(sophon.SophonError):
        sophon.parse_manifest(lying)


def test_sophon_manifest_rejects_an_implausible_chunk_size() -> None:
    chunk = (
        _field_bytes(1, "chunk.bin")
        + _field_bytes(2, "0" * 32)
        + _field_varint(4, 1 << 31)
        + _field_varint(5, 1 << 31)
    )
    lying = _field_bytes(
        1,
        _field_bytes(1, "x.bin")
        + _field_bytes(2, chunk)
        + _field_varint(4, 1024),
    )
    with pytest.raises(sophon.SophonError):
        sophon.parse_manifest(lying)


def test_direct_sync_rejects_an_install_path_that_is_a_file(tmp_path: Path) -> None:
    target = tmp_path / "game"
    target.write_text("not a folder")
    with pytest.raises(dd.DirectError):
        dd.sync(dd.Plan("T", "1"), target)
    assert target.read_text() == "not a folder"


def test_kuro_token_fetch_refuses_a_cross_host_redirect(
    monkeypatch: pytest.MonkeyPatch,
) -> None:
    seen: dict[str, object] = {}

    class _Response:
        def read(self, amount: int) -> bytes:
            return b"{}"

        def __enter__(self) -> _Response:
            return self

        def __exit__(self, *exc: object) -> bool:
            return False

    def _open(url: str, **kwargs: object) -> _Response:
        seen.update(kwargs)
        return _Response()

    monkeypatch.setattr(kurotokens.download, "http_open", _open)
    assert kurotokens._fetch("https://index.example/tokens.json") == b"{}"

    allowed = seen["redirect_allowed"]
    assert callable(allowed)
    assert allowed("https://index.example/other.json")
    assert not allowed("https://evil.example/tokens.json")



# ---------------------------------------------------------------------------
# review wave 3 (network)
# ---------------------------------------------------------------------------
def _chunk_message(offset: int, size: int) -> bytes:
    return (
        _field_bytes(1, "chunk.bin")
        + _field_bytes(2, "0" * 32)
        + _field_varint(3, offset)
        + _field_varint(4, size)
        + _field_varint(5, size)
        + _field_bytes(7, "1" * 32)
    )


def _one_file_manifest(chunk: bytes, file_size: int) -> bytes:
    return _field_bytes(
        1, _field_bytes(1, "x.bin") + _field_bytes(2, chunk) + _field_varint(4, file_size)
    )


def test_sophon_chunk_must_lie_inside_its_file() -> None:
    inside = _one_file_manifest(_chunk_message(0, 512), 1024)
    assert sophon.parse_manifest(inside)[0].size == 1024
    exact = _one_file_manifest(_chunk_message(512, 512), 1024)
    assert sophon.parse_manifest(exact)[0].chunks[0].offset == 512
    far = _one_file_manifest(_chunk_message(60 << 30, 1 << 20), 1024)
    with pytest.raises(sophon.SophonError, match="outside"):
        sophon.parse_manifest(far)
    past_end = _one_file_manifest(_chunk_message(900, 512), 1024)
    with pytest.raises(sophon.SophonError, match="outside"):
        sophon.parse_manifest(past_end)


def test_a_pool_hop_that_needs_an_unsupported_proxy_is_refused_when_strict(monkeypatch) -> None:
    monkeypatch.setenv("https_proxy", "socks5://127.0.0.1:9")
    monkeypatch.setenv("HTTPS_PROXY", "socks5://127.0.0.1:9")
    monkeypatch.delenv("no_proxy", raising=False)
    monkeypatch.delenv("NO_PROXY", raising=False)
    pool = dl.HttpClientPool()
    try:
        with pytest.raises(dl.ProxyNotSupported):
            pool.open("https://cdn.example/file.bin", strict_proxy=True)
    finally:
        pool.close()


def test_a_redirect_into_a_proxied_scheme_falls_back_to_urllib(
    tmp_path: Path, monkeypatch: pytest.MonkeyPatch
) -> None:
    """http file URL, https redirect, socks proxy: the pool cannot honour the proxy."""
    monkeypatch.setenv("https_proxy", "socks5://127.0.0.1:9")
    monkeypatch.setenv("HTTPS_PROXY", "socks5://127.0.0.1:9")
    monkeypatch.delenv("http_proxy", raising=False)
    monkeypatch.delenv("HTTP_PROXY", raising=False)
    monkeypatch.delenv("no_proxy", raising=False)
    monkeypatch.delenv("NO_PROXY", raising=False)

    class _Pool:
        def open(self, url, **kwargs):
            assert kwargs.get("strict_proxy") is True
            raise dl.ProxyNotSupported("https_proxy needs a proxy type the pool cannot use")

    used: list[str] = []

    def fake_http_open(url, **kwargs):
        used.append(url)
        raise dl.DownloadError("stop here")

    monkeypatch.setattr(dl, "http_open", fake_http_open)
    with pytest.raises(dl.DownloadError, match="stop here"):
        dl._download_once(
            "http://mirror.example/file.bin",
            tmp_path / "f.part",
            headers=None,
            timeout=5,
            resume=False,
            emit=lambda e: None,
            cancel=None,
            chunk=1024,
            pool=_Pool(),
        )
    assert used == ["http://mirror.example/file.bin"]
