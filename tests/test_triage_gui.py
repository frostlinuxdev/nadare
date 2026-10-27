# SPDX-License-Identifier: GPL-3.0-or-later
"""Regression tests for the GUI/process triage batch.

#99 unvalidated logo path -> QPixmap on a FIFO
#101 notice/profile titles rendered as rich text
#22  STOP must run the backend ``stop`` operation, not the generic cancel
#63  Remember me enabled while a worker owns the profile
#100 an unusable ``XDG_RUNTIME_DIR`` must not stop the GUI opening
#61  single-instance activation split across reads
#76  single-instance lock must not be stolen by a concurrent start
"""

from __future__ import annotations

import os
import time
import uuid

os.environ.setdefault("QT_QPA_PLATFORM", "offscreen")

import pytest

try:  # pragma: no cover - exercised only when PyQt6 is missing
    from PyQt6.QtCore import Qt
    from PyQt6.QtWidgets import QApplication  # noqa: F401
except Exception:  # noqa: BLE001
    pytest.skip("PyQt6 is not importable in this interpreter", allow_module_level=True)

from nadare.core.config import Config
from nadare.gui.main_window import MainWindow


@pytest.fixture(scope="module")
def qapp():
    app = QApplication.instance()
    if app is None:
        app = QApplication([])
    yield app


def _config(tmp_path, **profiles) -> Config:
    config = Config.load(tmp_path / "config.json")
    config.data["check_updates"] = False
    for name, data in profiles.items():
        config.add_profile(name, **data)
    return config


def _wait_for(qapp, predicate, timeout=3.0) -> bool:
    deadline = time.monotonic() + timeout
    while time.monotonic() < deadline:
        qapp.processEvents()
        if predicate():
            return True
        time.sleep(0.01)
    return False


# ---- #99 ------------------------------------------------------------------
def test_icon_pixmap_skips_a_non_file_logo(tmp_path, qapp, monkeypatch):
    """A hand-edited FIFO ``logo`` must be filtered before it reaches QPixmap."""
    from nadare.gui import sidebar

    fifo = tmp_path / "logo.fifo"
    os.mkfifo(fifo)
    seen: list[str] = []
    monkeypatch.setattr(sidebar, "load_card_pixmap", lambda paths: seen.extend(paths) or None)
    sidebar.icon_pixmap({"logo": str(fifo), "title": "Game"}, "Game")
    assert str(fifo) not in seen, "a non-file logo must never be handed to QPixmap"


# ---- #101 -----------------------------------------------------------------
def test_externally_sourced_labels_are_plain_text(qapp):
    """Notice text and profile titles may contain '<'/'&': render them literally."""
    from nadare.gui.library import GameCard
    from nadare.gui.notice import AccountNoticeDialog
    from nadare.gui.sidebar import GameRow

    dialog = AccountNoticeDialog("<b>bold</b> & <i>italic</i>")
    card = GameCard("p", "<b>Title</b>", "wine", None, None)
    row = GameRow("p", "<b>Row</b>")
    try:
        for label in (dialog.notice_label, card.title_label, row.title_label):
            assert label.textFormat() == Qt.TextFormat.PlainText
    finally:
        dialog.deleteLater()
        card.deleteLater()
        row.deleteLater()
        qapp.processEvents()


# ---- #22 ------------------------------------------------------------------
def test_stop_button_requests_the_stop_operation(qapp):
    """While a launch is busy the primary button reads STOP and asks for stop."""
    from nadare.gui.game_view import GameView

    view = GameView()
    view.set_profile("p", {})
    view.set_busy("launch")
    actions: list[str] = []
    cancels: list[bool] = []
    view.action_requested.connect(actions.append)
    view.cancel_requested.connect(lambda: cancels.append(True))
    try:
        assert view.primary.text() == "STOP"
        view._primary_grace_until = 0.0  # past the double-click guard (#617)
        view.primary.click()
        assert actions == ["stop"]
        assert cancels == [], "STOP must not take the generic cancel path"
    finally:
        view.deleteLater()
        qapp.processEvents()


