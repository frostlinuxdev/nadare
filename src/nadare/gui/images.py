# SPDX-License-Identifier: GPL-3.0-or-later
# Copyright (C) 2026 Nadare contributors
"""Off-thread image decoding behind a small scaled-pixmap LRU.

Switching games must not decode multi-megabyte official PNGs on the GUI thread.
:class:`ImageLoader` resolves the first readable path, decodes and scales it on
a ``QThreadPool`` task, converts the resulting :class:`QImage` to a
:class:`QPixmap` on the GUI thread only, and caches it keyed by
``(path, width, height, crop, mtime)``.

The decode step is injectable so tests can assert it never runs on the GUI
thread; the callback is always invoked on the thread that owns the loader.
"""

from __future__ import annotations

import re
from collections import OrderedDict
from collections.abc import Callable, Iterable
from pathlib import Path

from PyQt6.QtCore import QObject, QRunnable, Qt, QThreadPool, pyqtSignal
from PyQt6.QtGui import QImage, QPixmap

#: Per-load scale modes: cover (centre-crop) vs contain (keep whole).
_EXPAND = Qt.AspectRatioMode.KeepAspectRatioByExpanding
_KEEP = Qt.AspectRatioMode.KeepAspectRatio


#: Publisher news thumbnails (``<slug>-news-<id>-thumb.<ext>``) are character art
#: with the post's category ("Notice", "GM Notes", ...) baked into the lower
#: third. Used as a cover that banner reads as a label on the game, so covers
#: drop that strip; the news list itself keeps the full image.
_NEWS_THUMB = re.compile(r"-news-[0-9a-f]+-thumb\.[A-Za-z0-9]+$")
NEWS_BANNER_TRIM = 0.30


def banner_trim_for(path: str | Path | None) -> float:
    """Fraction of the bottom to drop when ``path`` is used as a cover."""
    if path and _NEWS_THUMB.search(str(path)):
        return NEWS_BANNER_TRIM
    return 0.0


def _mtime_ns(path: str) -> int:
    try:
        return Path(path).stat().st_mtime_ns
    except OSError:
        return 0


