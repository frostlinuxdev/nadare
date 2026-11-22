# SPDX-License-Identifier: GPL-3.0-or-later
"""Adversarial tests for Area 4: parallel ranged downloads, resume, shared speed limit."""

from __future__ import annotations

import io
import json
import threading
from pathlib import Path

import pytest

from nadare.core import download as dl
from nadare.core import transfer


class _FakeResp:
    """Minimal streaming HTTP response matching HttpResponse/urllib response interface."""

    def __init__(self, status: int, headers: dict[str, str], body: bytes) -> None:
        self.status = status
        self.headers = headers
        self._buf = io.BytesIO(body)
        self.closed = False

    def getcode(self) -> int:
        return self.status

    def header(self, name: str, default: str | None = None) -> str | None:
        for k, v in self.headers.items():
            if k.lower() == name.lower():
                return v
        return default

    def read(self, amount: int = -1) -> bytes:
        return self._buf.read(amount)

    def close(self) -> None:
        self.closed = True

    def __enter__(self):
        return self

    def __exit__(self, *exc):
        self.close()
        return False


class _FakePool:
    def __init__(self, handler) -> None:
        self._handler = handler

    def open(
        self,
        url,
        *,
        headers=None,
        timeout=None,
        redirects=5,
        redirect_allowed=None,
        strict_proxy=True,
    ):
        return self._handler(url, headers or {})

    def close(self) -> None:
        pass


@pytest.fixture(autouse=True)
def small_ranges(monkeypatch):
    monkeypatch.setattr(dl, "RANGE_THRESHOLD", 100)
    monkeypatch.setattr(dl.urllib.request, "getproxies", lambda: {})


def test_oversized_range_is_rejected_on_every_retry(tmp_path):
    data = b"A" * 8192
    calls = []

    def handle(url, headers):
        span = headers["Range"]
        calls.append(span)
        start, end = map(int, span.removeprefix("bytes=").split("-"))
        body = data[start : end + 1]
        if span == "bytes=0-2047":
            body = b"X" * 2048 + b"extra"
        return _FakeResp(206, {"Content-Range": f"bytes {start}-{end}/8192"}, body)

    target = tmp_path / "file"
    with pytest.raises(dl.DownloadError, match="larger than requested"):
        dl.download(
            "https://cdn.invalid/file",
            target,
            expected_size=8192,
            chunk=1024,
            retries=2,
            backoff=0,
            pool=_FakePool(handle),
        )
    assert calls.count("bytes=0-2047") == 2
    assert not target.exists()
    assert not (tmp_path / "file.part-0").exists()
    assert not (tmp_path / "file.part-0.done").exists()


def test_full_unfinished_span_is_refetched(tmp_path):
    data = b"A" * 8192
    target = tmp_path / "file"
    calls = []

    def handle(url, headers):
        span = headers["Range"]
        calls.append(span)
        if span == "bytes=0-0":
            # Model interruption after writing a full span, before checking EOF.
            (tmp_path / "file.part-0").write_bytes(b"X" * 2048)
        start, end = map(int, span.removeprefix("bytes=").split("-"))
        return _FakeResp(206, {"Content-Range": f"bytes {start}-{end}/8192"}, data[start : end + 1])

    headers = {"Accept-Encoding": "identity"}
    import hashlib

    identity = hashlib.sha256(
        json.dumps(["https://cdn.invalid/file", 8192, 4, headers], sort_keys=True).encode()
    ).hexdigest()
    (tmp_path / "file.part.ranges.json").write_text(identity)
    dl.download("https://cdn.invalid/file", target, expected_size=8192, pool=_FakePool(handle))
    assert target.read_bytes() == data
    assert "bytes=0-2047" in calls


