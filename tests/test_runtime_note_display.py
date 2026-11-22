# SPDX-License-Identifier: GPL-3.0-or-later
"""#675: the launch log names the display Proton actually gets, not just the backend default.

With Display on "auto" a PROTON_ENABLE_WAYLAND the user exported is passed on (setdefault), and
without a Wayland session Proton runs on X11 whatever is asked; the note said the default.
"""

from __future__ import annotations

import pytest
from _backend_helpers import make_context, write_exe

from nadare.backends.winegame import WineGameBackend


def _backend(tmp_path, monkeypatch, display="auto"):
    wine = write_exe(tmp_path / "wine")
    ctx = make_context(tmp_path, "wine", {"wine_binary": str(wine), "display": display})
    backend = WineGameBackend(ctx)
    monkeypatch.setattr(backend, "resolve_runner", lambda: (None, "proton"))
    return backend


@pytest.fixture(autouse=True)
def _wayland_session(monkeypatch):
    monkeypatch.setenv("WAYLAND_DISPLAY", "wayland-0")
    monkeypatch.delenv("PROTON_ENABLE_WAYLAND", raising=False)


def test_auto_follows_a_value_the_user_exported(tmp_path, monkeypatch):
    backend = _backend(tmp_path, monkeypatch)  # WineGameBackend defaults to wayland
    monkeypatch.setenv("PROTON_ENABLE_WAYLAND", "0")
    assert "display: x11" in backend._runtime_note()


def test_an_explicit_choice_wins_over_the_exported_value(tmp_path, monkeypatch):
    backend = _backend(tmp_path, monkeypatch, display="wayland")
    monkeypatch.setenv("PROTON_ENABLE_WAYLAND", "0")
    assert "display: wayland" in backend._runtime_note()


def test_without_a_wayland_session_it_says_x11(tmp_path, monkeypatch):
    backend = _backend(tmp_path, monkeypatch, display="wayland")
    monkeypatch.delenv("WAYLAND_DISPLAY")
    assert "display: x11 (no Wayland session)" in backend._runtime_note()


def test_the_default_is_still_reported_when_nothing_overrides_it(tmp_path, monkeypatch):
    backend = _backend(tmp_path, monkeypatch)
    assert "display: wayland" in backend._runtime_note()


def test_a_profile_extra_env_wins_over_the_display_choice(tmp_path, monkeypatch):
    from _backend_helpers import make_context

    wine = write_exe(tmp_path / "wine")
    ctx = make_context(
        tmp_path,
        "wine",
        {
            "wine_binary": str(wine),
            "display": "wayland",
            "extra": {"extra_env": {"PROTON_ENABLE_WAYLAND": "0"}},
        },
    )
    backend = WineGameBackend(ctx)
    monkeypatch.setattr(backend, "resolve_runner", lambda: (None, "proton"))
    assert "display: x11" in backend._runtime_note()


# #1299: the default display is code, not data; a release that flips a game's default changes old
# profiles without a word. The launch log now says where the display choice came from.
def test_the_note_says_the_display_is_the_games_default(tmp_path, monkeypatch):
    backend = _backend(tmp_path, monkeypatch)
    assert "display from: this game's default" in backend._runtime_note()


@pytest.mark.parametrize("choice", ["wayland", "x11", "xwayland"])
def test_the_note_says_the_display_is_the_players_setting(tmp_path, monkeypatch, choice):
    backend = _backend(tmp_path, monkeypatch, display=choice)
    assert "display from: the game's Display setting" in backend._runtime_note()


def test_the_note_names_an_exported_variable(tmp_path, monkeypatch):
    backend = _backend(tmp_path, monkeypatch)
    monkeypatch.setenv("PROTON_ENABLE_WAYLAND", "0")
    assert "display from: PROTON_ENABLE_WAYLAND in Nadare's environment" in backend._runtime_note()


def test_the_note_names_the_profile_environment(tmp_path, monkeypatch):
    wine = write_exe(tmp_path / "wine")
    ctx = make_context(
        tmp_path,
        "wine",
        {"wine_binary": str(wine), "extra": {"extra_env": {"PROTON_ENABLE_WAYLAND": "0"}}},
    )
    backend = WineGameBackend(ctx)
    monkeypatch.setattr(backend, "resolve_runner", lambda: (None, "proton"))
    assert "display from: the game's Environment setting" in backend._runtime_note()
