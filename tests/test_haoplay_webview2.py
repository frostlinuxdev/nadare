# SPDX-License-Identifier: GPL-3.0-or-later
"""Haoplay's direct client needs WebView2 in its own Wine prefix."""

from __future__ import annotations

import pytest
from _backend_helpers import make_context

from nadare.backends import direct
from nadare.backends.czn.backend import CznBackend
from nadare.core.backends import BackendError


def _backend(tmp_path):
    prefix = tmp_path / "haoplay-prefix"
    return direct.Gfl2HaoplayBackend(
        make_context(tmp_path, "direct-gfl2-haoplay", {"prefix": str(prefix)})
    )


def test_install_sets_up_webview2_in_haoplay_prefix_before_sync(tmp_path, monkeypatch):
    backend = _backend(tmp_path)
    calls = []
    monkeypatch.setattr(backend, "_ensure_default_runner", lambda strict: None)
    monkeypatch.setattr(backend, "_ensure_direct_prefix", lambda: calls.append("prefix"))
    monkeypatch.setattr(
        CznBackend,
        "_ensure_webview2",
        lambda self: calls.append(("webview2", self.prefix(), self.ctx)),
    )
    monkeypatch.setattr(backend, "_sync_direct", lambda: calls.append("sync"))

    backend.install()

    assert calls == ["prefix", ("webview2", backend.prefix(), backend.ctx), "sync"]


def test_install_stops_when_webview2_setup_fails(tmp_path, monkeypatch):
    backend = _backend(tmp_path)
    monkeypatch.setattr(backend, "_ensure_default_runner", lambda strict: None)
    monkeypatch.setattr(backend, "_ensure_direct_prefix", lambda: None)
    monkeypatch.setattr(
        CznBackend,
        "_ensure_webview2",
        lambda self: (_ for _ in ()).throw(BackendError("WebView2 setup failed")),
    )
    monkeypatch.setattr(
        backend, "_sync_direct", lambda: pytest.fail("game sync ran after WebView2 failure")
    )

    with pytest.raises(BackendError, match="WebView2 setup failed"):
        backend.install()


def test_webview2_installer_uses_haoplay_prefix_runner_and_xwayland(tmp_path, monkeypatch):
    backend = _backend(tmp_path)
    backend.ctx.profile["prefix"] = ""
    backend.ctx.profile["wineprefix"] = str(tmp_path / "legacy-prefix")
    seen = []

    def command(args, extra_env=None):
        seen.append((args, extra_env))
        return ["haoplay-runner", *args], {"WINEPREFIX": str(backend.prefix()), **extra_env}

    monkeypatch.setattr(backend, "_prefix_command", command)
    monkeypatch.setattr(
        CznBackend, "_ensure_webview2", lambda self: self._run_in_prefix(["installer.exe"])
    )
    monkeypatch.setattr(
        CznBackend,
        "_stream",
        lambda self, cmd, env, cwd=None: seen.append((cmd, env, cwd)) or 0,
    )

    backend._ensure_webview2()

    assert seen[0] == (["installer.exe"], {"PROTON_ENABLE_WAYLAND": "0"})
    assert seen[1] == (
        ["haoplay-runner", "installer.exe"],
        {"WINEPREFIX": str(backend.prefix()), "PROTON_ENABLE_WAYLAND": "0"},
        backend.prefix(),
    )


def test_existing_haoplay_install_gets_webview2_before_launch(tmp_path, monkeypatch):
    backend = _backend(tmp_path)
    target = backend.install_dir() / backend.direct_exe
    target.parent.mkdir(parents=True)
    target.write_bytes(b"MZ")
    calls = []
    monkeypatch.setattr(backend, "_ensure_default_runner", lambda strict: None)
    monkeypatch.setattr(CznBackend, "_ensure_webview2", lambda self: calls.append("webview2"))
    monkeypatch.setattr(backend, "_launch_direct", lambda: calls.append("launch") or 0)

    assert backend.launch() == 0
    assert calls == ["webview2", "launch"]


def test_haoplay_launch_passes_webview2_browser_flags_and_wayland(tmp_path, monkeypatch):
    backend = _backend(tmp_path)
    monkeypatch.setattr(backend, "resolve_runner", lambda: ("/opt/proton", "proton"))
    monkeypatch.setattr("nadare.core.umu.system", lambda configured="umu-run": "/usr/bin/umu-run")

    _, env = backend._prefix_command(["game.exe"], backend.launch_extra_env())

    assert env["WEBVIEW2_ADDITIONAL_BROWSER_ARGUMENTS"] == "--no-sandbox"
    assert env["PROTON_ENABLE_WAYLAND"] == "1"
    assert env["UMU_ID"] == "0"


def test_haoplay_display_and_browser_flags_can_be_overridden(tmp_path, monkeypatch):
    backend = _backend(tmp_path)
    backend.ctx.profile["display"] = "x11"
    backend.ctx.profile["extra_env"] = {
        "WEBVIEW2_ADDITIONAL_BROWSER_ARGUMENTS": "--disable-gpu",
    }
    monkeypatch.setattr(backend, "resolve_runner", lambda: ("/opt/proton", "proton"))
    monkeypatch.setattr("nadare.core.umu.system", lambda configured="umu-run": "/usr/bin/umu-run")

    _, env = backend._prefix_command(["game.exe"], backend.launch_extra_env())

    assert env["WEBVIEW2_ADDITIONAL_BROWSER_ARGUMENTS"] == "--disable-gpu"
    assert env["PROTON_ENABLE_WAYLAND"] == "0"
