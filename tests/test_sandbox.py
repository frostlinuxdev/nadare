# SPDX-License-Identifier: GPL-3.0-or-later
"""Flatpak awareness: host-only tools and Proton without umu."""

from __future__ import annotations

import subprocess

import pytest

from nadare.core import sandbox


def test_in_flatpak_detection(monkeypatch, tmp_path):
    monkeypatch.setenv("NADARE_FLATPAK", "1")
    assert sandbox.in_flatpak()
    monkeypatch.delenv("NADARE_FLATPAK")
    monkeypatch.setattr(sandbox, "FLATPAK_INFO", tmp_path / "nope")
    assert not sandbox.in_flatpak()
    (tmp_path / "info").write_text("[Application]\n")
    monkeypatch.setattr(sandbox, "FLATPAK_INFO", tmp_path / "info")
    assert sandbox.in_flatpak()


def test_host_argv_is_a_passthrough_outside_flatpak(monkeypatch):
    monkeypatch.setattr(sandbox, "in_flatpak", lambda: False)
    argv = sandbox.host_argv(["waydroid", "status"], env={"A": "1"}, cwd="/x")
    assert argv == ["waydroid", "status"]


def test_host_argv_wraps_with_env_delta_and_cwd(monkeypatch):
    monkeypatch.setattr(sandbox, "in_flatpak", lambda: True)
    monkeypatch.setenv("KEEP", "same")
    monkeypatch.delenv("NEW", raising=False)
    argv = sandbox.host_argv(
        ["lepton-fgo", "status"], env={"KEEP": "same", "NEW": "v 1", "B": "2"}, cwd="/games"
    )
    assert argv == [
        "flatpak-spawn",
        "--host",
        "--directory=/games",
        "--env=B=2",
        "--env=NEW=v 1",
        "lepton-fgo",
        "status",
    ]


def test_host_argv_without_env_or_cwd(monkeypatch):
    monkeypatch.setattr(sandbox, "in_flatpak", lambda: True)
    assert sandbox.host_argv(["waydroid"]) == ["flatpak-spawn", "--host", "waydroid"]


def test_host_argv_refuses_secret_env(monkeypatch):
    monkeypatch.setattr(sandbox, "in_flatpak", lambda: True)
    monkeypatch.delenv("MY_TOKEN", raising=False)
    with pytest.raises(ValueError):
        sandbox.host_argv(["waydroid"], env={"MY_TOKEN": "shh"})


def test_host_argv_allows_non_secret_env(monkeypatch):
    monkeypatch.setattr(sandbox, "in_flatpak", lambda: True)
    monkeypatch.delenv("NADARE_DISPLAY", raising=False)
    argv = sandbox.host_argv(["waydroid"], env={"NADARE_DISPLAY": ":0"})
    assert argv == ["flatpak-spawn", "--host", "--env=NADARE_DISPLAY=:0", "waydroid"]


def test_host_which_outside_flatpak_uses_shutil(monkeypatch):
    monkeypatch.setattr(sandbox, "in_flatpak", lambda: False)
    monkeypatch.setattr(sandbox.shutil, "which", lambda name: "/usr/bin/" + name)
    assert sandbox.host_which("waydroid") == "/usr/bin/waydroid"


def test_host_which_in_flatpak_asks_the_host(monkeypatch):
    monkeypatch.setattr(sandbox, "in_flatpak", lambda: True)
    calls = []

    def fake_run(argv, **kwargs):
        calls.append(argv)
        return subprocess.CompletedProcess(argv, 0, stdout="/usr/bin/waydroid\n", stderr="")

    monkeypatch.setattr(sandbox.subprocess, "run", fake_run)
    assert sandbox.host_which("waydroid") == "/usr/bin/waydroid"
    assert calls[0][:2] == ["flatpak-spawn", "--host"]
    # The name is passed as an argument, never interpolated into the script.
    assert calls[0][-1] == "waydroid"
    assert all("waydroid" not in part for part in calls[0][2:-1])


def test_host_which_missing_or_failing_is_none(monkeypatch):
    monkeypatch.setattr(sandbox, "in_flatpak", lambda: True)
    monkeypatch.setattr(
        sandbox.subprocess,
        "run",
        lambda argv, **kw: subprocess.CompletedProcess(argv, 1, stdout="", stderr=""),
    )
    assert sandbox.host_which("nope") is None

    def boom(*a, **k):
        raise OSError("no flatpak-spawn")

    monkeypatch.setattr(sandbox.subprocess, "run", boom)
    assert sandbox.host_which("x") is None


def test_proton_direct_links_pfx_to_a_plain_wine_prefix(tmp_path):
    proton = tmp_path / "GE-Proton"
    proton.mkdir()
    (proton / "proton").write_text("#!/usr/bin/env python3\n")
    prefix = tmp_path / "prefix"
    (prefix / "drive_c").mkdir(parents=True)
    argv, env = sandbox.proton_direct(proton, prefix, ["C:\\game.exe", "-x"])
    assert argv == [str(proton / "proton"), "run", "C:\\game.exe", "-x"]
    assert env["STEAM_COMPAT_DATA_PATH"] == str(prefix)
    assert env["STEAM_COMPAT_CLIENT_INSTALL_PATH"] == str(prefix)
    assert (prefix / "pfx").is_symlink()
    assert (prefix / "pfx").resolve() == prefix.resolve()


def test_proton_direct_keeps_an_existing_pfx(tmp_path):
    proton = tmp_path / "p"
    proton.mkdir()
    prefix = tmp_path / "compat"
    (prefix / "pfx" / "drive_c").mkdir(parents=True)
    sandbox.proton_direct(proton, prefix, [])
    assert (prefix / "pfx").is_dir() and not (prefix / "pfx").is_symlink()
