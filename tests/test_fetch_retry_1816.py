# SPDX-License-Identifier: GPL-3.0-or-later
"""#1816: publisher index/manifest fetches retry transient failures.

Every test uses a fake ``download.http_get`` and a fake ``download.wait``: no
socket is opened and nothing waits for real. The cancel tests go through
``DirectBackend.plan()``/``predownload()`` so they exercise the production wiring,
not only the optional ``cancel=`` argument.
"""

from __future__ import annotations

import http.client
import logging
import random
import socket
import ssl
import urllib.error
from pathlib import Path

import pytest
from _backend_helpers import make_context, write_exe

from nadare.backends import direct
from nadare.backends.direct_strinova import StrinovaBackend
from nadare.core import directdl, download, sources
from nadare.core.events import Cancelled
from nadare.core.sources import bpsrglobal, strinova

URL = "https://resource-download.strinova.com/Client/Win/GameDepot/Game_Release/manifest.txt"
FIX = Path(__file__).parent / "fixtures" / "catalog"


def unreachable(url: str, inner: BaseException) -> download.DownloadError:
    """The error ``http_open`` raises: ``DownloadError`` caused by ``URLError(inner)``."""
    exc = download.DownloadError(f"cannot reach {url}: {inner}")
    exc.__cause__ = urllib.error.URLError(inner)
    return exc


def status_error(url: str, status: int, retry_after: float | None = None) -> download.DownloadError:
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
    monkeypatch.setattr(random, "random", lambda: 0.0)


def _strinova_backend(tmp_path, ctx=None):
    ctx = ctx or make_context(tmp_path, "direct-strinova", {"prefix": str(tmp_path / "prefix")})
    return StrinovaBackend(ctx), ctx


# --------------------------------------------------------------- retries happen
def test_two_timeouts_then_success(fake_get, fake_wait, no_jitter):
    get = fake_get(
        [
            unreachable(URL, TimeoutError("timed out")),
            unreachable(URL, TimeoutError("timed out")),
            b"data",
        ]
    )
    assert sources.default_fetch(URL) == b"data"
    assert len(get.calls) == 3
    assert [delay for delay, _cancel in fake_wait.calls] == [2.0, 8.0]


def test_backoff_has_jitter(monkeypatch, fake_get, fake_wait):
    monkeypatch.setattr(random, "random", lambda: 0.5)
    fake_get([unreachable(URL, TimeoutError("timed out")), unreachable(URL, TimeoutError()), b"ok"])
    assert sources.default_fetch(URL) == b"ok"
    # 0.5 of the 20 % jitter window on top of 2 s and 8 s.
    assert [delay for delay, _cancel in fake_wait.calls] == [2.2, 8.8]


def test_three_timeouts_raise_the_same_direct_error(fake_get, fake_wait, no_jitter):
    get = fake_get([unreachable(URL, TimeoutError("timed out"))] * 3)
    with pytest.raises(directdl.DirectError) as caught:
        sources.default_fetch(URL)
    assert str(caught.value) == f"cannot reach {URL}: timed out"
    assert len(get.calls) == 3
    assert len(fake_wait.calls) == 2


def test_headers_are_sent_on_every_attempt(fake_get, fake_wait):
    get = fake_get([unreachable(URL, TimeoutError()), b"ok"])
    assert sources.default_fetch(URL, {"X-Test": "1"}) == b"ok"
    assert [kwargs["headers"] for _url, kwargs in get.calls] == [{"X-Test": "1"}] * 2


@pytest.mark.parametrize(
    "inner",
    [
        TimeoutError("timed out"),
        ConnectionRefusedError(111, "Connection refused"),
        ConnectionResetError(104, "Connection reset by peer"),
        ConnectionAbortedError(103, "Software caused connection abort"),
        socket.gaierror(socket.EAI_AGAIN, "Temporary failure in name resolution"),
        OSError(101, "Network is unreachable"),
    ],
)
def test_transient_network_errors_are_retried(fake_get, fake_wait, inner):
    get = fake_get([unreachable(URL, inner), b"ok"])
    assert sources.default_fetch(URL) == b"ok"
    assert len(get.calls) == 2


@pytest.mark.parametrize(
    "inner",
    [
        PermissionError(13, "Permission denied"),
        ssl.SSLCertVerificationError(1, "certificate verify failed"),
        socket.gaierror(socket.EAI_NONAME, "Name or service not known"),
        http.client.InvalidURL("bad URL"),
    ],
)
def test_permanent_causes_are_not_retried(fake_get, fake_wait, inner):
    get = fake_get([unreachable(URL, inner)])
    with pytest.raises(directdl.DirectError):
        sources.default_fetch(URL)
    assert len(get.calls) == 1
    assert fake_wait.calls == []


