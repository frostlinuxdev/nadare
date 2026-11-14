# SPDX-License-Identifier: GPL-3.0-or-later
# ruff: noqa: F811 - tests take the fixtures imported below as arguments
"""GUI smoke: the main window, the worker bridge and the game page basics (split out of
test_gui_smoke.py, #1248)."""

from __future__ import annotations

from _gui_smoke_helpers import (  # noqa: F401 - helpers and pytest fixtures, found by name
    _collect_garbage_between_tests,
    _empty_config,
    _wait_for,
    qapp,
)
from _parts_patch import patch_everywhere

from nadare.core.config import Config
from nadare.gui.main_window import MainWindow


def test_main_window_constructs_and_closes(tmp_path, qapp):
    window = MainWindow(_empty_config(tmp_path))
    try:
        qapp.processEvents()
        assert window.windowTitle()
        assert window.centralWidget() is not None
        assert not hasattr(window, "profile_combo")
        assert window.stack.currentIndex() == 3  # no games yet: the Store
    finally:
        window.close()
        window.deleteLater()
        qapp.processEvents()


def test_main_window_first_run_takes_half_the_screen(tmp_path, qapp):
    window = MainWindow(_empty_config(tmp_path))
    try:
        area = window.screen().availableGeometry()
        # The screen wins over the designed minimum when it is smaller.
        assert window.width() == min(max(880, area.width() // 2), area.width())
        assert window.height() == min(max(560, area.height() - 48), area.height())
        assert window.minimumWidth() <= area.width()
    finally:
        window.close()
        window.deleteLater()
        qapp.processEvents()


def test_main_window_remembers_its_geometry(tmp_path, qapp):
    window = MainWindow(_empty_config(tmp_path))
    window.resize(1010, 700)
    window.show()
    qapp.processEvents()
    window.close()
    window.deleteLater()
    qapp.processEvents()
    assert (tmp_path / "window.ini").exists()

    again = MainWindow(_empty_config(tmp_path))
    try:
        # The offscreen screen is narrower than 1010 px, so Qt clamps the
        # width; the height (default would be 640) proves it was restored.
        assert again.height() == 700
    finally:
        again.close()
        again.deleteLater()
        qapp.processEvents()


def test_main_window_with_profile_tears_down_cleanly(tmp_path, qapp):
    config = Config.load(tmp_path / "config.json")
    config.add_profile(
        "sample",
        backend="wine",
        title="Sample",
        prefix=str(tmp_path / "prefix"),
        install_dir=str(tmp_path / "game"),
        exe_name="missing.exe",
    )
    window = MainWindow(config, profile="sample")
    try:
        qapp.processEvents()
        assert window.config.active == "sample"
        assert window.stack.currentIndex() == 1  # game page
        assert window._current_profile == "sample"
    finally:
        window.close()  # closeEvent -> controller.shutdown()
        window.deleteLater()
        qapp.processEvents()


def test_open_window_helper(tmp_path, qapp):
    from nadare.gui import open_window

    window = open_window(_empty_config(tmp_path), app=qapp)
    try:
        qapp.processEvents()
        assert isinstance(window, MainWindow)
        assert window.isVisible()
    finally:
        window.close()
        window.deleteLater()
        qapp.processEvents()


def test_game_view_shows_and_hides_the_login_qr(tmp_path, qapp):
    """The auth worker emits a step event whose data carries the QR png path;
    the game view must render it (czn_gui.py did; a rewrite dropped it)."""
    from PyQt6.QtGui import QPixmap

    from nadare.core.events import Event
    from nadare.gui.game_view import GameView

    view = GameView()
    view.set_profile("sample", {})
    qr = tmp_path / "qr.png"
    pix = QPixmap(96, 96)
    pix.fill()
    assert pix.save(str(qr))
    try:
        view.append_event(
            Event("step", "Scan the QR code with the STOVE app", data={"png": str(qr)})
        )
        qapp.processEvents()
        assert not view.qr_frame.isHidden(), "QR panel should be shown"
        shown = view.qr_label.pixmap()
        assert shown is not None and not shown.isNull(), "QR image should be rendered"
        assert "Scan this QR" in view.qr_hint.text()

        # The auth worker reports progress under the code; the hint must track it.
        view.append_event(Event("step", "waiting", data={"qr_state": "waiting"}))
        qapp.processEvents()
        assert "Waiting" in view.qr_hint.text()
        assert view.qr_label.pixmap() is not None

        view.append_event(Event("step", "approved", data={"qr_state": "approved"}))
        qapp.processEvents()
        assert "Approved" in view.qr_hint.text()

        view.append_event(Event("step", "expired", data={"qr_state": "expired"}))
        qapp.processEvents()
        assert "expired" in view.qr_hint.text().lower()
        assert view.qr_label.pixmap().isNull(), "a dead code should be cleared"

        view.append_event(Event("done", "Logged in to STOVE"))
        qapp.processEvents()
        assert view.qr_frame.isHidden(), "QR panel should hide once login finishes"

        # A broken/missing path must never crash or blank the UI.
        view.append_event(Event("step", "Scan", data={"png": str(tmp_path / "nope.png")}))
        qapp.processEvents()
        assert view.qr_frame.isHidden()
    finally:
        view.deleteLater()
        qapp.processEvents()


def test_game_view_logout_button_visibility_and_signal(tmp_path, qapp):
    """Log out is a first-class account control, shown only when supported."""
    from nadare.core.backends import Status
    from nadare.gui.game_view import GameView

    view = GameView()
    view.set_profile("sample", {"backend": "stove-czn"})
    received: list[str] = []
    view.action_requested.connect(received.append)
    try:
        assert view.logout.isHidden(), "hidden until the window confirms support"
        view.set_account_controls(True)
        assert not view.logout.isHidden()
        assert not view.logout.isEnabled(), "nothing to log out of before install"
        view.set_status(Status("installed"))
        assert view.logout.isEnabled()
        view.logout.click()
        assert "logout" in received
        view.set_account_controls(False)
        assert view.logout.isHidden()
    finally:
        view.deleteLater()
        qapp.processEvents()


def test_game_view_login_visibility_follows_support(tmp_path, qapp):
    """Log in is hidden by default and only shown for a login backend."""
    from nadare.gui.game_view import GameView

    view = GameView()
    view.set_profile("sample", {"backend": "stove-czn"})
    try:
        assert view.login.isHidden(), "hidden until the window confirms support"
        view.set_account_controls(True)
        assert not view.login.isHidden()
        assert not view.logout.isHidden()
        view.set_account_controls(False)
        assert view.login.isHidden()
        assert view.logout.isHidden()
    finally:
        view.deleteLater()
        qapp.processEvents()


def test_game_page_login_only_for_backends_that_log_in(tmp_path, qapp, monkeypatch):
    """Only CZN (STOVE QR) advertises a login; the others hide the controls."""
    from nadare.gui.main_window import MainWindow

    monkeypatch.setattr(
        MainWindow, "_start", lambda self, profile, operation, payload=None, **kwargs: None
    )
    config = Config.load(tmp_path / "config.json")
    config.add_profile("czn", backend="stove-czn", title="CZN")
    config.add_profile("fgo", backend="fgo-lepton", title="FGO")
    config.add_profile("ak", backend="arknights-yostar", title="Arknights")
    config.add_profile("ss", backend="stellasora-yostar", title="Stella Sora")
    config.add_profile("wine", backend="wine", title="Wine")

    for name, expected in (
        ("czn", True),
        ("fgo", False),
        ("ak", False),
        ("ss", False),
        ("wine", False),
    ):
        window = MainWindow(config, profile=name)
        try:
            qapp.processEvents()
            assert window.game.login.isHidden() is not expected, name
        finally:
            window.close()
            window.deleteLater()
            qapp.processEvents()


def test_settings_remember_me_only_for_supporting_backends(tmp_path, qapp):
    """The Remember me toggle appears for STOVE and persists the per-profile flag."""
    from nadare.gui.settings import SettingsPage

    config = Config.load(tmp_path / "config.json")
    config.add_profile("czn", backend="stove-czn")
    config.add_profile("winey", backend="wine")
    page = SettingsPage()
    try:
        page.load(config, "czn")
        assert not page.account_card.isHidden()
        assert page.remember.isChecked() is False  # off by default
        page.remember.setChecked(True)
        page.save()
        assert config.profile("czn")["remember_me"] is True

        page.load(config, "winey")
        assert page.account_card.isHidden(), "non-remember backends hide the toggle"
    finally:
        page.deleteLater()
        qapp.processEvents()


def test_worker_applies_remember_and_logout(tmp_path, qapp, monkeypatch):
    """The controller can carry a payload to set_remember and call logout."""
    from nadare.gui import workers as workers_mod

    calls: list[tuple] = []

    class Fake:
        def set_remember(self, enabled: bool, persist_flag: bool = True) -> None:
            calls.append(("remember", enabled, persist_flag))

        def logout(self) -> None:
            calls.append(("logout",))

    config = Config.load(tmp_path / "config.json")
    monkeypatch.setattr(workers_mod, "create", lambda *args, **kwargs: Fake())

    workers_mod.BackendWorker(config, "p", "set_remember", payload=True).run()
    workers_mod.BackendWorker(config, "p", "logout").run()

    assert calls == [("remember", True, False), ("logout",)]


def test_controller_bookkeeping_runs_on_the_gui_thread(tmp_path, qapp, monkeypatch):
    """Regression: a background refresh preempted by a user operation must
    respawn from the GUI thread, not from the finishing worker's thread.

    The controller's dict bookkeeping and any respawn must only ever happen on
    the thread that owns it; routing worker signals through a real slot keeps
    them queued there.
    """
    import threading

    from nadare.gui import workers

    gui_thread = threading.current_thread()
    release = threading.Event()
    ran: list[str] = []
    threads: list[tuple[str, bool]] = []

    class Fake:
        def status(self):
            release.wait(3)  # hold the background refresh open
            return {"kind": "status"}

        def launch(self):
            ran.append("launch")
            return 0

    monkeypatch.setattr(workers, "create", lambda *args, **kwargs: Fake())

    config = Config.load(tmp_path / "config.json")
    config.add_profile("p", backend="wine", title="P")
    controller = workers.BackendController(config)

    original_finish = controller._finish
    original_respawn = controller._on_worker_finished

    def record_finish(profile, operation, worker):
        threads.append(("finish", threading.current_thread() is gui_thread))
        return original_finish(profile, operation, worker)

    def record_respawn(profile, worker):
        threads.append(("respawn", threading.current_thread() is gui_thread))
        return original_respawn(profile, worker)

    controller._finish = record_finish
    controller._on_worker_finished = record_respawn

    try:
        assert controller.start("p", "status") is True
        assert controller.start("p", "launch") is True  # queued behind status
        assert controller._pending["p"] == ("launch", None)
        release.set()
        # The worker thread stops before its queued "finished" slot has run on the GUI
        # thread, so is_busy() can go false a moment before the bookkeeping is cleared.
        assert _wait_for(
            qapp,
            lambda: not controller.is_busy("p") and "p" not in controller._workers,
            timeout=5,
        )
        assert ran == ["launch"], "the queued user operation must run"
        assert "p" not in controller._pending
        assert ("respawn", True) in threads
        assert all(on_gui for _, on_gui in threads), threads
    finally:
        release.set()
        if controller._workers:
            controller.shutdown(wait_ms=1000)
        controller.deleteLater()
        qapp.processEvents()


def test_worker_bridge_slots_log_instead_of_raising(qapp, monkeypatch):
    """A broken controller callback (or signal emit) on any bridge slot is logged,
    not allowed to escape: an unhandled exception in a Qt slot aborts the app."""
    from nadare.core.events import Event
    from nadare.gui import workers

    class Boom:
        def emit(self, *args):
            raise RuntimeError("emit failed")

    class Controller:
        def __init__(self):
            self.backend_event = Boom()
            self.result = Boom()

        def _on_succeeded(self, *args):
            raise RuntimeError("succeeded failed")

        def _on_failed(self, *args):
            raise RuntimeError("failed failed")

        def _on_cancelled(self, *args):
            raise RuntimeError("cancelled failed")

    logged: list[str] = []
    monkeypatch.setattr(
        workers.log, "exception", lambda msg, *args, **kwargs: logged.append(msg % args)
    )

    bridge = workers._WorkerBridge(Controller(), "p", "install", object())
    try:
        bridge.forward_event(Event("log", "line"))
        bridge.forward_result({"kind": "status"})
        bridge.on_succeeded(0)
        bridge.on_failed("boom")
        bridge.on_cancelled()
    finally:
        bridge.deleteLater()
        qapp.processEvents()

    assert len(logged) == 5, logged


def test_game_view_logout_enabled_before_install(tmp_path, qapp):
    """Log out is available whenever the game is idle, installed or not."""
    from nadare.core.backends import Status
    from nadare.gui.game_view import GameView

    view = GameView()
    view.set_profile("sample", {"backend": "stove-czn"})
    view.set_account_controls(True)
    try:
        view.set_status(Status("not_installed"))
        assert view.logout.isEnabled(), "no need to be installed to log out"
        view.set_status(Status("running", running=True))
        assert not view.logout.isEnabled(), "disabled while the game runs"
        view.set_busy("launch")
        assert not view.logout.isEnabled(), "disabled while an operation is busy"
    finally:
        view.deleteLater()
        qapp.processEvents()


def test_busy_remember_off_still_forgets_saved_login(tmp_path, qapp, monkeypatch):
    """Turning Remember me off during a busy profile must still wipe the keyring,
    and the (blocking) keyring call must run off the GUI thread."""
    import threading

    from nadare.backends.czn import backend as czn_backend
    from nadare.gui import workers

    # Never start real backend workers here: a live STOVE news fetch outliving
    # the window used to abort the whole test run.
    monkeypatch.setattr(
        workers.BackendController, "start", lambda self, profile, op, payload=None: True
    )
    config = Config.load(tmp_path / "config.json")
    config.add_profile("czn", backend="stove-czn", remember_me=False)
    window = MainWindow(config)
    gui_thread = threading.current_thread()
    calls: list[str] = []
    threads: list[bool] = []

    def fake_forget(profile: str) -> None:
        calls.append(profile)
        threads.append(threading.current_thread() is gui_thread)

    patch_everywhere(monkeypatch, czn_backend, "forget_saved_credential", fake_forget)
    try:
        window._busy["czn"] = "launch"
        window._sync_remember("czn")
        assert _wait_for(qapp, lambda: bool(calls))
        assert calls == ["czn"], "the helper must run even while busy"
        assert threads == [False], "the keyring wipe must not block the GUI thread"
    finally:
        window.close()
        window.deleteLater()
        qapp.processEvents()