@pytest.mark.parametrize("failure_at", ["probe", "worker"])
def test_http_errors_keep_ranges_and_retry_after(tmp_path, monkeypatch, failure_at):
    data = b"A" * 8192
    requests = []
    finished = set()
    ready = threading.Event()
    lock = threading.Lock()
    failed = []
    sleeps = []
    monkeypatch.setattr(dl, "_sleep", lambda delay, cancel: sleeps.append(delay))

    def emit(event):
        if event.data and "range_finished" in event.data:
            with lock:
                finished.add(event.data["range_finished"])
                if {0, 1, 2} <= finished:
                    ready.set()

    def handle(url, headers):
        span = headers.get("Range")
        with lock:
            requests.append(span)
        trigger = "bytes=0-0" if failure_at == "probe" else "bytes=6144-8191"
        if span == trigger and not failed:
            failed.append(True)
            if failure_at == "worker":
                assert ready.wait(5), "other workers did not finish"
            return _FakeResp(503, {"Retry-After": "2"}, b"busy")
        if span is None:
            return _FakeResp(200, {"Content-Length": "8192"}, data)
        start, end = map(int, span.removeprefix("bytes=").split("-"))
        return _FakeResp(206, {"Content-Range": f"bytes {start}-{end}/8192"}, data[start : end + 1])

    target = tmp_path / "file"
    dl.download(
        "https://cdn.invalid/file",
        target,
        expected_size=8192,
        pool=_FakePool(handle),
        emit=emit,
        retries=2,
        backoff=0,
    )
    assert target.read_bytes() == data
    assert None not in requests
    assert sleeps == [2]
    for span in ["bytes=0-2047", "bytes=2048-4095", "bytes=4096-6143"]:
        assert requests.count(span) == 1


def test_corrupt_range_metadata_resets_spans(tmp_path):
    target = tmp_path / "file"
    (tmp_path / "file.part.ranges.json").write_bytes(b"\xff\xfe")
    (tmp_path / "file.part-0.done").write_bytes(b"X" * 2048)
    data = b"A" * 8192

    def handle(url, headers):
        start, end = map(int, headers["Range"].removeprefix("bytes=").split("-"))
        return _FakeResp(206, {"Content-Range": f"bytes {start}-{end}/8192"}, data[start : end + 1])

    dl.download(
        "https://cdn.invalid/file", target, expected_size=8192, pool=_FakePool(handle), retries=1
    )
    assert target.read_bytes() == data
    assert not list(tmp_path.glob("file.part*"))


@pytest.mark.parametrize("redirect", [False, True])
def test_https_proxy_uses_urllib(tmp_path, monkeypatch, redirect):
    proxies = {"https": "https://proxy.invalid:8443"}
    monkeypatch.setattr(dl.urllib.request, "getproxies", lambda: proxies)
    monkeypatch.setattr(dl.urllib.request, "proxy_bypass", lambda host: False)
    data = b"P" * 4096
    opened = []

    class Opener:
        def open(self, request, timeout):
            opened.append(request.full_url)
            return _FakeResp(200, {"Content-Length": "4096"}, data)

    monkeypatch.setattr(dl, "_build_opener", lambda allowed: Opener())
    url = "http://cdn.invalid/file" if redirect else "https://cdn.invalid/file"

    def pool_open(self, url, **kwargs):
        assert redirect, "unsupported initial proxy must skip the range pool"
        raise dl.ProxyNotSupported("redirect uses HTTPS proxy")

    monkeypatch.setattr(dl.HttpClientPool, "open", pool_open)
    target = tmp_path / "file"
    dl.download(url, target, expected_size=4096, retries=1)
    assert target.read_bytes() == data
    assert opened == [url]


def test_shared_budget_samples_time_under_lock(tmp_path: Path, monkeypatch) -> None:
    """A peer advances the budget during flock acquisition; time never decreases."""
    budget_file = tmp_path / "budget.json"
    budget_file.write_text(json.dumps([1.0, 0.0, 99.0]))
    clock = [100.0]
    monkeypatch.setattr(transfer.time, "monotonic", lambda: clock[0])
    bucket = transfer.TokenBucket()
    bucket.attach(budget_file)
    real_flock = transfer.fcntl.flock
    peer_updated = []

    def contended_flock(fd, operation):
        if not peer_updated:
            clock[0] = 100.05
            budget_file.write_text(json.dumps([1.0, 0.0, clock[0]]))
            peer_updated.append(clock[0])
        return real_flock(fd, operation)

    monkeypatch.setattr(transfer.fcntl, "flock", contended_flock)
    written_after_first_step = []

    def spy_wait(_timeout=None):
        written_after_first_step.append(json.loads(budget_file.read_text()))
        clock[0] += 0.1

    monkeypatch.setattr(bucket._condition, "wait", spy_wait)
    bucket.take(1000)

    assert peer_updated == [100.05]
    assert written_after_first_step, "Expected a wait after draining an empty budget"
    first_state = written_after_first_step[0]
    assert first_state[2] >= peer_updated[0], (
        f"Shared timestamp moved backwards despite a nondecreasing clock: {first_state}"
    )
