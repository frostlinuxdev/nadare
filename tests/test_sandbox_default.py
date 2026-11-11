# SPDX-License-Identifier: GPL-3.0-or-later
"""The Sandbox is on by default for every game that offers it.

An unset ``secure_mode`` means on; an explicit ``False`` keeps it off, an explicit ``True`` is a
promise: no bubblewrap then stops the launch. A game that is on only by default still starts
when bubblewrap cannot run here, says so, and the page's pill shows what the process really got.
"""

from __future__ import annotations

import os
from pathlib import Path

os.environ.setdefault("QT_QPA_PLATFORM", "offscreen")

import pytest
from _backend_helpers import make_context, write_exe

from nadare.backends.launcher import NikkeBackend
from nadare.core import backends, gamesandbox, sandbox
from nadare.core.backends import BackendError
from nadare.core.events import Event

SUPPORTED = ("direct-pgr", "launcher-nikke", "stellasora-yostar")


@pytest.fixture
def default_on(monkeypatch):
    """Undo the suite-wide test default (conftest turns it off for older tests)."""
    monkeypatch.setattr(gamesandbox, "DEFAULT_ON", True)


@pytest.fixture
def has_bwrap(monkeypatch):
    monkeypatch.setattr(gamesandbox.shutil, "which", lambda name, **_kw: "/usr/bin/bwrap")
    monkeypatch.setattr(gamesandbox, "works", lambda binary=gamesandbox.BINARY: (True, ""))


@pytest.fixture
def no_bwrap(monkeypatch):
    monkeypatch.setattr(gamesandbox.shutil, "which", lambda name, **_kw: None)
    monkeypatch.setattr(gamesandbox, "locate", lambda **_: gamesandbox.Located(binary=None))


def _backend(tmp_path: Path, events: list, **profile) -> NikkeBackend:
    values = {"prefix": str(tmp_path / "prefix"), "wine_binary": "/usr/bin/wine", **profile}
    write_exe(tmp_path / "prefix" / "drive_c" / "NIKKE" / "Launcher" / "nikke_launcher.exe", "MZ")
    ctx = make_context(tmp_path, "launcher-nikke", values)
    ctx.emit = events.append
    return NikkeBackend(ctx)


def _sandbox_reports(events: list) -> list[bool]:
    return [e.data["sandbox"] for e in events if "sandbox" in (e.data or {})]


# ---- the rule ------------------------------------------------------------------------------
def test_the_shipped_default_is_on():
    import importlib

    fresh = importlib.reload(gamesandbox)
    try:
        assert fresh.DEFAULT_ON is True
    finally:
        importlib.reload(gamesandbox)


@pytest.mark.parametrize(
    ("profile", "supported", "expected"),
    [
        ({}, True, True),
        ({"secure_mode": True}, True, True),
        ({"secure_mode": False}, True, False),
        ({"extra": {"secure_mode": False}}, True, False),
        ({"extra": {"secure_mode": True}}, True, True),
        ({}, False, False),
        ({"secure_mode": True}, False, False),
    ],
)
def test_wanted_follows_the_choice_then_the_default(default_on, profile, supported, expected):
    assert gamesandbox.wanted(profile, supported=supported) is expected


@pytest.mark.parametrize(
    ("profile", "expected"),
    [({}, None), ({"secure_mode": True}, True), ({"secure_mode": False}, False),
     ({"extra": {"secure_mode": False}}, False)],
)
def test_choice_tells_an_explicit_answer_from_none(profile, expected):
    assert gamesandbox.choice(profile) is expected


def test_the_test_default_is_off_unless_a_test_asks():
    assert gamesandbox.wanted({}, supported=True) is False


@pytest.mark.parametrize("kind", SUPPORTED)
def test_the_settings_option_defaults_to_on(kind):
    option = next(o for o in backends.get(kind).options if o.key == "secure_mode")
    assert option.default is True


# ---- the launch ----------------------------------------------------------------------------
def test_an_unset_profile_plays_in_the_sandbox(tmp_path, default_on, has_bwrap):
    events: list = []
    argv, _env, _target = _backend(tmp_path, events).build_launch()
    assert argv[0] == "bwrap"
    assert _sandbox_reports(events) == [True]


def test_an_explicit_off_plays_without_it(tmp_path, default_on, has_bwrap):
    events: list = []
    argv, _env, _target = _backend(tmp_path, events, secure_mode=False).build_launch()
    assert argv[0] == "/usr/bin/wine"
    assert _sandbox_reports(events) in ([], [False])


