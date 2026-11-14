# SPDX-License-Identifier: GPL-3.0-or-later
"""A Proton game never runs on a random plain Wine (#1338, flxpc 2026-11-14).

A dev update pinned DW-Proton 11.0-16, which the desktop did not have yet. Launcherless NIKKE
skipped the runner setup the normal Play path does, ``resolve_runner()`` (which never
downloads) answered "wine", and the plain-Wine branch took ``first_wine()``: an old Wine TkG
from another project, on the Proton prefix. Every launcherless NIKKE player would hit this the
first time a new DW-Proton is pinned.
"""

from __future__ import annotations

from types import SimpleNamespace

import pytest
from _backend_helpers import make_context

from nadare.backends import launcher
from nadare.core.backends import BackendError


def _nikke(tmp_path, profile=None):
    values = {"prefix": str(tmp_path / "pfx"), **(profile or {})}
    return launcher.NikkeBackend(make_context(tmp_path, "launcher-nikke", values))


def test_launcherless_play_sets_up_the_pinned_runner_first(tmp_path, monkeypatch):
    backend = _nikke(tmp_path)
    order: list[str] = []
    monkeypatch.setattr(backend, "_launcherless_wanted", lambda: True)
    monkeypatch.setattr(backend, "_client_missing_but_signed_in", lambda: False)
    monkeypatch.setattr(backend, "_launcherless_plan", lambda: SimpleNamespace())
    monkeypatch.setattr(backend, "_update_before_play", lambda: order.append("update"))
    monkeypatch.setattr(
        backend, "_ensure_default_runner", lambda *, strict: order.append(f"runner:{strict}")
    )
    monkeypatch.setattr(backend, "_launch_launcherless", lambda files: order.append("go") or 0)
    assert backend.launch() == 0
    assert "runner:True" in order and order.index("runner:True") < order.index("go")


def test_a_proton_game_without_its_runner_refuses_plain_wine(tmp_path, monkeypatch):
    backend = _nikke(tmp_path)
    monkeypatch.setattr(backend, "resolve_runner", lambda: (None, "wine"))
    used: list[str] = []
    monkeypatch.setattr(
        "nadare.backends.winegame_parts.launching.first_wine",
        lambda: used.append("first_wine") or "/legacy/wine-tkg/bin/wine",
        raising=False,
    )
    with pytest.raises(BackendError, match="Proton"):
        backend._prefix_command(["x.exe"])
    assert used == []


def test_an_explicit_wine_binary_still_runs_plain_wine(tmp_path, monkeypatch):
    """A Wine the player named on purpose (``wine_binary``) is still honoured."""
    wine = tmp_path / "mywine"
    wine.write_text("#!/bin/sh\n")
    wine.chmod(0o755)
    backend = _nikke(tmp_path, {"wine_binary": str(wine)})
    monkeypatch.setattr(backend, "resolve_runner", lambda: (None, "wine"))
    argv, _env = backend._prefix_command(["x.exe"])
    assert argv[0] == str(wine)
