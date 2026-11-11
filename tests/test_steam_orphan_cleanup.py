# SPDX-License-Identifier: GPL-3.0-or-later
"""#1031: a game removed while Steam ran loses its Steam shortcut the next time Nadare starts.

Steam rewrites ``shortcuts.vdf`` while it runs, so the removal left the entry behind with only
a warning line. At start, with Steam closed, Nadare now drops entries of its own whose game is
gone; it changes nothing else in the file (no titles, no paths).
"""

from __future__ import annotations

import os

os.environ.setdefault("QT_QPA_PLATFORM", "offscreen")

import threading

import pytest
from test_steam_shortcut_ownership import FOREIGN, NADARES, _entry, _read, _write

from nadare.core import shortcuts


@pytest.fixture(autouse=True)
def steam_closed(monkeypatch):
    monkeypatch.setattr(shortcuts, "steam_running", lambda: False)


def test_orphans_only_drops_entries_of_removed_games_and_changes_nothing_else(
    tmp_path, monkeypatch
):
    kept = _entry("/old/path/Nadare.AppImage", 'launch -p "kept"', title="Old title")
    vdf = _write(tmp_path / "shortcuts.vdf", [FOREIGN, NADARES["appimage"], kept])
    monkeypatch.setattr(shortcuts, "find_steam_shortcut_files", lambda: [vdf])
    result = shortcuts.refresh_steam_shortcuts(
        {"kept": {"title": "New title"}}, argv=["/new/Nadare.AppImage"], orphans_only=True
    )
    entries = _read(vdf)
    assert [e["AppName"] for e in entries] == ["Work tool", "Old title"]  # title untouched
    assert entries[1]["Exe"] == '"/old/path/Nadare.AppImage"'  # path untouched
    assert any("work" in item for item in result["removed"])


def test_orphans_only_with_nothing_to_drop_writes_nothing(tmp_path, monkeypatch):
    kept = _entry("/x/Nadare.AppImage", 'launch -p "kept"', title="Old title")
    vdf = _write(tmp_path / "shortcuts.vdf", [kept])
    before = vdf.read_bytes()
    monkeypatch.setattr(shortcuts, "find_steam_shortcut_files", lambda: [vdf])
    shortcuts.refresh_steam_shortcuts({"kept": {"title": "New"}}, orphans_only=True)
    assert vdf.read_bytes() == before
    assert not (tmp_path / "shortcuts.vdf.nadare-bak").exists()


def test_the_removal_warning_says_what_the_user_can_do(tmp_path, monkeypatch):
    vdf = _write(tmp_path / "shortcuts.vdf", [NADARES["appimage"]])
    monkeypatch.setattr(shortcuts, "steam_running", lambda: True)
    with pytest.raises(shortcuts.ShortcutError) as caught:
        shortcuts.remove_steam_shortcut(vdf, "work")
    assert "close Steam and remove it again" in str(caught.value)


def test_the_window_cleans_up_orphans_off_the_gui_thread_at_start(tmp_path, monkeypatch):
    pytest.importorskip("PyQt6.QtWidgets")
    from PyQt6.QtWidgets import QApplication

    from nadare.core.config import Config
    from nadare.gui.main_window import MainWindow

    app = QApplication.instance() or QApplication([])
    seen: list[tuple[set, bool, bool]] = []
    done = threading.Event()

    def fake(profiles, argv=None, *, orphans_only=False):
        on_gui = threading.current_thread() is threading.main_thread()
        seen.append((set(profiles), orphans_only, on_gui))
        done.set()
        return {"updated": [], "removed": [], "warnings": []}

    monkeypatch.setattr(shortcuts, "refresh_steam_shortcuts", fake)
    vdf = tmp_path / "shortcuts.vdf"
    monkeypatch.setattr(shortcuts, "find_steam_shortcut_files", lambda: [vdf])
    config = Config.load(tmp_path / "config.json")
    config.add_profile("g", backend="wine", title="G")
    config.save()
    window = MainWindow(config)
    try:
        assert done.wait(5)
        assert seen == [({"g"}, True, False)]
    finally:
        window.close()
        window.deleteLater()
        app.processEvents()


def test_without_any_steam_shortcut_file_no_thread_is_started(tmp_path, monkeypatch):
    pytest.importorskip("PyQt6.QtWidgets")
    from PyQt6.QtWidgets import QApplication

    from nadare.core.config import Config
    from nadare.gui.main_window import MainWindow

    app = QApplication.instance() or QApplication([])
    monkeypatch.setattr(shortcuts, "find_steam_shortcut_files", lambda: [])
    monkeypatch.setattr(
        shortcuts, "refresh_steam_shortcuts", lambda *a, **k: pytest.fail("started a cleanup")
    )
    window = MainWindow(Config.load(tmp_path / "config.json"))
    try:
        app.processEvents()
    finally:
        window.close()
        window.deleteLater()
        app.processEvents()
