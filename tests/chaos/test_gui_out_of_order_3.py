# SPDX-License-Identifier: GPL-3.0-or-later
# ruff: noqa: F811 - tests take the fixtures imported below as arguments
"""The found-copy banner is navigation too: it must respect unsaved Settings edits."""

from __future__ import annotations

from pathlib import Path

from _out_of_order_helpers import (  # noqa: F401 - fixtures used by name
    make_window,
    no_background_work,
    qapp,
)

from nadare.core.gamefinder import Copy


def test_review_found_copy_asks_before_leaving_edited_settings(
    tmp_path, qapp, monkeypatch, no_background_work
):
    window, _config = make_window(
        tmp_path,
        {"g": {"backend": "wine", "title": "G"}, "h": {"backend": "wine", "title": "H"}},
    )
    try:
        window.open_game("g")
        window.open_settings()
        window.settings.title.setText("an unsaved title")
        assert window.settings._has_unsaved_changes()

        asked: list[int] = []
        monkeypatch.setattr(
            window.settings, "confirm_leave", lambda: asked.append(1) or False
        )
        copy = Copy(
            game_dir=tmp_path / "h-copy",
            library=None,
            install_dir=None,
            prefix=Path(tmp_path / "h-copy" / "prefix"),
            made_by_nadare=(),
            version=None,
            complete=False,
            recorded=False,
            from_manifest=False,
            kind="wine",
        )
        window._found_copies = {"h": [copy]}
        window._review_found_copies()
        print("confirm_leave called:", asked)
        print("page shown:", window.stack.currentIndex(), "profile:", window._current_profile)
        assert asked == [1], "Review switched away from edited Settings without asking"
        assert window._current_profile == "g"
    finally:
        window.close()
