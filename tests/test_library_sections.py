# SPDX-License-Identifier: GPL-3.0-or-later
"""The compact widget library, including legacy configuration and launch transitions."""

import json
from datetime import UTC, datetime, timedelta

import pytest
from PyQt6.QtCore import QEvent, QRect, Qt
from PyQt6.QtGui import QFocusEvent
from PyQt6.QtTest import QTest
from PyQt6.QtWidgets import QApplication, QPushButton

from nadare.core.backends import Status
from nadare.core.config import Config
from nadare.gui import library, theme
from nadare.gui.main_window import MainWindow


@pytest.fixture(scope="module")
def qapp():
    return QApplication.instance() or QApplication([])


def stamp(days=0, hours=0):
    return (datetime.now(UTC) - timedelta(days=days, hours=hours)).isoformat()


@pytest.fixture
def page(qapp):
    page = library.LibraryPage()
    yield page
    page.wait_for_images()
    page.close()
    page.deleteLater()
    qapp.processEvents()


def test_recent_order_and_cap(page):
    page.refresh({str(i): {"last_played": stamp(hours=i + 2)} for i in range(6)})
    assert [t.profile for t in page.recent_tiles] == ["0", "1", "2", "3"]
    assert not page.recent_section.isHidden()


def test_empty_strips_and_empty_call_to_action(page):
    page.refresh({})
    assert page.recent_section.isHidden()
    assert page.favourites_section.isHidden()
    assert not page.empty_state.isHidden()
    assert "No games yet" in page.subtitle.text()
    seen = []
    page.add_game.connect(lambda: seen.append(True))
    page.empty_state.findChild(QPushButton).click()
    page.add_button.click()
    assert seen == [True, True]
    assert page._flow.count() == 0


def test_search_title_id_escape_and_strips(page, qapp):
    page.refresh(
        {
            "id-one": {"title": "Alpha", "favourite": True, "last_played": stamp()},
            "id-two": {"title": "Beta"},
        }
    )
    page.search.setText("ALp")
    assert not page._cards["id-one"].isHidden()
    assert page._cards["id-two"].isHidden()
    assert page.recent_section.isHidden() and page.favourites_section.isHidden()
    page.search.setText("ID-TWO")
    assert page._cards["id-one"].isHidden()
    assert not page._cards["id-two"].isHidden()
    page.search.setText("missing")
    assert not page.no_matches.isHidden()
    assert page.no_matches.text() == "No game matches"
    QTest.keyClick(page.search, Qt.Key.Key_Escape)
    assert page.search.text() == ""
    assert page.no_matches.isHidden()
    assert all(not c.isHidden() for c in page._cards.values())
    assert not page.recent_section.isHidden() and not page.favourites_section.isHidden()


@pytest.mark.parametrize(
    "days,played,new", [(0, False, True), (13, False, True), (15, False, False), (1, True, False)]
)
def test_new_pill_rule(page, days, played, new):
    page.refresh({"a": {"added_at": stamp(days=days), "last_played": stamp() if played else ""}})
    assert page._cards["a"].new_pill.isHidden() is not new


def test_relative_text_and_subline(page):
    page.refresh({"a": {"added_at": stamp(days=3), "backend": "steam"}})
    card = page._cards["a"]
    assert card.meta_label.text() == "added 3 days ago"
    card.set_state(Status("not_installed"))
    assert card.meta_label.text() == theme.badge_style("not_installed")[1]
    card.set_state(Status("installed"))
    assert card.meta_label.text() == "added 3 days ago"
    assert library._ago(stamp(hours=1)) == "1 hour ago"
    assert library._ago(stamp()) == "just now"


@pytest.mark.parametrize(
    "field,value", [("favourite", True), ("last_played", "2026-11-16"), ("added_at", "2026-11-15")]
)
def test_signature_metadata_rebuild(page, field, value):
    profiles = {"a": {"title": "A"}}
    page.refresh(profiles)
    before = page._cards["a"]
    profiles["a"][field] = value
    page.refresh(profiles)
    assert page._cards["a"] is not before


@pytest.mark.parametrize(
    "width,scale,columns", [(1016, 1, 4), (600, 1, 3), (300, 1, 1), (1016, 1.5, 3), (450, 1.5, 1)]
)
def test_responsive_columns(page, monkeypatch, width, scale, columns):
    monkeypatch.setattr(theme, "UI_SCALE", scale)
    page.refresh({str(i): {"title": str(i)} for i in range(14)})
    page._flow.setGeometry(QRect(0, 0, width, 1000))
    cards = list(page._cards.values())
    assert sum(c.y() == cards[0].y() for c in cards) == columns
    assert all(c.width() >= theme.scaled(130 * 0.75) for c in cards)
    assert max(c.geometry().right() for c in cards) < width


