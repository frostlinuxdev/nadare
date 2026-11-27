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
import re
import time
import traceback
from collections.abc import Callable
from contextlib import ExitStack
from dataclasses import dataclass
from datetime import datetime
from typing import cast

from PyQt6.QtCore import QObject, QRunnable, QThread, pyqtSignal

from .. import __version__
from ..core import (
    exitcode,
    flatpakreport,
    friendly,
    launch_window,
    launchgate,
    operation_policy,
    patchops,
    paths,
    runlog,
)
from ..core.backends import (
    BackendError,
    BackendUnsupported,
    GameBackend,
    PredownloadBackend,
    create,
    supports_media,
)
from ..core.config import Config, bounded_int
from ..core.events import Cancel, Cancelled, Event, mask

log = logging.getLogger(__name__)

#: Operations nobody asked for; a removed game may drop them at once (the same set as the
#: window's SILENT_OPERATIONS).
_BACKGROUND_OPERATIONS = operation_policy.operations_with("background")

#: Operations a worker understands.
OPERATIONS = tuple(operation_policy.POLICIES)

#: A DXVK/VKD3D ``info:`` line (optionally behind a thread id or a decimal timestamp).
#: These are needed while the launch runs (the first-frame watch reads them) but are far
#: too many per run for the run log and the 400-event replay (#1821). The prefix is the
#: first-frame detector's grammar, so what the cap calls a layer line is exactly what the
#: detector can accept (#1821 follow-up).
_LAYER_INFO_RE = re.compile(r"^\s*" + launch_window.LOG_PREFIX + r"info:")

#: How many of those lines one run keeps, for a readable start-up trace. ``err:`` and
#: ``warn:`` lines are never dropped.
LAYER_INFO_LINES = 20

#: Presenter/swapchain lines kept per run, outside :data:`LAYER_INFO_LINES`: the one
#: that ended the sheet, a launcher's before it, and a few resizes after.
PRESENTER_LINES = 8

#: Longest line written to the run log. Masking a longer line costs time on the thread
#: that drains the game's output; a game that prints a 20 KB blob loses its tail.
LOG_LINE_CHARS = 4096


@dataclass(frozen=True)
class QualityCleanup:
    """The payload of ``remove_qualities`` (#1474): the tiers the user confirmed, and the
    settings the question was asked with (a Settings save meanwhile must not change which
    tier is deleted)."""

    only: tuple[str, ...]
    config: Config


@dataclass(frozen=True)
class QualityCleanupResult:
    """What ``remove_qualities`` reports: bytes freed, warnings, and the tiers asked for."""

    freed: int
    warnings: tuple[str, ...]
    only: tuple[str, ...]


#: Operations that exclude each other (and Remove) per profile across processes.
LOCKED_OPERATIONS = operation_policy.operations_with("locked")
PAUSABLE = operation_policy.operations_with("pausable")
CAPPED = operation_policy.operations_with("capped")
DEFAULT_ACTIVE_INSTALLS = 1
MAX_ACTIVE_INSTALLS = 4
MUTATING = operation_policy.operations_with("mutating")
_WAIT_FOR_PAUSED = operation_policy.operations_with("wait_for_paused")


class _CallSignals(QObject):
    """Signals for :class:`CallTask` (created on the caller's thread)."""

    done = pyqtSignal(object)
    failed = pyqtSignal(str)


def _game_pids(backend):
    """``backend.game_pids()``, or ``None``: the window monitor must never block Play."""
    try:
        return backend.game_pids()
    except Exception:  # noqa: BLE001 - an optional observation, not a launch failure
        log.debug("no process rule for the launch monitor", exc_info=True)
        return None


def _game_processes(backend, pids):
    """The game itself (not its launcher or helper), as the first-frame gate (#1821)."""
    try:
        game = getattr(backend, "game_processes", None)
        if callable(game):
            found = game()
            if found is not None:
                return found
    except Exception:  # noqa: BLE001 - an optional observation, not a launch failure
        log.debug("no game process rule for the launch monitor", exc_info=True)
    if pids is None:
        return None
    return lambda: bool(pids())


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



