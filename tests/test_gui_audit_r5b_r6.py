# SPDX-License-Identifier: GPL-3.0-or-later
# ruff: noqa: F811 - tests take the fixtures imported below as arguments
"""GUI audit rounds 5b and 6: the notice gate, shutdown and later regressions (split out of
test_gui_smoke.py, #1248)."""

from __future__ import annotations

from _gui_smoke_helpers import (  # noqa: F401 - helpers and pytest fixtures, found by name
    _collect_garbage_between_tests,
    _empty_config,
    qapp,
)
from _main_window_patch import patch_main_window

from nadare.gui.main_window import MainWindow


# ---------------------------------------------------------------------------
# Audit round 5b: app/GUI notice gate and shutdown (06 / 04-L3)
# ---------------------------------------------------------------------------
def test_update_is_gated_by_the_account_notice(tmp_path, qapp, monkeypatch):
    """04-L3 GUI: an un-acked profile must be prompted before an update."""
    from nadare.gui import main_window as mw
    from nadare.gui import workers

    assert "update" in mw.NOTICE_OPERATIONS
    starts: list[str] = []
    monkeypatch.setattr(
        workers.BackendController,
        "start",
        lambda self, profile, op, payload=None: starts.append(op) or True,
    )
    config = _empty_config(tmp_path)
    config.add_profile("gfgo", backend="fgo-lepton", title="FGO")
    window = MainWindow(config, profile="gfgo")
    starts.clear()
    shown: list[str] = []

    class _Accept:
        def __init__(self, notice, parent=None):
            shown.append(notice)

        def exec(self):
            return mw.QDialog.DialogCode.Accepted

    patch_main_window(monkeypatch, "AccountNoticeDialog", _Accept)
    try:
        window._on_game_action("update")
        assert shown, "an un-acked update must show the notice modal"
        assert starts == ["update"]
        assert config.profile("gfgo")["accept_account_notice"] is True
    finally:
        window.close()
        window.deleteLater()
        qapp.processEvents()


def test_cli_update_is_gated_by_the_account_notice(tmp_path, monkeypatch, capsys):
    """04-L3 CLI: ``update`` matches install/launch (notice + opt-in flag)."""
    from nadare.app import run_cli
    from nadare.backends.fgo.backend import FgoLeptonBackend
    from nadare.core.config import Config

    config = Config.load(tmp_path / "config.json")
    config.add_profile("gfgo", backend="fgo-lepton", title="FGO")
    config.save()

    code = run_cli(["-c", str(config.path), "update", "-p", "gfgo"])
    captured = capsys.readouterr()
    assert code == 2
    assert "BEFORE YOU PLAY" in captured.out
    assert "--accept-account-notice" in captured.err

    calls: list[str] = []
    monkeypatch.setattr(FgoLeptonBackend, "update", lambda self: calls.append(self.ctx.name))
    code = run_cli(["-c", str(config.path), "update", "-p", "gfgo", "--accept-account-notice"])
    capsys.readouterr()
    assert code == 0
    assert calls == ["gfgo"]
    assert Config.load(config.path).profile("gfgo")["accept_account_notice"] is True


def test_single_instance_exit_status_is_never_a_silent_success():
    """06-10: only "another instance activated" is 0; a dead lock is non-zero."""
    from nadare.gui import EXIT_ACTIVATED_OTHER, EXIT_NO_SINGLE_INSTANCE, _single_instance_exit

    class _Instance:
        def __init__(self, status):
            self.status = status

    assert EXIT_ACTIVATED_OTHER == 0
    assert EXIT_NO_SINGLE_INSTANCE != 0
    activated = _Instance("another instance is already running")
    assert _single_instance_exit(activated) == EXIT_ACTIVATED_OTHER
    for status in ("", "could not create the single-instance lock '/run/x.sock'"):
        assert _single_instance_exit(_Instance(status)) == EXIT_NO_SINGLE_INSTANCE


def test_hard_exit_waits_for_a_mutating_worker(qapp):
    """06-5: a still-running install/update is waited out before os._exit."""
    from nadare.gui import _wait_for_mutating_workers

    class _Controller:
        def __init__(self, mutating):
            self._mutating = list(mutating)
            self.shutdowns: list[int] = []

        def mutating_profiles(self):
            return list(self._mutating)

        def shutdown(self, wait_ms=0):
            self.shutdowns.append(wait_ms)

    idle = _Controller([])
    assert _wait_for_mutating_workers(idle) is False
    assert idle.shutdowns == []

    busy = _Controller(["p"])
    assert _wait_for_mutating_workers(busy, wait_ms=250) is True
    assert busy.shutdowns == [250]

    assert _wait_for_mutating_workers(None) is False

    class _Boom:
        def mutating_profiles(self):
            raise RuntimeError("workers gone")

    assert _wait_for_mutating_workers(_Boom()) is False


