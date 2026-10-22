# SPDX-License-Identifier: GPL-3.0-or-later
"""Regressions for the L2a GUI sweep (#335, #342, #351, #338)."""

from __future__ import annotations

import os

os.environ.setdefault("QT_QPA_PLATFORM", "offscreen")

import pytest

try:  # pragma: no cover - exercised only when PyQt6 is missing
    from PyQt6.QtWidgets import QApplication
except Exception:  # noqa: BLE001
    pytest.skip("PyQt6 is not importable in this interpreter", allow_module_level=True)

from nadare.core.backends import Status
from nadare.core.config import Config
from nadare.core.news import NewsResult
from nadare.gui.game_view import GameView
from nadare.gui.library import GameCard, LibraryPage, backend_label, count_text
from nadare.gui.main_window import MainWindow, _removal_reason_text
from nadare.gui.sidebar import GameRow

# Keep one application for the whole test process: letting a module-scoped
# fixture drop the last reference can destroy Qt's font database mid-run.
_APP = QApplication.instance() or QApplication([])


@pytest.fixture(scope="session")
def qapp():
    return _APP


def _window(tmp_path):
    config = Config.load(tmp_path / "config.json")
    return MainWindow(config)


# ---- #335 F-04: the empty library says what to do --------------------------
def test_empty_library_points_at_add_game(qapp):
    page = LibraryPage()
    try:
        page.refresh({})
        text = page.subtitle.text()
        assert "No games yet" in text
        assert "Add game" in text
        assert "backend" not in text.lower()
        assert page.hint.text() == "0 games"
    finally:
        page.deleteLater()
        qapp.processEvents()


# ---- #335 F-11 / #342 F-09: raw backend ids become human labels -----------
def test_backend_ids_are_shown_as_human_labels(qapp):
    assert backend_label("arknights-yostar") == "Yostar launcher"
    # Legacy configs may have stored the id in upper case.
    assert backend_label("ARKNIGHTS-YOSTAR") == "Yostar launcher"

    card = GameCard("p", "P", "ARKNIGHTS-YOSTAR")
    view = GameView()
    try:
        assert card.meta_label.text() == "Yostar launcher"
        view.set_profile("p", {"backend": "ARKNIGHTS-YOSTAR", "title": "P"})
        assert view.backend_label.text() == "Yostar launcher"
    finally:
        card.deleteLater()
        view.deleteLater()
        qapp.processEvents()


# ---- #335 F-09: the settings primary action is clickable -------------------
def test_settings_primary_action_opens_settings(qapp):
    view = GameView()
    opened: list[bool] = []
    actions: list[str] = []
    view.settings_requested.connect(lambda: opened.append(True))
    view.action_requested.connect(actions.append)
    try:
        view.set_profile("p", {"backend": "wine"})
        view.set_status(Status("not_installed", data={"can_install": False}))
        assert view.primary.isEnabled()
        assert "SETTINGS" in view.primary.text().upper()
        view._on_primary()
        assert opened == [True]
        assert "settings" not in actions
    finally:
        view.deleteLater()
        qapp.processEvents()


# ---- #342 F-07: colour-only status dots have a name and tooltip ------------
def test_sidebar_status_dots_are_labelled(qapp, tmp_path):
    running = GameRow("p", "P")
    update = GameRow("p", "P")
    window = _window(tmp_path)
    try:
        running.set_state(Status("running"))
        assert running.dot.accessibleName() == "Running"
        assert running.dot.toolTip() == "Running"
        update.set_state(Status("installed", data={"update_available": True}))
        assert update.dot.accessibleName() == "Update available"
        assert update.dot.toolTip() == "Update available"

        window._set_side_status("Viewing P")
        assert window._status_dot.accessibleName() == "Viewing P"
        assert window._status_dot.toolTip() == "Viewing P"
    finally:
        running.deleteLater()
        update.deleteLater()
        window.close()
        window.deleteLater()
        qapp.processEvents()


# ---- #351 F-13: a fresh page says CHECKING…, never UNKNOWN ----------------
def test_fresh_profile_shows_checking_not_unknown(qapp):
    view = GameView()
    try:
        view.set_profile("p", {"backend": "stove-czn", "title": "P"})
        assert view.badge.text() == "CHECKING…"
        assert "UNKNOWN" not in view.badge.text()
        view.set_status(Status("installed"))
        assert view.badge.text() == "READY"
    finally:
        view.deleteLater()
        qapp.processEvents()


# ---- #351 F-12: unavailable news has a reason and a working Retry ---------
def test_unavailable_news_offers_reason_and_retry(qapp):
    view = GameView()
    requested: list[str] = []
    view.action_requested.connect(requested.append)
    try:
        view.set_profile("p", {"backend": "stove-czn", "title": "P"})
        view.set_news(NewsResult(items=[], available=False))
        assert view.news_hint.text() == "News unavailable"
        assert "connection" in view.news_reason.text().lower()
        assert not view.news_frame.isHidden()
        assert not view.news_retry.isHidden()
        view.news_retry.click()
        assert requested == ["news"]
    finally:
        view.deleteLater()
        qapp.processEvents()


# ---- #351 F-16: counts use real singular/plural ---------------------------
def test_counts_use_singular_and_plural(tmp_path, qapp):
    assert count_text(1, "image") == "1 image"
    assert count_text(2, "image") == "2 images"
    assert count_text(0, "unused runner") == "0 unused runners"
    window = _window(tmp_path)
    try:
        window._runner_cleanup_finished(1, 1024, 0)
        assert "Deleted 1 unused runner," in window.side_status.text()
        window._runner_cleanup_finished(2, 1024, 0)
        assert "Deleted 2 unused runners," in window.side_status.text()
    finally:
        window.close()
        window.deleteLater()
        qapp.processEvents()


# ---- #351 F-15: removal refusals explain themselves in plain language -----
def test_removal_reasons_are_rewritten_in_plain_language():
    assert _removal_reason_text("is a mount point") == "it is a separate disk or drive"
    assert _removal_reason_text("is a symlink") == "it is a link to another folder"
    assert _removal_reason_text("is home or an ancestor of home") == "it contains your home folder"
    assert (
        _removal_reason_text("is inside nadare's own directories")
        == "it is inside Nadare's own folder"
    )
    assert _removal_reason_text("shared with h") == "another game (h) also uses it"
    for internal in ("is a mount point", "is a symlink", "shared with h"):
        assert internal not in _removal_reason_text(internal)
