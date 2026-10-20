# SPDX-License-Identifier: GPL-3.0-or-later
"""Pins: account store ignores plaintext file; keyring read errors surface; active repoint."""

from __future__ import annotations

from pathlib import Path

import pytest

from nadare.core import secrets as core_secrets
from nadare.core.config import Config
from nadare.core.secrets import SecretStore, SecretStoreError


def test_account_store_never_reads_a_token_from_the_plaintext_file(tmp_path: Path) -> None:
    path = tmp_path / "secrets.json"
    SecretStore(path).set("prof", "PLAINTEXT-TOKEN")  # legacy fallback store wrote it
    assert "PLAINTEXT-TOKEN" in path.read_text()
    assert SecretStore(path, allow_fallback=False).get("prof") is None


def test_keyring_read_failure_is_an_error_not_logged_out(
    tmp_path: Path, monkeypatch: pytest.MonkeyPatch
) -> None:
    class Locked:
        def get_password(self, service, account):
            raise RuntimeError("keyring is locked")

    monkeypatch.setattr(core_secrets, "_keyring_module", lambda: Locked())
    store = SecretStore(tmp_path / "secrets.json", allow_fallback=False)
    with pytest.raises(SecretStoreError):
        store.get("prof")


def test_removing_the_active_profile_repoints_active(tmp_path: Path) -> None:
    cfg = Config.load(tmp_path / "config.json")
    cfg.add_profile("a", backend="wine", title="A")
    cfg.add_profile("b", backend="wine", title="B")
    cfg.set_active("a")
    cfg.remove_profile("a")
    assert cfg.active == "b"
    assert cfg.active in cfg.profiles
