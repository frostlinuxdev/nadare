# SPDX-License-Identifier: GPL-3.0-or-later
from nadare.core import userns


def test_profile_file_is_not_proof_the_runtime_is_allowed(monkeypatch):
    monkeypatch.setattr(userns, "restricted", lambda: True)
    monkeypatch.setattr(userns, "profile_installed", lambda: True)
    monkeypatch.setattr(userns, "probe", lambda *a, **k: False)
    assert userns.blocks_runtime() is True