def test_quick_play_focus_hover_running_and_forward(page):
    page.refresh({"a": {"title": "Alpha"}})
    card = page._cards["a"]
    seen = []
    page.play_game.connect(seen.append)
    card.set_state(Status("installed"))
    assert card.play_button.isHidden()
    card.focusInEvent(QFocusEvent(QEvent.Type.FocusIn))
    assert not card.play_button.isHidden()
    card.play_button.click()
    assert seen == ["a"] and card.play_button.accessibleName() == "Play Alpha"
    card.focusOutEvent(QFocusEvent(QEvent.Type.FocusOut))
    assert card.play_button.isHidden()
    QApplication.sendEvent(card, QEvent(QEvent.Type.Enter))
    assert not card.play_button.isHidden()
    for status in (
        Status("running"),
        Status("not_installed"),
        Status("installed", data={"update_available": True}),
    ):
        card.set_state(status)
        assert card.play_button.isHidden()
    card.set_state(Status("running"))
    assert card.art.property("running") is True
    assert "running" in card.art.styleSheet()
    card.set_state(Status("installed"))
    assert card.art.property("running") is False


def test_legacy_config_opens_without_metadata(tmp_path, page):
    path = tmp_path / "config.json"
    path.write_text(json.dumps({"profiles": {"old": {"backend": "wine", "title": "Old"}}}))
    config = Config.load(path)
    page.refresh(config.profiles)
    assert list(page._cards) == ["old"]
    assert page.recent_section.isHidden() and page.favourites_section.isHidden()
    assert page._cards["old"].new_pill.isHidden()
    assert json.loads(path.read_text()) == {
        "profiles": {"old": {"backend": "wine", "title": "Old"}}
    }


def test_pin_menu_roundtrip_and_actual_running_transition(tmp_path, qapp, monkeypatch):
    config = Config.load(tmp_path / "config.json")
    config.add_profile("a", backend="wine", title="Alpha")
    assert config.profiles["a"]["added_at"]
    win = MainWindow(config)
    try:
        for expected in (True, False):
            menu = win.library.card_menu("a")
            text = "Remove from favourites" if not expected else "Add to favourites"
            next(a for a in menu.actions() if a.text() == text).trigger()
            assert Config.load(config.path).profiles["a"]["favourite"] is expected
            assert ("a" in win.library._favourite_cards) is expected
        calls = []
        original = config.mark_played
        monkeypatch.setattr(config, "mark_played", lambda n: calls.append(n) or original(n))
        for status in (
            Status("installed"),
            Status("running"),
            Status("running"),
            Status("installed"),
            Status("installed", running=True),
        ):
            win._on_result("a", "status", status)
        assert calls == ["a", "a"]
        assert Config.load(config.path).profiles["a"]["last_played"]
    finally:
        win.close()


def test_glow_and_translucent_sidebar():
    css = theme.stylesheet()
    assert "qradialgradient" in css
    sidebar = css.split("QWidget#Sidebar {")[1].split("}")[0]
    assert "rgba(" in sidebar


def test_scrolled_page_has_four_tile_columns_at_reference_width(page, qapp):
    page.refresh(
        {
            str(i): {"title": str(i), "favourite": i < 3, "last_played": stamp() if i < 3 else ""}
            for i in range(14)
        }
    )
    page.resize(1072, 800)  # 1280 minus the 208px sidebar
    page.show()
    for _ in range(5):
        qapp.processEvents()
    cards = list(page._cards.values())
    assert sum(c.y() == cards[0].y() for c in cards) == 4


def test_update_subline_and_malformed_running_preserve_status_words(page):
    page.refresh({"a": {}})
    card = page._cards["a"]
    card.set_state(Status("installed", data={"update_available": True}))
    assert card.meta_label.text() == "Update available"
    card.set_state(Status("running", running=True, data=[]))
    assert card.meta_label.text() == "ERROR"
    assert card.art.property("running") is True


