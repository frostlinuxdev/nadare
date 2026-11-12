# SPDX-License-Identifier: GPL-3.0-or-later
"""#1252: PGR's patch lists are ~20 MB; they must load, while other fetches keep their cap."""

from __future__ import annotations

import pytest

from nadare.core import directdl, download, sources


class _Response:
    def __init__(self, size):
        self._data = b"x" * size

    def read(self, n=-1):
        return self._data if n < 0 else self._data[:n]

    def __enter__(self):
        return self

    def __exit__(self, *exc):
        return False


@pytest.fixture()
def big(monkeypatch):
    size = download.MAX_TEXT_BYTES + 4 * 1024 * 1024
    monkeypatch.setattr(download, "http_open", lambda url, **kw: _Response(size))
    return size


def test_a_normal_fetch_still_refuses_a_big_answer(big):
    with pytest.raises(directdl.DirectError, match="too large"):
        sources.default_fetch("https://x/index.json")


def test_a_patch_list_of_twenty_megabytes_loads(big):
    assert len(sources.patch_list_fetch("https://x/indexFile.json")) == big


def test_a_patch_list_still_has_a_cap(monkeypatch):
    size = sources.MAX_PATCH_LIST_BYTES + 1
    monkeypatch.setattr(download, "http_open", lambda url, **kw: _Response(size))
    with pytest.raises(directdl.DirectError, match="too large"):
        sources.patch_list_fetch("https://x/indexFile.json")
