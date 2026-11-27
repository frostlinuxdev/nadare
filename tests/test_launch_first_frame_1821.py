# SPDX-License-Identifier: GPL-3.0-or-later
"""#1821: the launch sheet ends at the game's first frame, not at a 20 s guess.

DXVK and VKD3D-Proton report the swapchain/presenter they created at info level. Nadare
already captures the launch's output line by line, so that line is the primary window
signal and works on Wayland where the X11 window walk finds nothing.
"""
from __future__ import annotations

import contextlib
import threading
import time
from contextlib import contextmanager
from pathlib import Path
from types import SimpleNamespace

import pytest
from _backend_helpers import make_context, write_exe
from PyQt6.QtCore import QPoint, Qt
from PyQt6.QtWidgets import QMessageBox

from nadare.backends import script as script_mod
from nadare.backends import wine, winegame
from nadare.backends.direct import DirectBackend
from nadare.backends.direct_games import DnaBackend
from nadare.backends.direct_wuwa import WutheringWavesBackend
from nadare.backends.launcher import NikkeBackend
from nadare.backends.script import ScriptBackend
from nadare.backends.wine import WineBackend
from nadare.backends.winegame_parts import launching
from nadare.core import gamesandbox, launch_window, process
from nadare.core.backends import BackendError
from nadare.core.config import Config
from nadare.core.events import Cancel
from nadare.gui import launch_sheet, workers
from nadare.gui.main_window import MainWindow
from nadare.gui.operation_presentation import OperationPresentation

QUERY_BACKEND = SimpleNamespace(_patterns=lambda: "query")

DXVK_PRESENTER = "info:  Presenter: Actual swapchain properties:"
VKD3D_SWAPCHAIN = "info:  Creating swapchain (1280 x 720), BufferCount = 3."
VKD3D_PREFIXED = (
    "0124:info:d3d12_swapchain_create_vulkan_swapchain: "
    "Creating swapchain (1280 x 720), BufferCount = 3."
)
VKD3D_REALLOCATED = (
    "info:vkd3d-proton: Reallocating swapchain (1920 x 1080), BufferCount = 2."
)
DXVK_PREFIXED = "0124:info:  Presenter: Actual swapchain properties:"


# ---- the launch environment asks the layers for the line -------------------
FIRST_FRAME_ENV = {
    "DXVK_LOG_LEVEL": "info",
    "DXVK_LOG_PATH": "none",
    "VKD3D_DEBUG": "info",
}


def _clear_layer_env(monkeypatch):
    for name in FIRST_FRAME_ENV:
        monkeypatch.delenv(name, raising=False)


def _wine_profile(tmp_path, *, exe="game.exe", profile=None):
    game = tmp_path / "game"
    write_exe(game / exe)
    values = {
        "install_dir": str(game),
        "exe_name": exe,
        "prefix": str(tmp_path / "prefix"),
        "wine_binary": "/usr/bin/wine",
    }
    values.update(profile or {})
    return WineBackend(make_context(tmp_path, "wine", values))


def test_the_shared_environment_builder_does_not_set_them(monkeypatch):
    """wineboot, installers, scripts and Lepton share ``build_env``: none of them need these."""
    _clear_layer_env(monkeypatch)
    env = process.build_env({}, base={})
    for name in FIRST_FRAME_ENV:
        assert name not in env


def test_a_game_launch_turns_on_first_frame_logging(tmp_path, monkeypatch):
    _clear_layer_env(monkeypatch)
    _argv, env, _prefix, _target = _wine_profile(tmp_path).build()
    for name, value in FIRST_FRAME_ENV.items():
        assert env[name] == value


def test_a_profile_log_level_still_wins(tmp_path, monkeypatch):
    _clear_layer_env(monkeypatch)
    backend = _wine_profile(
        tmp_path, profile={"extra_env": {"DXVK_LOG_LEVEL": "none"}}
    )
    env = backend.build()[1]
    assert env["DXVK_LOG_LEVEL"] == "none"
    assert env["DXVK_LOG_PATH"] == "none"


def test_a_host_log_level_still_wins(tmp_path, monkeypatch):
    _clear_layer_env(monkeypatch)
    monkeypatch.setenv("DXVK_LOG_LEVEL", "none")
    monkeypatch.setenv("VKD3D_DEBUG", "err")
    env = _wine_profile(tmp_path).build()[1]
    assert env["DXVK_LOG_LEVEL"] == "none"
    assert env["VKD3D_DEBUG"] == "err"


def test_a_direct_game_launch_turns_on_first_frame_logging(tmp_path, monkeypatch):
    _clear_layer_env(monkeypatch)
    install = tmp_path / "direct-game"
    write_exe(install / "Game.exe")
    backend = DirectBackend(
        make_context(
            tmp_path,
            "direct",
            {"install_dir": str(install), "exe_name": "Game.exe"},
        )
    )
    monkeypatch.setattr(
        backend,
        "_prefix_command",
        lambda args, *a, **k: (["umu-run", *args], {"WINEPREFIX": str(tmp_path / "p")}),
    )
    _argv, env, _target = backend.build_launch()
    for name, value in FIRST_FRAME_ENV.items():
        assert env[name] == value


