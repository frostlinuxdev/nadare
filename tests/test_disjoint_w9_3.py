# SPDX-License-Identifier: GPL-3.0-or-later
"""W9-3: a substring runner tag must mean the same build to CZN, WineGame and cleanup."""

from __future__ import annotations

from _backend_helpers import make_context

from nadare.backends.czn.backend import CznBackend
from nadare.core import runners


def test_ge_tag_resolves_to_the_same_runner_for_winegame(tmp_path, monkeypatch):
    root = tmp_path / "runners"
    root.mkdir()
    installed = root / "GE-Proton11-7-x86_64"
    installed.mkdir()
    (installed / "proton").write_text("#!/bin/sh\n")
    monkeypatch.setattr(runners.paths, "runners_dir", lambda: root)
    monkeypatch.setattr(runners, "steam_runners", lambda: [])

    czn = CznBackend(
        make_context(tmp_path, "stove-czn", {"runner": "GE-Proton11-7",
                                             "prefix": str(tmp_path / "pfx")})
    )
    czn_path, _kind = czn.resolve_runner()
    usage = runners.runner_usage({"czn": {"backend": "stove-czn", "runner": "GE-Proton11-7"}})
    try:
        winegame_path = runners.resolve_runner("GE-Proton11-7")
    except Exception as exc:  # noqa: BLE001 - the failure is the finding
        print("CZN ->", czn_path, "| cleanup usage ->", usage,
              "| WineGame resolve raised:", type(exc).__name__, exc)
        raise
    print("CZN ->", czn_path, "| WineGame ->", winegame_path,
          "| cleanup usage ->", usage)
    assert czn_path == str(installed), "fixture: CZN uses the tag by substring"
    assert winegame_path == installed, (
        "WineGame says the same tag is 'not found' even though CZN uses it and cleanup says in use"
    )
