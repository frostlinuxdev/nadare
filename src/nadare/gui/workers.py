# SPDX-License-Identifier: GPL-3.0-or-later
# Copyright (C) 2026 Nadare contributors
"""Worker-thread plumbing: the only place blocking backend calls run.

The GUI never calls a backend from the main thread. :class:`BackendWorker` is a
``QThread`` that builds the backend through ``core.backends.create`` and runs one
operation, forwarding :class:`~nadare.core.events.Event` objects to Qt signals.
:class:`BackendController` owns at most one worker per profile, which is exactly
the "one mutating operation per profile" rule from ARCHITECTURE.md.
"""

from __future__ import annotations

import logging
import os
import time
import traceback
from datetime import datetime

from PyQt6.QtCore import QObject, QRunnable, QThread, pyqtSignal

from .. import __version__
from ..core import logview, paths
from ..core.backends import BackendError, BackendUnsupported, create
from ..core.config import Config
from ..core.events import Cancel, Cancelled, Event, mask
from ..core.process import OPERATION_BUSY, OperationLock, ProfileLockError

log = logging.getLogger(__name__)

#: Operations a worker understands.
OPERATIONS = (
    "status",
    "install",
    "update",
    "repair",
    "remake_prefix",
    "login",
    "launch",
    "stop",
    "verify",
    "uninstall",
    "set_remember",
    "logout",
    "news",
    "media",
    "check_update",
)

#: Operations that exclude each other (and Remove) per profile across processes.
LOCKED_OPERATIONS = frozenset({"install", "update", "repair", "remake_prefix", "uninstall"})

#: Operations that can be paused and later resumed from their partial downloads.
PAUSABLE = frozenset({"install", "update", "repair"})

#: Operations that mutate the installation and therefore take a per-profile lock.
MUTATING = frozenset(
    {
        "install",
        "update",
        "repair",
        "remake_prefix",
        "login",
        "launch",
        "uninstall",
        "set_remember",
        "logout",
    }
)


class _CallSignals(QObject):
    """Signals for :class:`CallTask` (created on the caller's thread)."""

    done = pyqtSignal(object)
    failed = pyqtSignal(str)


class CallTask(QRunnable):
    """Run a zero-argument callable on the global thread pool.

    One-shot work that must not block the GUI thread (an artwork download, a
    keyring delete). ``signals.done``/``signals.failed`` are delivered on the
    thread that created the task; a raised exception is reported, never lost.
    """

    def __init__(self, func) -> None:
        super().__init__()
        self.signals = _CallSignals()
        self._func = func

    def run(self) -> None:  # noqa: D102 - QRunnable entry point
        try:
            result = self._func()
        except Exception as exc:  # noqa: BLE001 - reported to the caller
            log.debug("background task failed", exc_info=True)
            self._emit("failed", str(exc))
            return
        self._emit("done", result)

    def _emit(self, name: str, value: object) -> None:
        try:
            getattr(self.signals, name).emit(value)
        except RuntimeError:
            # Qt deleted the signal object (the app is quitting): nobody is left to tell.
            log.debug("background task finished after its receiver was gone")