def test_the_real_direct_launch_path_turns_on_first_frame_logging(tmp_path, monkeypatch):
    """``launch()`` runs ``_launch_direct()``; ``build_launch()`` is the official route."""
    _clear_layer_env(monkeypatch)
    install = tmp_path / "direct-game"
    write_exe(install / "Game.exe")
    backend = DirectBackend(
        make_context(
            tmp_path,
            "direct",
            {"install_dir": str(install), "exe_name": "Game.exe"},
        )
    )
    captured: dict[str, str] = {}

    def fake_stream(argv, env, **kwargs):
        captured.update(env)
        return 0

    monkeypatch.setattr(backend, "_run_vcredist", lambda *args, **kwargs: None)
    monkeypatch.setattr(
        backend,
        "_prefix_command",
        lambda args, *a, **k: (["umu-run", *args], {}),
    )
    monkeypatch.setattr(backend, "_secure_wrap", lambda argv, env: (argv, env))
    monkeypatch.setattr(backend, "_record_polite_stop", lambda *args, **kwargs: None)
    monkeypatch.setattr(backend, "_runtime_note", lambda: "")
    monkeypatch.setattr(backend, "_runner_facts", lambda: {})
    monkeypatch.setattr(backend, "_log_launch_details", lambda env: None)
    monkeypatch.setattr(backend, "_run_lock", contextlib.nullcontext)
    closed = threading.Event()
    closed.set()
    monkeypatch.setattr(backend, "_game_process_watch", lambda: contextlib.nullcontext(closed))
    monkeypatch.setattr(backend, "_begin_game_output", lambda: None)
    monkeypatch.setattr(backend, "_end_game_output", lambda: None)
    monkeypatch.setattr(backend, "_stop_link_relay", lambda: None)
    monkeypatch.setattr(type(backend), "_renderer_probe", lambda self: _Probe())
    monkeypatch.setattr(launching.graphics, "effective_renderer", lambda profile: (None, ""))
    monkeypatch.setattr(launching, "stream", fake_stream)

    assert backend._launch_direct() == 0
    for name, value in FIRST_FRAME_ENV.items():
        assert captured[name] == value


class _Probe:
    result = ""

    def start(self):
        return self

    def stop(self) -> None:
        pass

    def join(self, timeout=None) -> None:
        pass


def test_wineboot_and_installer_commands_do_not_get_the_first_frame_env(tmp_path, monkeypatch):
    _clear_layer_env(monkeypatch)
    backend = _wine_profile(tmp_path)
    for argv in (["wineboot", "-u"], ["setup.exe", "/S"]):
        _argv, env = backend._prefix_command(argv)
        for name in FIRST_FRAME_ENV:
            assert name not in env


def test_a_script_launch_does_not_get_the_first_frame_env(tmp_path, monkeypatch):
    _clear_layer_env(monkeypatch)
    write_exe(tmp_path / "run.sh", "#!/bin/sh\necho hi\n")
    captured = {}

    def fake_stream(argv, env, **kwargs):
        captured.update(env)
        return 0

    monkeypatch.setattr(script_mod, "stream", fake_stream)
    backend = ScriptBackend(
        make_context(
            tmp_path, "script", {"launch_exe": "run.sh", "install_dir": str(tmp_path)}
        )
    )
    assert backend.launch() == 0
    for name in FIRST_FRAME_ENV:
        assert name not in captured


@pytest.mark.parametrize("flatpak", [False, True])
def test_the_sandbox_passes_the_first_frame_environment_through(flatpak, tmp_path):
    home, prefix = tmp_path / "home", tmp_path / "prefix"
    home.mkdir(exist_ok=True)
    prefix.mkdir(exist_ok=True)
    env = _wine_profile(tmp_path).build()[1]
    if flatpak:
        argv, loader_env = gamesandbox.wrap_flatpak(
            ["game.exe"], env, prefix=prefix, home=home, env_dir=tmp_path
        )
        envfile = Path(argv[argv.index("nadare-sandbox") + 1])
        child = dict(
            item.split("=", 1) for item in envfile.read_text().split("\0") if item
        )
    else:
        argv, loader_env = gamesandbox.wrap(["game.exe"], env, prefix=prefix, home=home)
        child = dict(loader_env)
        for index, arg in enumerate(argv):
            if arg == "--setenv":
                child[argv[index + 1]] = argv[index + 2]
    for name, value in FIRST_FRAME_ENV.items():
        assert child[name] == value
        assert loader_env[name] == value


