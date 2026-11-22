# SPDX-License-Identifier: GPL-3.0-or-later
"""Duet Night Abyss defaults to X11 (XWayland) (#1608): on native Wayland its window stays white
(DXVK and wined3d alike); on X11 the same prefix, runner and Sandbox reach the game's first
screen (amdnode, 2026-11-18)."""

from __future__ import annotations

from _backend_helpers import make_context

from nadare.backends import direct


def _backend(tmp_path, **profile):
    ctx = make_context(tmp_path, "direct-dna", {"prefix": str(tmp_path / "pfx"), **profile})
    return direct.DnaBackend(ctx)


def test_dna_runs_on_x11_by_default(tmp_path, monkeypatch):
    monkeypatch.delenv("PROTON_ENABLE_WAYLAND", raising=False)
    backend = _backend(tmp_path)
    assert backend._wayland_default() == "0"
    assert backend._display_in_use() == "x11"


def test_dna_settings_name_x11_as_the_default():
    assert direct.DnaBackend.default_display == "x11"
    option = next(o for o in direct.DnaBackend.options if o.key == "display")
    assert dict((value, label) for label, value in option.choices)["auto"] == "Auto (X11)"


def test_dna_can_still_choose_wayland(tmp_path, monkeypatch):
    monkeypatch.setenv("WAYLAND_DISPLAY", "wayland-0")
    backend = _backend(tmp_path, display="wayland")
    assert backend._display_in_use() == "wayland"