def test_default_on_without_bubblewrap_plays_unprotected_and_says_so(
    tmp_path, default_on, no_bwrap
):
    events: list = []
    argv, _env, _target = _backend(tmp_path, events).build_launch()
    assert argv[0] == "/usr/bin/wine"
    assert _sandbox_reports(events) == [False]
    note = next(e.message for e in events if (e.data or {}).get("sandbox") is False)
    assert "Sandbox" in note and "without" in note and "bubblewrap" in note


def test_default_on_with_a_blocked_bubblewrap_plays_unprotected_and_says_why(
    tmp_path, default_on, has_bwrap, monkeypatch
):
    monkeypatch.setattr(
        gamesandbox, "works", lambda binary=gamesandbox.BINARY: (False, "uid map: denied")
    )
    events: list = []
    argv, _env, _target = _backend(tmp_path, events).build_launch()
    assert argv[0] == "/usr/bin/wine"
    note = next(e.message for e in events if (e.data or {}).get("sandbox") is False)
    assert "uid map" in note


def test_an_explicit_on_without_bubblewrap_still_stops_the_launch(
    tmp_path, default_on, no_bwrap
):
    with pytest.raises(BackendError) as caught:
        _backend(tmp_path, [], secure_mode=True).build_launch()
    assert "bubblewrap" in str(caught.value)


def test_default_on_in_the_flatpak_plays_in_flatpaks_own_sandbox(
    tmp_path, default_on, monkeypatch
):
    monkeypatch.setattr(sandbox, "in_flatpak", lambda: True)
    monkeypatch.setattr(gamesandbox, "_flatpak_spawn", lambda: "/usr/bin/flatpak-spawn")
    (tmp_path / "run").mkdir()
    monkeypatch.setenv("XDG_RUNTIME_DIR", str(tmp_path / "run"))
    events: list = []
    backend = _backend(tmp_path, events)
    try:
        argv, _env, _target = backend.build_launch()
    finally:
        backend._stop_link_relay()
    assert "flatpak-spawn" in argv and "--sandbox" in argv
    assert _sandbox_reports(events) == [True]


def test_an_explicit_on_in_the_flatpak_refuses_without_flatpak_spawn(
    tmp_path, default_on, monkeypatch
):
    monkeypatch.setattr(sandbox, "in_flatpak", lambda: True)
    monkeypatch.setattr(gamesandbox, "_flatpak_spawn", lambda: None)
    with pytest.raises(BackendError):
        _backend(tmp_path, [], secure_mode=True).build_launch()


def test_default_on_with_a_game_in_the_home_folder_stops_and_says_how(
    tmp_path, default_on, has_bwrap, monkeypatch
):
    """Maintainer, 2026-11-11: never run a game unsandboxed just because its folder is too broad
    (it used to play unprotected with only a log line)."""

    def broad(*args, **kwargs):
        raise gamesandbox.BroadBindError("the game folder is your home folder")

    monkeypatch.setattr(gamesandbox, "wrap", broad)
    events: list = []
    with pytest.raises(BackendError) as caught:
        _backend(tmp_path, events).build_launch()
    text = str(caught.value)
    assert "home folder" in text and "Move" in text and "Sandbox off" in text
    assert _sandbox_reports(events) != [False]


# ---- the window and the page ---------------------------------------------------------------
try:
    from PyQt6.QtWidgets import QApplication

    from nadare.core.config import Config
    from nadare.gui.game_view import GameView
    from nadare.gui.main_window import MainWindow

    HAVE_QT = True
except Exception:  # noqa: BLE001 - optional GUI dependencies
    HAVE_QT = False

needs_qt = pytest.mark.skipif(not HAVE_QT, reason="PyQt6 is not importable")


@pytest.fixture(scope="module")
def qapp():
    return QApplication.instance() or QApplication([])


def _window(tmp_path, **profile):
    config = Config.load(tmp_path / "config.json")
    config.profiles["pgr"] = {
        "backend": "direct-pgr", "install_dir": str(tmp_path / "g"), **profile
    }
    window = MainWindow(config)
    window._current_profile = "pgr"
    window.game.set_profile("pgr", config.profiles["pgr"])
    return window


def _close(window, qapp):
    window.close()
    window.deleteLater()
    qapp.processEvents()


@needs_qt
def test_an_unset_profile_shows_the_switch_on(qapp, tmp_path, default_on):
    window = _window(tmp_path)
    try:
        window._sync_sandbox("pgr")
        assert window.game.sandbox.isChecked()
    finally:
        _close(window, qapp)


@needs_qt
def test_an_explicit_off_shows_the_switch_off(qapp, tmp_path, default_on):
    window = _window(tmp_path, secure_mode=False)
    try:
        window._sync_sandbox("pgr")
        assert not window.game.sandbox.isChecked()
    finally:
        _close(window, qapp)


