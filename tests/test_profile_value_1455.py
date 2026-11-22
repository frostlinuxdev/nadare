# SPDX-License-Identifier: GPL-3.0-or-later
"""#1455 R4-11 (from #1304): one reader for a profile key (top level, else ``extra``).

``core/runners.py`` kept a copy and ``gui/steam_wiring.py`` another that fell through to
``extra`` on any falsy top-level value, unlike the backends."""

from __future__ import annotations

from nadare.core import config, runners
from nadare.gui import steam_wiring


def test_runners_reads_with_the_config_reader():
    assert runners.profile_value is config.profile_value


def test_steam_wiring_agrees_with_the_backends():
    profile = {"steam_binary": " /usr/bin/steam ", "extra": {"steam_binary": "other"}}
    assert steam_wiring.profile_value(profile, "steam_binary") == "/usr/bin/steam"
    assert steam_wiring.profile_value({"extra": {"steam_binary": "x"}}, "steam_binary") == "x"
    assert steam_wiring.profile_value({}, "steam_binary") == ""
    # a real top-level value wins, even a falsy one the backends would use
    assert steam_wiring.profile_value({"k": 0, "extra": {"k": 5}}, "k") == "0"
