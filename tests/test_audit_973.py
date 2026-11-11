# SPDX-License-Identifier: GPL-3.0-or-later
"""#973: a running batch script reads as running; the generic Wine game uses a managed umu.

1. A ``.bat`` profile launches and stops through Wine, but ``status()`` looked only for a shell
   running the script, so a running batch game read as installed (wrong Play state, no Stop).
2. ``status()`` of the generic Wine game accepts an installed managed umu, but the launch used it
   only when 32-bit host libraries were missing; with complete libraries Play failed with
   "umu-run not found" although status said ready.
"""

from __future__ import annotations

from pathlib import Path

import pytest
from _backend_helpers import make_context, write_exe

from nadare.backends import script as script_mod
from nadare.backends import wine as wine_mod
from nadare.backends.script import ScriptBackend
from nadare.backends.wine import WineBackend
from nadare.core import umu


# ---- 1. batch status ----------------------------------------------------------------------
def _batch_backend(tmp_path: Path) -> ScriptBackend:
    write_exe(tmp_path / "game.bat", "@echo off\n")
    return ScriptBackend(
        make_context(
            tmp_path,
            "script",
            {"launch_exe": "game.bat", "install_dir": str(tmp_path), "prefix": str(tmp_path / "p")},
        )
    )


def test_a_running_batch_game_reads_as_running(tmp_path, monkeypatch):
    backend = _batch_backend(tmp_path)
    seen = []

    def scan(query):
        seen.append(query)
        # Only the Wine-side query (what _BatchWine.stop uses) finds the game.
        return [4242] if not query.arg_runners or "bash" not in query.arg_runners else []

    monkeypatch.setattr(script_mod, "scan_pids", scan)
    status = backend.status()
    assert status.state == "running" and status.running
    assert status.data["batch"] is True and status.data["pids"] == [4242]


def test_a_batch_game_uses_the_same_query_as_stop(tmp_path, monkeypatch):
    backend = _batch_backend(tmp_path)
    queries = []
    monkeypatch.setattr(script_mod, "scan_pids", lambda q: queries.append(q) or [])
    backend.status()
    delegate = script_mod._BatchWine(backend.ctx, tmp_path / "game.bat")
    assert queries == [delegate._patterns()]


def test_a_shell_script_still_uses_the_shell_query(tmp_path, monkeypatch):
    write_exe(tmp_path / "run.sh", "#!/bin/sh\n")
    backend = ScriptBackend(
        make_context(tmp_path, "script", {"launch_exe": "run.sh", "install_dir": str(tmp_path)})
    )
    queries = []
    monkeypatch.setattr(script_mod, "scan_pids", lambda q: queries.append(q) or [])
    status = backend.status()
    assert status.state == "installed"
    assert "bash" in queries[0].arg_runners


# ---- 2. managed umu with complete host libraries --------------------------------------------
def _wine_backend(tmp_path: Path, monkeypatch) -> tuple[WineBackend, Path]:
    proton = tmp_path / "Proton"
    proton.mkdir()
    (proton / "proton").write_text("")
    game = tmp_path / "game"
    write_exe(game / "game.exe", "MZ")
    backend = WineBackend(
        make_context(
            tmp_path,
            "wine",
            {
                "install_dir": str(game),
                "exe_name": "game.exe",
                "prefix": str(tmp_path / "prefix"),
                "proton_runner": str(proton),
            },
        )
    )
    monkeypatch.setattr(backend, "_runner_path", lambda runner: proton)
    monkeypatch.setattr(wine_mod.shutil, "which", lambda name, *a, **k: None)
    monkeypatch.setattr(wine_mod.sandbox, "in_flatpak", lambda: False)
    monkeypatch.setattr(wine_mod.hostlibs, "missing_32bit", lambda: [])  # complete libraries
    monkeypatch.setattr(umu, "ensure_managed", lambda **k: pytest.fail("must not download"))
    return backend, game / "game.exe"


def test_an_installed_managed_umu_is_used_with_complete_libraries(tmp_path, monkeypatch):
    backend, target = _wine_backend(tmp_path, monkeypatch)
    managed = write_exe(tmp_path / "managed-umu", "#!/usr/bin/python3\n")
    monkeypatch.setattr(umu, "managed", lambda: managed)
    assert backend._runtime_error() is None
    argv, _env, _prefix, built_target = backend.build()
    assert argv == [str(managed), str(built_target)]


def test_without_any_umu_the_message_is_unchanged(tmp_path, monkeypatch):
    backend, _target = _wine_backend(tmp_path, monkeypatch)
    monkeypatch.setattr(umu, "managed", lambda: None)
    assert "umu-run not found" in (backend._runtime_error() or "")
    with pytest.raises(Exception, match="umu-run not found"):
        backend.build()
