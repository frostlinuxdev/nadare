# SPDX-License-Identifier: GPL-3.0-or-later
"""#992: the "how this game runs" notice is one short line once the game is installed."""

from __future__ import annotations

import os
from pathlib import Path

os.environ.setdefault("QT_QPA_PLATFORM", "offscreen")

import pytest

try:  # pragma: no cover - only when PyQt6 is missing
    from PyQt6.QtWidgets import QApplication
except Exception:  # noqa: BLE001
    pytest.skip("PyQt6 is not importable in this interpreter", allow_module_level=True)

from nadare.core import backends as core_backends
from nadare.core.backends import Status
from nadare.core.config import Config
from nadare.gui.game_view import GameView


@pytest.fixture(scope="module")
def qapp():
    return QApplication.instance() or QApplication([])


@pytest.fixture
def view(qapp):
    widget = GameView()
    yield widget
    widget.deleteLater()
    qapp.processEvents()


def test_every_notice_has_a_short_line_that_is_shorter():
    for kind in core_backends.kinds():
        notice = core_backends.page_notice(kind)
        if not notice:
            continue
        summary = core_backends.page_summary(kind)
        assert summary and len(summary) <= 240, kind
        assert len(summary) <= len(notice[0]), kind


def test_nikke_short_line_keeps_what_must_not_be_missed():
    summary = core_backends.page_summary("launcher-nikke")
    assert "PLAYING" in summary and "sign in" in summary


def test_nikke_notice_no_longer_calls_a_verified_game_experimental():
    assert "experimental" not in core_backends.page_notice("launcher-nikke")[0].lower()


def test_before_install_the_whole_notice_shows(view):
    view.set_profile("nikke", {"backend": "launcher-nikke", "title": "NIKKE"})
    view.set_status(Status("not_installed"))
    assert "miniloader" in view.notice_label.text()


def test_once_installed_only_the_short_line_shows_with_a_way_to_read_more(view):
    view.set_profile("nikke", {"backend": "launcher-nikke", "title": "NIKKE"})
    view.set_status(Status("installed"))
    text = view.notice_label.text()
    assert "miniloader" not in text
    assert "PLAYING" in text
    assert "How this game runs" in text
    view.notice_label.linkActivated.emit("nadare:more")
    assert "miniloader" in view.notice_label.text()
    assert "Show less" in view.notice_label.text()


def test_the_choice_is_remembered_across_games_and_saved(tmp_path: Path, qapp, monkeypatch):
    from nadare.gui.main_window import MainWindow
    from nadare.gui.workers import BackendController

    monkeypatch.setattr(BackendController, "start", lambda *a, **k: True)
    config = Config.load(tmp_path / "config.json")
    config.add_profile("nikke", backend="launcher-nikke", title="NIKKE")
    config.save()
    window = MainWindow(config)
    try:
        window.open_game("nikke")
        window.game.set_status(Status("installed"))
        window.game.notice_label.linkActivated.emit("nadare:more")
        assert Config.load(tmp_path / "config.json").data.get("page_notice_expanded") is True
        window.game.notice_label.linkActivated.emit("nadare:less")
        assert Config.load(tmp_path / "config.json").data.get("page_notice_expanded") is False
        assert "miniloader" not in window.game.notice_label.text()
    finally:
        window.close()
        window.deleteLater()
        qapp.processEvents()


def test_a_one_sentence_notice_has_no_toggle(view, monkeypatch):
    from nadare.gui import game_view as game_view_mod

    monkeypatch.setattr(game_view_mod, "page_notice", lambda kind: ("Only one line.", "https://x/"))
    monkeypatch.setattr(game_view_mod, "page_summary", lambda kind: "Only one line.")
    view.set_profile("ss", {"backend": "stellasora-yostar", "title": "Stella Sora"})
    view.set_status(Status("installed"))
    text = view.notice_label.text()
    assert "Only one line." in text
    assert "How this game runs" not in text and "Show less" not in text


def test_outside_links_still_open_and_internal_ones_never_leave_the_app(view, monkeypatch):
    opened: list[str] = []
    monkeypatch.setattr(view, "_open_external", opened.append)
    view.set_profile("nikke", {"backend": "launcher-nikke", "title": "NIKKE"})
    view.notice_label.linkActivated.emit("nadare:more")
    view.notice_label.linkActivated.emit("https://nikke-en.com/")
    assert opened == ["https://nikke-en.com/"]
