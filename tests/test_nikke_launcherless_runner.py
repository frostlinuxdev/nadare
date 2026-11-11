"""Tests for the launcherless NIKKE orchestrator (no Wine needed)."""

from __future__ import annotations

import importlib.util
import shutil
from pathlib import Path

import pytest

MODULE_PATH = (
    Path(__file__).resolve().parents[1]
    / "scripts"
    / "nikke_launcherless"
    / "launcherless.py"
)
_spec = importlib.util.spec_from_file_location("nikke_launcherless", MODULE_PATH)
assert _spec and _spec.loader
launcherless = importlib.util.module_from_spec(_spec)
_spec.loader.exec_module(launcherless)


def test_intl_command_passes_service_out_and_parent():
    cmd = launcherless.intl_command(
        "intlsvc_host.exe",
        r"C:\NIKKE\Launcher\intl_service\intl_service.exe",
        r"S:\tmp\session.enc.json",
    )
    assert cmd[0] == "intlsvc_host.exe"
    assert "--service" in cmd
    assert r"C:\NIKKE\Launcher\intl_service\intl_service.exe" in cmd
    assert "--out" in cmd
    assert r"S:\tmp\session.enc.json" in cmd
    assert cmd[cmd.index("--parent-name") + 1] == "nikke_launcher"
    assert cmd[cmd.index("--game-id") + 1] == "29080"


def test_sail_command_uses_session_and_launch_triplet():
    cmd = launcherless.sail_command(
        "sail_host.exe",
        r"S:\tmp\session.json",
        r"S:\NIKKE\NIKKE\game\nikke.exe",
        r"S:\NIKKE\NIKKE\game",
        ["-timestamps"],
    )
    assert cmd[:2] == ["sail_host.exe", r"S:\tmp\session.json"]
    assert cmd[2] == "--launch"
    assert cmd[3:6] == [
        r"S:\NIKKE\NIKKE\game\nikke.exe",
        r"S:\NIKKE\NIKKE\game",
        "-timestamps",
    ]


def test_host_env_sets_prefix_display_and_launcher_dir():
    env = launcherless.host_env(
        "/tmp/prefix",
        display=":98",
        launcher_dir=r"C:\NIKKE\Launcher",
        log=r"S:\tmp\sail_host.log",
        dll_overrides="d3d11=n,b",
        base={"KEEP": "1"},
    )
    assert env["WINEPREFIX"] == "/tmp/prefix"
    assert env["DISPLAY"] == ":98"
    assert env["SAIL_LAUNCHER_DIR"] == r"C:\NIKKE\Launcher"
    assert env["SAIL_HOST_LOG"] == r"S:\tmp\sail_host.log"
    assert env["WINEDLLOVERRIDES"] == "d3d11=n,b"
    assert env["KEEP"] == "1"


def test_run_launcherless_rejects_missing_hosts(tmp_path):
    with pytest.raises(FileNotFoundError):
        launcherless.run_launcherless(
            wine="true",
            prefix=tmp_path,
            hosts_dir=tmp_path,
            service="service.exe",
            game_exe="game.exe",
            game_cwd=".",
        )

def test_intl_command_carries_login_mode_and_channel():
    cmd = launcherless.intl_command(
        "intlsvc_host.exe",
        "service.exe",
        "Z:\\tmp\\session.enc.json",
        login_channel=6,
        auto_login=0,
    )
    assert cmd[cmd.index("--login-channel") + 1] == "6"
    assert cmd[cmd.index("--auto-login") + 1] == "0"


def test_cache_roundtrip_is_private(tmp_path):
    src = tmp_path / "src"
    (src / "mmkv").mkdir(parents=True)
    (src / "intl_auth.txt").write_bytes(b"secret-session")
    (src / "mmkv" / "mmkv.default").write_bytes(b"device-ids")
    cache = tmp_path / "cache"

    launcherless.save_cache(src, cache)
    assert launcherless.has_cache(cache)
    assert (cache / "intl_auth.txt").stat().st_mode & 0o777 == 0o600
    assert (cache / "mmkv" / "mmkv.default").stat().st_mode & 0o777 == 0o600

    shutil.rmtree(src)
    dst = tmp_path / "dst"
    launcherless.restore_cache(cache, dst)
    assert (dst / "intl_auth.txt").read_bytes() == b"secret-session"
    assert (dst / "mmkv" / "mmkv.default").read_bytes() == b"device-ids"


def test_save_cache_requires_auth_file(tmp_path):
    src = tmp_path / "src"
    src.mkdir()
    with pytest.raises(RuntimeError, match="intl_auth"):
        launcherless.save_cache(src, tmp_path / "cache")
