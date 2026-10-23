# SPDX-License-Identifier: GPL-3.0-or-later
"""Regression guards for the small2 sweep: process ownership and doctor --strict."""

from __future__ import annotations

import os
from pathlib import Path

from nadare import app
from nadare.backends import steam as steam_backend
from nadare.backends import wine
from nadare.backends.wine import ProcessQuery
from nadare.core import backends, graphics, hostlibs, process, runners, sandbox, secrets, userns


def _fake_pid(root: Path, pid: int, argv: list[str], env: dict[str, str] | None = None) -> None:
    entry = root / str(pid)
    entry.mkdir(parents=True)
    (entry / "cmdline").write_bytes(b"\0".join(a.encode() for a in argv) + b"\0")
    if env is not None:
        blob = b"".join(f"{k}={v}".encode() + b"\0" for k, v in env.items())
        (entry / "environ").write_bytes(blob)


# ---- #29: never match a process owned by another user ----------------------
def test_scan_query_skips_a_process_owned_by_another_user(tmp_path, monkeypatch) -> None:
    root = tmp_path / "proc"
    _fake_pid(root, 801, ["C:/games/game.exe"])
    query = ProcessQuery(names=("game.exe",))

    assert wine.scan_pids(query, proc_root=root) == [801]

    real_uid = os.getuid()
    monkeypatch.setattr(os, "getuid", lambda: real_uid + 1)
    assert wine.scan_pids(query, proc_root=root) == []


def test_applaunch_pids_skip_a_process_owned_by_another_user(tmp_path, monkeypatch) -> None:
    root = tmp_path / "proc"
    _fake_pid(root, 802, ["/usr/bin/steam", "-applaunch", "730"])

    assert steam_backend._applaunch_pids("730", proc_root=root) == [802]

    real_uid = os.getuid()
    monkeypatch.setattr(os, "getuid", lambda: real_uid + 1)
    assert steam_backend._applaunch_pids("730", proc_root=root) == []


# ---- #35: doctor --strict exits non-zero when the host is not ready ---------
def _ready_host(monkeypatch) -> None:
    monkeypatch.setattr(
        graphics, "vulkan_info", lambda **k: graphics.VulkanInfo(True, "1.3.200")
    )
    monkeypatch.setattr(graphics, "dxvk_ok", lambda: True)
    monkeypatch.setattr(process, "which", lambda name, **k: "/usr/bin/umu-run")
    monkeypatch.setattr(hostlibs, "missing_32bit", lambda: [])
    monkeypatch.setattr(sandbox, "in_flatpak", lambda: False)
    monkeypatch.setattr(backends, "unavailable", list)
    monkeypatch.setattr(backends, "kinds", lambda: ["wine"])
    monkeypatch.setattr(runners, "all_runners", list)
    monkeypatch.setattr(secrets, "keyring_diagnosis", lambda: (True, "test"))


def _not_ready(monkeypatch) -> None:
    _ready_host(monkeypatch)
    monkeypatch.setattr(graphics, "vulkan_info", lambda **k: graphics.VulkanInfo(False))


def test_doctor_strict_flag_is_parsed() -> None:
    parser = app.build_parser()
    assert parser.parse_args(["doctor"]).strict is False
    assert parser.parse_args(["doctor", "--strict"]).strict is True


def test_doctor_strict_passes_on_a_ready_host(monkeypatch, capsys) -> None:
    _ready_host(monkeypatch)

    assert app.run_cli(["doctor", "--strict"]) == 0
    assert "doctor: not ready" not in capsys.readouterr().out


def test_doctor_strict_fails_when_vulkan_is_unavailable(monkeypatch, capsys) -> None:
    _not_ready(monkeypatch)

    assert app.run_cli(["doctor", "--strict"]) == 1
    out = capsys.readouterr().out
    assert "doctor: not ready (Vulkan unavailable)" in out


def test_doctor_without_strict_still_exits_zero(monkeypatch, capsys) -> None:
    _not_ready(monkeypatch)

    assert app.run_cli(["doctor"]) == 0
    assert "doctor: not ready" not in capsys.readouterr().out


def test_doctor_strict_fails_when_no_backend_loaded(monkeypatch, capsys) -> None:
    _ready_host(monkeypatch)
    monkeypatch.setattr(backends, "unavailable", lambda: ["wine"])
    monkeypatch.setattr(backends, "kinds", lambda: ["wine"])

    assert app.run_cli(["doctor", "--strict"]) == 1
    out = capsys.readouterr().out
    assert "backends: 0/1 loaded" in out
    assert "doctor: not ready (no backends loaded)" in out


def test_doctor_strict_fails_when_libs_are_missing_and_the_runtime_is_blocked(
    monkeypatch, capsys
) -> None:
    _ready_host(monkeypatch)
    monkeypatch.setattr(process, "which", lambda name, **k: None)
    monkeypatch.setattr(hostlibs, "missing_32bit", lambda: ["libGL.so.1"])
    monkeypatch.setattr(userns, "restricted", lambda *a, **k: True)
    monkeypatch.setattr(userns, "profile_installed", lambda *a, **k: False)

    assert app.run_cli(["doctor", "--strict"]) == 1
    out = capsys.readouterr().out
    assert "32-bit libs: missing libGL.so.1" in out
    assert "doctor: not ready (32-bit libs missing and the runtime sandbox is blocked)" in out


def test_doctor_strict_passes_when_libs_are_missing_but_the_runtime_works(
    monkeypatch, capsys
) -> None:
    # The managed runtime brings its own 32-bit libraries.
    _ready_host(monkeypatch)
    monkeypatch.setattr(process, "which", lambda name, **k: None)
    monkeypatch.setattr(hostlibs, "missing_32bit", lambda: ["libGL.so.1"])
    monkeypatch.setattr(userns, "restricted", lambda *a, **k: False)

    assert app.run_cli(["doctor", "--strict"]) == 0
    out = capsys.readouterr().out
    assert "32-bit libs: missing libGL.so.1" in out
    assert "not needed while Nadare's own runtime works" in out
    assert "not ready" not in out