# ---- the presenter line is the primary signal ------------------------------
def test_the_dxvk_presenter_line_counts_at_once(tmp_path):
    probe = launch_window.WindowEvidence(set(), proc_root=tmp_path / "gone")
    probe.feed(DXVK_PRESENTER)
    assert probe.seen([], 1.0, {})


def test_the_vkd3d_swapchain_line_counts_at_once(tmp_path):
    probe = launch_window.WindowEvidence(set(), proc_root=tmp_path / "gone")
    probe.feed(VKD3D_SWAPCHAIN)
    assert probe.seen([], 1.0, {})


@pytest.mark.parametrize("line", [VKD3D_PREFIXED, VKD3D_REALLOCATED, DXVK_PREFIXED])
def test_a_real_prefixed_swapchain_line_counts_at_once(tmp_path, line):
    """VKD3D-Proton puts its function after ``info:``; logs also carry a thread id."""
    probe = launch_window.WindowEvidence(set(), proc_root=tmp_path / "gone")
    probe.feed(line)
    assert probe.seen([], 1.0, {})


@pytest.mark.parametrize(
    "line",
    [
        "info:  DXVK: v3.1.1-19-g601930949",
        "info:  Created cache file: C:\\users\\steamuser\\AppData\\Local\\dxvk\\x.dxvk.bin",
        "renderer: DXVK on Vulkan",
        "info:  Presenter: Got -1000208000, recreating swapchain",
        "err:   DXGI: Failed to parse display metadata",
        "info:  Failed to create swapchain, vr -9",
        "0124:err:d3d12_swapchain_create_vulkan_swapchain: "
        "Creating swapchain (1280 x 720), BufferCount = 3.",
        "0124:info:d3d12_swapchain_create_vulkan_swapchain: Failed to create swapchain",
    ],
)
def test_a_dxvk_or_vkd3d_line_that_is_not_the_first_frame_does_not_count(tmp_path, line):
    probe = launch_window.WindowEvidence(set(), proc_root=tmp_path / "gone")
    probe.feed(line)
    assert not probe.seen([], 1.0, {})


def test_the_wait_does_not_end_before_the_presenter_line(tmp_path):
    """A DXVK banner and a swapchain *error* arrive before the first frame."""
    probe = launch_window.WindowEvidence(set(), proc_root=tmp_path / "gone")
    for line in (
        "info:  DXVK: v3.1.1",
        "info:  Presenter: Failed to create Vulkan swapchain:",
        "info:  Presenter: Got -1000208000, recreating swapchain",
    ):
        probe.feed(line)
    assert not probe.seen([], launch_window.GRAPHICS_GRACE - 0.1, {})
    probe.feed(DXVK_PRESENTER)
    assert probe.seen([], 1.0, {})


def test_the_presenter_ends_a_wayland_launch_without_x11_windows(tmp_path):
    # Native Wayland: the X11 walk returns nothing, so this line is the only signal.
    probe = launch_window.WindowEvidence(set(), proc_root=tmp_path / "gone")
    assert probe.seen([10], 1.0, {}) is False
    probe.feed(DXVK_PRESENTER)
    assert probe.seen([10], 1.0, {})


# ---- a launcher or helper line is not the game -----------------------------
def test_a_launcher_line_before_the_game_process_does_not_count(tmp_path):
    running = False

    def game_running():
        return running

    probe = launch_window.WindowEvidence(
        set(), proc_root=tmp_path / "gone", game_processes=game_running
    )
    probe.feed(DXVK_PRESENTER)  # the launcher's own swapchain
    assert not probe.seen([10], 1.0, {})

    running = True
    probe.feed(DXVK_PRESENTER)  # the game's swapchain
    assert probe.seen([10], 1.0, {})


def test_the_watch_ignores_a_launcher_line_and_ends_on_the_game_line(monkeypatch):
    running = [False]
    monkeypatch.setattr(launch_window, "x11_windows", lambda: {})
    monkeypatch.setattr(launch_window, "POLL_INTERVAL", 0.01)
    monkeypatch.setattr(wine, "scan_pids", lambda query: [10] if running[0] else [])
    emitted = []
    with launch_window.watch(
        wine.game_pids(QUERY_BACKEND),
        emitted.append,
        Cancel(),
        game_processes=lambda: running[0],
    ) as watched:
        watched.feed(DXVK_PRESENTER)  # HottaLauncher / NTE launcher
        time.sleep(0.05)
        assert emitted == []

        running[0] = True
        time.sleep(0.05)
        assert emitted == []  # the launcher line is gone, not remembered

        watched.feed(DXVK_PRESENTER)  # QRSL.exe / HTGame.exe
        deadline = time.monotonic() + 2
        while not emitted and time.monotonic() < deadline:
            time.sleep(0.01)
    assert [event.kind for event in emitted] == ["window_seen"]


