# SPDX-License-Identifier: GPL-3.0-or-later
"""Regressions found by the round-3 audit of the post-35b2833 fixes."""

from __future__ import annotations

import os
import stat

import pytest

from nadare.core import paths, shortcuts
from nadare.core.events import mask


def test_truncated_vdf_with_other_shortcuts_is_never_overwritten(tmp_path, monkeypatch):
    monkeypatch.setattr(shortcuts, "steam_running", lambda: False)
    data = shortcuts.vdf_dumps([{"AppName": "Keep", "tags": {}}, {"AppName": "Also", "tags": {}}])
    vdf = tmp_path / "shortcuts.vdf"
    cut = data[: len(shortcuts._SHORTCUTS_HEADER) + 12]
    vdf.write_bytes(cut)
    with pytest.raises(shortcuts.ShortcutError):
        shortcuts.add_steam_shortcut(vdf, "prof", "New", ["/opt/x"])
    assert vdf.read_bytes() == cut
    # Read-only callers still degrade to "no shortcuts".
    assert shortcuts.vdf_loads(cut) == []
    with pytest.raises(ValueError):
        shortcuts.vdf_loads(cut, strict=True)


def test_mask_redacts_an_empty_username_password():
    out = mask("GET https://:longlowercasepassword@example.com/x")
    assert "longlowercasepassword" not in out


def test_ensure_dirs_tightens_preexisting_dirs(tmp_path, monkeypatch):
    for name, var in (
        ("data", "XDG_DATA_HOME"),
        ("config", "XDG_CONFIG_HOME"),
        ("state", "XDG_STATE_HOME"),
        ("cache", "XDG_CACHE_HOME"),
    ):
        monkeypatch.setenv(var, str(tmp_path / name))
    old = paths.data_dir()
    old.mkdir(parents=True)
    os.chmod(old, 0o755)
    paths.ensure_dirs()
    assert stat.S_IMODE(old.stat().st_mode) == 0o700


# --- GUI ---------------------------------------------------------------------
os.environ.setdefault("QT_QPA_PLATFORM", "offscreen")
try:  # pragma: no cover
    from PyQt6.QtWidgets import QApplication
except Exception:  # noqa: BLE001
    QApplication = None

gui = pytest.mark.skipif(QApplication is None, reason="PyQt6 is not importable")


@pytest.fixture(scope="module")
def qapp():
    app = QApplication.instance() or QApplication([])
    yield app


@gui
def test_logo_with_an_unresolvable_home_is_ignored(qapp):
    from nadare.gui import sidebar

    sidebar.icon_pixmap({"logo": "~nosuchuser_zzz/x.png", "title": "G"}, "G")


@gui
def test_title_tooltips_are_not_rich_text(qapp):
    from nadare.gui.library import GameCard

    card = GameCard("p", "<b>X</b>", "wine")
    assert "<b>X</b>" not in card.title_label.toolTip()


@gui
def test_shutdown_reports_dropped_queued_operations(tmp_path, qapp):
    from nadare.core.config import Config
    from nadare.gui.workers import BackendController

    controller = BackendController(Config.load(tmp_path / "config.json"))
    controller._pending["p"] = ("install", None)
    busy, cancelled = [], []
    controller.busy.connect(lambda *a: busy.append(a))
    controller.cancelled.connect(lambda *a: cancelled.append(a))
    controller.shutdown(wait_ms=10, keep_launches=True)
    assert ("p", "install", False) in busy and ("p", "install") in cancelled
    assert controller._pending == {}


@gui
def test_indeterminate_progress_event_shows_a_busy_bar(qapp):
    from nadare.core.events import Event
    from nadare.gui.game_view import GameView

    view = GameView()
    try:
        view.set_busy("install")
        view.append_event(
            Event("progress", "Installing the WebView2 Runtime…", data={"indeterminate": True})
        )
        assert view.progress.maximum() == 0  # busy animation
        assert "WebView2" in view.step_label.text()
        view.append_event(Event("progress", "downloading", progress=0.5))
        assert view.progress.maximum() == 100 and view.progress.value() == 50
        view.append_event(
            Event("progress", "again…", data={"indeterminate": True})
        )
        view.finish("install", 0)
        assert view.progress.maximum() == 100  # no stuck animation after the run
    finally:
        view.deleteLater()
