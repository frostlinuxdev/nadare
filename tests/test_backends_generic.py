# SPDX-License-Identifier: GPL-3.0-or-later
"""Command-line/argv construction for the generic backends.

These tests assert the exact argv/env a profile produces.  No game or Wine is ever
started: subprocess entry points are monkeypatched.
"""

from __future__ import annotations

from pathlib import Path

import pytest
from _backend_helpers import make_context, write_exe

from nadare.backends import script as script_mod
from nadare.backends import steam as steam_mod
from nadare.backends import wine as wine_mod
from nadare.backends.script import ScriptBackend
from nadare.backends.steam import SteamBackend
from nadare.backends.wine import WineBackend
from nadare.core.backends import BackendError


# ---------------------------------------------------------------------------
# wine
# ---------------------------------------------------------------------------
class TestWineBuild:
    def test_bare_wine_binary_and_list_args(self, tmp_path):
        game = tmp_path / "game"
        exe = write_exe(game / "game.exe", "MZ")
        ctx = make_context(
            tmp_path,
            "wine",
            {
                "install_dir": str(game),
                "exe_name": "game.exe",
                "prefix": str(tmp_path / "prefix"),
                "wine_binary": "/usr/bin/wine",
                "launch_args": ["-windowed", "-nointro"],
            },
        )
        argv, env, prefix, target = WineBackend(ctx).build()
        assert argv == ["/usr/bin/wine", str(exe), "-windowed", "-nointro"]
        assert target == exe
        assert prefix == tmp_path / "prefix"
        assert env["WINEPREFIX"] == str(tmp_path / "prefix")

    def test_plain_wine_suppresses_the_mono_and_gecko_prompts(self, tmp_path):
        game = tmp_path / "game"
        write_exe(game / "game.exe", "MZ")
        ctx = make_context(
            tmp_path,
            "wine",
            {
                "install_dir": str(game),
                "exe_name": "game.exe",
                "prefix": str(tmp_path / "prefix"),
                "wine_binary": "/usr/bin/wine",
            },
        )
        _argv, env, *_ = WineBackend(ctx).build()
        assert "mscoree,mshtml=" in env["WINEDLLOVERRIDES"]

    def test_relative_exe_is_resolved_against_install_dir(self, tmp_path):
        game = tmp_path / "game"
        write_exe(game / "bin" / "game.exe", "MZ")
        ctx = make_context(
            tmp_path,
            "wine",
            {
                "install_dir": str(game),
                "exe_name": "bin/game.exe",
                "wine_binary": "/usr/bin/wine",
            },
        )
        argv, _env, _prefix, target = WineBackend(ctx).build()
        assert target == game / "bin" / "game.exe"
        assert argv[1] == str(game / "bin" / "game.exe")

    def test_string_launch_args_are_shlex_split(self, tmp_path):
        game = tmp_path / "game"
        write_exe(game / "game.exe", "MZ")
        ctx = make_context(
            tmp_path,
            "wine",
            {
                "install_dir": str(game),
                "exe_name": "game.exe",
                "wine_binary": "/usr/bin/wine",
                "launch_args": "--name 'two words'",
            },
        )
        argv, *_ = WineBackend(ctx).build()
        assert argv == ["/usr/bin/wine", str(game / "game.exe"), "--name", "two words"]

    def test_extra_env_is_merged(self, tmp_path):
        game = tmp_path / "game"
        write_exe(game / "game.exe", "MZ")
        ctx = make_context(
            tmp_path,
            "wine",
            {
                "install_dir": str(game),
                "exe_name": "game.exe",
                "wine_binary": "/usr/bin/wine",
                "extra_env": {"DXVK_HUD": "fps"},
            },
        )
        _argv, env, *_ = WineBackend(ctx).build()
        assert env["DXVK_HUD"] == "fps"

    def test_renderer_opengl_forces_wined3d_on_wine(self, tmp_path):
        game = tmp_path / "game"
        write_exe(game / "game.exe", "MZ")
        ctx = make_context(
            tmp_path,
            "wine",
            {
                "install_dir": str(game),
                "exe_name": "game.exe",
                "wine_binary": "/usr/bin/wine",
                "renderer": "opengl",
            },
        )
        _argv, env, *_ = WineBackend(ctx).build()
        assert env["WINEDLLOVERRIDES"].endswith("d3d9,d3d10core,d3d11,dxgi=b")

    def test_renderer_opengl_forces_wined3d_on_proton(self, tmp_path, monkeypatch):
        game = tmp_path / "game"
        write_exe(game / "game.exe", "MZ")
        proton = tmp_path / "proton"
        (proton / "files" / "bin").mkdir(parents=True)
        (proton / "files" / "bin" / "wine").write_text("")
        monkeypatch.setattr(
            wine_mod.shutil, "which", lambda name: "/opt/umu-run" if name == "umu-run" else None
        )
        ctx = make_context(
            tmp_path,
            "wine",
            {
                "install_dir": str(game),
                "exe_name": "game.exe",
                "proton_runner": str(proton),
                "renderer": "opengl",
            },
        )
        _argv, env, *_ = WineBackend(ctx).build()
        assert env["PROTON_USE_WINED3D"] == "1"

    def test_renderer_vulkan_adds_nothing(self, tmp_path):
        game = tmp_path / "game"
        write_exe(game / "game.exe", "MZ")
        ctx = make_context(
            tmp_path,
            "wine",
            {
                "install_dir": str(game),
                "exe_name": "game.exe",
                "wine_binary": "/usr/bin/wine",
                "renderer": "vulkan",
            },
        )
        _argv, env, *_ = WineBackend(ctx).build()
        # Only the Mono/Gecko prompt suppression; no renderer (d3d*/dxgi) overrides.
        assert env["WINEDLLOVERRIDES"] == "mscoree,mshtml="
        assert "PROTON_USE_WINED3D" not in env

    def test_proton_runner_uses_umu(self, tmp_path, monkeypatch):
        game = tmp_path / "game"
        write_exe(game / "game.exe", "MZ")
        proton = tmp_path / "proton"
        proton.mkdir()
        (proton / "proton").write_text("")
        monkeypatch.setattr(
            wine_mod.shutil, "which", lambda name: "/opt/umu-run" if name == "umu-run" else None
        )
        ctx = make_context(
            tmp_path,
            "wine",
            {
                "install_dir": str(game),
                "exe_name": "game.exe",
                "prefix": str(tmp_path / "prefix"),
                "proton_runner": str(proton),
            },
        )
        argv, env, _prefix, _target = WineBackend(ctx).build()
        assert argv == ["/opt/umu-run", str(game / "game.exe")]
        assert env["PROTONPATH"] == str(proton)
        assert env["STEAM_COMPAT_DATA_PATH"] == str(tmp_path / "prefix")

    def test_unknown_runner_tag_raises(self, tmp_path, monkeypatch):
        game = tmp_path / "game"
        write_exe(game / "game.exe", "MZ")
        ctx = make_context(
            tmp_path,
            "wine",
            {"install_dir": str(game), "exe_name": "game.exe", "proton_runner": "no-such-runner"},
        )
        with pytest.raises(BackendError):
            WineBackend(ctx).build()

    def test_proton_runner_without_umu_raises(self, tmp_path, monkeypatch):
        game = tmp_path / "game"
        write_exe(game / "game.exe", "MZ")
        proton = tmp_path / "proton"
        proton.mkdir()
        (proton / "proton").write_text("")
        monkeypatch.setattr(wine_mod.shutil, "which", lambda name: None)
        ctx = make_context(
            tmp_path,
            "wine",
            {
                "install_dir": str(game),
                "exe_name": "game.exe",
                "proton_runner": str(proton),
            },
        )
        with pytest.raises(BackendError):
            WineBackend(ctx).build()

    def test_missing_exe_is_not_installed(self, tmp_path):
        ctx = make_context(
            tmp_path,
            "wine",
            {
                "install_dir": str(tmp_path / "game"),
                "exe_name": "nope.exe",
                "prefix": str(tmp_path / "prefix"),
                "wine_binary": "/usr/bin/wine",
            },
        )
        status = WineBackend(ctx).status()
        assert status.state == "not_installed"
        assert status.running is False

    def test_no_exe_key_raises(self, tmp_path):
        ctx = make_context(tmp_path, "wine", {"prefix": str(tmp_path / "p")})
        with pytest.raises(BackendError):
            WineBackend(ctx).build()

    def test_whitespace_wine_binary_falls_back_to_autodetect(self, tmp_path, monkeypatch):
        """05-9: a whitespace-only wine_binary must not become the name "None"."""
        game = tmp_path / "game"
        write_exe(game / "game.exe", "MZ")
        monkeypatch.setattr(wine_mod, "first_wine", lambda: Path("/opt/wine/bin/wine"))
        ctx = make_context(
            tmp_path,
            "wine",
            {"install_dir": str(game), "exe_name": "game.exe", "wine_binary": "   "},
        )
        argv, *_ = WineBackend(ctx).build()
        assert argv[0] == "/opt/wine/bin/wine"

    def test_build_pins_xdg_environment(self, tmp_path):
        """R2-F1: the child env pins Nadare's XDG roots, not the host's."""
        from nadare.core import paths

        game = tmp_path / "game"
        write_exe(game / "game.exe", "MZ")
        ctx = make_context(
            tmp_path,
            "wine",
            {"install_dir": str(game), "exe_name": "game.exe", "wine_binary": "/usr/bin/wine"},
        )
        _argv, env, *_ = WineBackend(ctx).build()
        assert env["XDG_DATA_HOME"] == str(paths.data_dir().parent)
        assert env["XDG_CONFIG_HOME"] == str(paths.config_dir().parent)