def test_a_backend_without_a_process_rule_still_watches_the_presenter(monkeypatch):
    calls = []
    monkeypatch.setattr(
        launch_window, "x11_windows", lambda: calls.append("windows") or {}
    )
    monkeypatch.setattr(launch_window, "POLL_INTERVAL", 0.01)
    emitted = []
    with launch_window.watch(None, emitted.append, Cancel()) as watched:
        assert watched is not None
        watched.feed("info:  DXVK: v3.1.1")
        time.sleep(0.05)
        assert emitted == []
        watched.feed(DXVK_PRESENTER)
        deadline = time.monotonic() + 2
        while not emitted and time.monotonic() < deadline:
            time.sleep(0.01)
    assert [event.kind for event in emitted] == ["window_seen"]
    assert calls == []  # no process rule: no X11 walk


def _proc_entry(root: Path, pid: int, argv: list[str], env: dict[str, str] | None = None) -> None:
    entry = root / str(pid)
    entry.mkdir(parents=True)
    (entry / "cmdline").write_bytes(b"\0".join(item.encode() for item in argv) + b"\0")
    if env is not None:
        (entry / "environ").write_bytes(
            b"".join(f"{key}={value}".encode() + b"\0" for key, value in env.items())
        )


@pytest.fixture
def fake_proc(tmp_path, monkeypatch):
    """A fake /proc for the real ``scan_pids``: no scan is stubbed out, only its root."""
    root = tmp_path / "proc"
    root.mkdir()
    real_scan = wine.scan_pids
    monkeypatch.setattr(wine, "_own_pid", lambda: -1)
    monkeypatch.setattr(wine, "scan_pids", lambda query: real_scan(query, proc_root=root))
    return root


def test_a_wine_profile_game_rule_matches_the_game_in_its_prefix(tmp_path, fake_proc):
    """``WineBackend`` has ``_prefix()``, not ``prefix()``: the rule must use ``_patterns()``."""
    backend = _wine_profile(tmp_path)
    prefix = backend._patterns().prefix
    _proc_entry(
        fake_proc, 100, [r"Z:\games\game.exe"], {"WINEPREFIX": prefix + "/pfx"}
    )
    rule = wine.game_processes(backend)
    assert rule is not None
    assert rule() is True


def test_a_wine_profile_game_rule_ignores_another_prefix(tmp_path, fake_proc):
    backend = _wine_profile(tmp_path)
    _proc_entry(
        fake_proc, 101, [r"Z:\games\game.exe"], {"WINEPREFIX": str(tmp_path / "other")}
    )
    assert wine.game_processes(backend)() is False


def test_a_wine_profile_game_rule_follows_process_match_to_a_spawned_game(tmp_path, fake_proc):
    """A generic profile whose ``exe_name`` is a launcher: ``process_match`` names the game."""
    backend = _wine_profile(
        tmp_path, exe="launcher.exe", profile={"process_match": "RealGame.exe"}
    )
    prefix = backend._patterns().prefix
    _proc_entry(fake_proc, 102, [r"Z:\games\RealGame.exe"], {"WINEPREFIX": prefix})
    assert wine.game_processes(backend)() is True


def test_wuthering_waves_game_rule_names_the_client_it_launches(tmp_path, fake_proc):
    """``Wuthering Waves.exe`` is Kuro's stub; Nadare starts ``Client-Win64-Shipping``."""
    install = tmp_path / "wuwa"
    write_exe(install / "Wuthering Waves.exe")
    backend = WutheringWavesBackend(
        make_context(tmp_path, "direct-wuwa", {"install_dir": str(install)})
    )
    prefix = str(backend.prefix())
    _proc_entry(
        fake_proc,
        200,
        [r"Z:\wuwa\Client\Binaries\Win64\Client-Win64-Shipping.exe"],
        {"WINEPREFIX": prefix + "/pfx"},
    )
    assert backend.game_processes()() is True


def test_wuthering_waves_game_rule_ignores_the_launcher_stub(tmp_path, fake_proc):
    install = tmp_path / "wuwa"
    write_exe(install / "Wuthering Waves.exe")
    backend = WutheringWavesBackend(
        make_context(tmp_path, "direct-wuwa", {"install_dir": str(install)})
    )
    prefix = str(backend.prefix())
    _proc_entry(fake_proc, 201, [r"Z:\wuwa\Wuthering Waves.exe"], {"WINEPREFIX": prefix})
    assert backend.game_processes()() is False


def test_duet_night_abyss_game_rule_names_the_client_behind_the_stub(tmp_path, fake_proc):
    """``EM.exe`` hands off to ``EM-Win64-Shipping.exe`` and exits."""
    install = tmp_path / "dna"
    write_exe(install / "EM.exe")
    backend = DnaBackend(make_context(tmp_path, "direct-dna", {"install_dir": str(install)}))
    prefix = str(backend.prefix())
    _proc_entry(
        fake_proc,
        300,
        [r"Z:\dna\EM\Binaries\Win64\EM-Win64-Shipping.exe"],
        {"WINEPREFIX": prefix + "/pfx"},
    )
    assert backend.game_processes()() is True


