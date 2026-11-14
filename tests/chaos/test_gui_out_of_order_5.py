# SPDX-License-Identifier: GPL-3.0-or-later
# ruff: noqa: F811 - tests take the fixtures imported below as arguments
"""A profile removed in another process must lose its paused download too."""

from __future__ import annotations

from _out_of_order_helpers import (  # noqa: F401 - fixtures used by name
    make_window,
    no_background_work,
    qapp,
)

from nadare.core.config import Config


def test_disk_removal_forgets_the_paused_download(
    tmp_path, qapp, no_background_work
):
    window, config = make_window(tmp_path, {"g": {"backend": "wine", "title": "G"}})
    try:
        window.controller._paused["g"] = ("install", None)
        other = Config.load(config.path)
        other.remove_profile("g")
        other.save()

        window._sync_profiles_from_disk()
        print("profiles:", config.profiles)
        print("paused operations:", window.controller._paused)
        assert "g" not in config.profiles
        assert not window.controller.is_paused("g"), (
            "a game removed by another process still has a resumable download"
        )
    finally:
        window.close()
