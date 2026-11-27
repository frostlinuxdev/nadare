# SPDX-License-Identifier: GPL-3.0-or-later
"""Leftovers of the launch sheet review (#1852)."""

from __future__ import annotations

import time
from contextlib import contextmanager
from pathlib import Path
from types import SimpleNamespace

import pytest

from nadare.backends import wine
from nadare.backends.wine import ProcessQuery
from nadare.core import launch_window
from nadare.core.config import Config
from nadare.core.events import Cancel
from nadare.gui import workers

FRAME = "info:  Presenter: Actual swapchain properties"


def test_a_frame_line_after_the_watch_has_ended_scans_nothing():
    scans: list[int] = []

    def game_processes():
        scans.append(1)
        return []

    evidence = launch_window.WindowEvidence(set(), game_processes=game_processes)
    evidence.feed(FRAME)
    assert scans == [1]  # still watching: the frame line is checked against the game
    evidence.stop()
    for _ in range(50):
        evidence.feed(FRAME)
    assert scans == [1]


def test_the_watch_stops_its_evidence_when_it_gives_up(monkeypatch):
    monkeypatch.setattr(launch_window, "WATCH_SECONDS", 0.0)
    monkeypatch.setattr(launch_window, "POLL_INTERVAL", 0.01)
    monkeypatch.setattr(launch_window, "x11_windows", dict)
    scans: list[int] = []
    with launch_window.watch(
        None, lambda event: None, Cancel(), game_processes=lambda: scans.append(1) or []
    ) as evidence:
        deadline = time.monotonic() + 2
        while not evidence.stopped and time.monotonic() < deadline:
            time.sleep(0.01)
        assert evidence.stopped
        evidence.feed(FRAME)
    assert scans == []


# --- Neon 5 and 9: process_match under require_name, through game_processes() on a fake /proc


def _proc_entry(root: Path, pid: int, argv: list[str], env: dict[str, str]) -> None:
    entry = root / str(pid)
    entry.mkdir(parents=True)
    (entry / "cmdline").write_bytes(b"\0".join(item.encode() for item in argv) + b"\0")
    (entry / "environ").write_bytes(
        b"".join(f"{key}={value}".encode() + b"\0" for key, value in env.items())
    )


@pytest.fixture
def fake_proc(tmp_path, monkeypatch):
    root = tmp_path / "proc"
    root.mkdir()
    real_scan = wine.scan_pids
    monkeypatch.setattr(wine, "_own_pid", lambda: -1)
    monkeypatch.setattr(wine, "scan_pids", lambda query: real_scan(query, proc_root=root))
    return root


def _launcher_backend(prefix: str, *, match: str = ""):
    """A launcher game: the Stop rule names launcher, helper and game; the gate wants the game."""
    return SimpleNamespace(
        _patterns=lambda: ProcessQuery(
            prefix=prefix, names=("launcher.exe", "helper.exe", "Game.exe"), match=match
        ),
        _launcher_query=lambda: ProcessQuery(names=("Launcher.exe",)),
        game_process_exclude=("Helper.exe",),
        ctx=SimpleNamespace(profile={}),
    )


def test_process_match_on_the_launchers_own_folder_does_not_count_the_launcher(tmp_path, fake_proc):
    """``process_match = "Games"`` is in the launcher's path too: the launcher is not the game."""
    prefix = str(tmp_path / "prefix")
    gate = wine.game_processes(_launcher_backend(prefix, match="Games"))
    _proc_entry(fake_proc, 801, [r"Z:\Games\Launcher.exe"], {"WINEPREFIX": prefix})
    assert gate() is False
    _proc_entry(fake_proc, 802, [r"Z:\Games\Helper.exe"], {"WINEPREFIX": prefix})
    assert gate() is False
    _proc_entry(fake_proc, 803, [r"Z:\Games\Renamed-Client.exe"], {"WINEPREFIX": prefix})
    assert gate() is True  # the override still follows a game spawned under another name


def test_a_launcher_shown_as_one_windows_command_line_is_still_not_the_game(tmp_path, fake_proc):
    prefix = str(tmp_path / "prefix")
    gate = wine.game_processes(_launcher_backend(prefix, match="Games"))
    _proc_entry(fake_proc, 804, [r"Z:\Games\Launcher.exe --start Game.exe"], {"WINEPREFIX": prefix})
    assert gate() is False