def test_duet_night_abyss_game_rule_ignores_the_start_stub(tmp_path, fake_proc):
    install = tmp_path / "dna"
    write_exe(install / "EM.exe")
    backend = DnaBackend(make_context(tmp_path, "direct-dna", {"install_dir": str(install)}))
    prefix = str(backend.prefix())
    _proc_entry(fake_proc, 301, [r"Z:\dna\EM.exe"], {"WINEPREFIX": prefix})
    assert backend.game_processes()() is False


def test_wuthering_waves_game_rule_follows_the_profile_exe_override(tmp_path, fake_proc):
    """A profile ``exe_name`` replaces the exe Nadare starts, so the gate follows it."""
    install = tmp_path / "wuwa"
    write_exe(install / "Wuthering Waves.exe")
    backend = WutheringWavesBackend(
        make_context(
            tmp_path,
            "direct-wuwa",
            {"install_dir": str(install), "exe_name": "Wuthering Waves.exe"},
        )
    )
    _proc_entry(
        fake_proc,
        202,
        [r"Z:\wuwa\Wuthering Waves.exe"],
        {"WINEPREFIX": str(backend.prefix())},
    )
    assert backend.game_processes()() is True


@pytest.mark.parametrize(
    "backend_cls,kind,stub,client",
    [
        (WutheringWavesBackend, "direct-wuwa", "Wuthering Waves.exe", "Client-Win64-Shipping.exe"),
        (DnaBackend, "direct-dna", "EM.exe", "EM-Win64-Shipping.exe"),
    ],
)
def test_direct_games_keep_the_b29_running_rule(
    backend_cls, kind, stub, client, tmp_path, monkeypatch
):
    """The first-frame name must not drive Flatpak detection or the auto-stop (#1821 R3)."""
    install = tmp_path / kind
    write_exe(install / stub)
    backend = backend_cls(make_context(tmp_path, kind, {"install_dir": str(install)}))
    target = backend.direct_target()
    assert target is not None
    write_exe(target)
    assert backend.game_process_names == ()
    assert backend.first_frame_process_names == (client,)

    monkeypatch.setattr(winegame.sandbox, "in_flatpak", lambda: True)
    monkeypatch.setattr(winegame.runlock, "running", lambda prefix: True)
    monkeypatch.setattr(winegame, "scan_pids", lambda query: [])
    monkeypatch.setattr(winegame.graphics, "vulkan_note", lambda: "")
    assert backend._game_running() is True
    assert backend.status().running is True
    with pytest.raises(BackendError, match="Stop the game"):
        backend._refuse_if_running("updating it")


@pytest.mark.parametrize(
    "backend_cls,kind,stub",
    [
        (WutheringWavesBackend, "direct-wuwa", "Wuthering Waves.exe"),
        (DnaBackend, "direct-dna", "EM.exe"),
    ],
)
def test_a_direct_game_does_not_auto_stop_when_the_client_exits(
    backend_cls, kind, stub, tmp_path, monkeypatch
):
    """Without ``game_process_names`` the watcher reports a start and leaves Stop alone."""
    install = tmp_path / kind
    write_exe(install / stub)
    backend = backend_cls(make_context(tmp_path, kind, {"install_dir": str(install)}))
    events, stopped, logs = [], [], []
    backend.ctx.emit = events.append
    monkeypatch.setattr(backend, "stop", lambda: stopped.append(True))
    monkeypatch.setattr(backend, "info", logs.append)
    scans = iter(([123], [], [], [], [], [], [], []))
    monkeypatch.setattr(launching, "scan_pids", lambda query: next(scans))
    now = [0.0]
    monkeypatch.setattr(launching.time, "monotonic", lambda: now[0])

    class Done:
        def wait(self, interval):
            now[0] += 1
            return now[0] > 8

    closed = threading.Event()
    backend._watch_game_process(Done(), closed)
    assert stopped == []
    assert not closed.is_set()
    assert logs == []
    assert [event.kind for event in events] == ["game_running"]


def test_nikke_game_rule_excludes_the_sail_host(tmp_path, fake_proc):
    """The Sail host starts before the game and is windowless: it must not open the gate."""
    from nadare.backends import nikke as nikke_sail

    backend = NikkeBackend(
        make_context(tmp_path, "launcher-nikke", {"prefix": str(tmp_path / "prefix")})
    )
    prefix = str(backend.prefix())
    _proc_entry(fake_proc, 400, [nikke_sail.SAIL_HOST_NAME], {"WINEPREFIX": prefix})
    assert backend.game_processes()() is False


