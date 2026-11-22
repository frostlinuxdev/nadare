# SPDX-License-Identifier: GPL-3.0-or-later
"""CZN Play refuses when WebView2 is missing or broken, and points to Repair files.

Without WebView2 the game shows only its own "run the game through the launcher" error.
Play checks the files and the registry entry (no Wine, no download) and stops before
the update check and before a game token is minted.
"""

from __future__ import annotations

from pathlib import Path

import pytest
from _backend_helpers import make_context

from nadare.backends.czn.backend import CznBackend
from nadare.core.backends import BackendError

_APP = "drive_c/Program Files (x86)/Microsoft/EdgeWebView/Application"
_KEY = (
    r"[Software\\Wow6432Node\\Microsoft\\EdgeUpdate\\Clients\\"
    r"{F3017226-FE2A-4295-8BDF-00C3A9A7E4C5}]"
)


def _files(prefix: Path, version: str = "155.0.4283.45") -> None:
    folder = prefix / _APP / version
    folder.mkdir(parents=True, exist_ok=True)
    (folder / "msedgewebview2.exe").write_bytes(b"MZ")


def _register(prefix: Path, version: str = "155.0.4283.45") -> None:
    prefix.mkdir(parents=True, exist_ok=True)
    (prefix / "system.reg").write_text(f'WINE REGISTRY Version 2\n\n{_KEY} 1\n"pv"="{version}"\n')


@pytest.fixture
def play(tmp_path, monkeypatch):
    """A CZN backend whose launch is stubbed past the WebView2 check; records what ran."""
    ctx = make_context(tmp_path, "stove-czn", {"prefix": str(tmp_path / "prefix")})
    backend = CznBackend(ctx)
    seen: list[str] = []
    monkeypatch.delenv("CZN_SKIP_UPDATE", raising=False)
    monkeypatch.setattr(backend, "installed", lambda: True)
    monkeypatch.setattr(backend, "_game_running", lambda: False)
    monkeypatch.setattr(backend, "_run_module", lambda *a, **k: seen.append("update") or 0)
    monkeypatch.setattr(backend, "_restore_attempted", True)
    monkeypatch.setattr(backend, "_refresh_token", "token")
    monkeypatch.setattr(backend, "_mint_session", lambda: seen.append("mint") or 0)
    monkeypatch.setattr(backend, "_cleanup_stale_session", lambda: None)
    monkeypatch.setattr(backend, "_ensure_webview2", lambda: seen.append("download webview2"))
    monkeypatch.setattr(backend, "_ensure_stub", lambda: tmp_path / "stub")
    monkeypatch.setattr(backend, "_ensure_vcredist", lambda: None)
    monkeypatch.setattr(backend, "_prefix_command", lambda args, extra=None, **_kw: (["stub"], {}))
    monkeypatch.setattr(backend, "_stub_result", lambda log: "ok")
    monkeypatch.setattr(backend, "_stream", lambda cmd, env, **k: seen.append("game") or 0)
    backend.prefix().mkdir(parents=True, exist_ok=True)
    return backend, seen


def _refused(backend: CznBackend, seen: list[str]) -> str:
    with pytest.raises(BackendError) as info:
        backend.launch()
    message = info.value.message
    assert "WebView2" in message
    assert "Repair files" in message
    # Stopped before anything slow or sensitive: no update check, no token, no download.
    assert seen == []
    return message


def test_play_refuses_when_webview2_is_not_in_the_prefix(play):
    backend, seen = play
    _refused(backend, seen)


def test_play_refuses_when_the_files_are_there_but_not_registered(play):
    """A half-finished install leaves the exe without the registry entry (kill tests)."""
    backend, seen = play
    _files(backend.prefix())
    _refused(backend, seen)


def test_play_refuses_when_registered_but_the_files_are_gone(play):
    backend, seen = play
    _register(backend.prefix())
    _refused(backend, seen)


def test_play_refuses_when_the_registered_version_folder_is_missing(play):
    backend, seen = play
    _files(backend.prefix(), "150.0.0.1")
    _register(backend.prefix(), "155.0.4283.45")
    _refused(backend, seen)


def test_play_starts_the_game_when_webview2_is_installed_and_registered(play):
    backend, seen = play
    _files(backend.prefix())
    _register(backend.prefix())
    assert backend.launch() == 0
    assert seen == ["update", "mint", "game"]