def decode_scaled(
    path: str, width: int, height: int, *, crop: bool = True, trim_bottom: float = 0.0
) -> QImage | None:
    """Decode ``path`` and scale it to ``width``x``height``.

    ``crop`` centre-crops to fill the target (cover); otherwise the image is
    kept whole inside the box (contain). Returns ``None`` for an unreadable
    file. This is designed to run on a worker thread and never touches QPixmap.
    """
    image = QImage(str(path))
    if image.isNull():
        return None
    if 0.0 < trim_bottom < 0.6:
        image = image.copy(0, 0, image.width(), max(1, int(image.height() * (1 - trim_bottom))))
    width = max(1, int(width))
    height = max(1, int(height))
    mode = _EXPAND if crop else _KEEP
    scaled = image.scaled(width, height, mode, Qt.TransformationMode.SmoothTransformation)
    if crop and (scaled.width() != width or scaled.height() != height):
        x = max(0, (scaled.width() - width) // 2)
        y = max(0, (scaled.height() - height) // 2)
        scaled = scaled.copy(x, y, width, height)
    return scaled


class _TaskSignals(QObject):
    done = pyqtSignal(object, object)  # key, QImage | None


class _DecodeTask(QRunnable):
    """One decode on a pool thread.

    ``signals`` belongs to the loader, never to the task: Qt deletes a finished task on a
    pool thread, and a QObject created on the GUI thread must not be destroyed there
    (it left dangling wrappers that crashed PyQt when the QApplication was released).
    """

    def __init__(
        self,
        key: tuple,
        signals: _TaskSignals,
        path: str,
        width: int,
        height: int,
        crop: bool,
        decode: Callable[..., QImage | None],
        trim_bottom: float = 0.0,
    ) -> None:
        super().__init__()
        self.signals = signals
        self._key = key
        self._path = path
        self._width = width
        self._height = height
        self._crop = crop
        self._decode = decode
        self._trim = trim_bottom

    def run(self) -> None:  # noqa: D102 - QRunnable entry point
        try:
            if self._trim:
                image = self._decode(
                    self._path, self._width, self._height, crop=self._crop, trim_bottom=self._trim
                )
            else:
                image = self._decode(self._path, self._width, self._height, crop=self._crop)
        except Exception:  # noqa: BLE001 - a broken image is not fatal
            image = None
        try:
            self.signals.done.emit(self._key, image)
        except RuntimeError:  # the loader was destroyed while the decode ran
            pass


class ImageLoader(QObject):
    """Decode-and-scale images off the GUI thread with a scaled-pixmap LRU."""

    def __init__(
        self,
        capacity: int = 96,
        parent: QObject | None = None,
        *,
        decode: Callable[..., QImage | None] | None = None,
        pool: QThreadPool | None = None,
    ) -> None:
        super().__init__(parent)
        self._capacity = max(1, int(capacity))
        self._cache: OrderedDict[tuple, QPixmap] = OrderedDict()
        self._pending: dict[tuple, _DecodeTask] = {}
        self._waiters: dict[tuple, list[Callable]] = {}
        self._decode = decode or decode_scaled
        self._pool = pool or QThreadPool.globalInstance()
        self._signals = _TaskSignals(self)
        self._signals.done.connect(self._on_done)

    @staticmethod
    def resolve(paths: Iterable[str | None] | str | None) -> str | None:
        """Return the first candidate that exists on disk, or ``None``."""
        if isinstance(paths, (str, Path)):
            paths = [str(paths)]
        for path in paths or ():
            if not path:
                continue
            if Path(str(path)).is_file():
                return str(path)
        return None

    def request(
        self,
        paths: Iterable[str | None] | str | None,
        width: int,
        height: int,
        callback: Callable[[QPixmap | None], None],
        *,
        crop: bool = True,
        cover: bool = False,
    ) -> tuple | None:
        """Decode the first readable candidate and hand a QPixmap to ``callback``.

        A cache hit calls back synchronously; a miss schedules a worker task and
        calls back later on the GUI thread. Returns the cache key (or ``None``
        when no candidate exists).
        """
        path = self.resolve(paths)
        if path is None:
            callback(None)
            return None
        trim = banner_trim_for(path) if cover else 0.0
        key = (path, int(width), int(height), bool(crop), _mtime_ns(path), trim)
        cached = self._cache.get(key)
        if cached is not None:
            self._cache.move_to_end(key)
            callback(cached)
            return key
        if key in self._pending:
            self._waiters.setdefault(key, []).append(callback)
            return key
        self._waiters[key] = [callback]
        task = _DecodeTask(key, self._signals, path, width, height, crop, self._decode, trim)
        self._pending[key] = task
        self._pool.start(task)
        return key

    def _on_done(self, key: tuple, image: object) -> None:
        self._pending.pop(key, None)
        pixmap: QPixmap | None = None
        if isinstance(image, QImage) and not image.isNull():
            pixmap = QPixmap.fromImage(image)
        if pixmap is not None:
            self._cache[key] = pixmap
            self._cache.move_to_end(key)
            while len(self._cache) > self._capacity:
                self._cache.popitem(last=False)
        for callback in self._waiters.pop(key, []):
            callback(pixmap)

    def cached(
        self, path: str, width: int, height: int, *, crop: bool = True
    ) -> QPixmap | None:
        """Return a cached pixmap without scheduling a decode."""
        key = (str(path), int(width), int(height), bool(crop), _mtime_ns(str(path)))
        pixmap = self._cache.get(key)
        if pixmap is not None:
            self._cache.move_to_end(key)
        return pixmap

    def pending(self) -> int:
        """Number of tasks still in flight."""
        return len(self._pending)

    def wait(self, timeout_ms: int = 5000) -> None:
        """Block until in-flight tasks have delivered their callbacks."""
        from PyQt6.QtCore import QCoreApplication, QElapsedTimer

        timer = QElapsedTimer()
        timer.start()
        self._pool.waitForDone(timeout_ms)
        while self._pending and timer.elapsed() < timeout_ms:
            QCoreApplication.processEvents()


__all__ = ["ImageLoader", "decode_scaled"]
