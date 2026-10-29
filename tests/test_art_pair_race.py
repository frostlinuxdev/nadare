# SPDX-License-Identifier: GPL-3.0-or-later
"""An official cover and its ``.src`` sidecar always name one URL when downloads overlap."""

from __future__ import annotations

import os
import threading
from pathlib import Path

import pytest

from nadare.core import artwork

A = "https://a.example/x.jpg"
B = "https://b.example/y.jpg"
BODIES = {A: b"A" * 4096, B: b"B" * 4096}


@pytest.fixture
def rig(tmp_path, monkeypatch):
    monkeypatch.setattr(artwork, "cache_dir", lambda: tmp_path)
    monkeypatch.setattr(
        artwork, "_download", lambda url, timeout=0, cancel=None: (BODIES[url], "jpg")
    )
    monkeypatch.setattr(artwork, "_recode_webp", lambda content, ext: (content, ext))
    return tmp_path


def test_overlapping_official_downloads_keep_image_and_sidecar_together(rig, monkeypatch):
    real_replace = os.replace
    a_replaced = threading.Event()
    b_done = threading.Event()

    def slow_replace(src, dst):
        first_byte = Path(src).read_bytes()[:1]
        real_replace(src, dst)
        if first_byte == b"A":
            a_replaced.set()
            b_done.wait(0.5)  # B can only finish in this window if nothing orders the pair

    monkeypatch.setattr(artwork.os, "replace", slow_replace)

    def go(url, done=None):
        artwork._store(url, "slug", "cover", official=True)
        if done:
            done.set()

    a = threading.Thread(target=go, args=(A,))
    b = threading.Thread(target=go, args=(B, b_done))
    a.start()
    assert a_replaced.wait(5)
    b.start()
    a.join(10)
    b.join(10)
    image = rig / "slug-cover.jpg"
    sidecar = artwork.src_sidecar(image).read_text(encoding="utf-8")
    assert BODIES[sidecar] == image.read_bytes(), "the sidecar names a different image's URL"


def test_a_plain_official_download_still_works_and_is_reused(rig):
    path = artwork._store(A, "solo", "cover", official=True)
    assert path.read_bytes() == BODIES[A]
    assert artwork.src_sidecar(path).read_text(encoding="utf-8") == A
    assert artwork._store(A, "solo", "cover", official=True) == path


def test_a_changed_official_url_replaces_both_halves(rig):
    artwork._store(A, "solo", "cover", official=True)
    path = artwork._store(B, "solo", "cover", official=True)
    assert path.read_bytes() == BODIES[B]
    assert artwork.src_sidecar(path).read_text(encoding="utf-8") == B