@needs_qt
def test_turning_it_off_saves_an_explicit_false(qapp, tmp_path, default_on):
    window = _window(tmp_path)
    try:
        window._on_sandbox_toggled(False)
        assert Config.load(tmp_path / "config.json").profiles["pgr"]["secure_mode"] is False
    finally:
        _close(window, qapp)


@pytest.fixture
def view(qapp):
    page = GameView()
    page.set_profile("pgr", {"backend": "direct-pgr", "title": "PGR"})
    yield page
    page.deleteLater()
    qapp.processEvents()


@needs_qt
def test_the_pill_believes_the_launch_report_not_the_switch(view):
    view.set_sandbox(True, True)
    view.set_busy("launch")
    assert view.sandbox_pill.text() == "SANDBOX"  # until the backend says otherwise
    view.append_event(Event("log", "Sandbox is not available here", data={"sandbox": False}))
    assert view.sandbox_pill.isHidden() or view.sandbox_pill.text() != "SANDBOX"


@needs_qt
def test_a_sandboxed_launch_report_keeps_the_pill(view):
    view.set_sandbox(True, True)
    view.set_busy("launch")
    view.append_event(Event("log", "Sandbox: hidden", data={"sandbox": True}))
    assert not view.sandbox_pill.isHidden() and view.sandbox_pill.text() == "SANDBOX"


@needs_qt
def test_a_report_outside_a_launch_changes_nothing(view):
    view.set_sandbox(True, True)
    view.append_event(Event("log", "Sandbox: hidden", data={"sandbox": False}))
    assert view.sandbox_pill.text() == "SANDBOX" and not view.sandbox_pill.isHidden()


# ---- no claim the machine cannot keep --------------------------------------------------------
def test_facts_without_bubblewrap_show_no_pill():
    from nadare.core.gamefacts import SandboxFacts

    idle = SandboxFacts(supported=True, chosen=True, ready=False)
    assert idle.pill() == "" and not idle.protected_now()
    assert "bubblewrap" in idle.tooltip("default")
    running = SandboxFacts(supported=True, chosen=True, running=True, ready=False)
    assert running.pill() == ""
    # A launch that reported it really is sandboxed wins over the guess.
    proven = SandboxFacts(supported=True, chosen=True, running=True, active=True, ready=False)
    assert proven.pill() == "SANDBOX"


def test_facts_when_blocked_show_no_pill():
    from nadare.core.gamefacts import SandboxFacts

    assert SandboxFacts(supported=True, chosen=True, blocked="Flatpak").pill() == ""


def _wait_for_sandbox_probe(window, qapp) -> None:
    """The bubblewrap probe runs off the GUI thread (#1207 b191); let it answer."""
    import time

    deadline = time.monotonic() + 3
    while time.monotonic() < deadline:
        qapp.processEvents()
        if not window._sandbox_probe_running:
            qapp.processEvents()
            return
        time.sleep(0.01)


@needs_qt
def test_the_window_shows_no_pill_without_bubblewrap(qapp, tmp_path, default_on, monkeypatch):
    monkeypatch.setattr(gamesandbox, "ready", lambda: False)
    window = _window(tmp_path)
    try:
        window._sync_sandbox("pgr")
        _wait_for_sandbox_probe(window, qapp)
        assert window.game.sandbox.isChecked()  # the choice is still on
        assert window.game.sandbox_pill.isHidden()
        assert "bubblewrap" in window.game.sandbox.toolTip()
    finally:
        _close(window, qapp)


@needs_qt
def test_the_window_shows_the_pill_with_bubblewrap(qapp, tmp_path, default_on, monkeypatch):
    monkeypatch.setattr(gamesandbox, "ready", lambda: True)
    window = _window(tmp_path)
    try:
        window._sync_sandbox("pgr")
        _wait_for_sandbox_probe(window, qapp)
        assert not window.game.sandbox_pill.isHidden()
    finally:
        _close(window, qapp)


def test_default_on_in_the_flatpak_with_a_too_broad_folder_stops_too(
    tmp_path, default_on, monkeypatch
):
    monkeypatch.setattr(sandbox, "in_flatpak", lambda: True)
    monkeypatch.setattr(gamesandbox, "_flatpak_spawn", lambda: "/usr/bin/flatpak-spawn")
    (tmp_path / "run").mkdir()
    monkeypatch.setenv("XDG_RUNTIME_DIR", str(tmp_path / "run"))

    def broad(*args, **kwargs):
        raise gamesandbox.BroadBindError("the game folder is your home folder")

    monkeypatch.setattr(gamesandbox, "wrap_flatpak", broad)
    backend = _backend(tmp_path, [])
    try:
        with pytest.raises(BackendError, match="Sandbox off"):
            backend.build_launch()
    finally:
        backend._stop_link_relay()
