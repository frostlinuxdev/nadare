# SPDX-License-Identifier: GPL-3.0-or-later
"""Release b20, area process: launch environment, umu, lifecycle and renderer guards."""

from __future__ import annotations

import os
import signal
import threading
import time
from pathlib import Path

import pytest
from _backend_helpers import make_context, write_exe

from nadare.backends.wine import WineBackend
from nadare.backends.winegame import WineGameBackend
from nadare.core import graphics, process, umu


# ---------------------------------------------------------------------------
# #595: the launch pins the prefix variables even when extra_env names them
# ---------------------------------------------------------------------------
def test_launch_extra_env_cannot_move_the_winegame_prefix(tmp_path: Path) -> None:
    prefix = tmp_path / "prefix"
    other = tmp_path / "other"
    wine = write_exe(tmp_path / "wine")
    ctx = make_context(
        tmp_path,
        "wine",
        {
            "prefix": str(prefix),
            "wine_binary": str(wine),
            "extra_env": {
                "WINEPREFIX": str(other),
                "STEAM_COMPAT_DATA_PATH": str(other),
                "PROTONPATH": str(other),
                "DXVK_HUD": "fps",
            },
        },
    )
    _argv, env = WineGameBackend(ctx)._prefix_command(["game.exe"])
    assert env["WINEPREFIX"] == str(prefix)
    assert env.get("STEAM_COMPAT_DATA_PATH") != str(other)
    assert env.get("PROTONPATH") != str(other)
    assert env["DXVK_HUD"] == "fps"


def test_the_callers_extra_env_cannot_move_the_winegame_prefix_either(tmp_path: Path) -> None:
    prefix = tmp_path / "prefix"
    other = tmp_path / "other"
    wine = write_exe(tmp_path / "wine")
    ctx = make_context(tmp_path, "wine", {"prefix": str(prefix), "wine_binary": str(wine)})
    _argv, env = WineGameBackend(ctx)._prefix_command(
        ["game.exe"], extra_env={"WINEPREFIX": str(other), "PROTON_ENABLE_WAYLAND": "0"}
    )
    assert env["WINEPREFIX"] == str(prefix)
    assert env["PROTON_ENABLE_WAYLAND"] == "0"  # other variables still pass through


def test_launch_extra_env_cannot_move_the_wine_prefix(tmp_path: Path) -> None:
    prefix = tmp_path / "prefix"
    other = tmp_path / "other"
    wine = write_exe(tmp_path / "wine")
    ctx = make_context(
        tmp_path,
        "wine",
        {
            "prefix": str(prefix),
            "wine_binary": str(wine),
            "extra_env": {"WINEPREFIX": str(other), "DXVK_HUD": "fps"},
        },
    )
    _argv, env = WineBackend(ctx)._prefix_command(["game.exe"])
    assert env["WINEPREFIX"] == str(prefix)
    assert env["DXVK_HUD"] == "fps"


def test_relative_path_entries_are_dropped_without_a_system_python(monkeypatch) -> None:
    monkeypatch.setattr(process, "_system_python_exists", lambda: False)
    out = process.sanitize_game_env(
        {"PATH": ".:bin:/opt/tools:../x:/home/u/.pyenv/shims:/usr/bin"}
    )
    entries = out["PATH"].split(":")
    assert all(entry.startswith("/") for entry in entries), entries
    assert "." not in entries and "bin" not in entries and "../x" not in entries
    # Without a system python the shim is the only python3 the child may find.
    assert "/home/u/.pyenv/shims" in entries


# ---------------------------------------------------------------------------
# #596: ensure_managed must not accept a zipapp its shebang cannot execute
# ---------------------------------------------------------------------------
def test_ensure_managed_refuses_without_the_shebang_python(
    monkeypatch, tmp_path: Path
) -> None:
    monkeypatch.setattr(umu, "managed_path", lambda: tmp_path / "umu-run")
    monkeypatch.setattr(umu, "SHEBANG_PYTHON", str(tmp_path / "no-such-python3"))
    monkeypatch.setattr(
        umu.shutil, "which", lambda name, *a, **k: "/usr/bin/python3"
    )
    monkeypatch.setattr(umu.download, "download", lambda *a, **k: pytest.fail("downloaded"))

    with pytest.raises(umu.UmuError, match="python3"):
        umu.ensure_managed()
    assert not (tmp_path / "umu-run").exists()


