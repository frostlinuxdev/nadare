# SPDX-License-Identifier: GPL-3.0-or-later
"""Library and favourite cards share the smaller responsive size rule."""

import time

from PyQt6.QtWidgets import QApplication, QScrollArea

from nadare.gui import library, theme


def test_card_width_helper():
    minimum, maximum, gap = theme.scaled(130 * 0.75), theme.scaled(211 * 0.75), theme.scaled(14)
    assert library.card_width_for(minimum - 1) == minimum
    for available in (1280, 2560, 10000):
        width = library.card_width_for(available)
        assert minimum < width <= maximum
        per_row = (available + gap) // (width + gap)
        assert per_row * width + (per_row - 1) * gap <= available
        assert (per_row + 1) * width + per_row * gap > available
    assert library.card_width_for(2560) > library.card_width_for(1280)


def test_shown_library_cards_grow_and_request_resized_art(monkeypatch):
    app = QApplication.instance() or QApplication([])
    page = library.LibraryPage()
    requests = []
    monkeypatch.setattr(page._images, "request", lambda *args, **kwargs: requests.append(args))
    monkeypatch.setattr(library, "profile_art_candidates", lambda profile: [])
    monkeypatch.setattr(library, "recent_art_candidates", lambda profile: [])
    try:
        page.refresh({str(i): {"title": f"Game {i}"} for i in range(24)})
        # Exercise AddGameCard in the same layout; the current library uses a header Add button.
        add = library.AddGameCard()
        page._flow.addWidget(add)
        page.resize(1280, 1000)
        page.show()
        for _ in range(5):
            app.processEvents()
        cards = list(page._cards.values())
        narrow_width, narrow_art_h = cards[0].width(), cards[0].art.height()
        requests.clear()
        page.resize(2560, 1440)
        for _ in range(5):
            app.processEvents()
        width = cards[0].width()
        assert width > narrow_width
        assert width == library.tile_width_for(page._container.width())
        assert cards[0].art.height() > narrow_art_h
        assert all(c.art.height() == round(c.width() * 9 / 16) for c in cards)
        assert add.size() == cards[0].size()
        page._art_timer.timeout.emit()
        assert (width, cards[0].art.height()) in [(r[1], r[2]) for r in requests]
        columns = (page._container.width() + theme.scaled(14)) // (width + theme.scaled(14))
        assert sum(c.y() == cards[0].y() for c in cards) == columns
        scroll = page.findChild(QScrollArea)
        assert scroll.horizontalScrollBar().maximum() == 0
        page.resize(356, 1000)
        for _ in range(5):
            app.processEvents()
        assert all(c.width() >= theme.scaled(130 * 0.75) for c in cards)
        assert scroll.horizontalScrollBar().maximum() == 0
    finally:
        page.close()
        page.deleteLater()
        app.processEvents()


def test_a_drag_resize_decodes_each_cover_once_after_it_settles(monkeypatch):
    """Resizing step by step must not queue one decode per card per step."""
    app = QApplication.instance() or QApplication([])
    page = library.LibraryPage()
    requests = []
    monkeypatch.setattr(page._images, "request", lambda *args, **kwargs: requests.append(args))
    monkeypatch.setattr(library, "profile_art_candidates", lambda profile: [])
    monkeypatch.setattr(library, "recent_art_candidates", lambda profile: [])
    try:
        page.refresh({str(i): {"title": f"Game {i}"} for i in range(12)})
        page.resize(1280, 1000)
        page.show()
        for _ in range(5):
            app.processEvents()
        page._art_timer.stop()
        requests.clear()
        # The test fires the timer itself: a loaded CI host can stall between two steps for
        # longer than the real 150 ms interval, and that pause would rightly count as settled.
        page._art_timer.setInterval(3_600_000)
        for width in range(1300, 2600, 40):
            page.resize(width, 1000)
            app.processEvents()
            if width == 1340:
                time.sleep(0.2)  # such a stall must not change the count below
                app.processEvents()
        # 33 resize steps queue no decode: each one only restarts the pending timer.
        assert requests == []
        assert page._art_timer.isActive()
        # When it fires, each cover is decoded once, at the final size.
        page._art_timer.timeout.emit()
        app.processEvents()
        card = next(iter(page._cards.values()))
        assert len(requests) == len(page._cards)
        assert all((r[1], r[2]) == (card.art.width(), card.art.height()) for r in requests)
    finally:
        page.close()
        page.deleteLater()
        app.processEvents()


def test_cards_are_tile_sized():
    app = QApplication.instance() or QApplication([])
    assert library.CARD_SCALE == 0.75
    assert (library.CARD_WIDTH, library.CARD_HEIGHT) == (library.TILE_WIDTH, library.TILE_ART_H)
    card, add = library.GameCard("a", "A"), library.AddGameCard()
    assert card.size() == add.size() == library.RecentTile("b", "B", "").size()
    for available, expected in ((892, 99), (1212, 122), (1852, 158)):
        assert library.card_width_for(theme.scaled(available)) == theme.scaled(expected)
    card.deleteLater()
    add.deleteLater()
    app.processEvents()