def test_nikke_game_rule_still_counts_the_game(tmp_path, fake_proc):
    backend = NikkeBackend(
        make_context(tmp_path, "launcher-nikke", {"prefix": str(tmp_path / "prefix")})
    )
    prefix = str(backend.prefix())
    _proc_entry(fake_proc, 401, [r"Z:\NIKKE\game\nikke.exe"], {"WINEPREFIX": prefix})
    assert backend.game_processes()() is True


def test_czn_has_a_game_rule_that_ignores_install_helpers(tmp_path, monkeypatch):
    """Without a rule the worker watches every presenter line ungated (vcredist, WebView2)."""
    from nadare.backends.czn.backend import CznBackend
    from nadare.backends.czn.backend_parts import launching as czn_launching

    backend = CznBackend(
        make_context(tmp_path, "stove-czn", {"prefix": str(tmp_path / "prefix")})
    )
    calls = []
    monkeypatch.setattr(
        czn_launching,
        "game_pids",
        lambda game_dir, *, helpers=True, **kwargs: calls.append(helpers) or [],
    )
    rule = backend.game_processes()
    assert rule is not None
    assert rule() is False
    assert calls == [False]  # the helper processes are not the game

    monkeypatch.setattr(
        czn_launching, "game_pids", lambda game_dir, *, helpers=True, **kwargs: [4242]
    )
    assert rule() is True


def test_czn_game_rule_ignores_the_shield_in_the_stub_arguments(tmp_path, monkeypatch):
    """The stub carries the shield path in argv[1..]; only its own exe can open the gate."""
    from nadare.backends.czn.backend import CznBackend
    from nadare.backends.czn.backend_parts import launching as czn_launching

    backend = CznBackend(
        make_context(tmp_path, "stove-czn", {"prefix": str(tmp_path / "prefix")})
    )
    root = tmp_path / "proc"
    root.mkdir()
    game_exe = "Z:" + str(backend.shield()).replace("/", "\\")
    _proc_entry(
        root,
        500,
        [r"C:\Stove\czn_pcsdk_stub.exe", game_exe, "--launch"],
    )
    monkeypatch.setattr(czn_launching, "PROC_ROOT", root)
    rule = backend.game_processes()
    assert rule is not None
    assert rule() is False

    _proc_entry(root, 501, [game_exe])
    assert rule() is True


# ---- the fallback and the X11 signal stay ----------------------------------
def test_the_graphics_fallback_still_needs_the_grace(tmp_path):
    entry = tmp_path / "10"
    entry.mkdir()
    (entry / "stat").write_text("10 (game) S 1 0 0")
    (entry / "maps").write_text("/wine/d3d11.dll")
    (entry / "fd").mkdir()
    probe = launch_window.WindowEvidence(set(), proc_root=tmp_path)
    assert not probe.seen([10], launch_window.GRAPHICS_GRACE - 0.1, {})
    assert probe.seen([10], launch_window.GRAPHICS_GRACE, {})


def test_an_x11_window_still_counts_without_any_log_line(tmp_path):
    entry = tmp_path / "10"
    entry.mkdir()
    (entry / "stat").write_text("10 (game) S 1 0 0")
    (entry / "fd").mkdir()
    probe = launch_window.WindowEvidence(set(), proc_root=tmp_path)
    assert probe.seen([10], 1.0, {101: 10})


# ---- the watch and the worker feed it --------------------------------------
def test_the_watch_ends_on_the_presenter_and_not_on_another_dxvk_line(monkeypatch):
    monkeypatch.setattr(launch_window, "x11_windows", lambda: {})
    monkeypatch.setattr(launch_window, "POLL_INTERVAL", 0.01)
    monkeypatch.setattr(wine, "scan_pids", lambda query: [10])
    emitted = []
    with launch_window.watch(wine.game_pids(QUERY_BACKEND), emitted.append, Cancel()) as watched:
        assert watched is not None
        watched.feed("info:  DXVK: v3.1.1")
        time.sleep(0.05)
        assert emitted == []
        watched.feed(DXVK_PRESENTER)
        deadline = time.monotonic() + 2
        while not emitted and time.monotonic() < deadline:
            time.sleep(0.01)
    assert [event.kind for event in emitted] == ["window_seen"]


def test_the_worker_feeds_only_launch_log_lines_to_the_watch(monkeypatch):
    worker = workers.BackendWorker(Config(), "g", "launch")
    fed = []
    seen_kwargs = []

    class FakeWatch:
        def feed(self, line):
            fed.append(line)

    @contextmanager
    def watch(actual, emit, cancel, **kwargs):
        seen_kwargs.append(kwargs)
        yield FakeWatch()

    monkeypatch.setattr(launch_window, "watch", watch)

    def play(backend):
        worker._on_event(workers.Event("log", DXVK_PRESENTER))
        worker._on_event(workers.Event("step", "starting the game"))
        return 0

    monkeypatch.setattr(workers.launchgate, "play", play)
    assert worker._dispatch(SimpleNamespace(game_pids=lambda: (lambda: []))) == 0
    assert fed == [DXVK_PRESENTER]
    assert callable(seen_kwargs[0]["game_processes"])
    worker._on_event(workers.Event("log", "info:  Presenter: Actual swapchain properties:"))
    assert fed == [DXVK_PRESENTER]  # the watch is over


