# SPDX-License-Identifier: GPL-3.0-or-later
# ruff: noqa: F811 - tests take the fixtures imported below as arguments
"""GUI audit round 5 regressions (split out of test_gui_smoke.py, #1248)."""

from __future__ import annotations

import os

from _gui_smoke_helpers import (  # noqa: F401 - helpers and pytest fixtures, found by name
    _collect_garbage_between_tests,
    _empty_config,
    _wait_for,
    qapp,
)

from nadare.core.config import Config
from nadare.gui.main_window import MainWindow


# ---------------------------------------------------------------------------
# Audit round 5 regressions (06-gui)
# ---------------------------------------------------------------------------
def test_single_instance_socket_lives_in_the_runtime_dir(monkeypatch, tmp_path):
    """Regression (06-3): the lock is not a bare name under the shared temp dir."""
    from nadare.gui import single_instance

    monkeypatch.setenv("XDG_RUNTIME_DIR", str(tmp_path))
    name = single_instance.server_name()
    assert name.startswith(str(tmp_path) + os.sep)
    assert name.endswith(".sock")


def test_single_instance_reports_why_it_bowed_out(qapp, tmp_path):
    """Regression (06-10): a secondary start records a reason instead of silence."""
    from nadare.gui.single_instance import SingleInstance

    name = f"nadare-gui-status-{os.getpid()}-{tmp_path.name}"
    primary = SingleInstance(name)
    assert primary.start() is True
    try:
        secondary = SingleInstance(name)
        assert secondary.start() is False
        assert secondary.status, "the secondary must know why it did not open"
        secondary.close()
    finally:
        primary.close()


def test_no_dead_silent_bookkeeping(tmp_path, qapp):
    """Regression (06-13): the write-only ``_silent`` set is gone."""
    window = MainWindow(_empty_config(tmp_path))
    try:
        assert not hasattr(window, "_silent")
    finally:
        window.close()
        window.deleteLater()
        qapp.processEvents()


def test_single_instance_only_activates_on_the_activate_payload(qapp, tmp_path):
    """Regression (06-3): an arbitrary local connection must not steal focus."""
    import time

    from PyQt6.QtNetwork import QLocalSocket

    from nadare.gui.single_instance import SingleInstance

    name = f"nadare-gui-smoke-{os.getpid()}-{tmp_path.name}"
    primary = SingleInstance(name)
    assert primary.start() is True
    activations: list[int] = []
    primary.activation_requested.connect(lambda: activations.append(1))
    try:
        for payload in (b"", b"not-an-activation"):
            socket = QLocalSocket()
            socket.connectToServer(name)
            assert socket.waitForConnected(300)
            if payload:
                socket.write(payload)
                socket.flush()
                socket.waitForBytesWritten(300)
            socket.disconnectFromServer()
            deadline = time.monotonic() + 0.3
            while time.monotonic() < deadline:
                qapp.processEvents()
                time.sleep(0.005)
        assert activations == [], "only a valid 'activate' payload may raise the window"

        valid = QLocalSocket()
        valid.connectToServer(name)
        assert valid.waitForConnected(300)
        valid.write(b"activate")
        valid.flush()
        valid.waitForBytesWritten(300)
        assert _wait_for(qapp, lambda: bool(activations))
    finally:
        primary.close()


def test_news_media_workers_do_not_mutate_the_live_config(tmp_path, qapp, monkeypatch):
    """Regression (06-4): workers must not write the config dict on their thread."""
    from nadare.core import artwork, yostar
    from nadare.gui import workers

    recorded: list[tuple] = []
    monkeypatch.setattr(
        artwork, "record_official", lambda *args, **kwargs: recorded.append(args) or True
    )
    monkeypatch.setattr(yostar, "configured_media", lambda profile: "https://example.invalid/m")
    monkeypatch.setattr(
        yostar, "load_media", lambda source, game_tag=None, **kwargs: object()
    )
    monkeypatch.setattr(
        yostar,
        "cache_media",
        lambda media, slug, **kwargs: {"hero": "/cache/hero.png", "banners": []},
    )

    class FakeBackend:
        game_tag = "czn"
        kind = "stove-czn"

        def __init__(self) -> None:
            self.ctx = type("Ctx", (), {"profile": {"title": "P"}})()

    worker = workers.BackendWorker(Config.load(tmp_path / "config.json"), "p", "media")
    result = worker._load_media(FakeBackend())
    assert result["hero"] == "/cache/hero.png"
    assert recorded == [], "the worker must not record official art itself"


