# SPDX-License-Identifier: GPL-3.0-or-later
"""A system umu-run whose --version fails is unusable; the managed copy must win (#1338)."""

from __future__ import annotations

from types import SimpleNamespace

from nadare.core import umu


def test_a_system_umu_that_fails_version_does_not_shadow_the_managed_copy(
    tmp_path, monkeypatch
) -> None:
    managed = tmp_path / "managed" / "umu-run"
    managed.parent.mkdir()
    managed.write_text("#!/usr/bin/python3\n")
    monkeypatch.setattr(umu, "managed", lambda: managed)

    def broken(cmd, **_kwargs):
        return SimpleNamespace(
            returncode=1, stdout="", stderr="Traceback: No module named 'umu'"
        )

    found = umu.system("umu-run", which=lambda _n: "/usr/bin/umu-run", run=broken)
    # A nonzero --version is not merely an unreadable banner: the binary does not run.
    assert found is None
    assert umu.resolve(allow_download=False) == str(managed)
