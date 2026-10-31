# SPDX-License-Identifier: GPL-3.0-or-later
# Copyright (C) 2026 Nadare contributors
"""The Steam Set up dialog: the checklist from ``core.steamsetup``, run one step at a time.

The planner and the actions live in the core.  This window only draws the plan,
asks before a step that changes something, and runs that step on the thread pool
so the GUI keeps drawing while Steam closes, a prefix is backed up or an installer
runs.  Reading Steam's files (and probing Vulkan) happens on the pool too.
"""

from __future__ import annotations

import contextlib
import threading

from PyQt6.QtCore import QObject, QRunnable, QThreadPool, pyqtSignal
from PyQt6.QtWidgets import (
    QDialog,
    QFrame,
    QHBoxLayout,
    QLabel,
    QMessageBox,
    QPushButton,
    QVBoxLayout,
)

from ..core import steamsetup
from ..core.steamrun import ActionResult
from .theme import set_state_property

#: What each planner state looks like in the left-hand column.
MARKERS = {
    steamsetup.STATE_DONE: "✓",
    steamsetup.STATE_TODO: "○",
    steamsetup.STATE_BLOCKED: "–",
    steamsetup.STATE_OPTIONAL: "+",
    steamsetup.STATE_RECOMMENDED: "▲",
}

_RUNNING = 0
_RUNNING_LOCK = threading.Lock()


def steps_running() -> int:
    """How many Set up steps are running right now (the window may already be gone)."""
    with _RUNNING_LOCK:
        return _RUNNING


def _count(delta: int) -> None:
    global _RUNNING
    with _RUNNING_LOCK:
        _RUNNING += delta


class _Signals(QObject):
    """Owned by the dialog, never by a task: a finished runnable dies on a pool thread."""

    done = pyqtSignal(object, object, object)  # result, new plan (or None), plan error (or None)
    planned = pyqtSignal(object, object)  # plan (or None), error (or None)


def _plan_of(plan_fn):
    try:
        return plan_fn(), None
    except Exception as exc:  # noqa: BLE001 - shown in the window, never lost
        return None, exc


class _ActionTask(QRunnable):
    def __init__(self, fn, argument, plan_fn, signals: _Signals) -> None:
        super().__init__()
        self._fn = fn
        self._argument = argument
        self._plan_fn = plan_fn
        self._signals = signals

    def run(self) -> None:  # noqa: D102 - QRunnable entry point
        try:
            try:
                result = self._fn(self._argument)
            except Exception as exc:  # noqa: BLE001 - a raised step is shown as its line
                result = ActionResult(False, str(exc))
            if not isinstance(result, ActionResult):
                result = ActionResult(False, "The step returned no result.")
            plan, error = _plan_of(self._plan_fn)
            with contextlib.suppress(RuntimeError):  # the window may be gone by now
                self._signals.done.emit(result, plan, error)
        finally:
            _count(-1)


class _PlanTask(QRunnable):
    def __init__(self, plan_fn, signals: _Signals) -> None:
        super().__init__()
        self._plan_fn = plan_fn
        self._signals = signals

    def run(self) -> None:  # noqa: D102 - QRunnable entry point
        plan, error = _plan_of(self._plan_fn)
        with contextlib.suppress(RuntimeError):
            self._signals.planned.emit(plan, error)


#: The steps whose change can be undone, with the name of their Undo button.
_UNDO_LABELS = {
    "set_runner": "Undo runner change",
    "set_wayland": "Undo native Wayland",
}


#: The checklist row each undoable step belongs to.
_UNDO_ITEMS = {"set_runner": "runner", "set_wayland": "wayland"}


