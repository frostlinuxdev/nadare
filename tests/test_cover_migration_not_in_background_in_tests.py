# SPDX-License-Identifier: GPL-3.0-or-later
"""#1024: a cover lookup in a test does not start the background WebP migration.

``fetch`` -> ``_cached`` schedules ``migrate_cached_covers`` on a daemon thread; in a test that
thread converted the test's own ``*.png`` to WebP and deleted the PNG and its ``.src`` sidecar
mid-assertion (CI 3.13: FileNotFoundError on ``genshin-impact-cover.png.src``). conftest stubs
the scheduler; the real one is kept for the test below.
"""

from __future__ import annotations

import os
import threading
import time

os.environ.setdefault("QT_QPA_PLATFORM", "offscreen")

import pytest

from nadare.core import artwork


def _png() -> bytes:
    pytest.importorskip("PyQt6.QtGui")
    from PyQt6.QtCore import QBuffer, QByteArray, QIODevice
    from PyQt6.QtGui import QColor, QImage

    image = QImage(256, 256, QImage.Format.Format_RGB32)
    image.fill(QColor(10, 20, 30))
    data = QByteArray()
    buffer = QBuffer(data)
    buffer.open(QIODevice.OpenModeFlag.WriteOnly)
    assert image.save(buffer, "PNG")
    return bytes(data)


def test_a_lookup_leaves_the_cached_png_alone(tmp_path, monkeypatch):
    monkeypatch.setattr(artwork, "cache_dir", lambda: tmp_path)
    png = tmp_path / "game-cover.png"
    png.write_bytes(_png())
    assert artwork._cached("game", "cover") == png
    time.sleep(0.3)
    assert png.is_file() and not (tmp_path / "game-cover.webp").exists()


def test_the_real_scheduler_still_migrates(tmp_path, monkeypatch):
    monkeypatch.setattr(artwork, "cache_dir", lambda: tmp_path)
    monkeypatch.setattr(artwork, "_schedule_cover_migration",
                        artwork._real_schedule_cover_migration)
    monkeypatch.setattr(artwork, "_migrated_cache_roots", set())
    monkeypatch.setattr(artwork, "_migrating_cache_roots", set())
    (tmp_path / "game-cover.png").write_bytes(_png())
    artwork._cached("game", "cover")
    for thread in threading.enumerate():
        if thread.name == "cover-cache-migration":
            thread.join(10)
    assert (tmp_path / "game-cover.webp").is_file()
