# SPDX-License-Identifier: GPL-3.0-or-later
"""#1732: a launch that fails at once says which runner it ran on.

A b24 player's every game crash-looped until they picked DW-Proton by hand. The box said only
"exit code N, open the logs", so nothing pointed at the runner. The launch now reports the runner
it used and whether that is the recommended build; a quick failure names it, and a runner that is
not the recommended one gets the advice to try that one, with a button to the game's settings.
"""

from __future__ import annotations

import os

os.environ.setdefault("QT_QPA_PLATFORM", "offscreen")

import time
from pathlib import Path

import pytest
from runner_layout import make_runner as make_runner_layout

from nadare.core import runners
from nadare.core.events import Event

PIN = runners.pinned_tag(runners.DEFAULT_FAMILY)


# ---- what the backend reports ----------------------------------------------------------------
@pytest.fixture
def own(tmp_path, monkeypatch):
    folder = tmp_path / "runners"
    folder.mkdir()
    monkeypatch.setattr(runners.paths, "runners_dir", lambda: folder)
    monkeypatch.setattr(runners, "steam_runners", lambda: [])
    return folder


def _runner(root: Path, name: str, version: str | None = None) -> Path:
    return make_runner_layout(root / name, version=version)


def test_the_recommended_build_is_reported_as_recommended(own):
    pinned = _runner(own, f"{PIN}-x86_64", f"1794581887 {PIN}")
    facts = runners.launch_facts(str(pinned))
    assert facts == {"runner": PIN, "runner_recommended": True, "runner_default": PIN}


def test_another_build_is_named_and_not_recommended(own):
    other = _runner(own, "GE-Proton10-3", "1700000000 GE-Proton10-3")
    facts = runners.launch_facts(str(other))
    assert facts["runner"] == "GE-Proton10-3"
    assert facts["runner_recommended"] is False
    assert facts["runner_default"] == PIN


def test_a_build_without_a_version_file_is_named_by_its_folder(own):
    odd = _runner(own, "my-proton")
    assert runners.launch_facts(str(odd))["runner"] == "my-proton"


def test_a_folder_named_like_the_pin_elsewhere_is_not_recommended(own, tmp_path):
    # A copy in some other folder that Nadare would not pick as the pin (#1731).
    _runner(own, f"{PIN}-x86_64", f"1794581887 {PIN}")
    elsewhere = tmp_path / "elsewhere"
    elsewhere.mkdir()
    copy = _runner(elsewhere, f"{PIN}-x86_64", f"1794581887 {PIN}")
    assert runners.launch_facts(str(copy))["runner_recommended"] is False


def test_plain_wine_is_named_and_never_recommended(own):
    facts = runners.launch_facts(None, wine="/opt/wine-9.0/bin/wine64")
    assert facts["runner"] == "Wine (/opt/wine-9.0/bin/wine64)"
    assert facts["runner_recommended"] is False
    assert runners.launch_facts(None)["runner"] == "system Wine"


def test_the_wine_game_backend_reports_the_runner_it_resolves(own, tmp_path, monkeypatch):
    from _backend_helpers import make_context

    from nadare.backends.winegame import WineGameBackend

    other = _runner(own, "GE-Proton10-3", "1700000000 GE-Proton10-3")
    backend = WineGameBackend(make_context(tmp_path, "wine", {}))
    monkeypatch.setattr(backend, "resolve_runner", lambda: (str(other), "proton"))
    assert backend._runner_facts()["runner"] == "GE-Proton10-3"
    monkeypatch.setattr(backend, "resolve_runner", lambda: (None, "wine"))
    assert backend._runner_facts()["runner_recommended"] is False


# ---- what the game page says -----------------------------------------------------------------
@pytest.fixture
def view():
    try:
        from PyQt6.QtWidgets import QApplication
    except Exception:  # pragma: no cover - optional GUI dependencies
        pytest.skip("PyQt6 is not importable in this interpreter")
    from nadare.gui.game_view import GameView

    app = QApplication.instance() or QApplication([])
    page = GameView()
    page.set_profile("g", {"backend": "wine", "title": "G"})
    shown: list[dict] = []

    def choice(title, message, can_retry, *, settings=False):
        shown.append({"title": title, "message": message, "settings": settings})
        return page.answer

    page.answer = "close"
    page._failure_choice = choice
    page.shown = shown
    opened: list[bool] = []
    page.settings_requested.connect(lambda: opened.append(True))
    page.opened = opened
    yield page
    page.deleteLater()
    app.processEvents()


def _launched(view, seconds: float, **facts) -> None:
    view.set_busy("launch")
    view.append_event(Event("log", "runner: x", data=facts))
    view._last_step_at = time.time() - seconds


def test_a_quick_failure_on_another_runner_names_it_and_points_to_the_pin(view):
    _launched(view, 3, runner="GE-Proton10-3", runner_recommended=False, runner_default=PIN)
    view.finish("launch", 5)
    (box,) = view.shown
    assert "GE-Proton10-3" in box["message"]
    assert PIN in box["message"] and "recommended" in box["message"]
    assert box["settings"] is True


def test_the_settings_button_opens_the_games_settings(view):
    view.answer = "settings"
    _launched(view, 3, runner="GE-Proton10-3", runner_recommended=False, runner_default=PIN)
    view.finish("launch", 5)
    assert view.opened == [True]


def test_a_quick_failure_on_the_recommended_runner_names_it_without_advice(view):
    _launched(view, 3, runner=PIN, runner_recommended=True, runner_default=PIN)
    view.finish("launch", 5)
    (box,) = view.shown
    assert PIN in box["message"]
    assert "try the recommended" not in box["message"].lower()
    assert box["settings"] is False


def test_a_crash_after_a_long_session_names_the_runner_too(view):
    _launched(view, 600, runner="GE-Proton10-3", runner_recommended=False, runner_default=PIN)
    view.finish("launch", -11)
    (box,) = view.shown
    assert "GE-Proton10-3" in box["message"] and box["settings"] is True


def test_a_launch_that_reported_no_runner_keeps_the_old_box(view):
    view.set_busy("launch")
    view._last_step_at = time.time() - 3
    view.finish("launch", 5)
    (box,) = view.shown
    assert "did not finish" in box["message"] and "ran on" not in box["message"]
    assert box["settings"] is False


def test_the_runner_of_an_earlier_launch_is_forgotten(view):
    _launched(view, 3, runner="GE-Proton10-3", runner_recommended=False, runner_default=PIN)
    view.finish("launch", 0)
    view.set_busy("launch")
    view._last_step_at = time.time() - 3
    view.finish("launch", 5)
    assert "GE-Proton10-3" not in view.shown[-1]["message"]


def test_a_normal_quit_says_nothing_about_the_runner(view):
    _launched(view, 600, runner="GE-Proton10-3", runner_recommended=False, runner_default=PIN)
    view.finish("launch", 1)
    assert view.shown == []