# ---------------------------------------------------------------------------
# #381 F-06: output from a process that outlives the leader must not block
# ---------------------------------------------------------------------------
def test_run_keeps_draining_after_the_leader_exits(tmp_path: Path) -> None:
    marker = tmp_path / "marker"
    pidfile = tmp_path / "grandchild.pid"
    script = (
        "( sleep 2; yes x | head -c 500000; : > " + str(marker) + " ) &\n"
        "echo $! > " + str(pidfile) + "\n"
        "exit 0\n"
    )
    try:
        process.run(["sh", "-c", script], capture=False, stream=False)
        deadline = time.monotonic() + 10
        while time.monotonic() < deadline and not marker.exists():
            time.sleep(0.05)
        assert marker.exists(), "a descendant blocked writing to the abandoned stdout pipe"
        deadline = time.monotonic() + 5
        while time.monotonic() < deadline and [
            t for t in threading.enumerate() if t.name == "nadare-run-reader"
        ]:
            time.sleep(0.05)
    finally:
        try:
            os.kill(int(pidfile.read_text()), signal.SIGKILL)
        except (OSError, ValueError):
            pass
    assert not [t for t in threading.enumerate() if t.name == "nadare-run-reader"]


# ---------------------------------------------------------------------------
# #94: a hybrid iGPU + dGPU resolves to the DXVK-capable device
# ---------------------------------------------------------------------------
_HYBRID = """\
Vulkan Instance Version: 1.3.275
GPU0:
\tapiVersion = 1.2.170
\tdeviceType = PHYSICAL_DEVICE_TYPE_INTEGRATED_GPU
\tdeviceName = Intel Iris Xe
\tdriverName = Intel
GPU1:
\tapiVersion = 1.3.280
\tdeviceType = PHYSICAL_DEVICE_TYPE_DISCRETE_GPU
\tdeviceName = NVIDIA GeForce RTX 3060
\tdriverName = NVIDIA
"""

_HYBRID_SAME_VERSION = """\
GPU0:
\tapiVersion = 1.3.280
\tdeviceType = PHYSICAL_DEVICE_TYPE_INTEGRATED_GPU
\tdeviceName = Intel Iris Xe
\tdriverName = Intel
GPU1:
\tapiVersion = 1.3.280
\tdeviceType = PHYSICAL_DEVICE_TYPE_DISCRETE_GPU
\tdeviceName = NVIDIA GeForce RTX 3060
\tdriverName = NVIDIA
"""


def test_hybrid_gpu_prefers_the_higher_api_version() -> None:
    info = graphics.parse_vulkan_summary(_HYBRID)
    assert info.device == "NVIDIA GeForce RTX 3060"
    assert info.api_version == "1.3.280"


def test_hybrid_gpu_prefers_the_discrete_device_on_a_tie() -> None:
    info = graphics.parse_vulkan_summary(_HYBRID_SAME_VERSION)
    assert info.device == "NVIDIA GeForce RTX 3060"


# ---------------------------------------------------------------------------
# #670: the launch log names the API the game was asked to use
# ---------------------------------------------------------------------------
def test_runtime_note_names_the_requested_renderer(tmp_path: Path, monkeypatch) -> None:
    wine = write_exe(tmp_path / "wine")
    ctx = make_context(
        tmp_path, "wine", {"wine_binary": str(wine), "renderer": "opengl"}
    )
    backend = WineGameBackend(ctx)
    monkeypatch.setattr(backend, "resolve_runner", lambda: (None, "wine"))
    note = backend._runtime_note()
    assert "renderer:" in note and "OpenGL" in note


def test_runtime_note_says_when_auto_chose_vulkan(tmp_path: Path, monkeypatch) -> None:
    wine = write_exe(tmp_path / "wine")
    ctx = make_context(tmp_path, "wine", {"wine_binary": str(wine)})
    backend = WineGameBackend(ctx)
    monkeypatch.setattr(backend, "resolve_runner", lambda: (None, "wine"))
    monkeypatch.setattr(graphics, "dxvk_ok", lambda: True)
    note = backend._runtime_note()
    assert "Vulkan" in note and "auto" in note


# ---------------------------------------------------------------------------
# #119: PCSDK stub hardening items
# ---------------------------------------------------------------------------
_STUB = (
    Path(__file__).resolve().parents[1]
    / "src/nadare/backends/czn/stub_src/czn_pcsdk_stub.c"
)


def _stub_text() -> str:
    return _STUB.read_text(encoding="utf-8")







