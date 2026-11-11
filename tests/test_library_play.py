# SPDX-License-Identifier: GPL-3.0-or-later
"""One-click Play on Library cards.

The card's Play runs the same checks as the game page's LAUNCH (#765): it launches only when the
last known status makes LAUNCH the page's main action and no other operation owns the game;
otherwise it opens the game page, where the right action (Install, Update, Log in, Resume) is.
"""

from __future__ import annotations

import os
from pathlib import Path

os.environ.setdefault("QT_QPA_PLATFORM", "offscreen")

import pytest

try:
    from PyQt6.QtWidgets import QApplication
except Exception:  # pragma: no cover - optional GUI dependencies
    pytest.skip("PyQt6 is not importable in this interpreter", allow_module_level=True)

from nadare.core import artwork
from nadare.core.backends import Status
from nadare.core.config import Config
from nadare.core.gamefacts import primary_operation
from nadare.gui.library import GameCard, LibraryPage
from nadare.gui.main_window import MainWindow


@pytest.fixture(scope="module")
def qapp():
    return QApplication.instance() or QApplication([])


@pytest.fixture(autouse=True)
def no_real_art(monkeypatch):
    monkeypatch.setattr(artwork, "fetch", lambda *args, **kwargs: None)


# ---- the rule, shared by the page and the card ------------------------------
@pytest.mark.parametrize(
    ("status", "expected"),
    [
        (None, None),
        (Status("installed"), "launch"),
        (Status("installed", running=True), "stop"),
        (Status("running"), "stop"),
        (Status("error"), "retry"),
        (Status("not_installed"), "install"),
        (Status("not_installed", data={"can_install": False}), "settings"),
        (Status("installed", data={"update_available": True}), "update"),
        (Status("installed", data={"login_required": True, "logged_in": False}), "login"),
        (Status("installed", data={"login_required": True, "logged_in": True}), "launch"),
    ],
)
def test_primary_operation_follows_the_status(status, expected):
    assert primary_operation(status) == expected


# ---- the card ----------------------------------------------------------------
def test_card_play_is_hidden_until_the_game_is_ready(qapp):
    card = GameCard("p", "My Game", "wine")
    try:
        assert card.play_button.isHidden()
        card.set_state("installed")
        assert not card.play_button.isHidden()
        assert card.play_button.text() == "Play"
        assert card.badge.isHidden()  # Play already says it is ready
        card.set_state("installed", running=True)
        assert card.play_button.isHidden()
        assert not card.badge.isHidden()
        for state in ("not_installed", "error", "running", "unknown"):
            card.set_state(state)
            assert card.play_button.isHidden(), state
    finally:
        card.deleteLater()
        qapp.processEvents()


def test_card_play_emits_play_and_not_clicked(qapp):
    card = GameCard("p", "My Game", "wine")
    played: list[str] = []
    opened: list[str] = []
    card.play_clicked.connect(played.append)
    card.clicked.connect(opened.append)
    try:
        card.set_state("installed")
        card.play_button.click()
        assert played == ["p"]
        assert opened == []  # the button does not also open the page
        assert card.play_button.accessibleName() == "Play My Game"
    finally:
        card.deleteLater()
        qapp.processEvents()


def test_library_forwards_card_play(qapp):
    page = LibraryPage()
    played: list[str] = []
    page.play_game.connect(played.append)
    try:
        page.refresh({"a": {"title": "A", "backend": "wine"}})
        page.set_state("a", "installed")
        page._cards["a"].play_button.click()
        assert played == ["a"]
    finally:
        page.deleteLater()
        qapp.processEvents()


# ---- the main window ---------------------------------------------------------
def _window(tmp_path: Path, **profiles: dict) -> MainWindow:
    config = Config.load(tmp_path / "config.json")
    for name, values in profiles.items():
        config.add_profile(name, **values)
    config.save()
    return MainWindow(config)


def _close(window, qapp) -> None:
    window.close()
    window.deleteLater()
    qapp.processEvents()


@pytest.fixture
def window(tmp_path, qapp):
    win = _window(tmp_path, game={"backend": "wine", "title": "Game"})
    win.calls = []  # type: ignore[attr-defined]
    win.open_game = lambda name: win.calls.append(("open", name))
    win._start = lambda name, op, *a, **k: win.calls.append((op, name)) or True
    yield win
    _close(win, qapp)


def _status(win, status: Status | None) -> None:
    win._snapshot("game")["status"] = status


def test_play_launches_a_ready_game_then_shows_its_page(window):
    _status(window, Status("installed"))
    window.library.play_game.emit("game")
    assert window.calls == [("launch", "game"), ("open", "game")]


@pytest.mark.parametrize(
    "status",
    [
        None,
        Status("not_installed"),
        Status("installed", data={"update_available": True}),
        Status("installed", data={"login_required": True, "logged_in": False}),
        Status("installed", running=True),
        Status("error"),
    ],
)
def test_play_opens_the_page_when_launch_is_not_the_action(window, status):
    _status(window, status)
    window.library.play_game.emit("game")
    assert window.calls == [("open", "game")]


def test_play_opens_the_page_while_another_operation_owns_the_game(window):
    _status(window, Status("installed"))
    window._busy["game"] = "verify"
    window.library.play_game.emit("game")
    assert window.calls == [("open", "game")]


def test_play_opens_the_page_for_a_paused_download(window, monkeypatch):
    _status(window, Status("installed"))
    monkeypatch.setattr(window.controller, "is_paused", lambda name: name == "game")
    window.library.play_game.emit("game")
    assert window.calls == [("open", "game")]


def test_play_declined_notice_starts_nothing_and_stays(window, monkeypatch):
    _status(window, Status("installed"))
    monkeypatch.setattr(window, "_needs_notice", lambda name: True)
    monkeypatch.setattr(window, "_confirm_notice", lambda name: False)
    window.library.play_game.emit("game")
    assert window.calls == []


def test_play_declined_unofficial_notice_starts_nothing(window, monkeypatch):
    _status(window, Status("installed"))
    monkeypatch.setattr(window, "_confirm_unofficial", lambda name: False)
    window.library.play_game.emit("game")
    assert window.calls == []


def test_play_checks_the_notices_of_the_card_game_not_the_open_one(tmp_path, qapp, monkeypatch):
    win = _window(
        tmp_path,
        game={"backend": "wine", "title": "Game"},
        other={"backend": "wine", "title": "Other"},
    )
    try:
        win._current_profile = "other"
        asked: list[str] = []
        monkeypatch.setattr(win, "_needs_notice", lambda name: asked.append(name) or False)
        monkeypatch.setattr(win, "_confirm_unofficial", lambda name: asked.append(name) or True)
        started: list[tuple] = []
        win._start = lambda name, op, *a, **k: started.append((op, name)) or True
        win.open_game = lambda name: None
        win._snapshot("game")["status"] = Status("installed")
        win.library.play_game.emit("game")
        assert set(asked) == {"game"}
        assert started == [("launch", "game")]
    finally:
        _close(win, qapp)


def test_play_that_could_not_start_still_opens_the_page(window):
    _status(window, Status("installed"))
    window._start = lambda name, op, *a, **k: window.calls.append((op, name)) and False
    window.library.play_game.emit("game")
    assert window.calls == [("launch", "game"), ("open", "game")]


def test_play_for_a_removed_profile_does_nothing(window):
    window.library.play_game.emit("gone")
    assert window.calls == []
