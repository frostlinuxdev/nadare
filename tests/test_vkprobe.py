# SPDX-License-Identifier: GPL-3.0-or-later
"""The GPU is read from the Vulkan driver itself when ``vulkaninfo`` is not installed.

Maintainer's Flatpak (2026-11-11, RTX 3060, Arknights): every launch said "Vulkan version could not
be read from the driver (vulkan-tools is missing); DXVK was assumed to work", with an empty GPU
name. The Flatpak never has ``vulkaninfo``, most distros do not install it, and nobody can add it
to a Flatpak. A short helper process (``nadare _vkprobe``) asks ``libvulkan`` for the devices, the
way ``vulkaninfo`` does, so the page knows the real GPU and says nothing when it is fine.
"""

from __future__ import annotations

import json
import subprocess
import sys

import pytest

from nadare.core import graphics, vkprobe

NVIDIA = {"name": "NVIDIA GeForce RTX 3060", "api": "1.4.312", "type": "discrete", "vendor": 4318}
LAVAPIPE = {
    "name": "llvmpipe (LLVM 19.1.7, 256 bits)",
    "api": "1.4.305",
    "type": "cpu",
    "vendor": 65541,
}
IGPU = {
    "name": "AMD Radeon Graphics (RADV RENOIR)",
    "api": "1.3.289",
    "type": "integrated",
    "vendor": 4098,
}


@pytest.fixture(autouse=True)
def _fresh():
    graphics.reset_cache()
    yield
    graphics.reset_cache()


@pytest.fixture
def no_vulkaninfo(monkeypatch):
    monkeypatch.setattr(graphics, "_vulkaninfo_path", lambda: None)
    monkeypatch.setattr(graphics, "_hardware_icd_present", lambda dirs=None: True)
    monkeypatch.setattr(
        graphics,
        "_instance_version",
        lambda: graphics.VulkanInfo(available=True, api_version="1.4.321"),
    )


def _probe_says(monkeypatch, result):
    monkeypatch.setattr(graphics, "_run_vkprobe", lambda: result)


# ---- the decision ------------------------------------------------------------------------------


def test_the_driver_probe_names_the_gpu_and_the_warning_goes(no_vulkaninfo, monkeypatch):
    _probe_says(monkeypatch, {"instance": "1.4.321", "devices": [NVIDIA]})
    info = graphics.vulkan_info()
    assert info.available and not info.loader_only
    assert info.device == "NVIDIA GeForce RTX 3060"
    assert info.api_version == "1.4.312"
    assert graphics.vulkan_note() == ""


def test_the_best_gpu_is_chosen_over_a_cpu_rasteriser(no_vulkaninfo, monkeypatch):
    _probe_says(monkeypatch, {"instance": "1.4.321", "devices": [LAVAPIPE, IGPU, NVIDIA]})
    assert graphics.vulkan_info().device == "NVIDIA GeForce RTX 3060"


def test_only_a_cpu_rasteriser_is_no_gpu(no_vulkaninfo, monkeypatch):
    _probe_says(monkeypatch, {"instance": "1.4.321", "devices": [LAVAPIPE]})
    info = graphics.vulkan_info()
    assert not info.available and info.driver == "software"


def test_an_old_gpu_is_reported_as_it_is(no_vulkaninfo, monkeypatch):
    old = dict(IGPU, api="1.2.170")
    _probe_says(monkeypatch, {"instance": "1.4.321", "devices": [old]})
    assert graphics.vulkan_info().api_version == "1.2.170"
    assert not graphics.dxvk_ok()


def test_a_failed_probe_keeps_the_old_assumption(no_vulkaninfo, monkeypatch):
    _probe_says(monkeypatch, None)
    info = graphics.vulkan_info()
    assert info.available and info.loader_only  # as before: assumed, with the note


