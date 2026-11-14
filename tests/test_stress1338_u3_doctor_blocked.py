# SPDX-License-Identifier: GPL-3.0-or-later
"""Doctor must not say "ready" when every Proton launch will stop for AppArmor (#1338 lens).

`userns.blocks_runtime()` now refuses a desktop launch (with 32-bit libs present, no system
umu) before umu starts. `doctor`'s verdict only adds the runtime block when the 32-bit
libraries are *missing*, so the same host gets "verdict: ready to install and play".
"""

from __future__ import annotations

from nadare.app import run_cli
from nadare.core import graphics, process, runners, userns


def test_doctor_is_not_ready_when_the_launch_will_be_blocked(monkeypatch, capsys):
    monkeypatch.setattr(graphics, "vulkan_info", lambda **k: graphics.VulkanInfo(True))
    monkeypatch.setattr(graphics, "dxvk_ok", lambda: True)
    monkeypatch.setattr(process, "which", lambda name, **k: None)
    monkeypatch.setattr(runners, "all_runners", list)
    # The host from the report: 32-bit libs present (conftest makes missing_32bit []),
    # Ubuntu AppArmor restricts userns, no profile, and the runtime probe is blocked.
    monkeypatch.setattr(userns, "restricted", lambda *a, **k: True)
    monkeypatch.setattr(userns, "profile_installed", lambda *a, **k: False)
    monkeypatch.setattr(userns, "probe", lambda *a, **k: False)

    assert run_cli(["doctor"]) == 0
    out = capsys.readouterr().out
    assert "blocked by AppArmor" in out  # the detail line is right
    # But the verdict that the GUI's System check shows must say it is not ready.
    assert "verdict: not ready" in out
