# SPDX-License-Identifier: GPL-3.0-or-later
"""Secure mode on a launch: NIKKE's Play runs inside bubblewrap, installs never do (#804)."""

from __future__ import annotations

from pathlib import Path

import pytest
from _backend_helpers import make_context, write_exe
from _parts_patch import patch_everywhere

from nadare.backends.launcher import NikkeBackend
from nadare.core import config as config_module
from nadare.core import gamesandbox, sandbox
from nadare.core.backends import BackendError, get


def _backend(tmp_path: Path, **profile) -> NikkeBackend:
    values = {"prefix": str(tmp_path / "prefix"), "wine_binary": "/usr/bin/wine", **profile}
    write_exe(tmp_path / "prefix" / "drive_c" / "NIKKE" / "Launcher" / "nikke_launcher.exe", "MZ")
    return NikkeBackend(make_context(tmp_path, "launcher-nikke", values))


@pytest.fixture
def has_bwrap(monkeypatch):
    monkeypatch.setattr(gamesandbox.shutil, "which", lambda name, **_kw: "/usr/bin/bwrap")
    monkeypatch.setattr(gamesandbox, "works", lambda binary=gamesandbox.BINARY: (True, ""))


def test_secure_mode_is_off_by_default(tmp_path):
    backend = _backend(tmp_path)
    argv, _env, target = backend.build_launch()
    assert argv[0] == "/usr/bin/wine" and str(target) in argv


def test_secure_mode_wraps_the_play_command(tmp_path, has_bwrap):
    backend = _backend(tmp_path, secure_mode=True)
    argv, env, target = backend.build_launch()
    assert Path(argv[0]).name == "bwrap"
    rest = argv[argv.index("--") + 1 :]
    assert rest[0] == "/usr/bin/wine" and str(target) in rest
    assert "--tmpfs" in argv
    assert env["WINEPREFIX"] == str(tmp_path / "prefix")


def test_the_game_folder_and_prefix_are_bound_back(tmp_path, has_bwrap):
    argv, _env, _target = _backend(tmp_path, secure_mode=True).build_launch()
    prefix = str((tmp_path / "prefix").resolve())
    binds = [argv[i + 1] for i, f in enumerate(argv) if f == "--bind"]
    assert prefix in binds


def test_a_missing_bwrap_stops_the_launch_and_says_why(tmp_path, monkeypatch):
    monkeypatch.setattr(gamesandbox.shutil, "which", lambda name, **_kw: None)
    backend = _backend(tmp_path, secure_mode=True)
    with pytest.raises(BackendError) as caught:
        backend.build_launch()
    message = str(caught.value)
    assert "Sandbox" in message and "bubblewrap" in message


def test_flatpak_without_flatpak_spawn_refuses_an_explicit_sandbox(tmp_path, monkeypatch):
    monkeypatch.setattr(sandbox, "in_flatpak", lambda: True)
    monkeypatch.setattr(gamesandbox, "_flatpak_spawn", lambda: None)
    with pytest.raises(BackendError, match="flatpak-spawn"):
        _backend(tmp_path, secure_mode=True).build_launch()


def test_installs_and_prefix_setup_are_never_wrapped(tmp_path, has_bwrap):
    backend = _backend(tmp_path, secure_mode=True)
    argv, _env = backend._prefix_command(["wineboot", "-u"])
    assert argv[0] == "/usr/bin/wine"


def _offers(kind: str) -> bool:
    return any(o.key == "secure_mode" and o.kind == "bool" for o in get(kind).options)


def test_every_launcher_game_and_every_direct_game_offer_the_option():
    from nadare.backends.direct import DirectBackend
    from nadare.backends.launcher import LauncherBackend
    from nadare.core.backends import kinds

    direct_kinds = [k for k in kinds() if issubclass(get(k), DirectBackend)]
    launcher_kinds = [k for k in kinds() if issubclass(get(k), LauncherBackend)]
    assert len(direct_kinds) >= 10, direct_kinds
    assert "launcher-nikke" in launcher_kinds
    for kind in [*launcher_kinds, *direct_kinds]:
        assert _offers(kind), kind
        assert get(kind).supports_secure_mode, kind


def test_games_that_cannot_use_it_do_not_offer_it():
    for kind in (
        "steam",
        "script",
        "stove-czn",
        "fgo-lepton",
        "arknights-yostar",
    ):
        assert not _offers(kind), kind
        assert not getattr(get(kind), "supports_secure_mode", False), kind


def test_a_launcher_game_runs_play_inside_the_sandbox(tmp_path, monkeypatch, has_bwrap):
    """The publisher's launcher is the code Secure mode exists for."""
    from _backend_helpers import fake_launcher_backend

    prefix = tmp_path / "prefix"
    launcher_cls = fake_launcher_backend()
    backend = launcher_cls(
        make_context(
            tmp_path,
            launcher_cls.kind,
            {"prefix": str(prefix), "wine_binary": "/usr/bin/wine", "secure_mode": True},
        )
    )
    exe = backend.find_launcher() or write_exe(
        backend.install_dir() / backend.launcher_names[0], "MZ"
    )
    argv, _env, target = backend.build_launch()
    assert Path(argv[0]).name == "bwrap" and "--tmpfs" in argv
    assert argv[argv.index("--") + 1 :][-1] == str(target) == str(exe)


