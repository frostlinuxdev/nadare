# SPDX-License-Identifier: GPL-3.0-or-later
"""doctor's verdict must agree with the new pre-launch AppArmor refusal (#1338 lens).

On Ubuntu 24.04 with a usable system umu (so no missing 32-bit libraries), no Nadare
AppArmor profile and a blocked runtime probe, every Proton Play now stops in
`userns.blocks_runtime()` before launching.  `doctor` prints the block as a detail but its
verdict stays "ready", because it only adds the userns block to the blockers when the
32-bit libraries are missing.
"""

from __future__ import annotations

from nadare.app import run_cli
from nadare.core import graphics, process, runners, umu, userns


def test_doctor_is_not_ready_when_every_play_will_be_blocked(monkeypatch, capsys):
    monkeypatch.setattr(graphics, "vulkan_info", lambda **k: graphics.VulkanInfo(True))
    monkeypatch.setattr(graphics, "dxvk_ok", lambda: True)
    # A usable system umu, so doctor takes the "found" path where missing_32bit is not consulted.
    monkeypatch.setattr(process, "which", lambda name, **k: "/usr/bin/umu-run")
    monkeypatch.setattr(umu, "system", lambda configured="umu-run", **k: "/usr/bin/umu-run")
    monkeypatch.setattr(runners, "all_runners", list)
    monkeypatch.setattr(userns, "restricted", lambda *a, **k: True)
    monkeypatch.setattr(userns, "profile_installed", lambda *a, **k: False)
    monkeypatch.setattr(userns, "probe", lambda *a, **k: False)

    assert run_cli(["doctor"]) == 0
    out = capsys.readouterr().out
    assert "blocked by AppArmor" in out  # the detail line is right
    # The GUI's System check leads with this verdict; it must not say every Proton game is ready.
    assert "verdict: not ready" in out
