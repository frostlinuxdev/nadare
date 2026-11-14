# SPDX-License-Identifier: GPL-3.0-or-later
"""W9-2: CZN must accept the auto/latest aliases Settings accepts and core resolves."""

from __future__ import annotations

from _backend_helpers import make_context

from nadare.backends.czn.backend import CznBackend
from nadare.core import runners


def _make_runner(root, name):
    path = root / name
    path.mkdir(parents=True)
    (path / "proton").write_text("#!/bin/sh\n")
    return path


def test_czn_resolves_auto_like_the_rest_of_nadare(tmp_path, monkeypatch):
    root = tmp_path / "runners"
    root.mkdir()
    expected = _make_runner(root, "GE-Proton11-7-x86_64")
    monkeypatch.setattr(runners.paths, "runners_dir", lambda: root)
    monkeypatch.setattr(runners, "steam_runners", lambda: [])

    core_answer = runners.resolve_runner("auto")
    backend = CznBackend(
        make_context(tmp_path, "stove-czn", {"runner": "auto", "prefix": str(tmp_path / "pfx")})
    )
    try:
        czn_answer, kind = backend.resolve_runner()
    except Exception as exc:  # noqa: BLE001 - the failure is the finding
        print("core 'auto' ->", core_answer, "| CZN raised:", type(exc).__name__, exc)
        raise
    print("core 'auto' ->", core_answer, "| CZN ->", (czn_answer, kind))
    assert kind == "proton"
    assert czn_answer == str(expected), "CZN did not resolve 'auto' to the installed runner"