def test_a_direct_game_runs_play_inside_the_sandbox(tmp_path, monkeypatch, has_bwrap):
    from nadare.backends import winegame
    from nadare.backends.direct import BrownDust2Backend

    prefix = tmp_path / "prefix"
    exe = write_exe(prefix / "drive_c" / "Games" / "BrownDust2" / "BrownDust II.exe", "MZ")
    values = {"prefix": str(prefix), "wine_binary": "/usr/bin/wine", "secure_mode": True}
    backend = BrownDust2Backend(make_context(tmp_path, "direct-bd2", values))
    seen = []

    class Probe:
        result = None

        def start(self):
            return self

        def stop(self):
            pass

        def join(self, timeout=None):
            pass

    patch_everywhere(
        monkeypatch, winegame, "stream", lambda argv, env, **kw: seen.append(argv) or 0
    )
    monkeypatch.setattr(backend, "_ensure_default_runner", lambda strict: None)
    monkeypatch.setattr(backend, "_run_vcredist", lambda created=False: None)
    monkeypatch.setattr(backend, "_renderer_probe", lambda: Probe())
    assert backend.launch() == 0
    argv = seen[0]
    assert Path(argv[0]).name == "bwrap" and "--tmpfs" in argv
    assert argv[argv.index("--") + 1 :][-1] == str(exe)


def test_a_direct_game_without_the_option_runs_unwrapped(tmp_path, monkeypatch):
    from nadare.backends import winegame
    from nadare.backends.direct import BrownDust2Backend

    prefix = tmp_path / "prefix"
    write_exe(prefix / "drive_c" / "Games" / "BrownDust2" / "BrownDust II.exe", "MZ")
    values = {"prefix": str(prefix), "wine_binary": "/usr/bin/wine"}
    backend = BrownDust2Backend(make_context(tmp_path, "direct-bd2", values))
    seen = []

    class Probe:
        result = None

        def start(self):
            return self

        def stop(self):
            pass

        def join(self, timeout=None):
            pass

    patch_everywhere(
        monkeypatch, winegame, "stream", lambda argv, env, **kw: seen.append(argv) or 0
    )
    monkeypatch.setattr(backend, "_ensure_default_runner", lambda strict: None)
    monkeypatch.setattr(backend, "_run_vcredist", lambda created=False: None)
    monkeypatch.setattr(backend, "_renderer_probe", lambda: Probe())
    assert backend.launch() == 0
    assert seen[0][0] == "/usr/bin/wine"


def test_a_hand_edited_string_is_a_real_boolean():
    assert "secure_mode" in config_module._PROFILE_BOOLS
    data = config_module.Config._normalise(
        {"profiles": {"n": {"backend": "launcher-nikke", "secure_mode": "false"}}}
    )
    assert data["profiles"]["n"]["secure_mode"] is False


def test_a_blocked_bwrap_stops_the_launch_with_a_secure_mode_message(
    tmp_path, monkeypatch, has_bwrap
):
    monkeypatch.setattr(
        gamesandbox,
        "works",
        lambda binary=gamesandbox.BINARY: (False, "bwrap: setting up uid map: Permission denied"),
    )
    with pytest.raises(BackendError) as caught:
        _backend(tmp_path, secure_mode=True).build_launch()
    message = str(caught.value)
    assert "Sandbox" in message and "uid map" in message and "Ubuntu" in message


class _Relay:
    def __init__(self, path):
        self.path = path
        self.stopped = False

    def stop(self):
        self.stopped = True


def _direct_game(tmp_path):
    from nadare.backends.direct import BrownDust2Backend

    prefix = tmp_path / "prefix"
    write_exe(prefix / "drive_c" / "Games" / "BrownDust2" / "BrownDust II.exe", "MZ")
    values = {"prefix": str(prefix), "wine_binary": "/usr/bin/wine", "secure_mode": True}
    return BrownDust2Backend(make_context(tmp_path, "direct-bd2", values))


def test_the_link_pipe_is_bound_in_and_stops_when_the_game_ends(tmp_path, monkeypatch, has_bwrap):
    from nadare.backends import winegame
    from nadare.core import linkrelay

    backend = _direct_game(tmp_path)
    relay = _Relay(tmp_path / "relay" / "steam.pipe")
    started = []

    def fake_start(self):
        started.append(True)
        return relay.path

    monkeypatch.setattr(linkrelay.LinkRelay, "start", fake_start)
    monkeypatch.setattr(linkrelay.LinkRelay, "stop", lambda self: setattr(relay, "stopped", True))
    seen = []

    class Probe:
        result = None

        def start(self):
            return self

        def stop(self):
            pass

        def join(self, timeout=None):
            pass

    def fake_stream(argv, env, **kw):
        assert not relay.stopped, "the relay must be up while the game runs"
        seen.append(argv)
        return 0

    patch_everywhere(monkeypatch, winegame, "stream", fake_stream)
    monkeypatch.setattr(backend, "_ensure_default_runner", lambda strict: None)
    monkeypatch.setattr(backend, "_run_vcredist", lambda created=False: None)
    monkeypatch.setattr(backend, "_renderer_probe", lambda: Probe())
    assert backend.launch() == 0
    assert started and str(relay.path) in seen[0]
    assert relay.stopped, "the relay must stop with the game"


