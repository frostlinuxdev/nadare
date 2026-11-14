# SPDX-License-Identifier: GPL-3.0-or-later
# ruff: noqa: F811 - tests take the fixtures imported below as arguments
"""Removing a game withdraws the 'found a copy' offer for it."""

from __future__ import annotations

from types import SimpleNamespace

from _out_of_order_helpers import (  # noqa: F401 - fixtures used by name
    make_window,
    no_background_work,
    qapp,
)

from nadare.core.gamefinder import Copy


def test_removal_hides_the_offer_and_the_banner(tmp_path, qapp, no_background_work):
    window, config = make_window(tmp_path, {"g": {"backend": "wine", "title": "G"}})
    try:
        real = tmp_path / "real-copy"
        real.mkdir()
        copy = Copy(
            game_dir=real,
            library=None,
            install_dir=None,
            prefix=real / "prefix",
            made_by_nadare=(),
            version="1.0",
            complete=True,
            recorded=False,
            from_manifest=False,
            kind="wine",
        )
        window._show_found_copies({"g": [copy]})
        assert not window.found_banner.isHidden()
        window._on_game_removed("g", SimpleNamespace(profile_kept=False, warnings=[]))
        print("profiles:", config.profiles)
        print("offers:", window._found_copies)
        print("banner hidden:", window.found_banner.isHidden())
        assert "g" not in window._found_copies, "the removed game still has a found-copy offer"
        assert window.found_banner.isHidden(), "the banner still offers the removed game"
    finally:
        window.close()
