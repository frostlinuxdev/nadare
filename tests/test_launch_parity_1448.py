# SPDX-License-Identifier: GPL-3.0-or-later
"""Every kind that plays from a Wine prefix builds its Proton command the same way (#1448).

The command was built in three places (the shared WineGame path, CZN's own copy, the generic
``wine`` kind) and launch fixes landed in one of them only: CZN kept taking a system umu-run
first and skipped the kept locales, the gamepad-scan skip and the FSR4 setup (#1449). This test
builds Play's command for every such kind in a faked host and checks the shared rules, so the
next fix that lands in one path only fails here.

A new backend kind must be put in ``PREFIX_KINDS`` or ``NO_PREFIX_KINDS`` (with the reason).
"""

from __future__ import annotations

from pathlib import Path

import pytest
from _backend_helpers import make_context

from nadare.core import backends, directkinds, fsr4, pvlocales, sandbox, umu, userns, winebus
from nadare.core.backends import BackendError

#: Kinds with no Wine prefix of their own, so no Proton command to build.
NO_PREFIX_KINDS = {
    "steam": "Steam starts the game; Nadare only asks it to",
    "script": "the user's own script decides how it runs",
    "fgo-lepton": "an Android container, not Wine",
}

PREFIX_KINDS = sorted(
    {
        *directkinds.DIRECT_KINDS,
        *directkinds.LAUNCHER_KINDS,
        "arknights-yostar",
        "azurpromilia-jp-yostar",
        "hbr-yostar",
        "stellasora-yostar",
        "stove-czn",
        "wine",
    }
)

OWN_UMU = "/own/nadare/umu-run"
SYSTEM_UMU = "/usr/bin/umu-run"


def test_every_kind_is_classified():
    backends.load_builtins()
    known = set(backends.kinds()) - set(directkinds.RETIRED_KINDS) - set(backends.unavailable())
    assert known == set(PREFIX_KINDS) | set(NO_PREFIX_KINDS)


@pytest.fixture
def host(monkeypatch):
    """A desktop host: Nadare's own umu and a system umu-run, no Flatpak, nothing blocked."""
    state = {"resolve": OWN_UMU, "winebus": [], "userns_blocked": False}
    monkeypatch.setattr(sandbox, "in_flatpak", lambda: False)
    monkeypatch.setattr(umu, "system", lambda *a, **k: SYSTEM_UMU)
    monkeypatch.setattr(umu, "resolve", lambda *a, **k: state["resolve"])
    monkeypatch.setattr(umu, "note_first_runtime", lambda emit: None)
    monkeypatch.setattr(userns, "blocks_runtime", lambda *a, **k: state["userns_blocked"])
    monkeypatch.setattr(
        pvlocales, "prepare", lambda env, cache, **kw: {"PARITY_LOCALES_KEPT": "1"}
    )
    monkeypatch.setattr(fsr4, "upgrade_env", lambda env: {"PROTON_FSR4_UPGRADE": "0"})

    def _winebus(prefix, env):
        state["winebus"].append(Path(prefix))
        return None

    monkeypatch.setattr(winebus, "prepare", _winebus)
    return state


def _backend(kind: str, tmp_path: Path, monkeypatch, profile: dict | None = None):
    prefix = tmp_path / "prefix"
    prefix.mkdir(exist_ok=True)
    runner = tmp_path / "runner"
    runner.mkdir(exist_ok=True)
    (runner / "proton").write_text("#!/bin/sh\n")
    values = {"prefix": str(prefix), **(profile or {})}
    if kind == "wine":
        values.setdefault("runner", "dwproton")
    backend = backends.get(kind)(make_context(tmp_path, kind, values))
    if kind == "wine":
        monkeypatch.setattr(backend, "_runner_for_build", lambda rid: runner)
    else:
        monkeypatch.setattr(backend, "resolve_runner", lambda: (str(runner), "proton"))
    return backend, prefix


def _build(backend, extra_env=None):
    if extra_env is None:
        return backend._prefix_command(["game.exe"])
    return backend._prefix_command(["game.exe"], extra_env)


@pytest.mark.parametrize("kind", PREFIX_KINDS)
def test_proton_goes_through_nadare_s_own_umu(kind, tmp_path, monkeypatch, host):
    backend, prefix = _backend(kind, tmp_path, monkeypatch)
    argv, env = _build(backend)
    assert argv[0] == OWN_UMU, f"{kind} did not take umu.resolve's answer (own copy first)"
    assert argv[-1] == "game.exe"
    assert env["WINEPREFIX"] == str(prefix)


@pytest.mark.parametrize("kind", PREFIX_KINDS)
def test_without_umu_a_desktop_play_refuses(kind, tmp_path, monkeypatch, host):
    """No bare ``proton run`` outside the Flatpak (#1338): it skips the runtime and the fixes."""
    host["resolve"] = None
    backend, _prefix = _backend(kind, tmp_path, monkeypatch)
    with pytest.raises(BackendError):
        _build(backend)


@pytest.mark.parametrize("kind", PREFIX_KINDS)
def test_a_blocked_user_namespace_is_refused_before_launch(kind, tmp_path, monkeypatch, host):
    host["userns_blocked"] = True
    backend, _prefix = _backend(kind, tmp_path, monkeypatch)
    with pytest.raises(BackendError):
        _build(backend)


@pytest.mark.parametrize("kind", PREFIX_KINDS)
def test_play_speed_ups_apply(kind, tmp_path, monkeypatch, host):
    backend, prefix = _backend(kind, tmp_path, monkeypatch)
    _argv, env = _build(backend)
    assert env.get("PARITY_LOCALES_KEPT") == "1", f"{kind}: kept locales missing"
    assert env.get("PROTON_FSR4_UPGRADE") == "0", f"{kind}: FSR4 setup missing"
    assert host["winebus"] == [prefix], f"{kind}: gamepad-scan skip missing"


@pytest.mark.parametrize("kind", PREFIX_KINDS)
def test_the_profile_cannot_redirect_the_prefix(kind, tmp_path, monkeypatch, host):
    backend, prefix = _backend(
        kind,
        tmp_path,
        monkeypatch,
        {"extra_env": {"WINEPREFIX": "/elsewhere", "PROTONPATH": "/evil", "PARITY_OK": "1"}},
    )
    _argv, env = _build(backend)
    assert env["WINEPREFIX"] == str(prefix)
    assert env["PROTONPATH"] == str(tmp_path / "runner")
    assert env["PARITY_OK"] == "1"


@pytest.mark.parametrize("kind", [k for k in PREFIX_KINDS if k != "wine"])
def test_a_caller_cannot_redirect_the_prefix(kind, tmp_path, monkeypatch, host):
    backend, prefix = _backend(kind, tmp_path, monkeypatch)
    _argv, env = _build(backend, {"WINEPREFIX": "/elsewhere", "PARITY_CALLER": "1"})
    assert env["WINEPREFIX"] == str(prefix)
    assert env["PARITY_CALLER"] == "1"


#: The generic ``wine`` kind has no Display setting (its launchers want native Wayland).
@pytest.mark.parametrize("kind", [k for k in PREFIX_KINDS if k != "wine"])
def test_the_display_choice_wins_over_the_desktop(kind, tmp_path, monkeypatch, host):
    monkeypatch.setenv("PROTON_ENABLE_WAYLAND", "1")
    backend, _prefix = _backend(kind, tmp_path, monkeypatch, {"display": "x11"})
    _argv, env = _build(backend)
    assert env["PROTON_ENABLE_WAYLAND"] == "0", f"{kind} ignored Display: X11"
