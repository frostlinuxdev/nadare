# SPDX-License-Identifier: GPL-3.0-or-later
# Copyright (C) 2026 Nadare contributors
"""What the game page shows while an operation runs, pauses, ends or fails.

Split out of :mod:`nadare.gui.game_view`; the code is unchanged.
"""

from __future__ import annotations

import time

from ...core.eta import format_eta, format_speed
from ...core.events import Event
from ...core.exitcode import STOPPED_EXIT_CODES, crash_signal, play_time

#: User-facing operation names used in dialogs (never the raw operation id).
OPERATION_TITLES = {
    "install": "Installing",
    "update": "Updating",
    "repair": "Repairing",
    "remake_prefix": "Rebuilding the Wine prefix",
    "launch": "Launching",
    "verify": "Checking game files",
    "login": "Logging in",
    "status": "Checking",
    "remove": "Removing",
}


def operation_title(operation: str) -> str:
    """Friendly title for ``operation``; unknown ids degrade readably."""
    return OPERATION_TITLES.get(operation, operation.capitalize())

#: Busy operation -> pill state shown while it runs.
_BUSY_STATES = {
    "install": "installing",
    "update": "updating",
    "repair": "repairing",
    "remake_prefix": "rebuilding",
    "launch": "launching",
    "login": "login",
    "verify": "verifying",
    "stop": "stopping",
    "uninstall": "uninstalling",
    "remove": "uninstalling",
    "set_remember": "saving",
    "logout": "logging out",
}

#: A second click right after a button turns into CANCEL must not cancel the new job.
_CANCEL_GRACE_S = 0.5

#: A game that exits non-zero after at least this long had a real session; a quicker
#: non-zero exit is a failed launch. Stop-requested signal codes are always normal.
_LAUNCH_EXIT_GRACE_S = 30.0

_PAUSED_TEXT = "Paused. Everything downloaded so far is kept."

#: Operations whose success makes the status shown before them out of date.
_STATE_CHANGING = frozenset({"install", "update", "repair", "remake_prefix", "uninstall"})

#: Operations whose success is said on the progress line afterwards: OUTPUT is folded (#993),
#: so its "done" line alone would leave no sign that an install or update worked.
_ANNOUNCED_ON_SUCCESS = frozenset({"install", "update", "repair", "verify", "remake_prefix"})

#: A backend's own last line for a launch (CZN); the page then adds no second one.
_GAME_END_LINES = frozenset({"game exited normally", "game stopped"})

#: Operations the failure dialog and the RETRY button can run again as they were.
_RETRYABLE = frozenset(
    {"install", "update", "repair", "remake_prefix", "verify", "login", "launch"}
)


def can_pause_kind(kind: str) -> bool:
    """Whether installs of backend ``kind`` resume from what is already on disk."""
    from ...core.directkinds import DIRECT_KINDS

    return kind in DIRECT_KINDS or kind == "stellasora-yostar"


