# SPDX-License-Identifier: GPL-3.0-or-later
"""NIKKE runs on X11 (XWayland) by default (#1124).

Measured on a KDE Wayland desktop (RX 7800 XT): under native Wayland the launcher's embedded
Chromium (`tbs_browser.exe`) hung its GPU process, which was killed after 60 s, and the window
stayed blank white in 9 of 9 runs; on X11 it drew within 10 s in 3 of 3. NTE defaults to X11 for
the same reason. A profile's explicit `display: wayland` still wins.
"""

from __future__ import annotations

from _backend_helpers import make_context, write_exe

from nadare.backends.launcher import NikkeBackend
from nadare.core.backends import get


def _nikke(tmp_path, **profile):
    values = {"prefix": str(tmp_path / "prefix"), "wine_binary": "/usr/bin/wine", **profile}
    write_exe(tmp_path / "prefix" / "drive_c" / "NIKKE" / "Launcher" / "nikke_launcher.exe", "MZ")
    return NikkeBackend(make_context(tmp_path, "launcher-nikke", values))


def test_nikke_defaults_to_x11(tmp_path):
    assert _nikke(tmp_path)._wayland_default() == "0"


def test_an_explicit_wayland_choice_still_wins(tmp_path):
    assert _nikke(tmp_path, display="wayland")._wayland_default() == "1"


def test_the_settings_choice_says_auto_means_x11_for_nikke():
    display = next(o for o in get("launcher-nikke").options if o.key == "display")
    labels = dict((value, label) for label, value in display.choices)
    assert labels["auto"] == "Auto (X11 with the launcher)"


def test_other_launcher_games_keep_wayland_as_auto():
    from _backend_helpers import fake_launcher_backend

    display = next(o for o in fake_launcher_backend().options if o.key == "display")
    assert dict((v, label) for label, v in display.choices)["auto"] == "Auto (Wayland)"
