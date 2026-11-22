# SPDX-License-Identifier: GPL-3.0-or-later
"""Only deliberate launch settings cross the game process boundary."""

import io
import logging
import subprocess
import sys
from pathlib import Path

import pytest
from _backend_helpers import make_context

from nadare.backends import script, steam
from nadare.backends.czn.backend import CznBackend
from nadare.backends.direct import BrownDust2Backend
from nadare.backends.launcher import NikkeBackend
from nadare.backends.wine import WineBackend
from nadare.backends.winegame import WineGameBackend
from nadare.core import gamesandbox, launchenv, paths, process, protoncmd, sandbox


def test_inherited_environment_is_allowlisted_and_log_has_names_only(caplog, monkeypatch):
    logger = logging.getLogger("nadare.core.launchenv")
    monkeypatch.setattr(logger, "propagate", False)
    # Capture at the emitting logger; Nadare normally stops root propagation.
    monkeypatch.setattr(logger, "handlers", [caplog.handler])
    base = dict.fromkeys(
        (
            "UNRELATED",
            "SSH_AUTH_SOCK",
            "GPG_AGENT_INFO",
            "http_proxy",
            "HTTPS_PROXY",
            "AWS_ACCESS_KEY_ID",
            "LD_PRELOAD",
            "LD_LIBRARY_PATH",
            "NADARE_PRIVATE",
        ),
        "value-must-never-be-logged",
    )
    with caplog.at_level(logging.DEBUG, logger="nadare.core.launchenv"):
        env = process.build_env(base=base)
    assert not base.keys() & env.keys()
    lines = [r.message for r in caplog.records if "dropped" in r.message.lower()]
    assert len(lines) == 1
    assert all(name in lines[0] for name in base)
    assert "value-must-never-be-logged" not in caplog.text
    from nadare.core.logging import MaskingFilter

    record = caplog.records[0]
    MaskingFilter().filter(record)
    assert all(name in record.getMessage() for name in base)


def test_required_host_variables_survive():
    names = """DISPLAY WAYLAND_DISPLAY XAUTHORITY XDG_RUNTIME_DIR XDG_SESSION_TYPE
    XDG_CURRENT_DESKTOP DESKTOP_SESSION LANG LC_ALL LANGUAGE TZ HOME USER LOGNAME SHELL PATH
    XDG_DATA_DIRS XDG_CONFIG_DIRS DBUS_SESSION_BUS_ADDRESS PULSE_SERVER PIPEWIRE_REMOTE
    ALSA_CONFIG_PATH ALSA_CARD ALSA_PCM_CARD JACK_DEFAULT_SERVER XKB_DEFAULT_LAYOUT LOCPATH
    ALSOFT_DRIVERS AUDIODEV XMODIFIERS GTK_IM_MODULE QT_IM_MODULE SDL_VIDEODRIVER
    __GLX_VENDOR_LIBRARY_NAME __GL_THREADED_OPTIMIZATIONS __NV_PRIME_RENDER_OFFLOAD
    __VK_LAYER_NV_optimus
    VK_ICD_FILENAMES DRI_PRIME MESA_LOADER_DRIVER_OVERRIDE mesa_glthread RADV_PERFTEST
    AMD_VULKAN_ICD
    NVIDIA_VISIBLE_DEVICES DXVK_HUD VKD3D_CONFIG PROTON_ENABLE_WAYLAND WINEDEBUG STEAM_RUNTIME
    SteamAppId SteamGameId STEAM_COMPAT_CLIENT_INSTALL_PATH UMU_LOG GAMEID STORE
    PRESSURE_VESSEL_LOG_INFO
    MANGOHUD_CONFIG ENABLE_VKBASALT FLATPAK_ID NADARE_FLATPAK container WAYLANDDRV_PRIMARY_MONITOR
    GDK_BACKEND QT_QPA_PLATFORM NADARE_WINEBUS_SDL""".split()
    base = dict.fromkeys(names, "kept")
    env = process.build_env(base=base)
    assert {k: env.get(k) for k in base} == base


@pytest.mark.parametrize("nested", [False, True])
def test_profile_escape_hatches(nested, monkeypatch):
    monkeypatch.setenv("http_proxy", "user-proxy")
    monkeypatch.setenv("CUSTOM_TOKEN", "user-choice")
    settings = {
        "pass_env": ["http_proxy", "CUSTOM_TOKEN", "MISSING"],
        "extra_env": {"CUSTOM_SECRET": "explicit", "http_proxy": "override"},
    }
    profile = {"extra": settings} if nested else settings
    env = process.build_env(profile)
    assert env["CUSTOM_TOKEN"] == "user-choice"
    assert env["CUSTOM_SECRET"] == "explicit"
    assert env["http_proxy"] == "override"
    assert "MISSING" not in env