def test_the_worker_reads_the_watch_once_per_line():
    """The monitor can clear ``_window_watch`` between the check and the call (#1821)."""
    fed = []

    class RaceWorker(workers.BackendWorker):
        reads = 0
        watch = None

        def __getattribute__(self, name):
            if name == "_window_watch":
                reads = object.__getattribute__(self, "reads") + 1
                object.__setattr__(self, "reads", reads)
                if reads > 1:
                    return None
                return object.__getattribute__(self, "watch")
            return super().__getattribute__(name)

    raced = RaceWorker(Config(), "g", "launch")
    raced.watch = SimpleNamespace(feed=fed.append)
    raced._on_event(workers.Event("log", DXVK_PRESENTER))  # must not raise
    assert fed == [DXVK_PRESENTER]
    assert raced.reads == 1


def test_the_worker_ends_on_a_streamed_presenter_line(monkeypatch):
    """BackendWorker + the real watch, with no X11 window, emits ``window_seen``."""
    worker = workers.BackendWorker(Config(), "g", "launch")
    events = []
    worker.backend_event.connect(events.append, Qt.ConnectionType.DirectConnection)
    monkeypatch.setattr(launch_window, "x11_windows", lambda: {})
    monkeypatch.setattr(launch_window, "POLL_INTERVAL", 0.01)

    def play(backend):
        worker._on_event(workers.Event("log", "info:  DXVK: v3.1.1"))
        worker._on_event(workers.Event("log", DXVK_PREFIXED))
        deadline = time.monotonic() + 2
        while not any(event.kind == "window_seen" for event in events):
            if time.monotonic() >= deadline:
                break
            time.sleep(0.01)
        return 0

    monkeypatch.setattr(workers.launchgate, "play", play)
    backend = SimpleNamespace(game_pids=lambda: (lambda: [10]))
    assert worker._dispatch(backend) == 0
    assert [event.kind for event in events if event.kind == "window_seen"] == [
        "window_seen"
    ]


def test_the_worker_ignores_a_helper_presenter_line(monkeypatch):
    """A pre-game helper line must not emit ``window_seen`` through the real worker watch."""
    worker = workers.BackendWorker(Config(), "g", "launch")
    events = []
    worker.backend_event.connect(events.append, Qt.ConnectionType.DirectConnection)
    monkeypatch.setattr(launch_window, "x11_windows", lambda: {})
    monkeypatch.setattr(launch_window, "POLL_INTERVAL", 0.01)
    running = [False]
    backend = SimpleNamespace(
        game_pids=lambda: (lambda: [10]),
        game_processes=lambda: (lambda: running[0]),
    )

    def play(_backend):
        worker._on_event(workers.Event("log", DXVK_PRESENTER))  # vcredist / WebView2
        time.sleep(0.05)
        assert not any(event.kind == "window_seen" for event in events)
        running[0] = True
        worker._on_event(workers.Event("log", DXVK_PRESENTER))  # the game's own line
        deadline = time.monotonic() + 2
        while not any(event.kind == "window_seen" for event in events):
            if time.monotonic() >= deadline:
                break
            time.sleep(0.01)
        return 0

    monkeypatch.setattr(workers.launchgate, "play", play)
    assert worker._dispatch(backend) == 0
    assert [event.kind for event in events if event.kind == "window_seen"] == [
        "window_seen"
    ]


def test_a_dxvk_info_burst_does_not_flood_the_run_log_or_replay(tmp_path, monkeypatch):
    """5000 DXVK info lines: the run log and the 400-event replay keep Nadare's own lines."""
    logs = tmp_path / "logs"
    monkeypatch.setattr(workers.paths, "logs_dir", lambda: logs)
    worker = workers.BackendWorker(Config.load(tmp_path / "config.json"), "g", "launch")
    presentation = OperationPresentation()
    presentation.busy_changed("g", "launch", True)
    worker.backend_event.connect(
        lambda event: presentation.event("g", event), Qt.ConnectionType.DirectConnection
    )
    own = [
        "runner: GE-Proton11-7",
        "exec: umu-run game.exe",
        "env: DXVK_LOG_LEVEL=info",
    ]
    for line in own:
        worker._on_event(workers.Event("log", line))
    for _ in range(5000):
        worker._on_event(workers.Event("log", "info:  DXVK: v3.1.1"))
    worker._on_event(workers.Event("log", "err:   DXGI: Failed to parse display metadata"))
    worker._close_log()

    text = next((logs / "g").glob("launch-*.log")).read_text(encoding="utf-8")
    for label in ("runner:", "exec:", "env:"):
        assert f"[log] {label}" in text
    assert text.count("DXVK: v3.1.1") <= 20
    assert "err:   DXGI: Failed to parse display metadata" in text

    messages = [event.message for event in presentation.view("g").events]
    for line in own:
        assert line in messages
    assert sum("DXVK: v3.1.1" in message for message in messages) <= 20
    assert any("err:   DXGI" in message for message in messages)


