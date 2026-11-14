# SPDX-License-Identifier: GPL-3.0-or-later
# ruff: noqa: F811 - tests take the fixtures imported below as arguments
"""GUI smoke: the news panel, console, store and Add game dialog (split out of test_gui_smoke.py,
#1248)."""

from __future__ import annotations

from _gui_smoke_helpers import (  # noqa: F401 - helpers and pytest fixtures, found by name
    _collect_garbage_between_tests,
    _empty_config,
    _wait_for,
    qapp,
)

from nadare.core.config import Config
from nadare.gui.main_window import MainWindow


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

        view.append_event(Event("log", "something to read"))  # the panel needs output to show
        assert not view.output_head.isHidden()
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


def test_game_view_without_news_source_keeps_the_output_header(tmp_path, qapp):
    """A backend with no official feed hides the section and leaves the log (folded, #993)."""
    from nadare.core.events import Event
    from nadare.core.news import NewsResult
    from nadare.gui.game_view import GameView

    view = GameView()
    try:
        view.set_profile("sample", {"backend": "wine"})
        view.append_event(Event("log", "something to read"))  # an empty log is hidden
        qapp.processEvents()
        assert view.news_frame.isHidden()
        assert not view.output_head.isHidden() and view.console.isHidden()

        view.set_news(NewsResult(items=[], available=False))
        qapp.processEvents()
        assert view.news_frame.isHidden()
        assert not view.output_head.isHidden() and view.console.isHidden()
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


def test_nikke_page_notice_explains_the_manual_install_clicks(tmp_path, qapp):
    from nadare.gui.game_view import GameView

    view = GameView()
    try:
        view.set_profile("nikke", {"backend": "launcher-nikke", "title": "NIKKE"})
        text = view.notice_label.text()
        assert "INSTALL" in text
        assert "miniloader" in text.lower()
        assert "installer window" in text.lower()
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


def test_sidebar_shows_the_version(tmp_path, qapp):
    """The running version is on screen, so a screenshot or bug report always carries it."""
    from nadare import __version__

    window = MainWindow(_empty_config(tmp_path))
    try:
        assert window.version_label.text() == f"v{__version__}"
        assert not window.version_label.isHidden()
        assert __version__ in window.beta_pill.toolTip()
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
        assert "not allowed" in dialog.error_label.text()

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
