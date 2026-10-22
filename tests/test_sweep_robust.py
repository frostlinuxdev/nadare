# SPDX-License-Identifier: GPL-3.0-or-later
"""Robustness sweep: GUI profile hand-off, safe watchdogs and clear errors."""

from __future__ import annotations

import logging
import os
from pathlib import Path

import pytest
from _backend_helpers import make_context

from nadare import app
from nadare.backends import arknights as ak
from nadare.backends import steam as steam_backend
from nadare.backends import wine
from nadare.backends.arknights import ArknightsBackend
from nadare.backends.fgo.backend import FgoLeptonBackend
from nadare.backends.stellasora import StellaSoraBackend
from nadare.core import process, vcredist
from nadare.core.backends import BackendError
from nadare.core.config import Config


def _config(tmp_path: Path, *names: str) -> tuple[Config, Path]:
    path = tmp_path / "config.json"
    cfg = Config.load(path)
    for name in names:
        cfg.add_profile(name, backend="wine", title=name.title())
    cfg.save()
    return cfg, path


# ---- #240: `nadare gui -p` opens that profile -------------------------------
def test_gui_unknown_profile_fails_before_the_window_is_created(
    tmp_path, monkeypatch, capsys
) -> None:
    _, path = _config(tmp_path, "game")

    def unexpected(*_args, **_kwargs):
        pytest.fail("the GUI window must not be created for an unknown profile")

    monkeypatch.setattr(app, "run_gui", unexpected)

    assert app.run_cli(["-c", str(path), "gui", "-p", "missing"]) == 2
    err = capsys.readouterr().err
    assert "no such profile: missing" in err


def test_gui_passes_the_selected_profile_to_the_window(tmp_path, monkeypatch) -> None:
    _, path = _config(tmp_path, "game")
    seen: dict[str, object] = {}

    def fake_run_gui(config, *, profile=None):
        seen["profile"] = profile
        return 0

    monkeypatch.setattr(app, "run_gui", fake_run_gui)

    assert app.run_cli(["-c", str(path), "gui", "-p", "game"]) == 0
    assert seen["profile"] == "game"


# ---- #245: the VC++ watchdog only stops its own installer -------------------
def _fake_process(
    proc: Path, pid: int, cmdline: bytes, *, ppid: int, pgrp: int | None = None
) -> None:
    entry = proc / str(pid)
    entry.mkdir(parents=True)
    (entry / "cmdline").write_bytes(cmdline)
    # /proc/<pid>/stat: pid (comm) state ppid pgrp ...
    (entry / "stat").write_text(f"{pid} (test) S {ppid} {pgrp or pid} 0 0\n")


def test_vcredist_watchdog_ignores_another_process_watch_installer(tmp_path, monkeypatch):
    proc = tmp_path / "proc"
    cmd = b"wine\0/x/vc_redist.x64.exe\0/install\0/quiet\0/norestart\0"
    _fake_process(proc, 500, b"python3\0nadare\0", ppid=1)
    _fake_process(proc, 101, cmd, ppid=500, pgrp=101)
    _fake_process(proc, 900, b"python3\0other-nadare\0", ppid=1)
    _fake_process(proc, 103, cmd, ppid=900, pgrp=103)
    _fake_process(proc, 102, b"vim\0notes.txt\0", ppid=500)

    groups: list[tuple[int, int]] = []
    monkeypatch.setattr(os, "killpg", lambda group, sig: groups.append((group, sig)))
    monkeypatch.setattr(vcredist.time, "sleep", lambda _seconds: None)

    assert vcredist._stop_installer(str(proc), root_pid=500) == 1
    assert {group for group, _sig in groups} == {101}


# ---- #248: Steam -applaunch PIDs use the PID-recycle guard -----------------
def test_steam_applaunch_pid_with_a_changed_start_time_is_not_signalled(
    tmp_path, monkeypatch
) -> None:
    proc = tmp_path / "proc"
    pid = 4242
    entry = proc / str(pid)
    entry.mkdir(parents=True)
    (entry / "cmdline").write_bytes(b"steam\0-applaunch\0" + b"12345\0")

    monkeypatch.setattr(process, "_pid_starttime", lambda _pid: "100")
    assert steam_backend._applaunch_pids("12345", proc_root=proc) == [pid]

    # Same PID, different start time: it was recycled onto a bystander.
    monkeypatch.setattr(process, "_pid_starttime", lambda _pid: "200")
    signalled: list[int] = []
    monkeypatch.setattr(os, "kill", lambda target, _sig: signalled.append(target))
    wine.stop_pids([pid])
    assert signalled == []


# ---- #358: FGO's launch error is actionable, the path goes to the log ------
def test_fgo_launch_error_names_install_and_logs_the_prefix(tmp_path, caplog) -> None:
    prefix = tmp_path / "lepton-nv"
    backend = FgoLeptonBackend(
        make_context(
            tmp_path,
            "fgo-lepton",
            {"prefix": str(prefix), "accept_account_notice": True},
        )
    )
    with caplog.at_level(logging.INFO, logger="nadare.backends.fgo.backend"):
        with pytest.raises(BackendError) as excinfo:
            backend.launch()

    assert excinfo.value.message == (
        "FGO is not installed for this profile. Press Install to set it up again."
    )
    assert any("lepton-nv" in record.getMessage() for record in caplog.records)


# ---- #360: malformed Yostar manifests keep schema words out of the dialog --
def _broken_manifest(tmp_path: Path) -> Path:
    path = tmp_path / "manifest.json"
    path.write_text('{"version": "1.0", "files": []}', encoding="utf-8")
    return path


@pytest.mark.parametrize(
    ("backend_cls", "kind", "display"),
    [
        (ArknightsBackend, ak.KIND, "Arknights"),
        (StellaSoraBackend, StellaSoraBackend.kind, "Stella Sora"),
    ],
)
def test_malformed_manifest_is_logged_and_replaced(
    tmp_path, caplog, backend_cls, kind, display
) -> None:
    backend = backend_cls(
        make_context(tmp_path, kind, {"manifest_file": str(_broken_manifest(tmp_path))})
    )
    with caplog.at_level(logging.WARNING, logger="nadare.backends.arknights"):
        with pytest.raises(BackendError) as excinfo:
            backend._load_manifest()

    assert excinfo.value.message == (
        f"{display}'s download information is invalid or incomplete. "
        "Nothing was changed. Try again later or update Nadare."
    )
    assert "manifest has no game_tag" in caplog.text
    assert "game_tag" not in excinfo.value.message
