# SPDX-License-Identifier: GPL-3.0-or-later
"""W7-F4: a publisher installer is not pinned to XWayland on a Wayland-default game."""

from __future__ import annotations

from _backend_helpers import make_context

from nadare.backends import winegame as wg
from nadare.backends.arknights import ArknightsBackend
from nadare.backends.winegame_parts import installer as installer_module


def test_the_arknights_publisher_installer_runs_on_xwayland(tmp_path, monkeypatch):
    backend = ArknightsBackend(make_context(tmp_path, ArknightsBackend.kind, {}, "ak"))
    monkeypatch.setattr(backend, "resolve_runner", lambda: (tmp_path / "proton", "proton"))
    monkeypatch.setattr(wg.shutil, "which", lambda name, *_a, **_k: "/usr/bin/umu-run")
    monkeypatch.delenv("PROTON_ENABLE_WAYLAND", raising=False)
    seen: list[dict[str, str]] = []

    def fake_stream(argv, env, on_line=None, cancel=None):
        seen.append(dict(env))
        return 0

    monkeypatch.setattr(installer_module, "stream", fake_stream)
    setup = tmp_path / "Arknights_setup.exe"
    setup.write_bytes(b"MZ")

    assert backend._run_installer(setup) == 0
    value = seen[0].get("PROTON_ENABLE_WAYLAND")
    assert value == "0", (
        "CZN's and Haoplay's installers are pinned to PROTON_ENABLE_WAYLAND=0 because the "
        "installer window fails on native Wayland, but Arknights' publisher installer ran "
        f"with PROTON_ENABLE_WAYLAND={value!r} (profile display: auto)"
    )
