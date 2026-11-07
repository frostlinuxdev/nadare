# SPDX-License-Identifier: GPL-3.0-or-later
"""A download is only charged for the bytes still to be fetched (#853)."""

from __future__ import annotations

import hashlib

import pytest

from nadare.core import directdl as dd
from nadare.core import diskspace
from nadare.core.backends import BackendError


@pytest.fixture
def no_space(monkeypatch):
    monkeypatch.setattr(diskspace, "free_bytes", lambda path: 0)


def _remote(data: bytes) -> dd.RemoteFile:
    return dd.RemoteFile(
        "a.bin", "https://example.invalid/a.bin", len(data), md5=hashlib.md5(data).hexdigest()
    )


def test_a_verified_cached_file_needs_no_space_and_no_download(tmp_path, monkeypatch, no_space):
    data = b"x" * 18
    target = tmp_path / "a.bin"
    target.write_bytes(data)
    monkeypatch.setattr(dd, "download", lambda *a, **k: pytest.fail("downloaded a cached file"))
    dd._download_remote(_remote(data), target, emit=None, cancel=None)
    assert target.read_bytes() == data


def test_a_fresh_download_with_no_room_is_still_refused(tmp_path, monkeypatch, no_space):
    monkeypatch.setattr(dd, "download", lambda *a, **k: pytest.fail("started without room"))
    with pytest.raises(BackendError, match="Not enough disk space"):
        dd._download_remote(_remote(b"x" * 18), tmp_path / "a.bin", emit=None, cancel=None)


def test_a_resumed_download_is_charged_only_for_what_is_missing(tmp_path, monkeypatch):
    data = b"y" * 1000
    target = tmp_path / "a.bin"
    (tmp_path / "a.bin.part").write_bytes(data[:900])  # 900 of 1000 already here
    monkeypatch.setattr(diskspace, "free_bytes", lambda path: 200)  # room for the last 100 bytes
    seen = {}

    def fake_download(url, destination, **kwargs):
        seen["called"] = True
        destination.write_bytes(data)

    monkeypatch.setattr(dd, "download", fake_download)
    dd._download_remote(_remote(data), target, emit=None, cancel=None)
    assert seen["called"] and target.read_bytes() == data


def test_a_replacement_of_an_installed_file_needs_the_whole_new_file(tmp_path, monkeypatch):
    """The old file stays until the new one is verified, so both must fit."""
    data = b"z" * 1000
    target = tmp_path / "a.bin"
    target.write_bytes(b"old")
    monkeypatch.setattr(diskspace, "free_bytes", lambda path: 500)
    monkeypatch.setattr(dd, "download", lambda *a, **k: pytest.fail("started without room"))
    with pytest.raises(BackendError, match="Not enough disk space"):
        dd._download_remote(_remote(data), target, emit=None, cancel=None)
    assert target.read_bytes() == b"old"
