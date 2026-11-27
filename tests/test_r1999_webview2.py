# SPDX-License-Identifier: GPL-3.0-or-later
"""Reverse: 1999's Google sign-in needs WebView2 in its own Wine prefix.

Without it the game asks to download WebView2 itself; under Proton that offer does nothing
and the sign-in page stays a blank spinner (a b29 test run, 2026-11-24).
"""

from __future__ import annotations

import pytest
from _backend_helpers import make_context

from nadare.backends import direct, direct_r1999
from nadare.core.backends import BackendError


def _backend(tmp_path):
    prefix = tmp_path / "r1999-prefix"
    return direct_r1999.R1999Backend(
        make_context(tmp_path, "direct-r1999", {"prefix": str(prefix)})
    )


def test_install_sets_up_webview2_in_the_prefix_before_sync(tmp_path, monkeypatch):
    backend = _backend(tmp_path)
    calls = []
    monkeypatch.setattr(backend, "_ensure_default_runner", lambda strict: None)
    monkeypatch.setattr(backend, "_ensure_direct_prefix", lambda: calls.append("prefix"))
    monkeypatch.setattr(
        direct_r1999.R1999Backend,
        "_ensure_webview2",
        lambda self: calls.append(("webview2", self.prefix())),
    )
    monkeypatch.setattr(backend, "_sync_direct", lambda: calls.append("sync"))

    backend.install()

    assert calls == ["prefix", ("webview2", backend.prefix()), "sync"]


def test_install_stops_when_webview2_setup_fails(tmp_path, monkeypatch):
    backend = _backend(tmp_path)
    monkeypatch.setattr(backend, "_ensure_default_runner", lambda strict: None)
    monkeypatch.setattr(backend, "_ensure_direct_prefix", lambda: None)
    monkeypatch.setattr(
        direct_r1999.R1999Backend,
        "_ensure_webview2",
        lambda self: (_ for _ in ()).throw(BackendError("WebView2 setup failed")),
    )
    monkeypatch.setattr(backend, "_sync_direct", lambda: pytest.fail("sync ran after failure"))

    with pytest.raises(BackendError, match="WebView2 setup failed"):
        backend.install()


def test_an_existing_install_gets_webview2_before_launch(tmp_path, monkeypatch):
    backend = _backend(tmp_path)
    target = backend.install_dir() / backend.direct_exe
    target.parent.mkdir(parents=True)
    target.write_bytes(b"MZ")
    calls = []
    monkeypatch.setattr(backend, "_ensure_default_runner", lambda strict: None)
    monkeypatch.setattr(
        direct_r1999.R1999Backend, "_ensure_webview2", lambda self: calls.append("webview2")
    )
    monkeypatch.setattr(backend, "_launch_direct", lambda: calls.append("launch") or 0)

    assert backend.launch() == 0
    assert calls == ["webview2", "launch"]


def test_launch_passes_the_webview2_browser_flags(tmp_path, monkeypatch):
    backend = _backend(tmp_path)
    monkeypatch.setattr(backend, "resolve_runner", lambda: ("/opt/proton", "proton"))
    monkeypatch.setattr("nadare.core.umu.system", lambda configured="umu-run": "/usr/bin/umu-run")

    _, env = backend._prefix_command(["game.exe"], backend.launch_extra_env())

    assert env["WEBVIEW2_ADDITIONAL_BROWSER_ARGUMENTS"] == "--no-sandbox"


def test_games_without_a_webview2_sign_in_are_unchanged(tmp_path, monkeypatch):
    """The base install/launch hook is a no-op: other direct games get no WebView2 step."""
    backend = _backend(tmp_path)
    assert direct.DirectBackend._ensure_sign_in_runtime(backend) is None
    assert "WEBVIEW2_ADDITIONAL_BROWSER_ARGUMENTS" not in direct.DirectBackend.launch_extra_env(
        backend
    )


def test_sign_in_runtime_runs_the_game_exe_as_windows_7(tmp_path, monkeypatch):
    # The plugin (SLWebView2.dll) gives its window WS_EX_LAYERED on Windows 8.1 and later;
    # Wine does not draw WebView2's child windows inside it, so the Google page stays white.
    # As Windows 7 it uses a plain window and the page renders (b29 run, 11-24).
    backend = _backend(tmp_path)
    calls = []
    monkeypatch.setattr(direct_r1999.R1999Backend, "_ensure_webview2", lambda self: None)
    monkeypatch.setattr(
        "nadare.core.winereg.set_app_version",
        lambda prefix, exe, version: calls.append((prefix, exe, version)) or True,
    )

    backend._ensure_sign_in_runtime()

    assert calls == [(backend.prefix(), "reverse1999.exe", "win7")]


def test_haoplay_keeps_the_default_windows_version(tmp_path, monkeypatch):
    from nadare.backends import direct_games

    calls = []
    monkeypatch.setattr(direct_games.Gfl2HaoplayBackend, "_ensure_webview2", lambda self: None)
    monkeypatch.setattr("nadare.core.winereg.set_app_version", lambda *a: calls.append(a) or True)
    backend = direct_games.Gfl2HaoplayBackend(
        make_context(tmp_path, "direct-gfl2-haoplay", {"prefix": str(tmp_path / "p")})
    )

    backend._ensure_sign_in_runtime()

    assert calls == []
