# SPDX-License-Identifier: GPL-3.0-or-later
"""Update and Repair always end with a visible result (user sim r2 #6)."""
import pytest
from PyQt6.QtWidgets import QApplication

from nadare.core.backends import Status
from nadare.core.config import Config
from nadare.core.events import Event
from nadare.gui.main_window import MainWindow

SUMMARY = "Sample: synced 0 item(s), 1 already up to date"


@pytest.fixture
def window(tmp_path, monkeypatch):
    app = QApplication.instance() or QApplication([])
    config = Config.load(tmp_path / "config.json")
    config.add_profile("alpha", backend="wine")
    monkeypatch.setattr(MainWindow, "_probe_all", lambda self: None)
    win = MainWindow(config)
    started = []
    monkeypatch.setattr(win, "_start", lambda *a, **k: started.append(a) or False)
    monkeypatch.setattr(win, "_notify_finished", lambda *a: None)
    monkeypatch.setattr(win, "_maybe_offer_runner_cleanup", lambda: None)
    monkeypatch.setattr(win, "_maybe_offer_quality_cleanup", lambda *a: None)
    monkeypatch.setattr(win, "_confirm_close", lambda: True)
    win._current_profile = "alpha"
    win.game.set_profile("alpha", config.profiles["alpha"])
    yield win, app
    win.close()
    win.deleteLater()
    app.processEvents()


def run(win, app, operation, lines):
    win._on_busy("alpha", operation, True)
    for kind, text in lines:
        win._on_event("alpha", Event(kind, text))
    win._on_busy("alpha", operation, False)
    win._on_succeeded("alpha", operation, 0)
    # the status re-probe that follows every success
    win._on_busy("alpha", "status", True)
    win._on_result("alpha", "status", Status("installed", version="293"))
    win._on_busy("alpha", "status", False)
    win._on_succeeded("alpha", "status", 0)
    app.processEvents()
    return win.game.step_label


@pytest.mark.parametrize("operation", ["update", "repair"])
def test_the_result_survives_the_status_probe(window, operation):
    win, app = window
    label = run(win, app, operation, [("log", SUMMARY)])
    assert label.text().startswith("✓ "), label.text()
    assert label.isVisibleTo(win.game), "result line hidden"


def test_repair_with_nothing_to_fix_says_so(window):
    win, app = window
    label = run(win, app, "repair", [("log", SUMMARY)])
    assert "nothing" in label.text().lower() or "up to date" in label.text().lower(), label.text()


def test_update_with_nothing_new_says_up_to_date(window):
    win, app = window
    label = run(win, app, "update", [("log", SUMMARY)])
    assert "up to date" in label.text().lower(), label.text()


def test_repair_that_fixed_files_says_how_many(window):
    win, app = window
    label = run(win, app, "repair", [("log", "Sample: synced 3 item(s), 40 already up to date")])
    assert "3" in label.text(), label.text()


def test_an_update_that_patched_files_names_the_versions_not_up_to_date(window):
    win, app = window
    label = run(win, app, "update", [
        ("log", "Sample: update 2.1 -> 2.2: downloaded 1.2 GiB (patches 1.0 GiB, whole files 0 B)"),
        ("log", "Sample: synced 0 item(s), 40 already up to date"),
    ])
    assert "2.1 -> 2.2" in label.text(), label.text()
    assert "up to date" not in label.text().lower(), label.text()
