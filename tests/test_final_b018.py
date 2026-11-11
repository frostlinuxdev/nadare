# SPDX-License-Identifier: GPL-3.0-or-later
"""uninstall() without purge must keep the profile's login/session data."""

from nadare.backends.czn.backend import CznBackend
from nadare.core import paths
from nadare.core.backends import BackendContext
from nadare.core.config import Config


def test_uninstall_without_purge_keeps_login_when_prefix_is_the_profile_dir(
    tmp_path, monkeypatch
):
    name = "stove-czn"
    data = paths.profiles_dir() / name
    cfg = Config.load(tmp_path / "config.json")
    cfg.add_profile(name, backend="stove-czn", prefix=str(data))
    backend = CznBackend(BackendContext(name=name, profile=cfg.profile(name), config=cfg))

    (data / "drive_c").mkdir(parents=True, exist_ok=True)
    session = data / "session.json"
    session.write_text('{"refresh_token": "live"}')
    monkeypatch.setattr(backend, "_running", lambda: False)

    backend.uninstall()  # purge=False: user data must be kept

    assert session.exists(), "uninstall() without purge deleted the saved login"