def test_a_url_error_with_a_text_reason_is_not_retried(fake_get, fake_wait):
    exc = download.DownloadError(f"cannot reach {URL}: unknown url type: nope")
    exc.__cause__ = urllib.error.URLError("unknown url type: nope")
    get = fake_get([exc])
    with pytest.raises(directdl.DirectError):
        sources.default_fetch(URL)
    assert len(get.calls) == 1
    assert fake_wait.calls == []


# --------------------------------------------------------------- HTTP statuses
@pytest.mark.parametrize("status", [400, 401, 403, 404])
def test_client_errors_are_not_retried(fake_get, fake_wait, status):
    get = fake_get([status_error(URL, status)])
    with pytest.raises(directdl.DirectError, match=f"HTTP {status}"):
        sources.default_fetch(URL)
    assert len(get.calls) == 1
    assert fake_wait.calls == []


@pytest.mark.parametrize("status", [500, 502, 503, 504])
def test_server_errors_are_retried(fake_get, fake_wait, no_jitter, status):
    get = fake_get([status_error(URL, status), b"ok"])
    assert sources.default_fetch(URL) == b"ok"
    assert len(get.calls) == 2
    assert fake_wait.calls[0][0] == 2.0


@pytest.mark.parametrize("status", [408, 425])
def test_408_and_425_stay_retried_like_full_downloads(fake_get, fake_wait, status):
    get = fake_get([status_error(URL, status), b"ok"])
    assert sources.default_fetch(URL) == b"ok"
    assert len(get.calls) == 2


def test_429_without_retry_after_uses_the_normal_backoff(fake_get, fake_wait, no_jitter):
    get = fake_get([status_error(URL, 429), b"ok"])
    assert sources.default_fetch(URL) == b"ok"
    assert len(get.calls) == 2
    assert fake_wait.calls[0][0] == 2.0


def test_429_honours_retry_after(fake_get, fake_wait):
    get = fake_get([status_error(URL, 429, retry_after=5), b"ok"])
    assert sources.default_fetch(URL) == b"ok"
    assert len(get.calls) == 2
    assert fake_wait.calls[0][0] == 5


def test_retry_after_is_capped_at_thirty_seconds(fake_get, fake_wait):
    get = fake_get([status_error(URL, 503, retry_after=120), b"ok"])
    assert sources.default_fetch(URL) == b"ok"
    assert len(get.calls) == 2
    assert fake_wait.calls[0][0] == 30


# --------------------------------------------------------------- not retried
def test_size_cap_refusal_is_not_retried(fake_get, fake_wait):
    get = fake_get([download.DownloadError(f"response too large (over 64 bytes) for {URL}")])
    with pytest.raises(directdl.DirectError, match="too large"):
        sources.default_fetch(URL)
    assert len(get.calls) == 1
    assert fake_wait.calls == []


def test_redirect_policy_refusal_is_not_retried(fake_get, fake_wait):
    get = fake_get([download.DownloadError("refusing insecure redirect https://a to http://b")])
    with pytest.raises(directdl.DirectError, match="refusing"):
        sources.default_fetch(URL)
    assert len(get.calls) == 1
    assert fake_wait.calls == []


# --------------------------------------------------------------- mid-body reads
@pytest.mark.parametrize(
    "raw",
    [
        TimeoutError("timed out while reading the body"),
        ConnectionResetError(104, "Connection reset by peer"),
        http.client.IncompleteRead(b"half", 10),
    ],
)
def test_mid_body_read_errors_are_retried(fake_get, fake_wait, no_jitter, raw):
    get = fake_get([raw, b"ok"])
    assert sources.default_fetch(URL) == b"ok"
    assert len(get.calls) == 2
    assert fake_wait.calls[0][0] == 2.0


def test_three_mid_body_errors_become_a_direct_error(fake_get, fake_wait):
    get = fake_get([ConnectionResetError(104, "reset")] * 3)
    with pytest.raises(directdl.DirectError, match="cannot reach"):
        sources.default_fetch(URL)
    assert len(get.calls) == 3


