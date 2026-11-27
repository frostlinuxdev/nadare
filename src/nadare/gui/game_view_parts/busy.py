# SPDX-License-Identifier: GPL-3.0-or-later
# Copyright (C) 2026 Nadare contributors
"""What the game page shows while an operation runs, pauses, ends or fails.

Split out of :mod:`nadare.gui.game_view`; the code is unchanged.
"""

from __future__ import annotations

import re
import time

from ...core import operation_policy, runner_failures
from ...core.eta import format_eta, format_speed
from ...core.events import Event
from ...core.exitcode import STOPPED_EXIT_CODES, crash_signal, play_time
from ...core.winecrash import WineCrash

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
    "remove_qualities": "Removing old quality files",
    "apply_patch": "Applying the patch",
    "update_patch": "Updating the patch",
    "remove_patch": "Removing the patch",
}


def start_label(operation: str) -> str:
    """The step text when ``operation`` starts: the patch operations get their title, the
    others keep the plain verb they always showed."""
    if operation.endswith("_patch"):
        return operation_title(operation)
    return operation.capitalize()


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
    "remove_qualities": "cleaning",
    "apply_patch": "updating",
    "update_patch": "updating",
    "remove_patch": "updating",
}

#: A second click right after a button turns into CANCEL must not cancel the new job.
_CANCEL_GRACE_S = 0.5

#: A game that exits non-zero after at least this long had a real session; a quicker
#: non-zero exit is a failed launch. Stop-requested signal codes are always normal.
_LAUNCH_EXIT_GRACE_S = 30.0

_PAUSED_TEXT = "Paused. Everything downloaded so far is kept."

#: Operations whose success makes the status shown before them out of date.
_STATE_CHANGING = operation_policy.operations_with("state_changing") | {"uninstall"}

#: Operations whose success is said on the progress line afterwards: OUTPUT is folded (#993),
#: so its "done" line alone would leave no sign that an install or update worked.
_ANNOUNCED_ON_SUCCESS = operation_policy.operations_with("announce_success")

#: A backend's own last line for a launch (CZN); the page then adds no second one.
_GAME_END_LINES = frozenset(
    {"game exited normally", "game stopped", "steam returned without starting the game"}
)

#: Operations the failure dialog and the RETRY button can run again as they were.
_RETRYABLE = operation_policy.operations_with("retryable")


#: The file sync's own summary line (``direct`` and Arknights backends): what a sync changed.
_SYNC_SUMMARY = re.compile(r"synced (\d+) item\(s\), (\d+) already up to date")
#: The update receipt (``direct.update_receipt``): the version really changed.
_UPDATE_RECEIPT = re.compile(r"update (\S+) -> (\S+):")
#: A backend's launch step: the game is starting, so a post-download bar has done its job.
_LAUNCHING_STEP = re.compile(r"\blaunching\b")


def result_line(
    operation: str,
    last_done: str,
    sync: tuple[int, int] | None,
    versions: tuple[str, str] | None = None,
) -> str:
    """What the progress line says after ``operation`` succeeded (user sim r2 #6).

    A sync summary says what really happened (nothing to fix, N files fixed, already up to
    date); without one the backend's own "done" line, else "<operation> finished".
    """
    if operation == "update" and versions is not None:
        return f"Update finished: {versions[0]} -> {versions[1]}"
    if sync is not None and operation in ("repair", "update"):
        changed, kept = sync
        files = "file" if changed == 1 else "files"
        if operation == "repair":
            if changed == 0:
                return f"Repair finished: nothing needed fixing ({kept} checked)"
            return f"Repair finished: {changed} {files} fixed"
        if changed == 0:
            return "Already up to date: nothing to download"
        return f"Update finished: {changed} {files} updated"
    return last_done or f"{operation_title(operation)} finished"


def can_pause_kind(kind: str) -> bool:
    """Whether installs of backend ``kind`` resume from what is already on disk."""
    from ...core.backends import supports_resume

    return supports_resume(kind)