# ---------------------------------------------------------------------------
# steam
# ---------------------------------------------------------------------------
class TestSteamBuild:
    def test_rungameid_uri(self, tmp_path, monkeypatch):
        monkeypatch.setattr(
            steam_mod.shutil, "which", lambda name: "/usr/bin/steam" if name == "steam" else None
        )
        ctx = make_context(tmp_path, "steam", {"steam_app_id": "12345", "steam_binary": "steam"})
        argv, app_id = SteamBackend(ctx).build()
        assert argv == ["/usr/bin/steam", "steam://rungameid/12345"]
        assert app_id == "12345"

    def test_flatpak_client_keeps_its_args(self, tmp_path, monkeypatch):
        monkeypatch.setattr(
            steam_mod.shutil,
            "which",
            lambda name: "/usr/bin/flatpak" if name == "flatpak" else None,
        )
        ctx = make_context(
            tmp_path,
            "steam",
            {"app_id": "777", "steam_binary": "flatpak run com.valvesoftware.Steam"},
        )
        argv, app_id = SteamBackend(ctx).build()
        assert argv == [
            "/usr/bin/flatpak",
            "run",
            "com.valvesoftware.Steam",
            "steam://rungameid/777",
        ]
        assert app_id == "777"

    def test_missing_app_id_raises(self, tmp_path):
        ctx = make_context(tmp_path, "steam", {"steam_binary": "steam"})
        with pytest.raises(BackendError):
            SteamBackend(ctx).build()

    def test_status_without_client_is_not_installed(self, tmp_path, monkeypatch):
        monkeypatch.setattr(steam_mod.shutil, "which", lambda name: None)
        ctx = make_context(tmp_path, "steam", {"steam_app_id": "1"})
        status = SteamBackend(ctx).status()
        assert status.state == "not_installed"

    def test_non_numeric_app_id_is_rejected(self, tmp_path):
        ctx = make_context(tmp_path, "steam", {"steam_app_id": "not-a-number"})
        with pytest.raises(BackendError):
            SteamBackend(ctx).build()

    def test_whitespace_binary_falls_back_to_default(self, tmp_path, monkeypatch):
        """05-5: a whitespace-only steam_binary must not raise IndexError."""
        monkeypatch.setattr(
            steam_mod.shutil, "which", lambda name: "/usr/bin/steam" if name == "steam" else None
        )
        ctx = make_context(tmp_path, "steam", {"steam_app_id": "1", "steam_binary": "   "})
        argv, app_id = SteamBackend(ctx).build()
        assert argv == ["/usr/bin/steam", "steam://rungameid/1"]
        assert app_id == "1"
        # No appmanifest in a Steam library: launchable, flagged as not found (#291).
        status = SteamBackend(ctx).status()
        assert status.state == "installed" and status.data["installed"] is False

    def test_patterns_match_the_launch_uri_and_steam_appid_token(self, tmp_path):
        """N1: the real Steam launch forms are matched, not the bare app id."""
        ctx = make_context(tmp_path, "steam", {"steam_app_id": "730"})
        query = SteamBackend(ctx)._patterns("730")
        assert query.args == ("steam://rungameid/730", "AppId=730")
        assert query.match == ""

    def test_user_process_match_still_replaces_the_extra_match(self, tmp_path):
        ctx = make_context(
            tmp_path, "steam", {"steam_app_id": "730", "process_match": "MyGame.exe"}
        )
        query = SteamBackend(ctx)._patterns("730")
        assert query.match == "MyGame.exe"
        assert query.args == ("steam://rungameid/730", "AppId=730")


