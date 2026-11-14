# SPDX-License-Identifier: GPL-3.0-or-later
"""A NUL in a profile's recorded folder is damaged data, not a crash in Find copies."""

from __future__ import annotations

import json

from nadare.core import gamefinder
from nadare.core.config import Config


def test_find_copies_ignores_a_recorded_folder_with_nul(tmp_path):
    path = tmp_path / "config.json"
    path.write_text(
        json.dumps(
            {
                "active_profile": "a",
                "profiles": {
                    "a": {
                        "backend": "direct-bd2",
                        "owned_dirs": ["/tmp/a\x00b"],
                    }
                },
            }
        ),
        encoding="utf-8",
    )
    config = Config.load(path)
    print("owned_dirs:", repr(config.profile("a")["owned_dirs"]))

    copies = gamefinder.find_copies(config, "a", None)

    print("copies:", copies)
    assert copies == []
