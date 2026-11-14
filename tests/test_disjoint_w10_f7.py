# SPDX-License-Identifier: GPL-3.0-or-later
"""W10-F7: the Steam backend accepts an app id with a leading zero, the Store cannot match it."""
from __future__ import annotations

from nadare.backends import steam as steam_backend
from nadare.core import steamstore
from nadare.core.backends import BackendContext
from nadare.core.config import Config


def test_an_accepted_steam_app_id_is_canonical_for_store_tiles(tmp_path):
    raw = {"profiles": {"e7": {"backend": "steam", "title": "E7", "steam_app_id": "05019180"}}}
    cfg = Config(path=tmp_path / "config.json", data=Config._normalise(raw))
    profile = cfg.profile("e7")
    backend = steam_backend.SteamBackend(
        BackendContext(name="e7", profile=profile, config=cfg)
    )
    accepted = backend._app_id()  # the backend accepts this (no error)
    kinds = steamstore.installed_kinds({"e7": profile})
    canonical = steamstore.kind_for("5019180")
    print("backend app id:", accepted, "installed kinds:", kinds, "store tile:", canonical)
    assert accepted == "5019180", "the backend accepts a form the Store cannot recognise"
    assert canonical in kinds
