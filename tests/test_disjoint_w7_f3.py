# SPDX-License-Identifier: GPL-3.0-or-later
"""W7-F3: an exported PROTON_ENABLE_WAYLAND beats CZN's display: x11 setting."""

from __future__ import annotations

from _backend_helpers import make_context

from nadare.backends.czn import backend as czn_backend
from nadare.backends.czn.backend import CznBackend


def test_czn_display_x11_wins_over_an_exported_proton_enable_wayland(
    tmp_path, monkeypatch
):
    proton = tmp_path / "MyProton"
    proton.mkdir()
    (proton / "proton").write_text("#!/bin/sh\n", encoding="utf-8")
    backend = CznBackend(
        make_context(
            tmp_path,
            "stove-czn",
            {"prefix": str(tmp_path / "prefix"), "runner": str(proton), "display": "x11"},
        )
    )
    monkeypatch.setattr(czn_backend.shutil, "which", lambda name, *a, **k: None)
    monkeypatch.setattr(CznBackend, "_download_runner", lambda self, rid, latest: None)
    monkeypatch.setenv("PROTON_ENABLE_WAYLAND", "1")
    monkeypatch.setenv("NADARE_SKIP_HOSTLIBS_CHECK", "1")

    _argv, env = backend._prefix_command(["game.exe"])

    assert env["PROTON_ENABLE_WAYLAND"] == "0", (
        "the profile says display: x11 (the NVIDIA escape hatch), but the exported "
        f"PROTON_ENABLE_WAYLAND=1 still reaches the game: {env['PROTON_ENABLE_WAYLAND']!r}"
    )
