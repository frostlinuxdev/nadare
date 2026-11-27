# SPDX-License-Identifier: GPL-3.0-or-later
# Copyright (C) 2026 Nadare contributors
"""Reactions to the backend controller: busy, queue, pause, cancel, events, results and failures.

Widget adapter for :mod:`nadare.gui.operation_presentation`.
"""

from __future__ import annotations

import copy
import logging
import time
from typing import Any

from PyQt6.QtWidgets import (
    QMessageBox,
)

from ...core import backends as core_backends
from ...core import (
    friendly,
    gamefacts,
    operation_policy,
    process,
)
from ...core.backends import (
    Status,
)
from ...core.config import Config
from ...core.events import Event
from ...core.exitcode import STOPPED_EXIT_CODES
from ...core.news import NewsResult
from ...core.verification import VerifyResult
from .. import clickguard, notify
from ..game_view import OPERATION_TITLES, can_pause_kind
from ..operation_presentation import REPLAY_EVENTS
from ..terms_dialog import TermsDialog
from ..workers import CAPPED, QualityCleanupResult

_REPLAY_EVENTS = REPLAY_EVENTS

log = logging.getLogger("nadare.gui.main_window")

#: Background refreshes that must never put the page into the busy state.
SILENT_OPERATIONS = operation_policy.operations_with("background")


def _unknown_backend_message(value: str) -> str:
    """Plain explanation for an unknown/empty ``backend`` value (#366)."""
    return (
        f"This game's type ('{value}') is not recognised by this version of Nadare. "
        "Remove the game and add it again, or correct 'backend' in config.json."
    )


def _status_error_text(kind: str, message: str) -> str:
    """What a failed status check shows: the backend's own words, or a plain "unknown type".

    A retired kind (a launcher version Nadare no longer offers) keeps the backend's message,
    which says what to do and where the old files are; the generic text would say "add it
    again" (seen in the b23-rc1 click-through).
    """
    if core_backends.retired_replacement(kind) is not None:
        return message
    if not kind or not core_backends.is_available(kind):
        # A raw "unknown backend: ..." is no help to someone who has to fix (or re-add) the
        # game (#366).
        return _unknown_backend_message(kind)
    return message

#: Busy operations that cannot stop mid-flight, so their button must not offer CANCEL.
UNCANCELLABLE_OPERATIONS = frozenset(operation_policy.POLICIES) - operation_policy.operations_with(
    "cancellable"
)

#: Failures after which the install state may have changed: probe it again.
REPROBE_OPERATIONS = operation_policy.operations_with("reprobe")

#: Side-status wording for operations whose name reads badly as-is.
#: What each operation's result is (#1458); an operation not listed has no result.
_RESULT_TYPES: dict[str, type | tuple[type, ...]] = {
    "status": Status,
    "verify": (VerifyResult, dict),
    "news": NewsResult,
    "media": dict,
    "remove_qualities": QualityCleanupResult,
}

_OPERATION_LABELS = {"set_remember": "Saving", "remove_qualities": "Removing old quality files"}

#: The Library card's badge while one of these runs; the status probe keeps saying "not installed"
#: until an install ends (user sim r2 item 5). Other operations keep the probe's state.
LIBRARY_BUSY_STATES = {
    "install": "installing",
    "update": "updating",
    "repair": "repairing",
    "remake_prefix": "repairing",
    "verify": "verifying",
}


#: The game page's rule (``busy._LAUNCH_EXIT_GRACE_S``): a game that ran this long, or showed
#: its window, was really played. A non-zero exit after it is a game that closed (Wine games
#: often exit non-zero on quit), and no later failure undoes its play stamp.
LAUNCH_EXIT_GRACE_SECONDS = 30.0


