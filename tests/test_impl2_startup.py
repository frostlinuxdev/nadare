# SPDX-License-Identifier: GPL-3.0-or-later
"""Startup work stays off the GUI thread and never waits for every profile."""

from __future__ import annotations

import os
import threading
import time

os.environ.setdefault("QT_QPA_PLATFORM", "offscreen")

import pytest

try:  # pragma: no cover - exercised only when PyQt6 is missing
    from PyQt6.QtCore import QThread
    from PyQt6.QtGui import QImage
    from PyQt6.QtWidgets import QApplication
except Exception:  # noqa: BLE001
    pytest.skip("PyQt6 is not importable in this interpreter", allow_module_level=True)

from nadare.core import news, runners
from nadare.core.config import Config
from nadare.core.events import Cancel
from nadare.core.news import NewsResult
from nadare.gui import workers
from nadare.gui.library import LibraryPage
from nadare.gui.main_window import MainWindow
from nadare.gui.runners_dialog import ManageRunnersDialog


@pytest.fixture(scope="module")
def qapp():
    app = QApplication.instance()
    if app is None:
        app = QApplication([])
    yield app


def _wait(qapp, predicate, seconds=5.0):
    end = time.monotonic() + seconds
    while time.monotonic() < end:
        qapp.processEvents()
        if predicate():
            return True
        time.sleep(0.01)
    return False


def _items(count: int = 9) -> list[dict]:
    return [
        {
            "id": str(index),
            "title": f"News {index}",
            "summary": "",
            "category": "News",
            "published_at": 1_700_000_000_000 - index,
            "image": f"https://cdn.example/{index}.png",
            "url": f"https://example.invalid/{index}",
        }
        for index in range(count)
    ]


# ---- F-01: probing the library must not start every profile's news fetch -----
def test_startup_probes_only_fetch_news_for_the_open_profile(tmp_path, qapp, monkeypatch):
    config = Config.load(tmp_path / "config.json")
    config.add_profile("alpha", backend="arknights-yostar", title="Alpha")
    config.add_profile("beta", backend="arknights-yostar", title="Beta")
    calls: list[tuple[str, str]] = []
    monkeypatch.setattr(
        workers.BackendController,
        "start",
        lambda self, profile, operation, payload=None: calls.append((profile, operation)) or True,
    )
    window = MainWindow(config)
    try:
        calls.clear()
        for name in config.profiles:
            window._on_succeeded(name, "status", 0)
        assert [(p, o) for p, o in calls if o == "news"] == [("alpha", "news")]
    finally:
        window.close()
        window.deleteLater()
        qapp.processEvents()


# ---- F-02/F-05: a cancelled news load stops without waiting for all images ----
def test_news_load_stops_promptly_when_cancelled(monkeypatch):
    started = threading.Event()

    def slow_image(url, slug, kind, hosts, *, timeout=3, cancel=None):
        started.set()
        time.sleep(0.3)
        return None

    monkeypatch.setattr(news, "_download_image", slow_image)
    cancel = Cancel()

    def stop_soon():
        started.wait(2)
        cancel.cancel()

    threading.Thread(target=stop_soon, daemon=True).start()
    before = time.monotonic()
    result = news.load(
        lambda: NewsResult(items=_items()),
        key="cancel",
        hosts=("cdn.example",),
        slug="cancel",
        ttl=0,
        cancel=cancel,
    )
    elapsed = time.monotonic() - before
    assert elapsed < 0.7, f"cancelled load waited {elapsed:.2f}s"
    assert result.available


# ---- F-03: the whole news image pass shares one deadline --------------------
def test_news_load_honours_the_overall_deadline(monkeypatch):
    def slow_image(url, slug, kind, hosts, *, timeout=3, cancel=None):
        time.sleep(0.3)
        return None

    monkeypatch.setattr(news, "_download_image", slow_image)
    before = time.monotonic()
    result = news.load(
        lambda: NewsResult(items=_items()),
        key="deadline",
        hosts=("cdn.example",),
        slug="deadline",
        ttl=0,
        deadline=0.15,
    )
    elapsed = time.monotonic() - before
    assert elapsed < 0.7, f"news load overran its deadline by {elapsed:.2f}s"
    assert result.available


def test_media_cache_stops_at_the_deadline(monkeypatch):
    from nadare.core import artwork, yostar

    calls: list[str] = []

    def fake_download(url, slug, kind, hosts=()):
        calls.append(kind)
        return None

    monkeypatch.setattr(artwork, "download", fake_download)
    media = yostar.Media(
        game_tag="Arknights_EN",
        generated_at="t",
        background="https://launcher-pkg-ark-en.yo-star.com/bg.jpg",
        banners=(
            yostar.Banner("https://launcher-pkg-ark-en.yo-star.com/b1.jpg", "https://n/1"),
            yostar.Banner("https://launcher-pkg-ark-en.yo-star.com/b2.jpg", "https://n/2"),
        ),
    )
    result = yostar.cache_media(media, "arknights", deadline=time.monotonic() - 1)
    assert result == {"hero": None, "banners": []}
    assert calls == []


