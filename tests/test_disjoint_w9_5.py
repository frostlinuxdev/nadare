# SPDX-License-Identifier: GPL-3.0-or-later
"""W9-5: cleanup must keep the installed fallback runner CZN actually launches with."""

from __future__ import annotations

from _backend_helpers import make_context

from nadare.backends.czn.backend import CznBackend
from nadare.core import runners
from nadare.core.backends import BackendError


def test_fallback_runner_is_not_offered_for_deletion(tmp_path, monkeypatch):
    root = tmp_path / "runners"
    root.mkdir()
    fallback = root / "dwproton-11.0-16-x86_64"
    fallback.mkdir()
    (fallback / "proton").write_text("#!/bin/sh\n")
    monkeypatch.setattr(runners.paths, "runners_dir", lambda: root)
    monkeypatch.setattr(runners, "steam_runners", lambda: [])

    profile = {"backend": "stove-czn", "runner": "dwproton", "prefix": str(tmp_path / "pfx")}
    backend = CznBackend(make_context(tmp_path, "stove-czn", profile))

    def offline(rid, latest):
        raise BackendError("offline (the pinned build is not installed)", 4)

    monkeypatch.setattr(backend, "_download_runner", offline)
    chosen, kind = backend.resolve_runner()
    stale = runners.unused_old_runners({"czn": profile})
    print("CZN launches with:", chosen, kind, "| cleanup would delete:", [p.name for p in stale])
    assert chosen == str(fallback), "fixture: CZN should fall back to the installed family build"
    assert fallback not in stale, (
        "cleanup offers the exact fallback runner CZN launches with for deletion"
    )
