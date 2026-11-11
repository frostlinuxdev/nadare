# SPDX-License-Identifier: GPL-3.0-or-later
"""A cover that failed to download is tried again later, not skipped all session (#900)."""

from __future__ import annotations

import os

os.environ.setdefault("QT_QPA_PLATFORM", "offscreen")

import pytest

try:  # pragma: no cover
    from PyQt6.QtCore import QThreadPool
    from PyQt6.QtWidgets import QApplication
except Exception:  # noqa: BLE001
    pytest.skip("PyQt6 is not importable in this interpreter", allow_module_level=True)

from nadare.gui import store
from nadare.gui.store import StorePage


@pytest.fixture(scope="module")
def qapp():
    return QApplication.instance() or QApplication([])


@pytest.fixture
def page(qapp, monkeypatch):
    started: list[str] = []
    def start(self, runnable, *a):
        # Only cover fetches are counted. A fetch queued before this test can finish while
        # it is set up and ask the image loader for a decode (#1024): that is not a request.
        if isinstance(runnable, store._FetchArt):
            started.append(runnable.kind)

    monkeypatch.setattr(QThreadPool, "start", start)
    widget = StorePage()
    widget.show()  # builds the grid
    qapp.processEvents()
    started.clear()  # showing the page already asks for the covers once
    widget._art_requested.clear()
    for card in widget._cards.values():
        card.has_art = True  # the catalog's art table decides this online; pretend all have art
    widget.started = started
    yield widget
    widget.deleteLater()
    qapp.processEvents()


def _attempts(page, kind):
    return page.started.count(kind)


def _first_kind(page):
    return next(card.art_kind for card in page._cards.values() if card.has_art)


def test_every_cover_is_requested_once_to_begin_with(page):
    page._request_visible_art()
    page._request_visible_art()
    kind = _first_kind(page)
    assert _attempts(page, kind) == 1


def test_a_failed_cover_is_not_hammered_straight_away(page):
    page._request_visible_art()
    kind = _first_kind(page)
    page._on_art_fetched(kind, None)
    page.refresh()
    assert _attempts(page, kind) == 1


def test_a_failed_cover_is_tried_again_after_the_backoff(page, monkeypatch):
    page._request_visible_art()
    kind = _first_kind(page)
    clock = {"now": 1000.0}
    monkeypatch.setattr(store.time, "monotonic", lambda: clock["now"])
    page._on_art_fetched(kind, None)
    clock["now"] += store.COVER_RETRY_SECONDS + 1
    page.refresh()
    assert _attempts(page, kind) == 2


def test_a_cover_that_arrived_is_never_fetched_again(page, monkeypatch, tmp_path):
    page._request_visible_art()
    kind = _first_kind(page)
    monkeypatch.setattr(page, "_apply_art", lambda card: None)
    page._on_art_fetched(kind, str(tmp_path / "cover.png"))
    clock = {"now": 5000.0}
    monkeypatch.setattr(store.time, "monotonic", lambda: clock["now"] + 10_000)
    page.refresh()
    assert _attempts(page, kind) == 1


def test_other_covers_are_not_refetched_when_one_fails(page, monkeypatch):
    page._request_visible_art()
    kinds = [c.art_kind for c in page._cards.values() if c.has_art]
    failed = kinds[0]
    clock = {"now": 1000.0}
    monkeypatch.setattr(store.time, "monotonic", lambda: clock["now"])
    page._on_art_fetched(failed, None)
    clock["now"] += store.COVER_RETRY_SECONDS + 1
    page.refresh()
    assert all(_attempts(page, k) == 1 for k in kinds[1:])