@pytest.mark.parametrize("nested", [False, True])
@pytest.mark.parametrize("backend_cls", [WineBackend, BrownDust2Backend, NikkeBackend, CznBackend])
def test_prefix_game_installer_and_helper_environments(backend_cls, nested, tmp_path, monkeypatch):
    monkeypatch.setenv("UNRELATED", "private-host-value")
    monkeypatch.setenv("http_proxy", "opted-in")
    monkeypatch.setenv("CUSTOM_TOKEN", "passed")
    profile = {
        "prefix": str(tmp_path / "prefix"),
        "wine_binary": "/usr/bin/wine",
        "pass_env": ["http_proxy", "CUSTOM_TOKEN"],
        "extra_env": {"CUSTOM_SECRET": "explicit", "LD_PRELOAD": "/game/overlay.so"},
    }
    if nested:
        profile["extra"] = {key: profile.pop(key) for key in ("pass_env", "extra_env")}
    backend = backend_cls(make_context(tmp_path, backend_cls.kind, profile))
    monkeypatch.setattr(protoncmd, "play_speedups", lambda *a, **kw: None)
    if isinstance(backend, CznBackend):
        monkeypatch.setattr(backend, "resolve_runner", lambda: (tmp_path / "proton", "proton"))
        monkeypatch.setattr(protoncmd, "note_prefix_upgrade", lambda *a, **kw: None)
        monkeypatch.setattr(protoncmd, "proton_argv", lambda args, **kw: ["proton", *args])
    elif isinstance(backend, WineGameBackend):
        monkeypatch.setattr(backend, "resolve_runner", lambda: (None, "wine"))
    for exe in ("game.exe", "setup.exe", "helper.exe"):
        _, env = backend._prefix_command([exe])
        assert "UNRELATED" not in env
        assert env["http_proxy"] == "opted-in"
        assert env["CUSTOM_TOKEN"] == "passed"
        assert env["CUSTOM_SECRET"] == "explicit"
        assert env["LD_PRELOAD"] == "/game/overlay.so"
        assert env["WINEPREFIX"] == str(tmp_path / "prefix")
        if isinstance(backend, CznBackend):
            assert env["PROTONPATH"] == str(tmp_path / "proton")
        else:
            assert "mscoree,mshtml=" in env["WINEDLLOVERRIDES"]


@pytest.mark.parametrize("kind", ["script", "steam"])
def test_native_launch_environment(kind, tmp_path, monkeypatch):
    monkeypatch.setenv("UNRELATED", "private-host-value")
    monkeypatch.setenv("http_proxy", "opted-in")
    module, cls = (
        (script, script.ScriptBackend) if kind == "script" else (steam, steam.SteamBackend)
    )
    source = tmp_path / "game.sh"
    source.write_text("exit 0")
    backend = cls(
        make_context(
            tmp_path,
            kind,
            {
                "script": str(source),
                "app_id": "123",
                "pass_env": ["http_proxy"],
                "extra_env": {"CUSTOM_SECRET": "explicit"},
            },
        )
    )
    seen = []
    monkeypatch.setattr(module, "stream", lambda argv, env, **kw: seen.append(env) or 0)
    if kind == "steam":
        monkeypatch.setattr(backend, "build", lambda: (["steam", "steam://run/123"], "123"))
        monkeypatch.setattr(backend, "_wait_for_game", lambda app: 0)
    assert backend.launch() == 0
    assert "UNRELATED" not in seen[0]
    assert seen[0]["http_proxy"] == "opted-in"
    if kind == "script":
        assert seen[0]["CUSTOM_SECRET"] == "explicit"


@pytest.mark.parametrize("flatpak", [False, True])
def test_sandbox_game_environment(flatpak, tmp_path, monkeypatch):
    monkeypatch.setenv("UNRELATED", "private-host-value")
    home, prefix = tmp_path / "home", tmp_path / "prefix"
    home.mkdir(exist_ok=True)
    prefix.mkdir()
    env = process.build_env(extra={"WINEPREFIX": str(prefix), "LD_PRELOAD": "/game/overlay.so"})
    if flatpak:
        argv, loader_env = gamesandbox.wrap_flatpak(
            ["game.exe"], env, prefix=prefix, home=home, env_dir=tmp_path
        )
        envfile = Path(argv[argv.index("nadare-sandbox") + 1])
        child = dict(item.split("=", 1) for item in envfile.read_text().split("\0") if item)
    else:
        argv, loader_env = gamesandbox.wrap(["game.exe"], env, prefix=prefix, home=home)
        child = dict(loader_env)
        for i, arg in enumerate(argv):
            if arg == "--setenv":
                child[argv[i + 1]] = argv[i + 2]
    assert "UNRELATED" not in child
    assert "UNRELATED" not in loader_env
    assert "LD_PRELOAD" not in loader_env
    assert child["LD_PRELOAD"] == "/game/overlay.so"


