# SPDX-License-Identifier: GPL-3.0-or-later
"""Flatpak: Proton runs through umu and the Steam Runtime, except inside the Sandbox.

Measured on a real Flatpak (1.16): umu's pressure-vessel starts the Steam Runtime with Flatpak's
sub-sandboxing, and games then behave as in the AppImage (Girls' Frontline 2 plays; run directly
on the Flatpak runtime its intro video fails). Heroic, Lutris and Bottles do the same. A
Sandboxed game cannot use umu: pressure-vessel needs the session bus to start its sub-sandbox,
and with that bus the game could leave the Sandbox. So a Sandboxed launch runs Proton directly.
"""

from __future__ import annotations

from pathlib import Path

import pytest
from _backend_helpers import make_context, write_exe

from nadare.backends import winegame as wg
from nadare.backends.launcher import NikkeBackend
from nadare.core import gamesandbox, sandbox, umu

UMU = "/managed/umu-run"


@pytest.fixture
def flatpak(monkeypatch, tmp_path):
    monkeypatch.setattr(sandbox, "in_flatpak", lambda: True)
    monkeypatch.setenv("XDG_RUNTIME_DIR", str(tmp_path / "xdg-run"))
    (tmp_path / "xdg-run").mkdir()
    monkeypatch.setattr(gamesandbox, "_flatpak_spawn", lambda: "/usr/bin/flatpak-spawn")
    calls = []

    def resolve(configured="umu-run", *, allow_download, emit=None, cancel=None):
        calls.append(allow_download)
        return UMU

    monkeypatch.setattr(umu, "system", lambda *_a, **_k: None)
    monkeypatch.setattr(umu, "resolve", resolve)
    return calls


def _nikke(tmp_path: Path, monkeypatch, **profile) -> NikkeBackend:
    proton = tmp_path / "runner"
    write_exe(proton / "proton")
    values = {"prefix": str(tmp_path / "prefix"), **profile}
    write_exe(tmp_path / "prefix" / "drive_c" / "NIKKE" / "Launcher" / "nikke_launcher.exe", "MZ")
    backend = NikkeBackend(make_context(tmp_path, "launcher-nikke", values))
    monkeypatch.setattr(backend, "resolve_runner", lambda: (proton, "proton"))
    return backend


def test_without_the_sandbox_play_uses_umu(tmp_path, monkeypatch, flatpak):
    argv, env, _target = _nikke(tmp_path, monkeypatch, secure_mode=False).build_launch()
    assert argv[0] == UMU
    assert flatpak == [True]  # the managed copy may be downloaded, as in the AppImage
    assert env["PROTONPATH"] == str(tmp_path / "runner")


def test_with_the_sandbox_play_runs_proton_directly_inside_it(tmp_path, monkeypatch, flatpak):
    argv, _env, _target = _nikke(tmp_path, monkeypatch, secure_mode=True).build_launch()
    assert "flatpak-spawn" in argv
    inner = argv[argv.index("--", argv.index("flatpak-spawn")) + 1 :]
    assert inner[:2] == [str(tmp_path / "runner" / "proton"), "run"]
    assert UMU not in argv


def test_prefix_setup_and_installs_use_umu_even_when_play_is_sandboxed(
    tmp_path, monkeypatch, flatpak
):
    backend = _nikke(tmp_path, monkeypatch, secure_mode=True)
    argv, _env = backend._prefix_command(["wineboot", "-u"])
    assert argv[0] == UMU


def test_outside_flatpak_every_launch_goes_through_umu(tmp_path, monkeypatch):
    """#1338 (user): the AppImage no longer runs `proton run` when the host has the 32-bit
    libraries; it sets up Nadare's own umu instead."""
    monkeypatch.setattr(sandbox, "in_flatpak", lambda: False)
    monkeypatch.setattr(umu, "system", lambda *_a, **_k: None)
    monkeypatch.setattr(wg.hostlibs, "missing_32bit", lambda *_a, **_k: [])
    monkeypatch.setattr(umu, "resolve", lambda *_a, **_k: "/data/nadare/umu/umu-run")
    from nadare.core import userns

    monkeypatch.setattr(userns, "blocks_runtime", lambda: False)
    backend = _nikke(tmp_path, monkeypatch, secure_mode=False)
    argv, _env = backend._prefix_command(["game.exe"])
    assert argv[0] == "/data/nadare/umu/umu-run"


def test_doctor_says_the_flatpak_uses_umu(monkeypatch, capsys, tmp_path):
    from nadare import app
    from nadare.core.config import Config

    monkeypatch.setattr(sandbox, "in_flatpak", lambda: True)
    monkeypatch.setattr(umu, "managed", lambda: None)
    app._doctor_report(Config.load(tmp_path / "config.json"), None, [])
    out = capsys.readouterr().out
    assert "umu-run: set up at the first launch" in out
    assert "not needed" not in out


def test_the_game_folder_is_the_same_drive_with_and_without_the_sandbox(tmp_path):
    """umu sets STEAM_COMPAT_INSTALL_PATH to the game's folder, which Proton maps as drive S:.
    The direct path (Sandbox on) must do the same, or a game that saved its install path
    (Brown Dust 2 asks to) finds it on Z: one day and S: the next when the Sandbox is toggled."""
    game = tmp_path / "Games" / "bd2" / "game"
    game.mkdir(parents=True)
    exe = game / "BrownDust II.exe"
    exe.write_bytes(b"MZ")
    _argv, env = sandbox.proton_direct(tmp_path / "runner", tmp_path / "prefix", [str(exe)])
    assert env["STEAM_COMPAT_INSTALL_PATH"] == str(game)


def test_a_command_that_is_not_a_file_sets_no_game_drive(tmp_path):
    _argv, env = sandbox.proton_direct(tmp_path / "runner", tmp_path / "prefix", ["wineboot"])
    assert "STEAM_COMPAT_INSTALL_PATH" not in env
