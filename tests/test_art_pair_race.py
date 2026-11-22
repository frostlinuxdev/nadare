# SPDX-License-Identifier: GPL-3.0-or-later
"""An official cover and its ``.src`` sidecar always name one URL when downloads overlap."""

from __future__ import annotations

import os
import threading
from pathlib import Path

import pytest
from _timing import scaled

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


class _WatchedLock:
    """A real lock that records when a second thread has to wait for it.

    The test stands in for ``artwork._pair_lock``: reaching the lock is the hand-off that
    tells the first download the second one is queued behind it.
    """

    def __init__(self) -> None:
        self._inner = threading.Lock()
        self._owner: int | None = None
        self.waited = threading.Event()

    def __enter__(self) -> _WatchedLock:
        me = threading.get_ident()
        if self._owner is not None and self._owner != me:
            self.waited.set()
        self._inner.acquire()
        self._owner = me
        return self

    def __exit__(self, *_exc: object) -> None:
        self._owner = None
        self._inner.release()


class _RecordingSidecar:
    """Delegates to the real sidecar path and notes the URL it was given."""

    def __init__(self, path: Path, note) -> None:
        self._path = path
        self._note = note

    def write_text(self, data: str, **kwargs) -> int:
        written = self._path.write_text(data, **kwargs)
        self._note("A sidecar" if data == A else "B sidecar")
        return written

    def __getattr__(self, name: str):
        return getattr(self._path, name)


def test_overlapping_official_downloads_keep_image_and_sidecar_together(rig, monkeypatch):
    real_replace = os.replace
    real_sidecar = artwork.src_sidecar
    pair = _WatchedLock()
    monkeypatch.setattr(artwork, "_pair_lock", pair)

    events: list[str] = []
    order = threading.Lock()

    def note(event: str) -> None:
        with order:
            events.append(event)

    monkeypatch.setattr(
        artwork, "src_sidecar", lambda path: _RecordingSidecar(real_sidecar(path), note)
    )
    a_replaced = threading.Event()

    def slow_replace(src, dst):
        first_byte = Path(src).read_bytes()[:1]
        real_replace(src, dst)
        note("A image" if first_byte == b"A" else "B image")
        if first_byte == b"A":
            a_replaced.set()
            # Wait until B is queued on the pair lock rather than for a wall-clock window.
            # With nothing ordering the pair B is already done, this times out, and the
            # events below show the two halves written apart.
            pair.waited.wait(scaled(10))

    monkeypatch.setattr(artwork.os, "replace", slow_replace)

    def go(url, done=None):
        artwork._store(url, "slug", "cover", official=True)
        if done:
            done.set()

    a = threading.Thread(target=go, args=(A,))
    b = threading.Thread(target=go, args=(B,))
    a.start()
    assert a_replaced.wait(scaled(5))
    b.start()
    a.join(scaled(30))
    b.join(scaled(30))
    # Reading while a thread is still between "image replaced" and "sidecar written" is what
    # failed under CI load (#1024): a thread that did not finish is a hang, say so.
    assert not a.is_alive() and not b.is_alive(), "a download thread did not finish"
    assert pair.waited.is_set(), "the second download was never ordered behind the first"
    assert events == ["A image", "A sidecar", "B image", "B sidecar"], (
        f"an image and its sidecar were not written as one pair: {events}"
    )
    image = rig / "slug-cover.jpg"
    sidecar = real_sidecar(image).read_text(encoding="utf-8")
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
