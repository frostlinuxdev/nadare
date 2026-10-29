# SPDX-License-Identifier: GPL-3.0-or-later
"""Vendor children (games, installers, Wine/Proton) must not inherit launcher tokens.

Nadare's GitHub token is for the launcher itself.  Everything it spawns gets a
copy of the environment with secrets removed, while the CZN helper's credential
paths and harmless ``NADARE_*`` switches survive.
"""

from __future__ import annotations

import pytest
from _backend_helpers import make_context

from nadare.backends.czn import backend as czn_backend
from nadare.backends.czn.backend import CznBackend
from nadare.core import process

_SECRETS = {
    "NADARE_GITHUB_TOKEN": "ghp-launcher",
    "NADARE_INDEX_TOKEN": "index-secret",
    "NADARE_DEPLOY_SECRET": "deploy-secret",
    "NADARE_DB_PASSWORD": "db-password",
    "NADARE_SIGNING_KEY": "signing-key",
    "GITHUB_TOKEN": "ghp-generic",
    "GH_TOKEN": "ghp-gh",
    "GITHUB_PAT": "ghp-pat",
    "SGDB_API_KEY": "steamgriddb-key",
}


def test_child_environ_drops_secrets_and_keeps_operational_vars() -> None:
    base = {
        **_SECRETS,
        "PATH": "/usr/bin:/bin",
        "HOME": "/home/player",
        "WINEPREFIX": "/games/pfx",
        "CZN_CREDENTIAL_IN": "/tmp/in.json",
        "CZN_CREDENTIAL_OUT": "/tmp/out.json",
        "NADARE_VCREDIST_METHOD": "auto",
    }

    env = process.child_environ(base)

    for name in _SECRETS:
        assert name not in env
    assert env["PATH"] == "/usr/bin:/bin"
    assert env["HOME"] == "/home/player"
    assert env["WINEPREFIX"] == "/games/pfx"
    assert env["CZN_CREDENTIAL_IN"] == "/tmp/in.json"
    assert env["CZN_CREDENTIAL_OUT"] == "/tmp/out.json"
    assert env["NADARE_VCREDIST_METHOD"] == "auto"
    # The caller's mapping is never mutated.
    assert base["NADARE_GITHUB_TOKEN"] == "ghp-launcher"


def test_build_env_has_no_token_even_when_exported(monkeypatch: pytest.MonkeyPatch) -> None:
    for name, value in _SECRETS.items():
        monkeypatch.setenv(name, value)
    monkeypatch.setenv("PATH", "/usr/bin:/bin")

    env = process.build_env()

    for name in _SECRETS:
        assert name not in env
    assert env["PATH"] == "/usr/bin:/bin"


def test_czn_command_env_has_no_launcher_token(tmp_path, monkeypatch: pytest.MonkeyPatch) -> None:
    proton = tmp_path / "MyProton"
    proton.mkdir()
    (proton / "proton").write_text("#!/bin/sh\n")
    ctx = make_context(tmp_path, "stove-czn", {"prefix": str(tmp_path / "prefix")})
    backend = CznBackend(ctx)
    backend.ctx.profile["runner"] = str(proton)
    monkeypatch.setattr(czn_backend.shutil, "which", lambda name, *a, **k: None)
    monkeypatch.setenv("NADARE_GITHUB_TOKEN", "ghp-launcher")

    _argv, env = backend._prefix_command(["game.exe"])

    assert "NADARE_GITHUB_TOKEN" not in env
    assert env["WINEPREFIX"] == str(tmp_path / "prefix")
