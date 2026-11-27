# SPDX-License-Identifier: GPL-3.0-or-later
"""The Sandbox must not move Proton's game drive S: (#1614, NTE on an NVIDIA PC, 2026-11-17).

umu walks up from the game's folder to the first mount point and Proton maps it as drive ``S:``
(umu forces the game drive on). Outside the Sandbox that is the real disk (``/mnt/games``), so
NTE's launcher runs from ``S:\\Games\\...\\install\\NTEGlobal``. Inside the Sandbox the game
folder is itself a bubblewrap bind mount, so umu picked the game folder: the launcher ran from
``S:\\NTEGlobal`` and refused with "does not support placing it in the root directory of the
disk". Nadare now names the game drive from the host's view before the Sandbox starts, so the
game sees the same drive with the Sandbox on or off.
"""

from __future__ import annotations

from pathlib import Path

import pytest
from _backend_helpers import make_context, write_exe

from nadare.backends.launcher import NikkeBackend
from nadare.core import gamesandbox, umu

KEY = "STEAM_COMPAT_LIBRARY_PATHS"


def _mounts(*points: str):
    real = {str(Path(p)) for p in points}
    return lambda path: str(Path(path)) in real


def _exe(tmp_path: Path, rel: str = "Games/nte/install/NTEGlobal/Launcher.exe") -> Path:
    exe = tmp_path / rel
    exe.parent.mkdir(parents=True, exist_ok=True)
    exe.write_bytes(b"MZ")
    return exe


def test_umu_launch_gets_the_host_disk_not_the_game_folder(tmp_path):
    exe = _exe(tmp_path)
    install = exe.parent.parent
    # The Sandbox's view would make the game folder a mount; the host's view must win.
    drive = gamesandbox.game_drive(
        ["/usr/bin/umu-run", str(exe), "/launcher"], {}, is_mount=_mounts(str(tmp_path))
    )
    assert drive == str(tmp_path)
    assert drive != str(install)


def test_proton_run_launch_uses_the_program_after_the_verb(tmp_path):
    exe = _exe(tmp_path)
    drive = gamesandbox.game_drive(
        ["/r/proton", "run", str(exe)], {}, is_mount=_mounts(str(tmp_path / "Games"))
    )
    assert drive == str(tmp_path / "Games")


def test_a_wrapper_before_umu_run_is_skipped(tmp_path):
    exe = _exe(tmp_path)
    drive = gamesandbox.game_drive(
        ["/usr/bin/gamemoderun", "/usr/bin/umu-run", str(exe)], {}, is_mount=_mounts(str(tmp_path))
    )
    assert drive == str(tmp_path)


def test_the_folder_itself_never_counts_only_its_parents(tmp_path):
    """umu checks ``Path.parents``: the exe's own folder being a mount does not make it S:."""
    exe = _exe(tmp_path)
    drive = gamesandbox.game_drive(
        ["umu-run", str(exe)], {}, is_mount=_mounts(str(exe.parent), str(tmp_path))
    )
    assert drive == str(tmp_path)


def test_an_install_path_already_in_the_env_is_where_the_walk_starts(tmp_path):
    exe = _exe(tmp_path)
    env = {"STEAM_COMPAT_INSTALL_PATH": str(tmp_path / "Games" / "nte")}
    drive = gamesandbox.game_drive(
        ["umu-run", str(exe)],
        env,
        is_mount=_mounts(str(tmp_path / "Games" / "nte" / "install"), str(tmp_path)),
    )
    assert drive == str(tmp_path)


def test_no_mount_but_the_root_maps_s_to_the_root(tmp_path):
    """Everything on ``/``: outside the Sandbox umu finds nothing; inside it would find the
    hidden home's tmpfs or the bind mount. ``/`` keeps the game's paths unchanged."""
    exe = _exe(tmp_path)
    assert gamesandbox.game_drive(["umu-run", str(exe)], {}, is_mount=_mounts("/")) == "/"


@pytest.mark.parametrize(
    "argv",
    [
        [],
        ["/usr/bin/wine", "game.exe"],
        ["umu-run"],
        ["/r/proton", "run"],
        ["umu-run", "relative.exe"],
    ],
)
def test_nothing_to_pin_without_a_proton_program(argv):
    assert gamesandbox.game_drive(argv, {}, is_mount=_mounts("/")) is None


# ---- through the launch ------------------------------------------------------------------
@pytest.fixture
def sandbox_on(monkeypatch):
    monkeypatch.setattr(gamesandbox, "DEFAULT_ON", True)
    monkeypatch.setattr(gamesandbox.shutil, "which", lambda name, **_kw: "/usr/bin/bwrap")
    monkeypatch.setattr(gamesandbox, "works", lambda binary=gamesandbox.BINARY: (True, ""))
    monkeypatch.setattr(umu, "runtime_downloaded", lambda: True)


def _backend(tmp_path: Path, **profile) -> NikkeBackend:
    values = {"prefix": str(tmp_path / "prefix"), "wine_binary": "/usr/bin/wine", **profile}
    write_exe(tmp_path / "prefix" / "drive_c" / "NIKKE" / "Launcher" / "nikke_launcher.exe", "MZ")
    return NikkeBackend(make_context(tmp_path, "launcher-nikke", values))


def _wrap(backend: NikkeBackend, env: dict[str, str]):
    try:
        return backend._secure_wrap(["/usr/bin/umu-run", "/games/nte/Launcher.exe"], env)
    finally:
        backend._stop_link_relay()


def test_a_sandboxed_launch_carries_the_pinned_game_drive(tmp_path, sandbox_on, monkeypatch):
    monkeypatch.setattr(gamesandbox, "game_drive", lambda argv, env, **_kw: "/mnt/disk")
    argv, env = _wrap(_backend(tmp_path), {})
    assert Path(argv[0]).name == "bwrap"
    assert env.get(KEY) == "/mnt/disk"


def test_a_library_path_already_set_is_kept(tmp_path, sandbox_on, monkeypatch):
    monkeypatch.setattr(gamesandbox, "game_drive", lambda argv, env, **_kw: "/mnt/disk")
    _argv, env = _wrap(_backend(tmp_path), {KEY: "/mine"})
    assert env.get(KEY) == "/mine"


def test_without_the_sandbox_nothing_changes(tmp_path, monkeypatch):
    monkeypatch.setattr(gamesandbox, "game_drive", lambda argv, env, **_kw: "/mnt/disk")
    _argv, env = _wrap(_backend(tmp_path, secure_mode=False), {})
    assert KEY not in env