def test_real_child_does_not_inherit_unknown_variable(monkeypatch):
    monkeypatch.setenv("UNRELATED", "private-host-value")
    result = subprocess.run(
        [sys.executable, "-c", "import os; print(os.environ.get('UNRELATED', 'absent'))"],
        env=process.build_env(),
        capture_output=True,
        text=True,
        check=True,
    )
    assert result.stdout.strip() == "absent"


def test_backend_helper_explicit_environment(tmp_path, monkeypatch):
    backend = BrownDust2Backend(
        make_context(
            tmp_path,
            "browndust2",
            {"prefix": str(tmp_path / "prefix"), "wine_binary": "/usr/bin/wine"},
        )
    )
    monkeypatch.setattr(backend, "resolve_runner", lambda: (None, "wine"))
    monkeypatch.setattr(protoncmd, "play_speedups", lambda *a, **kw: None)
    _, env = backend._prefix_command(
        ["helper.exe"], {"SAIL_SIGNIN_CMD": "signin", "HELPER_TOKEN": "explicit"}
    )
    assert env["SAIL_SIGNIN_CMD"] == "signin"
    assert env["HELPER_TOKEN"] == "explicit"


@pytest.mark.parametrize("host_data", [None, "/host/data"])
@pytest.mark.parametrize("client", ["launch", "setup", "flatpak"])
def test_steam_client_keeps_host_xdg(client, host_data, tmp_path, monkeypatch):
    from nadare.core import steamglue

    monkeypatch.setattr(paths, "data_dir", lambda: tmp_path / "moved" / "nadare")
    if host_data is None:
        monkeypatch.delenv("XDG_DATA_HOME", raising=False)
    else:
        monkeypatch.setenv("XDG_DATA_HOME", host_data)
    monkeypatch.setenv("PULSE_SERVER", "unix:/run/flatpak/pulse/native")
    seen = []
    argv = (["flatpak-spawn", "--host", "/usr/bin/steam", "steam://run/123"]
            if client == "flatpak" else ["steam", "steam://run/123"])
    monkeypatch.setattr(sandbox, "in_flatpak", lambda: client == "flatpak")
    if client == "setup":
        class Started:
            def wait(self, timeout):
                return 0

        def popen(command, **kwargs):
            seen.append((command, kwargs["env"]))
            return Started()

        monkeypatch.setattr(steamglue.subprocess, "Popen", popen)
        assert steamglue.run(argv) == 0
    else:
        backend = steam.SteamBackend(make_context(tmp_path, "steam", {"app_id": "123"}))
        monkeypatch.setattr(backend, "build", lambda: (argv, "123"))
        monkeypatch.setattr(backend, "_wait_for_game", lambda app: 0)
        monkeypatch.setattr(steam, "stream", lambda command, env, **kw:
                            seen.append((command, env)) or 0)
        assert backend.launch() == 0
    command, env = seen[0]
    assert command == argv  # The portal supplies the host session; do not clear or overwrite it.
    assert env.get("XDG_DATA_HOME") == host_data


@pytest.mark.parametrize("method", ["run", "stream"])
def test_lepton_host_environment_delta_is_computed_once(method, tmp_path, monkeypatch):
    from nadare.backends.fgo import lepton
    from nadare.backends.fgo.backend import FgoLeptonBackend

    backend = FgoLeptonBackend(make_context(tmp_path, "fgo-lepton", {}))
    adapter = backend.adapter
    monkeypatch.setattr(sandbox, "in_flatpak", lambda: True)
    monkeypatch.setenv("PULSE_SERVER", "unix:/run/flatpak/pulse/native")
    calls = []

    def env():
        calls.append(True)
        return {"PULSE_SERVER": "unix:/run/flatpak/pulse/native",
                "LEPTON_CONFIG_DIR": str(tmp_path)}

    monkeypatch.setattr(adapter, "env", env)
    seen = []

    class Started:
        stdout = io.StringIO()
        def poll(self):
            return 0
        def wait(self):
            return 0

    def spawn(argv, **kwargs):
        seen.append((argv, kwargs["env"]))
        return subprocess.CompletedProcess(argv, 0, "", "") if method == "run" else Started()

    monkeypatch.setattr(lepton.subprocess, "run" if method == "run" else "Popen", spawn)
    getattr(adapter, method)(["lepton-fgo", "status"])
    assert calls == [True]
    argv, child_env = seen[0]
    assert argv == ["flatpak-spawn", "--host", f"--env=LEPTON_CONFIG_DIR={tmp_path}",
                    "lepton-fgo", "status"]
    assert child_env["LEPTON_CONFIG_DIR"] == str(tmp_path)


