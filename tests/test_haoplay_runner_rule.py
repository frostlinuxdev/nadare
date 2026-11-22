# SPDX-License-Identifier: GPL-3.0-or-later
"""GFL2 Haoplay is the one game on GE-Proton, and only where DXVK can run (user, 2026-11-19).

GE-Proton draws the Haoplay sign-in window under native Wayland (DW-Proton froze on it), but
it has no DXVK-Sarek: below Vulkan 1.3 the game stays on DW-Proton, which switches to Sarek
by itself. Every other game keeps DW-Proton.
"""

from __future__ import annotations

import pytest

from nadare.core import graphics, runners, steamglue, steamsetup

HAOPLAY_KIND = "direct-gfl2-haoplay"
HAOPLAY_STEAM = "3308670"


@pytest.fixture
def modern_vulkan(monkeypatch):
    monkeypatch.setattr(graphics, "dxvk_ok", lambda: True)


@pytest.fixture
def old_vulkan(monkeypatch):
    monkeypatch.setattr(graphics, "dxvk_ok", lambda: False)


# ---- Nadare's own install --------------------------------------------------------------
def test_haoplay_defaults_to_ge_where_dxvk_runs(modern_vulkan):
    assert runners.default_family(HAOPLAY_KIND) == "ge"
    assert runners._wanted_family({"backend": HAOPLAY_KIND}) == "ge"


def test_haoplay_stays_on_dw_proton_below_vulkan_13(old_vulkan):
    assert runners.default_family(HAOPLAY_KIND) == "dwproton"
    assert runners._wanted_family({"backend": HAOPLAY_KIND}) == "dwproton"


@pytest.mark.parametrize("kind", ["direct-gfl2", "direct-wuwa", "stove-czn", "wine", None])
def test_every_other_game_keeps_dw_proton(modern_vulkan, kind):
    assert runners.default_family(kind) == runners.DEFAULT_FAMILY == "dwproton"


def test_a_runner_the_player_picked_still_wins(modern_vulkan):
    assert runners._wanted_family({"backend": HAOPLAY_KIND, "runner": "dwproton"}) == "dwproton"


def test_the_runner_list_counts_ge_as_used_by_haoplay(modern_vulkan, monkeypatch, tmp_path):
    ge, dw = tmp_path / "GE-Proton11-7", tmp_path / "dwproton-11.0-17"
    monkeypatch.setattr(runners, "nadare_runners", lambda: [ge, dw])
    monkeypatch.setattr(
        runners,
        "installed_pinned",
        lambda family=runners.DEFAULT_FAMILY: {"ge": ge}.get(family, dw),
    )
    usage = runners.runner_usage(
        {"hao": {"backend": HAOPLAY_KIND}, "w": {"backend": "direct-wuwa"}}
    )
    assert usage[str(ge)] == ["hao"]
    assert usage[str(dw)] == ["w"]


def test_the_backend_asks_for_the_same_family(modern_vulkan, monkeypatch):
    backend = _haoplay_backend()
    assert backend._wanted_family() == "ge"
    monkeypatch.setattr(graphics, "dxvk_ok", lambda: False)
    assert backend._wanted_family() == "dwproton"


def _haoplay_backend():
    from nadare.backends.direct import Gfl2HaoplayBackend

    backend = Gfl2HaoplayBackend.__new__(Gfl2HaoplayBackend)
    backend.ctx = type("Ctx", (), {"profile": {"backend": HAOPLAY_KIND}})()
    return backend


# ---- the Steam copy --------------------------------------------------------------------
def test_steam_family_follows_the_same_rule():
    game = steamsetup.game_for(HAOPLAY_STEAM)
    assert steamsetup.runner_family_for(game, "1.4.3") == "ge"
    assert steamsetup.runner_family_for(game, None) == "ge"  # unknown: the sign-in needs GE
    assert steamsetup.runner_family_for(game, "1.2.170") == "dwproton"
    other = steamsetup.game_for("3224770")
    assert steamsetup.runner_family_for(other, "1.4.3") == "dwproton"


def test_steam_set_runner_fetches_dw_proton_on_old_vulkan(tmp_path, monkeypatch):
    asked: list[str] = []

    def fake_pinned(family, *, emit=None, cancel=None):
        asked.append(family)
        raise RuntimeError("stop here, Steam is not touched in this test")

    monkeypatch.setattr(runners, "ensure_pinned", fake_pinned)
    item = steamsetup.SetupItem(key="runner", label="Runner", state="todo", action="set_runner")
    for version, family in (("1.2.170", "dwproton"), ("1.4.3", "ge")):
        monkeypatch.setattr(
            graphics, "vulkan_info", lambda version=version, **k: graphics.VulkanInfo(True, version)
        )
        steamglue.make_actions(HAOPLAY_STEAM, root=tmp_path, emit=lambda e: None)(item)
        assert asked[-1] == family
