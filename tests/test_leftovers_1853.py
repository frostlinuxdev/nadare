# SPDX-License-Identifier: GPL-3.0-or-later
"""Leftovers of the fetch retry review (#1853), each with in-memory HTTP bodies only."""

from __future__ import annotations

import math

import pytest
from test_download import FakeResponse

from nadare.core import download as dl

BROKEN = [math.nan, math.inf, -math.inf, -5.0, "soon", object()]
#: Stable test ids: ``repr(object())`` holds an address, which differs between xdist workers.
BROKEN_IDS = ["nan", "inf", "-inf", "negative", "text", "object"]


def _status_error(raw: object) -> dl.HTTPStatusError:
    exc = dl.HTTPStatusError("synthetic error", status=503)
    exc.retry_after = raw  # set past the constructor, as a wrapper or a subclass may do
    return exc


@pytest.mark.parametrize("raw", BROKEN, ids=BROKEN_IDS)
def test_a_broken_raw_retry_after_falls_back_to_the_backoff(raw):
    assert dl.usable_retry_after(_status_error(raw)) is None


@pytest.mark.parametrize(
    ("raw", "want"), [(0, 0.0), (7, 7.0), ("3", 3.0), (10**9, dl.RETRY_AFTER_MAX)]
)
def test_a_good_raw_retry_after_is_kept_and_capped(raw, want):
    assert dl.usable_retry_after(_status_error(raw)) == want


def test_an_error_without_retry_after_has_none():
    assert dl.usable_retry_after(OSError("reset")) is None


@pytest.mark.parametrize("raw", BROKEN, ids=BROKEN_IDS)
def test_download_waits_a_real_backoff_on_a_broken_retry_after(tmp_path, monkeypatch, raw):
    waits: list[float] = []
    calls: list[int] = []
    body = b"game"

    def open_response(*a, **k):
        calls.append(1)
        if len(calls) == 1:
            raise _status_error(raw)
        return FakeResponse(body, headers={"Content-Length": str(len(body))})

    monkeypatch.setattr(dl, "http_open", open_response)
    monkeypatch.setattr(dl, "_sleep", lambda seconds, cancel: waits.append(seconds))
    dl.download("https://example.invalid/file", tmp_path / "g.bin", retries=3, backoff=2)
    assert len(waits) == 1
    assert math.isfinite(waits[0]) and waits[0] >= 2
