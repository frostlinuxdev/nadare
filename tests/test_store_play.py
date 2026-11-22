# SPDX-License-Identifier: GPL-3.0-or-later
"""#765: a game that is already installed offers Play on its Store page."""

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
from nadare.core.config import Config
from nadare.core.storecatalog import StoreEntry, entries
from nadare.gui.main_window import MainWindow


@pytest.fixture(scope="module")
def qapp():
    return QApplication.instance() or QApplication([])


@pytest.fixture(autouse=True)
def no_real_art(monkeypatch):
    monkeypatch.setattr(artwork, "fetch", lambda *args, **kwargs: None)


def _entry(kind: str, **changes) -> StoreEntry:
    base = next(item for item in entries() if item.kind == kind)
    values = {field: getattr(base, field) for field in StoreEntry.__dataclass_fields__}
    values.update(changes)
    return StoreEntry(**values)


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


def test_play_in_the_store_opens_that_games_page(tmp_path, qapp):
    window = _window(
        tmp_path,
        other={"backend": "launcher-nikke", "title": "NIKKE"},
        pgr={"backend": "direct-pgr", "title": "PGR"},
    )
    opened = []
    window.open_game = opened.append
    try:
        window.show_store()
        window.store.grid.entry_activated.emit("direct-pgr")  # the card itself (#1536)
        assert opened == ["pgr"]
    finally:
        _close(window, qapp)


def test_play_with_two_copies_opens_the_first_and_does_not_start_an_install(tmp_path, qapp):
    window = _window(
        tmp_path,
        first={"backend": "direct-pgr", "title": "PGR"},
        second={"backend": "direct-pgr", "title": "PGR 2"},
    )
    opened = []
    window.open_game = opened.append
    started = []
    window._start = lambda *a, **k: started.append(a) or True
    try:
        window.show_store()
        window.store.grid.entry_activated.emit("direct-pgr")  # the card itself (#1536)
        assert opened == ["first"] and started == []
    finally:
        _close(window, qapp)


def test_play_for_a_game_whose_profile_vanished_does_not_crash(tmp_path, qapp):
    window = _window(tmp_path)
    opened = []
    window.open_game = opened.append
    try:
        window._store_play("direct-pgr")  # no profile for this kind any more
        assert opened == []
    finally:
        _close(window, qapp)