def test_lepton_launch_environment(tmp_path, monkeypatch):
    from nadare.backends.fgo.backend import FgoLeptonBackend

    monkeypatch.setenv("UNRELATED", "host-private")
    monkeypatch.setenv("http_proxy", "opted-in")
    backend = FgoLeptonBackend(
        make_context(
            tmp_path,
            "fgo-lepton",
            {
                "prefix": str(tmp_path / "lepton"),
                "pass_env": ["http_proxy"],
                "extra_env": {"CUSTOM_SECRET": "explicit"},
            },
        )
    )
    env = backend.adapter.env()
    assert "UNRELATED" not in env
    assert env["http_proxy"] == "opted-in"
    assert env["CUSTOM_SECRET"] == "explicit"
    assert env["LEPTON_CONFIG_DIR"] == str(backend.adapter.config_dir)


def test_flatpak_proton_libraries_do_not_restore_host_loader_paths(tmp_path, monkeypatch):
    libs = tmp_path / "libs"
    libs.mkdir()
    prefix = tmp_path / "prefix"
    prefix.mkdir()
    monkeypatch.setenv("LD_LIBRARY_PATH", "/private/host-libraries")
    monkeypatch.setattr(sandbox, "PROTON_LIBS", libs)
    _, env = sandbox.proton_direct(tmp_path / "proton", prefix, ["setup.exe"])
    assert env["LD_LIBRARY_PATH"] == f"{libs}/x86_64:{libs}/i386"


@pytest.mark.parametrize("flatpak", [False, True])
def test_steam_prefix_installer_environment(flatpak, tmp_path, monkeypatch):
    from test_steam_wiring import E7, _e7_tree, _Edges

    from nadare.gui import steam_wiring

    root, prefix, proton = _e7_tree(tmp_path)
    edges = _Edges(monkeypatch)
    monkeypatch.setattr(sandbox, "in_flatpak", lambda: flatpak)
    if flatpak:
        monkeypatch.setattr(protoncmd.umu, "resolve", lambda *a, **kw: None)
    result = steam_wiring.install_webview2(E7, root=root, run_env={
        "DISPLAY": ":0", "UNRELATED": "host-private", "LD_PRELOAD": "/host/lib.so"})
    assert result.ok, result.line
    _, kwargs = edges.spawned[0]
    env = kwargs["env"]
    assert "UNRELATED" not in env
    assert "LD_PRELOAD" not in env
    assert env["STEAM_COMPAT_DATA_PATH"] == str(prefix.parent)
    assert env["STEAM_COMPAT_CLIENT_INSTALL_PATH"] == str(root)
    assert env["PROTON_ENABLE_WAYLAND"] == "0"


@pytest.mark.parametrize("setting", ["http_proxy", ["http_*"], [None, 1], None])
def test_pass_env_accepts_only_exact_names(setting):
    env = process.build_env({"pass_env": setting}, base={"http_proxy": "host-proxy"})
    assert "http_proxy" not in env


def test_steam_setup_client_environment(tmp_path, monkeypatch):
    from nadare.core import steamglue

    monkeypatch.setenv("UNRELATED", "host-private")
    monkeypatch.setenv("http_proxy", "host-proxy")
    seen = []

    class Started:
        def wait(self, timeout):
            return 0

    def popen(argv, **kwargs):
        seen.append(kwargs["env"])
        return Started()

    monkeypatch.setattr(steamglue.subprocess, "Popen", popen)
    assert steamglue.run(["steam"]) == 0
    assert "UNRELATED" not in seen[0]
    assert "http_proxy" not in seen[0]


@pytest.mark.parametrize("names", [
    "XCURSOR_SIZE XCURSOR_THEME GAMESCOPE_WAYLAND_DISPLAY",
    "LIBGL_DEBUG GALLIUM_DRIVER __EGL_VENDOR_LIBRARY_FILENAMES GBM_BACKEND",
    "ENABLE_VK_LAYER_TEST DISABLE_VK_LAYER_TEST DISABLE_LAYER_AMD_SWITCHABLE_GRAPHICS_1",
    "ENABLE_GAMESCOPE_WSI EGL_PLATFORM OBS_VKCAPTURE GAMEMODERUNEXEC",
    "GDK_SCALE GDK_DPI_SCALE QT_SCALE_FACTOR QT_AUTO_SCREEN_SCALE_FACTOR "
    "QT_SCREEN_SCALE_FACTORS QT_ENABLE_HIGHDPI_SCALING",
    "TERM TMPDIR XDG_SESSION_DESKTOP XDG_SESSION_CLASS XDG_SESSION_ID XDG_SEAT XDG_VTNR",
    "SteamDeck SteamOS",
])
def test_desktop_and_graphics_variable_groups(names):
    base = dict.fromkeys(names.split(), "kept")
    base.update(LD_PRELOAD="unsafe", LD_LIBRARY_PATH="unsafe")
    env = launchenv.inherit(base)
    assert env == dict.fromkeys(names.split(), "kept")