def test_the_graphics_fallback_works_through_the_watch(tmp_path, monkeypatch):
    entry = tmp_path / "10"
    entry.mkdir()
    (entry / "stat").write_text("10 (game) S 1 0 0")
    (entry / "maps").write_text("/wine/d3d11.dll")
    (entry / "fd").mkdir()

    real_evidence = launch_window.WindowEvidence

    def evidence(baseline, **kwargs):
        return real_evidence(baseline, proc_root=tmp_path)

    monkeypatch.setattr(launch_window, "WindowEvidence", evidence)
    monkeypatch.setattr(launch_window, "x11_windows", lambda: {})
    monkeypatch.setattr(launch_window, "POLL_INTERVAL", 0.01)
    monkeypatch.setattr(launch_window, "GRAPHICS_GRACE", 0.3)
    monkeypatch.setattr(wine, "scan_pids", lambda query: [10])
    emitted = []
    with launch_window.watch(
        wine.game_pids(QUERY_BACKEND),
        emitted.append,
        Cancel(),
        game_processes=lambda: True,
    ):
        time.sleep(0.1)
        assert emitted == []  # still inside the grace
        deadline = time.monotonic() + 2
        while not emitted and time.monotonic() < deadline:
            time.sleep(0.01)
    assert [event.kind for event in emitted] == ["window_seen"]


# ---- Hide appears after 10 s, Stop stays -----------------------------------
@pytest.fixture
def window(tmp_path, qtbot, monkeypatch):
    config = Config.load(tmp_path / "config.json")
    config.data.update(check_updates=False, check_notices=False)
    config.add_profile("g", backend="wine", title="Example Game")
    window = MainWindow(config)
    qtbot.addWidget(window)
    monkeypatch.setattr(window.controller, "start", lambda *a, **k: True)
    monkeypatch.setattr(QMessageBox, "warning", lambda *a: None)
    monkeypatch.setattr(QMessageBox, "critical", lambda *a: None)
    window.show()
    window.open_game("g")
    yield window
    window.hide()


def test_hide_appears_after_ten_seconds_and_stop_stays(window, qtbot):
    assert launch_sheet.HIDE_AFTER == 10
    window._start("g", "launch")
    sheet = window._launch_sheet
    sheet.started = float(int(sheet.started))  # whole seconds: started + N - started == N
    window._on_event("g", workers.Event("game_running"))
    assert [button.text() for button in sheet.buttons()] == ["Stop"]
    assert sheet.status.text() == "Waiting for the window"

    sheet.tick(sheet.started + launch_sheet.HIDE_AFTER - 0.1)
    assert [button.text() for button in sheet.buttons()] == ["Stop"]
    assert sheet.status.text() == "Waiting for the window"

    sheet.tick(sheet.started + launch_sheet.HIDE_AFTER)
    assert [button.text() for button in sheet.buttons()] == ["Stop", "Hide"]
    assert sheet.status.text() == "Waiting for the window"  # not the 90 s timeout text

    # Once Hide is on screen, nothing else is a way out: Esc, close, outside clicks.
    qtbot.keyClick(sheet, Qt.Key.Key_Escape)
    assert sheet.isVisible()
    sheet.close()
    assert sheet.isVisible()
    qtbot.mouseClick(sheet, Qt.MouseButton.LeftButton, pos=QPoint(2, 2))
    assert sheet.isVisible()

    qtbot.mouseClick(sheet.hide_button, Qt.MouseButton.LeftButton)
    assert not sheet.isVisible()
    assert window._launch_sheet is None


def test_hide_follows_the_raw_elapsed_time(window, monkeypatch):
    """A fractional Hide-after must not wait for the next whole second."""
    window._start("g", "launch")
    sheet = window._launch_sheet
    window._on_event("g", workers.Event("game_running"))
    monkeypatch.setattr(launch_sheet, "HIDE_AFTER", 0.5)
    sheet.tick(sheet.started + 0.75)
    assert [button.text() for button in sheet.buttons()] == ["Stop", "Hide"]
    assert sheet.status.text() == "Waiting for the window"


def test_the_sheet_still_says_why_at_the_hard_timeout(window):
    window._start("g", "launch")
    sheet = window._launch_sheet
    sheet.started = float(int(sheet.started))  # whole seconds: started + N - started == N
    sheet.tick(sheet.started + launch_window.WATCH_SECONDS)
    assert sheet.status.text() == (
        "Still starting. Its own launcher or anti-cheat can take a while."
    )
    assert [button.text() for button in sheet.buttons()] == ["Stop", "Hide"]