# ---- F-04: library covers decode away from the GUI thread -------------------
def test_library_decodes_covers_off_the_gui_thread(tmp_path, qapp, monkeypatch):
    from nadare.gui import images

    cover = tmp_path / "cover.png"
    image = QImage(320, 480, QImage.Format.Format_ARGB32)
    image.fill(0xFF2288FF)
    assert image.save(str(cover))

    real_decode = images.decode_scaled
    threads: list[bool] = []

    def tracking_decode(path, width, height, *, crop=True, trim_bottom=0.0):
        threads.append(QThread.currentThread() is qapp.thread())
        return real_decode(path, width, height, crop=crop, trim_bottom=trim_bottom)

    monkeypatch.setattr(images, "decode_scaled", tracking_decode)
    page = LibraryPage()
    try:
        before = time.monotonic()
        page.refresh({"p": {"backend": "wine", "title": "P", "cover": str(cover)}})
        assert time.monotonic() - before < 0.3, "refresh decoded the cover on the GUI thread"
        page.wait_for_images(3000)
        qapp.processEvents()
        assert threads == [False], threads
        pixmap = page._cards["p"].art.pixmap()
        assert pixmap is not None and not pixmap.isNull()
    finally:
        page.deleteLater()
        qapp.processEvents()


# ---- F-06: runner usage is computed in the background, once per open --------
def test_manage_runners_computes_usage_off_the_gui_thread(root, qapp, monkeypatch):
    _make_runner(root, "dwproton-11.0-13-x86_64")
    real_usage = runners.runner_usage
    threads: list[bool] = []
    calls: list[dict] = []

    def slow_usage(profiles):
        threads.append(QThread.currentThread() is qapp.thread())
        calls.append(dict(profiles))
        time.sleep(0.2)
        return real_usage(profiles)

    monkeypatch.setattr(runners, "runner_usage", slow_usage)
    before = time.monotonic()
    dialog = ManageRunnersDialog({"czn": {"backend": "stove-czn"}})
    try:
        assert time.monotonic() - before < 0.15, "opening Manage runners blocked the GUI"
        assert _wait(qapp, lambda: bool(threads))
        assert threads == [False], threads
        assert len(calls) == 1, "usage must not be computed twice for one open"
    finally:
        dialog.deleteLater()
        qapp.processEvents()


def test_main_window_checks_runner_usage_off_the_gui_thread(tmp_path, qapp, monkeypatch):
    _make_runner(paths_runners_dir(), "dwproton-11.0-13-x86_64")
    _make_runner(paths_runners_dir(), "dwproton-11.0-15-x86_64")
    config = Config.load(tmp_path / "config.json")
    config.add_profile("czn", backend="stove-czn", title="CZN")
    monkeypatch.setattr(
        workers.BackendController, "start", lambda self, profile, operation, payload=None: True
    )
    window = MainWindow(config)
    real_usage = runners.runner_usage
    threads: list[bool] = []

    def slow_usage(profiles):
        threads.append(QThread.currentThread() is qapp.thread())
        time.sleep(0.2)
        return real_usage(profiles)

    monkeypatch.setattr(runners, "runner_usage", slow_usage)
    monkeypatch.setattr("nadare.gui.main_window.QMessageBox.question", lambda *a, **k: None)
    try:
        window._runners_before = set()
        before = time.monotonic()
        window._maybe_offer_runner_cleanup()
        assert time.monotonic() - before < 0.15, "runner cleanup check blocked the GUI"
        assert _wait(qapp, lambda: bool(threads))
        assert all(is_gui is False for is_gui in threads), threads
    finally:
        window.close()
        window.deleteLater()
        qapp.processEvents()


# ---- helpers ---------------------------------------------------------------
@pytest.fixture
def root(tmp_path, monkeypatch):
    folder = tmp_path / "runners"
    folder.mkdir()
    monkeypatch.setattr(runners.paths, "runners_dir", lambda: folder)
    monkeypatch.setattr(runners, "steam_runners", lambda: [])
    return folder


def _make_runner(folder, name: str):
    target = folder / name
    target.mkdir(parents=True, exist_ok=True)
    (target / "proton").write_text("#!/bin/sh\n")
    (target / "blob").write_bytes(b"x" * 128)
    return target


def paths_runners_dir():
    return runners.paths.runners_dir()