def test_news_result_records_official_hero_on_the_gui_thread(tmp_path, qapp, monkeypatch):
    """Regression (06-4): the window applies publisher-art provenance itself."""
    from nadare.core import artwork
    from nadare.core.news import NewsResult

    config = _empty_config(tmp_path)
    config.add_profile("p", backend="stove-czn", title="P")
    window = MainWindow(config)
    recorded: list[tuple] = []
    monkeypatch.setattr(
        artwork, "record_official", lambda *args, **kwargs: recorded.append(args) or True
    )
    try:
        window._on_result("p", NewsResult(items=[], hero="/cache/h.png"))
        assert recorded == [(config.profile("p"), "/cache/h.png", "hero")]
    finally:
        window.close()
        window.deleteLater()
        qapp.processEvents()


def test_close_warns_when_a_mutating_operation_is_running(tmp_path, qapp, monkeypatch):
    """Regression (06-5): quitting mid-install prompts instead of hard-exiting."""
    from PyQt6.QtWidgets import QMessageBox

    from nadare.gui import main_window as mw
    from nadare.gui import workers

    monkeypatch.setattr(
        workers.BackendController, "start", lambda self, profile, op, payload=None: True
    )
    config = _empty_config(tmp_path)
    config.add_profile("p", backend="wine", title="P")
    window = MainWindow(config)

    class Installing:
        operation = "install"

        def isRunning(self) -> bool:  # noqa: N802 - Qt naming
            return True

    window.controller._workers["p"] = Installing()
    prompts: list[str] = []
    monkeypatch.setattr(
        mw.QMessageBox,
        "warning",
        lambda *args, **kwargs: prompts.append(args[2]) or QMessageBox.StandardButton.Cancel,
    )
    try:
        assert window.close() is False, "Cancel must keep the window open"
        assert prompts and "p" in prompts[0]
    finally:
        window.controller._workers.clear()
        window.close()
        window.deleteLater()
        qapp.processEvents()


def test_fetch_art_runs_off_the_gui_thread(tmp_path, qapp, monkeypatch):
    """Regression (06-6): 'Fetch art' must not run blocking HTTPS in the event loop."""
    import threading

    from nadare.core import artwork
    from nadare.gui.settings import SettingsPage

    config = _empty_config(tmp_path)
    config.add_profile("p", backend="wine", title="P")
    page = SettingsPage()
    page.load(config, "p")
    cover = tmp_path / "cover.png"
    cover.write_bytes(b"not-a-real-image")
    threads: list[bool] = []

    def fake_fetch(profile, title=None, kinds=("cover", "hero", "logo")):
        threads.append(threading.current_thread() is qapp.thread())
        return {"cover": cover}

    monkeypatch.setattr(artwork, "fetch_all", fake_fetch)
    try:
        page._fetch_art()
        assert _wait_for(qapp, lambda: page.background.text() != "")
        assert threads == [False], "the fetch must run off the GUI thread"
        assert page.background.text() == str(cover)
    finally:
        page.deleteLater()
        qapp.processEvents()


def test_settings_refuses_to_save_an_unknown_backend(tmp_path, qapp, monkeypatch):
    """Regression (06-7): saving must not silently rewrite an unknown backend."""
    from nadare.gui import settings as settings_mod

    config = _empty_config(tmp_path)
    config.add_profile("odd", backend="not-a-backend", title="Odd")
    page = settings_mod.SettingsPage()
    errors: list[str] = []
    monkeypatch.setattr(
        settings_mod.QMessageBox, "critical", lambda *args, **kwargs: errors.append(args[2])
    )
    try:
        page.load(config, "odd")
        assert page._backend_kind == "not-a-backend"
        page.save()
        assert config.profile("odd")["backend"] == "not-a-backend"
        assert errors, "an unknown backend must be refused on save"
    finally:
        page.deleteLater()
        qapp.processEvents()


