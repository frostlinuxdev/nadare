# SPDX-License-Identifier: GPL-3.0-or-later
"""A probe that cannot answer is "unknown", and unknown must count as blocked (#1338)."""

from __future__ import annotations

from nadare.core import userns


def test_an_exception_from_the_probe_counts_as_blocked(monkeypatch) -> None:
    monkeypatch.setattr(userns, "restricted", lambda: True)
    monkeypatch.setattr(userns, "profile_installed", lambda: False)

    def boom(*_a, **_k):
        raise RuntimeError("the probe could not run")

    monkeypatch.setattr(userns, "probe", boom)
    # blocks_runtime() documents "Unknown ... counts as blocked"; an exception is unknown.
    assert userns.blocks_runtime() is True