# --------------------------------------------------------------- config at call time
def test_attempts_follow_download_default_retries_at_call_time(
    monkeypatch, fake_get, fake_wait, no_jitter
):
    monkeypatch.setattr(download, "DEFAULT_RETRIES", 2)
    get = fake_get([unreachable(URL, TimeoutError())] * 3)
    with pytest.raises(directdl.DirectError):
        sources.default_fetch(URL)
    assert len(get.calls) == 2


# --------------------------------------------------------------- the log line
def test_retry_log_names_the_host_and_no_query_string(fake_get, fake_wait, caplog):
    url = URL + "?token=secret-value"
    fake_get([unreachable(url, TimeoutError("timed out")), b"ok"])
    with caplog.at_level(logging.INFO, logger="nadare.core.sources"):
        assert sources.default_fetch(url) == b"ok"
    retries = [r.getMessage() for r in caplog.records if "fetch retry" in r.getMessage()]
    assert retries == ["fetch retry 1/3 for resource-download.strinova.com: timeout"]
    assert "secret-value" not in retries[0]


def test_second_retry_logs_two_of_three(fake_get, fake_wait, caplog, no_jitter):
    fake_get([unreachable(URL, ConnectionRefusedError(111, "refused"))] * 2 + [b"ok"])
    with caplog.at_level(logging.INFO, logger="nadare.core.sources"):
        assert sources.default_fetch(URL) == b"ok"
    retries = [r.getMessage() for r in caplog.records if "fetch retry" in r.getMessage()]
    assert retries == [
        "fetch retry 1/3 for resource-download.strinova.com: connection error",
        "fetch retry 2/3 for resource-download.strinova.com: connection error",
    ]


def test_malformed_url_is_a_direct_error_and_logs_no_url(fake_get, fake_wait, caplog):
    url = "https://[bad/path?token=secret-value"
    fake_get([unreachable(url, TimeoutError()), b"ok"])
    with caplog.at_level(logging.INFO, logger="nadare.core.sources"):
        assert sources.default_fetch(url) == b"ok"
    retries = [r.getMessage() for r in caplog.records if "fetch retry" in r.getMessage()]
    assert retries == ["fetch retry 1/3 for <invalid url>: timeout"]
    assert "secret-value" not in retries[0]


def test_a_hostless_url_logs_no_url(fake_get, fake_wait, caplog):
    url = "manifest.txt?token=secret-value"
    fake_get([unreachable(url, TimeoutError()), b"ok"])
    with caplog.at_level(logging.INFO, logger="nadare.core.sources"):
        assert sources.default_fetch(url) == b"ok"
    retries = [r.getMessage() for r in caplog.records if "fetch retry" in r.getMessage()]
    assert retries == ["fetch retry 1/3 for <invalid url>: timeout"]
    assert "secret-value" not in retries[0]


def test_a_malformed_url_that_fails_is_a_direct_error(fake_get, fake_wait):
    url = "https://[bad/path"
    fake_get([unreachable(url, TimeoutError())] * 3)
    with pytest.raises(directdl.DirectError, match="cannot reach"):
        sources.default_fetch(url)


# --------------------------------------------------------------- patch lists
def test_patch_list_fetch_retries_and_keeps_its_max_bytes(fake_get, fake_wait):
    get = fake_get([unreachable(URL, TimeoutError()), b"patch list"])
    assert sources.patch_list_fetch(URL) == b"patch list"
    assert len(get.calls) == 2
    assert all(kwargs["max_bytes"] == sources.MAX_PATCH_LIST_BYTES for _url, kwargs in get.calls)


def test_default_fetch_keeps_http_gets_default_max_bytes(fake_get, fake_wait):
    get = fake_get([unreachable(URL, TimeoutError()), b"ok"])
    assert sources.default_fetch(URL) == b"ok"
    assert all("max_bytes" not in kwargs for _url, kwargs in get.calls)


# --------------------------------------------------------------- other sources
def test_bpsr_plan_retries_and_keeps_its_redirect_policy(monkeypatch, fake_wait, no_jitter):
    base = "https://pc-download.playbpsr.com/apj_obt"
    routes = {
        f"{base}/branch.txt": b"release_4.4=release\n",
        f"{base}/release_4.4/last/verlist.txt": (FIX / "bpsr_verlist.txt").read_bytes(),
        f"{base}/release_4.4/last/md5list.txt": (FIX / "bpsr_md5list.txt").read_bytes(),
    }
    calls = []

    def get(url, **kwargs):
        calls.append(url)
        assert kwargs["redirect_allowed"] is bpsrglobal.url_allowed
        if calls.count(url) == 1 and url == f"{base}/branch.txt":
            raise unreachable(url, ConnectionRefusedError(111, "Connection refused"))
        return routes[url]

    monkeypatch.setattr(download, "http_get", get)
    plan = bpsrglobal.resolve()
    assert plan.version == "1.0.46902.268"
    assert calls == [
        f"{base}/branch.txt",
        f"{base}/branch.txt",
        f"{base}/release_4.4/last/verlist.txt",
        f"{base}/release_4.4/last/md5list.txt",
    ]


