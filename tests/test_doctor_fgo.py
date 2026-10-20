# SPDX-License-Identifier: GPL-3.0-or-later
"""``nadare doctor`` wiring for the FGO readiness section (fake probes)."""

from __future__ import annotations

import argparse

from nadare import app
from nadare.app import run_cli
from nadare.backends.fgo import readiness
from nadare.backends.fgo.readiness import Check, Distro
from nadare.core import backends, graphics, process, runners, secrets
from nadare.core.config import Config


def _patch_doctor(monkeypatch) -> None:
    monkeypatch.setattr(graphics, "vulkan_info", lambda **k: graphics.VulkanInfo(False))
    monkeypatch.setattr(graphics, "dxvk_ok", lambda: False)
    monkeypatch.setattr(process, "which", lambda name, **k: "/usr/bin/umu-run")
    monkeypatch.setattr(runners, "all_runners", list)
    monkeypatch.setattr(backends, "unavailable", list)
    monkeypatch.setattr(backends, "kinds", lambda: ["wine"])
    monkeypatch.setattr(secrets, "keyring_diagnosis", lambda: (True, "test"))


def _fedora_distro(text=None) -> Distro:
    return Distro("fedora", "", "fedora")


def test_doctor_json_flag_is_parsed() -> None:
    assert app.build_parser().parse_args(["doctor", "--fgo"]).fgo is True


def test_doctor_prints_fgo_readiness_with_flag(monkeypatch, capsys) -> None:
    _patch_doctor(monkeypatch)
    checks = [Check("podman", "blocking", True, "found at /usr/bin/podman")]
    monkeypatch.setattr(readiness, "run_checks", lambda probe=None: checks)
    monkeypatch.setattr(readiness, "detect_distro", lambda text=None: Distro("arch", "", "arch"))

    assert run_cli(["doctor", "--fgo"]) == 0
    out = capsys.readouterr().out
    assert "FGO readiness:" in out
    assert "podman" in out


def test_doctor_hides_fgo_without_flag_or_profile(monkeypatch, capsys) -> None:
    _patch_doctor(monkeypatch)
    assert run_cli(["doctor"]) == 0
    assert "FGO readiness:" not in capsys.readouterr().out


def test_fgo_section_when_a_profile_uses_the_backend(monkeypatch, capsys, tmp_path) -> None:
    _patch_doctor(monkeypatch)
    monkeypatch.setattr(readiness, "run_checks", lambda probe=None: [])
    monkeypatch.setattr(readiness, "detect_distro", _fedora_distro)

    config = Config.load(tmp_path / "config.json")
    config.add_profile("fgo", backend="fgo-lepton", title="FGO")
    app._print_fgo_readiness(config, argparse.Namespace(fgo=False))
    assert "FGO readiness:" in capsys.readouterr().out


def test_no_fgo_section_for_other_backends(monkeypatch, capsys, tmp_path) -> None:
    _patch_doctor(monkeypatch)
    monkeypatch.setattr(readiness, "run_checks", lambda probe=None: [])
    monkeypatch.setattr(readiness, "detect_distro", _fedora_distro)

    config = Config.load(tmp_path / "config.json")
    config.add_profile("w", backend="wine", title="Wine")
    app._print_fgo_readiness(config, None)
    assert "FGO readiness:" not in capsys.readouterr().out