class BackendWorker(QThread):
    """Run one backend operation off the main thread.

    Signals
    -------
    event(object)        -- an :class:`Event` from the backend, in order.
    result(object)       -- the return value of ``status()`` / ``verify()``.
    succeeded(int)       -- exit code (``0`` for non-launch operations).
    failed(str)          -- a user-safe error message.
    cancelled()          -- the operation stopped because Cancel was requested.
    """

    # NB: must not be named ``event`` — that shadows QObject.event and makes
    # QThread.__init__ raise "native Qt signal is not callable".
    backend_event = pyqtSignal(object)
    result = pyqtSignal(object)
    succeeded = pyqtSignal(int)
    failed = pyqtSignal(str)
    cancelled = pyqtSignal()

    def __init__(
        self,
        config: Config,
        profile: str,
        operation: str,
        parent: QObject | None = None,
        payload: object = None,
    ) -> None:
        super().__init__(parent)
        if operation not in OPERATIONS:
            raise ValueError(f"unknown operation: {operation!r}")
        self._config = config
        self._profile = profile
        self._operation = operation
        self._payload = payload
        self._cancel_token = Cancel()
        self._log = None
        self._last_progress_logged = float("-inf")

    # ---- lifecycle ---------------------------------------------------------
    @property
    def profile(self) -> str:
        return self._profile

    @property
    def operation(self) -> str:
        return self._operation

    @property
    def payload(self) -> object:
        return self._payload

    def cancel(self) -> None:
        """Ask the backend to stop at its next cancellation point."""
        self._cancel_token.cancel()

    def run(self) -> None:  # noqa: D401 - QThread entry point
        code: int | None = None
        lock = None
        try:
            if self._operation in LOCKED_OPERATIONS:
                lock = OperationLock(self._profile)
                try:
                    lock.acquire()
                except ProfileLockError:
                    raise BackendError(OPERATION_BUSY.format(name=self._profile)) from None
            backend = create(
                self._profile, self._config, emit=self._on_event, cancel=self._cancel_token
            )
            if self._operation == "status":
                from ..core.process import ProcessScanRound, use_process_scan_round

                if isinstance(self._payload, ProcessScanRound):
                    with use_process_scan_round(self._payload):
                        code = self._dispatch(backend)
                else:
                    code = self._dispatch(backend)
            else:
                code = self._dispatch(backend)
        except Cancelled:
            self._note("cancelled by user")
            self.cancelled.emit()
            return
        except BackendUnsupported as exc:
            self._fail_exception(exc)
            return
        except BackendError as exc:
            self._fail_exception(exc)
            return
        except Exception as exc:  # noqa: BLE001 - surfaced to the user, logged raw
            self._note(traceback.format_exc())
            self._fail_exception(exc)
            return
        finally:
            if lock is not None:
                lock.release()
            self._close_log()
        self.succeeded.emit(0 if code is None else int(code))

    def _dispatch(self, backend) -> int:
        op = self._operation
        if op == "status":
            self.result.emit(backend.status())
        elif op == "verify":
            self.result.emit(backend.verify())
        elif op == "install":
            backend.install()
        elif op == "update":
            backend.update()
        elif op == "repair":
            backend.repair()
        elif op == "remake_prefix":
            backend.remake_prefix()
        elif op == "login":
            backend.login()
        elif op == "launch":
            return int(backend.launch())
        elif op == "stop":
            backend.stop()
        elif op == "uninstall":
            backend.uninstall()
        elif op == "set_remember":
            backend.set_remember(bool(self._payload), persist_flag=False)
        elif op == "logout":
            backend.logout()
        elif op == "news":
            self.result.emit(self._load_news(backend))
        elif op == "media":
            self.result.emit(self._load_media(backend))
        elif op == "check_update":
            backend.check_update()
        else:  # pragma: no cover - guarded in __init__
            raise BackendError(f"unsupported operation: {op}")
        return 0

    def _load_news(self, backend):
        """Fetch official news (and its images) for ``backend``.

        Never raises: a backend without a source or an offline fetch yields an
        unavailable feed, which the UI renders as one muted line.
        """
        from ..core import artwork, news

        source = getattr(type(backend), "news_source", None)
        if source is None:
            return news.NewsResult(items=[], available=False)
        profile = backend.ctx.profile
        title = str(profile.get("title") or self._profile)
        hosts = tuple(getattr(type(backend), "official_image_hosts", ()) or ())
        hero_categories = tuple(getattr(type(backend), "news_hero_categories", ()) or ())
        hero_key = getattr(type(backend), "news_hero_key", None)
        result = news.load(
            source,
            key=backend.kind,
            hosts=hosts,
            slug=artwork.slug(profile, title),
            hero_categories=hero_categories,
            hero_key=hero_key,
            deadline=news.NEWS_DEADLINE,
            cancel=self._cancel_token,
        )
        # A queued Install/Launch must start as soon as the user asked, not
        # after the feed's daemon download threads finish.
        self._cancel_token.raise_if_cancelled()
        return result

    def _load_media(self, backend):
        """Fetch the official launcher background/banners for ``backend``.

        Never raises: a backend without configured media yields an empty result
        that the UI renders as today's look.
        """
        from ..core import artwork, news, yostar

        profile = backend.ctx.profile
        title = str(profile.get("title") or self._profile)
        game_tag = str(getattr(type(backend), "game_tag", "") or "")
        index_source = ""
        if hasattr(backend, "manifest_source"):
            try:
                index_source = backend.manifest_source()
            except Exception:  # noqa: BLE001 - best effort
                index_source = ""
        source = yostar.configured_media(profile) or index_source
        empty = {"kind": "media", "hero": None, "banners": []}
        if not source:
            return empty
        deadline = time.monotonic() + news.NEWS_DEADLINE
        try:
            timeout = max(1, int(deadline - time.monotonic()))
            media = yostar.load_media(source, game_tag=game_tag or None, timeout=timeout)
        except yostar.MediaError:
            return empty
        cached = yostar.cache_media(
            media, artwork.slug(profile, title), deadline=deadline, cancel=self._cancel_token
        )
        self._cancel_token.raise_if_cancelled()
        hero = cached.get("hero")
        return {"kind": "media", "hero": hero, "banners": cached.get("banners", [])}

    # ---- emitter plumbing --------------------------------------------------
    #: Seconds between two ``[progress]`` lines in the run log (the UI still gets every event).
    PROGRESS_LOG_INTERVAL = 30.0

    def _on_event(self, event: Event) -> None:
        if self._worth_logging(event):
            self._note(f"[{event.kind}] {event.message}")
        self.backend_event.emit(event)

    def _worth_logging(self, event: Event) -> bool:
        """Progress ticks arrive per megabyte; the log keeps one every half minute and the last."""
        if event.kind != "progress":
            return True
        now = time.monotonic()
        finished = event.progress is not None and event.progress >= 1.0
        if finished or now - self._last_progress_logged >= self.PROGRESS_LOG_INTERVAL:
            self._last_progress_logged = now
            return True
        return False

    def _fail(self, message: str) -> None:
        self._note(f"error: {message}")
        self.backend_event.emit(Event("error", message))
        self.failed.emit(message)

    def _fail_exception(self, exc: Exception) -> None:
        """Report an exception's own text; keep the class name in the log only."""
        log.warning("%s: %s", type(exc).__name__, exc)
        text = str(exc)
        if text:
            self._fail(text)
            return
        detail = getattr(exc, "message", "")
        self._fail(f"{type(exc).__name__}: {detail}")

    # ---- raw log -----------------------------------------------------------
    def _open_log(self):
        if self._log is not None:
            return self._log
        try:
            paths.logs_dir().mkdir(parents=True, exist_ok=True)
            path = paths.logs_dir() / f"{self._profile}-{self._operation}.log"
            # Keep the per-operation logs bounded: drop old runs before appending.
            try:
                logview.trim_sessions(path)
            except Exception:  # noqa: BLE001 - trimming is best effort
                pass
            # Owner-only, whatever the umask: logs can quote paths and account state.
            fd = os.open(path, os.O_WRONLY | os.O_CREAT | os.O_APPEND, 0o600)
            self._log = os.fdopen(fd, "a", encoding="utf-8")
            self._log.write(
                f"\n===== {datetime.now().isoformat(timespec='seconds')} "
                f"{self._operation} ({self._profile}) | nadare {__version__} =====\n"
            )
            self._log.flush()
        except OSError:
            self._log = None
        return self._log

    def _note(self, line: str) -> None:
        log = self._open_log()
        if log is None:
            return
        try:
            log.write(mask(line.rstrip("\n")) + "\n")
            log.flush()
        except OSError:
            pass

    def _close_log(self) -> None:
        if self._log is not None:
            try:
                self._log.close()
            except OSError:
                pass
            self._log = None