# ---------------------------------------------------------------------------
# script
# ---------------------------------------------------------------------------
class TestScriptBuild:
    def test_launch_uses_bash_and_the_resolved_script(self, tmp_path, monkeypatch):
        script = write_exe(tmp_path / "run.sh", "#!/bin/sh\necho hi\n")
        captured: list[list[str]] = []

        def fake_stream(argv, env, on_line=None, cancel=None, stop_wine=True):
            captured.append(list(argv))
            return 0

        monkeypatch.setattr(script_mod, "stream", fake_stream)
        ctx = make_context(
            tmp_path, "script", {"launch_exe": "run.sh", "install_dir": str(tmp_path)}
        )
        assert ScriptBackend(ctx).launch() == 0
        assert captured == [["bash", str(script)]]

    def test_batch_file_is_routed_to_wine(self, tmp_path):
        write_exe(tmp_path / "install.bat", "@echo off\n")
        ctx = make_context(
            tmp_path, "script", {"launch_exe": "install.bat", "install_dir": str(tmp_path)}
        )
        backend = ScriptBackend(ctx)
        assert backend._is_batch() is True
        query = backend._patterns()
        assert query.args == (str(tmp_path / "install.bat"),)

    def test_batch_via_script_key_launches_through_wine(self, tmp_path, monkeypatch):
        """NEW-R4-4: a .bat configured only through the documented ``script``
        key must reach WineBackend's target resolution."""
        bat = write_exe(tmp_path / "install.bat", "@echo off\n")
        prefix = tmp_path / "prefix"
        ctx = make_context(
            tmp_path,
            "script",
            {
                "script": "install.bat",
                "install_dir": str(tmp_path),
                "prefix": str(prefix),
                "wine_binary": "/usr/bin/wine",
            },
        )
        backend = ScriptBackend(ctx)
        assert backend._is_batch() is True
        argv, env, _prefix, target = WineBackend(ctx).build()
        assert target == bat
        assert argv == ["/usr/bin/wine", str(bat)]
        assert env["WINEPREFIX"] == str(prefix)

    def test_verify_and_status(self, tmp_path):
        write_exe(tmp_path / "run.sh", "#!/bin/sh\n")
        ctx = make_context(
            tmp_path, "script", {"launch_exe": "run.sh", "install_dir": str(tmp_path)}
        )
        backend = ScriptBackend(ctx)
        report = backend.verify()
        assert report["verified"] is True
        assert report["script"] == str(tmp_path / "run.sh")
        status = backend.status()
        assert status.state == "installed"
        assert status.data["batch"] is False

    def test_missing_script_is_not_installed(self, tmp_path):
        ctx = make_context(
            tmp_path, "script", {"launch_exe": "gone.sh", "install_dir": str(tmp_path)}
        )
        backend = ScriptBackend(ctx)
        assert backend.status().state == "not_installed"
        assert backend.verify()["verified"] is False

    def test_launch_pins_the_xdg_environment(self, tmp_path, monkeypatch):
        """05-8: a host script runs with Nadare's pinned XDG roots."""
        from nadare.core import paths

        write_exe(tmp_path / "run.sh", "#!/bin/sh\n")
        captured: dict[str, str] = {}

        def fake_stream(argv, env, on_line=None, cancel=None, stop_wine=True):
            captured.update(env)
            return 0

        monkeypatch.setattr(script_mod, "stream", fake_stream)
        ctx = make_context(
            tmp_path, "script", {"launch_exe": "run.sh", "install_dir": str(tmp_path)}
        )
        ScriptBackend(ctx).launch()
        assert captured["XDG_DATA_HOME"] == str(paths.data_dir().parent)
        assert captured["XDG_CONFIG_HOME"] == str(paths.config_dir().parent)


