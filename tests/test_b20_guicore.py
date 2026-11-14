# SPDX-License-Identifier: GPL-3.0-or-later
# Copyright (C) 2026 Nadare contributors
"""Regressions for the b20 GUI-core follow-ups (#27, #102, #388, #598, #620, #624)."""

from __future__ import annotations

import os
import threading
import time

os.environ.setdefault("QT_QPA_PLATFORM", "offscreen")

import pytest

try:  # pragma: no cover - exercised only when PyQt6 is missing
    from PyQt6.QtCore import QEvent, Qt
    from PyQt6.QtWidgets import QApplication
except Exception:  # noqa: BLE001
    pytest.skip("PyQt6 is not importable in this interpreter", allow_module_level=True)

from _main_window_patch import patch_main_window

from nadare.core import removal
from nadare.core.backends import Status
from nadare.core.config import Config


@pytest.fixture(scope="module")
def qapp():
    app = QApplication.instance()
    if app is None:
        app = QApplication([])
    yield app


def _wait_until(qapp, predicate, timeout: float = 5.0) -> bool:
    deadline = time.monotonic() + timeout
    while time.monotonic() < deadline:
        qapp.processEvents()
        if predicate():
            return True
        time.sleep(0.005)
    return predicate()


# ---- #27: removal worker must not read the live Config -----------------------
def test_remove_worker_gets_a_detached_config_snapshot(tmp_path, qapp, monkeypatch):
    from nadare.gui import main_window as mw

    config = Config.load(tmp_path / "config.json")
    config.add_profile("p", backend="wine", title="P")
    window = mw.MainWindow(config)
    seen: dict[str, object] = {}

    def fake_remove(cfg, name, **kwargs):
        seen["config"] = cfg
        return removal.RemovalResult(profile=name)

    class _Pool:
        def start(self, task):
            task.run()

    monkeypatch.setattr(removal, "remove_game", fake_remove)
    monkeypatch.setattr(mw.QThreadPool, "globalInstance", lambda: _Pool())
    try:
        window._remove_game("p", False, [])
        assert seen["config"] is not config, "the worker got the live Config"
        assert "p" in seen["config"].profiles, "the snapshot was taken too late"
        assert "p" not in config.profiles
    finally:
        window.close()
        window.deleteLater()
        qapp.processEvents()


# ---- #102: detection/free-space queries must not run on the GUI thread ------
def test_import_detection_runs_off_the_gui_thread(tmp_path, qapp, monkeypatch):
    from nadare.gui import add_game

    dialog = add_game.AddGameDialog(
        Config.load(tmp_path / "config.json"), data_root=tmp_path / "data"
    )
    dialog.set_backend("arknights-yostar")
    dialog.set_mode(True)
    game = tmp_path / "game"
    game.mkdir()
    (game / "Arknights.exe").write_text("MZ")
    dialog.set_install_dir(str(game))

    callers: list[threading.Thread] = []
    gate = threading.Event()

    def fake_detect(kind, folder, prefix):
        gate.wait(5)  # nothing can have run yet when the GUI thread checks below
        callers.append(threading.current_thread())
        return {"install_dir": str(game), "prefix": "", "notes": ["Found it."]}

    monkeypatch.setattr(add_game, "detect_import", fake_detect)
    try:
        assert dialog.create_button.isEnabled()
        dialog._detect_async()
        assert not callers, "detection ran synchronously on the GUI thread"
        gate.set()
        # Add is never disabled while a check runs: a click on a disabled button is lost, and
        # create_profile() re-checks the folder itself.
        assert dialog.create_button.isEnabled()
        assert _wait_until(qapp, lambda: bool(callers))
        assert callers[0] is not threading.main_thread()
        assert dialog.create_button.isEnabled()
    finally:
        dialog.deleteLater()
        qapp.processEvents()


def test_typing_while_a_check_runs_does_not_strand_the_add_button(tmp_path, qapp, monkeypatch):
    from nadare.gui import add_game

    dialog = add_game.AddGameDialog(
        Config.load(tmp_path / "config.json"), data_root=tmp_path / "data"
    )
    dialog.set_backend("arknights-yostar")
    dialog.set_mode(True)
    game = tmp_path / "game"
    game.mkdir()
    (game / "Arknights.exe").write_text("MZ")
    dialog.set_install_dir(str(game))
    monkeypatch.setattr(
        add_game,
        "detect_import",
        lambda kind, folder, prefix: {"install_dir": str(game), "prefix": "", "notes": []},
    )
    try:
        dialog._detect_async()
        dialog.folder.setText(str(game) + "x")  # a keystroke while the worker is still busy
        qapp.processEvents()
        assert dialog.create_button.isEnabled()
        _wait_until(qapp, lambda: False, timeout=0.3)  # let any stale answer arrive
        assert dialog.create_button.isEnabled()
    finally:
        dialog.deleteLater()
        qapp.processEvents()


