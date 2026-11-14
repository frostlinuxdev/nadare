# SPDX-License-Identifier: GPL-3.0-or-later
"""The Sandbox's first start before umu's runtime exists (Ubuntu 24.04 VM, 2026-11-14).

Found on a fresh Ubuntu VM with the test build: the first Sandboxed Proton launch checked for a
bubblewrap BEFORE umu had downloaded its Steam Runtime (the runtime's ``srt-bwrap`` is the copy
the Sandbox uses). Two faults:

A. The first Sandbox start records the runtime copies it trusts. With none on disk it saved an
   EMPTY record (``{}``), and an empty record counted as "recorded": the copy umu downloaded a
   moment later was "not recorded" forever. With the Sandbox on by default the game then ran
   unsandboxed on every launch, with only a log line. A b23 player who ran Proton without umu
   (no runtime) can already have that ``{}`` file.
B. Nothing downloaded the runtime before the Sandbox looked for it. Now a Sandboxed launch
   through umu first lets umu set up its runtime and the prefix (``umu-run ""``, no game code)
   outside the Sandbox, like prefix setup already runs.
"""

from __future__ import annotations

import json
from pathlib import Path

import pytest
from _backend_helpers import make_context, write_exe

from nadare.backends import wine as generic_wine
from nadare.core import gamesandbox, umu, userns


def _copy(tmp_path: Path, name: str = "srt-bwrap") -> Path:
    copy = tmp_path / "rt" / name
    copy.parent.mkdir(parents=True, exist_ok=True)
    copy.write_text(f"#!/bin/sh\n# {name}\nexit 0\n")
    copy.chmod(0o755)
    return copy


@pytest.fixture
def copies(monkeypatch):
    on_disk: list[str] = []
    monkeypatch.setattr(gamesandbox, "runtime_copies", lambda: list(on_disk))
    monkeypatch.setattr(gamesandbox, "works", lambda binary=gamesandbox.BINARY: (True, ""))
    monkeypatch.setattr(gamesandbox.shutil, "which", lambda *a, **k: None)
    return on_disk


# ------------------------------------------------------------------ A: the record
def test_no_runtime_yet_records_nothing(tmp_path, copies):
    assert gamesandbox.trusted_runtime_copies() == []
    assert not gamesandbox.pins_path().exists()


def test_the_runtime_that_arrives_later_is_recorded_and_used(tmp_path, copies):
    assert gamesandbox.locate().binary is None  # first start: nothing on disk
    copy = _copy(tmp_path)
    copies.append(str(copy))  # umu downloaded its runtime
    located = gamesandbox.locate()
    assert located.binary == str(copy) and located.source == "runtime"
    assert list(json.loads(gamesandbox.pins_path().read_text())) == [str(copy)]


def test_an_empty_record_from_an_older_version_heals(tmp_path, copies):
    """A b23 machine without umu's runtime may already have saved ``{}``."""
    gamesandbox.pins_path().parent.mkdir(parents=True, exist_ok=True)
    gamesandbox.pins_path().write_text("{}")
    copy = _copy(tmp_path)
    copies.append(str(copy))
    assert gamesandbox.trusted_runtime_copies() == [str(copy)]


@pytest.mark.parametrize("damaged", ["not json", "[]", '"x"', "{\"a\": 1"])
def test_a_damaged_record_still_trusts_nothing(tmp_path, copies, damaged):
    gamesandbox.pins_path().parent.mkdir(parents=True, exist_ok=True)
    gamesandbox.pins_path().write_text(damaged)
    copies.append(str(_copy(tmp_path)))
    assert gamesandbox.trusted_runtime_copies() == []


def test_a_copy_that_appears_after_a_real_record_is_still_not_trusted(tmp_path, copies):
    """The #871 rule stays: once something was recorded, a newer copy is never run."""
    first = _copy(tmp_path, "srt-bwrap")
    copies.append(str(first))
    assert gamesandbox.trusted_runtime_copies() == [str(first)]
    planted = _copy(tmp_path / "other", "srt-bwrap")
    copies.insert(0, str(planted))
    assert gamesandbox.trusted_runtime_copies() == [str(first)]


def test_doctor_and_pages_still_record_nothing(tmp_path, copies):
    copies.append(str(_copy(tmp_path)))
    assert gamesandbox.recorded_runtime_copies() == []
    assert not gamesandbox.pins_path().exists()


# ------------------------------------------------------------------ B: runtime first
def _backend(tmp_path, monkeypatch, *, umu_run="/data/nadare/umu/1.4.4/umu-run", **profile):
    game = tmp_path / "games" / "Thing"
    exe = write_exe(game / "thing.exe", "MZ")
    proton = tmp_path / "Proton"
    proton.mkdir(exist_ok=True)
    (proton / "proton").write_text("")
    ctx = make_context(
        tmp_path,
        "wine",
        {"prefix": str(tmp_path / "pfx"), "install_dir": str(game), "exe_name": str(exe),
         "runner": "MyProton", **profile},
    )
    backend = generic_wine.WineBackend(ctx)
    monkeypatch.setattr(backend, "_runner_for_build", lambda runner: proton)
    monkeypatch.setattr(generic_wine.sandbox, "in_flatpak", lambda: False)
    monkeypatch.setattr(userns, "restricted", lambda: False)
    monkeypatch.setattr(umu, "resolve", lambda *a, **k: umu_run)
    return backend


