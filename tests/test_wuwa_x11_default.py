# SPDX-License-Identifier: GPL-3.0-or-later
"""WuWa defaults to X11 (XWayland): on native Wayland its sign-in window opened on another
screen for a player. The display choice in Settings names the real default for every game."""

from __future__ import annotations

import pytest
from _backend_helpers import make_context

from nadare.backends import direct


def _backend(tmp_path, cls, kind, **profile):
    return cls(make_context(tmp_path, kind, {"prefix": str(tmp_path / "pfx"), **profile}))


def test_wuwa_runs_on_x11_by_default(tmp_path, monkeypatch):
    monkeypatch.delenv("PROTON_ENABLE_WAYLAND", raising=False)
    backend = _backend(tmp_path, direct.WutheringWavesBackend, "direct-wuwa")
    assert backend._wayland_default() == "0"
    assert backend._display_in_use() == "x11"


def test_wuwa_can_still_choose_wayland(tmp_path, monkeypatch):
    monkeypatch.setenv("WAYLAND_DISPLAY", "wayland-0")
    backend = _backend(tmp_path, direct.WutheringWavesBackend, "direct-wuwa", display="wayland")
    assert backend._wayland_default() == "1"
    assert backend._display_in_use() == "wayland"


def _auto_label(cls) -> str:
    option = next(o for o in cls.options if o.key == "display")
    return dict((value, label) for label, value in option.choices)["auto"]


@pytest.mark.parametrize(
    "cls", [direct.WutheringWavesBackend, direct.PgrBackend, direct.NteDirectBackend]
)
def test_an_x11_default_game_says_so_in_settings(cls):
    assert cls.default_display == "x11"
    assert _auto_label(cls) == "Auto (X11)"
    help_text = next(o for o in cls.options if o.key == "display").help
    assert "X11 (XWayland) is the default" in help_text


def test_a_wayland_default_game_keeps_its_label():
    assert direct.BrownDust2Backend.default_display == "wayland"
    assert _auto_label(direct.BrownDust2Backend) == "Auto (Wayland)"