def test_the_game_shown_as_one_windows_command_line_is_found(tmp_path, fake_proc):
    """Wine can show ``Game.exe -arg`` as one ``argv[0]``: it is still the game."""
    prefix = str(tmp_path / "prefix")
    gate = wine.game_processes(_launcher_backend(prefix))
    _proc_entry(fake_proc, 805, [r"Z:\Games\Game.exe -screen-fullscreen 1"], {"WINEPREFIX": prefix})
    assert gate() is True


def test_a_launcher_command_line_naming_the_game_is_not_the_game(tmp_path, fake_proc):
    prefix = str(tmp_path / "prefix")
    gate = wine.game_processes(_launcher_backend(prefix))
    _proc_entry(
        fake_proc, 806, [r"Z:\Games\launcher.exe Z:\Games\Game.exe"], {"WINEPREFIX": prefix}
    )
    assert gate() is False


def test_the_stop_rule_still_counts_the_launcher_through_process_match(tmp_path, fake_proc):
    """Only the first-frame gate excludes launchers: Stop must still end them."""
    prefix = str(tmp_path / "prefix")
    query = ProcessQuery(prefix=prefix, names=("Game.exe",), match="Games", require_name=True)
    _proc_entry(fake_proc, 807, [r"Z:\Games\Launcher.exe"], {"WINEPREFIX": prefix})
    assert wine.scan_pids(query) == [807]


# --- Neon 10: BackendWorker's real fallback from game_processes to game_pids


def _watched_gate(monkeypatch, backend):
    """The ``game_processes`` the worker hands the window watch for ``backend``."""
    seen: list = []

    @contextmanager
    def watch(pids, emit, cancel, **kwargs):
        seen.append(kwargs["game_processes"])
        yield None

    monkeypatch.setattr(launch_window, "watch", watch)
    monkeypatch.setattr(workers.launchgate, "play", lambda backend: 0)
    assert workers.BackendWorker(Config(), "g", "launch")._dispatch(backend) == 0
    assert len(seen) == 1
    return seen[0]


def _broken():
    raise OSError("no prefix")


def test_the_worker_uses_the_backends_own_game_rule(monkeypatch):
    def rule():
        return True

    backend = SimpleNamespace(game_pids=lambda: lambda: [], game_processes=lambda: rule)
    assert _watched_gate(monkeypatch, backend) is rule


@pytest.mark.parametrize(
    "game_processes", [None, lambda: None, _broken], ids=["absent", "no-rule", "raises"]
)
def test_the_worker_falls_back_to_the_stop_rule(monkeypatch, game_processes):
    running: list[int] = []
    backend = SimpleNamespace(game_pids=lambda: lambda: list(running))
    if game_processes is not None:
        backend.game_processes = game_processes
    gate = _watched_gate(monkeypatch, backend)
    assert gate() is False
    running.append(5)
    assert gate() is True


@pytest.mark.parametrize(
    "game_pids", [None, lambda: None, _broken], ids=["absent", "none", "raises"]
)
def test_no_rule_at_all_watches_without_a_gate(monkeypatch, game_pids):
    backend = SimpleNamespace(game_processes=lambda: None)
    if game_pids is not None:
        backend.game_pids = game_pids
    assert _watched_gate(monkeypatch, backend) is None


def test_a_generic_profiles_stub_is_not_the_game_once_process_match_names_it(
    tmp_path, fake_proc
):
    """``exe_name`` is the stub Nadare starts; ``process_match`` says which exe is the game."""
    from _backend_helpers import make_context, write_exe

    from nadare.backends.wine import WineBackend

    game = tmp_path / "game"
    write_exe(game / "Stub.exe")
    values = {
        "install_dir": str(game),
        "exe_name": "Stub.exe",
        "prefix": str(tmp_path / "prefix"),
        "wine_binary": "/usr/bin/wine",
        "process_match": "RealGame.exe",
    }
    backend = WineBackend(make_context(tmp_path, "wine", values))
    prefix = backend._patterns().prefix
    gate = wine.game_processes(backend)
    _proc_entry(fake_proc, 810, [r"Z:\games\Stub.exe"], {"WINEPREFIX": prefix})
    assert gate() is False
    _proc_entry(fake_proc, 811, [r"Z:\games\RealGame.exe"], {"WINEPREFIX": prefix})
    assert gate() is True