# ---- #598(a): a status tick must not rebuild an unchanged sidebar ----------
def test_set_status_does_not_rebuild_the_sidebar_when_the_order_stays(qapp, monkeypatch):
    from nadare.gui.sidebar import SidebarGames

    sidebar = SidebarGames()
    sidebar.set_profiles(
        {"a": {"title": "A"}, "b": {"title": "B"}, "c": {"title": "C"}}
    )
    rebuilds: list[bool] = []
    original = sidebar._rebuild

    def counting_rebuild():
        rebuilds.append(True)
        original()

    monkeypatch.setattr(sidebar, "_rebuild", counting_rebuild)
    try:
        sidebar.set_status("a", Status(state="not_installed"))
        assert rebuilds == [], "an unchanged rank still rebuilt every row"
    finally:
        sidebar.deleteLater()
        qapp.processEvents()


# ---- #598(b): sidebar rows are keyboard/AT reachable ------------------------
def test_sidebar_row_is_keyboard_focusable_and_activates(qapp):
    from PyQt6.QtTest import QTest

    from nadare.gui.sidebar import SidebarGames

    sidebar = SidebarGames()
    sidebar.set_profiles({"a": {"title": "A"}})
    opened: list[str] = []
    sidebar.open_game.connect(opened.append)
    row = sidebar.row("a")
    try:
        assert row is not None
        assert row.focusPolicy() == Qt.FocusPolicy.StrongFocus
        assert row.accessibleName()
        QTest.keyClick(row, Qt.Key.Key_Return)
        assert opened == ["a"]
        opened.clear()
        QTest.keyClick(row, Qt.Key.Key_Space)
        assert opened == ["a"]
    finally:
        sidebar.deleteLater()
        qapp.processEvents()


# ---- #598(c): an empty prefix must not invent an install folder ------------
def test_importing_an_empty_prefix_does_not_invent_an_install_dir(tmp_path):
    from nadare.gui.add_game import detect_import

    prefix = tmp_path / "Arknights_EN"
    prefix.mkdir()
    info = detect_import("arknights-yostar", None, str(prefix))
    assert info["install_dir"] == ""
    assert any("not installed" in note.lower() for note in info["notes"])


# ---- #388 F-07: startup probes are capped and continue as they finish ------
def test_startup_probes_are_capped_and_continue_as_they_finish(tmp_path, qapp, monkeypatch):
    from nadare.gui import main_window as mw

    config = Config.load(tmp_path / "config.json")
    window = mw.MainWindow(config)
    for index in range(9):
        config.add_profile(f"p{index}", backend="wine", title=f"P{index}")
    window._current_profile = "p0"
    started: list[str] = []

    def fake_start(profile, operation, payload=None, *, silent=False):
        started.append(profile)
        return True

    monkeypatch.setattr(window, "_start", fake_start)
    try:
        window._probe_all()
        assert 0 < len(started) <= mw.STARTUP_PROBE_LIMIT
        first = started[0]
        window._probe_finished(first)
        assert len(started) == mw.STARTUP_PROBE_LIMIT + 1
    finally:
        window.controller.shutdown()
        window.deleteLater()
        qapp.processEvents()


def test_a_hung_startup_probe_gives_its_slot_back_after_a_while(tmp_path, qapp, monkeypatch):
    from nadare.gui import main_window as mw

    config = Config.load(tmp_path / "config.json")
    window = mw.MainWindow(config)
    for index in range(6):
        config.add_profile(f"p{index}", backend="wine", title=f"P{index}")
    window._current_profile = "p0"
    started: list[str] = []
    def fake_start(profile, op, payload=None, *, silent=False):
        started.append(profile)
        return True

    monkeypatch.setattr(window, "_start", fake_start)
    try:
        window._probe_all()
        assert len(started) == mw.STARTUP_PROBE_LIMIT  # three running, none ever reports back
        patch_main_window(monkeypatch, "STARTUP_PROBE_TIMEOUT", 0.0)
        window._pump_probes()
        assert len(started) > mw.STARTUP_PROBE_LIMIT  # the stuck ones no longer hold the queue
    finally:
        window.controller.shutdown()
        window.deleteLater()
        qapp.processEvents()