def test_vulkaninfo_still_comes_first(monkeypatch):
    monkeypatch.setattr(graphics, "_vulkaninfo_path", lambda: "/usr/bin/vulkaninfo")
    called = []
    monkeypatch.setattr(graphics, "_run_vkprobe", lambda: called.append(1) or None)

    def fake_run(*a, **k):
        return subprocess.CompletedProcess(
            a[0],
            0,
            "Vulkan Instance Version: 1.4.321\nGPU0:\n\tapiVersion = 1.4.312\n"
            "\tdeviceType = PHYSICAL_DEVICE_TYPE_DISCRETE_GPU\n"
            "\tdeviceName = NVIDIA GeForce RTX 3060\n\tdriverName = NVIDIA\n",
            "",
        )

    monkeypatch.setattr(graphics.subprocess, "run", fake_run)
    assert graphics.vulkan_info().device == "NVIDIA GeForce RTX 3060"
    assert called == []


# ---- the helper process ------------------------------------------------------------------------


def test_the_probe_runs_in_its_own_process_with_a_timeout(monkeypatch):
    seen = {}

    def fake_run(argv, **kw):
        seen["argv"], seen["timeout"] = argv, kw.get("timeout")
        return subprocess.CompletedProcess(argv, 0, json.dumps({"devices": [NVIDIA]}), "")

    monkeypatch.setattr(graphics.subprocess, "run", fake_run)
    assert graphics._run_vkprobe() == {"devices": [NVIDIA]}
    assert seen["argv"][-1] == vkprobe.SUBCOMMAND
    assert seen["timeout"] and seen["timeout"] <= 30


@pytest.mark.parametrize(
    "outcome",
    [
        subprocess.TimeoutExpired("x", 20),
        OSError("no exec"),
        subprocess.CompletedProcess([], 0, "not json", ""),
        subprocess.CompletedProcess([], 3, json.dumps({"devices": [NVIDIA]}), ""),
        subprocess.CompletedProcess([], 0, json.dumps(["a list"]), ""),
    ],
)
def test_any_probe_failure_is_none(monkeypatch, outcome):
    def fake_run(argv, **kw):
        if isinstance(outcome, BaseException):
            raise outcome
        return outcome

    monkeypatch.setattr(graphics.subprocess, "run", fake_run)
    assert graphics._run_vkprobe() is None


def test_the_command_frozen_and_from_source(monkeypatch):
    monkeypatch.setattr(sys, "frozen", True, raising=False)
    assert vkprobe.command() == [sys.executable, vkprobe.SUBCOMMAND]
    monkeypatch.delattr(sys, "frozen", raising=False)
    assert vkprobe.command() == [sys.executable, "-m", "nadare", vkprobe.SUBCOMMAND]


def test_the_cli_runs_the_probe_before_anything_else(monkeypatch):
    from nadare import app

    monkeypatch.setattr(vkprobe, "main", lambda: 7)
    monkeypatch.setattr(app, "build_parser", lambda: pytest.fail("parsed as a command"))
    assert app.run_cli([vkprobe.SUBCOMMAND]) == 7


def test_device_types_and_names_are_decoded():
    assert vkprobe.device_type(2) == "discrete"
    assert vkprobe.device_type(4) == "cpu"
    assert vkprobe.device_type(99) == "other"
    assert vkprobe.version_text((1 << 22) | (4 << 12) | 312) == "1.4.312"


def test_the_real_probe_answers_or_says_no_vulkan():
    """On a machine with a Vulkan loader it lists devices; without one it fails cleanly."""
    result = subprocess.run(
        vkprobe.command(), capture_output=True, text=True, timeout=60, check=False
    )
    if result.returncode == 0:
        data = json.loads(result.stdout)
        assert isinstance(data["devices"], list)
        for device in data["devices"]:
            assert device["name"] and device["api"].count(".") == 2
    else:
        assert result.returncode == vkprobe.NO_VULKAN


def test_on_the_same_vulkan_version_the_discrete_gpu_wins(no_vulkaninfo, monkeypatch):
    """This host lists an Intel iGPU and two AMD cards, all at 1.4.305."""
    igpu = dict(IGPU, api="1.4.305")
    card = dict(NVIDIA, api="1.4.305")
    _probe_says(monkeypatch, {"instance": "1.4.309", "devices": [igpu, card]})
    assert graphics.vulkan_info().device == card["name"]
