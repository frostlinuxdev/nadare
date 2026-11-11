# SPDX-License-Identifier: GPL-3.0-or-later
"""doctor (and the System check) opens with one verdict line (#1002)."""

from __future__ import annotations

from nadare.app import run_cli
from nadare.core import graphics, process, runners
from nadare.gui.system_check import report_html


def _fake(monkeypatch, *, vulkan: bool, dxvk: bool) -> None:
    monkeypatch.setattr(
        graphics,
        "vulkan_info",
        lambda **k: graphics.VulkanInfo(vulkan, api_version="1.4.0" if vulkan else ""),
    )
    monkeypatch.setattr(graphics, "dxvk_ok", lambda: dxvk)
    monkeypatch.setattr(process, "which", lambda name, **k: "/usr/bin/umu-run")
    monkeypatch.setattr(runners, "all_runners", list)


def test_a_ready_system_says_so_first(monkeypatch, capsys):
    _fake(monkeypatch, vulkan=True, dxvk=True)
    assert run_cli(["doctor"]) == 0
    first = capsys.readouterr().out.splitlines()[0]
    assert first.startswith("verdict: ready")


def test_no_vulkan_is_the_verdict_and_names_the_fix_below(monkeypatch, capsys):
    _fake(monkeypatch, vulkan=False, dxvk=False)
    assert run_cli(["doctor"]) == 0  # only --strict changes the exit code
    out = capsys.readouterr().out.splitlines()
    assert out[0].startswith("verdict: not ready") and "Vulkan unavailable" in out[0]
    assert sum(line.startswith("verdict:") for line in out) == 1


def test_an_old_32bit_vulkan_is_a_blocker_too(monkeypatch, capsys):
    _fake(monkeypatch, vulkan=True, dxvk=False)
    run_cli(["doctor"])
    first = capsys.readouterr().out.splitlines()[0]
    assert first.startswith("verdict: not ready") and "Vulkan 1.3" in first


def test_strict_still_fails_only_on_its_own_reasons(monkeypatch, capsys):
    _fake(monkeypatch, vulkan=True, dxvk=False)
    assert run_cli(["doctor", "--strict"]) == 0  # unchanged: DXVK alone was never strict


def test_the_system_check_leads_with_the_verdict_not_a_flag_count():
    ready = report_html(
        "verdict: ready to install and play\numu-run: not found\nkeyring: unavailable"
    )
    assert "Ready to install and play" in ready and "flagged" not in ready.split("<table")[0]
    blocked = report_html("verdict: not ready: Vulkan unavailable\nGPU/Vulkan: Vulkan unavailable")
    head = blocked.split("<table")[0]
    assert "Not ready" in head and "Vulkan unavailable" in head
    old = report_html("umu-run: not found")  # an older doctor text without a verdict
    assert "flagged" in old
