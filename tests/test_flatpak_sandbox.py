# SPDX-License-Identifier: GPL-3.0-or-later
"""Sandbox inside the Flatpak: the game runs in ``flatpak-spawn --sandbox`` with the home hidden.

Inside the Flatpak there is no bubblewrap to run, but Flatpak itself can start a child in a
tighter sandbox: an empty home, no ``/mnt``, only the folders Nadare exposes, and no session bus
(a bus would let the game ask the Flatpak portal for a new, unsandboxed process). The environment
is not inherited by that child, so it is handed over on a file descriptor (``--env-fd``): values
such as a login token never appear in a process's command line.
"""

from __future__ import annotations

import os
import stat
import subprocess
import sys
from pathlib import Path

import pytest
from _backend_helpers import make_context, write_exe

from nadare.core import gamesandbox, sandbox

TOKEN = "tok-very-secret-123"


@pytest.fixture(autouse=True)
def _own_state_folder(tmp_path, monkeypatch):
    monkeypatch.setenv("XDG_STATE_HOME", str(tmp_path / "state"))


@pytest.fixture
def layout(tmp_path):
    home = tmp_path / "home" / "user"
    prefix = tmp_path / "games" / "pgr" / "prefix"
    game = tmp_path / "games" / "pgr" / "game"
    runner = home / ".var/app/io.github.frostlinuxdev.Nadare/data/nadare/runners/GE-Proton"
    envdir = tmp_path / "run"
    for folder in (home, prefix, game, runner, envdir):
        folder.mkdir(parents=True)
    env = {
        "HOME": str(home),
        "PATH": "/app/bin:/usr/bin:/bin",
        "WINEPREFIX": str(prefix),
        "NMAUTH_TOKEN": TOKEN,
        "DBUS_SESSION_BUS_ADDRESS": "unix:path=/run/user/1000/bus",
        "XDG_CACHE_HOME": str(home / ".var/app/io.github.frostlinuxdev.Nadare/cache"),
    }
    return {"home": home, "prefix": prefix, "game": game, "runner": runner, "envdir": envdir,
            "env": env}


def _wrap(layout, argv=None, **kw):
    return gamesandbox.wrap_flatpak(
        argv or [str(layout["runner"] / "proton"), "run", "Game.exe", "-x"],
        layout["env"],
        prefix=layout["prefix"],
        install_dir=layout["game"],
        runner_dir=layout["runner"],
        home=layout["home"],
        env_dir=layout["envdir"],
        **kw,
    )


def _spawn_args(argv):
    """The arguments given to flatpak-spawn (everything between it and ``--``)."""
    start = argv.index("flatpak-spawn")
    end = argv.index("--", start)
    return argv[start + 1 : end]


def test_the_game_runs_in_a_flatpak_sandbox_child(layout):
    argv, _env = _wrap(layout)
    spawn = _spawn_args(argv)
    for flag in ("--sandbox", "--watch-bus", "--share-pids", "--env-fd=3"):
        assert flag in spawn, flag
    for name in ("share-gpu", "share-display", "share-sound"):
        assert f"--sandbox-flag={name}" in spawn, name


def test_the_game_gets_no_session_bus(layout):
    """Measured: with ``allow-dbus`` a game can ask the Flatpak portal for a new sub-sandbox,
    which gets the app's full file access (umu's pressure-vessel did exactly that and the game
    saw the whole home and /mnt). The child must have no bus at all."""
    spawn = _spawn_args(_wrap(layout)[0])
    assert not any("allow-dbus" in a or "allow-a11y" in a for a in spawn), spawn
    assert not any(a.startswith("--sandbox-expose") and "/run/user" in a for a in spawn)


def test_flatpak_spawn_itself_keeps_the_apps_environment(layout):
    """flatpak-spawn talks to the Flatpak portal over the app's session bus: the env it runs
    with is the caller's, unchanged (only the child gets the cleaned copy, via the env file)."""
    _argv, env = _wrap(layout)
    assert env == layout["env"]


def test_the_original_command_follows_the_double_dash(layout):
    original = [str(layout["runner"] / "proton"), "run", "Game.exe", "-x"]
    argv, _env = _wrap(layout, original)
    start = argv.index("flatpak-spawn")
    assert argv[argv.index("--", start) + 1 :] == original