@pytest.fixture
def ran(monkeypatch):
    calls: list[tuple[list[str], dict[str, str]]] = []

    def fake_stream(argv, env, on_line=None, cancel=None):
        calls.append((list(argv), dict(env)))
        return 0

    monkeypatch.setattr(generic_wine, "stream", fake_stream)
    return calls


def test_sandboxed_first_launch_lets_umu_set_up_its_runtime_first(
    tmp_path, monkeypatch, copies, ran
):
    backend = _backend(tmp_path, monkeypatch, secure_mode=True)
    monkeypatch.setattr(umu, "runtime_downloaded", lambda: False)

    def stream_then_runtime(argv, env, on_line=None, cancel=None):
        ran.append((list(argv), dict(env)))
        copies.append(str(_copy(tmp_path)))  # what umu-run "" leaves on disk
        return 0

    monkeypatch.setattr(generic_wine, "stream", stream_then_runtime)
    argv, env, _prefix, _target = backend.build()
    assert ran and ran[0][0] == ["/data/nadare/umu/1.4.4/umu-run", ""]
    assert ran[0][1]["WINEPREFIX"] == str(tmp_path / "pfx")
    assert ran[0][1]["PROTONPATH"]
    assert Path(argv[0]).name == "srt-bwrap"  # the Sandbox found the runtime's copy


def test_runtime_on_disk_runs_nothing_first(tmp_path, monkeypatch, copies, ran):
    copies.append(str(_copy(tmp_path)))
    monkeypatch.setattr(umu, "runtime_downloaded", lambda: True)
    _backend(tmp_path, monkeypatch, secure_mode=True).build()
    assert ran == []


def test_sandbox_off_runs_nothing_first(tmp_path, monkeypatch, copies, ran):
    monkeypatch.setattr(umu, "runtime_downloaded", lambda: False)
    _backend(tmp_path, monkeypatch, secure_mode=False).build()
    assert ran == []


def test_a_plain_wine_launch_runs_nothing_first(tmp_path, monkeypatch, copies, ran, has_bwrap):
    monkeypatch.setattr(umu, "runtime_downloaded", lambda: False)
    game = tmp_path / "games" / "Thing"
    exe = write_exe(game / "thing.exe", "MZ")
    ctx = make_context(tmp_path, "wine", {
        "prefix": str(tmp_path / "pfx"), "install_dir": str(game), "exe_name": str(exe),
        "wine_binary": "/usr/bin/wine", "secure_mode": True})
    generic_wine.WineBackend(ctx).build()
    assert ran == []


@pytest.fixture
def has_bwrap(monkeypatch):
    monkeypatch.setattr(gamesandbox.shutil, "which", lambda name, **_kw: "/usr/bin/bwrap")
    monkeypatch.setattr(gamesandbox, "works", lambda binary=gamesandbox.BINARY: (True, ""))


def test_a_failed_runtime_setup_still_reaches_the_normal_check(tmp_path, monkeypatch, copies):
    monkeypatch.setattr(umu, "runtime_downloaded", lambda: False)
    monkeypatch.setattr(generic_wine, "stream", lambda *a, **k: 1)
    backend = _backend(tmp_path, monkeypatch, secure_mode=True)
    with pytest.raises(generic_wine.BackendError, match="bubblewrap"):
        backend.build()  # explicit Sandbox, still no bubblewrap: the usual message


def test_the_catalog_games_take_the_same_first_step(tmp_path, monkeypatch, copies):
    """The direct games (PGR, WuWa, HSR...; Sandbox on by default) wrap through the same mixin."""
    from nadare.backends import winegame as wg
    from nadare.core import backends

    monkeypatch.setattr(wg.sandbox, "in_flatpak", lambda: False)
    monkeypatch.setattr(umu, "runtime_downloaded", lambda: False)
    seen: list[list[str]] = []

    def fake_stream(argv, env, on_line=None, cancel=None, **_kw):
        seen.append(list(argv))
        copies.append(str(_copy(tmp_path)))
        return 0

    monkeypatch.setattr(wg, "stream", fake_stream)
    ctx = make_context(tmp_path, "direct-pgr", {"prefix": str(tmp_path / "pfx"),
                                                "install_dir": str(tmp_path / "game"),
                                                "secure_mode": True})
    backend = backends.get("direct-pgr")(ctx)
    assert backend.secure_mode_enabled()
    env = {"PROTONPATH": str(tmp_path / "Proton"), "WINEPREFIX": str(tmp_path / "pfx")}
    argv, _env = backend._secure_wrap(["/usr/bin/umu-run", "game.exe"], env)
    assert seen == [["/usr/bin/umu-run", ""]]
    assert Path(argv[0]).name == "srt-bwrap"
