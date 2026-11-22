# SPDX-License-Identifier: GPL-3.0-or-later
"""Snowbreak defaults to X11 (XWayland): on native Wayland its sign-in window (a full-screen
layered window) is drawn black and placed off-centre, part of it off the screen; on X11 the same
prefix and runner show it centred and transparent (amdnode KDE Wayland, 2026-11-21, b25-rc2)."""

from __future__ import annotations

from _backend_helpers import make_context

from nadare.backends import direct


def _backend(tmp_path, **profile):
    ctx = make_context(tmp_path, "direct-snowbreak", {"prefix": str(tmp_path / "pfx"), **profile})
    return direct.SnowbreakBackend(ctx)


def test_snowbreak_runs_on_x11_by_default(tmp_path, monkeypatch):
    monkeypatch.delenv("PROTON_ENABLE_WAYLAND", raising=False)
    backend = _backend(tmp_path)
    assert backend._wayland_default() == "0"
    assert backend._display_in_use() == "x11"


def test_snowbreak_settings_name_x11_as_the_default():
    assert direct.SnowbreakBackend.default_display == "x11"
    option = next(o for o in direct.SnowbreakBackend.options if o.key == "display")
    assert dict((value, label) for label, value in option.choices)["auto"] == "Auto (X11)"


def test_snowbreak_can_still_choose_wayland(tmp_path, monkeypatch):
    monkeypatch.setenv("WAYLAND_DISPLAY", "wayland-0")
    backend = _backend(tmp_path, display="wayland")
    assert backend._display_in_use() == "wayland"
