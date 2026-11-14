# SPDX-License-Identifier: GPL-3.0-or-later
# ruff: noqa: F811 - tests take the fixtures imported below as arguments
"""A late success for a removed profile must not land on a re-added game with the same name."""

from __future__ import annotations

from types import SimpleNamespace

from _out_of_order_helpers import (  # noqa: F401 - fixtures used by name
    make_window,
    no_background_work,
    qapp,
)


def _remove_and_readd(window, config):
    window._worker_generations["g"] = window._profile_generation("g")
    window._on_game_removed("g", SimpleNamespace(profile_kept=False, warnings=[]))
    assert "g" not in config.profiles
    config.add_profile("g", backend="wine", title="New game")
    config.save()
    window.refresh_library()
    window.open_game("g")


def test_a_stale_success_does_not_finish_the_replacement(
    tmp_path, qapp, monkeypatch, no_background_work
):
    window, config = make_window(tmp_path, {"g": {"backend": "wine", "title": "Old game"}})
    try:
        window.open_game("g")
        _remove_and_readd(window, config)
        finished: list[tuple[str, int]] = []
        monkeypatch.setattr(window.game, "finish", lambda *args: finished.append(args))
        monkeypatch.setattr(window, "_start", lambda *args, **kwargs: True)
        window._on_succeeded("g", "install", 0)
        print("game.finish calls:", finished)
        assert finished == [], "the old profile's install success finished the new game"
    finally:
        window.close()


def test_a_stale_cancel_does_not_clear_the_replacement(
    tmp_path, qapp, monkeypatch, no_background_work
):
    window, config = make_window(tmp_path, {"g": {"backend": "wine", "title": "Old game"}})
    try:
        window.open_game("g")
        _remove_and_readd(window, config)
        cancelled: list[int] = []
        monkeypatch.setattr(window.game, "cancelled", lambda: cancelled.append(1))
        window._on_cancelled("g", "install")
        print("game.cancelled calls:", cancelled)
        assert cancelled == [], "the old profile's cancel cleared the new game's page"
    finally:
        window.close()
