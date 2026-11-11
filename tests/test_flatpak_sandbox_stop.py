# SPDX-License-Identifier: GPL-3.0-or-later
"""Flatpak Sandbox: Stop must end the game, not only Nadare's wait for it.

Measured on a real Flatpak: the game runs in a ``flatpak-spawn --sandbox`` child that shares the
app's process namespace (``--share-pids``), so cancelling the launch ends ``flatpak-spawn`` but
leaves Wine and the game running. The game's processes are visible to Nadare, so it stops them by
their prefix. A sandboxed game is told the real paths of a symlinked game folder, so matching a
process to its prefix compares real paths too.
"""

from __future__ import annotations

import signal
import time

from _backend_helpers import make_context, write_exe

from nadare.backends import wine as wine_mod
from nadare.backends import winegame as wg
from nadare.core import runlock, sandbox


def test_a_process_in_the_real_folder_of_a_symlinked_prefix_belongs_to_the_game(tmp_path):
    real = tmp_path / "drive" / "aether" / "prefix"
    real.mkdir(parents=True)
    games = tmp_path / "home" / "Games"
    games.mkdir(parents=True)
    (games / "aether").symlink_to(real.parent, target_is_directory=True)
    query = wine_mod.ProcessQuery(prefix=str(games / "aether" / "prefix"), names=("game.exe",))
    env = {"WINEPREFIX": str(real / "pfx") + "/", "STEAM_COMPAT_DATA_PATH": str(real)}
    assert wine_mod._query_matches(query, ["C:\\windows\\system32\\services.exe"], env)


def test_another_prefix_still_does_not_match(tmp_path):
    (tmp_path / "a").mkdir()
    (tmp_path / "b").mkdir()
    query = wine_mod.ProcessQuery(prefix=str(tmp_path / "a"), names=("game.exe",))
    assert not wine_mod._query_matches(query, ["x.exe"], {"WINEPREFIX": str(tmp_path / "b")})


def test_a_stop_request_ends_the_game_processes_not_only_the_wait(tmp_path, monkeypatch):
    from nadare.backends.launcher import NikkeBackend

    monkeypatch.setattr(sandbox, "in_flatpak", lambda: True)
    wine = write_exe(tmp_path / "wine")
    write_exe(tmp_path / "prefix" / "drive_c" / "NIKKE" / "Launcher" / "nikke_launcher.exe", "MZ")
    backend = NikkeBackend(
        make_context(
            tmp_path,
            "launcher-nikke",
            {"wine_binary": str(wine), "prefix": str(tmp_path / "prefix"), "secure_mode": False},
        )
    )
    monkeypatch.setattr(backend, "_ensure_default_runner", lambda strict=False: None)
    monkeypatch.setattr(wg, "scan_pids", lambda *_a, **_k: [4242, 4343])
    stopped: list[list[int]] = []
    monkeypatch.setattr(wg, "stop_pids", lambda pids, *_a, **_k: stopped.append(list(pids)))

    def fake_stream(argv, env, on_line=None, cancel=None, **_kw):
        runlock.request_stop(backend.prefix())
        deadline = time.monotonic() + 10
        while not cancel.cancelled and time.monotonic() < deadline:
            time.sleep(0.05)
        return 0

    monkeypatch.setattr(wg, "stream", fake_stream)
    try:
        backend.launch()
    except Exception:  # a cancelled launch may raise; the processes must be stopped either way
        pass
    assert [4242, 4343] in stopped


def test_stop_sweeps_again_for_wine_helpers_that_come_back(tmp_path, monkeypatch):
    """Measured: after the first sweep a winedevice.exe was running again and kept the launching
    Flatpak instance alive. Inside the Flatpak, Stop looks again until nothing is left."""
    from nadare.backends.launcher import NikkeBackend

    monkeypatch.setattr(sandbox, "in_flatpak", lambda: True)
    wine = write_exe(tmp_path / "wine")
    write_exe(tmp_path / "prefix" / "drive_c" / "NIKKE" / "Launcher" / "nikke_launcher.exe", "MZ")
    backend = NikkeBackend(
        make_context(
            tmp_path,
            "launcher-nikke",
            {"wine_binary": str(wine), "prefix": str(tmp_path / "prefix"), "secure_mode": False},
        )
    )
    rounds = iter([[1, 2], [3], [], []])
    monkeypatch.setattr(wg, "scan_pids", lambda *_a, **_k: next(rounds, []))
    stopped: list[tuple[list[int], int]] = []

    def record(pids, signal_=signal.SIGTERM):
        stopped.append((list(pids), signal_))

    monkeypatch.setattr(wg, "stop_pids", record)
    monkeypatch.setattr(wg, "stop_runner_wineserver", lambda **_k: None)
    monkeypatch.setattr(wg.time, "sleep", lambda _s: None)
    backend.stop()
    # Measured: winedevice.exe ignores SIGTERM, and the app's wineserver -k cannot reach the
    # Sandbox's wineserver, so whatever is still there after the first sweep is killed.
    assert stopped == [([1, 2], signal.SIGTERM), ([3], signal.SIGKILL)]
