# SPDX-License-Identifier: GPL-3.0-or-later
"""A game the user added (generic Wine/Proton) with the Sandbox on, inside the Flatpak.

Found on Ubuntu 24.04 and Mint 22.3 VMs with the Flatpak test build (2026-11-14): the generic
backend always resolved umu (#1340), even for a Sandboxed launch in the Flatpak, and handed
``.../data/nadare/umu/1.4.4/umu-run`` to ``flatpak-spawn --sandbox``, where that file does not
exist ("execvp ... No such file or directory"). The catalog games already run Proton directly
there (winegame launching: ``if not (flatpak and sandboxed)``); the generic one now does too.
"""

from __future__ import annotations

import pytest
from _backend_helpers import make_context, write_exe

from nadare.backends import wine as generic_wine
from nadare.core import gamesandbox, umu, userns


def _backend(tmp_path, monkeypatch, **profile):
    game = tmp_path / "Games" / "Probe"
    exe = write_exe(game / "probe.exe", "MZ")
    proton = tmp_path / "Proton"
    proton.mkdir(exist_ok=True)
    (proton / "proton").write_text("")
    ctx = make_context(tmp_path, "wine", {"prefix": str(tmp_path / "pfx"),
                                          "install_dir": str(game), "exe_name": str(exe),
                                          "runner": "MyProton", **profile})
    backend = generic_wine.WineBackend(ctx)
    monkeypatch.setattr(backend, "_runner_for_build", lambda runner: proton)
    monkeypatch.setattr(generic_wine.sandbox, "in_flatpak", lambda: True)
    monkeypatch.setattr(userns, "restricted", lambda: False)
    return backend, proton


def test_flatpak_sandboxed_launch_runs_proton_directly(tmp_path, monkeypatch):
    backend, proton = _backend(tmp_path, monkeypatch, secure_mode=True)
    monkeypatch.setattr(umu, "resolve", lambda *a, **k: pytest.fail("no umu in its Sandbox"))
    monkeypatch.setattr(gamesandbox, "_flatpak_spawn", lambda: "/usr/bin/flatpak-spawn")
    argv, _env, _prefix, _target = backend.build()
    inner = argv[argv.index("--") + 1:] if "--" in argv else argv
    assert str(proton / "proton") in argv
    assert not any(part.endswith("umu-run") for part in argv)
    assert "flatpak-spawn" in " ".join(argv) and inner


def test_flatpak_without_the_sandbox_still_uses_umu(tmp_path, monkeypatch):
    backend, _proton = _backend(tmp_path, monkeypatch, secure_mode=False)
    monkeypatch.setattr(umu, "resolve", lambda *a, **k: "/app-data/umu-run")
    argv, *_ = backend.build()
    assert argv[0] == "/app-data/umu-run"


def test_setup_steps_in_the_flatpak_still_use_umu(tmp_path, monkeypatch):
    """vcredist and other prefix steps are not Sandboxed: they keep the umu route."""
    backend, _proton = _backend(tmp_path, monkeypatch, secure_mode=True)
    monkeypatch.setattr(umu, "resolve", lambda *a, **k: "/app-data/umu-run")
    argv, _env = backend._prefix_command(["vc_redist.x64.exe"])
    assert argv[0] == "/app-data/umu-run"


def test_outside_the_flatpak_a_sandboxed_launch_keeps_umu(tmp_path, monkeypatch):
    backend, _proton = _backend(tmp_path, monkeypatch, secure_mode=True)
    monkeypatch.setattr(generic_wine.sandbox, "in_flatpak", lambda: False)
    monkeypatch.setattr(umu, "resolve", lambda *a, **k: "/usr/bin/umu-run")
    argv, _env = backend._prefix_command(["probe.exe"], sandboxed=True)
    assert argv[0] == "/usr/bin/umu-run"
