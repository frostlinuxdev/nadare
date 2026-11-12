# SPDX-License-Identifier: GPL-3.0-or-later
"""A Nadare-owned prefix without drive_c must still be removed by uninstall()."""

from nadare.backends.czn.backend import PREFIX_MARKER, CznBackend
from nadare.core.backends import BackendContext
from nadare.core.config import Config


def test_uninstall_removes_a_marked_prefix_without_drive_c(tmp_path, monkeypatch):
    prefix = tmp_path / "wine"
    prefix.mkdir()
    (prefix / PREFIX_MARKER).write_text("created by Nadare\n")
    (prefix / "czn-runner").write_text("GE-Proton9-20\n")
    (prefix / "leftover.bin").write_text("half-built")

    cfg = Config.load(tmp_path / "config.json")
    cfg.add_profile("stove-czn", backend="stove-czn", prefix=str(prefix))
    backend = CznBackend(
        BackendContext("stove-czn", cfg.profile("stove-czn"), cfg)
    )
    monkeypatch.setattr(backend, "_running", lambda: False)

    backend.uninstall()

    assert not prefix.exists(), "a Nadare-created prefix was left behind"
