# SPDX-License-Identifier: GPL-3.0-or-later
"""Cancel reaches the integrity check and the publish step of a download (#1469).

Hashing a large finished or resumed file could take minutes with Cancel ignored, and the file
was published after Cancel was pressed. The digest now checks the token between blocks, the
cached ``.part`` is not promoted after Cancel, and the last check is right before the rename
that publishes the file (the commit point: after it the download is done).
"""

from __future__ import annotations

import hashlib

import pytest

from nadare.core import download
from nadare.core.events import Cancel, Cancelled

PAYLOAD = b"synthetic received bytes" * 64


def _no_network(*_a, **_k):
    raise AssertionError("unexpected network")


def test_a_cancel_before_start_does_not_promote_a_cached_part(tmp_path, monkeypatch):
    dest = tmp_path / "asset.bin"
    part = dest.with_name("asset.bin.part")
    part.write_bytes(PAYLOAD)
    token = Cancel()
    token.cancel()
    monkeypatch.setattr(download, "http_open", _no_network)
    with pytest.raises(Cancelled):
        download.download(
            "https://invalid.example/asset", dest,
            expected_sha256=hashlib.sha256(PAYLOAD).hexdigest(), cancel=token,
        )
    assert not dest.exists()
    assert part.read_bytes() == PAYLOAD  # kept for the next try


def test_a_cancel_during_the_digest_does_not_publish(tmp_path, monkeypatch):
    dest = tmp_path / "asset.bin"
    token = Cancel()
    real_hash = download.hash_file

    def transfer(url, part, **kwargs):
        part.write_bytes(PAYLOAD)
        return False

    def hashing(*args, **kwargs):
        token.cancel()
        return real_hash(*args, **kwargs)

    monkeypatch.setattr(download, "_download_once", transfer)
    monkeypatch.setattr(download, "hash_file", hashing)
    with pytest.raises(Cancelled):
        download.download(
            "https://invalid.example/asset", dest,
            expected_sha256=hashlib.sha256(PAYLOAD).hexdigest(), cancel=token, retries=1,
        )
    assert not dest.exists()
    assert dest.with_name("asset.bin.part").read_bytes() == PAYLOAD


def test_a_previous_file_survives_a_cancelled_replacement(tmp_path, monkeypatch):
    dest = tmp_path / "asset.bin"
    dest.write_bytes(b"previous good file")
    token = Cancel()

    def transfer(url, part, **kwargs):
        part.write_bytes(PAYLOAD)
        token.cancel()
        return False

    monkeypatch.setattr(download, "_download_once", transfer)
    with pytest.raises(Cancelled):
        download.download(
            "https://invalid.example/asset", dest,
            expected_sha256=hashlib.sha256(PAYLOAD).hexdigest(), cancel=token, retries=1,
        )
    assert dest.read_bytes() == b"previous good file"


class _CancelAfterReads(Cancel):
    """A token that flips after ``n`` checks, i.e. between digest blocks."""

    def __init__(self, n: int) -> None:
        super().__init__()
        self._left = n

    @property
    def cancelled(self) -> bool:  # type: ignore[override]
        self._left -= 1
        return self._left < 0


def test_hashing_stops_between_blocks(tmp_path):
    path = tmp_path / "big.bin"
    path.write_bytes(b"x" * 10_000)
    reads: list[int] = []
    real_open = open

    class Counting:
        def __init__(self, fh):
            self.fh = fh

        def read(self, n):
            reads.append(n)
            return self.fh.read(n)

        def __enter__(self):
            return self

        def __exit__(self, *a):
            self.fh.close()

    import builtins

    def counting_open(p, mode="r", *a, **k):
        fh = real_open(p, mode, *a, **k)
        return Counting(fh) if str(p) == str(path) else fh

    builtins_open = builtins.open
    builtins.open = counting_open
    try:
        with pytest.raises(Cancelled):
            download.hash_file(path, chunk=100, cancel=_CancelAfterReads(3))
    finally:
        builtins.open = builtins_open
    assert len(reads) <= 5, f"kept hashing after cancel: {len(reads)} reads"


