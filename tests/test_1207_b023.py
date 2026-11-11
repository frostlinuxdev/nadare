# SPDX-License-Identifier: GPL-3.0-or-later
"""Log out must not leave the saved NIKKE session behind through a linked folder."""

from __future__ import annotations

from nadare.backends.launcher import NikkeBackend
from nadare.backends.nikke import hosts as h
from nadare.core.backends import BackendContext
from nadare.core.config import Config


def _exe(path, data="MZ"):
    path.parent.mkdir(parents=True, exist_ok=True)
    path.write_text(data)
    path.chmod(0o755)
    return path


def _backend(tmp_path):
    prefix = tmp_path / "prefix"
    _exe(prefix / "drive_c" / "NIKKE" / "Launcher" / "nikke_launcher.exe")
    values = {"backend": "launcher-nikke", "prefix": str(prefix), "wine_binary": "/usr/bin/wine"}
    cfg = Config(
        path=tmp_path / "config.json", data=Config._normalise({"profiles": {"test": values}})
    )
    return NikkeBackend(BackendContext(name="test", profile=cfg.profile("test"), config=cfg))


def test_logout_removes_a_linked_session_cache(tmp_path):
    backend = _backend(tmp_path)
    storage = h.cache_storage_dir(backend.prefix())
    storage.mkdir(parents=True, exist_ok=True)
    (storage / "intl_auth.txt").write_text("prefix session", encoding="utf-8")

    target = tmp_path / "relocated-session"
    (target / "mmkv").mkdir(parents=True)
    (target / "intl_auth.txt").write_text("saved", encoding="utf-8")
    (target / "mmkv" / "mmkv.default").write_text("device", encoding="utf-8")
    cache = backend._session_cache()
    cache.parent.mkdir(parents=True, exist_ok=True)
    cache.symlink_to(target, target_is_directory=True)

    backend.logout()

    assert not h.has_cache(cache)
    assert backend.status().data["logged_in"] is False
