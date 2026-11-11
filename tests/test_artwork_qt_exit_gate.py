# SPDX-License-Identifier: GPL-3.0-or-later
"""#1062: no thread may be inside Qt image code while Python finalises at exit.

Reproduced under host load on an Arch VM: the main thread was in ``Py_FinalizeEx`` while a
daemon thread was in ``QImage.save(QBuffer, "WEBP")`` (the cover WebP recode); the buffer was
collected under it and Qt crashed (exit 139). ``artwork.close_qt_images()`` now waits for an
encode in progress and refuses new ones; ``gui.run`` calls it before returning.
"""

from __future__ import annotations

import os
import threading
import time
from pathlib import Path

os.environ.setdefault("QT_QPA_PLATFORM", "offscreen")

import pytest

from nadare.core import artwork


@pytest.fixture(autouse=True)
def _open_gate(monkeypatch):
    monkeypatch.setattr(artwork, "_qt_closed", False)
    yield
    artwork._qt_closed = False


def _png() -> bytes:
    pytest.importorskip("PyQt6.QtGui")
    from PyQt6.QtCore import QBuffer, QByteArray, QIODevice
    from PyQt6.QtGui import QColor, QImage

    image = QImage(256, 256, QImage.Format.Format_RGB32)
    image.fill(QColor(30, 60, 90))
    data = QByteArray()
    buffer = QBuffer(data)
    buffer.open(QIODevice.OpenModeFlag.WriteOnly)
    assert image.save(buffer, "PNG")
    return bytes(data)


def test_after_closing_no_image_is_recoded():
    png = _png()
    artwork.close_qt_images()
    assert artwork._recode_webp(png, "png") == (png, "png")


def test_after_closing_images_are_still_recognised_by_their_signature():
    png = _png()
    artwork.close_qt_images()
    assert artwork._is_image(png) is True
    assert artwork._is_image(b"<html>nope</html>") is False


def test_closing_waits_for_an_encode_in_progress():
    entered = threading.Event()
    done: list[float] = []

    def slow_encode():
        with artwork._qt_images():
            entered.set()
            time.sleep(0.4)
            done.append(time.monotonic())

    worker = threading.Thread(target=slow_encode)
    worker.start()
    assert entered.wait(2)
    assert artwork.close_qt_images(timeout=5) is True
    closed_at = time.monotonic()
    worker.join()
    assert done and done[0] <= closed_at


def test_the_cache_migration_stops_once_closed(tmp_path, monkeypatch):
    monkeypatch.setattr(artwork, "cache_dir", lambda: tmp_path)
    (tmp_path / "cover.png").write_bytes(_png())
    artwork.close_qt_images()
    assert artwork.migrate_cached_covers() == 0
    assert (tmp_path / "cover.png").is_file()


def test_the_gui_closes_the_gate_before_python_finalises():
    text = Path("src/nadare/gui/__init__.py").read_text(encoding="utf-8")
    run = text[text.index("def run("):]
    run = run[: run.index("\ndef ", 1)]
    assert "close_qt_images" in run
    assert run.index("close_qt_images") < run.index("return int(code)")
    assert run.index("close_qt_images") < run.index("os._exit")
    assert "gate_ok" in run
    assert "not gate_ok" in run
