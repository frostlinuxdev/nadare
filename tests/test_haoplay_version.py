# SPDX-License-Identifier: GPL-3.0-or-later
"""Haoplay update identity includes both launcher-reported versions."""

from __future__ import annotations

import json
from pathlib import Path

from nadare.core.sources import gfl2

FIXTURES = Path(__file__).parent / "fixtures" / "catalog"
FLEX_URL = "https://gf2-launcher-intl.haoplay.com/flexible_config"


def _plan_with_client_version(client_version: str):
    doc = json.loads((FIXTURES / "gfl2_haoplay_flexible_config.json").read_text())
    data = doc["data"]["flexible_config"]
    plug = json.loads(data["nexon_plug"])
    plug["game_client_version"] = client_version
    data["nexon_plug"] = json.dumps(plug)
    payload = json.dumps(doc).encode()
    return gfl2.resolve_haoplay(lambda url, _headers: payload if url == FLEX_URL else None)


def test_plan_version_changes_when_only_client_version_changes():
    first = _plan_with_client_version("2.0.3936")
    second = _plan_with_client_version("2.0.3937")

    assert first.version == "2.0.3936.14886.28279+client-2.0.3936"
    assert second.version == "2.0.3936.14886.28279+client-2.0.3937"
    assert first.version != second.version
    assert first.files == second.files == ()
    assert first.archives[0].source.url != second.archives[0].source.url