def test_recent_cached_wide_is_preferred_without_fetch(page, tmp_path, monkeypatch):
    from PyQt6.QtGui import QColor, QImage

    from nadare.core import artwork

    def art(name, color):
        path = tmp_path / name
        image = QImage(400, 240, QImage.Format.Format_RGB32)
        image.fill(QColor(color))
        image.save(str(path))
        return path

    cover = art("cover.png", "red")
    wide = art("wide.png", "blue")
    monkeypatch.setattr(
        artwork, "cached", lambda slug, kind: wide if kind == "cover-wide" else None
    )
    monkeypatch.setattr(artwork, "fetch", lambda *a, **k: pytest.fail("library downloaded art"))
    page.refresh({"a": {"cover": str(cover), "last_played": stamp()}})
    page.wait_for_images()
    image = page.recent_tiles[0]._pixmap.toImage()
    assert image.pixelColor(image.width() // 2, image.height() // 2).blue() > 200


def test_exact_new_boundary(monkeypatch):
    class Clock(datetime):
        @classmethod
        def now(cls, tz=None):
            return datetime(2026, 11, 17, tzinfo=UTC)

    monkeypatch.setattr(library, "datetime", Clock)
    assert library._is_new("2026-11-03T00:00:01+00:00")
    assert not library._is_new("2026-11-03T00:00:00+00:00")
    assert not library._is_new("2026-11-18T00:00:00+00:00")
    assert not library._is_new("broken")


def test_running_and_keyboard_focus_paint_cover_edge(page, qapp):
    from PyQt6.QtGui import QColor, QPixmap

    page.refresh({"a": {"title": "A"}})
    card = page._cards["a"]
    art = QPixmap(300, 400)
    art.fill(QColor("#402020"))
    card.set_art(art)
    card._shadow.setEnabled(False)
    card.set_state(Status("installed"))
    quiet = card.art.grab().toImage()
    card.set_state(Status("running"))
    running = card.art.grab().toImage()
    x, y = 1, card.art.height() // 2
    assert running.pixelColor(x, y).blue() > quiet.pixelColor(x, y).blue() + 100
    card.set_state(Status("installed"))
    page.show()
    page.activateWindow()
    card.setFocus()
    qapp.processEvents()
    assert card.hasFocus()
    focused = card.art.grab().toImage()
    assert focused.pixelColor(x, y).blue() > quiet.pixelColor(x, y).blue() + 100


def test_keyboard_can_tab_to_quick_play(page, qapp):
    page.refresh({"a": {"title": "A"}})
    page.set_state("a", Status("installed"))
    page.show()
    page.activateWindow()
    card = page._cards["a"]
    card.setFocus()
    qapp.processEvents()
    QTest.keyClick(card, Qt.Key.Key_Tab)
    qapp.processEvents()
    assert card.play_button.hasFocus()
    assert card.play_button.isVisible()
    seen = []
    page.play_game.connect(seen.append)
    QTest.keyClick(card.play_button, Qt.Key.Key_Space)
    assert seen == ["a"]


def test_replaced_wide_art_invalidates_signature(page, tmp_path):
    import os

    wide = tmp_path / "wide.png"
    wide.write_bytes(b"placeholder")
    profiles = {"a": {"wide": str(wide), "last_played": stamp()}}
    page.refresh(profiles)
    before = page.recent_tiles[0]
    changed = wide.stat().st_mtime_ns + 1_000_000
    os.utime(wide, ns=(changed, changed))
    page.refresh(profiles)
    assert page.recent_tiles[0] is not before


def test_refused_quick_play_does_not_stamp_played(tmp_path, qapp, monkeypatch):
    config = Config.load(tmp_path / "config.json")
    config.add_profile("a", backend="wine", title="Alpha")
    win = MainWindow(config)
    try:
        win._snapshot("a")["status"] = Status("installed")
        monkeypatch.setattr(win, "_start", lambda *args, **kwargs: False)
        win.library.play_game.emit("a")
        assert not config.profiles["a"]["last_played"]
        assert win.library.recent_section.isHidden()
    finally:
        win.close()


def test_config_metadata_defaults_and_roundtrip(tmp_path):
    path = tmp_path / "config.json"
    config = Config.load(path)
    profile = config.add_profile("a", backend="wine", added_at="2026-11-01")
    assert profile["added_at"] == "2026-11-01"
    assert profile["favourite"] is False and profile["last_played"] == ""
    before = dict(config.data)
    assert config.mark_played("absent") is False
    assert config.set_favourite("absent", True) is False
    assert config.mark_played("a") is True
    assert config.set_favourite("a", True) is True
    assert {k: v for k, v in config.data.items() if k not in ("profiles", "active_profile")} == {
        k: v for k, v in before.items() if k not in ("profiles", "active_profile")
    }
    config.save()
    again = Config.load(path)
    assert again.profiles["a"]["last_played"] == profile["last_played"]
    assert again.profiles["a"]["favourite"] is True


def test_favourite_elision_uses_the_rendered_font(page, qapp):
    page.setStyleSheet(theme.stylesheet())
    page.refresh({"a": {"title": "Wuthering Waves", "favourite": True}})
    page.show()
    qapp.processEvents()
    card = page._favourite_cards["a"]
    label = card.title_label
    expected = label.fontMetrics().elidedText(
        "Wuthering Waves", Qt.TextElideMode.ElideRight, label.width()
    )
    assert label.text() == expected


def test_the_empty_state_names_the_button_it_shows(page):
    """The sentence names the button by its own label, so a player can find it."""
    page.refresh({})
    button = page.empty_add.text()
    assert button
    assert f"Click {button} " in page.subtitle.text()


@pytest.mark.parametrize("width", [960, 1280, 1920])
def test_favourites_share_grid_geometry_and_decode_size(page, qapp, monkeypatch, width):
    requests = []
    monkeypatch.setattr(page._images, "request", lambda *args, **kw: requests.append(args))
    monkeypatch.setattr(library, "profile_art_candidates", lambda profile: [])
    monkeypatch.setattr(library, "recent_art_candidates", lambda profile: [])
    page.refresh({"a": {"title": "A long favourite name", "favourite": True}})
    favourite = page._favourite_cards["a"]
    assert requests[-1][1:3] == (favourite.art.width(), favourite.art.height())
    page.resize(width, 1100)
    page.show()
    for _ in range(5):
        qapp.processEvents()
    card = page._cards["a"]
    assert favourite.size() == card.size()
    assert favourite.width() == library.tile_width_for(page._container.width())
    assert favourite.art.size() == card.art.size()
    assert favourite.meta_label.isHidden()  # tiles show the name only
    assert favourite.heart.geometry().right() < favourite.title_label.x()
    assert favourite.title_label.geometry().right() == card.title_label.geometry().right()
    requests.clear()
    page._art_timer.timeout.emit()
    assert len(requests) == 2  # both copies must be decoded, despite sharing a profile id
    assert all(r[1:3] == (card.art.width(), card.art.height()) for r in requests)


@pytest.mark.parametrize("width", [960, 1280, 1920])
def test_recent_tiles_and_grid_cards_are_one_size(page, qapp, width):
    page.refresh({str(i): {"last_played": stamp()} for i in range(4)})
    page.resize(width, 1100)
    page.show()
    for _ in range(5):
        qapp.processEvents()
    card = page._cards["0"]
    assert all(tile.size() == card.size() for tile in page.recent_tiles)
    assert all(tile.height() == round(tile.width() * 9 / 16) for tile in page.recent_tiles)


def test_recent_play_hover_focus_signals_and_state(page, qapp):
    page.refresh({"a": {"title": "Alpha", "last_played": stamp()}})
    page.set_state("a", Status("installed"))
    tile = page.recent_tiles[0]
    played, opened = [], []
    page.play_game.connect(played.append)
    page.open_game.connect(opened.append)
    assert tile.play_button.isHidden()
    QApplication.sendEvent(tile, QEvent(QEvent.Type.Enter))
    assert not tile.play_button.isHidden()
    assert tile.play_button.accessibleName() == "Play Alpha"
    tile.play_button.click()
    assert played == ["a"] and opened == []
    QTest.mouseClick(tile, Qt.MouseButton.LeftButton)
    QTest.keyClick(tile, Qt.Key.Key_Return)
    assert opened == ["a", "a"]
    QApplication.sendEvent(tile, QEvent(QEvent.Type.Leave))
    assert tile.play_button.isHidden()
    tile.focusInEvent(QFocusEvent(QEvent.Type.FocusIn))
    assert not tile.play_button.isHidden()
    tile.focusOutEvent(QFocusEvent(QEvent.Type.FocusOut))
    assert tile.play_button.isHidden()
    for status in (
        Status("running"),
        Status("installed", running=True),
        Status("not_installed"),
        Status("installed", data={"update_available": True}),
    ):
        page.set_state("a", status)
        tile = page.recent_tiles[0]
        QApplication.sendEvent(tile, QEvent(QEvent.Type.Enter))
        assert tile.play_button.isHidden()
        assert tile.property("running") == (status.state == "running" or status.running)
        assert tile.badge.text() == page._cards["a"].badge.text()
        assert not tile.badge.isHidden()
    page.set_state("a", "unknown")
    assert page.recent_tiles[0].badge.isHidden()
    assert page.recent_tiles[0].play_button.isHidden()
    page.set_state("a", "installed")
    assert not page.recent_tiles[0].play_button.isHidden()
    page.clear_states()
    assert page.recent_tiles[0].play_button.isHidden()


def test_recent_keyboard_can_tab_to_play(page, qapp):
    page.refresh({"a": {"title": "Alpha", "last_played": stamp()}})
    page.set_state("a", Status("installed"))
    page.show()
    page.activateWindow()
    tile = page.recent_tiles[0]
    tile.setFocus()
    qapp.processEvents()
    QTest.keyClick(tile, Qt.Key.Key_Tab)
    qapp.processEvents()
    assert tile.play_button.hasFocus() and tile.play_button.isVisible()
    played, opened = [], []
    page.play_game.connect(played.append)
    page.open_game.connect(opened.append)
    QTest.keyClick(tile.play_button, Qt.Key.Key_Space)
    assert played == ["a"] and opened == []
