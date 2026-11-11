# SPDX-License-Identifier: GPL-3.0-or-later
"""A lock-busy refusal must not delete the marker of the sync holding the lock."""

from __future__ import annotations

import json

import pytest
from _backend_helpers import make_context, write_exe

from nadare.backends import arknights as ak
from nadare.backends.arknights import ArknightsBackend
from nadare.core import yostar
from nadare.core.backends import BackendError


def test_a_lock_busy_refusal_keeps_the_other_syncs_marker(tmp_path, monkeypatch):
    manifest = tmp_path / "manifest.json"
    manifest.write_text(
        json.dumps(
            {
                "game_tag": "Arknights_EN",
                "version": "2.0",
                "base_url": "https://cdn.yo-star.com/assets",
                "generated_at": "2026-01-01T00:00:00Z",
                "files": [
                    {"path": "Arknights.exe", "size": 2, "crc64": yostar.crc64(b"MZ")}
                ],
            }
        ),
        encoding="utf-8",
    )
    backend = ArknightsBackend(
        make_context(tmp_path, ak.KIND, {"manifest_file": str(manifest)})
    )
    write_exe(backend.direct_target(), "MZ")  # finished install, no marker yet
    marker = backend._install_marker_path()
    assert not marker.exists()

    def busy(man, dest, **kw):
        # The other process's sync is running now: it wrote its marker (recording
        # the update it is doing) and it holds the install lock that refuses us.
        backend._write_install_marker(("1.0", "2.0"))
        raise yostar.SyncBusy("another install or update is already running")

    monkeypatch.setattr(ak.yostar, "sync", busy)
    with pytest.raises(BackendError):
        backend.update()

    assert marker.exists(), "the running sync's incomplete-marker was unlinked"


def test_the_real_install_lock_refuses_with_sync_busy(tmp_path, monkeypatch):
    monkeypatch.setenv("XDG_STATE_HOME", str(tmp_path / "state"))
    target = tmp_path / "game"
    target.mkdir()
    manifest = yostar.Manifest(
        game_tag="Arknights_EN", version="2.0", base_url="https://cdn.yo-star.com/assets",
        generated_at="2026-01-01T00:00:00Z", files=[], source=str(tmp_path / "m.json"),
    ) if hasattr(yostar, "Manifest") else None
    if manifest is None:
        pytest.skip("no Manifest type")
    with yostar._install_lock(target), pytest.raises(yostar.SyncBusy):
        yostar.sync(manifest, target)