def test_prefix_and_game_folder_are_exposed_writable_and_the_runner_read_only(layout):
    spawn = _spawn_args(_wrap(layout)[0])
    assert f"--sandbox-expose-path={layout['prefix'].resolve()}" in spawn
    assert f"--sandbox-expose-path={layout['game'].resolve()}" in spawn
    assert f"--sandbox-expose-path-ro={layout['runner'].resolve()}" in spawn
    assert f"--sandbox-expose-path={layout['runner'].resolve()}" not in spawn


def test_nothing_else_of_the_home_is_exposed(layout):
    spawn = _spawn_args(_wrap(layout)[0])
    exposed = {a.split("=", 1)[1] for a in spawn if a.startswith("--sandbox-expose")}
    # The game's own shader cache, like the bubblewrap Sandbox gives it.
    cache = Path(layout["env"]["XDG_CACHE_HOME"]) / "nadare" / "sandbox-cache"
    allowed = {
        str(layout["prefix"].resolve()),
        str(layout["game"].resolve()),
        str(layout["runner"].resolve()),
        str(cache.resolve()),
    }
    assert exposed <= allowed, exposed - allowed


def test_the_game_gets_its_own_cache_folder(layout):
    argv, env = _wrap(layout)
    cache = Path(layout["env"]["XDG_CACHE_HOME"]) / "nadare" / "sandbox-cache"
    assert cache.is_dir()
    assert f"--sandbox-expose-path={cache.resolve()}" in _spawn_args(argv)
    env_file = next(layout["envdir"].iterdir())
    entries = dict(c.split("=", 1) for c in env_file.read_text().split("\0") if c)
    assert entries["XDG_CACHE_HOME"] == str(cache)


def test_a_missing_folder_is_skipped_not_fatal(layout):
    layout["game"].rmdir()
    spawn = _spawn_args(_wrap(layout)[0])
    assert not any(str(layout["game"]) in a for a in spawn)


def test_a_folder_inside_an_exposed_one_is_not_exposed_twice(layout):
    inner = layout["prefix"] / "drive_c"
    inner.mkdir()
    spawn = _spawn_args(_wrap(layout, read_only=[inner])[0])
    assert not any(a.endswith(str(inner.resolve())) for a in spawn)


def test_read_only_files_are_exposed_read_only(layout, tmp_path):
    installer = tmp_path / "downloads" / "setup.exe"
    installer.parent.mkdir()
    installer.write_bytes(b"MZ")
    spawn = _spawn_args(_wrap(layout, read_only=[installer])[0])
    assert f"--sandbox-expose-path-ro={installer.resolve()}" in spawn


def test_no_environment_value_is_on_any_command_line(layout):
    argv, _env = _wrap(layout)
    assert not any(a.startswith("--env=") for a in argv)
    assert not any(TOKEN in a for a in argv)


def test_the_environment_file_is_private_and_holds_the_game_environment(layout):
    argv, _env = _wrap(layout)
    files = list(layout["envdir"].iterdir())
    assert len(files) == 1
    env_file = files[0]
    assert str(env_file) in argv
    assert stat.S_IMODE(env_file.stat().st_mode) == 0o600
    entries = dict(
        chunk.split("=", 1) for chunk in env_file.read_text().split("\0") if chunk
    )
    assert entries["NMAUTH_TOKEN"] == TOKEN
    assert entries["WINEPREFIX"] == str(layout["prefix"])
    # The app's own bus address does not exist in the child; Flatpak gives it a filtered one.
    assert "DBUS_SESSION_BUS_ADDRESS" not in entries


def test_the_wrapped_command_reads_and_deletes_the_environment_file(layout, tmp_path):
    """Run the real wrapper with a stand-in flatpak-spawn that prints what reaches it on fd 3."""
    fake_bin = tmp_path / "bin"
    fake_bin.mkdir()
    fake = fake_bin / "flatpak-spawn"
    fake.write_text(
        f"#!{sys.executable}\n"
        "import os, sys\n"
        "data = os.read(3, 1 << 16).decode()\n"
        "sys.stdout.write(data.replace('\\0', '\\n'))\n"
        "sys.stdout.write('ARGS ' + ' '.join(sys.argv[1:]) + '\\n')\n"
    )
    fake.chmod(0o755)
    argv, env = _wrap(layout, ["/bin/true"])
    env_file = next(layout["envdir"].iterdir())
    run_env = dict(env, PATH=f"{fake_bin}:{env.get('PATH', '')}")
    done = subprocess.run(argv, env=run_env, capture_output=True, text=True, timeout=30)
    assert done.returncode == 0, done.stderr
    assert f"NMAUTH_TOKEN={TOKEN}" in done.stdout
    assert TOKEN not in done.stdout.split("ARGS", 1)[1]
    assert not env_file.exists()


