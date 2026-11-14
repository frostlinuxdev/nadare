# SPDX-License-Identifier: GPL-3.0-or-later
"""W10-F5: a valid Kuro predownload version containing '+' cannot name its cache folder."""
from __future__ import annotations

from nadare.backends import direct
from nadare.core.backends import BackendContext
from nadare.core.config import Config


def test_a_plus_version_is_a_usable_predownload_cache_name(tmp_path):
    raw = {"profiles": {"pgr": {"backend": "direct-pgr", "install_dir": str(tmp_path / "install")}}}
    cfg = Config(path=tmp_path / "config.json", data=Config._normalise(raw))
    backend = direct.PgrBackend(BackendContext(name="pgr", profile=cfg.profile("pgr"), config=cfg))
    version = "3.8.0+somepack-3.8.1"
    cache = backend._predownload_cache(version)
    print("cache:", cache)
    assert cache.name == version
