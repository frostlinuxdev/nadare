# SPDX-License-Identifier: GPL-3.0-or-later
"""A too-old system umu-run must not shadow Nadare's managed copy (Bazzite ships 1.2.6)."""

from __future__ import annotations

import subprocess
from types import SimpleNamespace

from nadare.core import umu

OLD = "umu-launcher version 1.2.6 (3.13.7 (main, Aug 14 2025, 00:00:00) [GCC 15.2.1])\n"
NEW = "umu-launcher version 1.4.4 (3.13.7 (main, Aug 14 2025, 00:00:00) [GCC 15.2.1])\n"


def _run(stdout: str, code: int = 0):
    def run(cmd, **kwargs):  # noqa: ANN001, ANN003
        return SimpleNamespace(returncode=code, stdout=stdout, stderr="")

    return run


def test_parse_version_reads_the_banner() -> None:
    assert umu.parse_version(OLD) == (1, 2, 6)
    assert umu.parse_version(NEW) == (1, 4, 4)
    assert umu.parse_version("umu-launcher version 1.3.0.rc1 (3.12)") == (1, 3, 0)
    assert umu.parse_version("garbage") is None
    assert umu.parse_version("") is None


def test_old_system_umu_is_skipped() -> None:
    assert umu.system("umu-run", which=lambda name: "/usr/bin/umu-run", run=_run(OLD)) is None


def test_new_enough_system_umu_is_used() -> None:
    found = umu.system("umu-run", which=lambda name: "/usr/bin/umu-run", run=_run(NEW))
    assert found == "/usr/bin/umu-run"
    exact = "umu-launcher version 1.3.0 (3.12)"
    assert umu.system("umu-run", which=lambda n: "/x/umu-run", run=_run(exact)) == "/x/umu-run"


def test_unknown_version_is_trusted() -> None:
    assert umu.system("umu-run", which=lambda n: "/x/umu-run", run=_run("???")) == "/x/umu-run"

    def boom(cmd, **kwargs):  # noqa: ANN001, ANN003
        raise subprocess.TimeoutExpired(cmd, 5)

    assert umu.system("umu-run", which=lambda n: "/x/umu-run", run=boom) == "/x/umu-run"


def test_an_explicitly_configured_binary_always_wins() -> None:
    found = umu.system("/opt/my-umu", which=lambda name: name, run=_run(OLD))
    assert found == "/opt/my-umu"


def test_missing_system_umu_is_none() -> None:
    assert umu.system("umu-run", which=lambda name: None, run=_run(NEW)) is None


def test_resolve_falls_back_to_managed_when_the_system_copy_is_old(tmp_path, monkeypatch) -> None:
    managed = tmp_path / "umu-run"
    managed.write_text("x")
    monkeypatch.setattr(umu, "system", lambda configured="umu-run", **k: None)
    monkeypatch.setattr(umu, "managed", lambda: managed)
    assert umu.resolve("umu-run", allow_download=False) == str(managed)


def test_doctor_names_an_old_system_umu(monkeypatch, capsys) -> None:
    from nadare.app import run_cli
    from nadare.core import graphics, process, runners

    monkeypatch.setattr(graphics, "vulkan_info", lambda **k: graphics.VulkanInfo(True))
    monkeypatch.setattr(graphics, "dxvk_ok", lambda: True)
    monkeypatch.setattr(process, "which", lambda name, **k: "/usr/bin/umu-run")
    monkeypatch.setattr(umu, "system", lambda configured="umu-run", **k: None)
    monkeypatch.setattr(runners, "all_runners", list)
    run_cli(["doctor"])
    out = capsys.readouterr().out
    assert "/usr/bin/umu-run is older than 1.3.0" in out
    assert "umu-run: not found" not in out