# ---------------------------------------------------------------------------
# Audit round 6 regressions (02-NV-1 / 06-N-1)
# ---------------------------------------------------------------------------
def test_free_space_hints_degrade_on_a_statvfs_failure(tmp_path, qapp, monkeypatch):
    """Regression (02-NV-1): ``free_bytes`` raises ``BackendError`` now; the GUI
    hint helpers must catch it and degrade to an empty hint rather than let it
    escape a Qt slot."""
    from nadare.core import diskspace
    from nadare.core.backends import BackendError
    from nadare.gui.add_game import AddGameDialog
    from nadare.gui.settings import SettingsPage

    def boom(_path):
        raise BackendError("cannot read free space")

    monkeypatch.setattr(diskspace, "free_bytes", boom)

    dialog = AddGameDialog(_empty_config(tmp_path))
    try:
        dialog.folder.setText(str(tmp_path))
        dialog.prefix.setText(str(tmp_path))
        dialog._update_free_space()  # must not raise out of the slot
        assert dialog._free_hint(str(tmp_path)) == ""
        assert dialog.folder_free.text() == ""
        assert dialog.prefix_free.text() == ""
    finally:
        dialog.close()
        dialog.deleteLater()
        qapp.processEvents()

    config = _empty_config(tmp_path)
    config.add_profile("p", backend="wine")
    page = SettingsPage()
    page.load(config, "p")
    try:
        assert page._free_hint(str(tmp_path)) == ""
    finally:
        page.deleteLater()
        qapp.processEvents()


def test_settings_save_holds_the_config_lock(tmp_path, qapp, monkeypatch):
    """Regression (06-N-1): a GUI settings save must take ``config.locked()``,
    the same lock the worker-side ``BackendContext.set`` uses, so a concurrent
    worker write cannot corrupt or reject the save."""
    import threading

    from nadare.core.backends import BackendContext
    from nadare.core.config import Config
    from nadare.gui.settings import SettingsPage

    config = _empty_config(tmp_path)
    config.add_profile("p", backend="wine", title="P")
    config.save()
    ctx = BackendContext(name="p", profile=config.profile("p"), config=config)

    locked_threads: list[threading.Thread] = []
    real_locked = Config.locked

    def spy(self):
        locked_threads.append(threading.current_thread())
        return real_locked(self)

    monkeypatch.setattr(Config, "locked", spy)

    page = SettingsPage()
    page.load(config, "p")
    errors: list[BaseException] = []

    def worker() -> None:
        try:
            for index in range(60):
                ctx.set(f"k{index}", index)
        except BaseException as exc:  # noqa: BLE001 - collected for the assertion
            errors.append(exc)

    thread = threading.Thread(target=worker)
    thread.start()
    try:
        for index in range(40):
            page.title.setText(f"title-{index}")
            page.save()  # must not raise or be refused
    finally:
        thread.join()
        page.deleteLater()
        qapp.processEvents()

    assert not errors
    assert any(t is threading.current_thread() for t in locked_threads), (
        "SettingsPage.save must take the config lock on the GUI thread"
    )
    reloaded = Config.load(config.path)
    assert reloaded.profile("p")["title"] == "title-39"
    for index in range(60):
        assert reloaded.profile("p")[f"k{index}"] == index




def test_login_qr_is_an_in_app_overlay_with_a_cancel_button(tmp_path, qapp):
    """The QR is a centred card inside the page (not a new window), scaled to a
    scannable size; its button cancels, while finishing the login must not."""
    from PyQt6.QtGui import QPixmap

    from nadare.core.events import Event
    from nadare.gui.game_view import GameView, _QrPanel

    view = GameView()
    view.resize(1000, 700)
    view.show()
    view.set_profile("sample", {})
    qr = tmp_path / "qr.png"
    pix = QPixmap(96, 96)
    pix.fill()
    assert pix.save(str(qr))
    cancelled: list[bool] = []
    view.cancel_requested.connect(lambda: cancelled.append(True))
    try:
        view.append_event(Event("step", "Scan", data={"png": str(qr)}))
        qapp.processEvents()
        assert not view.qr_frame.isHidden()
        assert not view.qr_frame.isWindow(), "the QR must not open a new window"
        assert view.qr_label.pixmap().width() == _QrPanel.QR_SIZE
        centre = view.qr_frame.geometry().center()
        assert abs(centre.x() - view.width() // 2) <= 2

        view.append_event(Event("done", "Logged in to STOVE"))
        qapp.processEvents()
        assert view.qr_frame.isHidden()
        assert cancelled == [], "finishing the login must not look like a cancel"

        view.append_event(Event("step", "Scan", data={"png": str(qr)}))
        qapp.processEvents()
        view.qr_frame.cancel_button.click()
        assert cancelled == [True]
    finally:
        view.deleteLater()
        qapp.processEvents()


def test_news_hides_while_an_operation_runs_and_returns_after(qapp):
    """An install/login gets the room: the news section steps aside and is
    restored when the operation ends."""
    from nadare.gui.game_view import GameView

    view = GameView()
    view.resize(1000, 700)
    view.show()
    view.set_profile("sample", {})
    try:
        view._set_news_visible(True)
        assert not view.news_frame.isHidden()
        view.set_busy("install")
        assert view.news_frame.isHidden()
        view._set_news_visible(True)  # a late refresh must not pop it back
        assert view.news_frame.isHidden()
        view.finish("install", 0)
        assert not view.news_frame.isHidden()
    finally:
        view.deleteLater()
        qapp.processEvents()
