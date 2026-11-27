# SPDX-License-Identifier: GPL-3.0-or-later
"""Strinova's publisher launcher needs WebView2 in its prefix (#1819).

A real launch on an AMD test desktop (2026-11-27) stopped at the launcher's own
"Install the following plugins to launch game: 1. WebView2" box, even with
``IsSkipInstallPlugin=true``. Its sign-in page is WebView2. As for Reverse: 1999 and GFL2
Haoplay, whose own WebView2 offers did nothing under Proton, Nadare installs Microsoft's
runtime itself, here before each launch.
"""

from __future__ import annotations

from _backend_helpers import make_context

from nadare.backends import direct_strinova
from nadare.backends.webview2_signin import WebView2SignInMixin


def _backend(tmp_path, **extra):
    prefix = tmp_path / "strinova-prefix"
    return direct_strinova.StrinovaBackend(
        make_context(tmp_path, "direct-strinova", {"prefix": str(prefix), **extra})
    )


def test_strinova_sets_up_webview2():
    assert issubclass(direct_strinova.StrinovaBackend, WebView2SignInMixin)


def test_install_leaves_webview2_to_the_launch(tmp_path, monkeypatch):
    # Install keeps its b29 behaviour: a failed runner download still syncs the 58 GB of
    # game files, and WebView2 (which needs the runner) comes before the first launch.
    backend = _backend(tmp_path)
    calls = []
    monkeypatch.setattr(backend, "_ensure_default_runner", lambda strict: None)
    monkeypatch.setattr(backend, "_ensure_direct_prefix", lambda: calls.append("prefix"))
    monkeypatch.setattr(
        direct_strinova.StrinovaBackend, "_ensure_webview2", lambda self: calls.append("webview2")
    )
    monkeypatch.setattr(backend, "_sync_direct", lambda: calls.append("sync"))

    backend.install()

    assert calls == ["prefix", "sync"]


def test_an_existing_install_gets_webview2_before_the_launcher_starts(tmp_path, monkeypatch):
    backend = _backend(tmp_path)
    target = backend.direct_target()
    target.parent.mkdir(parents=True)
    target.write_bytes(b"MZ")
    calls = []
    monkeypatch.setattr(backend, "_ensure_default_runner", lambda strict: None)
    monkeypatch.setattr(
        direct_strinova.StrinovaBackend, "_ensure_webview2", lambda self: calls.append("webview2")
    )
    monkeypatch.setattr(backend, "_ensure_game_drive", lambda: calls.append("drive"))
    monkeypatch.setattr(backend, "_launch_direct", lambda: calls.append("launch") or 0)

    assert backend.launch() == 0
    assert calls == ["webview2", "drive", "launch"]


def test_launch_passes_the_webview2_browser_flags(tmp_path, monkeypatch):
    backend = _backend(tmp_path)
    monkeypatch.setattr(backend, "resolve_runner", lambda: ("/opt/proton", "proton"))
    monkeypatch.setattr("nadare.core.umu.system", lambda configured="umu-run": "/usr/bin/umu-run")

    _, env = backend._prefix_command(["Strinova.exe"], backend.launch_extra_env())

    assert env["WEBVIEW2_ADDITIONAL_BROWSER_ARGUMENTS"] == "--no-sandbox"


def test_a_missing_prefix_is_made_before_webview2(tmp_path, monkeypatch):
    # A library install outside the prefix: wineboot (with its clean-up on failure) runs
    # before WebView2's installer, which would otherwise half-create the prefix itself.
    backend = _backend(tmp_path, install_dir=str(tmp_path / "library" / "Strinova"))
    assert not (backend.prefix() / "drive_c").exists()
    target = backend.direct_target()
    target.parent.mkdir(parents=True)
    target.write_bytes(b"MZ")
    calls = []

    def ensure_prefix():
        calls.append("prefix")
        (backend.prefix() / "drive_c").mkdir(parents=True)
        return True

    monkeypatch.setattr(backend, "_ensure_default_runner", lambda strict: None)
    monkeypatch.setattr(backend, "_ensure_prefix", ensure_prefix)
    monkeypatch.setattr(
        direct_strinova.StrinovaBackend, "_ensure_webview2", lambda self: calls.append("webview2")
    )
    monkeypatch.setattr(backend, "_launch_direct", lambda: calls.append("launch") or 0)

    assert backend.launch() == 0
    assert calls == ["prefix", "webview2", "launch"]