def test_update_check_swallows_background_errors(tmp_path, qapp, monkeypatch):
    """Regression (06-11): an update-check failure must not die in excepthook."""
    import threading
    import time

    from nadare.core import updatecheck

    monkeypatch.setattr(
        updatecheck, "check_latest", lambda: (_ for _ in ()).throw(RuntimeError("boom"))
    )
    failed: list[object] = []
    monkeypatch.setattr(threading, "excepthook", lambda args: failed.append(args))
    window = MainWindow(_empty_config(tmp_path))
    try:
        deadline = time.monotonic() + 2
        while time.monotonic() < deadline:
            qapp.processEvents()
            if not any(t.name == "nadare-update-check" for t in threading.enumerate()):
                break
            time.sleep(0.01)
        assert failed == [], "the update check must catch its own errors"
    finally:
        window.close()
        window.deleteLater()
        qapp.processEvents()


def test_main_window_ignores_an_unsafe_language(tmp_path, qapp):
    """Regression (06-12): a hand-edited language is never used to build a path."""
    from nadare.gui import i18n

    config = _empty_config(tmp_path)
    config.data["language"] = "../../../etc/passwd"
    window = MainWindow(config)
    try:
        assert i18n.language() in i18n.available()
        assert i18n.language() == i18n.DEFAULT_LANGUAGE
    finally:
        window.close()
        window.deleteLater()
        qapp.processEvents()
        i18n.set_language(i18n.DEFAULT_LANGUAGE)


def test_worker_bridge_is_reaped_even_if_bookkeeping_raises(qapp, tmp_path):
    """Regression (round-4 NEW-3): a throwing finished slot must not leak the bridge."""
    from nadare.gui import workers

    config = _empty_config(tmp_path)
    config.add_profile("p", backend="wine")
    controller = workers.BackendController(config)

    class FakeWorker:
        pass

    bridge = workers._WorkerBridge(controller, "p", "status", FakeWorker())

    def boom(profile, worker):
        raise RuntimeError("malformed pending payload")

    controller._on_worker_finished = boom
    deleted: list[bool] = []
    bridge.deleteLater = lambda: deleted.append(True)
    bridge.on_finished()  # must neither raise nor skip deleteLater
    assert deleted == [True]

    controller.deleteLater()
    qapp.processEvents()


def test_game_view_art_callback_tolerates_a_destroyed_widget(qapp):
    """Regression (round-4 F-1): a decode delivered after its label was
    destroyed must be dropped, not raised out of the image callback."""
    from PyQt6 import sip

    from nadare.gui.game_view import GameView

    view = GameView()
    callbacks: list = []
    view.images.request = lambda paths, w, h, cb, crop=True, cover=False: callbacks.append(cb)
    try:
        view.set_profile("sample", {"title": "S", "cover": "/missing/cover.png"})
        assert callbacks, "the profile art decode must be scheduled"
        sip.delete(view.art_label)  # the widget is gone before the callback runs
        callbacks[0](None)  # must not raise
    finally:
        view.deleteLater()
        qapp.processEvents()


def test_game_view_media_callback_tolerates_a_destroyed_widget(qapp):
    """Regression (round-4 F-1): the hero callback has the same guard."""
    from PyQt6 import sip

    from nadare.gui.game_view import GameView

    view = GameView()
    view.set_profile("sample", {"backend": "arknights-yostar"})
    callbacks: list = []
    view.images.request = lambda paths, w, h, cb, crop=True: callbacks.append(cb)
    try:
        view.set_media("/missing/hero.png", [])
        assert callbacks, "the hero decode must be scheduled"
        sip.delete(view.hero)
        callbacks[0](None)  # must not raise
    finally:
        view.deleteLater()
        qapp.processEvents()
