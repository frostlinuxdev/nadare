# SPDX-License-Identifier: GPL-3.0-or-later
"""Keep the locales pressure-vessel compiles on every launch.

The Steam Runtime container always wants ``en_US.UTF-8`` (and the user's own locale). A host that
has not generated it (a British or German desktop, say) gets both compiled into a throw-away
folder on **every** launch: 1.6-2 s of each Play, measured on Endfield (15.5 s -> 13.5 s to the
window). Nadare now runs the same tool (``pv-locale-gen``) once, keeps its output, and hands it
over with ``LOCPATH`` + ``PRESSURE_VESSEL_GENERATE_LOCALES=0``: the container sees the same files
it would have made itself.

Only when the host's glibc is at least the runtime's (then the container uses the host's glibc,
which built the files); otherwise pressure-vessel keeps doing it itself.
"""

from __future__ import annotations

import subprocess
from pathlib import Path

import pytest

from nadare.core import pvlocales


class Tool:
    """Stands in for pv-locale-gen: writes ``locales`` into --output-dir, exits ``code``."""

    def __init__(self, locales=("en_GB.UTF-8", "en_US.UTF-8"), code=72, raise_=None):
        self.locales = locales
        self.code = code
        self.raise_ = raise_
        self.calls = []

    def __call__(self, argv, **kwargs):
        self.calls.append((list(argv), kwargs))
        if self.raise_ is not None:
            raise self.raise_
        out = Path(argv[argv.index("--output-dir") + 1])
        for name in self.locales:
            (out / name).mkdir(parents=True)
            (out / name / "LC_CTYPE").write_bytes(b"x")
        return subprocess.CompletedProcess(argv, self.code, "", "")


@pytest.fixture
def setup(tmp_path, monkeypatch):
    tool_path = tmp_path / "pv-locale-gen"
    tool_path.write_text("#!/bin/sh\n")
    monkeypatch.setattr(pvlocales, "locale_gen_tool", lambda: tool_path)
    monkeypatch.setattr(pvlocales, "host_glibc", lambda: (2, 41))
    monkeypatch.setattr(pvlocales, "runtime_glibc", lambda: (2, 41))
    monkeypatch.setattr(pvlocales, "_in_flatpak", lambda: False)
    return tmp_path / "cache"


ENV = {"LANG": "en_GB.UTF-8", "LANGUAGE": "en_GB:en", "PATH": "/usr/bin"}


def test_the_first_launch_builds_them_once_and_later_ones_reuse_them(setup):
    tool = Tool()
    first = pvlocales.prepare(ENV, setup, run=tool)
    again = pvlocales.prepare(ENV, setup, run=tool)

    assert len(tool.calls) == 1
    assert first == again
    assert first["PRESSURE_VESSEL_GENERATE_LOCALES"] == "0"
    folder = Path(first["LOCPATH"])
    assert sorted(p.name for p in folder.iterdir()) == ["en_GB.UTF-8", "en_US.UTF-8"]
    assert setup in folder.parents


def test_the_tool_gets_the_locale_and_an_output_folder(setup):
    tool = Tool()
    pvlocales.prepare({**ENV, "LC_TIME": "de_DE.UTF-8", "SECRET": "x"}, setup, run=tool)

    argv, kwargs = tool.calls[0]
    assert "--output-dir" in argv
    env = kwargs["env"]
    assert env["LANG"] == "en_GB.UTF-8"
    assert env["LC_TIME"] == "de_DE.UTF-8"
    assert "SECRET" not in env
    assert kwargs["timeout"] <= 120


def test_another_locale_gets_its_own_folder(setup):
    tool = Tool()
    a = pvlocales.prepare(ENV, setup, run=tool)
    b = pvlocales.prepare({**ENV, "LANG": "de_DE.UTF-8"}, setup, run=tool)
    assert len(tool.calls) == 2
    assert a["LOCPATH"] != b["LOCPATH"]


def test_a_new_glibc_builds_them_again(setup, monkeypatch):
    tool = Tool()
    pvlocales.prepare(ENV, setup, run=tool)
    monkeypatch.setattr(pvlocales, "host_glibc", lambda: (2, 42))
    pvlocales.prepare(ENV, setup, run=tool)
    assert len(tool.calls) == 2


def test_nothing_missing_means_no_locpath_and_no_second_run(setup):
    tool = Tool(locales=(), code=0)
    first = pvlocales.prepare(ENV, setup, run=tool)
    again = pvlocales.prepare(ENV, setup, run=tool)
    # LOCPATH would hide the host's own locales; pressure-vessel itself sets none here.
    assert first == again == {"PRESSURE_VESSEL_GENERATE_LOCALES": "0"}
    assert len(tool.calls) == 1


