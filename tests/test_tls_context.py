# SPDX-License-Identifier: GPL-3.0-or-later
# Copyright (C) 2026 Nadare contributors
"""Nadare's own HTTPS paths verify against the pinned CA bundle, not the host.

No sockets: contexts are faked and ``urllib`` openers are built but never used.
"""

from __future__ import annotations

import ssl
import sys
import types
import urllib.error
import urllib.request

import pytest

from nadare.core import download as dl
from nadare.core import tls


@pytest.fixture(autouse=True)
def _clean_tls(monkeypatch):
    monkeypatch.delenv("NADARE_CA_BUNDLE", raising=False)
    monkeypatch.delenv("SSL_CERT_FILE", raising=False)
    monkeypatch.delenv("SSL_CERT_DIR", raising=False)
    tls.clear_context_cache()
    yield
    tls.clear_context_cache()


class _FakeContext:
    """An SSLContext stand-in; only identity matters to these tests."""

    def __init__(self, **kwargs: object) -> None:
        self.kwargs = kwargs


def _fake_create(monkeypatch) -> list[dict[str, object]]:
    calls: list[dict[str, object]] = []

    def _create(**kwargs: object) -> _FakeContext:
        calls.append(kwargs)
        return _FakeContext(**kwargs)

    monkeypatch.setattr(tls.ssl, "create_default_context", _create)
    return calls


def _fake_certifi(monkeypatch, tmp_path) -> str:
    bundle = tmp_path / "certifi.pem"
    bundle.write_text("pem")
    module = types.ModuleType("certifi")
    module.where = lambda: str(bundle)  # type: ignore[attr-defined]
    monkeypatch.setitem(sys.modules, "certifi", module)
    return str(bundle)


def test_nadare_ca_bundle_wins(monkeypatch, tmp_path):
    bundle = tmp_path / "corp.pem"
    bundle.write_text("pem")
    monkeypatch.setenv("NADARE_CA_BUNDLE", str(bundle))
    monkeypatch.setenv("SSL_CERT_FILE", "/host/ca.pem")
    calls = _fake_create(monkeypatch)

    ctx = tls.ssl_context()

    assert isinstance(ctx, _FakeContext)
    assert calls == [{"cafile": str(bundle)}]
    assert tls.ca_source() == "NADARE_CA_BUNDLE"


def test_environment_overrides_certifi(monkeypatch, tmp_path):
    monkeypatch.setenv("SSL_CERT_FILE", "/host/ca.pem")
    _fake_certifi(monkeypatch, tmp_path)
    calls = _fake_create(monkeypatch)

    ctx = tls.ssl_context()

    assert isinstance(ctx, _FakeContext)
    assert calls == [{}]
    assert tls.ca_source() == "environment"


def test_ssl_cert_dir_alone_counts_as_environment(monkeypatch):
    monkeypatch.setenv("SSL_CERT_DIR", "/host/certs")
    calls = _fake_create(monkeypatch)

    tls.ssl_context()

    assert calls == [{}]
    assert tls.ca_source() == "environment"


def test_certifi_is_the_default(monkeypatch, tmp_path):
    bundle = _fake_certifi(monkeypatch, tmp_path)
    calls = _fake_create(monkeypatch)

    tls.ssl_context()

    assert calls == [{"cafile": bundle}]
    assert tls.ca_source() == "certifi"


def test_certifi_path_does_not_set_ssl_cert_file(monkeypatch, tmp_path):
    _fake_certifi(monkeypatch, tmp_path)
    _fake_create(monkeypatch)
    import os

    tls.ssl_context()

    assert "SSL_CERT_FILE" not in os.environ


def test_system_fallback_uses_ensure_ca_bundle(monkeypatch):
    monkeypatch.setitem(sys.modules, "certifi", None)  # import certifi raises
    called: list[bool] = []

    def _ensure() -> None:
        called.append(True)

    monkeypatch.setattr(tls, "ensure_ca_bundle", _ensure)
    calls = _fake_create(monkeypatch)

    tls.ssl_context()

    assert called == [True]
    assert calls == [{}]
    assert tls.ca_source() == "system"


def test_context_is_cached_per_environment(monkeypatch, tmp_path):
    _fake_certifi(monkeypatch, tmp_path)
    calls = _fake_create(monkeypatch)

    first = tls.ssl_context()
    second = tls.ssl_context()

    assert first is second
    assert len(calls) == 1

    monkeypatch.setenv("NADARE_CA_BUNDLE", str(tmp_path / "certifi.pem"))
    third = tls.ssl_context()
    assert third is not first
    assert len(calls) == 2

    tls.clear_context_cache()
    tls.ssl_context()
    assert len(calls) == 3


