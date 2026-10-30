# SPDX-License-Identifier: GPL-3.0-or-later
"""The WebView2 pipeline lives in core/webview2.py; CZN and GFL2-Haoplay both call it."""

from __future__ import annotations

import inspect
from pathlib import Path

import pytest
from _backend_helpers import make_context

from nadare.backends import direct
from nadare.backends.czn import backend as czn_backend
from nadare.backends.czn.backend import CznBackend
from nadare.core import paths, webview2
from nadare.core.backends import BackendError

PUBLIC = (
    "installed",
    "resolve_pin",
    "pin_error",
    "overridden",
    "index_pins",
    "index_source",
    "verify",
    "download",
    "ensure",
)


@pytest.mark.parametrize("name", PUBLIC)
def test_the_shared_module_exposes_the_pipeline(name):
    assert callable(getattr(webview2, name))


def test_the_exit_codes_and_messages_are_shared_not_copied():
    assert webview2.EXIT_WEBVIEW2_UNVERIFIED == 10
    assert webview2.EXIT_WEBVIEW2_FAILED == 11
    assert czn_backend.EXIT_WEBVIEW2_UNVERIFIED is webview2.EXIT_WEBVIEW2_UNVERIFIED
    assert czn_backend.EXIT_WEBVIEW2_FAILED is webview2.EXIT_WEBVIEW2_FAILED
    assert czn_backend.WEBVIEW2_MESSAGES is webview2.MESSAGES


def test_installed_looks_for_the_runtime_executable_in_the_prefix(tmp_path):
    assert webview2.installed(tmp_path) is False
    app = tmp_path / "drive_c/Program Files (x86)/Microsoft/EdgeWebView/Application/1.2.3"
    app.mkdir(parents=True)
    (app / "msedgewebview2.exe").write_bytes(b"MZ")
    assert webview2.installed(tmp_path) is True


def _ensure_kwargs(boom, **overrides):
    parameters = inspect.signature(webview2.ensure).parameters
    kwargs = {
        name: boom for name in parameters if name not in ("prefix", "profile", "cache_dir_fn")
    }
    kwargs.update(overrides)
    return kwargs


def _boom(*args, **kwargs):
    raise AssertionError("must not be called")


def _put_runtime(prefix):
    app = prefix / "drive_c/Program Files (x86)/Microsoft/EdgeWebView/Application/9.9.9"
    app.mkdir(parents=True)
    (app / "msedgewebview2.exe").write_bytes(b"MZ")


def test_ensure_asks_the_injected_installed_check_first(tmp_path):
    calls = []
    kwargs = _ensure_kwargs(_boom, installed_fn=lambda: calls.append("installed") or True)
    webview2.ensure(tmp_path, profile={}, cache_dir_fn=lambda: tmp_path / "cache", **kwargs)
    assert calls == ["installed"]


def test_ensure_looks_in_the_prefix_when_no_check_is_injected(tmp_path):
    _put_runtime(tmp_path)
    kwargs = _ensure_kwargs(_boom)
    kwargs.pop("installed_fn", None)
    webview2.ensure(tmp_path, profile={}, cache_dir_fn=lambda: tmp_path / "cache", **kwargs)


def _haoplay(tmp_path):
    return direct.Gfl2HaoplayBackend(
        make_context(tmp_path, "direct-gfl2-haoplay", {"prefix": str(tmp_path / "haoplay-prefix")})
    )


def test_haoplay_no_longer_builds_a_czn_backend_to_install_webview2(tmp_path, monkeypatch):
    def forbidden(self, *args, **kwargs):
        raise AssertionError("Haoplay must not construct a CznBackend")

    monkeypatch.setattr(CznBackend, "__init__", forbidden)
    seen = {}
    monkeypatch.setattr(webview2, "ensure", lambda prefix, **kw: seen.update(prefix=prefix, **kw))
    backend = _haoplay(tmp_path)

    backend._ensure_webview2()

    assert seen["prefix"] == backend.prefix()
    assert seen["profile"] is backend.ctx.profile
    assert seen["emit"] == backend.ctx.emit
    assert seen["cancel"] is backend.ctx.cancel
    # the installer download is cached once for both games, as before
    assert seen["cache_dir_fn"]() == paths.cache_dir() / "czn"
    assert seen["explain"] == backend._watch.explain
    assert callable(seen["run_in_prefix"])


def test_haoplay_runs_the_installer_in_its_own_prefix_through_xwayland(tmp_path, monkeypatch):
    backend = _haoplay(tmp_path)
    seen = []

    def command(args, extra_env=None):
        seen.append(("command", list(args), extra_env))
        return ["haoplay-runner", *args], {"WINEPREFIX": str(backend.prefix())}

    monkeypatch.setattr(backend, "_prefix_command", command)
    captured = {}
    monkeypatch.setattr(webview2, "ensure", lambda prefix, **kw: captured.update(kw))
    backend._ensure_webview2()

    ran = []

    def fake_stream(cmd, env, **kwargs):
        ran.append((cmd, env, kwargs))
        return 0

    monkeypatch.setattr(direct, "stream", fake_stream, raising=False)
    import nadare.backends.arknights as arknights

    monkeypatch.setattr(arknights, "stream", fake_stream)
    captured["run_in_prefix"](["installer.exe", "/silent", "/install"])

    assert seen[0][0] == "command"
    assert seen[0][1] == ["installer.exe", "/silent", "/install"]
    assert seen[0][2] == {"PROTON_ENABLE_WAYLAND": "0"}
    cmd, env, kwargs = ran[0]
    assert cmd[0] == "haoplay-runner"
    assert env["WINEPREFIX"] == str(backend.prefix())
    assert Path(kwargs["cwd"]) == backend.prefix()


def _raising_cache():
    raise OSError("EACCES")


def test_a_failing_cache_dir_is_not_looked_at_when_already_installed(tmp_path):
    kwargs = _ensure_kwargs(_boom, installed_fn=lambda: True)
    webview2.ensure(tmp_path, profile={}, cache_dir_fn=_raising_cache, **kwargs)


def test_a_missing_pin_is_reported_before_the_cache_is_touched(tmp_path):
    kwargs = _ensure_kwargs(_boom, installed_fn=lambda: False, pin_fn=lambda: "")
    with pytest.raises(BackendError) as caught:
        webview2.ensure(tmp_path, profile={}, cache_dir_fn=_raising_cache, **kwargs)
    assert caught.value.code == webview2.EXIT_WEBVIEW2_UNVERIFIED


def test_a_failing_cache_dir_is_a_mapped_error_not_a_raw_oserror(tmp_path, monkeypatch):
    monkeypatch.setattr(webview2.graphics, "has_display", lambda: True)
    kwargs = _ensure_kwargs(_boom, installed_fn=lambda: False, pin_fn=lambda: "a" * 64)
    with pytest.raises(BackendError, match="cannot access the WebView2 cache") as caught:
        webview2.ensure(tmp_path, profile={}, cache_dir_fn=_raising_cache, **kwargs)
    assert caught.value.code == webview2.EXIT_WEBVIEW2_FAILED
