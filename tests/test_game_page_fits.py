# SPDX-License-Identifier: GPL-3.0-or-later
"""The game page fits the smallest window (b23-rc2 click-through, 2026-11-11).

At 880 px (the minimum, and the first-run size on a 1366-1760 px screen) the header's
Settings and More (Remove, Move) buttons and the cover were cut off on the right, with
no way to scroll to them.
"""

from __future__ import annotations

import os

os.environ.setdefault("QT_QPA_PLATFORM", "offscreen")

import pytest

try:
    from PyQt6.QtCore import QPoint
    from PyQt6.QtWidgets import QApplication
except ImportError:  # pragma: no cover
    pytest.skip("PyQt6 is not importable", allow_module_level=True)

from nadare.core.config import Config
from nadare.gui.main_window import MainWindow


@pytest.fixture(scope="module")
def qapp():
    return QApplication.instance() or QApplication([])


@pytest.fixture
def window(tmp_path, monkeypatch, qapp):
    for key in ("DATA", "CONFIG", "STATE", "CACHE"):
        monkeypatch.setenv(f"XDG_{key}_HOME", str(tmp_path / key.lower()))
    cfg = Config.load(tmp_path / "config.json")
    cfg.add_profile("bd2", backend="direct-bd2", title="Brown Dust 2")
    cfg.add_profile("nte-launcher", backend="launcher-nte", title="Neverness to Everness")
    cfg.save()
    win = MainWindow(cfg)
    win.show()
    win.resize(win.minimumWidth(), 952)
    qapp.processEvents()
    yield win
    win.close()
    win.deleteLater()
    qapp.processEvents()


def _right_edge(widget, root) -> int:
    return widget.mapTo(root, QPoint(widget.width(), 0)).x()


@pytest.mark.parametrize("profile", ["bd2", "nte-launcher"])
def test_the_header_buttons_and_cover_fit_the_smallest_window(window, qapp, profile):
    window.open_game(profile)
    for _ in range(5):
        qapp.processEvents()
    page = window.game
    viewport = page.scroll.viewport()
    limit = _right_edge(viewport, window)
    assert window.width() == window.minimumWidth()
    assert page.more_button.isVisible()
    assert _right_edge(page.more_button, window) <= limit
    assert page.art_label.isVisible()  # the cover always has a pixmap (a placeholder at worst)
    assert _right_edge(page.art_label, window) <= limit


def test_a_retired_game_offers_remove_not_retry(window, qapp, monkeypatch):
    """b23-rc2 click-through: RETRY on a retired launcher kind can never work."""
    from PyQt6.QtWidgets import QMessageBox

    boxes: list[str] = []
    monkeypatch.setattr(
        QMessageBox, "exec", lambda self: boxes.append(self.text()) or QMessageBox.StandardButton.No
    )
    window.open_game("nte-launcher")
    for _ in range(20):
        qapp.processEvents()
    page = window.game
    asked: list[str] = []
    page.remove_requested.connect(asked.append)
    assert "REMOVE" in page.primary.text()
    page.primary.click()
    assert asked == ["nte-launcher"]
    assert any("Remove" in text for text in boxes)  # the usual Remove question, answered No
    assert "nte-launcher" in window.config.profiles


def test_primary_operation_for_a_retired_kind():
    from nadare.core.backends import Status
    from nadare.core.gamefacts import primary_operation

    assert primary_operation(Status(state="error", message="x", data={"retired": True})) == "remove"
    assert primary_operation(Status(state="error", message="x")) == "retry"


def test_the_page_says_removing_while_a_retired_game_is_removed(window, qapp, monkeypatch):
    """Round 3: REMOVE stayed enabled while its own removal ran."""
    from PyQt6.QtWidgets import QMessageBox

    monkeypatch.setattr(
        QMessageBox, "exec", lambda self: QMessageBox.StandardButton.Yes
    )
    monkeypatch.setattr(window, "_ask_remove_trees", lambda *a, **k: ((), False), raising=False)
    started = []
    monkeypatch.setattr(
        "nadare.gui.main_window.QThreadPool.start", lambda self, task: started.append(task)
    )
    window.open_game("nte-launcher")
    for _ in range(20):
        qapp.processEvents()
    window.game.primary.click()
    qapp.processEvents()
    try:
        assert window._busy.get("nte-launcher") == "remove"
        assert window.game._busy_op == "remove"
        assert not window.game.primary.isEnabled()
        assert window.game.primary.text().startswith("REMOVING")
    finally:
        window._busy.pop("nte-launcher", None)  # the stubbed worker never finishes
