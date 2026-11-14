# SPDX-License-Identifier: GPL-3.0-or-later
# ruff: noqa: F811 - tests take the fixtures imported below as arguments
"""Using a found copy must not re-point a game that is running."""

from __future__ import annotations

from types import SimpleNamespace

from _out_of_order_helpers import (  # noqa: F401 - fixtures used by name
    make_window,
    no_background_work,
    qapp,
)


def test_use_this_folder_is_refused_while_the_game_runs(
    tmp_path, qapp, monkeypatch, no_background_work
):
    old = tmp_path / "old"
    (old / "install").mkdir(parents=True)
    (old / "prefix").mkdir()
    new = tmp_path / "new"
    (new / "install").mkdir(parents=True)
    (new / "prefix" / "drive_c").mkdir(parents=True)
    window, config = make_window(
        tmp_path,
        {
            "g": {
                "backend": "wine",
                "title": "G",
                "install_dir": str(old / "install"),
                "prefix": str(old / "prefix"),
            }
        },
    )
    try:
        window.open_game("g")
        from nadare.core.backends import Status

        window._on_result("g", Status(state="running", running=True, message="Running"))
        assert window.game._status.state == "running"

        copy = SimpleNamespace(game_dir=new.resolve())
        window._found_copies = {"g": [copy]}
        used: list[object] = []
        monkeypatch.setattr(
            "nadare.core.gamefinder.use_and_save",
            lambda *args, **kwargs: used.append(args),
        )
        monkeypatch.setattr(
            type(window), "_choose_found_copy", lambda self, name, copies: copies[0]
        )
        window._use_found_copy("g")
        print("use_and_save calls:", used)
        print("install_dir after:", config.profiles["g"]["install_dir"])
        assert used == [], "Use this folder re-pointed the profile of a running game"
    finally:
        window.close()
