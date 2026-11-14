# SPDX-License-Identifier: GPL-3.0-or-later
"""W4-F8: a profile with only a prefix must be size-checked where the backend installs."""

from __future__ import annotations

from pathlib import Path

from nadare.core import backends
from nadare.core.config import Config
from nadare.gui.main_window_parts.confirm import download_folder


def test_download_folder_matches_the_backends_install_folder(tmp_path):
    config = Config.load(tmp_path / "config.json")
    prefix = tmp_path / "prefix"
    prefix.mkdir()
    config.add_profile(
        "bd2", backend="direct-bd2", title="Brown Dust 2", prefix=str(prefix)
    )
    backend = backends.create("bd2", config)
    if not callable(getattr(backend, "install_dir", None)):
        return  # a kind with no install folder has nothing to compare
    got = Path(download_folder("direct-bd2", config.profiles["bd2"]))
    want = backend.install_dir()
    assert got == want, (got, want, config.profiles["bd2"])
