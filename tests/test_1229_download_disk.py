# SPDX-License-Identifier: GPL-3.0-or-later
"""#1229: the download-size question checks the disk the game's files really go to."""

from __future__ import annotations

import pytest

pytest.importorskip("PyQt6.QtWidgets")

from nadare.core import paths  # noqa: E402
from nadare.gui import main_window  # noqa: E402


@pytest.mark.parametrize("kind", ["stove-czn", "launcher-nikke"])
def test_a_prefix_game_is_checked_where_its_prefix_is(kind):
    data = {"prefix": "/big/Nadare/G/prefix", "install_dir": "/somewhere/else"}
    assert main_window.download_folder(kind, data) == "/big/Nadare/G/prefix"


def test_a_direct_game_is_checked_where_its_files_go():
    data = {"prefix": "/a/prefix", "install_dir": "/b/install"}
    assert main_window.download_folder("direct-bd2", data) == "/b/install"


def test_nothing_set_means_nadares_own_folder():
    assert main_window.download_folder("direct-bd2", {}) == str(paths.profiles_dir())
    assert main_window.download_folder("stove-czn", {}) == str(paths.profiles_dir())