def test_queued_startup_probes_do_not_read_an_old_process_snapshot(tmp_path, qapp, monkeypatch):
    """Review wave 3: one shared round cached forever made late probes report old process lists."""
    from nadare.gui import main_window as mw

    config = Config.load(tmp_path / "config.json")
    window = mw.MainWindow(config)
    for index in range(6):
        config.add_profile(f"p{index}", backend="wine", title=f"P{index}")
    window._current_profile = "p0"
    payloads: list[object] = []

    def fake_start(profile, operation, payload=None, *, silent=False):
        payloads.append(payload)
        return True

    monkeypatch.setattr(window, "_start", fake_start)
    try:
        patch_main_window(monkeypatch, "PROBE_ROUND_MAX_AGE", 3600.0)
        window._probe_all()
        first_burst = list(payloads)
        assert len({id(p) for p in first_burst}) == 1  # a burst shares one snapshot
        patch_main_window(monkeypatch, "PROBE_ROUND_MAX_AGE", 0.0)  # everything later is "old"
        for profile in list(window._probe_active):
            window._probe_finished(profile)
        assert len(payloads) > len(first_burst)
        assert all(p is not first_burst[0] for p in payloads[len(first_burst):])
    finally:
        window.controller.shutdown()
        window.deleteLater()
        qapp.processEvents()


# ---- #624 (1): a prefix rebuild writes files and must say so ---------------
def test_close_warns_about_a_prefix_rebuild_as_writing_files(tmp_path, qapp, monkeypatch):
    from PyQt6.QtWidgets import QMessageBox

    from nadare.gui import main_window as mw
    from nadare.gui import workers

    monkeypatch.setattr(
        workers.BackendController, "start", lambda self, profile, op, payload=None: True
    )
    config = Config.load(tmp_path / "config.json")
    config.add_profile("p", backend="wine", title="P")
    window = mw.MainWindow(config)

    class Rebuilding:
        operation = "remake_prefix"

        def isRunning(self) -> bool:  # noqa: N802 - Qt naming
            return True

    window.controller._workers["p"] = Rebuilding()
    prompts: list[tuple[str, str]] = []
    monkeypatch.setattr(
        mw.QMessageBox,
        "warning",
        lambda *args, **kwargs: prompts.append((args[1], args[2]))
        or QMessageBox.StandardButton.Cancel,
    )
    try:
        assert window.close() is False
        assert prompts and "Operation in progress" in prompts[0][0]
    finally:
        monkeypatch.undo()
        window.controller._workers.clear()
        window.close()
        window.deleteLater()
        qapp.processEvents()


# ---- #624 (2): RETRY must show it is checking and refuse a second click ----
def test_retry_disables_the_primary_button_until_a_status_arrives(qapp):
    from nadare.gui.game_view import GameView

    view = GameView()
    view.set_status(Status(state="error", message="offline"))
    requested: list[str] = []
    view.action_requested.connect(requested.append)
    try:
        assert view.primary.text() == "RETRY"
        assert view.primary.isEnabled()
        view.primary.click()
        assert requested == ["status"]
        assert not view.primary.isEnabled(), "a second RETRY click can race the first"
        view.set_status(Status(state="installed"))
        assert view.primary.isEnabled()
    finally:
        view.deleteLater()
        qapp.processEvents()


# ---- #624: the remove dialog must not promise files are kept ---------------
def test_remove_confirmation_matches_the_optional_tree_deletion(tmp_path, qapp, monkeypatch):
    from PyQt6.QtWidgets import QMessageBox

    from nadare.gui import main_window as mw

    config = Config.load(tmp_path / "config.json")
    config.add_profile("p", backend="wine", title="P")
    window = mw.MainWindow(config)
    texts: list[str] = []
    real_set_text = QMessageBox.setInformativeText

    def spy(self, text):
        texts.append(text)
        real_set_text(self, text)  # the real box must stay intact: a hollow one crashes later

    monkeypatch.setattr(mw.QMessageBox, "exec", lambda self: QMessageBox.StandardButton.Cancel)
    monkeypatch.setattr(mw.QMessageBox, "setInformativeText", spy)
    try:
        window._on_remove_requested("p")
        assert texts
        assert "kept unless you tick them" in texts[0].lower()
        assert "not deleted" not in texts[0].lower()
    finally:
        window.close()
        for widget in qapp.topLevelWidgets():
            if isinstance(widget, QMessageBox):
                widget.deleteLater()
        window.deleteLater()
        qapp.sendPostedEvents(None, QEvent.Type.DeferredDelete)
        qapp.processEvents()