#: Background refreshes. They never mark the page busy and can be cancelled when
#: a user operation needs the profile's slot.
BACKGROUND = frozenset({"status", "news", "media", "check_update"})


#: Workers still running after their controller shut down (see ``shutdown``).
_detached: set[QThread] = set()


def wait_detached(timeout_ms: int = 5000) -> bool:
    """Wait for detached workers to end; True when none is still running."""
    deadline = time.monotonic() + timeout_ms / 1000
    for worker in list(_detached):
        remaining = max(0, int((deadline - time.monotonic()) * 1000))
        try:
            finished = worker.wait(remaining) or not worker.isRunning()
        except RuntimeError:
            # ``finished -> deleteLater`` already destroyed the C++ object; the
            # worker has ended, only the Python wrapper is left (#48).
            finished = True
        if finished:
            _detached.discard(worker)
    return not _detached


class _WorkerBridge(QObject):
    """Deliver one worker's signals on the controller's (GUI) thread.

    A worker emits from its own thread, but these slots live on a ``QObject``
    owned by the controller, so Qt queues them onto the GUI thread.  The
    controller therefore only ever mutates ``_workers``/``_pending`` and calls
    :meth:`BackendController._spawn` from the thread that owns it, never from a
    finishing worker's thread.  The bridge deletes itself once its worker has
    finished.
    """

    def __init__(
        self,
        controller: BackendController,
        profile: str,
        operation: str,
        worker: BackendWorker,
        parent: QObject | None = None,
    ) -> None:
        super().__init__(parent)
        self._controller = controller
        self._profile = profile
        self._operation = operation
        self._worker = worker

    def forward_event(self, event: Event) -> None:
        self._controller.backend_event.emit(self._profile, event)

    def forward_result(self, result: object) -> None:
        self._controller.result.emit(self._profile, result)

    def on_succeeded(self, code: int) -> None:
        self._controller._on_succeeded(self._profile, self._operation, code, self._worker)

    def on_failed(self, message: str) -> None:
        self._controller._on_failed(self._profile, self._operation, message, self._worker)

    def on_cancelled(self) -> None:
        self._controller._on_cancelled(self._profile, self._operation, self._worker)

    def on_finished(self) -> None:
        # Always reap the bridge, even if the controller's bookkeeping raises,
        # so a broken payload cannot leak the bridge for the whole process; the
        # exception must not escape the slot either.
        try:
            self._controller._on_worker_finished(self._profile, self._worker)
        except Exception:  # noqa: BLE001 - a slot must never raise
            log.exception("worker-finished bookkeeping failed for %r", self._profile)
        finally:
            self.deleteLater()


