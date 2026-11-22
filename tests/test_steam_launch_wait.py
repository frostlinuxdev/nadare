# SPDX-License-Identifier: GPL-3.0-or-later
"""#1359: a Steam launch waits for the game instead of ending in 0 s.

With Steam already running, ``steam steam://rungameid/<id>`` hands the URI to the running client
and exits 0 at once. Nadare said "game exited normally" while Blue Archive was still starting, and
the page offered Launch again (a second launch 5 s later). The launch now waits for the game's
processes, stays open while they run, and says so plainly when Steam starts nothing.

Inside Nadare's Flatpak the host's processes are not in the sandbox's /proc (5 processes there vs
about 1,200 on the host, measured on the test desktop 2026-11-15), so the game is listed and
stopped on the host through ``flatpak-spawn --host``.
"""

from __future__ import annotations

import os
import subprocess

import pytest
from _backend_helpers import make_context

from nadare.backends import steam as steam_backend
from nadare.backends.steam import SteamBackend
from nadare.core import sandbox
from nadare.core.events import Cancelled

APP = "3557620"


class _Clock:
    def __init__(self):
        self.now = 1000.0

    def monotonic(self):
        return self.now

    def sleep(self, seconds):
        self.now += seconds


@pytest.fixture
def clock(monkeypatch):
    c = _Clock()
    monkeypatch.setattr(steam_backend, "_monotonic", c.monotonic)
    monkeypatch.setattr(steam_backend, "_sleep", c.sleep)
    return c


def _backend(tmp_path, events):
    ctx = make_context(tmp_path, "steam", {"steam_app_id": APP})
    ctx.emit = events.append
    backend = SteamBackend(ctx)
    backend.build = lambda: (["steam", f"steam://rungameid/{APP}"], APP)
    return backend


def _scripted(monkeypatch, backend, answers):
    """``_matching_pids`` answers in order; the last one repeats."""
    seen = []

    def matching(app_id, **kwargs):
        seen.append(app_id)
        return answers[min(len(seen) - 1, len(answers) - 1)]

    monkeypatch.setattr(backend, "_matching_pids", matching)
    return seen


def test_the_launch_stays_open_until_the_game_has_started_and_ended(
    tmp_path, monkeypatch, clock
):
    events = []
    backend = _backend(tmp_path, events)
    monkeypatch.setattr(steam_backend, "stream", lambda *a, **k: 0)  # Steam hands over, exits 0
    seen = _scripted(monkeypatch, backend, [[], [], [], [42], [42], [42], [42], []])
    assert backend.launch() == 0
    assert len(seen) == 8  # the launch did not end before the game had come and gone
    texts = [e.message for e in events]
    assert any("running" in t.lower() for t in texts)
    assert not any("without starting" in t for t in texts)


def test_steam_starting_nothing_ends_with_its_own_words(tmp_path, monkeypatch, clock):
    events = []
    backend = _backend(tmp_path, events)
    monkeypatch.setattr(steam_backend, "stream", lambda *a, **k: 0)
    _scripted(monkeypatch, backend, [[]])
    start = clock.now
    assert backend.launch() == 0
    assert clock.now - start <= steam_backend.APPEAR_TIMEOUT + 2  # bounded wait
    done = [e for e in events if e.kind == "done"]
    assert done and done[-1].message == "Steam returned without starting the game"


def test_the_page_adds_no_game_exited_line_after_that():
    from nadare.gui.game_view_parts import busy

    assert "steam returned without starting the game" in busy._GAME_END_LINES


def test_stop_during_the_wait_ends_the_launch(tmp_path, monkeypatch, clock):
    events = []
    backend = _backend(tmp_path, events)
    monkeypatch.setattr(steam_backend, "stream", lambda *a, **k: 0)
    calls = {"n": 0}

    def matching(app_id, **kwargs):
        calls["n"] += 1
        if calls["n"] == 3:
            backend.ctx.cancel.cancel()
        return [42]

    monkeypatch.setattr(backend, "_matching_pids", matching)
    with pytest.raises(Cancelled):
        backend.launch()