def test_a_link_the_game_opens_is_logged_by_host_only(tmp_path, monkeypatch, has_bwrap):
    from nadare.core import linkrelay

    backend = _direct_game(tmp_path)
    lines = []
    monkeypatch.setattr(backend, "info", lambda message, **_data: lines.append(message))
    captured = {}
    real_init = linkrelay.LinkRelay.__init__

    def spy_init(self, *a, **kw):
        captured["on_open"] = kw.get("on_open")
        real_init(self, *a, **kw)

    monkeypatch.setattr(linkrelay.LinkRelay, "__init__", spy_init)
    monkeypatch.setattr(linkrelay.LinkRelay, "start", lambda self: tmp_path / "steam.pipe")
    backend.build_launch()
    captured["on_open"]("https://accounts.example.com/login?token=SECRET&x=1")
    text = " ".join(lines)
    assert "accounts.example.com" in text and "SECRET" not in text and "token" not in text


def test_the_runtimes_own_bubblewrap_is_used_when_there_is_no_system_one(tmp_path, monkeypatch):
    srt = (
        tmp_path / "data/umu/steamrt4/pressure-vessel/libexec/steam-runtime-tools-0/srt-bwrap"
    )
    srt.parent.mkdir(parents=True)
    srt.write_text("#!/bin/sh\n")
    srt.chmod(0o755)
    monkeypatch.setenv("XDG_DATA_HOME", str(tmp_path / "data"))
    monkeypatch.setattr(gamesandbox.shutil, "which", lambda name, **_kw: None)
    monkeypatch.setattr(gamesandbox, "works", lambda binary=gamesandbox.BINARY: (True, ""))
    argv, _env, _target = _backend(tmp_path, secure_mode=True).build_launch()
    assert argv[0] == str(srt)


def test_without_any_bubblewrap_the_message_gives_the_install_command(tmp_path, monkeypatch):
    from nadare.core import hostlibs

    monkeypatch.setenv("XDG_DATA_HOME", str(tmp_path / "nothing"))
    monkeypatch.setattr(gamesandbox.shutil, "which", lambda name, **_kw: None)
    monkeypatch.setattr(hostlibs, "distro_family", lambda os_release=None: "fedora")
    with pytest.raises(BackendError) as caught:
        _backend(tmp_path, secure_mode=True).build_launch()
    message = str(caught.value)
    assert "sudo dnf install bubblewrap" in message
    assert "Sandbox" in message and "runtime" in message


def test_stella_sora_offers_the_option():
    assert _offers("stellasora-yostar")


def _stella_sora(tmp_path: Path, monkeypatch, **profile):
    from nadare.backends import winegame
    from nadare.backends.stellasora import StellaSoraBackend

    prefix = tmp_path / "prefix"
    exe = write_exe(
        prefix / "drive_c" / "YostarGames" / "StellaSora_EN" / "StellaSora.exe", "MZ"
    )
    values = {
        "prefix": str(prefix),
        "wine_binary": "/usr/bin/wine",
        "manifest_file": str(tmp_path / "manifest.json"),
        **profile,
    }
    backend = StellaSoraBackend(make_context(tmp_path, "stellasora-yostar", values))
    seen = []

    class Probe:
        result = None

        def start(self):
            return self

        def stop(self):
            pass

        def join(self, timeout=None):
            pass

    patch_everywhere(
        monkeypatch, winegame, "stream", lambda argv, env, **kw: seen.append(argv) or 0
    )
    monkeypatch.setattr(backend, "_ensure_default_runner", lambda strict: None)
    monkeypatch.setattr(backend, "_run_vcredist", lambda created=False: None)
    monkeypatch.setattr(backend, "_renderer_probe", lambda: Probe())
    return backend, exe, seen


def test_stella_sora_runs_play_inside_the_sandbox(tmp_path, monkeypatch, has_bwrap):
    backend, exe, seen = _stella_sora(tmp_path, monkeypatch, secure_mode=True)
    assert backend.launch() == 0
    argv = seen[0]
    assert Path(argv[0]).name == "bwrap" and "--tmpfs" in argv
    assert argv[argv.index("--") + 1 :][-1] == str(exe)


def test_stella_sora_without_the_option_runs_unwrapped(tmp_path, monkeypatch):
    backend, exe, seen = _stella_sora(tmp_path, monkeypatch)
    assert backend.launch() == 0
    assert Path(seen[0][0]).name != "bwrap" and seen[0][-1] == str(exe)
