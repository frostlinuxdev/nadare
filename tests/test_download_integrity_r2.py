# SPDX-License-Identifier: GPL-3.0-or-later
"""Pins sha512 verification and truncation detection in nadare.core.download."""

from __future__ import annotations

import hashlib
from pathlib import Path

import pytest

from nadare.core import download as dl


class FakeResponse:
    def __init__(self, body, status=200, headers=None):
        self._body, self.status, self.headers, self._pos = body, status, headers or {}, 0

    def getcode(self):
        return self.status

    def read(self, n=-1):
        end = len(self._body) if n is None or n < 0 else self._pos + n
        data = self._body[self._pos : end]
        self._pos += len(data)
        return data

    def __enter__(self):
        return self

    def __exit__(self, *exc):
        return False


def _fake_http(body):
    def _open(url, *, headers=None, timeout=dl.DEFAULT_TIMEOUT):
        return FakeResponse(body, headers={"Content-Length": str(len(body))})

    return _open


def test_download_rejects_wrong_sha512_and_cleans_up(tmp_path: Path, monkeypatch) -> None:
    body = b"runner tarball bytes"
    monkeypatch.setattr(dl, "http_open", _fake_http(body))
    dest = tmp_path / "r.tgz"
    with pytest.raises(dl.IntegrityError):
        dl.download("http://example.invalid/r", dest, expected_sha512="0" * 128)
    assert not dest.exists()
    assert not (tmp_path / "r.tgz.part").exists()


def test_download_accepts_correct_sha512(tmp_path: Path, monkeypatch) -> None:
    body = b"runner tarball bytes"
    monkeypatch.setattr(dl, "http_open", _fake_http(body))
    dest = tmp_path / "r.tgz"
    dl.download(
        "http://example.invalid/r", dest, expected_sha512=hashlib.sha512(body).hexdigest()
    )
    assert dest.read_bytes() == body


def test_verify_file_checks_sha512(tmp_path: Path) -> None:
    p = tmp_path / "x.bin"
    p.write_bytes(b"payload")
    assert dl.verify_file(p, sha512=hashlib.sha512(b"payload").hexdigest()) is True
    assert dl.verify_file(p, sha512=hashlib.sha512(b"other").hexdigest()) is False


def test_tampered_part_is_not_promoted_when_sha512_differs(tmp_path: Path, monkeypatch) -> None:
    good = b"A" * 32
    dest = tmp_path / "r.tgz"
    (tmp_path / "r.tgz.part").write_bytes(b"B" * 32)  # right size, wrong content
    monkeypatch.setattr(dl, "http_open", _fake_http(good))
    dl.download(
        "http://example.invalid/r",
        dest,
        expected_size=32,
        expected_sha512=hashlib.sha512(good).hexdigest(),
    )
    assert dest.read_bytes() == good


def test_truncated_body_without_hash_is_rejected(tmp_path: Path, monkeypatch) -> None:
    def _open(url, *, headers=None, timeout=dl.DEFAULT_TIMEOUT):
        return FakeResponse(b"x" * 500, headers={"Content-Length": "1000"})

    monkeypatch.setattr(dl, "http_open", _open)
    monkeypatch.setattr(dl, "_sleep", lambda s: None, raising=False)
    dest = tmp_path / "t.bin"
    with pytest.raises(dl.DownloadError):
        dl.download("http://example.invalid/t", dest, retries=1)
    assert not dest.exists()
