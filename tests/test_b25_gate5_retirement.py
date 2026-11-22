# SPDX-License-Identifier: GPL-3.0-or-later
"""Gate 5: an already-recorded retirement must not modify settings again."""

from __future__ import annotations

import json

from nadare.core import owngames
from nadare.core.config import Config


def test_completed_retirement_never_repeats_even_if_a_legacy_profile_reappears(tmp_path):
    path = tmp_path / "config.json"
    path.write_text(
        json.dumps(
            {
                "schema": 1,
                "profiles": {"legacy": {"backend": "wine", "title": "Restored settings"}},
                owngames.RETIRED_FLAG: True,
            }
        ),
        encoding="utf-8",
    )
    cfg = Config.load(path)
    before = path.read_bytes()
    assert owngames.retire(cfg) == ""
    assert "legacy" in cfg.profiles
    assert path.read_bytes() == before
    assert not list(tmp_path.glob("config.json.before-own-games-removed*"))