class BusyMixin:
    """Mixin for :class:`GameView`; see the module text."""

    # ---- lifecycle ---------------------------------------------------------
    def set_busy(self, operation: str) -> None:
        """Mark ``operation`` as running and start the elapsed clock."""
        resumed = self._paused_op is not None
        self._paused_op = None
        self._failed_op = None
        self._last_done = ""
        self._queue_waiting = False
        self._busy_op = operation
        if operation == "launch":
            # What this launch has is fixed now; the switch can change later, the process cannot.
            # Only a guess until the backend reports: never one the machine cannot keep.
            self._sandbox_active = bool(
                self._sandbox_supported
                and self.sandbox.isChecked()
                and self._sandbox_ready
                and not self._sandbox_blocked
            )
        self._primary_grace_until = time.monotonic() + _CANCEL_GRACE_S
        self.news_frame.setVisible(False)
        self._started_at = time.time()
        self._last_step_at = 0.0
        # Remove sends no progress events: show the busy animation, not a dead 0%.
        self.progress.setRange(0, 0 if operation == "remove" else 100)
        self.progress.setVisible(operation != "launch")
        self.progress.setValue(0)
        self.step_label.setText(
            "Playing"
            if operation == "launch"
            else "Removing…"
            if operation == "remove"
            else "Resuming: checking what is already downloaded…"
            if resumed
            else operation.capitalize()
        )
        if resumed:
            self._append_line("resumed", "status")
        self.elapsed_label.setText("·  0:00")
        self._set_badge(
            "running" if operation == "launch" else _BUSY_STATES.get(operation, "installing"), ""
        )
        self._hide_qr()
        self._timer.start()
        self._set_console_expanded(self._console_preference)
        self._refresh_actions()

    def set_waiting(self, operation: str, ahead: int = 0, can_now: bool = False) -> None:
        """Show that ``operation`` waits for another install to finish (#770).

        ``ahead`` is how many waiting installs come before this one; ``can_now`` whether the
        running one can be paused so this one starts at once.
        """
        self.set_busy(operation)
        self._timer.stop()
        self.elapsed_label.setText("")
        self.progress.setRange(0, 0)  # the busy animation: nothing is downloading yet
        self._queue_waiting = True
        self.set_queue_position(ahead, can_now)
        self._append_line("waiting for another install to finish", "status")

    def set_queue_position(self, ahead: int, can_now: bool) -> None:
        """Update the waiting message and which queue buttons make sense."""
        if ahead > 0:
            noun = "install" if ahead == 1 else "installs"
            self.step_label.setText(f"Waiting · {ahead} {noun} ahead of this one")
        else:
            self.step_label.setText("Waiting for another install to finish…")
        self.queue_next_btn.setVisible(ahead > 0)
        self.queue_now_btn.setVisible(can_now)
        self._refresh_actions()

    def _on_skip_clicked(self) -> None:
        # Hide first: a second click must not skip whatever step comes next.
        self.skip_button.setHidden(True)
        self.skip_step_requested.emit()

    def _clear_busy_progress(self) -> None:
        """Return the progress area to its idle shape when an operation ends."""
        self._queue_waiting = False
        self._sandbox_active = None
        self._apply_sandbox()
        self.skip_button.setHidden(True)
        self.progress.setRange(0, 100)  # never leave the busy animation running
        self.progress.setValue(0)
        self.progress.setVisible(False)
        self._eta.reset()
        self._speed.reset()
        self.progress.setFormat("%p%")
        self.step_label.setText("")
        self.elapsed_label.setText("")
        self._started_at = 0.0

    def finish(self, operation: str, exit_code: int) -> None:
        """Stop the clock and report the result."""
        from ..workers import BACKGROUND

        # Snapshot what ended before clearing the busy state: whether STOP was
        # requested while the game ran, and how long it ran for.
        stop_requested = self._busy_op == "stop"
        retry_op = operation if operation in _RETRYABLE else None
        # Preparation (runner download, prefix setup) is not play time: count from the
        # last phase step, which a launch emits right before it starts the game.
        since = self._last_step_at or self._started_at
        elapsed = time.time() - since if since else 0.0
        # A background refresh ending must not drop a pause on this page.
        if operation not in BACKGROUND:
            self._paused_op = None
        self._busy_op = None
        self._clear_busy_progress()
        self.news_frame.setVisible(self._news_shown)
        self._timer.stop()
        self._hide_qr()
        self._tick()
        self._awaiting_status = exit_code == 0 and operation in _STATE_CHANGING
        self._restore_badge()
        self._refresh_actions()
        if operation in BACKGROUND and self._paused_op is not None:
            # The pause survives a background refresh, and so does its message (#1207 b160).
            self.step_label.setText(_PAUSED_TEXT)
        if operation == "launch":
            if exit_code == 0:
                if self._last_done.lower() not in _GAME_END_LINES:  # CZN says it itself
                    self._append_line("game exited normally", "done")
                return
            crash = "" if stop_requested else crash_signal(exit_code)
            if crash and elapsed >= _LAUNCH_EXIT_GRACE_S:
                # A real session that ended in a crash signal is not a normal quit (#1031).
                self.show_error(
                    "The game crashed",
                    f"The game crashed ({crash}, exit code {exit_code}) after "
                    f"{play_time(elapsed)}. Open the logs for details.",
                    retry_op=retry_op,
                    failed=True,
                )
                return
            # Many Wine/Proton games and launchers exit non-zero on quit, and
            # STOP ends them with a signal code. Neither is a failed launch.
            if (
                stop_requested
                or exit_code in STOPPED_EXIT_CODES
                or elapsed >= _LAUNCH_EXIT_GRACE_S
            ):
                self._append_line(f"Game closed (exit code {exit_code})", "status")
                return
        if exit_code == 0 and operation in _ANNOUNCED_ON_SUCCESS:
            done = self._last_done or f"{operation_title(operation)} finished"
            self.step_label.setText(f"✓ {done}")  # cleared by the next operation
        if exit_code != 0:
            self.show_error(
                "Operation failed",
                f"{operation_title(operation)} did not finish (exit code {exit_code}). "
                "Open the logs for details.",
                retry_op=retry_op,
                failed=True,
            )

    def fail(self, message: str, *, quiet: bool = False) -> None:
        """End the busy state after a failure; ``quiet`` writes it to OUTPUT without a box.

        Quiet is for a failure another dialog explains next (the game's terms).
        """
        retry_op = self._busy_op if self._busy_op in _RETRYABLE else None
        self._paused_op = None
        self._busy_op = None
        self._clear_busy_progress()
        self.news_frame.setVisible(self._news_shown)
        self._timer.stop()
        self._hide_qr()
        self._restore_badge()
        self._refresh_actions()
        if quiet:
            self._append_line(message, "error")
            return
        self.show_error("Nadare could not finish", message, retry_op=retry_op, failed=True)

    def cancelled(self) -> None:
        self._paused_op = None
        self._busy_op = None
        self._clear_busy_progress()
        self.news_frame.setVisible(self._news_shown)
        self._timer.stop()
        self._hide_qr()
        self._restore_badge()
        self._refresh_actions()
        self._append_line("cancelled", "status")

    def is_paused(self) -> bool:
        return self._paused_op is not None

    def _can_pause(self) -> bool:
        """Pause only where the downloads really resume from what is already on disk."""
        from ..workers import PAUSABLE

        return self._busy_op in PAUSABLE and can_pause_kind(
            str(self._profile_data.get("backend") or "")
        )

    def set_paused(self, operation: str) -> None:
        """Show that ``operation`` is paused, with RESUME ready on the primary button."""
        self._busy_op = None
        self._clear_busy_progress()
        self.news_frame.setVisible(self._news_shown)
        self._timer.stop()
        self._hide_qr()
        self._restore_badge()
        self._paused_op = operation
        self.step_label.setText(_PAUSED_TEXT)
        self._refresh_actions()
        self._append_line("paused", "status")

    def _restore_badge(self) -> None:
        """Drop the busy pill once an operation ends without a status refresh."""
        if self._status is not None:
            self._set_badge(self._status.state, self._status.message)
        else:
            self._set_badge("checking", "")

    def _tick(self) -> None:
        if not self._started_at:
            return
        elapsed = int(time.time() - self._started_at)
        minutes, seconds = divmod(elapsed, 60)
        self.elapsed_label.setText(f"·  {minutes}:{seconds:02d}")

    # ---- events ------------------------------------------------------------
    def append_event(self, event: Event) -> None:
        kind = event.kind
        data = event.data or {}
        if self._busy_op == "launch" and "sandbox" in data:
            self._sandbox_active = bool(data["sandbox"])
            self._apply_sandbox()
        if kind in ("step", "status", "done", "error"):
            # Only a step that says so can be skipped, and only until the next step or the end.
            self.skip_button.setHidden(not (kind == "step" and (event.data or {}).get("skippable")))
        if kind == "step":
            self._last_step_at = time.time()
            self.step_label.setText(event.message or "")
            data = event.data or {}
            png = data.get("png")
            url = data.get("url")
            if not self._show_qr(png) and url:
                self._show_qr_link(str(url))
            if data.get("qr_state"):
                self._set_qr_state(str(data["qr_state"]))
            if event.progress is not None:
                self._set_progress(event.progress)
        elif kind == "progress":
            if (event.data or {}).get("indeterminate"):
                # Busy bar: the phase has no measurable progress (silent installer).
                self.progress.setVisible(True)
                self.progress.setRange(0, 0)
            elif event.progress is not None:
                self._set_progress(event.progress)
            if event.message:
                self.step_label.setText(self._with_speed(event))
        elif kind == "status":
            if event.message:
                self._set_detail(event.message)
        elif kind == "done":
            self._hide_qr()
            self._last_done = event.message or ""
            self._append_line(event.message or "done", "done")
        elif kind == "error":
            self._hide_qr()
            if self._busy_op is None and event.message == self._last_line:
                # A background check (status, news) that fails again with the same words
                # every time would fill OUTPUT with one line per check (b23-rc4 click-through).
                return
            self._set_console_expanded(True)
            self._append_line(event.message, "error")
        else:
            self._append_line(event.message, kind)
        if event.progress is not None and kind not in ("step", "progress"):
            self._set_progress(event.progress)

    def _with_speed(self, event: Event) -> str:
        """The progress text, with the download speed after it when the event counts bytes."""
        data = event.data or {}
        done = data.get("done")
        if data.get("stage") != "download" or not isinstance(done, int | float):
            return event.message
        speed = format_speed(self._speed.update(float(done), time.monotonic()))
        return f"{event.message}  ·  {speed}" if speed else event.message

    def _set_progress(self, value: float) -> None:
        self.progress.setRange(0, 100)  # leave the busy (indeterminate) state
        self.progress.setVisible(True)
        self.progress.setValue(max(0, min(100, int(round(value * 100)))))
        eta = format_eta(self._eta.update(value, time.monotonic()))
        self.progress.setFormat(f"%p%  ·  {eta}" if eta else "%p%")
