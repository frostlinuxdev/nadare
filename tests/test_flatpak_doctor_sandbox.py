# SPDX-License-Identifier: GPL-3.0-or-later
"""A Flatpak uses its own sandbox, so doctor never asks for bubblewrap there."""

import pytest

from nadare.app import run_cli
from nadare.core import gamesandbox, graphics, runners, sandbox, secrets


def test_doctor_reports_the_flatpak_sandbox_without_probing_bubblewrap(monkeypatch, capsys):
    monkeypatch.setattr(sandbox, "in_flatpak", lambda: True)
    monkeypatch.setattr(
        gamesandbox, "_flatpak_spawn", lambda: "/usr/bin/flatpak-spawn", raising=False
    )
    monkeypatch.setattr(graphics, "vulkan_info", lambda **kw: graphics.VulkanInfo(False))
    monkeypatch.setattr(graphics, "dxvk_ok", lambda: False)
    monkeypatch.setattr(runners, "all_runners", list)
    monkeypatch.setattr(secrets, "keyring_diagnosis", lambda: (False, "test"))
    monkeypatch.setattr(
        gamesandbox, "locate", lambda: pytest.fail("unsupported feature was probed")
    )
    assert run_cli(["doctor"]) == 0
    output = capsys.readouterr().out
    assert "Sandbox: available (Flatpak sandbox" in output
    assert "bubblewrap is not installed" not in output


def test_flatpak_vulkan_failure_points_to_runtime_updates_not_host_i386_packages(
    monkeypatch, capsys
):
    monkeypatch.setattr(sandbox, "in_flatpak", lambda: True)
    monkeypatch.setattr(graphics, "vulkan_info", lambda **kw: graphics.VulkanInfo(False))
    monkeypatch.setattr(graphics, "dxvk_ok", lambda: False)
    monkeypatch.setattr(runners, "all_runners", list)
    monkeypatch.setattr(secrets, "keyring_diagnosis", lambda: (False, "test"))
    monkeypatch.setattr(gamesandbox, "locate", lambda: gamesandbox.Located(None))
    assert run_cli(["doctor"]) == 0
    output = capsys.readouterr().out
    assert "flatpak update" in output
    assert "sudo apt" not in output
    assert "sudo dnf" not in output
    assert "sudo pacman" not in output
