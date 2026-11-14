# SPDX-License-Identifier: GPL-3.0-or-later
from nadare.core.config import TOP_DEFAULTS, Config


def test_top_level_list_defaults_are_not_shared_between_configs(tmp_path):
    first = Config.load(tmp_path / "a.json")
    second = Config.load(tmp_path / "b.json")
    assert first.data["dismissed_notices"] == []
    first.data["dismissed_notices"].append("notice-1")
    # A second, independent config (and the shared default table) must not see it.
    assert second.data["dismissed_notices"] == []
    assert TOP_DEFAULTS["dismissed_notices"] == []