def test_a_failed_client_ends_at_once(tmp_path, monkeypatch, clock):
    events = []
    backend = _backend(tmp_path, events)
    monkeypatch.setattr(steam_backend, "stream", lambda *a, **k: 3)
    seen = _scripted(monkeypatch, backend, [[42], []])  # a wrong wait would end fast with 0
    assert backend.launch() == 3
    assert seen == []


# ---------------------------------------------------------------------------
# Inside the Flatpak: the host's processes
# ---------------------------------------------------------------------------
PS = (
    f"  101 /usr/bin/steam steam://rungameid/{APP}\n"
    f"  102 reaper SteamLaunch AppId={APP} -- /path/proton waitforexitandrun BlueArchive.exe\n"
    f"  103 /usr/bin/steam -applaunch {APP}\n"
    "  104 reaper SteamLaunch AppId=35576201 -- other.exe\n"
    f"  105 vim notes-about-AppId={APP}.txt\n"
    f"  106 /usr/bin/steam -applaunch 1 {APP}\n"
    "  garbage line\n"
)


class _Run:
    def __init__(self, stdout=PS, code=0, exc=None):
        self.calls = []
        self.stdout, self.code, self.exc = stdout, code, exc

    def __call__(self, argv, **kwargs):
        self.calls.append(list(argv))
        if self.exc is not None:
            raise self.exc
        return subprocess.CompletedProcess(argv, self.code, self.stdout, "")


def test_the_host_process_list_finds_the_game_by_exact_tokens():
    run = _Run()
    assert steam_backend.host_matching_pids(APP, run=run) == [101, 102, 103]
    assert run.calls[0][:3] == ["flatpak-spawn", "--host", "ps"]
    assert str(os.getuid()) in run.calls[0]  # only this user's processes


def test_the_user_process_match_works_on_the_host_too():
    assert steam_backend.host_matching_pids(APP, extra="BlueArchive.exe", run=_Run()) == [
        101,
        102,
        103,
    ]
    assert steam_backend.host_matching_pids(None, extra="other.exe", run=_Run()) == [104]


@pytest.mark.parametrize(
    "run",
    [_Run(code=1), _Run(exc=FileNotFoundError("flatpak-spawn")),
     _Run(exc=subprocess.TimeoutExpired("ps", 10))],
)
def test_a_failed_host_list_finds_nothing_and_never_raises(run):
    assert steam_backend.host_matching_pids(APP, run=run) == []


def test_in_the_flatpak_status_sees_the_host_game(tmp_path, monkeypatch):
    monkeypatch.setattr(sandbox, "in_flatpak", lambda: True)
    monkeypatch.setattr(sandbox, "host_which", lambda name: "/usr/bin/steam")
    steam_backend.clear_host_client_cache()
    monkeypatch.setattr(steam_backend.subprocess, "run", _Run())
    backend = SteamBackend(make_context(tmp_path, "steam", {"steam_app_id": APP}))
    status = backend.status()
    steam_backend.clear_host_client_cache()
    assert status.running and status.data["pids"] == [101, 102, 103]


def test_in_the_flatpak_stop_signals_the_host_processes(tmp_path, monkeypatch):
    monkeypatch.setattr(sandbox, "in_flatpak", lambda: True)
    run = _Run()
    monkeypatch.setattr(steam_backend.subprocess, "run", run)
    killed = []
    monkeypatch.setattr(steam_backend, "stop_pids", killed.append)  # the sandbox's own kill
    backend = SteamBackend(make_context(tmp_path, "steam", {"steam_app_id": APP}))
    backend.stop()
    assert killed == []  # host PIDs mean nothing to os.kill inside the sandbox
    kills = [c for c in run.calls if "kill" in c]
    assert kills == [["flatpak-spawn", "--host", "kill", "-TERM", "101", "102", "103"]]


def test_in_the_flatpak_stop_with_nothing_running_signals_nothing(tmp_path, monkeypatch):
    monkeypatch.setattr(sandbox, "in_flatpak", lambda: True)
    run = _Run(stdout="")
    monkeypatch.setattr(steam_backend.subprocess, "run", run)
    SteamBackend(make_context(tmp_path, "steam", {"steam_app_id": APP})).stop()
    assert not [c for c in run.calls if "kill" in c]
