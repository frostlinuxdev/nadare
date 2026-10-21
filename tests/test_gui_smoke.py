# SPDX-License-Identifier: GPL-3.0-or-later
"""Offscreen smoke test for the PyQt6 main window.

The whole module is skipped when PyQt6 cannot be imported, so the test suite
still runs headless on interpreters without the GUI extras.  Qt is forced onto
the ``offscreen`` platform before any Qt module is imported.
"""

from __future__ import annotations

import os

os.environ.setdefault("QT_QPA_PLATFORM", "offscreen")

import pytest

try:  # pragma: no cover - exercised only when PyQt6 is missing
    from PyQt6.QtWidgets import QApplication
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


def _empty_config(tmp_path):
    return Config.load(tmp_path / "config.json")


def test_main_window_constructs_and_closes(tmp_path, qapp):
    window = MainWindow(_empty_config(tmp_path))
    try:
        qapp.processEvents()
        assert window.windowTitle()
        assert window.centralWidget() is not None
        assert not hasattr(window, "profile_combo")
        assert window.stack.currentIndex() == 0  # library page
    finally:
        window.close()
        window.deleteLater()
        qapp.processEvents()


def test_main_window_first_run_takes_half_the_screen(tmp_path, qapp):
    window = MainWindow(_empty_config(tmp_path))
    try:
        area = window.screen().availableGeometry()
        # The minimum size wins on tiny screens (the offscreen test screen).
        assert window.width() == max(960, min(area.width() // 2, area.width()))
        assert window.height() == max(640, area.height())
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
        assert _wait_for(qapp, lambda: not controller.is_busy("p"), timeout=5)
        assert ran == ["launch"], "the queued user operation must run"
        assert "p" not in controller._pending
        assert "p" not in controller._workers
        assert ("respawn", True) in threads
        assert all(on_gui for _, on_gui in threads), threads
    finally:
        release.set()
        if controller._workers:
            controller.shutdown(wait_ms=1000)
        controller.deleteLater()
        qapp.processEvents()


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

    monkeypatch.setattr(czn_backend, "forget_saved_credential", fake_forget)
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


def test_game_view_news_panel_renders_and_opens_url(tmp_path, qapp, monkeypatch):
    """The news panel lists official items and clicks open the publisher URL."""
    from PyQt6.QtCore import QUrl

    from nadare.core.news import CREDIT, NewsResult
    from nadare.gui import game_view as game_view_mod
    from nadare.gui.game_view import GameView

    opened: list[QUrl] = []
    monkeypatch.setattr(
        game_view_mod.QDesktopServices, "openUrl", lambda url: opened.append(url) or True
    )

    view = GameView()
    view.set_profile("sample", {"backend": "stove-czn"})
    items = [
        {
            "id": "1",
            "title": "Patch notes",
            "summary": "changes",
            "category": "Updates",
            "published_at": 1_700_000_000_000,
            "image": None,
            "url": "https://page.onstove.com/chaoszeronightmare/en/view/1",
        }
    ]
    try:
        view.set_news(NewsResult(items=items))
        qapp.processEvents()
        assert not view.news_frame.isHidden(), "panel should show when items exist"
        assert not view.news_rows[0].isHidden()
        assert "Patch notes" in view.news_rows[0].title_label.text()
        assert view.news_credit.text() == CREDIT

        view.news_rows[0].clicked.emit(items[0]["url"])
        qapp.processEvents()
        assert [str(u.toString()) for u in opened] == [items[0]["url"]]

        # No items -> one muted line, no rows.
        view.set_news(NewsResult(items=[]))
        qapp.processEvents()
        assert view.news_rows == []
        assert "News unavailable" in view.news_hint.text()
    finally:
        view.deleteLater()
        qapp.processEvents()


def _news_items(count: int = 8) -> list[dict]:
    return [
        {
            "id": str(index + 1),
            "title": f"Official news item number {index + 1}",
            "summary": "",
            "category": "Updates",
            "published_at": 1_700_000_000_000,
            "image": None,
            "url": f"https://example.invalid/news/{index + 1}",
        }
        for index in range(count)
    ]


def test_game_view_news_grid_is_responsive(tmp_path, qapp):
    """Eight items become eight cards: three/two/one columns by width."""
    from nadare.core.news import NewsResult
    from nadare.gui.game_view import GameView

    view = GameView()
    view.set_profile("sample", {"backend": "stove-czn"})
    view.show()
    try:
        view.set_news(NewsResult(items=_news_items(8)))
        view.resize(1800, 900)
        qapp.processEvents()
        assert view.news_columns == 4
        assert view.news_grid.getItemPosition(4)[:2] == (1, 0)
        thumb = view.news_rows[0].thumb
        assert thumb.height() == min(240, round(thumb.width() * 9 / 16))

        view.resize(1200, 900)
        qapp.processEvents()
        assert len(view.news_rows) == 8
        assert view.news_grid.count() == 8
        assert view.news_columns == 3
        assert view.news_grid.getItemPosition(3)[:2] == (1, 0)
        assert view.news_grid.getItemPosition(4)[:2] == (1, 1)
        assert view.news_grid.getItemPosition(5)[:2] == (1, 2)

        view.resize(800, 900)
        qapp.processEvents()
        assert view.news_columns == 2
        assert view.news_grid.getItemPosition(2)[:2] == (1, 0)
        assert view.news_grid.getItemPosition(3)[:2] == (1, 1)

        view.resize(600, 900)
        qapp.processEvents()
        assert view.news_columns == 1
        assert view.news_grid.getItemPosition(1)[:2] == (1, 0)
    finally:
        view.deleteLater()
        qapp.processEvents()


def test_game_view_console_collapses_with_news_and_expands_on_busy_or_error(tmp_path, qapp):
    """The output console is folded away when news fills the page, but no
    operation or error may ever run with the log hidden."""
    from nadare.core.events import Event
    from nadare.core.news import NewsResult
    from nadare.gui.game_view import GameView

    view = GameView()
    view.set_profile("sample", {"backend": "stove-czn"})
    try:
        view.set_news(NewsResult(items=_news_items(2)))
        qapp.processEvents()
        assert view._console_expanded is False
        assert view.console.isHidden()
        assert view.console_toggle.text() == "Show output"

        view.console_toggle.click()
        assert view._console_expanded is True
        assert not view.console.isHidden()

        view.set_news(NewsResult(items=_news_items(2)))
        view.set_busy("launch")
        assert view._console_expanded is True
        assert not view.console.isHidden()

        view.set_profile("sample", {"backend": "stove-czn"})
        view.set_news(NewsResult(items=_news_items(2)))
        assert view.console.isHidden()
        view.append_event(Event("error", "boom"))
        qapp.processEvents()
        assert not view.console.isHidden()
    finally:
        view.deleteLater()
        qapp.processEvents()


def test_game_view_without_news_source_keeps_console_expanded(tmp_path, qapp):
    """A backend with no official feed hides the section and leaves the log."""
    from nadare.core.news import NewsResult
    from nadare.gui.game_view import GameView

    view = GameView()
    try:
        view.set_profile("sample", {"backend": "wine"})
        qapp.processEvents()
        assert view.news_frame.isHidden()
        assert not view.console.isHidden()

        view.set_news(NewsResult(items=[], available=False))
        qapp.processEvents()
        assert view.news_frame.isHidden()
        assert not view.console.isHidden()
    finally:
        view.deleteLater()
        qapp.processEvents()


def test_game_view_set_media_shows_hero_and_banners(tmp_path, qapp, monkeypatch):
    """Official media paints a full-bleed hero and a clickable banner strip."""
    from PyQt6.QtGui import QPixmap

    from nadare.gui import game_view as game_view_mod
    from nadare.gui.game_view import GameView

    opened: list[str] = []
    monkeypatch.setattr(
        game_view_mod.QDesktopServices, "openUrl", lambda url: opened.append(url.toString()) or True
    )
    hero = tmp_path / "hero.png"
    pix = QPixmap(640, 320)
    pix.fill()
    assert pix.save(str(hero))
    banners = []
    for index in range(2):
        banner = tmp_path / f"banner-{index}.png"
        pixb = QPixmap(360, 160)
        pixb.fill()
        assert pixb.save(str(banner))
        banners.append({"image_path": str(banner), "url": f"https://example.invalid/e/{index}"})

    view = GameView()
    view.set_profile("sample", {"backend": "arknights-yostar"})
    try:
        assert view.hero._source is None
        assert view.banners.isHidden()
        view.set_media(str(hero), banners)
        view.wait_for_images()
        qapp.processEvents()
        assert view.hero._source is not None
        assert not view.banners.isHidden()
        assert view.banners._row.count() == 2

        view.banners._row.itemAt(0).widget().clicked.emit("https://example.invalid/e/0")
        qapp.processEvents()
        assert opened == ["https://example.invalid/e/0"]

        view.set_profile("sample", {"backend": "arknights-yostar"})
        assert view.hero._source is None
        assert view.banners.isHidden()
    finally:
        view.deleteLater()
        qapp.processEvents()


def test_media_fetch_is_chained_after_news_not_started_alongside(tmp_path, qapp, monkeypatch):
    """Regression: news and media both started on status success; the controller
    allows one operation per profile, so media was refused for news-capable games."""
    config = Config.load(tmp_path / "config.json")
    config.add_profile("ark", backend="arknights-yostar", title="Ark", prefix=str(tmp_path / "p"))
    window = MainWindow(config)
    started: list[str] = []
    monkeypatch.setattr(
        window.controller, "start", lambda profile, op, payload=None: started.append(op) or True
    )
    popups: list[str] = []
    monkeypatch.setattr(
        "nadare.gui.main_window.QMessageBox.information", lambda *a, **k: popups.append(a[1])
    )
    try:
        window._on_succeeded("ark", "status", 0)
        assert started == ["news"]
        window._on_succeeded("ark", "news", 0)
        assert started == ["news", "media"]
        started.clear()
        window._on_failed("ark", "news", "offline")
        assert started == ["media"]  # media still runs when news fails
        assert popups == []
    finally:
        window.close()
        window.deleteLater()
        qapp.processEvents()


def test_game_view_shows_backend_page_notice_with_link(tmp_path, qapp):
    """A backend's ``page_notice`` (e.g. Stella Sora coming to Steam) is shown with its link."""
    from nadare.gui.game_view import GameView

    view = GameView()
    try:
        view.set_profile("ss", {"backend": "stellasora-yostar", "title": "Stella Sora"})
        assert not view.notice_label.isHidden()
        assert "Steam" in view.notice_label.text()
        assert "store.steampowered.com/app/4813850" in view.notice_label.text()
        view.set_profile("w", {"backend": "wine", "title": "Other"})
        assert view.notice_label.isHidden()
    finally:
        view.deleteLater()
        qapp.processEvents()


def test_page_notice_link_label_matches_the_backend(tmp_path, qapp):
    """The link text must describe the target (FGO links to setup notes, not a Steam page)."""
    from nadare.gui.game_view import GameView

    view = GameView()
    try:
        view.set_profile("ss", {"backend": "stellasora-yostar", "title": "Stella Sora"})
        assert ">Steam store page</a>" in view.notice_label.text()
        view.set_profile("fgo", {"backend": "fgo-lepton", "title": "FGO"})
        text = view.notice_label.text()
        assert ">Setup notes</a>" in text
        assert "Steam store page" not in text
    finally:
        view.deleteLater()
        qapp.processEvents()


def test_sidebar_shows_beta_pill(tmp_path, qapp):
    window = MainWindow(_empty_config(tmp_path))
    try:
        assert window.beta_pill.text() == "BETA"
        assert "expect bugs" in window.beta_pill.toolTip()
    finally:
        window.close()
        window.deleteLater()
        qapp.processEvents()


def test_branding_is_the_snowflake_and_cjk_mark(tmp_path, qapp):
    """The mark renders from the SVG and 雪崩 sits on the BETA row."""
    from nadare.gui.theme import app_icon, brand_pixmap

    window = MainWindow(_empty_config(tmp_path))
    try:
        assert window.brand_cjk.text() == "雪崩"
        assert not window.brand_mark_icon.pixmap().isNull()
        assert not window.windowIcon().isNull()

        icon = app_icon()
        assert not icon.isNull()
        for size in (16, 256):
            pix = brand_pixmap(size)
            assert not pix.isNull()
            assert pix.width() == size and pix.height() == size
    finally:
        window.close()
        window.deleteLater()
        qapp.processEvents()


def _wait_for(qapp, predicate, timeout=3.0):
    import time

    deadline = time.monotonic() + timeout
    while time.monotonic() < deadline:
        qapp.processEvents()
        if predicate():
            return True
        time.sleep(0.01)
    return False


def test_update_check_shows_a_button_that_opens_the_release(tmp_path, qapp, monkeypatch):
    from nadare.core import updatecheck
    from nadare.gui import main_window

    info = updatecheck.UpdateInfo("0.2.0", "https://forge.example/r/v0.2.0", "Nadare 0.2.0")
    monkeypatch.setattr(updatecheck, "check_latest", lambda: info)
    opened = []
    monkeypatch.setattr(main_window.QDesktopServices, "openUrl", lambda url: opened.append(url))
    window = MainWindow(_empty_config(tmp_path))
    try:
        assert _wait_for(qapp, lambda: not window.update_button.isHidden())
        assert "v0.2.0" in window.update_button.text()
        window.update_button.click()
        assert [url.toString() for url in opened] == ["https://forge.example/r/v0.2.0"]
    finally:
        window.close()
        window.deleteLater()
        qapp.processEvents()


def test_update_check_respects_the_setting(tmp_path, qapp, monkeypatch):
    from nadare.core import updatecheck

    def boom():
        raise AssertionError("update check must not run")

    monkeypatch.setattr(updatecheck, "check_latest", boom)
    config = _empty_config(tmp_path)
    config.data["check_updates"] = False
    window = MainWindow(config)
    try:
        qapp.processEvents()
        assert window.update_button.isHidden()
    finally:
        window.close()
        window.deleteLater()
        qapp.processEvents()


def test_add_game_dialog_rejects_unsafe_profile_name(tmp_path, qapp):
    """A hand-typed profile name must never smuggle a path traversal into the
    profiles directory; a clean (even non-ASCII) name still creates the profile."""
    from nadare.gui.add_game import AddGameDialog

    config = _empty_config(tmp_path)
    dialog = AddGameDialog(config)
    try:
        assert dialog.selected_kind(), "a game is preselected"
        dialog.name.setText("../escape")
        assert dialog.create_profile() is None
        assert config.profiles == {}
        assert "separators" in dialog.error_label.text()

        dialog.name.setText("My Game")
        assert dialog.create_profile() == "My Game"
        assert "My Game" in config.profiles
    finally:
        dialog.close()
        dialog.deleteLater()
        qapp.processEvents()


def test_add_game_dialog_default_name_is_always_valid(tmp_path, qapp):
    """Regression (N3): a non-ASCII or over-long title must still yield a
    generated default that the profile-name validator accepts."""
    from nadare.core.config import MAX_PROFILE_NAME, valid_profile_name
    from nadare.gui.add_game import AddGameDialog, _slugify

    config = _empty_config(tmp_path)
    dialog = AddGameDialog(config)
    try:
        assert _slugify("ゲーム") == ""
        assert _slugify("A" * 200) == "a" * MAX_PROFILE_NAME
        for title in ("ゲーム", "Café", "A" * 200, "Fate/Grand Order (NA)"):
            name = dialog._default_name(title)
            assert name, title
            assert valid_profile_name(name), (title, name)

        # A full-length base that collides still stays within the length cap.
        dialog.config.add_profile("a" * MAX_PROFILE_NAME, backend="wine")
        disambiguated = dialog._default_name("a" * MAX_PROFILE_NAME)
        assert disambiguated != "a" * MAX_PROFILE_NAME
        assert valid_profile_name(disambiguated)
    finally:
        dialog.close()
        dialog.deleteLater()
        qapp.processEvents()


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
    monkeypatch.setattr(yostar, "load_media", lambda source, game_tag=None: object())
    monkeypatch.setattr(
        yostar, "cache_media", lambda media, slug: {"hero": "/cache/hero.png", "banners": []}
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

    monkeypatch.setattr(mw, "AccountNoticeDialog", _Accept)
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


# ---------------------------------------------------------------------------
# Audit round 7 regressions (G-3/G-4/G-7/G-8/G-9)
# ---------------------------------------------------------------------------
def test_busy_badge_reflects_the_operation_not_installing(qapp):
    """G-3: stop/uninstall/set_remember/logout must not show the INSTALLING pill."""
    from nadare.gui.game_view import GameView

    view = GameView()
    view.set_profile("sample", {})
    try:
        for operation, label in (
            ("stop", "STOPPING"),
            ("uninstall", "UNINSTALLING"),
            ("set_remember", "SAVING"),
            ("logout", "LOGGING OUT"),
        ):
            view.set_busy(operation)
            assert view.badge.text() == label, operation
    finally:
        view.deleteLater()
        qapp.processEvents()


def test_busy_badge_resets_after_fail_and_cancel(qapp, monkeypatch):
    """G-4: a failed or cancelled operation must drop the busy pill."""
    from nadare.core.backends import Status
    from nadare.gui import game_view as game_view_mod
    from nadare.gui.game_view import GameView

    monkeypatch.setattr(game_view_mod.QMessageBox, "warning", lambda *a, **k: None)
    view = GameView()
    view.set_profile("sample", {})
    view.set_status(Status("installed"))
    try:
        assert view.badge.text() == "READY"
        view.set_busy("install")
        assert view.badge.text() == "INSTALLING"
        view.fail("boom")
        assert view.badge.text() == "READY", "a failed op kept the busy pill"

        view.set_busy("launch")
        assert view.badge.text() == "RUNNING"
        view.cancelled()
        assert view.badge.text() == "READY", "a cancelled op kept the busy pill"
    finally:
        view.deleteLater()
        qapp.processEvents()


def test_remember_card_follows_the_profiles_backend(tmp_path, qapp):
    """G-7: the Account card shows for stove-czn and stays hidden for wine."""
    from nadare.gui.settings import SettingsPage

    config = Config.load(tmp_path / "config.json")
    config.add_profile("winey", backend="wine")
    config.add_profile("czn", backend="stove-czn")
    page = SettingsPage()
    try:
        page.load(config, "winey")
        assert page.account_card.isHidden(), "wine has no Remember me"
        page.load(config, "czn")
        assert not page.account_card.isHidden(), "stove-czn supports Remember me"
        page.load(config, "winey")
        assert page.account_card.isHidden()
    finally:
        page.deleteLater()
        qapp.processEvents()


def test_settings_has_no_backend_picker(tmp_path, qapp):
    """The backend is fixed when a game is added; settings must not offer to change it."""
    from PyQt6.QtWidgets import QComboBox

    from nadare.core.backends import kinds
    from nadare.gui.settings import SettingsPage

    config = Config.load(tmp_path / "config.json")
    config.add_profile("czn", backend="stove-czn")
    page = SettingsPage()
    try:
        page.load(config, "czn")
        assert not hasattr(page, "backend")
        offered = {
            combo.itemText(i)
            for combo in page.findChildren(QComboBox)
            for i in range(combo.count())
        }
        assert not offered & set(kinds()), "a backend kind is selectable in settings"
        page.save()
        assert config.profile("czn")["backend"] == "stove-czn"
    finally:
        page.deleteLater()
        qapp.processEvents()


def test_login_qr_is_restored_when_returning_to_a_busy_login(tmp_path, qapp, monkeypatch):
    """G-8: leaving and reopening a busy login must bring the QR back."""
    from PyQt6.QtGui import QPixmap

    from nadare.core.events import Event
    from nadare.gui import workers

    monkeypatch.setattr(
        workers.BackendController, "start", lambda self, profile, op, payload=None: True
    )
    config = Config.load(tmp_path / "config.json")
    config.data["check_updates"] = False
    config.add_profile("czn", backend="stove-czn", title="CZN")
    config.add_profile("other", backend="wine", title="Other")
    window = MainWindow(config, profile="czn")
    qr = tmp_path / "qr.png"
    pix = QPixmap(96, 96)
    pix.fill()
    assert pix.save(str(qr))
    try:
        window._busy["czn"] = "login"
        window._on_event("czn", Event("step", "Scan", data={"png": str(qr)}))
        window.show_library()
        window.open_game("czn")
        qapp.processEvents()
        assert not window.game.qr_frame.isHidden(), "the QR must survive a revisit"
        assert not window.game.qr_label.pixmap().isNull()

        window._on_event("czn", Event("done", "Logged in to STOVE"))
        window._busy.pop("czn", None)
        window.show_library()
        window.open_game("czn")
        qapp.processEvents()
        assert window.game.qr_frame.isHidden(), "a finished login must not restore a QR"
    finally:
        window.close()
        window.deleteLater()
        qapp.processEvents()


def test_remember_notice_reaches_the_user_after_settings_save(tmp_path, qapp, monkeypatch):
    """G-9: the no-keyring notice must be shown, not dropped on a hidden page."""
    from nadare.backends.czn.backend import REMEMBER_NOTICE
    from nadare.core.events import Event
    from nadare.gui import main_window as mw
    from nadare.gui import workers

    monkeypatch.setattr(
        workers.BackendController, "start", lambda self, profile, op, payload=None: True
    )
    config = Config.load(tmp_path / "config.json")
    config.data["check_updates"] = False
    config.add_profile("czn", backend="stove-czn", title="CZN", remember_me=True)
    window = MainWindow(config)
    shown: list[tuple] = []
    monkeypatch.setattr(mw.QMessageBox, "information", lambda *a, **k: shown.append(a))
    try:
        window._on_settings_saved("czn")
        window._on_event("czn", Event("status", REMEMBER_NOTICE))
        window._on_succeeded("czn", "set_remember", 0)
        assert shown, "the no-keyring notice must reach the user"
        assert shown[0][2] == REMEMBER_NOTICE
    finally:
        window.close()
        window.deleteLater()
        qapp.processEvents()


def test_message_boxes_are_themed():
    """Dialogs must not fall back to the system's light palette on the dark app."""
    from nadare.gui.theme import PANEL, STYLESHEET

    assert "QMessageBox" in STYLESHEET
    assert f"background: {PANEL}" in STYLESHEET.split("QMessageBox", 1)[1][:200]


def test_remove_game_from_the_gui_drops_profile_and_keeps_files(tmp_path, qapp, monkeypatch):
    monkeypatch.setenv("XDG_DATA_HOME", str(tmp_path / "data"))
    monkeypatch.setenv("XDG_CONFIG_HOME", str(tmp_path / "config"))
    install = tmp_path / "games" / "g"
    install.mkdir(parents=True)
    (install / "game.exe").write_text("x")
    cfg = _empty_config(tmp_path)
    cfg.add_profile("g", backend="wine", title="G", install_dir=str(install))
    cfg.add_profile("h", backend="wine", title="H")
    cfg.save()
    window = MainWindow(cfg)
    try:
        # The settings page exposes the action and forwards the profile name.
        got = []
        window.settings.remove_requested.disconnect()  # no modal confirmation in the test
        window.settings.remove_requested.connect(got.append)
        window.settings.load(cfg, "g")
        window.settings.remove_button.click()
        assert got == ["g"]

        window._remove_game("g", False)
        assert _wait_for(qapp, lambda: "g" not in cfg.profiles)
        assert "h" in cfg.profiles
        assert (install / "game.exe").exists()
        assert "g" not in Config.load(tmp_path / "config.json").profiles
        assert "g" not in window._busy
    finally:
        window.close()
        window.deleteLater()
        qapp.processEvents()


def test_remove_game_can_delete_the_install_dir_from_the_gui(tmp_path, qapp, monkeypatch):
    monkeypatch.setenv("XDG_DATA_HOME", str(tmp_path / "data"))
    monkeypatch.setenv("XDG_CONFIG_HOME", str(tmp_path / "config"))
    monkeypatch.setenv("HOME", str(tmp_path / "home"))
    (tmp_path / "home").mkdir(exist_ok=True)
    install = tmp_path / "games" / "g"
    install.mkdir(parents=True)
    (install / "game.exe").write_text("x")
    other = tmp_path / "games" / "h"
    other.mkdir()
    (other / "keep.exe").write_text("x")
    cfg = _empty_config(tmp_path)
    cfg.add_profile("g", backend="wine", title="G", install_dir=str(install))
    cfg.add_profile("h", backend="wine", title="H", install_dir=str(other))
    cfg.save()
    window = MainWindow(cfg)
    try:
        window._remove_game("g", False, ["install_dir"])
        assert _wait_for(qapp, lambda: "g" not in cfg.profiles)
        assert not install.exists()
        assert (other / "keep.exe").exists()
    finally:
        window.close()
        window.deleteLater()
        qapp.processEvents()


def test_a_refused_tree_keeps_the_profile_in_the_gui(tmp_path, qapp, monkeypatch):
    monkeypatch.setenv("XDG_DATA_HOME", str(tmp_path / "data"))
    monkeypatch.setenv("XDG_CONFIG_HOME", str(tmp_path / "config"))
    monkeypatch.setenv("HOME", str(tmp_path / "home"))
    (tmp_path / "home").mkdir(exist_ok=True)
    shared = tmp_path / "games" / "shared"
    shared.mkdir(parents=True)
    cfg = _empty_config(tmp_path)
    cfg.add_profile("g", backend="wine", title="G", install_dir=str(shared))
    cfg.add_profile("h", backend="wine", title="H", install_dir=str(shared))
    cfg.save()
    window = MainWindow(cfg)
    monkeypatch.setattr(
        "nadare.gui.main_window.QMessageBox.warning", lambda *a, **k: None
    )
    try:
        window._remove_game("g", False, ["install_dir"])
        assert _wait_for(qapp, lambda: "g" not in window._busy)
        assert "g" in cfg.profiles and shared.exists()
    finally:
        window.close()
        window.deleteLater()
        qapp.processEvents()


def test_game_removed_drops_cached_snapshot(tmp_path, qapp):
    window = MainWindow(_empty_config(tmp_path))
    try:
        window._snapshots["g"] = {"news": object()}
        window._remember_notice["g"] = "x"
        window._remember_watching.add("g")
        window._on_game_removed("g", None)
        assert "g" not in window._snapshots
        assert "g" not in window._remember_notice
        assert "g" not in window._remember_watching
    finally:
        window.close()
        window.deleteLater()
        qapp.processEvents()


def _confirm_window(tmp_path, monkeypatch):
    from PyQt6.QtWidgets import QMessageBox

    monkeypatch.setenv("XDG_DATA_HOME", str(tmp_path / "data"))
    monkeypatch.setenv("XDG_CONFIG_HOME", str(tmp_path / "config"))
    install = tmp_path / "games" / "g"
    install.mkdir(parents=True)
    shared = tmp_path / "games" / "shared"
    shared.mkdir(parents=True)
    cfg = _empty_config(tmp_path)
    cfg.add_profile("g", backend="wine", title="G", install_dir=str(install), prefix=str(shared))
    cfg.add_profile("h", backend="wine", title="H", install_dir=str(shared))
    cfg.save()
    window = MainWindow(cfg)
    calls = []
    window._remove_game = lambda *a, **k: calls.append(a)
    return window, calls, QMessageBox


def test_remove_dialog_cancel_does_nothing(tmp_path, qapp, monkeypatch):
    window, calls, QMessageBox = _confirm_window(tmp_path, monkeypatch)
    from PyQt6.QtWidgets import QCheckBox

    seen = {}

    def fake_exec(self):
        seen["default"] = self.defaultButton()
        seen["boxes"] = {c.text(): c.isEnabled() for c in self.findChildren(QCheckBox)}
        return QMessageBox.StandardButton.Cancel

    monkeypatch.setattr(QMessageBox, "exec", fake_exec)
    try:
        window._on_remove_requested("g")
        assert calls == []
        assert seen["default"] is not None
        enabled = [v for k, v in seen["boxes"].items() if "kept:" not in k and "install" in k]
        disabled = [v for k, v in seen["boxes"].items() if "kept:" in k]
        assert enabled == [True]  # install_dir is safe
        assert disabled == [False]  # prefix is shared with h: disabled, with the reason
        assert "g" in window.config.profiles
    finally:
        window.close()
        window.deleteLater()
        qapp.processEvents()


def test_remove_dialog_default_button_is_cancel(tmp_path, qapp, monkeypatch):
    window, calls, QMessageBox = _confirm_window(tmp_path, monkeypatch)
    seen = {}

    def fake_exec(self):
        seen["default"] = self.defaultButton().text()
        seen["cancel"] = self.button(QMessageBox.StandardButton.Cancel).text()
        return QMessageBox.StandardButton.Cancel

    monkeypatch.setattr(QMessageBox, "exec", fake_exec)
    try:
        window._on_remove_requested("g")
        assert seen["default"] == seen["cancel"]
    finally:
        window.close()
        window.deleteLater()
        qapp.processEvents()


def test_remove_dialog_second_confirmation_cancel_does_nothing(tmp_path, qapp, monkeypatch):
    window, calls, QMessageBox = _confirm_window(tmp_path, monkeypatch)
    from PyQt6.QtWidgets import QCheckBox

    def fake_exec(self):
        for check in self.findChildren(QCheckBox):
            if check.isEnabled():
                check.setChecked(True)
        return QMessageBox.StandardButton.Yes

    asked = []

    def fake_warning(*args):
        asked.append(args[1])
        return QMessageBox.StandardButton.Cancel

    monkeypatch.setattr(QMessageBox, "exec", fake_exec)
    monkeypatch.setattr(QMessageBox, "warning", staticmethod(fake_warning))
    try:
        window._on_remove_requested("g")
        assert asked == ["Delete game files?"]
        assert calls == []
    finally:
        window.close()
        window.deleteLater()
        qapp.processEvents()


def test_remove_profile_restored_on_save_failure(tmp_path, qapp, monkeypatch):
    from PyQt6.QtWidgets import QMessageBox

    cfg = _empty_config(tmp_path)
    cfg.add_profile("g", backend="wine", title="G")
    cfg.add_profile("h", backend="wine", title="H")
    cfg.save()
    window = MainWindow(cfg)
    shown = []
    monkeypatch.setattr(
        QMessageBox, "critical", staticmethod(lambda *a: shown.append(a[2]))
    )
    monkeypatch.setattr(QMessageBox, "warning", staticmethod(lambda *a: None))
    monkeypatch.setattr(cfg, "save", lambda *a, **k: (_ for _ in ()).throw(PermissionError("ro")))
    try:
        active = cfg.active
        window._on_game_removed("g", None)
        assert "g" in cfg.profiles and "h" in cfg.profiles
        assert cfg.active == active
        assert shown and "ro" in shown[0]
        assert "Removed" not in window.side_status.text()
    finally:
        window.close()
        window.deleteLater()
        qapp.processEvents()

import time  # noqa: E402

from PyQt6.QtWidgets import QCheckBox, QMessageBox  # noqa: E402


# ---- verification additions (opt-out and remove-confirmation guards) ----------
def _update_threads():
    import threading

    return [t for t in threading.enumerate() if t.name == "nadare-update-check"]

def _close(window, qapp):
    window.close()
    window.deleteLater()
    qapp.processEvents()


def _settle(qapp, seconds=1.0):
    """Give a (wrongly) started background removal time to finish."""
    deadline = time.monotonic() + seconds
    while time.monotonic() < deadline:
        qapp.processEvents()
        time.sleep(0.02)


def test_update_check_opt_out_never_starts_the_check(tmp_path, qapp, monkeypatch):
    from nadare.core import updatecheck

    calls: list[int] = []
    monkeypatch.setattr(updatecheck, "check_latest", lambda *a, **k: calls.append(1))
    cfg = _empty_config(tmp_path)
    cfg.data["check_updates"] = False
    window = MainWindow(cfg)
    try:
        for t in _update_threads():
            t.join(2)
        qapp.processEvents()
        assert calls == []
    finally:
        _close(window, qapp)


def test_update_check_runs_when_enabled(tmp_path, qapp, monkeypatch):
    from nadare.core import updatecheck

    calls: list[int] = []
    monkeypatch.setattr(updatecheck, "check_latest", lambda *a, **k: calls.append(1))
    window = MainWindow(_empty_config(tmp_path))
    try:
        deadline = time.monotonic() + 3
        while not calls and time.monotonic() < deadline:
            qapp.processEvents()
            time.sleep(0.01)
        assert calls == [1]
    finally:
        _close(window, qapp)


def _window_with_trees(tmp_path, qapp, monkeypatch):
    monkeypatch.setenv("XDG_DATA_HOME", str(tmp_path / "data"))
    monkeypatch.setenv("XDG_CONFIG_HOME", str(tmp_path / "config"))
    install = tmp_path / "games" / "g"
    install.mkdir(parents=True)
    (install / "game.exe").write_text("x")
    cfg = _empty_config(tmp_path)
    cfg.add_profile("g", backend="wine", title="G", install_dir=str(install))
    cfg.save()
    return MainWindow(cfg), cfg, install


def test_cancel_in_remove_dialog_removes_nothing(tmp_path, qapp, monkeypatch):
    window, cfg, install = _window_with_trees(tmp_path, qapp, monkeypatch)
    try:
        monkeypatch.setattr(QMessageBox, "exec", lambda self: QMessageBox.StandardButton.Cancel)
        window._on_remove_requested("g")
        _settle(qapp)
        assert "g" in cfg.profiles
        assert (install / "game.exe").exists()
    finally:
        _close(window, qapp)


def test_second_confirmation_cancel_keeps_the_game_files(tmp_path, qapp, monkeypatch):
    window, cfg, install = _window_with_trees(tmp_path, qapp, monkeypatch)
    try:
        def fake_exec(self):
            for check in self.findChildren(QCheckBox):
                if "install folder" in check.text():
                    check.setChecked(True)
            return QMessageBox.StandardButton.Yes

        asked = []

        def fake_warning(*args, **kwargs):
            asked.append(args[2])
            return QMessageBox.StandardButton.Cancel

        monkeypatch.setattr(QMessageBox, "exec", fake_exec)
        monkeypatch.setattr(QMessageBox, "warning", fake_warning)
        window._on_remove_requested("g")
        _settle(qapp)
        assert len(asked) == 1 and "permanently deletes" in asked[0]
        assert "g" in cfg.profiles
        assert (install / "game.exe").exists()
    finally:
        _close(window, qapp)


def test_unsafe_tree_checkbox_is_disabled_and_cancel_is_default(tmp_path, qapp, monkeypatch):
    monkeypatch.setenv("XDG_DATA_HOME", str(tmp_path / "data"))
    monkeypatch.setenv("XDG_CONFIG_HOME", str(tmp_path / "config"))
    home = tmp_path / "home"
    home.mkdir(exist_ok=True)
    monkeypatch.setenv("HOME", str(home))
    cfg = _empty_config(tmp_path)
    cfg.add_profile("g", backend="wine", title="G", install_dir=str(home))
    cfg.save()
    window = MainWindow(cfg)
    seen = {}
    try:
        def fake_exec(self):
            seen["checks"] = [(c.isEnabled(), c.isChecked()) for c in self.findChildren(QCheckBox)
                              if "install folder" in c.text()]
            seen["default"] = self.defaultButton().text() if self.defaultButton() else None
            seen["cancel"] = self.button(QMessageBox.StandardButton.Cancel).text()
            return QMessageBox.StandardButton.Cancel

        monkeypatch.setattr(QMessageBox, "exec", fake_exec)
        window._on_remove_requested("g")
        assert seen["checks"] == [(False, False)]
        assert seen["default"] == seen["cancel"]
    finally:
        _close(window, qapp)


def test_game_view_shows_login_state(tmp_path, qapp):
    """A visible Logged in / Not logged in label, only for backends that report it."""
    from nadare.core.backends import Status
    from nadare.gui.game_view import GameView

    view = GameView()
    view.set_profile("sample", {"backend": "stove-czn"})
    try:
        assert view.account_label.isHidden()
        view.set_status(Status("installed", data={"logged_in": True}))
        assert not view.account_label.isHidden()
        assert view.account_label.text() == "Logged in"
        view.set_status(Status("installed", data={"logged_in": False}))
        assert view.account_label.text() == "Not logged in"
        view.set_status(Status("not_installed", data={"logged_in": False}))
        assert view.account_label.isHidden()
        view.set_status(Status("installed", data={}))
        assert view.account_label.isHidden()
    finally:
        view.deleteLater()