class OperationsMixin:
    """Mixin for :class:`MainWindow`; see the module text."""

    def _show_launch_sheet(self, profile: str) -> None:
        from ..launch_sheet import LaunchSheet

        previous = self._launch_sheet
        if previous is not None:
            previous.finish()
            previous.deleteLater()
        sheet = LaunchSheet(self._game_title(profile), self)
        self._launch_sheet = sheet
        self._launch_sheet_profile = profile
        sheet.stopped.connect(lambda: self._start(profile, "stop"))
        sheet.finished.connect(lambda: self._forget_launch_sheet(sheet))

    def _forget_launch_sheet(self, sheet) -> None:
        """The sheet closed itself: it is no longer the live one."""
        if self._launch_sheet is sheet:
            self._launch_sheet = None
            self._launch_sheet_profile = None

    def _finish_launch_sheet(self, profile: str) -> None:
        if self._launch_sheet_profile == profile and self._launch_sheet is not None:
            self._launch_sheet.finish()

    def _on_busy(self, profile: str, operation: str, active: bool) -> None:
        if operation == "launch" and not active:
            self._finish_launch_sheet(profile)
        if operation in SILENT_OPERATIONS:
            # Background status/news/media refreshes never mark the game busy:
            # no CANCEL, no "CHECKING…" and no console expansion.
            return
        self._operations.busy_changed(profile, operation, active)
        if operation in LIBRARY_BUSY_STATES:
            self.library.set_busy(profile, LIBRARY_BUSY_STATES[operation] if active else None)
        if operation == "launch" and active:
            snap = self._snapshot(profile)
            snap.pop("launch_previous_played", None)
            snap.pop("launch_running_at", None)
            snap.pop("launch_failed", None)
        if active:
            self._download_progress.pop(profile, None)
            self._download_eta.pop(profile, None)
            self._reset_download_speed(profile)
        else:
            self._end_runner_downloads(profile)
            self._apply_pending_remember(profile)
            if profile == self._current_profile:
                self._sync_patch(profile)  # apply, remove, update and repair change the files
            if operation == "launch":
                self.controller.launch_ended(profile)
            self._maybe_finish_background_close()
        if profile == self._current_profile and active:
            self.game.set_busy(self._operations.view(profile).busy)
            label = _OPERATION_LABELS.get(operation, operation.replace("_", " ").capitalize())
            self._set_side_status(f"{label} · {self._game_title(profile)}")
            if operation in UNCANCELLABLE_OPERATIONS:
                # Verification cannot be stopped safely, so do not promise Cancel.
                self.game.primary.setText(f"{label.upper()}…")
                self.game.primary.setToolTip(
                    "This check cannot be cancelled. Wait for it to finish."
                )
                self.game.primary.setEnabled(False)

    def _on_queued(self, profile: str, operation: str) -> None:
        """The game's install/update/repair waits for a free slot (#770)."""
        if self._closing:
            return
        self._operations.queued(profile, operation)
        if operation in LIBRARY_BUSY_STATES:
            self.library.set_busy(profile, "waiting")
        if profile == self._current_profile:
            self._show_waiting(profile, operation)
            self._set_side_status(f"Waiting · {self._game_title(profile)}")

    def _can_pause_profile(self, profile: str) -> bool:
        return can_pause_kind(str((self.config.profiles.get(profile) or {}).get("backend") or ""))

    def _show_waiting(self, profile: str, operation: str) -> None:
        order = self.controller.queue_order()
        ahead = order.index(profile) if profile in order else 0
        can_now = self.controller.can_start_now(profile, self._can_pause_profile)
        self.game.set_waiting(operation, ahead, can_now)

    def _refresh_queue_view(self) -> None:
        """The waiting line changed: the open game's place and buttons may have too."""
        profile = self._current_profile
        if not profile or self._closing or not self.controller.is_waiting(profile):
            return
        order = self.controller.queue_order()
        self.game.set_queue_position(
            order.index(profile), self.controller.can_start_now(profile, self._can_pause_profile)
        )

    def _on_queue_next(self) -> None:
        if self._current_profile:
            self.controller.move_to_front(self._current_profile)

    def _on_queue_now(self) -> None:
        if self._current_profile:
            self.controller.start_now(self._current_profile, self._can_pause_profile)

    def _on_cancel(self) -> None:
        if self._current_profile:
            self._cancel_with_choice(self._current_profile)

    def _cancel_with_choice(self, profile: str) -> None:
        """Stop ``profile``'s job; a download first says its files stay, and can be removed (#991).

        Only installs and updates ask (they keep what they downloaded); a first install also
        offers to remove the game, which goes through the Remove dialog and its own checks.
        """
        # The user's own download wins over a background probe next to it: a paused or
        # waiting install/update is the operation Cancel on its row is about.
        operation = (
            self.controller.paused_operation(profile)
            or self.controller.waiting_operation(profile)
            or self.controller.running_operation(profile)
        )
        if operation in ("install", "update"):
            choice = self._ask_cancel_choice(self._game_title(profile), operation == "install")
            if choice == "continue":
                return
            if choice == "remove":
                self._remove_after_cancel.add(profile)
        self.controller.cancel(profile)

    def _ask_cancel_choice(self, title: str, first_install: bool) -> str:
        """``"keep"`` (stop, keep the files), ``"remove"`` (stop, then Remove) or ``"continue"``."""
        box = QMessageBox(self)
        box.setIcon(QMessageBox.Icon.Question)
        box.setWindowTitle("Stop download")
        box.setText(f"Stop downloading {title}?")
        more = (
            " Or remove the game from Nadare, and choose there whether to delete what was "
            "downloaded."
            if first_install
            else ""
        )
        box.setInformativeText(
            "What is already downloaded stays on disk, and the next Install or Update "
            "carries on from there." + more
        )
        keep = box.addButton("Stop, keep files", QMessageBox.ButtonRole.AcceptRole)
        remove = (
            box.addButton("Stop and remove the game…", QMessageBox.ButtonRole.DestructiveRole)
            if first_install
            else None
        )
        carry_on = box.addButton("Keep downloading", QMessageBox.ButtonRole.RejectRole)
        box.setDefaultButton(carry_on)
        box.setEscapeButton(carry_on)
        # CANCEL opens this under the pointer: a double-click's second click must not answer it.
        clickguard.hold_clicks(box)
        try:
            box.exec()
            clicked = box.clickedButton()
        finally:
            box.deleteLater()
        if clicked is keep:
            return "keep"
        if remove is not None and clicked is remove:
            return "remove"
        return "continue"

    def _on_skip_step(self) -> None:
        if self._current_profile:
            self.controller.skip_step(self._current_profile)

    def _on_pause(self) -> None:
        if self._current_profile:
            self.controller.pause(self._current_profile)

    def _on_resume(self) -> None:
        if self._current_profile:
            self.controller.resume(self._current_profile)

    def _on_paused(self, profile: str, operation: str) -> None:
        if self._closing:
            return
        self._operations.paused(profile, operation)
        if profile == self._current_profile:
            self.game.set_paused(self._operations.view(profile).paused)

    # ---- controller fan-out -------------------------------------------------
    def _on_event(self, profile: str, event: Event) -> None:
        if (
            event.kind == "error"
            and self._busy.get(profile) == "launch"
            and not self._stale_worker_result(profile, "launch")
        ):
            self._failed_launch(profile, event.message)
        if event.kind == "window_seen":
            # Internal launch feedback, not a backend line for the game console.
            if not self._stale_worker_result(profile, "launch"):
                self._finish_launch_sheet(profile)
                # The game showed its window: this play counts whatever the exit code.
                self._snapshot(profile).pop("launch_previous_played", None)
            return
        if (
            self._launch_sheet_profile == profile
            and self._launch_sheet is not None
            and not self._stale_worker_result(profile, "launch")
        ):
            self._launch_sheet.progress(event)
        data = event.data or {}
        if (
            event.kind == "game_running"
            and self._busy.get(profile) == "launch"
            and not self._snapshot(profile).get("launch_failed")
            and not self._stale_worker_result(profile, "launch")
        ):
            previous = self._snapshot(profile).get("status")
            self._on_result(
                profile,
                "status",
                Status(
                    "running",
                    version=getattr(previous, "version", None),
                    running=True,
                    data=dict(getattr(previous, "data", None) or {}),
                ),
            )
        runner_event = self._on_runner_download_event(profile, event)
        if not runner_event and self.controller.running_operation(profile) in CAPPED:
            self._track_download_progress(profile, event)
        if event.kind == "step" and data.get("png"):
            self._snapshot(profile)["qr"] = data["png"]
        elif event.kind in ("done", "error") or data.get("qr_state") == "expired":
            # The QR is no longer live; do not resurrect it on a later revisit.
            self._snapshot(profile).pop("qr", None)
        if (
            event.kind == "status"
            and event.message
            and (
                profile in self._remember_watching
                or data.get("remember_unavailable")
                or str(event.message).startswith("Remember me unavailable")
            )
        ):
            self._remember_notice[profile] = event.message
        self._operations.event(profile, event)
        if profile == self._current_profile:
            self.game.append_event(event)

    def _stale_worker_result(self, profile: str, operation: str | None = None) -> bool:
        """Whether a result belongs to a worker started before this profile changed."""
        generation = self._profile_generation(profile)
        if operation is not None:
            token = self._operation_generations.get((profile, operation))
            if token is not None:
                return token != generation
        return self._worker_generations.get(profile, generation) != generation

    def _on_result(self, profile: str, operation: str, result: Any) -> None:
        """A worker's result, routed by the operation that produced it (#1458).

        The payload must have the shape that operation returns; anything else (an unknown
        operation, or a payload of the wrong kind) is logged and dropped, never guessed.
        """
        if self._stale_worker_result(profile, operation):
            return  # the profile was removed/re-added after this worker started
        expected = _RESULT_TYPES.get(operation)
        if expected is None or not isinstance(result, expected):
            log.warning(
                "dropped a %s result of type %s for %r",
                operation, type(result).__name__, profile,
            )
            return
        if operation == "remove_qualities":
            self._quality_cleanup_finished(profile, result)
            return
        if operation == "news":
            hero = getattr(result, "hero", None)
            if hero:
                self._record_official_art(profile, hero)
            snap = self._snapshot(profile)
            snap["news"] = result
            snap["news_at"] = time.monotonic()
            if profile == self._current_profile:
                self.game.set_news(result)
        elif operation == "media":
            hero = result.get("hero")
            if hero:
                self._record_official_art(profile, hero)
            snap = self._snapshot(profile)
            snap["media"] = result
            snap["media_at"] = time.monotonic()
            if profile == self._current_profile:
                self.game.set_media(result.get("hero"), result.get("banners") or [])
        elif operation == "status":
            probe = result
            failure = self._verify_failures.get(profile)
            if failure is not None and probe.state != "error":
                # A failed file check outranks later status probes until Repair or Verify pass.
                result = Status(
                    state="error",
                    version=probe.version,
                    running=False,
                    message=failure,
                    data={**(probe.data or {}), "verify_failed": True},
                )
            snap = self._snapshot(profile)
            self._record_played(profile, snap.get("status"), result)
            snap["status"] = result
            snap["status_at"] = time.monotonic()
            if probe.state == "error":
                self._status_errors[profile] = self._status_errors.get(profile, 0) + 1
            else:
                self._status_errors.pop(profile, None)
            self.library.set_state(profile, result)
            self.sidebar_games.set_status(profile, result)
            self._maybe_offer_spare_copies(profile, result)
            if profile == self._current_profile:
                self.game.set_busy_elsewhere(self._busy_elsewhere(profile))
                self.game.set_status(result)
                title = self._game_title(profile)
                if gamefacts.is_running(result):
                    self._set_side_status(f"Playing {title}")
                else:
                    self._set_side_status(
                        f"{result.state.replace('_', ' ').title()} · {title}"
                    )
        elif operation == "verify":
            verified = VerifyResult.from_report(result)
            msg = verified.message
            if verified.outcome == "passed":
                self._verify_failures.pop(profile, None)
                QMessageBox.information(
                    self, "Check game files", str(msg or "All checks passed")
                )
            elif verified.outcome == "failed":
                message = str(msg or "File check failed")
                self._verify_failures[profile] = message
                QMessageBox.warning(self, "File check failed", message)
                self._show_verify_failure(profile, message)
            elif verified.outcome == "nothing_to_verify":
                # Nothing was checked, so nothing failed: do not outrank a later probe.
                self._verify_failures.pop(profile, None)
                QMessageBox.information(
                    self, "Check game files", str(msg or "nothing to verify")
                )
            else:
                # "not installed" is not a file-check failure; the re-probe shows INSTALL.
                self._verify_failures.pop(profile, None)

    def _show_verify_failure(self, profile: str, message: str) -> None:
        """Put a failed file check on the page at once, before its re-probe lands."""
        snap = self._snapshot(profile)
        previous = snap.get("status")
        status = Status(
            state="error",
            version=getattr(previous, "version", None),
            running=False,
            message=message,
            data={**(getattr(previous, "data", None) or {}), "verify_failed": True},
        )
        snap["status"] = status
        snap["status_at"] = time.monotonic()
        self.library.set_state(profile, status)
        self.sidebar_games.set_status(profile, status)
        if profile == self._current_profile:
            self.game.set_status(status)
            self._set_side_status(f"File check failed · {self._game_title(profile)}")

    def _busy_elsewhere(self, profile: str) -> bool:
        """Whether another process holds ``profile``'s operation lock (r3b05/r3b07 F2).

        This window's own jobs hold the same lock: a worker of ours, or our predownload.
        """
        if self._busy.get(profile) not in (None, "status") or self.predownloads.is_busy(profile):
            return False
        return process.operation_in_progress(profile)

    def _record_played(self, profile: str, previous: Status | None, status: Status) -> None:
        """Record running transitions, including external games; undo failed launches.

        A process is provisional evidence: keep its old stamp until the launch ends,
        so even a failure after a window appeared can restore the previous history.
        """
        snap = self._snapshot(profile)
        if snap.get("launch_failed"):
            if not gamefacts.is_running(status):
                snap.pop("launch_failed", None)
            return  # A surviving process is still shown, but is not a new play.
        if gamefacts.is_running(status) and not gamefacts.is_running(previous):
            if self._busy.get(profile) == "launch":
                data = self.config.profiles.get(profile, {})
                if "launch_previous_played" not in snap:
                    had = "last_played" in data
                    snap["launch_previous_played"] = (had, data.get("last_played"))
                    snap["launch_running_at"] = time.monotonic()
            if self.config.mark_played(profile):
                with self.config.locked():
                    self.config.save()
                self.library.refresh(self.config.profiles)

    def _launch_exit_failed(self, profile: str, exit_code: int) -> bool:
        """A non-zero exit soon after the start; a stop's signal code or a late exit is a quit."""
        if exit_code == 0 or exit_code in STOPPED_EXIT_CODES:
            return False
        running_at = self._snapshot(profile).get("launch_running_at")
        return running_at is None or time.monotonic() - running_at < LAUNCH_EXIT_GRACE_SECONDS

    def _failed_launch(self, profile: str, message: str) -> None:
        """Remove provisional play history and stale running UI before showing failure."""
        snap = self._snapshot(profile)
        snap["launch_failed"] = True
        previous = snap.pop("launch_previous_played", None)
        running_at = snap.pop("launch_running_at", None)
        if running_at is not None and time.monotonic() - running_at >= LAUNCH_EXIT_GRACE_SECONDS:
            previous = None  # it ran long enough to be a real play
        data = self.config.profiles.get(profile)
        if previous is not None and data is not None:
            present, value = previous
            with self.config.locked():
                if present:
                    data["last_played"] = value
                else:
                    data.pop("last_played", None)
                try:
                    self.config.save()
                except OSError:
                    log.warning("could not save restored play history", exc_info=True)
            self.library.refresh(self.config.profiles)
        self._finish_launch_sheet(profile)
        self._mark_status_error(profile, message)
        if profile == self._current_profile:
            self.game.step_label.setText("")

    def _record_official_art(self, profile: str, path: str) -> None:
        """Persist publisher-art provenance on the GUI thread.

        Workers never touch the live ``config.data`` (that would race a
        concurrent ``Config.save()``); they return the hero path instead and the
        window records it here.
        """
        data = self.config.profiles.get(profile)
        if data is None:
            return
        from ...core import artwork

        with self.config.locked():
            if artwork.record_official(data, path, "hero"):
                try:
                    self.config.save()
                except OSError:
                    log.warning("could not save official-art provenance", exc_info=True)

    def _on_succeeded(self, profile: str, operation: str, exit_code: int) -> None:
        if operation == "launch":
            self._finish_launch_sheet(profile)
        if self._closing or self._stale_worker_result(profile, operation):
            return
        if operation == "launch":
            if self._launch_exit_failed(profile, exit_code):
                self._failed_launch(profile, f"Launching did not finish (exit code {exit_code}).")
            else:
                self._snapshot(profile).pop("launch_previous_played", None)
                self._snapshot(profile).pop("launch_running_at", None)
        self._operations.complete(profile, operation, "succeeded")
        if operation in LIBRARY_BUSY_STATES:
            self.library.set_busy(profile, None)  # a queued job that never ran has no busy(False)
        if operation not in SILENT_OPERATIONS:
            self._remove_after_cancel.discard(profile)  # it finished before the stop landed
        if operation == "status":
            self._probe_finished(profile)
        if operation == "news":
            # One operation per profile: media runs after news, never alongside.
            self._maybe_start_media(profile)
            return
        if operation == "media":
            return
        if operation == "check_update":
            # The cached published version may now differ: refresh the badge. Until it lands
            # the page offers no Play, which would start an install the check just found
            # outdated (Strinova, 2026-11-27).
            if self._start(profile, "status", silent=True) and profile == self._current_profile:
                self.game.await_status()
            return
        if operation in ("repair", "install", "update"):
            # Fresh files supersede the failed check (Repair, or an install/update rewrote them).
            self._verify_failures.pop(profile, None)
        # A background check that ends late (one detached by a pause, say) must not clear the
        # page of an operation that is running now (#895).
        late_check = operation in SILENT_OPERATIONS and profile in self._busy
        if profile == self._current_profile and not late_check:
            self.game.finish(operation, exit_code)
        if operation == "status":
            self._after_status(profile)
        else:
            self._start(profile, "status", silent=True)
        if operation == "set_remember" or profile in self._remember_notice:
            # The login/launch path can discover that Remember me cannot save
            # too; do not leave the box ticked with no explanation.
            self._show_remember_notice(profile)
        if operation in ("install", "update", "launch"):
            self._maybe_offer_runner_cleanup()
        if operation in ("install", "update"):
            self._maybe_offer_quality_cleanup(profile)
        self._notify_finished(profile, operation, True)
        if operation == "launch":
            self.controller.launch_ended(profile)
            self._maybe_finish_background_close()

    def _notify_finished(self, profile: str, operation: str, ok: bool) -> None:
        """Tell the desktop that a long operation ended, when nobody is looking at the window."""
        if not self.config.data.get("notify_when_done", True) or self.isActiveWindow():
            return
        text = notify.finished_text(operation, self._game_title(profile), ok)
        if text is not None:
            notify.send(*text)

    def _mark_status_error(self, profile: str, message: str) -> None:
        """Show a failed check (and keep it in the snapshot) until a retry works."""
        kind = self._backend_kind(profile)
        message = _status_error_text(kind, message)
        retired = core_backends.retired_replacement(kind) is not None
        status = Status(state="error", message=message, data={"retired": True} if retired else {})
        self._status_errors[profile] = self._status_errors.get(profile, 0) + 1
        snap = self._snapshot(profile)
        snap["status"] = status
        snap["status_at"] = time.monotonic()
        self.library.set_state(profile, status)
        self.sidebar_games.set_status(profile, status)
        if profile == self._current_profile:
            self.game.set_status(status)
            self._set_side_status(f"Error · {self._game_title(profile)}")

    def _on_failed(self, profile: str, operation: str, message: str) -> None:
        if operation == "launch":
            self._finish_launch_sheet(profile)
        if self._closing or self._stale_worker_result(profile, operation):
            return
        if operation == "launch":
            self._failed_launch(profile, message)
        self._operations.complete(profile, operation, "failed", message=message)
        if operation in LIBRARY_BUSY_STATES:
            self.library.set_busy(profile, None)  # a queued job that never ran has no busy(False)
        if operation not in SILENT_OPERATIONS:
            self._remove_after_cancel.discard(profile)
        if operation == "status":
            self._probe_finished(profile)
        if operation in ("news", "media", "check_update"):
            # News/media/update checks are best-effort: never a dialog, just the
            # muted UI line.
            if operation == "news" and profile == self._current_profile:
                self.game.set_news(NewsResult(items=[], available=False))
            if operation == "news":
                self._maybe_start_media(profile)
            if operation == "check_update":
                # A failed check is asked again next time, not after six hours (final round).
                self._update_checked.pop(profile, None)
                # Keep the page's normal refresh going even when offline.
                self._start(profile, "status", silent=True)
            return
        if operation == "set_remember":
            self._remember_watching.discard(profile)
            self._remember_notice.pop(profile, None)
        if operation == "status":
            # A status probe is background noise; show it as a badge, never a dialog.
            self._mark_status_error(profile, message)  # its error status ends any wait too
            return
        terms = self._is_terms_failure(profile, operation, message)
        if profile == self._current_profile:
            # The terms dialog below says what to do; a Retry box first only fails again.
            self.game.fail(self._operations.view(profile).failure or message, quiet=terms)
        elif not terms:
            # Not the page on screen: say which game this is about.
            operation_title = OPERATION_TITLES.get(operation, operation.capitalize())
            QMessageBox.warning(
                self,
                f"{self._game_title(profile)}: {operation_title}",
                friendly.split(message)[0],  # the raw details stay in the game's OUTPUT
            )
        if profile in self._remember_notice:
            self._show_remember_notice(profile)
        self._notify_finished(profile, operation, False)
        if operation in REPROBE_OPERATIONS:
            # The failure may mean the files changed underneath us; never leave
            # the last READY state (and an enabled LAUNCH) on the page.
            self._mark_status_error(profile, message)
            self._start(profile, "status", silent=True)
        if operation == "launch":
            self.controller.launch_ended(profile)
            self._maybe_finish_background_close()
        if terms:
            self._offer_terms(profile, operation)

    def _is_terms_failure(self, profile: str, operation: str, message: str) -> bool:
        """True when a login/launch stopped only because the game's terms are not accepted."""
        if operation not in ("login", "launch"):
            return False
        kind = str(self.config.profiles.get(profile, {}).get("backend") or "")
        try:
            expected = getattr(core_backends.get(kind), "TERMS_MESSAGE", None)
        except Exception:  # noqa: BLE001 - an unknown backend has no terms flow
            return False
        return bool(expected) and message == expected

    def _offer_terms(self, profile: str, operation: str) -> None:
        """Show the terms and let the user agree (nothing is sent before they press Agree)."""
        if self._closing:
            return
        if self._terms_dialog is not None:
            # Another game's terms are open: show these next instead of dropping them (#799).
            if (profile, operation) not in self._terms_waiting:
                self._terms_waiting.append((profile, operation))
            return
        kind = str(self.config.profiles.get(profile, {}).get("backend") or "")
        backend_class = core_backends.get(kind)

        def worker_config() -> Config:
            # The worker threads must not read the live Config (a Settings save could resize
            # it), and each call takes the settings as they are now: "Remember me" turned off
            # while the dialog was open must hold for the agreement (#799).
            with self.config.locked():
                return Config(path=self.config.path, data=copy.deepcopy(self.config.data))

        shown: dict[str, object] = {}

        def terms() -> dict:
            result = core_backends.create(profile, worker_config()).terms()
            shown["member"] = result.get("member_no")  # the account these terms were read for
            return result

        def accept() -> None:
            member = shown.get("member")
            core_backends.create(profile, worker_config()).accept_terms(
                str(member) if member else None
            )

        dialog = TermsDialog(
            self._game_title(profile), terms, accept, backend_class.TERMS_URL, parent=self
        )
        dialog.agreed.connect(lambda: self._on_terms_agreed(profile, operation))
        dialog.finished.connect(lambda _result: self._on_terms_closed())
        self._terms_dialog = dialog
        dialog.open()
        dialog.start()

    def _on_terms_closed(self) -> None:
        self._terms_dialog = None
        while self._terms_waiting and not self._closing:
            profile, operation = self._terms_waiting.pop(0)
            if profile in self.config.profiles:
                self._offer_terms(profile, operation)
                return

    def _on_terms_agreed(self, profile: str, operation: str) -> None:
        if self._closing:
            return
        title = self._game_title(profile)
        # A login that stopped at the terms is a valid login: only Launch is left to press.
        self._set_side_status(
            f"Terms accepted for {title}. Press Launch again."
            if operation == "launch"
            else f"Terms accepted for {title}. You are logged in: press Launch."
        )
        self._start(profile, "status", silent=True)

    def _on_cancelled(self, profile: str, operation: str) -> None:
        if operation == "launch":
            self._finish_launch_sheet(profile)
        if self._closing or self._stale_worker_result(profile, operation):
            return
        self._operations.complete(profile, operation, "cancelled")
        if operation in LIBRARY_BUSY_STATES:
            self.library.set_busy(profile, None)  # a queued job that never ran has no busy(False)
        if operation == "status":
            self._probe_finished(profile)
            if profile == self._current_profile:
                self.game.stop_awaiting_status()
        if operation == "set_remember":
            self._remember_watching.discard(profile)
            self._remember_notice.pop(profile, None)
        if operation == "launch":
            self.controller.launch_ended(profile)
            self._maybe_finish_background_close()
        if operation == "install" and profile in self._remove_after_cancel:
            self._remove_after_cancel.discard(profile)
            # Pre-tick only what the dialog itself allows deleting; it still asks again.
            self._on_remove_requested(profile, preselect=frozenset({"install_dir", "prefix"}))
            if self._busy.get(profile) == "remove":
                # The Remove dialog started a background removal: the page must stay busy,
                # not drop to an Install button while the worker deletes the files.
                if profile == self._current_profile:
                    self.game.set_busy("remove")
                return
        if operation in SILENT_OPERATIONS or profile != self._current_profile:
            return
        self.game.cancelled()
