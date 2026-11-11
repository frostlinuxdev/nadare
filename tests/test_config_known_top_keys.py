# SPDX-License-Identifier: GPL-3.0-or-later
"""#622: settings Nadare itself writes never make a profile-less config look broken.

``_missing_profiles_section`` treats a top-level key outside ``TOP_DEFAULTS`` as possible lost
data and keeps a copy with a notice (#365). Six keys Nadare reads and writes were missing from
that list, so e.g. ``{"default_monitor": "DP-1"}`` was reported as "has no profiles section".
"""

from __future__ import annotations

import json
import re
from pathlib import Path

import pytest

from nadare.core.config import KNOWN_TOP_KEYS, Config


@pytest.mark.parametrize(
    "raw",
    [
        {"schema": 1, "sgdb_api_key": "k"},
        {"default_monitor": "DP-1", "max_concurrent_installs": 2},
        {"output_expanded": True, "page_notice_expanded": False, "unofficial_notice_ack": True},
    ],
)
def test_nadares_own_settings_are_not_lost_data(tmp_path, raw):
    path = tmp_path / "config.json"
    path.write_text(json.dumps(raw), encoding="utf-8")
    cfg = Config.load(path)
    assert cfg.notice is None
    assert not list(tmp_path.glob("config.json.corrupt-*"))


def test_every_top_level_key_the_code_reads_is_known():
    pattern = re.compile(r'(?:config|cfg)\.data(?:\.get\(|\[|\.setdefault\(|\.pop\()"([a-z_]+)"')
    used: set[str] = set()
    for path in Path("src/nadare").rglob("*.py"):
        used |= set(pattern.findall(path.read_text(encoding="utf-8")))
    used |= {"sgdb_api_key"}  # read through Config.load().data in artwork.py
    assert used - KNOWN_TOP_KEYS == set()
