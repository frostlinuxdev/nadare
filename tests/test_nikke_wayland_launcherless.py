# SPDX-License-Identifier: GPL-3.0-or-later
"""NIKKE's display "auto" follows how the game starts (user, 2026-11-10).

X11 is NIKKE's default only because the launcher's sign-in browser goes blank on native Wayland
(#1124, #1140). A launcherless run starts no launcher, so "auto" means Wayland there; a launcher
run (and the fallback to it) stays on X11. A Display choice in Settings always wins.
"""

from __future__ import annotations

import contextlib
import json

from test_nikke_launcherless_launch import AUTH, _NoProbe, make_install

from nadare.backends import launcher as launcher_module
from nadare.backends.launcher import NikkeBackend
from nadare.backends.nikke import hosts as h
from nadare.core.backends import get


def _run_launcherless(backend, monkeypatch):
    """Run a launcherless launch with Wine faked; return what each helper's command saw."""
    seen = []

    def fake_stream(argv, env, on_line=None, cancel=None, **kwargs):
        if h.INTL_HOST_NAME in argv[0]:
            out = argv[argv.index("--out") + 1]
            relative = out.split(":\\", 1)[1].replace("\\", "/")
            session_file = backend.prefix() / "drive_c" / relative
            session_file.parent.mkdir(parents=True, exist_ok=True)
            session_file.write_text(json.dumps(AUTH), encoding="utf-8")
        return 0

    def fake_prefix_command(self, args, extra_env=None, **kw):
        seen.append(self._wayland_default())
        return args, {}

    monkeypatch.setattr(launcher_module, "stream", fake_stream)
    monkeypatch.setattr(NikkeBackend, "_prefix_command", fake_prefix_command)
    monkeypatch.setattr(NikkeBackend, "_run_vcredist", lambda self, created=False: None)
    monkeypatch.setattr(NikkeBackend, "_log_launch_details", lambda self, env: None)
    monkeypatch.setattr(NikkeBackend, "_run_lock", lambda self: contextlib.nullcontext())
    monkeypatch.setattr(NikkeBackend, "_renderer_probe", lambda self: _NoProbe())
    monkeypatch.setattr(
        launcher_module.graphics, "effective_renderer", lambda profile: ("vulkan", "")
    )
    backend.info = lambda _text: None
    assert backend.launch() == 0
    return seen


def test_a_launcherless_run_uses_wayland_for_the_game_only(tmp_path, monkeypatch):
    """The sign-in helper stays on X11: NIKKE's intl_service hung under Wine's Wayland driver
    (rig, 2026-11-10: the helper never answered on Wayland, finished in seconds on X11)."""
    backend = make_install(tmp_path)
    assert _run_launcherless(backend, monkeypatch) == ["0", "1"]  # sign-in helper, then the game


def test_after_the_launcherless_run_auto_is_x11_again(tmp_path, monkeypatch):
    backend = make_install(tmp_path)
    _run_launcherless(backend, monkeypatch)
    assert backend._wayland_default() == "0"


def test_the_launcher_path_stays_on_x11(tmp_path):
    backend = make_install(tmp_path)
    backend.ctx.profile["launcherless"] = False
    assert backend._wayland_default() == "0"


def test_a_display_choice_wins_in_a_launcherless_run(tmp_path, monkeypatch):
    backend = make_install(tmp_path)
    backend.ctx.profile["display"] = "x11"
    assert _run_launcherless(backend, monkeypatch) == ["0", "0"]


def test_the_wayland_display_state_is_reported_during_a_launcherless_run(tmp_path, monkeypatch):
    monkeypatch.setenv("WAYLAND_DISPLAY", "wayland-0")
    monkeypatch.delenv("PROTON_ENABLE_WAYLAND", raising=False)
    backend = make_install(tmp_path)
    backend._launcherless_game = True
    assert backend._display_in_use() == "wayland"
    backend._launcherless_game = False
    assert backend._display_in_use() == "x11"


def test_settings_say_what_auto_means_for_nikke():
    display = next(o for o in get("launcher-nikke").options if o.key == "display")
    labels = dict((value, label) for label, value in display.choices)
    assert labels["auto"] == "Auto (X11 with the launcher)"
    assert "without the launcher" in display.help and "Wayland" in display.help
