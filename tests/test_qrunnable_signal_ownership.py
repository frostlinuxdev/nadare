# SPDX-License-Identifier: GPL-3.0-or-later
"""Pool tasks must not own a QObject: Qt destroys a finished task on a pool thread.

A QObject created on the GUI thread and deleted on a worker thread left dangling PyQt
wrappers, which crashed the process when the QApplication wrapper was released at the
end of a test module (a flaky segfault once the store's many image tasks were in use).
"""

from __future__ import annotations

import os

os.environ.setdefault("QT_QPA_PLATFORM", "offscreen")

import pytest
from PyQt6 import sip
from PyQt6.QtGui import QColor, QImage
from PyQt6.QtWidgets import QApplication

from nadare.core import artwork
from nadare.gui import images, store


@pytest.fixture(scope="module")
def qapp():
    return QApplication.instance() or QApplication([])


class _CapturePool:
    """A stand-in for QThreadPool that keeps the tasks so the test can inspect and run them."""

    def __init__(self) -> None:
        self.tasks: list = []

    def start(self, task) -> None:
        self.tasks.append(task)


def _png(path):
    image = QImage(40, 30, QImage.Format.Format_ARGB32)
    image.fill(QColor("#336699"))
    assert image.save(str(path))
    return path


def test_decode_tasks_share_the_loaders_signals_object(tmp_path, qapp):
    pool = _CapturePool()
    loader = images.ImageLoader(pool=pool)
    art = _png(tmp_path / "a.png")
    loader.request([str(art)], 20, 20, lambda pixmap: None)
    loader.request([str(art)], 30, 30, lambda pixmap: None)
    first, second = pool.tasks
    assert first.signals is second.signals is loader._signals
    loader.deleteLater()
    qapp.processEvents()


def test_a_finished_decode_still_reaches_its_callback(tmp_path, qapp):
    pool = _CapturePool()
    loader = images.ImageLoader(pool=pool)
    got = []
    loader.request([str(_png(tmp_path / "a.png"))], 20, 20, got.append)
    pool.tasks[0].run()
    qapp.processEvents()
    assert len(got) == 1 and got[0] is not None and not got[0].isNull()
    assert loader.pending() == 0
    loader.deleteLater()
    qapp.processEvents()


def test_a_decode_finishing_after_its_loader_died_does_not_raise(tmp_path, qapp):
    pool = _CapturePool()
    loader = images.ImageLoader(pool=pool)
    loader.request([str(_png(tmp_path / "a.png"))], 20, 20, lambda pixmap: None)
    sip.delete(loader)
    pool.tasks[0].run()  # must not raise, nor touch a dead object


def test_store_art_fetches_report_through_the_pages_signals(tmp_path, qapp, monkeypatch):
    art = _png(tmp_path / "cover.png")
    monkeypatch.setattr(artwork, "fetch", lambda profile, title=None, kind="cover": art)
    page = store.StorePage()
    try:
        task = store._FetchArt("direct-pgr", "Punishing: Gray Raven", page._art_signals)
        assert task.signals is page._art_signals
        task.run()
        qapp.processEvents()
        assert page._art_paths["direct-pgr"] == str(art)
    finally:
        page.deleteLater()
        qapp.processEvents()


def test_a_store_fetch_finishing_after_its_page_died_does_not_raise(tmp_path, qapp, monkeypatch):
    art = _png(tmp_path / "cover.png")
    monkeypatch.setattr(artwork, "fetch", lambda profile, title=None, kind="cover": art)
    page = store.StorePage()
    task = store._FetchArt("direct-pgr", "Punishing: Gray Raven", page._art_signals)
    sip.delete(page)
    task.run()