def _https_handler(handlers: tuple[object, ...]) -> urllib.request.HTTPSHandler:
    matches = [h for h in handlers if isinstance(h, urllib.request.HTTPSHandler)]
    assert len(matches) == 1
    return matches[0]


def test_http_open_uses_the_tls_context(monkeypatch):
    ctx = ssl.create_default_context()
    monkeypatch.setattr(tls, "ssl_context", lambda: ctx)
    captured: dict[str, urllib.request.OpenerDirector] = {}
    real_build = urllib.request.build_opener

    class _Opener:
        def open(self, *args: object, **kwargs: object) -> None:
            raise urllib.error.URLError("no network")

    def _build(*handlers: object) -> urllib.request.OpenerDirector:
        opener = real_build(*handlers)  # type: ignore[arg-type]
        opener.open = _Opener().open  # type: ignore[method-assign]
        captured["opener"] = opener
        return opener

    monkeypatch.setattr(dl.urllib.request, "build_opener", _build)
    with pytest.raises(dl.DownloadError):
        dl.http_get("https://example.invalid/x")

    assert _https_handler(tuple(captured["opener"].handlers))._context is ctx  # type: ignore[arg-type]


def test_remote_size_uses_the_tls_context(monkeypatch):
    ctx = ssl.create_default_context()
    monkeypatch.setattr(tls, "ssl_context", lambda: ctx)
    captured: dict[str, urllib.request.OpenerDirector] = {}
    real_build = urllib.request.build_opener

    class _Response:
        headers: dict[str, str] = {"Content-Length": "3"}

        def __enter__(self) -> _Response:
            return self

        def __exit__(self, *exc: object) -> bool:
            return False

    def _open(self: object, *args: object, **kwargs: object) -> _Response:
        return _Response()

    def _build(*handlers: object) -> urllib.request.OpenerDirector:
        opener = real_build(*handlers)  # type: ignore[arg-type]
        opener.open = _open  # type: ignore[method-assign]
        captured["opener"] = opener
        return opener

    monkeypatch.setattr(dl.urllib.request, "build_opener", _build)
    assert dl.remote_size("https://example.invalid/x") == 3

    assert _https_handler(tuple(captured["opener"].handlers))._context is ctx  # type: ignore[arg-type]


def test_connection_pool_passes_the_tls_context(monkeypatch):
    ctx = ssl.create_default_context()
    monkeypatch.setattr(tls, "ssl_context", lambda: ctx)
    captured: dict[str, object] = {}

    class _Conn:
        def __init__(self, host: str, port: int, timeout: int = 0, **kwargs: object):
            captured.update(kwargs)

        def close(self) -> None:
            pass

    monkeypatch.setattr(dl.http.client, "HTTPSConnection", _Conn)
    pool = dl.HttpClientPool()
    pool._connection("https", "example.invalid", 443, 5)

    assert captured["context"] is ctx


def test_doctor_prints_the_ca_source(monkeypatch, capsys):
    from nadare.app import run_cli
    from nadare.core import graphics, process, runners

    monkeypatch.setattr(graphics, "vulkan_info", lambda **k: graphics.VulkanInfo(False))
    monkeypatch.setattr(graphics, "dxvk_ok", lambda: False)
    monkeypatch.setattr(process, "which", lambda name, **k: None)
    monkeypatch.setattr(runners, "all_runners", list)
    monkeypatch.setattr(tls, "ca_source", lambda: "NADARE_CA_BUNDLE")

    assert run_cli(["doctor"]) == 0
    assert "tls: CA list = NADARE_CA_BUNDLE" in capsys.readouterr().out


def test_setup_does_not_export_a_host_bundle_when_certifi_is_available(monkeypatch):
    """An auto-set SSL_CERT_FILE would read as a user override and replace the pinned list."""
    for name in ("NADARE_CA_BUNDLE", "SSL_CERT_FILE", "SSL_CERT_DIR"):
        monkeypatch.delenv(name, raising=False)
    monkeypatch.setattr(tls, "_default_store_present", lambda: False)
    tls.clear_context_cache()
    assert tls.setup() is None
    assert "SSL_CERT_FILE" not in __import__("os").environ
    assert tls.ca_source() == "certifi"


def test_setup_leaves_a_user_override_alone(monkeypatch, tmp_path):
    bundle = tmp_path / "ca.pem"
    bundle.write_text("x")
    monkeypatch.setenv("SSL_CERT_FILE", str(bundle))
    tls.clear_context_cache()
    assert tls.setup() is None
    assert tls.ca_source() == "environment"
