# SPDX-License-Identifier: GPL-3.0-or-later
"""W9-4: CZN must not turn the tag GE-Proton9-1 into the newer GE-Proton9-10."""

from __future__ import annotations

from _backend_helpers import make_context

from nadare.backends.czn.backend import CznBackend
from nadare.core import runners


def _make_runner(root, name):
    path = root / name
    path.mkdir(parents=True)
    (path / "proton").write_text("#!/bin/sh\n")
    return path


def test_czn_tag_does_not_select_a_different_release(tmp_path, monkeypatch):
    root = tmp_path / "runners"
    root.mkdir()
    named = _make_runner(root, "GE-Proton9-1-x86_64")
    newer = _make_runner(root, "GE-Proton9-10-x86_64")
    monkeypatch.setattr(runners.paths, "runners_dir", lambda: root)
    monkeypatch.setattr(runners, "steam_runners", lambda: [])
    backend = CznBackend(
        make_context(tmp_path, "stove-czn", {"runner": "GE-Proton9-1",
                                             "prefix": str(tmp_path / "pfx")})
    )
    chosen, kind = backend.resolve_runner()
    print("tag GE-Proton9-1 ->", chosen, "| named:", named, "| newer:", newer)
    assert kind == "proton"
    assert chosen == str(named), (
        "CZN resolved the tag 'GE-Proton9-1' to a different installed build (9-10)"
    )
