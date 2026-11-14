# SPDX-License-Identifier: GPL-3.0-or-later
"""Shared helpers and fixtures of the tests split out of test_backends_czn.py."""

from __future__ import annotations

import hashlib
import json
import os
import time
from pathlib import Path

import pytest
from _backend_helpers import make_context

from nadare.backends.czn import backend as czn_backend
from nadare.backends.czn.backend import (
    GAME_SUBDIR,
    SERVICE,
    CznBackend,
)
from nadare.core import secrets as core_secrets
from nadare.core.backends import BackendError

_REAL_INDEX_PINS = CznBackend._webview2_index_pins


_REAL_INDEX_SOURCE = CznBackend._webview2_index_source


@pytest.fixture
def make_backend_context(tmp_path):
    """Local alias so the tests can build a context with a one-line call."""

    def _make(kind: str, profile: dict | None = None, name: str = "test"):
        return make_context(tmp_path, kind, profile, name)

    return _make


@pytest.fixture(autouse=True)
def _no_runner_downloads(monkeypatch):
    """No CZN test may download a runner (resolving the default one would)."""

    def offline(self, rid, latest):
        raise BackendError("offline (no runner downloads in tests)")

    monkeypatch.setattr(CznBackend, "_download_runner", offline)


@pytest.fixture(autouse=True)
def _no_webview2_index(monkeypatch):
    """Never hit the network for the published WebView2 hashes in unit tests."""
    monkeypatch.setattr(CznBackend, "_webview2_index_pins", staticmethod(lambda: set()))
    monkeypatch.setattr(CznBackend, "_webview2_index_source", staticmethod(lambda: ""))


@pytest.fixture(autouse=True)
def _reset_czn_process_state():
    """Keep the process-wide live session/keyring cache from leaking tests."""
    czn_backend._reset_process_state()
    yield
    czn_backend._reset_process_state()


def _make_backend(make_backend_context, prefix: Path) -> CznBackend:
    ctx = make_backend_context("stove-czn", {"prefix": str(prefix)})
    return CznBackend(ctx)


def _install_game(prefix: Path, version: int = 42100) -> None:
    game = prefix / GAME_SUBDIR
    (game / "bin").mkdir(parents=True, exist_ok=True)
    (game / "bin" / "ssr-stove-shield.exe").write_bytes(b"shield")
    manifest = game / "combinedata_manifest" / f"GameManifest_{SERVICE}.upf"
    manifest.parent.mkdir(parents=True, exist_ok=True)
    manifest.write_text(json.dumps({"local_version": version}))


def _write_session(root: Path, *, valid: bool = True) -> Path:
    root.mkdir(parents=True, exist_ok=True)
    session = root / "session.json"
    expire = int(time.time() * 1000) + (3600_000 if valid else -3600_000)
    session.write_text(json.dumps({"access_token": "tok", "expire_time": str(expire)}))
    return session


def _register_webview2(prefix) -> None:
    """The registry entry a finished WebView2 install leaves in system.reg (#912)."""
    key = (
        r"[Software\\Wow6432Node\\Microsoft\\EdgeUpdate\\Clients\\"
        r"{F3017226-FE2A-4295-8BDF-00C3A9A7E4C5}]"
    )
    (prefix / "system.reg").write_text(
        f'WINE REGISTRY Version 2\n\n{key} 1\n"pv"="1.0.0"\n'
    )


def _manifest_files(*rows: str) -> dict:
    return {"version_no": 10, "root_folder": "root", "execution": "game.exe", "files": list(rows)}


def _write_matching(path: Path, data: bytes) -> str:
    path.parent.mkdir(parents=True, exist_ok=True)
    path.write_bytes(data)
    return hashlib.md5(data).hexdigest()


SENTINEL = "SENTINEL-REFRESH-TOKEN-8f3a2b7c"


class _FakeKeyring:
    """Minimal stand-in for the ``keyring`` module (no disk, no service)."""

    def __init__(self) -> None:
        self.store: dict[tuple[str, str], str] = {}

    def get_password(self, service: str, account: str) -> str | None:
        return self.store.get((service, account))

    def set_password(self, service: str, account: str, value: str) -> None:
        self.store[(service, account)] = value

    def delete_password(self, service: str, account: str) -> None:
        del self.store[(service, account)]


@pytest.fixture
def fake_keyring(monkeypatch: pytest.MonkeyPatch) -> _FakeKeyring:
    kr = _FakeKeyring()
    monkeypatch.setattr(core_secrets, "_keyring_module", lambda: kr)
    return kr


def _cred_backend(make_backend_context, tmp_path, remember: bool = False) -> CznBackend:
    ctx = make_backend_context(
        "stove-czn", {"remember_me": remember, "prefix": str(tmp_path / "prefix")}
    )
    return CznBackend(ctx)


def _credential_blob(fake_keyring: _FakeKeyring) -> dict:
    assert len(fake_keyring.store) == 1, fake_keyring.store
    return json.loads(next(iter(fake_keyring.store.values())))


def _auth_runner(
    calls: list[list[str]],
    credential: dict | None = None,
    code: int = 0,
    *,
    write_session: bool = True,
):
    """Build a fake ``_run_module`` that mimics the auth worker's handoff.

    The credential is delivered through the 0600 file named by
    ``CZN_CREDENTIAL_OUT`` -- never through stdout/events.
    """

    def run(module, args, **kwargs):
        calls.append(list(args))
        extra = kwargs.get("extra_env") or {}
        handoff = extra.get("CZN_CREDENTIAL_OUT")
        if handoff and credential is not None:
            path = Path(handoff)
            path.parent.mkdir(parents=True, exist_ok=True)
            path.write_text(json.dumps(credential))
        if write_session and "--out" in args:
            out = Path(args[args.index("--out") + 1])
            out.parent.mkdir(parents=True, exist_ok=True)
            out.write_text("{}")
            os.chmod(out, 0o600)
        return code

    return run


@pytest.fixture(autouse=True)
def _no_vcredist_network(monkeypatch):
    """Unit tests must never fetch the real ~25 MB VC++ installer.

    Every test that exercises the download path replaces this stub in its body.
    """

    def _offline(*args, **kwargs):
        raise OSError("offline")

    monkeypatch.setattr("urllib.request.urlopen", _offline)