def test_the_home_or_a_private_folder_is_refused_like_bubblewrap(layout):
    with pytest.raises(gamesandbox.BroadBindError):
        gamesandbox.wrap_flatpak(
            ["/bin/true"],
            layout["env"],
            prefix=layout["home"],
            home=layout["home"],
            env_dir=layout["envdir"],
        )
    ssh = layout["home"] / ".ssh"
    ssh.mkdir()
    with pytest.raises(gamesandbox.BroadBindError):
        gamesandbox.wrap_flatpak(
            ["/bin/true"],
            layout["env"],
            prefix=layout["prefix"],
            install_dir=ssh,
            home=layout["home"],
            env_dir=layout["envdir"],
        )


def test_the_input_argv_and_env_are_not_mutated(layout):
    original = ["/bin/true", "a"]
    env_before = dict(layout["env"])
    _wrap(layout, list(original))
    assert layout["env"] == env_before


# ---- the backends ------------------------------------------------------------------------------
def _nikke(tmp_path: Path, **profile):
    from nadare.backends.launcher import NikkeBackend

    values = {"prefix": str(tmp_path / "prefix"), "wine_binary": "/usr/bin/wine", **profile}
    write_exe(tmp_path / "prefix" / "drive_c" / "NIKKE" / "Launcher" / "nikke_launcher.exe", "MZ")
    return NikkeBackend(make_context(tmp_path, "launcher-nikke", values))


@pytest.fixture
def in_flatpak(monkeypatch, tmp_path):
    monkeypatch.setattr(sandbox, "in_flatpak", lambda: True)
    monkeypatch.setenv("XDG_RUNTIME_DIR", str(tmp_path / "xdg-run"))
    (tmp_path / "xdg-run").mkdir()
    # No bubblewrap anywhere: the Flatpak path must not need one.
    monkeypatch.setattr(gamesandbox.shutil, "which", lambda name, **_kw: None)


def test_in_flatpak_an_explicit_sandbox_wraps_play_instead_of_refusing(tmp_path, in_flatpak):
    argv, env, target = _nikke(tmp_path, secure_mode=True).build_launch()
    assert "flatpak-spawn" in argv and "--sandbox" in argv
    rest = argv[argv.index("--", argv.index("flatpak-spawn")) + 1 :]
    assert rest[0] == "/usr/bin/wine" and str(target) in rest
    assert env["WINEPREFIX"] == str(tmp_path / "prefix")


def test_in_flatpak_the_default_on_sandbox_wraps_play_too(tmp_path, in_flatpak, monkeypatch):
    monkeypatch.setattr(gamesandbox, "DEFAULT_ON", True)
    backend = _nikke(tmp_path)
    if not backend.secure_mode_enabled():
        pytest.skip("this kind is not on by default")
    argv, _env, _target = backend.build_launch()
    assert "flatpak-spawn" in argv


def test_links_reach_the_relay_through_a_stand_in_xdg_open(layout, tmp_path):
    """Without a bus the game cannot open the browser itself. Wine's winebrowser runs
    ``xdg-open <link>`` (measured inside the Flatpak), so a stand-in at the front of the game's
    PATH writes the link to Nadare's relay pipe, in the line format the relay already reads."""
    relay_dir = tmp_path / "relay"
    relay_dir.mkdir()
    pipe = relay_dir / "steam.pipe"
    argv, _env = _wrap(layout, ["/bin/true"], link_pipe=pipe)
    spawn = _spawn_args(argv)
    assert f"--sandbox-expose-path={relay_dir.resolve()}" in spawn
    env_file = next(layout["envdir"].iterdir())
    entries = dict(c.split("=", 1) for c in env_file.read_text().split("\0") if c)
    assert entries["PATH"].split(":")[0] == str(relay_dir)
    assert entries["NADARE_LINK_PIPE"] == str(pipe)
    shim = relay_dir / "xdg-open"
    assert os.access(shim, os.X_OK)
    pipe.write_text("")  # a plain file stands in for the FIFO here
    url = "https://accounts.example.com/o/oauth2?x=1&y=%20z"
    subprocess.run([str(shim), url], env={"NADARE_LINK_PIPE": str(pipe)}, check=True, timeout=10)
    from nadare.core import linkrelay

    assert linkrelay.parse_request(pipe.read_text().strip()) == url