def mask_log_line(line: str) -> str:
    """``mask(line)``, cut to :data:`LOG_LINE_CHARS` first.

    The cut is at a whitespace boundary and a token that crosses the limit is dropped
    whole: a secret cut short could fall under the masking length and show in part.
    """
    if len(line) <= LOG_LINE_CHARS:
        return mask(line)
    head = line[:LOG_LINE_CHARS]
    if not line[LOG_LINE_CHARS].isspace():
        parts = head.rsplit(None, 1)
        head = parts[0] if len(parts) == 2 else ""
    return f"{mask(head)} …<{len(line) - len(head)} chars cut>"

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
        if operation == "stop" and payload is True:
            # A repeated Stop during the launch also applies to its queued cleanup.
            self._cancel_token.cancel()
        self._log = None
        self._last_progress_logged = float("-inf")
        #: DXVK/VKD3D info lines kept in this run's log and replay (see :data:`LAYER_INFO_LINES`).
        self._layer_info_kept = 0
        self._presenter_lines_kept = 0
        #: The launch's window evidence while ``launchgate.play`` runs (#1821).
        self._window_watch = None

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

    def skip_step(self) -> None:
        """Ask the step that offers a skip to stop; the operation itself carries on."""
        self._cancel_token.skip_step()

    def run(self) -> None:  # noqa: D401 - QThread entry point
        started = time.monotonic()
        code: int | None = None
        resources = ExitStack()
        try:
            resources.enter_context(operation_policy.operation_lock(self._operation, self._profile))
            config = self._config
            if self._operation == "remove_qualities":
                if not isinstance(self._payload, QualityCleanup):
                    raise BackendError("the quality cleanup needs its settings snapshot")
                config = self._payload.config
            backend = create(self._profile, config, emit=self._on_event, cancel=self._cancel_token)
            resources.enter_context(
                operation_policy.preflight(
                    self._operation,
                    backend,
                    name=self._profile,
                    adapter="gui",
                    on_retry=self._runtime_retry_notice,
                )
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
            self._note(self._end_text("cancelled by user", started))
            self.cancelled.emit()
            return
        except BackendUnsupported as exc:
            self._end_failed(exc, started)
            return
        except BackendError as exc:
            self._end_failed(exc, started)
            return
        except Exception as exc:  # noqa: BLE001 - surfaced to the user, logged raw
            self._note(traceback.format_exc())
            self._end_failed(exc, started)
            return
        else:
            code = 0 if code is None else int(code)
            detail = f"exit={code}"
            signal = exitcode.crash_signal(code)
            if signal:
                detail += f" signal={signal}"
            self._note(self._end_text(detail, started))
        finally:
            resources.close()
            self._close_log()
        self.succeeded.emit(code)

    def _end_text(self, detail: str, started: float) -> str:
        """One ``[end]`` line: the log must say how the run ended even when no dialog was read."""
        elapsed = exitcode.short_duration(time.monotonic() - started)
        return f"[end] {detail} after {elapsed}"

    def _end_failed(self, exc: Exception, started: float) -> None:
        """Report the failure and then end the log with the same one-line message."""
        message = self._fail_exception(exc)
        self._note(self._end_text(f"failed: {' '.join(message.split())}", started))

    def _dispatch(self, backend: GameBackend) -> int:
        op = self._operation
        if op == "status":
            # An old game outside the Flatpak's folders says why, not "not installed" (#1299).
            self.result.emit(flatpakreport.status_of(backend))
        elif op == "verify":
            self.result.emit(backend.verify())
        elif op == "install":
            backend.install()
        elif op == "update":
            backend.update()
            patchops.warn_if_undone(backend)
        elif op == "repair":
            backend.repair()
            patchops.warn_if_undone(backend)
        elif op == "apply_patch":
            patchops.apply(backend, str(self._payload))
        elif op == "update_patch":
            patchops.update(backend, str(self._payload))
        elif op == "remove_patch":
            patchops.remove(backend, str(self._payload))
        elif op == "remake_prefix":
            backend.remake_prefix()
        elif op == "login":
            backend.login()
        elif op == "launch":
            # Every pre-Play check, then the game (#1451).
            pids = _game_pids(backend)
            with launch_window.watch(
                pids,
                self._on_event,
                self._cancel_token,
                game_processes=_game_processes(backend, pids),
            ) as watched:
                self._window_watch = watched
                try:
                    return launchgate.play(backend)
                finally:
                    self._window_watch = None
        elif op == "stop":
            backend.stop()
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
        elif op == "predownload":
            if not backend.supports_predownload:
                raise BackendUnsupported("this game has no predownload")
            cast(PredownloadBackend, backend).predownload()
        elif op == "remove_qualities":
            payload = self._payload
            assert isinstance(payload, QualityCleanup)  # checked in run()
            freed, warnings = backend.remove_other_qualities(only=list(payload.only))
            self.result.emit(
                QualityCleanupResult(int(freed), tuple(str(w) for w in warnings), payload.only)
            )
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
        """Fetch the game-page hero (and any publisher banners) for ``backend``.

        A publisher feed supplies the background and banners where the backend has one; every
        other game page falls back to the index wide art (or Steam's library hero). Never
        raises: a game with no art anywhere keeps today's plain card.
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
        # Only a backend with a publisher feed reads one; every other game page gets the
        # index wide art (or Steam's library hero) below.
        source = ""
        if supports_media(str(profile.get("backend") or "")):
            source = yostar.configured_media(profile) or index_source

        deadline = time.monotonic() + news.NEWS_DEADLINE

        def await_media(call):
            self._cancel_token.raise_if_cancelled()
            if time.monotonic() >= deadline:
                return None
            try:
                # Reuse news' polling wait, including for images and fallback artwork.
                return news._await(call, cancel=self._cancel_token, deadline=deadline)
            except news._Stopped:
                self._cancel_token.raise_if_cancelled()
                return None

        def fallback_hero() -> str | None:
            # Already-cached art answers at once; the index is waited for only when there is
            # nothing on disk yet (a background page open must not sit on the network).
            wide = await_media(lambda: artwork.fetch(profile, title, "cover", art="wide"))
            if wide is None:
                wide = await_media(lambda: artwork.fetch(profile, title, "hero"))
            self._cancel_token.raise_if_cancelled()
            return str(wide) if wide is not None else None

        if not source:
            return {"kind": "media", "hero": fallback_hero(), "banners": []}
        try:
            media = await_media(
                lambda: yostar.load_media(
                    source,
                    game_tag=game_tag or None,
                    timeout=max(1, int(deadline - time.monotonic())),
                    cancel=self._cancel_token,
                )
            )
        except (yostar.MediaError, yostar.ManifestError, OSError, ValueError):
            # Bad or unreachable media: the page shows no banners, the worker does not fail
            # (#1207 b251). A cancel is not one of these and still stops the worker.
            return {"kind": "media", "hero": fallback_hero(), "banners": []}
        if media is None:
            return {"kind": "media", "hero": fallback_hero(), "banners": []}
        cached = await_media(
            lambda: yostar.cache_media(
                media, artwork.slug(profile, title), deadline=deadline, cancel=self._cancel_token
            )
        ) or {}
        self._cancel_token.raise_if_cancelled()
        hero = cached.get("hero") or fallback_hero()
        return {"kind": "media", "hero": hero, "banners": cached.get("banners", [])}

    # ---- emitter plumbing --------------------------------------------------
    #: Seconds between two ``[progress]`` lines in the run log (the UI still gets every event).
    PROGRESS_LOG_INTERVAL = 30.0

    def _retry_runtime_step(self, backend: object) -> None:
        """Repair also gives the Visual C++ step another chance.

        A skip, cancel or hang is remembered per prefix so launches do not keep asking; the user
        asking for a repair is the signal to forget it. Best effort: it never breaks the repair.
        """
        if operation_policy.retry_runtime(backend, adapter="gui"):
            self._runtime_retry_notice()

    def _runtime_retry_notice(self) -> None:
        self._on_event(
            Event(
                "status",
                "The Windows component (Visual C++ runtime) will be tried again at "
                "the next launch.",
            )
        )

    def _on_event(self, event: Event) -> None:
        # The launch's own output: WindowEvidence.feed recognises only the DXVK/VKD3D
        # swapchain line, so a RendererProbe or helper line cannot end the sheet (#1821).
        # Read once: the monitor's ``finally`` can clear it on another thread.
        watch = self._window_watch
        if event.kind == "log" and watch is not None:
            watch.feed(event.message)
        if event.kind == "log" and self._drop_layer_info(event.message):
            return
        if self._worth_logging(event):
            self._note(f"[{event.kind}] {event.message}")
        self.backend_event.emit(event)

    def _drop_layer_info(self, line: str) -> bool:
        """Whether this DXVK/VKD3D ``info:`` line is only noise now (#1821).

        The first :data:`LAYER_INFO_LINES` non-presenter lines of a run are kept for the
        log and the replay; after that the line has already been offered to the
        first-frame watch, so it is dropped instead of flushing thousands of lines and
        evicting Nadare's own ``runner:``/``exec:``/``env:`` lines from the replay. The
        first presenter line is always kept, so a maintainer can see in the log what
        ended the sheet; later ones (DXVK reallocates on every resize) share the cap.
        ``err:``/``warn:`` stay.
        """
        if not _LAYER_INFO_RE.match(line):
            return False
        if launch_window.is_first_frame(line):
            # Their own small budget: a launcher's swapchain line cannot use up the slot
            # of the game's, and a resize loop cannot flood the log.
            if self._presenter_lines_kept < PRESENTER_LINES:
                self._presenter_lines_kept += 1
                return False
            return True
        if self._layer_info_kept < LAYER_INFO_LINES:
            self._layer_info_kept += 1
            return False
        return True

    def _worth_logging(self, event: Event) -> bool:
        """Progress ticks arrive per megabyte; the log keeps one every half minute and the last."""
        if event.kind == "window_seen":
            # Internal launch feedback for the sheet, not a line a run log should carry.
            return False
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

    def _fail_exception(self, exc: Exception) -> str:
        """Report an exception in plain words where a common cause is known, else its own text."""
        log.warning("%s: %s", type(exc).__name__, exc)
        if str(exc):
            message = friendly.user_text(exc)
        else:
            detail = getattr(exc, "message", "")
            message = f"{type(exc).__name__}: {detail}"
        self._fail(message)
        return message

    # ---- raw log -----------------------------------------------------------
    def _open_log(self):
        if self._log is not None:
            return self._log
        try:
            logs = paths.logs_dir()
            logs.mkdir(parents=True, exist_ok=True)
            background = self._operation in BACKGROUND
            if background:
                # Status/news/media checks share one small rolling file per game.
                path = runlog.background_log_path(logs, self._profile)
            else:
                path = runlog.run_log_path(logs, self._profile, self._operation)
            path.parent.mkdir(mode=0o700, exist_ok=True)
            if background:
                runlog.trim_rolling(path)
            # Owner-only, whatever the umask: logs can quote paths and account state.
            fd = os.open(path, os.O_WRONLY | os.O_CREAT | os.O_APPEND, 0o600)
            self._log = os.fdopen(fd, "a", encoding="utf-8")
            self._log_started = time.monotonic()
            self._log.write(
                f"\n===== {datetime.now().isoformat(timespec='seconds')} "
                f"{self._operation} ({self._profile}) | nadare {__version__} =====\n"
            )
            self._log.flush()
            if not background:
                # Keep the game's folder bounded: drop old runs, never this one.
                try:
                    runlog.prune_game(path.parent, protect=[path])
                except Exception:  # noqa: BLE001 - pruning is best effort
                    pass
        except OSError:
            self._log = None
        return self._log

    def _note(self, line: str) -> None:
        log = self._open_log()
        if log is None:
            return
        try:
            text = mask_log_line(line.rstrip("\n"))
            if self._operation not in BACKGROUND:
                prefix = f"+{time.monotonic() - self._log_started:6.1f}s "
                text = "\n".join(prefix + part for part in text.split("\n"))
            log.write(text + "\n")
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
BACKGROUND = operation_policy.operations_with("background")


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
        self._epoch = getattr(controller, "_profile_epochs", {}).get(profile, 0)
        config = getattr(controller, "_config", None)
        self._profile_object = config.profiles.get(profile) if config is not None else None

    def _same_profile(self) -> bool:
        return (
            self._epoch == self._controller._profile_epochs.get(self._profile, 0)
            and self._profile_object is not None
            and self._controller._config.profiles.get(self._profile) is self._profile_object
        )

    def _current(self) -> bool:
        return self._same_profile() and self._controller._workers.get(self._profile) is self._worker

    def _retire(self) -> None:
        self._controller._finish(self._profile, self._operation, self._worker, notify=False)

    def forward_event(self, event: Event) -> None:
        try:
            if not self._current():
                return
            self._controller.backend_event.emit(self._profile, event)
        except Exception:  # noqa: BLE001 - a slot must never raise
            log.exception("worker-event forward failed for %r", self._profile)

    def forward_result(self, result: object) -> None:
        try:
            if not self._current():
                return
            self._controller.result.emit(self._profile, self._operation, result)
        except Exception:  # noqa: BLE001 - a slot must never raise
            log.exception("worker-result forward failed for %r", self._profile)

    def on_succeeded(self, code: int) -> None:
        try:
            if not self._current():
                self._retire()
                return
            self._controller._on_succeeded(self._profile, self._operation, code, self._worker)
        except Exception:  # noqa: BLE001 - a slot must never raise
            log.exception("worker-succeeded bookkeeping failed for %r", self._profile)

    def on_failed(self, message: str) -> None:
        try:
            if not self._current():
                self._retire()
                return
            self._controller._on_failed(self._profile, self._operation, message, self._worker)
        except Exception:  # noqa: BLE001 - a slot must never raise
            log.exception("worker-failed bookkeeping failed for %r", self._profile)

    def on_cancelled(self) -> None:
        try:
            if not self._current():
                self._retire()
                return
            self._controller._on_cancelled(self._profile, self._operation, self._worker)
        except Exception:  # noqa: BLE001 - a slot must never raise
            log.exception("worker-cancelled bookkeeping failed for %r", self._profile)

    def on_finished(self) -> None:
        # Always reap the bridge, even if the controller's bookkeeping raises,
        # so a broken payload cannot leak the bridge for the whole process; the
        # exception must not escape the slot either.
        try:
            if not self._same_profile():
                self._retire()
                return
            self._controller._on_worker_finished(self._profile, self._worker)
        except Exception:  # noqa: BLE001 - a slot must never raise
            log.exception("worker-finished bookkeeping failed for %r", self._profile)
        finally:
            self.deleteLater()


@dataclass
class InstallOverview:
    """``(profile, operation)`` pairs: what runs now, what waits (in order), what is paused."""

    running: list[tuple[str, str]]
    waiting: list[tuple[str, str]]
    paused: list[tuple[str, str]]


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
    result = pyqtSignal(str, str, object)  # profile, operation, its result (#1458)
    succeeded = pyqtSignal(str, str, int)  # profile, operation, exit code
    failed = pyqtSignal(str, str, str)  # profile, operation, message
    cancelled = pyqtSignal(str, str)  # profile, operation
    paused = pyqtSignal(str, str)  # profile, operation
    busy = pyqtSignal(str, str, bool)  # profile, operation, active
    queued = pyqtSignal(str, str)  # profile, operation: waiting for a free install slot
    queue_changed = pyqtSignal()  # the waiting line gained, lost or reordered a game

    def __init__(self, config: Config, parent: QObject | None = None) -> None:
        super().__init__(parent)
        self._config = config
        self._workers: dict[str, BackendWorker] = {}
        self._profile_epochs: dict[str, int] = {}
        #: Profiles with a live ``launch`` worker. Kept explicitly because a
        #: QThread still reports ``isRunning()`` while its final slots run.
        self._launches: set[str] = set()
        #: Profiles whose launch reported its end, even if the thread lingers.
        self._launch_ended: set[str] = set()
        #: Exactly one user operation waiting for a cancellable background refresh.
        self._pending: dict[str, tuple[str, object]] = {}
        #: Capped operations waiting for a free slot, first come first served (#770).
        self._waiting: dict[str, tuple[str, object]] = {}
        #: Profiles whose running worker was asked to pause.
        self._pausing: set[str] = set()
        #: Installs paused to let another one go first; they rejoin the front of the line.
        self._requeue: set[str] = set()
        #: Paused operations, with the payload needed to run them again.
        self._paused: dict[str, tuple[str, object]] = {}
        #: Once shutdown starts, no new worker may be spawned (a queued success
        #: callback used to start a status probe after the window was gone).
        self._closed = False

    # ---- queries -----------------------------------------------------------
    def is_busy(self, profile: str) -> bool:
        if profile in self._pending or profile in self._waiting:
            return True
        worker = self._workers.get(profile)
        return worker is not None and worker.isRunning()

    def worker(self, profile: str) -> BackendWorker | None:
        return self._workers.get(profile)

    def is_waiting(self, profile: str) -> bool:
        """Whether ``profile``'s install/update/repair waits for a free slot."""
        return profile in self._waiting

    def max_active_installs(self) -> int:
        """How many installs, updates and repairs may run at once (the setting, kept sane)."""
        value = self._config.data.get("max_concurrent_installs", DEFAULT_ACTIVE_INSTALLS)
        return bounded_int(value, DEFAULT_ACTIVE_INSTALLS, 1, MAX_ACTIVE_INSTALLS)

    def _active_installs(self) -> int:
        return sum(1 for worker in self._workers.values() if worker.operation in CAPPED)

    def busy_profiles(self) -> list[str]:
        return [name for name in self._workers if self.is_busy(name)]

    def mutating_profiles(self) -> list[str]:
        """Profiles with a running operation that changes the installation.

        Used at quit time to warn that an install/update is still writing.
        """
        return [
            name
            for name, worker in self._workers.items()
            if worker.isRunning()
            and worker.operation in MUTATING
            and not (worker.operation == "launch" and name in self._launch_ended)
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

    def waiting_operation(self, profile: str) -> str:
        """The operation waiting for a free install slot for ``profile`` ("" when none)."""
        entry = self._waiting.get(profile)
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
        if operation in _WAIT_FOR_PAUSED and profile in self._paused:
            # A check or another download must not throw away a paused download's RESUME
            # entry (#855), also not when it would queue behind a background probe (round 5);
            # resume() pops the entry before it starts the operation again.
            return False
        pending = self._pending.get(profile)
        worker = self._workers.get(profile)
        running = worker is not None and worker.isRunning()
        waiting = profile in self._waiting

        if operation == "stop" and running and (
            worker.operation == "stop"
            or (worker.operation == "launch" and pending is not None and pending[0] == "stop")
        ):
            # Keep escalation intent when the launch hands off to queued cleanup.
            if worker.operation == "launch":
                self._pending[profile] = ("stop", True)
            worker.cancel()
            return True

        if running or pending is not None or waiting:
            if operation in BACKGROUND:
                # Background refreshes are best-effort and never queue up.
                return False
            if operation == "stop" and pending is not None and pending[0] == "launch":
                self.cancel(profile)
                return True
            if pending is not None or waiting:
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
        return self._start_or_wait(profile, operation, payload)

    def _start_or_wait(self, profile: str, operation: str, payload: object = None) -> bool:
        """Run ``operation`` now, or hold a capped one until a slot frees up (#770)."""
        if operation in CAPPED and self._active_installs() >= self.max_active_installs():
            self._waiting[profile] = (operation, payload)
            self.busy.emit(profile, operation, True)
            self.queued.emit(profile, operation)
            self.queue_changed.emit()
            return True
        self._spawn(profile, operation, payload)
        return True

    def _release_waiting(self) -> None:
        """Start waiting operations, oldest first, while slots are free."""
        while (
            self._waiting
            and not self._closed
            and self._active_installs() < self.max_active_installs()
        ):
            profile = next(iter(self._waiting))
            operation, payload = self._waiting.pop(profile)
            self.queue_changed.emit()
            self._spawn(profile, operation, payload)

    def install_overview(self) -> InstallOverview:
        """Every install, update and repair that is running, waiting or paused, for one list."""
        running = [
            (name, worker.operation)
            for name, worker in self._workers.items()
            if worker.operation in CAPPED and worker.isRunning()
        ]
        waiting = [(name, entry[0]) for name, entry in self._waiting.items()]
        paused = [(name, entry[0]) for name, entry in self._paused.items() if entry[0] in CAPPED]
        return InstallOverview(running, waiting, paused)

    def queue_order(self) -> list[str]:
        """The games waiting for an install slot, first in line first."""
        return list(self._waiting)

    def _front(self, profile: str) -> None:
        entry = self._waiting.pop(profile)
        self._waiting = {profile: entry, **self._waiting}
        self.queue_changed.emit()

    def move_to_front(self, profile: str) -> bool:
        """Put a waiting install first in line. False when it is not waiting or already first."""
        if profile not in self._waiting or next(iter(self._waiting)) == profile:
            return False
        self._front(profile)
        return True

    def _victim(self, pausable: Callable[[str], bool]) -> str | None:
        """The running install to pause so another can go first (the newest that can pause)."""
        for name in reversed(list(self._workers)):
            worker = self._workers[name]
            if (
                worker.operation in CAPPED
                and worker.isRunning()
                and name not in self._pausing
                and pausable(name)
            ):
                return name
        return None

    def can_start_now(self, profile: str, pausable: Callable[[str], bool]) -> bool:
        """Whether :meth:`start_now` would work: ``profile`` waits and a running one can pause."""
        return profile in self._waiting and self._victim(pausable) is not None

    def start_now(self, profile: str, pausable: Callable[[str], bool]) -> bool:
        """Pause a running install and give its slot to the waiting ``profile``.

        The paused install keeps its downloads and goes back to the front of the line, so it
        carries on as soon as this one is done. ``pausable`` says which running games can pause
        (only those that resume from what is already on disk).
        """
        if profile not in self._waiting:
            return False
        victim = self._victim(pausable)
        if victim is None:
            return False
        self._requeue.add(victim)
        self._pausing.add(victim)
        self._front(profile)
        self._workers[victim].cancel()
        return True

    def pause(self, profile: str) -> bool:
        """Ask the running pausable operation for ``profile`` to stop and remember it."""
        if profile in self._pausing:
            return False
        waiting = self._waiting.pop(profile, None)
        if waiting is not None:
            # Still waiting for a slot: pausing it means it simply does not start.
            self.queue_changed.emit()
            self._paused[profile] = waiting
            self.busy.emit(profile, waiting[0], False)
            self.paused.emit(profile, waiting[0])
            return True
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
        """Drop everything a removed game could still resume or keep occupied."""
        self._profile_epochs[profile] = self._profile_epochs.get(profile, 0) + 1
        self._pausing.discard(profile)
        self._requeue.discard(profile)
        self._paused.pop(profile, None)
        self._pending.pop(profile, None)
        if self._waiting.pop(profile, None) is not None:
            self.queue_changed.emit()
        worker = self._workers.get(profile)
        if worker is not None and worker.operation in _BACKGROUND_OPERATIONS:
            # A pending status probe must not keep the name busy for a re-added game.
            # Only background work: a launch's slot must still reach _finish/launch_ended.
            self._workers.pop(profile, None)
            if worker.isRunning():
                worker.cancel()

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
        if operation == "launch":
            self._launches.add(profile)
            self._launch_ended.discard(profile)
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

    def skip_step(self, profile: str) -> None:
        """Skip the running step of ``profile``'s operation (it keeps running)."""
        worker = self._workers.get(profile)
        if worker is not None and worker.isRunning():
            worker.skip_step()

    def cancel(self, profile: str) -> None:
        # Cancel beats a pause that has not landed yet, even when the worker thread has already
        # exited and only its queued "cancelled" slot is left to run.
        self._pausing.discard(profile)
        self._requeue.discard(profile)
        waiting = self._waiting.pop(profile, None)
        if waiting is not None:
            self.queue_changed.emit()
            self.busy.emit(profile, waiting[0], False)
            self.cancelled.emit(profile, waiting[0])
            return
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
        running = worker is not None and worker.isRunning()
        entry = self._paused.pop(profile, None)
        if running:
            worker.cancel()
            # A paused download is not a running worker: cancel it too, or pressing Cancel
            # on its row would only stop the background status/news probe next to it.
            if entry is not None:
                self.cancelled.emit(profile, entry[0])
            return
        if entry is not None:
            self.cancelled.emit(profile, entry[0])

    def launching_profiles(self) -> list[str]:
        """Profiles whose game is currently running under a ``launch`` worker."""
        names = set(self._launches)
        for name, worker in self._workers.items():
            if (
                worker.operation == "launch"
                and name not in self._launch_ended
                and worker.isRunning()
            ):
                # A worker put in the slot directly (rather than through
                # ``_spawn``) is still honoured, as long as its end was not seen.
                names.add(name)
        return sorted(names)

    def launch_ended(self, profile: str) -> None:
        """Record that ``profile``'s launch reported success/failure/cancel.

        Its QThread may still be returning, so do not wait for ``isRunning()``
        to turn false before deciding the game is no longer running.
        """
        self._launches.discard(profile)
        self._launch_ended.add(profile)

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
        for profile, (operation, _payload) in list(self._waiting.items()):
            self.busy.emit(profile, operation, False)
            self.cancelled.emit(profile, operation)
        self._waiting.clear()
        self.queue_changed.emit()
        self._pausing.clear()
        self._requeue.clear()
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
    def _finish(
        self, profile: str, operation: str, worker: BackendWorker, *, notify: bool = True
    ) -> None:
        if self._workers.get(profile) is not worker:
            # A worker whose slot ran late (a stall on the GUI thread, or one that was detached):
            # the profile has moved on and its busy state belongs to the newer operation, or was
            # already cleared by the cancel or pause that detached this one.
            return
        self._workers.pop(profile, None)
        if operation == "launch":
            self.launch_ended(profile)
        if notify:
            self.busy.emit(profile, operation, False)
        self._release_waiting()

    def _on_worker_finished(self, profile: str, worker: BackendWorker) -> None:
        """Start a queued user operation once the background refresh has ended."""
        if worker.operation == "launch" and self._workers.get(profile) is worker:
            # A launch that produced no terminal signal still ends with its thread.
            self.launch_ended(profile)
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
        self._start_or_wait(profile, pending[0], pending[1])

    def _on_succeeded(self, profile: str, operation: str, code: int, worker) -> None:
        if self._workers.get(profile) is not worker:
            return
        self._pausing.discard(profile)
        self._requeue.discard(profile)
        self._finish(profile, operation, worker)
        self.succeeded.emit(profile, operation, code)

    def _on_failed(self, profile: str, operation: str, message: str, worker) -> None:
        if self._workers.get(profile) is not worker:
            return
        self._pausing.discard(profile)
        self._requeue.discard(profile)
        self._finish(profile, operation, worker)
        self.failed.emit(profile, operation, message)

    def _on_cancelled(self, profile: str, operation: str, worker) -> None:
        if self._workers.get(profile) is not worker:
            return
        if profile in self._pausing:
            self._pausing.discard(profile)
            if profile in self._requeue:
                # Paused to let another install go first: back to the front of the line.
                self._requeue.discard(profile)
                current = self._workers.get(profile) is worker
                self._finish(profile, operation, worker)
                if current and not self._closed:
                    self._start_or_wait(profile, operation, getattr(worker, "payload", None))
                    if profile in self._waiting:
                        self._front(profile)
                return
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
