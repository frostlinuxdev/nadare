# SPDX-License-Identifier: GPL-3.0-or-later
# Copyright (C) 2026 Nadare contributors
"""Run the AppImage self-update (#771) off the GUI thread and report one clear outcome.

``core.selfupdate`` knows how; this object only schedules it. The window owns every dialog:
nothing is downloaded until the user presses Update, and nothing restarts without being asked.
"""

from __future__ import annotations

import contextlib
import logging
from pathlib import Path
from typing import Any

from PyQt6.QtCore import QObject, QRunnable, QThreadPool, pyqtSignal

from ..core import selfupdate
from ..core.events import Cancel, Cancelled, Event
from ..core.updatecheck import UpdateInfo

log = logging.getLogger(__name__)


class _Signals(QObject):
    """Owned by the flow, never by a task: Qt destroys a finished runnable on a pool thread."""

    progress = pyqtSignal(int, int)  # bytes done, bytes total (0 when unknown)
    installed = pyqtSignal(object)  # Path of the new AppImage
    failed = pyqtSignal(str)
    cancelled = pyqtSignal()
    finished = pyqtSignal()


class _Task(QRunnable):
    def __init__(self, info: UpdateInfo, steps: Any, token: Cancel, signals: _Signals) -> None:
        super().__init__()
        self._info = info
        self._steps = steps
        self._token = token
        self._signals = signals

    def _on_event(self, event: Event) -> None:
        data = event.data or {}
        if event.kind == "progress" and "done" in data:
            self._emit("progress", int(data["done"]), int(data.get("total") or 0))

    def _emit(self, name: str, *args: object) -> None:
        # The receiver may be gone when the app quits during an update. The signal is looked up
        # inside the guard: reading ``self._signals.<name>`` itself raises once it is deleted.
        with contextlib.suppress(RuntimeError):
            getattr(self._signals, name).emit(*args)

    def run(self) -> None:  # noqa: D102 - QRunnable entry point
        steps = self._steps
        tmp: Path | None = None
        try:
            urls = steps.asset_urls(self._info)
            target = steps.appimage_target()
            if urls is None or target is None:
                raise selfupdate.SelfUpdateError("this copy of Nadare cannot update itself")
            expected = steps.expected_sha256(self._info)
            tmp = target.with_name("." + urls.filename + ".download")
            steps.download_verified(
                urls.appimage,
                tmp,
                expected.sha256,
                emit=self._on_event,
                cancel=self._token,
            )
            if self._token.cancelled:
                raise Cancelled("cancelled")
            # The index's hash is not signed: the release key's signature must match too.
            steps.verify_signature(urls, tmp)
            if self._token.cancelled:
                raise Cancelled("cancelled")
            new = steps.install_update(tmp, target, urls.filename, sha256=expected.sha256)
        except Cancelled:
            self._cleanup(tmp)
            self._emit("cancelled")
        except Exception as exc:  # noqa: BLE001 - always shown to the user, never lost
            log.debug("self-update failed", exc_info=True)
            self._cleanup(tmp)
            self._emit("failed", str(exc) or exc.__class__.__name__)
        else:
            self._emit("installed", new)
        finally:
            self._emit("finished")

    @staticmethod
    def _cleanup(tmp: Path | None) -> None:
        if tmp is None:
            return
        for leftover in (tmp, tmp.with_name(tmp.name + ".part")):
            with contextlib.suppress(OSError):
                leftover.unlink(missing_ok=True)


class SelfUpdateFlow(QObject):
    """One update at a time: start it, watch its signals, cancel it."""

    progress = pyqtSignal(int, int)
    installed = pyqtSignal(object)
    failed = pyqtSignal(str)
    cancelled = pyqtSignal()

    def __init__(self, parent: QObject | None = None, *, steps: Any = selfupdate) -> None:
        super().__init__(parent)
        self._steps = steps
        self._token: Cancel | None = None
        self._running = False
        self._signals = _Signals(self)
        self._signals.progress.connect(self.progress)
        self._signals.installed.connect(self.installed)
        self._signals.failed.connect(self.failed)
        self._signals.cancelled.connect(self.cancelled)
        self._signals.finished.connect(self._on_finished)

    @property
    def running(self) -> bool:
        return self._running

    def can_update(self, info: UpdateInfo | None) -> bool:
        """Whether ``info`` can be installed over the running AppImage."""
        if info is None:
            return False
        try:
            return self._steps.asset_urls(info) is not None and (
                self._steps.appimage_target() is not None
            )
        except Exception:  # noqa: BLE001 - a surprise here only means "use the page"
            log.debug("self-update availability check failed", exc_info=True)
            return False

    def start(self, info: UpdateInfo) -> bool:
        """Begin the update; ``False`` when one is already running."""
        if self._running:
            return False
        self._running = True
        self._token = Cancel()
        QThreadPool.globalInstance().start(_Task(info, self._steps, self._token, self._signals))
        return True

    def cancel(self) -> None:
        if self._token is not None:
            self._token.cancel()

    def _on_finished(self) -> None:
        self._running = False
        self._token = None