# ---- #620: a Steam shortcut with a quote in the profile can be removed ----
def test_steam_shortcut_with_a_quote_in_the_profile_can_be_removed(tmp_path, monkeypatch):
    from nadare.core import shortcuts

    monkeypatch.setattr(shortcuts, "steam_running", lambda: False)
    vdf = tmp_path / "shortcuts.vdf"
    profile = 'a"b'
    assert shortcuts.add_steam_shortcut(vdf, profile, "Game", ["/usr/bin/nadare"])
    assert shortcuts.remove_steam_shortcut(vdf, profile) is True
    assert shortcuts.vdf_loads(vdf.read_bytes()) == []


def test_a_steam_shortcut_profile_with_quotes_and_backslashes_round_trips():
    from nadare.core import shortcuts

    for profile in ('a"b', "plain", "back\\slash", 'both\\"mixed"'):
        entry = {
            "tags": {"0": shortcuts.STEAM_TAG},
            "LaunchOptions": f"launch -p {shortcuts._steam_quote(profile)}",
        }
        assert shortcuts._steam_shortcut_profile(entry) == profile
    assert shortcuts._steam_shortcut_profile({"LaunchOptions": "something else"}) is None


def test_a_free_space_read_does_not_cancel_an_import_check(tmp_path, qapp, monkeypatch):
    from nadare.gui import add_game

    dialog = add_game.AddGameDialog(
        Config.load(tmp_path / "config.json"), data_root=tmp_path / "data"
    )
    dialog.set_backend("arknights-yostar")
    dialog.set_mode(True)
    game = tmp_path / "game"
    game.mkdir()
    (game / "Arknights.exe").write_text("MZ")
    dialog.set_install_dir(str(game))
    monkeypatch.setattr(
        add_game,
        "detect_import",
        lambda kind, folder, prefix: {
            "install_dir": str(game),
            "prefix": "",
            "notes": ["Found it from the worker."],
        },
    )
    try:
        dialog._detect_async()
        dialog._update_free_space_async()  # same text, so only the counter could drop the answer
        assert _wait_until(qapp, lambda: "Found it from the worker." in dialog.detect_label.text())
    finally:
        dialog.deleteLater()
        qapp.processEvents()


# ---- #951: uninstalling is easy to find, and the button says what it will do ---------------
def test_ticking_game_files_turns_remove_into_uninstall(tmp_path, qapp, monkeypatch):
    from types import SimpleNamespace

    from PyQt6.QtWidgets import QCheckBox, QMessageBox

    from nadare.core import removal
    from nadare.gui import main_window as mw

    config = Config.load(tmp_path / "config.json")
    config.add_profile("p", backend="wine", title="P")
    window = mw.MainWindow(config)
    tree = SimpleNamespace(key="install_dir", path=tmp_path / "game", safe=True, reason="")
    monkeypatch.setattr(removal, "removable_trees", lambda cfg, profile: [tree])
    seen: dict[str, object] = {}

    def fake_exec(box):
        yes = box.button(QMessageBox.StandardButton.Yes)
        seen["title"] = box.windowTitle()
        seen["before"] = yes.text()
        game_box = next(c for c in box.findChildren(QCheckBox) if "game install folder" in c.text())
        game_box.setChecked(True)
        seen["ticked"] = yes.text()
        game_box.setChecked(False)
        seen["unticked"] = yes.text()
        return QMessageBox.StandardButton.Cancel

    monkeypatch.setattr(mw.QMessageBox, "exec", fake_exec)
    try:
        window._on_remove_requested("p")
        assert seen == {"title": "Uninstall or remove game", "before": "Remove",
                        "ticked": "Uninstall", "unticked": "Remove"}
    finally:
        window.close()
        for widget in qapp.topLevelWidgets():
            if isinstance(widget, QMessageBox):
                widget.deleteLater()
        window.deleteLater()
        qapp.processEvents()


def test_every_entry_point_says_uninstall():
    from pathlib import Path

    for name in ("game_view.py", "library.py", "settings.py"):
        text = Path("src/nadare/gui", name).read_text(encoding="utf-8")
        assert "Remove from library…" not in text
        assert "Uninstall or remove…" in text