@pytest.mark.parametrize(
    "tool",
    [
        Tool(code=1),
        Tool(code=72, locales=()),  # says it made some, made none
        Tool(raise_=subprocess.TimeoutExpired("pv-locale-gen", 60)),
        Tool(raise_=OSError("cannot run")),
    ],
)
def test_a_failure_leaves_pressure_vessel_to_it_and_tries_again_next_time(setup, tool):
    assert pvlocales.prepare(ENV, setup, run=tool) == {}
    assert not any((setup / "pv-locales").glob("*/*")) if (setup / "pv-locales").exists() else True
    pvlocales.prepare(ENV, setup, run=tool)
    assert len(tool.calls) == 2


def test_an_older_host_glibc_is_left_alone(setup, monkeypatch):
    """The container then uses the runtime's glibc; files built by the host's may not fit."""
    monkeypatch.setattr(pvlocales, "host_glibc", lambda: (2, 39))
    tool = Tool()
    assert pvlocales.prepare(ENV, setup, run=tool) == {}
    assert tool.calls == []


@pytest.mark.parametrize("missing", ["tool", "host", "runtime"])
def test_unknown_parts_are_left_alone(setup, monkeypatch, missing):
    name = {"tool": "locale_gen_tool", "host": "host_glibc", "runtime": "runtime_glibc"}[missing]
    monkeypatch.setattr(pvlocales, name, lambda: None)
    tool = Tool()
    assert pvlocales.prepare(ENV, setup, run=tool) == {}
    assert tool.calls == []


@pytest.mark.parametrize(
    "extra", [{"LOCPATH": "/mine"}, {"PRESSURE_VESSEL_GENERATE_LOCALES": "1"}]
)
def test_the_users_own_choice_wins(setup, extra):
    tool = Tool()
    assert pvlocales.prepare({**ENV, **extra}, setup, run=tool) == {}
    assert tool.calls == []


def test_not_in_the_flatpak(setup, monkeypatch):
    monkeypatch.setattr(pvlocales, "_in_flatpak", lambda: True)
    tool = Tool()
    assert pvlocales.prepare(ENV, setup, run=tool) == {}


def test_a_kept_folder_that_is_a_link_is_not_used(setup, tmp_path):
    tool = Tool()
    first = pvlocales.prepare(ENV, setup, run=tool)
    folder = Path(first["LOCPATH"])
    elsewhere = tmp_path / "elsewhere"
    folder.rename(elsewhere)
    folder.symlink_to(elsewhere)

    again = pvlocales.prepare(ENV, setup, run=tool)

    assert len(tool.calls) == 2  # rebuilt, not followed
    assert not Path(again["LOCPATH"]).is_symlink()


def test_glibc_versions_are_read_from_the_files(tmp_path, monkeypatch):
    libc = tmp_path / "umu" / "steamrt4" / "steamrt4_platform_4.0.1" / "files" / "lib"
    libc = libc / "x86_64-linux-gnu" / "libc.so.6"
    libc.parent.mkdir(parents=True)
    libc.write_bytes(b"\x7fELF junk GNU C Library (Debian GLIBC 2.41-12) stable release "
                     b"version 2.41.\n more")
    monkeypatch.setattr(pvlocales, "_umu_roots", lambda: [tmp_path / "umu"])
    pvlocales._RUNTIME_GLIBC.clear()
    assert pvlocales.runtime_glibc() == (2, 41)
    monkeypatch.setattr(pvlocales.os, "confstr", lambda name: "glibc 2.39")
    assert pvlocales.host_glibc() == (2, 39)


def test_prefix_command_uses_it_for_umu_launches(tmp_path, monkeypatch):
    from _backend_helpers import make_context

    from nadare.backends.direct import EndfieldBackend
    from nadare.backends.winegame_parts import launching
    from nadare.core import protoncmd

    backend = EndfieldBackend(
        make_context(tmp_path, "direct-endfield", {"prefix": str(tmp_path / "prefix")})
    )
    runner = tmp_path / "runner"
    runner.mkdir()
    monkeypatch.setattr(backend, "resolve_runner", lambda: (runner, "proton"))
    monkeypatch.setattr(protoncmd.umu, "resolve", lambda *a, **k: "/usr/bin/umu-run")
    monkeypatch.setattr(launching, "_refuse_if_userns_blocked", lambda: None)
    monkeypatch.setattr(protoncmd.umu, "note_first_runtime", lambda emit: None)
    monkeypatch.setattr(launching.sandbox, "in_flatpak", lambda: False)
    monkeypatch.setattr(
        protoncmd.pvlocales,
        "prepare",
        lambda env, cache, **kw: {"LOCPATH": "/kept", "PRESSURE_VESSEL_GENERATE_LOCALES": "0"},
    )

    argv, env = backend._prefix_command(["game.exe"])

    assert env["LOCPATH"] == "/kept"
    assert env["PRESSURE_VESSEL_GENERATE_LOCALES"] == "0"
