# SPDX-License-Identifier: GPL-3.0-or-later
"""A build without the Steam backend must not silently offer Steam tiles."""

from __future__ import annotations

from nadare.core import backends
from nadare.core.storecatalog import entries, unavailable_games


def test_a_missing_steam_backend_is_not_silently_offered(monkeypatch):
    monkeypatch.setattr(backends, "unavailable", lambda: ["steam"])
    offered = {e.kind for e in entries() if e.kind.startswith("steam-")}
    named = {kind for kind, _title in unavailable_games()}
    assert not offered or offered <= named