class BackendController(QObject):
    """Serialize backend operations per profile and fan their signals out.

    All signals carry the profile name first so a single controller can serve the
    whole window. ``busy(profile, operation, active)`` tracks the running task.

    Background refreshes (:data:`BACKGROUND`) are preemptible: asking for a user
    operation while one runs cancels it and queues the user operation to start as
    soon as the slot frees. At most one user operation is queued per profile.
    A :data:`PAUSABLE` operation can be paused, which cancels it while remembering
    its payload so :meth:`resume` can run it again from the partial downloads.
    """

    backend_event = pyqtSignal(str, object)  # profile, Event
    result = pyqtSignal(str, object)  # profile, Status | dict
    succeeded = pyqtSignal(str, str, int)  # profile, operation, exit code
    failed = pyqtSignal(str, str, str)  # profile, operation, message
    cancelled = pyqtSignal(str, str)  # profile, operation
    paused = pyqtSignal(str, str)  # profile, operation
    busy = pyqtSignal(str, str, bool)  # profile, operation, active

    def __init__(self, config: Config, parent: QObject | None = None) -> None:
        super().__init__(parent)
        self._config = config
        self._workers: dict[str, BackendWorker] = {}
        #: Exactly one user operation waiting for a cancellable background refresh.
        self._pending: dict[str, tuple[str, object]] = {}
        #: Profiles whose running worker was asked to pause.
        self._pausing: set[str] = set()
        #: Paused operations, with the payload needed to run them again.
        self._paused: dict[str, tuple[str, object]] = {}
        #: Once shutdown starts, no new worker may be spawned (a queued success
        #: callback used to start a status probe after the window was gone).
        self._closed = False

    # ---- queries -----------------------------------------------------------
    def is_busy(self, profile: str) -> bool:
        if profile in self._pending:
            return True
        worker = self._workers.get(profile)
        return worker is not None and worker.isRunning()

    def worker(self, profile: str) -> BackendWorker | None:
        return self._workers.get(profile)

    def busy_profiles(self) -> list[str]:
        return [name for name in self._workers if self.is_busy(name)]

    def mutating_profiles(self) -> list[str]:
        """Profiles with a running operation that changes the installation.

        Used at quit time to warn that an install/update is still writing.
        """
        return [
            name
            for name, worker in self._workers.items()
            if worker.isRunning() and worker.operation in MUTATING
        ]

    def running_operation(self, profile: str) -> str:
        """The operation currently running for ``profile`` ("" when idle)."""
        worker = self._workers.get(profile)
        return worker.operation if worker is not None and worker.isRunning() else ""

    def is_paused(self, profile: str) -> bool:
        return profile in self._paused

    def paused_operation(self, profile: str) -> str:
        """The paused operation for ``profile`` ("" when none)."""
        entry = self._paused.get(profile)
        return entry[0] if entry is not None else ""

    # ---- control -----------------------------------------------------------
    def start(self, profile: str, operation: str, payload: object = None) -> bool:
        """Start ``operation`` for ``profile``. Returns False if it cannot run.

        A background operation running in the slot is cancelled and the user
        operation is queued behind it (the page is told it is busy immediately).
        Another user operation, or a second queued user operation, is refused so
        the caller can show the usual Busy dialog.
        """
        if self._closed:
            return False
        pending = self._pending.get(profile)
        worker = self._workers.get(profile)
        running = worker is not None and worker.isRunning()

        if running or pending is not None:
            if operation in BACKGROUND:
                # Background refreshes are best-effort and never queue up.
                return False
            if pending is not None:
                return False
            if worker is not None and worker.operation in BACKGROUND:
                self._pending[profile] = (operation, payload)
                if operation not in BACKGROUND:
                    self._paused.pop(profile, None)
                self.busy.emit(profile, operation, True)
                worker.cancel()
                return True
            if operation == "stop" and worker is not None and worker.operation == "launch":
                # STOP while a launch is busy: cancel the launch worker (its
                # cancellation point stops the process group) and run the
                # backend-specific ``stop`` once the slot frees.
                self._pending[profile] = (operation, payload)
                if operation not in BACKGROUND:
                    self._paused.pop(profile, None)
                self.busy.emit(profile, operation, True)
                worker.cancel()
                return True
            return False

        if operation not in BACKGROUND:
            self._paused.pop(profile, None)
        self._spawn(profile, operation, payload)
        return True

    def pause(self, profile: str) -> bool:
        """Ask the running pausable operation for ``profile`` to stop and remember it."""
        if profile in self._pausing:
            return False
        queued = self._pending.get(profile)
        if queued is not None:
            # INSTALL was clicked while a status/news probe still ran, so it is only queued and
            # the page already shows it as busy: pausing it means it simply does not start.
            if queued[0] not in PAUSABLE:
                return False
            self._pending.pop(profile)
            probe = self._workers.get(profile)
            if probe is not None and probe.operation in BACKGROUND and probe.isRunning():
                probe.cancel()
                self._detach(profile, probe)
            self._paused[profile] = queued
            self.busy.emit(profile, queued[0], False)
            self.paused.emit(profile, queued[0])
            return True
        worker = self._workers.get(profile)
        if worker is None or not worker.isRunning() or worker.operation not in PAUSABLE:
            return False
        self._pausing.add(profile)
        worker.cancel()
        return True

    def forget(self, profile: str) -> None:
        """Drop a paused (or pausing) operation: the game was removed, nothing may resume it."""
        self._pausing.discard(profile)
        self._paused.pop(profile, None)

    def resume(self, profile: str) -> bool:
        """Run the operation paused for ``profile`` again with its payload."""
        entry = self._paused.pop(profile, None)
        if entry is None:
            return False
        if self.start(profile, entry[0], entry[1]):
            return True
        self._paused[profile] = entry
        return False

    def _spawn(self, profile: str, operation: str, payload: object = None) -> bool:
        if self._closed:
            return False
        worker = BackendWorker(self._config, profile, operation, parent=self, payload=payload)
        # Route every callback through a bridge that lives on this (GUI) thread.
        # Connecting to bound methods (not lambdas) means Qt queues the worker's
        # signals here instead of running controller bookkeeping in the worker.
        bridge = _WorkerBridge(self, profile, operation, worker, parent=self)
        worker.backend_event.connect(bridge.forward_event)
        worker.result.connect(bridge.forward_result)
        worker.succeeded.connect(bridge.on_succeeded)
        worker.failed.connect(bridge.on_failed)
        worker.cancelled.connect(bridge.on_cancelled)
        # The result signals fire from inside run(), before the thread has
        # exited: free the QThread only once it has really finished, or Qt
        # aborts with "QThread: Destroyed while thread is still running".
        worker.finished.connect(worker.deleteLater)
        worker.finished.connect(bridge.on_finished)
        self._workers[profile] = worker
        self.busy.emit(profile, operation, True)
        worker.start()
        return True

    def _detach(self, profile: str, worker: BackendWorker) -> None:
        """Let a blocked background worker finish without occupying the slot."""
        if self._workers.get(profile) is worker:
            self._workers.pop(profile, None)
        worker.setParent(None)
        _detached.add(worker)

    def cancel(self, profile: str) -> None:
        # Cancel beats a pause that has not landed yet, even when the worker thread has already
        # exited and only its queued "cancelled" slot is left to run.
        self._pausing.discard(profile)
        pending = self._pending.pop(profile, None)
        if pending is not None:
            # The queued user operation never started. Free the profile's slot
            # too: a news/status fetch may ignore cancellation until the network
            # returns, and it must not block the next INSTALL/LAUNCH attempt.
            worker = self._workers.get(profile)
            if worker is not None and worker.operation in BACKGROUND and worker.isRunning():
                worker.cancel()
                self._detach(profile, worker)
            self.busy.emit(profile, pending[0], False)
            self.cancelled.emit(profile, pending[0])
            return
        worker = self._workers.get(profile)
        if worker is not None and worker.isRunning():
            worker.cancel()
            return
        entry = self._paused.pop(profile, None)
        if entry is not None:
            self.cancelled.emit(profile, entry[0])

    def launching_profiles(self) -> list[str]:
        """Profiles whose game is currently running under a ``launch`` worker."""
        return [
            name
            for name, worker in self._workers.items()
            if worker.isRunning() and worker.operation == "launch"
        ]

    def reopen(self) -> None:
        """Accept operations again after ``shutdown(keep_launches=True)``.

        Closing the window while a game runs only hides it; showing it again
        must not leave every operation refused.
        """
        self._closed = False

    def shutdown(self, wait_ms: int = 4000, *, keep_launches: bool = False) -> None:
        """Ask every live worker to stop and wait within one overall deadline.

        A single deadline (rather than ``wait_ms`` per worker) keeps ``closeEvent``
        responsive when several probes are stuck on the network. With
        ``keep_launches`` running games are left alone: closing the window must
        not kill a game.
        """
        self._closed = True
        for profile, (operation, _payload) in list(self._pending.items()):
            # A queued user operation that will now never start: let the page
            # drop its busy state (the app may stay alive behind a running game).
            self.busy.emit(profile, operation, False)
            self.cancelled.emit(profile, operation)
        self._pending.clear()
        self._pausing.clear()
        self._paused.clear()
        workers = [
            worker
            for worker in self._workers.values()
            if not (keep_launches and worker.operation == "launch")
        ]
        for worker in workers:
            if worker.isRunning():
                worker.cancel()
        deadline = time.monotonic() + wait_ms / 1000
        for worker in workers:
            if worker.isRunning():
                remaining = max(0, int((deadline - time.monotonic()) * 1000))
                worker.wait(remaining)
        for worker in workers:
            if worker.isRunning():
                # A blocking fetch outlived the wait: destroying a running QThread
                # with its window aborts the process, so keep it alive unparented.
                worker.setParent(None)
                _detached.add(worker)

    # ---- callbacks ---------------------------------------------------------
    def _finish(self, profile: str, operation: str, worker: BackendWorker) -> None:
        if self._workers.get(profile) is worker:
            self._workers.pop(profile, None)
        self.busy.emit(profile, operation, False)

    def _on_worker_finished(self, profile: str, worker: BackendWorker) -> None:
        """Start a queued user operation once the background refresh has ended."""
        if self._closed:
            self._pending.pop(profile, None)
            return
        pending = self._pending.get(profile)
        if pending is None:
            return
        current = self._workers.get(profile)
        if current is not None and current is not worker and current.isRunning():
            return
        if self._workers.get(profile) is worker:
            self._workers.pop(profile, None)
        self._pending.pop(profile, None)
        self._spawn(profile, pending[0], pending[1])

    def _on_succeeded(self, profile: str, operation: str, code: int, worker) -> None:
        self._pausing.discard(profile)
        self._finish(profile, operation, worker)
        self.succeeded.emit(profile, operation, code)

    def _on_failed(self, profile: str, operation: str, message: str, worker) -> None:
        self._pausing.discard(profile)
        self._finish(profile, operation, worker)
        self.failed.emit(profile, operation, message)

    def _on_cancelled(self, profile: str, operation: str, worker) -> None:
        if profile in self._pausing:
            self._pausing.discard(profile)
            self._paused[profile] = (operation, getattr(worker, "payload", None))
            self._finish(profile, operation, worker)
            self.paused.emit(profile, operation)
            return
        self._finish(profile, operation, worker)
        self.cancelled.emit(profile, operation)


def is_mutating(operation: str) -> bool:
    """True when ``operation`` changes state and must not run concurrently."""
    return operation in MUTATING


__all__ = [
    "OPERATIONS",
    "MUTATING",
    "PAUSABLE",
    "BACKGROUND",
    "BackendWorker",
    "BackendController",
    "CallTask",
    "is_mutating",
    "wait_detached",
]
