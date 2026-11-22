# SPDX-License-Identifier: GPL-3.0-or-later
"""Retry classification and bad range replies, with in-memory HTTP bodies only."""

from __future__ import annotations

import hashlib

import pytest
from test_download import FakeResponse

from nadare.core import download as dl


@pytest.mark.parametrize("status", [400, 401, 403, 404, 410, 422])
def test_permanent_http_error_is_not_retried(tmp_path, monkeypatch, status):
    calls = []

    def fail(*a, **k):
        calls.append(1)
        raise dl.HTTPStatusError("synthetic error", status=status)

    monkeypatch.setattr(dl, "http_open", fail)
    dest = tmp_path / "game.bin"
    dest.write_bytes(b"installed")
    with pytest.raises(dl.DownloadError):
        dl.download("https://example.invalid/file", dest, retries=3, backoff=0)
    assert len(calls) == 1
    assert dest.read_bytes() == b"installed"


@pytest.mark.parametrize("status", [408, 425, 429, 500, 502, 503])
def test_transient_http_error_recovers_without_replacing_wrong_bytes(tmp_path, monkeypatch, status):
    calls = []
    body = b"verified game"

    def open_response(*a, **k):
        calls.append(1)
        if len(calls) < 3:
            raise dl.HTTPStatusError("synthetic error", status=status, retry_after=0)
        return FakeResponse(body, headers={"Content-Length": str(len(body))})

    monkeypatch.setattr(dl, "http_open", open_response)
    dest = tmp_path / "game.bin"
    dest.write_bytes(b"installed")
    dl.download(
        "https://example.invalid/file",
        dest,
        retries=3,
        backoff=0,
        expected_sha256=hashlib.sha256(body).hexdigest(),
    )
    assert len(calls) == 3 and dest.read_bytes() == body
    assert not dest.with_name(dest.name + ".part").exists()


@pytest.mark.parametrize("header", ["bytes 0-5/9", "bytes 4-5/9", "bytes 3-5/100", "nonsense"])
def test_wrong_range_is_never_promoted(tmp_path, monkeypatch, header):
    dest = tmp_path / "game.bin"
    dest.write_bytes(b"installed")
    dest.with_name(dest.name + ".part").write_bytes(b"abc")
    monkeypatch.setattr(
        dl,
        "http_open",
        lambda *a, **k: FakeResponse(
            b"def", status=206, headers={"Content-Range": header, "Content-Length": "3"}
        ),
    )
    with pytest.raises(dl.DownloadError):
        dl.download(
            "https://example.invalid/file",
            dest,
            expected_size=9,
            expected_sha256=hashlib.sha256(b"abcdefghi").hexdigest(),
            retries=1,
            backoff=0,
        )
    assert dest.read_bytes() == b"installed"