def test_the_relay_lives_where_flatpak_can_expose_it(layout, tmp_path):
    """Measured: a path under XDG_RUNTIME_DIR is silently NOT exposed to a sub-sandbox."""
    run = tmp_path / "run" / "user" / "1000"
    (run / "relay").mkdir(parents=True)
    env = dict(layout["env"], XDG_RUNTIME_DIR=str(run))
    with pytest.raises(ValueError):
        gamesandbox.wrap_flatpak(
            ["/bin/true"], env, prefix=layout["prefix"], home=layout["home"],
            env_dir=layout["envdir"], link_pipe=run / "relay" / "steam.pipe",
        )


def test_in_flatpak_play_starts_the_link_relay_outside_the_runtime_dir(
    tmp_path, in_flatpak, monkeypatch
):
    from nadare.core import linkrelay

    seen = []
    real_start = linkrelay.LinkRelay.start

    def start(self):
        path = real_start(self)
        seen.append(path)
        return path

    monkeypatch.setattr(linkrelay.LinkRelay, "start", start)
    backend = _nikke(tmp_path, secure_mode=True)
    try:
        argv, _env, _target = backend.build_launch()
        assert len(seen) == 1
        assert not str(seen[0]).startswith(str(tmp_path / "xdg-run"))
        assert f"--sandbox-expose-path={seen[0].parent.resolve()}" in argv
    finally:
        backend._stop_link_relay()


def test_in_flatpak_the_sandbox_is_ready_without_bubblewrap(in_flatpak, monkeypatch):
    monkeypatch.setattr(sandbox, "in_flatpak", lambda: True)
    gamesandbox._READY_CACHE.clear()
    monkeypatch.setattr(
        gamesandbox, "_flatpak_spawn", lambda: "/usr/bin/flatpak-spawn", raising=False
    )
    assert gamesandbox.ready() is True


def test_doctor_in_flatpak_says_the_sandbox_is_available(in_flatpak, capsys, monkeypatch):
    from nadare import app

    monkeypatch.setattr(
        gamesandbox, "_flatpak_spawn", lambda: "/usr/bin/flatpak-spawn", raising=False
    )
    app._print_sandbox_status()
    out = capsys.readouterr().out
    assert "Sandbox: available" in out and "Flatpak" in out
    assert "not available" not in out


def test_no_message_says_the_flatpak_lacks_the_sandbox():
    root = Path(__file__).resolve().parents[1] / "src" / "nadare"
    hits = [
        str(p.relative_to(root))
        for p in root.rglob("*.py")
        if "Flatpak version yet" in p.read_text(encoding="utf-8")
    ]
    assert hits == []


def test_a_game_reached_through_a_symlink_gets_the_real_paths(layout, tmp_path):
    """~/Games -> another drive is common. Flatpak exposes the symlink's target only (measured:
    the symlink path does not exist in the sub-sandbox), so the game must be told the target:
    Proton failed with "No such file or directory: ~/Games/aether/prefix/pfx.lock"."""
    drive = tmp_path / "drive" / "aether"
    (drive / "prefix").mkdir(parents=True)
    (drive / "game").mkdir()
    link = layout["home"] / "Games" / "aether"
    link.parent.mkdir()
    link.symlink_to(drive)
    env = dict(layout["env"], WINEPREFIX=str(link / "prefix"),
               STEAM_COMPAT_DATA_PATH=str(link / "prefix"),
               STEAM_COMPAT_INSTALL_PATH=str(link / "game"))
    argv, _env = gamesandbox.wrap_flatpak(
        [str(layout["runner"] / "proton"), "run", str(link / "game" / "Game.exe")],
        env,
        prefix=link / "prefix",
        install_dir=link / "game",
        runner_dir=layout["runner"],
        home=layout["home"],
        env_dir=layout["envdir"],
    )
    inner = argv[argv.index("--", argv.index("flatpak-spawn")) + 1 :]
    assert inner[2] == str(drive.resolve() / "game" / "Game.exe")
    env_file = next(layout["envdir"].iterdir())
    entries = dict(c.split("=", 1) for c in env_file.read_text().split("\0") if c)
    assert entries["WINEPREFIX"] == str(drive.resolve() / "prefix")
    assert entries["STEAM_COMPAT_DATA_PATH"] == str(drive.resolve() / "prefix")
    assert entries["STEAM_COMPAT_INSTALL_PATH"] == str(drive.resolve() / "game")
    assert not any(str(link) in a for a in inner)


