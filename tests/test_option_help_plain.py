# SPDX-License-Identifier: GPL-3.0-or-later
"""Settings help is written for players: no raw environment variable names."""

from __future__ import annotations

import re

from nadare.core import backends


def test_no_option_help_is_an_environment_variable_name():
    backends.load_builtins()
    shouty = re.compile(r"\b(WINEPREFIX|PROTONPATH|STEAM_COMPAT_DATA_PATH)\b")
    offenders = [
        f"{kind}.{option.key}"
        for kind in backends.kinds()
        if backends.is_available(kind)
        for option in backends.get(kind).options
        if shouty.search(option.help or "")
    ]
    assert offenders == []


def test_the_prefix_help_says_what_the_folder_is():
    assert "C: drive" in backends.PREFIX_HELP and "saves" in backends.PREFIX_HELP
