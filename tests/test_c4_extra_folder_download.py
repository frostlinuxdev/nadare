# SPDX-License-Identifier: GPL-3.0-or-later
"""C4: the free-space question reads a folder set only under ``extra``, as the backends do."""

from __future__ import annotations

from pathlib import Path

import pytest

pytest.importorskip("PyQt6.QtWidgets")

from nadare.gui import main_window  # noqa: E402


@pytest.mark.parametrize("kind", ["stove-czn", "launcher-nikke"])
def test_download_folder_prefix_game_uses_an_extra_only_prefix(kind):
    data = {"extra": {"prefix": "/big/Nadare/G/prefix"}}
    assert main_window.download_folder(kind, data) == "/big/Nadare/G/prefix"


def test_download_folder_direct_game_uses_an_extra_only_install_folder():
    data = {"extra": {"install_dir": "/b/install"}}
    assert Path(main_window.download_folder("direct-bd2", data)) == Path("/b/install")
