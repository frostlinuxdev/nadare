# SPDX-License-Identifier: GPL-3.0-or-later
"""Removing an unused resource tier waits for the install folder's lock (#1474).

Settings → Disk space removes WuWa's other quality tiers on a pool thread. A download, update or
predownload of the same game holds the install-folder lock while it writes there; the cleanup
took no lock, so it could delete files under a running sync. It now holds the same lock.
"""

from __future__ import annotations

import pytest

from nadare.backends.direct import WutheringWavesBackend
from nadare.core import directdl, installlock
from nadare.core.backends import BackendContext, BackendError
from nadare.core.config import Config


@pytest.fixture
def wuwa(tmp_path, monkeypatch):
    cfg = Config.load(tmp_path / "config.json")
    install = tmp_path / "game"
    tier = install / "old-tier"
    tier.mkdir(parents=True)
    (tier / "synthetic.bin").write_bytes(b"synthetic old tier")
    profile = cfg.add_profile("sample", backend="direct-wuwa", install_dir=str(install))
    backend = WutheringWavesBackend(BackendContext("sample", profile, cfg))
    monkeypatch.setattr(backend, "_refuse_if_running", lambda *a: None)
    monkeypatch.setattr(backend, "other_quality_folders", lambda: [("sd", tier, 18)])
    monkeypatch.setattr(backend, "_recorded_tiers", lambda: None)
    return backend, install, tier


def test_a_tier_is_not_removed_while_a_sync_holds_the_folder(wuwa):
    backend, install, tier = wuwa
    with installlock.hold(install, directdl.DirectError, "a sync holds the folder"):
        with pytest.raises(BackendError, match="(?i)download|update|busy|finish"):
            backend.remove_other_qualities(only=["sd"])
    assert (tier / "synthetic.bin").read_bytes() == b"synthetic old tier"


def test_the_tier_is_removed_once_the_folder_is_free(wuwa):
    backend, install, tier = wuwa
    with installlock.hold(install, directdl.DirectError, "a sync holds the folder"):
        pass
    freed, warnings = backend.remove_other_qualities(only=["sd"])
    assert not tier.exists() and freed == 18 and warnings == []


def test_a_sync_cannot_start_while_the_cleanup_holds_the_folder(wuwa, monkeypatch):
    """The other direction: the cleanup's lock is the one syncs take."""
    backend, install, tier = wuwa
    seen: list[str] = []
    real = backend.other_quality_folders

    def during_cleanup():
        with pytest.raises(directdl.DirectError):
            with installlock.hold(install, directdl.DirectError, "busy"):
                pass
        seen.append("refused")
        return real()

    monkeypatch.setattr(backend, "other_quality_folders", during_cleanup)
    backend.remove_other_qualities(only=["sd"])
    assert seen == ["refused"]