class SteamSetupDialog(QDialog):
    """A checklist for one Steam game, with one action running at a time."""

    def __init__(
        self,
        parent,
        *,
        plan_fn,
        act_fn,
        confirm_fn=None,
        undo_fn=None,
        async_plan: bool = False,
        app_id: str = "",
        initial_undo: tuple[str, object] | None = None,
        cancel_fn=None,
    ) -> None:
        super().__init__(parent)
        #: The Steam app this checklist is for (the main window keeps one window per game).
        self.app_id = app_id
        self._plan_fn = plan_fn
        self._act_fn = act_fn
        self._confirm_fn = confirm_fn or self._ask
        self._undo_fn = undo_fn
        self._busy = False
        self._undoing = False
        #: What an undo puts back: the earlier entry, or ``None`` meaning "remove the setting".
        self._undo_previous = None
        self._can_undo = False
        self._running_action = ""
        self._cancel_fn = cancel_fn
        self._cancel_requested = False
        self.rows: dict[str, QFrame] = {}
        self._signals = _Signals(self)
        self._signals.done.connect(self._on_done)
        self._signals.planned.connect(self._on_planned)

        self.setWindowTitle("Set up")
        self.resize(620, 560)

        layout = QVBoxLayout(self)
        layout.setContentsMargins(20, 18, 20, 18)
        layout.setSpacing(10)

        self.title_label = QLabel("Set up")
        self.title_label.setObjectName("Title")
        layout.addWidget(self.title_label)

        intro = QLabel("Steam stays the downloader; nothing changes before you confirm a step.")
        intro.setObjectName("Hint")
        intro.setWordWrap(True)
        layout.addWidget(intro)

        self.note_label = QLabel("")
        self.note_label.setObjectName("Muted")
        self.note_label.setWordWrap(True)
        layout.addWidget(self.note_label)

        self._rows_layout = QVBoxLayout()
        self._rows_layout.setSpacing(8)
        layout.addLayout(self._rows_layout)
        layout.addStretch(1)

        self.next_button = QPushButton("Do next step")
        self.next_button.setObjectName("Primary")
        self.next_button.clicked.connect(self._run_next)
        layout.addWidget(self.next_button)

        self.result_label = QLabel("")
        self.result_label.setObjectName("Hint")
        self.result_label.setWordWrap(True)
        layout.addWidget(self.result_label)

        footer = QHBoxLayout()
        self.undo_button = QPushButton("Undo runner change")
        self.undo_button.setObjectName("GhostButton")
        self.undo_button.setVisible(False)
        self.undo_button.clicked.connect(self._run_undo)
        footer.addWidget(self.undo_button)
        self.cancel_button = QPushButton("Cancel")
        self.cancel_button.setObjectName("GhostButton")
        self.cancel_button.setVisible(False)
        self.cancel_button.clicked.connect(self._cancel_step)
        footer.addWidget(self.cancel_button)
        footer.addStretch(1)
        self.close_button = QPushButton("Close")
        self.close_button.setObjectName("GhostButton")
        self.close_button.clicked.connect(self.accept)
        footer.addWidget(self.close_button)
        layout.addLayout(footer)

        self._plan = None
        #: An undo kept from an earlier session: ``(step, what the undo needs)``.
        self._initial_undo = initial_undo
        if async_plan:
            self._busy = True  # until the first plan arrives
            self.result_label.setText("Checking Steam…")
            self._render()
            QThreadPool.globalInstance().start(_PlanTask(plan_fn, self._signals))
        else:
            self._plan = plan_fn()
            self._adopt_initial_undo()
            self._render()

    def _adopt_initial_undo(self) -> None:
        """Offer an undo kept from an earlier session, if its change is still in place."""
        pending, self._initial_undo = self._initial_undo, None
        if pending is None or self._undo_fn is None or self._plan is None:
            return
        step, previous = pending
        key = _UNDO_ITEMS.get(step)
        item = next((i for i in self._plan.items if i.key == key), None)
        if item is None or item.state != steamsetup.STATE_DONE:
            return  # someone changed the setting since: this undo no longer fits
        self._can_undo = True
        self._undo_previous = previous
        self.undo_button.setText(_UNDO_LABELS[step])

    @property
    def busy(self) -> bool:
        """Whether an action (or the first look at Steam) is running on the pool."""
        return self._busy

    def refresh(self) -> None:
        """Ask the planner again and redraw the checklist (the result line stays)."""
        self._plan = self._plan_fn()
        self._render()

    # ---- closing -----------------------------------------------------------
    def done(self, result: int) -> None:  # noqa: D102 - Qt override
        # Escape, the title-bar X and Close all end here. A step that is running has to finish
        # (Steam may be closed in the middle of it); the window waits.
        if self._busy and self._plan is not None:
            self.result_label.setText("A step is still running. Please wait until it finishes.")
            return
        super().done(result)

    # ---- drawing -----------------------------------------------------------
    def _render(self) -> None:
        plan = self._plan
        title = f"Set up {plan.game}" if plan is not None else "Set up"
        self.title_label.setText(title)
        self.setWindowTitle(title)
        note = self._note_text()
        self.note_label.setText(note)
        self.note_label.setVisible(bool(note))

        while self._rows_layout.count():
            item = self._rows_layout.takeAt(0)
            widget = item.widget() if item is not None else None
            if widget is not None:
                widget.setParent(None)
                widget.deleteLater()
        self.rows = {}
        if plan is not None:
            for item in plan.items:
                self.rows[item.key] = self._make_row(item)
                self._rows_layout.addWidget(self.rows[item.key])

        next_item = plan.next_item if plan is not None else None
        if next_item is None:
            self.next_button.setText("Do next step")
        else:
            self.next_button.setText(
                next_item.action_label or f"Do next step: {next_item.label}"
            )
        self._apply_enabled()

    def _note_text(self) -> str:
        if self._plan is None:
            return ""
        parts: list[str] = []
        if not self._plan.verified:
            parts.append("Not verified on a real Steam yet.")
        if self._plan.note:
            parts.append(self._plan.note)
        return " ".join(parts)

    def _make_row(self, item: steamsetup.SetupItem) -> QFrame:
        row = QFrame()
        row.setObjectName("Card")
        row_layout = QHBoxLayout(row)
        row_layout.setContentsMargins(12, 9, 12, 9)
        row_layout.setSpacing(10)

        row.marker_label = QLabel(MARKERS.get(item.state, "•"))
        marker_name = "Accent" if item.state == steamsetup.STATE_RECOMMENDED else "Muted"
        row.marker_label.setObjectName(marker_name)
        row.marker_label.setFixedWidth(18)
        row_layout.addWidget(row.marker_label, 0)

        text_layout = QVBoxLayout()
        text_layout.setSpacing(2)
        row.text_label = QLabel(item.label)
        text_layout.addWidget(row.text_label)
        row.detail_label = QLabel(item.detail)
        row.detail_label.setObjectName("Dim" if item.state == steamsetup.STATE_BLOCKED else "Muted")
        row.detail_label.setWordWrap(True)
        row.detail_label.setVisible(bool(item.detail))
        text_layout.addWidget(row.detail_label)
        row_layout.addLayout(text_layout, 1)

        if item.action and item.state in (
            steamsetup.STATE_OPTIONAL,
            steamsetup.STATE_RECOMMENDED,
        ):
            row.button = QPushButton(item.action_label or "Run")
            row.button.setObjectName("GhostButton")
            row.button.clicked.connect(lambda _checked=False, i=item: self._run_item(i))
            row_layout.addWidget(row.button, 0)
        return row

    def _apply_enabled(self) -> None:
        enabled = not self._busy
        has_next = self._plan is not None and self._plan.next_item is not None
        self.next_button.setEnabled(has_next and enabled)
        self.close_button.setEnabled(enabled or self._plan is None)
        self.undo_button.setVisible(self._can_undo and self._undo_fn is not None)
        self.undo_button.setEnabled(enabled and self._undo_fn is not None)
        self.cancel_button.setVisible(self._can_cancel())
        self.cancel_button.setEnabled(not self._cancel_requested)
        for row in self.rows.values():
            button = getattr(row, "button", None)
            if button is not None:
                button.setEnabled(enabled)

    # ---- cancelling ----------------------------------------------------------
    def _can_cancel(self) -> bool:
        """Only a step that downloads can be stopped: the rest is quick or must not be cut."""
        action = self._running_action
        return (
            self._busy
            and self._cancel_fn is not None
            and self._plan is not None
            and (action == "set_runner" or action.startswith("install_prefix:"))
        )

    def _cancel_step(self) -> None:
        if self._cancel_requested or not self._busy or self._cancel_fn is None:
            return
        self._cancel_requested = True
        self.cancel_button.setText("Cancelling…")
        self.cancel_button.setEnabled(False)
        self._cancel_fn()

    # ---- running -----------------------------------------------------------
    def _run_next(self) -> None:
        item = self._plan.next_item if self._plan is not None else None
        if item is not None:
            self._run_item(item)

    def _run_item(self, item: steamsetup.SetupItem) -> None:
        if self._busy or not item.action:
            return
        if item.warning and not self._confirm_fn(f"Set up {self._plan.game}", item.warning):
            return  # a declined confirmation changes nothing at all
        self._running_action = item.action
        self._start(self._act_fn, item, undoing=False)

    def _run_undo(self) -> None:
        if self._busy or not self._can_undo or self._undo_fn is None:
            return
        self._running_action = "undo"
        self._start(self._undo_fn, self._undo_previous, undoing=True)

    def _start(self, fn, argument, *, undoing: bool) -> None:
        self._busy = True
        self._undoing = undoing
        self.result_label.setText("Working…")
        self.result_label.setObjectName("Muted")
        set_state_property(self.result_label, "")
        self._apply_enabled()
        _count(+1)
        QThreadPool.globalInstance().start(
            _ActionTask(fn, argument, self._plan_fn, self._signals)
        )

    def _on_planned(self, plan, error) -> None:
        """The first look at Steam is in (computed off the GUI thread)."""
        self._busy = False
        if plan is None:
            self._plan = None
            self._render()
            self._show_result(ActionResult(False, str(error) or "Steam could not be read."))
            return
        self._plan = plan
        self._adopt_initial_undo()
        self._render()
        self.result_label.setText("")
        self.result_label.setObjectName("Hint")
        set_state_property(self.result_label, "")

    def _on_done(self, result: ActionResult, plan, plan_error) -> None:
        self._busy = False
        if self._cancel_requested and result.ok:
            # The step had already got past the point where it can be stopped.
            result = ActionResult(
                result.ok,
                f"{result.line} The Cancel came too late: the step had already started.",
                result.changed,
                result.backup,
                result.previous,
            )
        self._cancel_requested = False
        self.cancel_button.setText("Cancel")
        if self._undoing:
            self._undoing = False
            if result.ok:  # a failed undo stays available to try again
                self._can_undo = False
                self._undo_previous = None
        elif self._running_action in _UNDO_LABELS and result.ok and result.changed:
            # Only a runner or launch-option change can be undone, and only the last one.
            self._can_undo = self._undo_fn is not None
            self._undo_previous = result.previous
            self.undo_button.setText(_UNDO_LABELS[self._running_action])
        if plan is not None:
            self._plan = plan
        self._render()  # also re-enables the buttons, whatever happened
        if plan_error is not None:
            result = ActionResult(
                result.ok,
                f"{result.line} (The checklist could not be refreshed: {plan_error})",
                result.changed,
                result.backup,
                result.previous,
            )
        self._show_result(result)

    def _show_result(self, result: ActionResult) -> None:
        self.result_label.setText(result.line or ("Done." if result.ok else "It did not work."))
        self.result_label.setObjectName("StateBad" if not result.ok else "Hint")
        set_state_property(self.result_label, "" if result.ok else "bad")

    def _ask(self, title: str, text: str) -> bool:
        """The real confirmation, used only when the caller passes no ``confirm_fn``."""
        box = QMessageBox(self)
        box.setWindowTitle(title)
        box.setText(text)
        box.setIcon(QMessageBox.Icon.Question)
        box.setStandardButtons(QMessageBox.StandardButton.Yes | QMessageBox.StandardButton.No)
        box.setDefaultButton(QMessageBox.StandardButton.No)
        return box.exec() == QMessageBox.StandardButton.Yes