# ---------------------------------------------------------------------------
# wine: Visual C++ runtime wiring (shared nadare.core.vcredist)
# ---------------------------------------------------------------------------
def _offline(*_a, **_k):
    raise OSError("offline")


class TestWineVcredist:
    def _ctx(self, tmp_path, profile: dict):
        game = tmp_path / "game"
        write_exe(game / "game.exe", "MZ")
        prefix = tmp_path / "prefix"
        (prefix / "drive_c").mkdir(parents=True, exist_ok=True)
        return make_context(
            tmp_path,
            "wine",
            {
                "install_dir": str(game),
                "exe_name": "game.exe",
                "prefix": str(prefix),
                "wine_binary": "/usr/bin/wine",
                **profile,
            },
        )

    def test_never_runs_when_disabled(self, tmp_path, monkeypatch):
        calls: list = []
        monkeypatch.setattr(wine_mod.vcredist, "ensure", lambda *a, **k: calls.append(1))
        WineBackend(self._ctx(tmp_path, {"vcredist": False}))._run_vcredist()
        assert calls == []

    def test_not_run_by_default(self, tmp_path, monkeypatch):
        calls: list = []
        monkeypatch.setattr(wine_mod.vcredist, "ensure", lambda *a, **k: calls.append(1))
        WineBackend(self._ctx(tmp_path, {}))._run_vcredist()
        assert calls == []

    def test_runs_when_enabled(self, tmp_path, monkeypatch):
        calls: list = []
        monkeypatch.setattr(wine_mod.vcredist, "ensure", lambda *a, **k: calls.append(k))
        WineBackend(self._ctx(tmp_path, {"vcredist": True}))._run_vcredist()
        assert len(calls) == 1
        assert callable(calls[0]["run_in_prefix"])

    def test_a_failure_never_breaks_launch(self, tmp_path, monkeypatch):
        ctx = self._ctx(tmp_path, {"vcredist": True})
        backend = WineBackend(ctx)
        events: list = []
        backend.ctx.emit = events.append
        monkeypatch.setattr(wine_mod, "stream", lambda *a, **k: 0)
        monkeypatch.setattr("urllib.request.urlopen", _offline)
        assert backend.launch() == 0
        assert any(e.kind == "status" and "skipped it" in e.message for e in events)


