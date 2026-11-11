# SPDX-License-Identifier: GPL-3.0-or-later
"""A Steam game in Nadare's Flatpak starts the host's Steam (maintainer, 2026-11-11).

Blue Archive (a Steam game) in the Flatpak showed "SET GAME FILE IN SETTINGS": the backend looked
for ``steam`` on the sandbox's PATH, found nothing, and called the game not installed. Steam lives
on the host (a distro package, or the Steam Flatpak); the Flatpak reaches it through
``flatpak-spawn --host``. Looking it up spawns a process, and the page asks for status every 30 s,
so the answer is cached.
"""

from __future__ import annotations

import pytest
from _backend_helpers import make_context

from nadare.backends import steam as steam_backend
from nadare.backends.steam import SteamBackend
from nadare.core import sandbox
from nadare.core.backends import BackendError


@pytest.fixture
def flatpak(monkeypatch, tmp_path):
    home = tmp_path / "home"
    home.mkdir(exist_ok=True)
    monkeypatch.setenv("HOME", str(home))
    monkeypatch.setattr(sandbox, "in_flatpak", lambda: True)
    monkeypatch.setattr(steam_backend.shutil, "which", lambda name, **kw: None)
    steam_backend.clear_host_client_cache()
    yield home
    steam_backend.clear_host_client_cache()


def _backend(tmp_path, **profile):
    return SteamBackend(make_context(tmp_path, "steam", {"steam_app_id": "3557620", **profile}))


def test_the_hosts_steam_is_started_through_flatpak_spawn(flatpak, tmp_path, monkeypatch):
    monkeypatch.setattr(sandbox, "host_which", lambda name: "/usr/bin/steam")
    argv, app_id = _backend(tmp_path).build()
    assert argv[:2] == ["flatpak-spawn", "--host"]
    assert "/usr/bin/steam" in argv and argv[-1] == "steam://rungameid/3557620"


def test_the_steam_flatpak_is_used_when_there_is_no_steam_program(flatpak, tmp_path, monkeypatch):
    monkeypatch.setattr(sandbox, "host_which", lambda name: None)
    (flatpak / ".var" / "app" / "com.valvesoftware.Steam").mkdir(parents=True)
    argv, _ = _backend(tmp_path).build()
    assert argv[:2] == ["flatpak-spawn", "--host"]
    assert argv[2:5] == ["flatpak", "run", "com.valvesoftware.Steam"]


def test_with_steam_on_the_host_the_game_is_launchable_not_set_game_file(
    flatpak, tmp_path, monkeypatch
):
    from nadare.core.gamefacts import primary_operation

    monkeypatch.setattr(sandbox, "host_which", lambda name: "/usr/bin/steam")
    status = _backend(tmp_path).status()
    assert status.state == "installed"
    assert primary_operation(status) == "launch"


def test_without_any_steam_the_message_says_so(flatpak, tmp_path, monkeypatch):
    monkeypatch.setattr(sandbox, "host_which", lambda name: None)
    status = _backend(tmp_path).status()
    assert "Steam was not found on this computer" in (status.message or "")
    assert "Steam Flatpak" in (status.message or "")
    with pytest.raises(BackendError, match="Steam"):
        _backend(tmp_path).build()


def test_the_host_lookup_is_cached_across_status_checks(flatpak, tmp_path, monkeypatch):
    calls = []
    monkeypatch.setattr(sandbox, "host_which", lambda name: calls.append(name) or "/usr/bin/steam")
    backend = _backend(tmp_path)
    for _ in range(5):
        backend.status()
    assert len(calls) == 1


def test_a_missing_steam_is_looked_up_again_later(flatpak, tmp_path, monkeypatch):
    """Installing Steam while Nadare runs must not need a restart."""
    answers = [None, "/usr/bin/steam"]
    monkeypatch.setattr(sandbox, "host_which", lambda name: answers.pop(0))
    now = [1000.0]
    monkeypatch.setattr(steam_backend.time, "monotonic", lambda: now[0])
    backend = _backend(tmp_path)
    with pytest.raises(BackendError):
        backend.build()
    now[0] += steam_backend.HOST_CLIENT_TTL + 1
    assert "/usr/bin/steam" in backend.build()[0]


def test_outside_a_flatpak_nothing_changes(monkeypatch, tmp_path):
    monkeypatch.setattr(sandbox, "in_flatpak", lambda: False)
    monkeypatch.setattr(steam_backend.shutil, "which", lambda name, **kw: "/usr/bin/steam")
    monkeypatch.setattr(sandbox, "host_which", lambda name: pytest.fail("asked the host"))
    argv, _ = _backend(tmp_path).build()
    assert argv == ["/usr/bin/steam", "steam://rungameid/3557620"]
