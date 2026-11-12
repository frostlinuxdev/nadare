# SPDX-License-Identifier: GPL-3.0-or-later
"""A regular file can never be a game's Wine prefix."""

from pathlib import Path

from nadare.core import prefixguard


def test_an_existing_regular_file_is_not_accepted_as_a_wine_prefix():
    home = Path.home()
    target = home / "Games" / "not-a-folder"
    target.parent.mkdir(parents=True)
    target.write_text("x", encoding="utf-8")
    assert prefixguard.unsafe_reason(target, as_prefix=True) != ""
