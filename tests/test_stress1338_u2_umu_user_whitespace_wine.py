# SPDX-License-Identifier: GPL-3.0-or-later
"""An empty (`"   "`) wine_binary must still mean the default Proton (#1338 lens).

A WineGame profile (Arknights/NIKKE machinery) with a whitespace-only `wine_binary` used to
mean "no custom Wine": the generic Wine backend test 05-9 says so.  The new #1338 fallback
guard asks `_wanted_family()`, but that still treats the whitespace as a real Wine, so
`_ensure_default_runner` is skipped and `_prefix_command` reaches `first_wine()`: a random
legacy Wine on the game's Proton prefix, exactly what #1338 wanted to prevent.
"""

from __future__ import annotations

import pytest
from test_backends_arknights import _backend, _install_launcher

from nadare.backends import winegame as wg


def test_whitespace_wine_binary_still_means_the_default_proton(tmp_path, monkeypatch):
    prefix = tmp_path / "prefix"
    _install_launcher(prefix)
    pinned = tmp_path / "runners" / "dwproton-11.0-16"
    pinned.mkdir(parents=True)
    (pinned / "proton").write_text("")

    monkeypatch.setattr(
        wg.runners, "installed_pinned", lambda family=wg.runners.DEFAULT_FAMILY: pinned
    )
    monkeypatch.setattr(wg.runners, "ensure_pinned", lambda *a, **k: pinned)
    monkeypatch.setattr(wg.shutil, "which", lambda name, *_a, **_k: None)
    monkeypatch.setattr(wg.sandbox, "in_flatpak", lambda: False)
    monkeypatch.setattr(wg.umu, "system", lambda *a, **k: None)
    monkeypatch.setattr(wg.umu, "managed", lambda: None)
    monkeypatch.setattr(wg.umu, "resolve", lambda *a, **k: "/usr/bin/umu-run")

    def no_random_wine():
        pytest.fail("an empty wine_binary ran a random plain Wine on the Proton prefix")

    monkeypatch.setattr(
        "nadare.backends.winegame_parts.launching.first_wine", no_random_wine
    )

    backend = _backend(tmp_path, {"prefix": str(prefix), "wine_binary": "   "})
    argv, env, _target = backend.build_launch()

    assert argv[0] == "/usr/bin/umu-run"
    assert env["PROTONPATH"] == str(pinned)