def test_stream_cancel_non_wine_does_not_stop_wineserver(monkeypatch) -> None:
    from nadare.core.events import Cancelled

    def boom(*a, **k):
        raise Cancelled()

    stopped: list[object] = []
    monkeypatch.setattr(wine_mod.process, "run", boom)
    monkeypatch.setattr(wine_mod.subprocess, "run", lambda *a, **k: stopped.append(a))
    with pytest.raises(Cancelled):
        wine_mod.stream(["/usr/bin/steam"], {}, stop_wine=False)
    with pytest.raises(Cancelled):
        wine_mod.stream(["/usr/bin/steam"], {})  # no prefix at all
    assert stopped == []


def test_stop_pids_skips_recycled_pid(monkeypatch) -> None:
    killed: list[int] = []
    monkeypatch.setattr(wine_mod.os, "kill", lambda pid, sig: killed.append(pid))
    monkeypatch.setattr(wine_mod.process, "_pid_starttime", lambda pid: "999")
    wine_mod._SCAN_START[4242] = "100"
    wine_mod.stop_pids([4242])
    assert killed == []
    wine_mod._SCAN_START[4242] = "999"
    wine_mod.stop_pids([4242])
    assert killed == [4242]


def test_steam_verify_fails_without_client(tmp_path, monkeypatch) -> None:
    monkeypatch.setenv("PATH", str(tmp_path))
    ctx = make_context(tmp_path, "steam", {"steam_app_id": "4813850"})
    result = SteamBackend(ctx).verify()
    assert result["verified"] is False