def test_without_a_cancel_hashing_and_downloads_work_as_before(tmp_path, monkeypatch):
    path = tmp_path / "f.bin"
    path.write_bytes(PAYLOAD)
    assert download.hash_file(path)["sha256"] == hashlib.sha256(PAYLOAD).hexdigest()
    assert download.verify_file(path, sha256=hashlib.sha256(PAYLOAD).hexdigest())

    dest = tmp_path / "asset.bin"

    def transfer(url, part, **kwargs):
        part.write_bytes(PAYLOAD)
        return False

    monkeypatch.setattr(download, "_download_once", transfer)
    result = download.download(
        "https://invalid.example/asset", dest,
        expected_sha256=hashlib.sha256(PAYLOAD).hexdigest(), cancel=Cancel(),
    )
    assert dest.read_bytes() == PAYLOAD and result.sha256 == hashlib.sha256(PAYLOAD).hexdigest()


def test_a_verified_cached_part_is_still_reused_without_a_cancel(tmp_path, monkeypatch):
    dest = tmp_path / "asset.bin"
    dest.with_name("asset.bin.part").write_bytes(PAYLOAD)
    monkeypatch.setattr(download, "http_open", _no_network)
    result = download.download(
        "https://invalid.example/asset", dest,
        expected_sha256=hashlib.sha256(PAYLOAD).hexdigest(), cancel=Cancel(),
    )
    assert result.resumed and dest.read_bytes() == PAYLOAD


def test_a_cancel_right_after_the_digest_does_not_publish(tmp_path, monkeypatch):
    dest = tmp_path / "asset.bin"
    token = Cancel()
    real_hash = download.hash_file

    def transfer(url, part, **kwargs):
        part.write_bytes(PAYLOAD)
        return False

    def hashing(*args, **kwargs):
        result = real_hash(*args, **kwargs)
        token.cancel()  # pressed just as the check finished
        return result

    monkeypatch.setattr(download, "_download_once", transfer)
    monkeypatch.setattr(download, "hash_file", hashing)
    with pytest.raises(Cancelled):
        download.download(
            "https://invalid.example/asset", dest,
            expected_sha256=hashlib.sha256(PAYLOAD).hexdigest(), cancel=token, retries=1,
        )
    assert not dest.exists()


def test_the_download_hands_its_cancel_to_the_digest(tmp_path, monkeypatch):
    """So hashing a large file stops early, not only at the end."""
    dest = tmp_path / "asset.bin"
    token = Cancel()
    seen: list[object] = []
    real_hash = download.hash_file

    def transfer(url, part, **kwargs):
        part.write_bytes(PAYLOAD)
        return False

    def hashing(*args, **kwargs):
        seen.append(kwargs.get("cancel"))
        return real_hash(*args, **kwargs)

    monkeypatch.setattr(download, "_download_once", transfer)
    monkeypatch.setattr(download, "hash_file", hashing)
    download.download("https://invalid.example/asset", dest, cancel=token)
    assert seen and seen[0] is token


def test_a_cancelled_call_does_not_promote_a_size_only_cached_part(tmp_path, monkeypatch):
    """No hash to check, so only the check before the rename can stop it."""
    dest = tmp_path / "asset.bin"
    part = dest.with_name("asset.bin.part")
    part.write_bytes(PAYLOAD)
    token = Cancel()
    token.cancel()
    monkeypatch.setattr(download, "http_open", _no_network)
    with pytest.raises(Cancelled):
        download.download(
            "https://invalid.example/asset", dest, expected_size=len(PAYLOAD), cancel=token
        )
    assert not dest.exists() and part.exists()


def test_checking_a_cached_part_hands_over_the_cancel(tmp_path, monkeypatch):
    dest = tmp_path / "asset.bin"
    dest.with_name("asset.bin.part").write_bytes(PAYLOAD)
    token = Cancel()
    seen: list[object] = []
    real_hash = download.hash_file

    def hashing(*args, **kwargs):
        seen.append(kwargs.get("cancel"))
        return real_hash(*args, **kwargs)

    monkeypatch.setattr(download, "hash_file", hashing)
    monkeypatch.setattr(download, "http_open", _no_network)
    download.download(
        "https://invalid.example/asset", dest,
        expected_sha256=hashlib.sha256(PAYLOAD).hexdigest(), cancel=token,
    )
    assert seen and seen[0] is token
