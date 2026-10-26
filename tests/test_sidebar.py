# SPDX-License-Identifier: GPL-3.0-or-later
"""Offscreen tests for the sidebar's game list.

Every game in the library appears in the sidebar: running first, then installed
(or updatable) games, then the rest, each alphabetical; status results reorder rows live.
"""

from __future__ import annotations

import os

os.environ.setdefault("QT_QPA_PLATFORM", "offscreen")

import pytest

try:  # pragma: no cover - exercised only when PyQt6 is missing
    from PyQt6.QtCore import Qt
    from PyQt6.QtWidgets import QApplication  # noqa: F401
except Exception:  # noqa: BLE001
    pytest.skip("PyQt6 is not importable in this interpreter", allow_module_level=True)

from nadare.core.backends import Status
from nadare.core.config import Config
from nadare.gui.main_window import PAGE_GAME, MainWindow


@pytest.fixture
def window(qtbot, tmp_path, monkeypatch):
    """A window with three profiles and a controller that never runs a backend."""
    from nadare.gui import workers

    monkeypatch.setattr(
        workers.BackendController,
        "start",
        lambda self, profile, operation, payload=None: True,
    )
    config = Config.load(tmp_path / "config.json")
    config.add_profile("alpha", backend="wine", title="Alpha")
    config.add_profile("beta", backend="wine", title="Beta")
    config.add_profile("gamma", backend="wine", title="Gamma")
    win = MainWindow(config)
    qtbot.addWidget(win)
    return win


def test_every_library_game_is_listed_installed_ones_first(window):
    window._on_result("alpha", Status("not_installed"))
    window._on_result("beta", Status("installed"))
    window._on_result("gamma", Status("error"))

    assert window.sidebar_games.profiles() == ["beta", "alpha", "gamma"]
    assert all(window.sidebar_games.row(name) is not None for name in ("alpha", "beta", "gamma"))


def test_games_are_listed_before_any_status_is_known(window):
    # Nothing has reported yet (the first probe is still running): no game may be missing.
    assert window.sidebar_games.profiles() == ["alpha", "beta", "gamma"]


def test_status_change_reorders_rows_but_never_drops_one(window):
    window._on_result("alpha", Status("not_installed"))
    window._on_result("beta", Status("installed"))
    assert window.sidebar_games.profiles() == ["beta", "alpha", "gamma"]

    window._on_result("alpha", Status("installed"))
    assert window.sidebar_games.profiles() == ["alpha", "beta", "gamma"]

    window._on_result("alpha", Status("not_installed"))
    assert window.sidebar_games.profiles() == ["beta", "alpha", "gamma"]


def test_a_game_added_to_the_library_shows_at_once_even_if_not_installed(window):
    window.config.add_profile("delta", backend="wine", title="Delta")
    window.refresh_library()

    assert "delta" in window.sidebar_games.profiles()
    row = window.sidebar_games.row("delta")
    assert row is not None and row.dot.isHidden()  # no running or update dot


def test_removing_a_game_still_removes_its_row(window):
    assert window.sidebar_games.row("beta") is not None
    del window.config.profiles["beta"]
    window.refresh_library()

    assert window.sidebar_games.profiles() == ["alpha", "gamma"]


def test_running_games_sort_first_then_alphabetically(window):
    window._on_result("gamma", Status("installed"))
    window._on_result("alpha", Status("installed"))
    window._on_result("beta", Status("running", running=True))

    assert window.sidebar_games.profiles() == ["beta", "alpha", "gamma"]


def test_initial_profile_probe_passes_one_shared_proc_round(window, monkeypatch):
    started = []
    monkeypatch.setattr(
        window,
        "_start",
        lambda profile, operation, payload=None, silent=False: started.append(
            (profile, operation, payload, silent)
        ) or True,
    )
    window._current_profile = ""

    window._probe_all()

    assert len(started) == 3
    assert all(operation == "status" and silent for _, operation, _, silent in started)
    assert started[0][2] is not None
    assert all(payload is started[0][2] for _, _, payload, _ in started)


def test_running_and_update_dots(window):
    window._on_result("alpha", Status("running", running=True))
    running = window.sidebar_games.row("alpha")
    assert running is not None
    assert running.dot.property("kind") == "running"
    assert not running.dot.isHidden()

    window._on_result("beta", Status("installed", data={"update_available": True}))
    update = window.sidebar_games.row("beta")
    assert update is not None
    assert update.dot.property("kind") == "update"
    assert not update.dot.isHidden()


def test_sidebar_decodes_cover_icons_off_the_gui_thread(window, tmp_path):
    import threading

    from PyQt6.QtGui import QColor, QImage

    cover = tmp_path / "cover.png"
    image = QImage(320, 480, QImage.Format.Format_RGB32)
    image.fill(QColor("steelblue"))
    assert image.save(str(cover))

    sidebar = window.sidebar_games
    loader = sidebar._images
    main_thread = threading.get_ident()
    decode_threads = []
    original_decode = loader._decode

    def record_thread(*args, **kwargs):
        decode_threads.append(threading.get_ident())
        return original_decode(*args, **kwargs)

    loader._decode = record_thread
    sidebar.set_profiles(
        {"alpha": {"backend": "wine", "title": "Alpha", "cover": str(cover)}},
        {"alpha": Status("installed")},
    )
    loader.wait()

    row = sidebar.row("alpha")
    assert row is not None and not row.icon.pixmap().isNull()
    assert decode_threads and all(thread_id != main_thread for thread_id in decode_threads)


def test_click_opens_the_game_page(window, qtbot):
    window.show()
    window.show_library()
    window._on_result("alpha", Status("installed"))
    qtbot.wait(10)

    row = window.sidebar_games.row("alpha")
    assert row is not None
    qtbot.mouseClick(row, Qt.MouseButton.LeftButton)

    assert window._current_profile == "alpha"
    assert window.stack.currentIndex() == PAGE_GAME
    assert row.property("active") == "true"


def test_click_that_rebuilds_rows_does_not_crash(window, qtbot):
    window.show()
    window.show_library()
    window._on_result("alpha", Status("installed"))
    qtbot.wait(10)

    row = window.sidebar_games.row("alpha")
    assert row is not None
    # Opening a game can trigger a synchronous status refresh that rebuilds the
    # list while the row's mouse handler is still on the stack.
    row.clicked.connect(
        lambda _profile: window.sidebar_games.set_status(
            "alpha", Status("running", running=True)
        )
    )
    qtbot.mouseClick(row, Qt.MouseButton.LeftButton)

    assert window.stack.currentIndex() == PAGE_GAME
    assert window.sidebar_games.row("alpha") is row


def test_busy_dialog_only_for_foreground_operations(window, monkeypatch):
    from PyQt6.QtWidgets import QMessageBox

    dialogs: list[tuple] = []
    monkeypatch.setattr(window.controller, "start", lambda *a, **k: False)
    monkeypatch.setattr(QMessageBox, "information", lambda *a, **k: dialogs.append(a))

    window._start("alpha", "launch")
    assert len(dialogs) == 1

    # A background refresh that loses the race is skipped silently.
    window._start("alpha", "status", silent=True)
    assert len(dialogs) == 1


def test_active_row_follows_open_game(window):
    window._on_result("alpha", Status("installed"))
    window._on_result("beta", Status("installed"))
    window.open_game("alpha")
    assert window.sidebar_games.row("alpha").property("active") == "true"
    assert window.sidebar_games.row("beta").property("active") == "false"

    window.open_game("beta")
    assert window.sidebar_games.row("beta").property("active") == "true"
    assert window.sidebar_games.row("alpha").property("active") == "false"