def test_strinova_plan_succeeds_after_one_transient_timeout(tmp_path, monkeypatch, fake_wait):
    index = (FIX / "strinova_index.txt").read_bytes()
    files = (FIX / "strinova_files.txt").read_bytes()
    calls = []

    def flaky(url, **kwargs):
        calls.append(url)
        if len(calls) == 1:
            raise unreachable(url, TimeoutError("timed out"))
        return index if url == strinova.MANIFEST_URL else files

    monkeypatch.setattr(download, "http_get", flaky)
    backend, _ctx = _strinova_backend(tmp_path)
    plan = backend.plan()
    assert plan.version == "1.12.1.2"
    assert len(calls) == 3, "index timed out once, was retried, then the file list was read"


# --------------------------------------------------------------- cancel, in production
def test_plan_is_cancelled_before_the_first_fetch(tmp_path, monkeypatch):
    backend, ctx = _strinova_backend(tmp_path)
    ctx.cancel.cancel()
    calls = []

    def get(url, **kwargs):
        calls.append(url)
        return b""

    monkeypatch.setattr(download, "http_get", get)
    with pytest.raises(Cancelled):
        backend.plan()
    assert calls == []


def test_plan_cancel_during_the_retry_stops_at_once(tmp_path, monkeypatch):
    backend, ctx = _strinova_backend(tmp_path)
    calls = []

    def get(url, **kwargs):
        calls.append(url)
        ctx.cancel.cancel()  # Stop arrives while the failed attempt unwinds
        raise status_error(url, 429, retry_after=0)

    monkeypatch.setattr(download, "http_get", get)
    with pytest.raises(Cancelled):
        backend.plan()
    assert len(calls) == 1


def test_predownload_source_fetch_gets_the_backend_cancel(tmp_path, monkeypatch, fake_wait):
    backend = direct.ZenlessBackend(
        make_context(tmp_path, "direct-zzz", {"prefix": str(tmp_path / "prefix")})
    )
    write_exe(backend.direct_target(), "MZ")
    backend._write_version_file("3.3.0", "zzz")
    ctx = backend.ctx
    get = FakeGet([unreachable(URL, TimeoutError()), b"index"])
    monkeypatch.setattr(download, "http_get", get)

    def factory(fetch=None, **kwargs):
        assert sources.default_fetch(URL) == b"index"
        return directdl.Plan("zzz", "3.4.0", exe="ZenlessZoneZero.exe")

    monkeypatch.setattr(type(backend), "predownload_factory", staticmethod(factory))
    monkeypatch.setattr(directdl, "predownload", lambda *a, **kw: {"bytes": 1})
    assert backend.predownload() == {"bytes": 1}
    assert len(fake_wait.calls) == 1
    assert fake_wait.calls[0][1] is ctx.cancel, "the backend's cancel token was not passed down"


def test_update_patch_list_fetch_gets_the_backend_cancel(tmp_path, monkeypatch, fake_wait):
    import threading

    from nadare.core import kuropatch

    backend = direct.WutheringWavesBackend(
        make_context(tmp_path, "direct-wuwa", {"prefix": str(tmp_path / "prefix")})
    )
    url = "https://cdn.example/p/3.7.1.json"
    extra = {
        kuropatch.EXTRA_KEY: {
            "root": "https://cdn.example/",
            "packs": {
                "common": [{"version": "3.7.1", "indexFile": "p/3.7.1.json", "baseUrl": "x/"}]
            },
        }
    }
    plan = directdl.Plan("wuwa", "3.8.0", files=(), extra=extra)
    backend._patch_numbers = {}
    get = FakeGet([unreachable(url, ConnectionResetError(104, "reset")), b'{"resource": []}'])
    monkeypatch.setattr(download, "http_get", get)
    assert backend._apply_update_patches(plan, "3.7.1", threading.Event()) == ()
    assert len(fake_wait.calls) == 1
    assert fake_wait.calls[0][1] is backend.ctx.cancel