class BusyMixin:
    """Mixin for :class:`GameView`; see the module text."""

    # ---- lifecycle ---------------------------------------------------------
    def set_busy(self, operation: str) -> None:
        """Mark ``operation`` as running and start the elapsed clock."""
        resumed = self._paused_op is not None
        self._paused_op = None
        self._failed_op = None
        self._last_done = ""
        self._last_sync = None  # (changed, unchanged) from the sync summary, if any
        self._last_versions = None  # (old, new) from an update receipt, if any
        self._queue_waiting = False
        # A user operation supersedes a pending status wait: if it is cancelled while still
        # queued, the status worker it detached never reports back (Neon, 2026-11-27).
        self._awaiting_status = False
        self._busy_op = operation
        self._operation_detail = operation_title(operation)
        self._set_detail("")
        if operation == "launch":
            self._wine_crash = WineCrash()
            self._launch_runner = {}  # what this launch runs on, once the backend says (#1732)
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
        self._download_finished = False
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
            else start_label(operation)
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
        self._set_badge("waiting", "")  # not INSTALLING: nothing of it runs yet
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
        self._download_finished = False
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
        # Nor the result of the operation before it: every success starts a status re-probe,
        # and its end wiped "✓ Repair finished" at once (user sim r2 #6).
        kept_result = (
            self.step_label.text()
            if operation in BACKGROUND and self._busy_op is None
            and self.step_label.text().startswith("✓ ")
            else ""
        )
        self._busy_op = None
        self._clear_busy_progress()
        if kept_result:
            self.step_label.setText(kept_result)
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
            crash = "" if stop_requested else crash_signal(exit_code)
            fast_failure = (
                not stop_requested and exit_code != 0
                and exit_code not in STOPPED_EXIT_CODES and elapsed < _LAUNCH_EXIT_GRACE_S
            )
            success = not stop_requested and (
                exit_code == 0 or (elapsed >= _LAUNCH_EXIT_GRACE_S and not crash)
            )
            facts = getattr(self, "_launch_runner", None) or {}
            runner_notice = runner_failures.record_result(
                self._profile, str(facts.get("runner") or ""),
                "failure" if fast_failure else "success" if success else "neutral",
            )
            if exit_code == 0:
                if self._last_done.lower() not in _GAME_END_LINES:  # CZN says it itself
                    if not stop_requested and elapsed < _LAUNCH_EXIT_GRACE_S:
                        # A publisher launcher hands the game to another process and exits 0 at
                        # once: the game keeps running, so it did not "exit normally" (#1554).
                        self._append_line(
                            f"The launcher closed after {play_time(elapsed)}; "
                            "the game may still be running.",
                            "status",
                        )
                    else:
                        self._append_line("game exited normally", "done")
                return
            runner_note, other_runner = self._runner_note()
            broken_runner = ""
            if runner_notice:
                name = str(facts["runner"])
                default = str(facts.get("runner_default") or "the recommended runner")
                advice = (
                    " This is already the recommended runner. Open the logs for details."
                    if facts.get("runner_recommended") else
                    f" Try the recommended runner ({default}): in this game's settings, "
                    "set Runner to Default · recommended."
                )
                broken_runner = (
                    f"Different games failed at startup on {name} in the last 24 hours."
                    f" This runner may be broken.{advice}"
                )
            wine_crash = getattr(self, "_wine_crash", None)
            if not stop_requested and wine_crash is not None and wine_crash.detected:
                module = wine_crash.module
                message = (
                    f"The game crashed inside {module} (Wine)"
                    if module else "The game crashed (Wine)"
                )
                # A startup Wine crash also counts against the runner (#1733): its one-time
                # notice goes in this box, or it would be used up unseen.
                tail = (f" {broken_runner}" if broken_runner
                        else f"{runner_note} Open the logs for details.")
                self.show_error(
                    "The game crashed",
                    f"{message}, exit code {exit_code}.{tail}",
                    retry_op=retry_op,
                    failed=True,
                    settings=other_runner,
                )
                return
            if crash and elapsed >= _LAUNCH_EXIT_GRACE_S:
                # A real session that ended in a crash signal is not a normal quit (#1031).
                self.show_error(
                    "The game crashed",
                    f"The game crashed ({crash}, exit code {exit_code}) after "
                    f"{play_time(elapsed)}.{runner_note} Open the logs for details.",
                    retry_op=retry_op,
                    failed=True,
                    settings=other_runner,
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
            if broken_runner:
                self.show_error(
                    "This runner may be broken",
                    broken_runner,
                    retry_op=retry_op, failed=True, settings=other_runner,
                )
                return
            # A launch that failed at once: say what it ran on (#1732).
            self.show_error(
                "Operation failed",
                f"{operation_title(operation)} did not finish (exit code {exit_code})."
                f"{runner_note} Open the logs for details.",
                retry_op=retry_op,
                failed=True,
                settings=other_runner,
            )
            return
        if exit_code == 0 and operation in _ANNOUNCED_ON_SUCCESS:
            done = result_line(
                operation, self._last_done, self._last_sync, self._last_versions
            )
            self.step_label.setText(f"✓ {done}")  # cleared by the next operation
        if exit_code != 0:
            self.show_error(
                "Operation failed",
                f"{operation_title(operation)} did not finish (exit code {exit_code}). "
                "Open the logs for details.",
                retry_op=retry_op,
                failed=True,
            )

    def _runner_note(self) -> tuple[str, bool]:
        """A sentence naming the runner this launch used, and whether it is not the recommended
        one (then the box points to the game's settings). Empty when the backend did not say."""
        facts = getattr(self, "_launch_runner", None) or {}
        name = str(facts.get("runner") or "")
        if not name:
            return "", False
        if facts.get("runner_recommended"):
            return f" It ran on {name}, the recommended runner.", False
        default = str(facts.get("runner_default") or "the recommended runner")
        return (
            f" It ran on {name}, which is not the recommended runner ({default}). If other "
            "games fail the same way, try the recommended one: in this game's settings, set "
            "Runner to Default · recommended.",
            True,
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
        self._set_detail(self._status.message if self._status is not None else "")
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
        if self._busy_op == "launch" and (
            kind == "game_running"
            or (kind == "step" and _LAUNCHING_STEP.search(event.message or ""))
        ):
            # The game is starting. A bar shown earlier for a runner download has done its job.
            self.progress.setRange(0, 100)
            self.progress.setVisible(False)
        if self._busy_op == "launch" and kind == "log":
            self._wine_crash.feed(event.message or "")
        if kind == "log" and self._busy_op in ("repair", "update") and event.message:
            match = _SYNC_SUMMARY.search(event.message)
            if match:
                self._last_sync = (int(match.group(1)), int(match.group(2)))
            receipt = _UPDATE_RECEIPT.search(event.message)
            if receipt:
                self._last_versions = (receipt.group(1), receipt.group(2))
        if self._busy_op is not None and kind in ("step", "done", "error"):
            # A finished step must not linger while the next phase is being prepared. Progress
            # counters stay in the bar below: the header names the phase only (2026-11-20).
            from ..game_view import breakable  # late: game_view imports this module

            # A pack file name must not widen the page (#1703): break points, as the step line has.
            text = (event.message or "") if kind == "step" else ""
            self._operation_detail = breakable(text)
            self._set_detail("")
        if self._busy_op == "launch" and "runner_recommended" in data:
            self._launch_runner = dict(data)
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
            # A step after a finished download is work with no percentage of its own
            # (unpacking, hashing, setting up). A busy bar says "working", not a frozen 100%.
            post_download = self._download_finished
            self._download_finished = False
            if post_download:
                self.progress.setVisible(True)
                self.progress.setRange(0, 0)
            png = data.get("png")
            url = data.get("url")
            if not self._show_qr(png) and url:
                self._show_qr_link(str(url))
            if data.get("qr_state"):
                self._set_qr_state(str(data["qr_state"]))
            if event.progress is not None and not post_download:
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
            self._download_finished = (
                event.progress is not None and event.progress >= 1.0
            )
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