def test_stop_preempts_a_running_launch(qapp, tmp_path):
    """A stop request may cancel-and-queue behind the running launch worker."""
    from nadare.gui.workers import BackendController

    class FakeWorker:
        operation = "launch"

        def __init__(self) -> None:
            self.cancelled = False
            self._running = True

        def isRunning(self) -> bool:  # noqa: N802 - Qt naming
            return self._running

        def cancel(self) -> None:
            self.cancelled = True

        def finish(self) -> None:
            self._running = False

    config = _config(tmp_path, p={"backend": "wine"})
    controller = BackendController(config)
    launch = FakeWorker()
    controller._workers["p"] = launch
    spawned: list[str] = []
    controller._spawn = lambda profile, operation, payload=None: spawned.append(operation)
    try:
        assert controller.start("p", "stop") is True
        assert launch.cancelled is True
        assert controller._pending["p"] == ("stop", None)
        launch.finish()
        controller._finish("p", "launch", launch)
        controller._on_worker_finished("p", launch)
        assert spawned == ["stop"]
    finally:
        controller._pending.clear()
        controller._workers.clear()
        controller.deleteLater()
        qapp.processEvents()


# ---- #63 ------------------------------------------------------------------
def test_remember_me_enabled_while_busy_is_applied_when_idle(tmp_path, qapp, monkeypatch):
    """Turning Remember me on during a busy profile must not be silently dropped."""
    from nadare.gui import workers

    starts: list[tuple[str, object]] = []
    monkeypatch.setattr(
        workers.BackendController,
        "start",
        lambda self, profile, operation, payload=None: starts.append((operation, payload))
        or True,
    )
    config = _config(tmp_path, czn={"backend": "stove-czn", "remember_me": True})
    window = MainWindow(config)
    try:
        starts.clear()
        window._busy["czn"] = "launch"
        window._sync_remember("czn")
        assert starts == [], "remember-me cannot start while the profile is busy"
        window._busy.pop("czn", None)
        window._on_busy("czn", "launch", False)  # the worker just finished
        assert ("set_remember", True) in starts, "the queued change must be applied"
    finally:
        window.close()
        window.deleteLater()
        qapp.processEvents()


# ---- #100 -----------------------------------------------------------------
def test_unusable_runtime_dir_falls_back_to_a_bare_name(monkeypatch, tmp_path):
    from nadare.gui import single_instance

    missing = tmp_path / "does-not-exist"
    monkeypatch.setenv("XDG_RUNTIME_DIR", str(missing))
    uid = os.getuid() if hasattr(os, "getuid") else 0
    assert single_instance.server_name() == f"nadare-{uid}"


def test_start_succeeds_with_a_missing_runtime_dir(qapp, monkeypatch, tmp_path):
    from nadare.gui.single_instance import SingleInstance

    monkeypatch.setenv("XDG_RUNTIME_DIR", str(tmp_path / "gone"))
    instance = SingleInstance()
    try:
        assert instance.start() is True
        assert instance.status == ""
    finally:
        instance.close()


# ---- #61 ------------------------------------------------------------------
def test_fragmented_activation_is_reassembled(qapp):
    """A payload split as 'act' + 'ivate' must still raise the running window."""
    from PyQt6.QtNetwork import QLocalSocket

    from nadare.gui.single_instance import SingleInstance

    name = f"nadare-triage-{os.getpid()}-{uuid.uuid4().hex}"
    primary = SingleInstance(name)
    assert primary.start() is True
    activations: list[int] = []
    primary.activation_requested.connect(lambda: activations.append(1))
    socket = QLocalSocket()
    try:
        socket.connectToServer(name)
        assert socket.waitForConnected(300)
        socket.write(b"act")
        socket.flush()
        socket.waitForBytesWritten(300)
        deadline = time.monotonic() + 0.3
        while time.monotonic() < deadline:
            qapp.processEvents()
            time.sleep(0.005)
        assert activations == [], "a fragment alone must not activate"

        socket.write(b"ivate")
        socket.flush()
        socket.waitForBytesWritten(300)
        assert _wait_for(qapp, lambda: bool(activations)), "the full payload must activate"
        assert activations == [1]
    finally:
        socket.abort()
        primary.close()


# ---- #76 ------------------------------------------------------------------
def test_start_does_not_steal_a_socket_that_appeared_after_the_ping(qapp):
    """A live server bound between the ping and listen must not be unlinked."""
    from nadare.gui.single_instance import SingleInstance

    name = f"nadare-triage-{os.getpid()}-{uuid.uuid4().hex}"
    primary = SingleInstance(name)
    assert primary.start() is True
    secondary = SingleInstance(name)
    calls = {"n": 0}

    def fake_ping() -> bool:
        calls["n"] += 1
        # The live primary's socket "appears" only after the first probe.
        return calls["n"] > 1

    secondary._ping = fake_ping
    try:
        assert secondary.start() is False, "a live lock must not be stolen"
        assert secondary.status, "the loser must report why"
        assert primary._ping() is True, "the primary's socket must still be live"
    finally:
        secondary.close()
        primary.close()