def test_windows_paths_into_a_symlinked_game_folder_are_rewritten_too(layout, tmp_path):
    """Endfield's launch helper gets a Z:\\home\\...\\Games\\endfield\\... path built from
    the symlink; inside the Sandbox only the real folder exists (measured: exit 1)."""
    drive = tmp_path / "drive" / "endfield"
    (drive / "prefix").mkdir(parents=True)
    (drive / "game").mkdir()
    link = layout["home"] / "Games" / "endfield"
    link.parent.mkdir()
    link.symlink_to(drive)
    win_link = "Z:" + str(link / "game" / "Endfield.exe").replace("/", "\\")
    argv, _env = gamesandbox.wrap_flatpak(
        ["proton", "run", "helper.exe", "--launcher", win_link],
        layout["env"],
        prefix=link / "prefix",
        install_dir=link / "game",
        home=layout["home"],
        env_dir=layout["envdir"],
    )
    inner = argv[argv.index("--", argv.index("flatpak-spawn")) + 1 :]
    assert inner[-1] == "Z:" + str(drive.resolve() / "game" / "Endfield.exe").replace("/", "\\")


def test_the_sandbox_lasts_until_wine_is_done_not_only_the_first_program(layout, tmp_path):
    """NTE's publisher launcher starts the game and exits. `proton run` then returns, the
    sandbox ends and takes the game with it (measured: the launcher exited 0, nothing ran).
    umu's container waits for every process; here the sandboxed command waits for Wine's
    server after Proton returns, and keeps Proton's exit code."""
    runner = layout["runner"]
    wineserver = write_exe(runner / "files" / "bin" / "wineserver")
    argv, _env = _wrap(layout, [str(runner / "proton"), "run", "Launcher.exe", "/autoplay"])
    inner = argv[argv.index("--", argv.index("flatpak-spawn")) + 1 :]
    assert inner[0] == "/bin/sh" and inner[1] == "-c"
    assert str(wineserver) in inner
    assert inner[-4:] == [str(runner / "proton"), "run", "Launcher.exe", "/autoplay"]


def test_the_wait_keeps_the_programs_exit_code(layout, tmp_path):
    runner = layout["runner"]
    log = tmp_path / "waited"
    write_exe(runner / "files" / "bin" / "wineserver", f'#!/bin/sh\necho "$1" > {log}\n')
    argv, _env = _wrap(layout, [str(runner / "proton"), "run", "x.exe"])
    inner = argv[argv.index("--", argv.index("flatpak-spawn")) + 1 :]
    proton = runner / "proton"
    proton.write_text("#!/bin/sh\nexit 7\n")
    proton.chmod(0o755)
    done = subprocess.run(inner, capture_output=True, text=True, timeout=30)
    assert done.returncode == 7
    assert log.read_text().strip() == "-w"


def test_a_command_that_is_not_proton_is_not_wrapped(layout):
    argv, _env = _wrap(layout, ["/usr/bin/wine", "game.exe"])
    inner = argv[argv.index("--", argv.index("flatpak-spawn")) + 1 :]
    assert inner == ["/usr/bin/wine", "game.exe"]


def test_a_new_prefix_is_made_and_exposed(layout):
    """First Sandboxed launch of a new game in the Flatpak (Ubuntu 26.04 VM, 2026-11-14): the
    prefix did not exist yet, so it was not exposed and Proton could not create pfx.lock."""
    import shutil

    shutil.rmtree(layout["prefix"])
    argv, _env = _wrap(layout)
    assert layout["prefix"].is_dir()
    assert f"--sandbox-expose-path={layout['prefix']}" in _spawn_args(argv)

