# SPDX-License-Identifier: GPL-3.0-or-later
"""Unit tests for nadare.core.secrets (plaintext fallback path)."""

from __future__ import annotations

import json
import logging
import os
import stat
from pathlib import Path

import pytest

from nadare.core import secrets as core_secrets
from nadare.core.secrets import SecretStore, SecretStoreError


class _FakeKeyring:
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


def test_tokens_refuse_plaintext_fallback(tmp_path: Path) -> None:
    path = tmp_path / "secrets.json"
    store = SecretStore(path, allow_fallback=False)
    assert store.secure is False
    assert store.available is False
    assert store.get("game") is None
    with pytest.raises(SecretStoreError):
        store.set("game", "token")
    # The plaintext fallback file must never be created.
    assert not path.exists()


def test_no_keyring_env_never_writes_a_file(
    tmp_path: Path, monkeypatch: pytest.MonkeyPatch
) -> None:
    monkeypatch.setenv("NADARE_NO_KEYRING", "1")
    path = tmp_path / "secrets.json"
    store = SecretStore(path, allow_fallback=False)
    assert store.secure is False
    assert store.available is False
    with pytest.raises(SecretStoreError):
        store.set("game", "token")
    assert not path.exists()


def test_keyring_store_never_touches_disk(
    tmp_path: Path, fake_keyring: _FakeKeyring
) -> None:
    path = tmp_path / "secrets.json"
    store = SecretStore(path, allow_fallback=False)
    assert store.secure is True
    assert store.available is True
    store.set("game", "token")
    assert store.get("game") == "token"
    store.delete("game")
    assert store.get("game") is None
    assert not path.exists()


def test_fallback_round_trip(tmp_path: Path) -> None:
    store = SecretStore(tmp_path / "secrets.json")

    assert store.secure is False
    assert store.available is True
    assert store.get("game") is None

    store.set("game", "secret-token-value")
    assert store.get("game") == "secret-token-value"
    assert store.profiles() == ["game"]

    store.set("game", "")
    assert store.get("game") is None
    assert store.profiles() == []


def test_delete_removes_token(tmp_path: Path) -> None:
    store = SecretStore(tmp_path / "secrets.json")
    store.set("a", "token-a")
    store.set("b", "token-b")

    store.delete("a")
    assert store.get("a") is None
    assert store.get("b") == "token-b"
    assert store.profiles() == ["b"]

    # Deleting a missing token is a no-op.
    store.delete("missing")


def test_fallback_file_is_0600(tmp_path: Path) -> None:
    path = tmp_path / "secrets.json"
    store = SecretStore(path)
    store.set("game", "token")

    mode = stat.S_IMODE(path.stat().st_mode)
    assert mode == 0o600, f"expected 0600, got {oct(mode)}"

    payload = path.read_text(encoding="utf-8")
    assert "token:game" in payload
    assert "token" in payload


def test_fallback_can_be_disabled(tmp_path: Path) -> None:
    store = SecretStore(tmp_path / "secrets.json", allow_fallback=False)
    assert store.available is False
    assert store.get("game") is None
    with pytest.raises(SecretStoreError):
        store.set("game", "token")


def test_corrupt_secret_file_raises(tmp_path: Path) -> None:
    path = tmp_path / "secrets.json"
    path.write_text("{ not json", encoding="utf-8")
    store = SecretStore(path)
    with pytest.raises(SecretStoreError):
        store.get("game")


def test_fallback_accounts_are_namespaced_by_kind(tmp_path: Path) -> None:
    """L3: two consumers sharing a profile must not collide in the fallback file."""
    path = tmp_path / "secrets.json"
    alpha = SecretStore(path, kind="alpha")
    beta = SecretStore(path, kind="beta")

    alpha.set("game", "token-alpha")
    beta.set("game", "token-beta")

    assert alpha.get("game") == "token-alpha"
    assert beta.get("game") == "token-beta"
    payload = json.loads(path.read_text(encoding="utf-8"))
    assert payload["alpha:game"] == "token-alpha"
    assert payload["beta:game"] == "token-beta"
    assert "token:game" not in payload


def test_legacy_fallback_account_is_read_and_deleted(tmp_path: Path) -> None:
    """A token written before namespacing stays readable, then migrates away."""
    path = tmp_path / "secrets.json"
    path.write_text(json.dumps({"token:game": "legacy"}), encoding="utf-8")
    store = SecretStore(path, kind="czn")

    assert store.get("game") == "legacy"
    store.set("game", "new-token")
    assert store.get("game") == "new-token"

    store.delete("game")
    assert store.get("game") is None
    payload = json.loads(path.read_text(encoding="utf-8"))
    assert "czn:game" not in payload
    assert "token:game" not in payload


def test_keyring_accounts_are_namespaced_by_kind(
    tmp_path: Path, fake_keyring: _FakeKeyring
) -> None:
    path = tmp_path / "secrets.json"
    alpha = SecretStore(path, kind="alpha")
    beta = SecretStore(path, kind="beta")
    alpha.set("game", "a")
    beta.set("game", "b")

    assert alpha.get("game") == "a"
    assert beta.get("game") == "b"
    assert ("nadare-launcher", "alpha:game") in fake_keyring.store
    assert ("nadare-launcher", "beta:game") in fake_keyring.store


def test_fallback_permissions_are_reattached_on_read(tmp_path: Path) -> None:
    path = tmp_path / "secrets.json"
    store = SecretStore(path)
    store.set("game", "token")

    os.chmod(path, 0o644)
    assert store.get("game") == "token"
    assert stat.S_IMODE(path.stat().st_mode) == 0o600


def test_keyring_delete_failure_is_reported(
    tmp_path: Path, monkeypatch: pytest.MonkeyPatch
) -> None:
    class _Broken:
        def delete_password(self, service: str, account: str) -> None:
            raise RuntimeError("keyring locked")

    monkeypatch.setattr(core_secrets, "_keyring_module", lambda: _Broken())
    store = SecretStore(tmp_path / "secrets.json", allow_fallback=False)
    assert store.secure is True
    with pytest.raises(SecretStoreError):
        store.delete("game")


def test_keyring_delete_missing_entry_is_ignored(
    tmp_path: Path, monkeypatch: pytest.MonkeyPatch
) -> None:
    errors = pytest.importorskip("keyring.errors")

    class _Missing:
        def delete_password(self, service: str, account: str) -> None:
            raise errors.PasswordDeleteError("no such password")

    monkeypatch.setattr(core_secrets, "_keyring_module", lambda: _Missing())
    store = SecretStore(tmp_path / "secrets.json", allow_fallback=False)
    store.delete("game")  # a token that is already gone is a no-op


def test_secret_values_are_masked_in_logs(tmp_path: Path) -> None:
    from nadare.core import logging as nadare_logging

    # Reset the module's once-only configuration so we own the handlers.
    root = logging.getLogger(nadare_logging.LOGGER_NAME)
    saved_handlers = list(root.handlers)
    saved_configured = nadare_logging._configured
    root.handlers.clear()
    nadare_logging._configured = False
    try:
        log_file = tmp_path / "nadare.log"
        logger = nadare_logging.setup(filename=log_file, level=logging.INFO)
        secret = "Q" * 20 + "SUPERSECRET" * 2  # > 24 chars, matches mask()
        nadare_logging.get_logger("secrets.test").warning("token=%s", secret)
        for handler in logger.handlers:
            handler.flush()
            handler.close()

        content = log_file.read_text(encoding="utf-8")
        assert secret not in content
        assert "<redacted>" in content  # a ``token=`` pair is redacted by key (#49)
    finally:
        for handler in list(root.handlers):
            handler.close()
        root.handlers.clear()
        root.handlers.extend(saved_handlers)
        nadare_logging._configured = saved_configured


def test_masking_filter_masks_message_and_args() -> None:
    from nadare.core.logging import MaskingFilter

    secret = "Z" * 32
    record = logging.LogRecord("nadare", logging.WARNING, __file__, 1, "value=%s", (secret,), None)
    assert MaskingFilter().filter(record) is True
    rendered = record.getMessage()
    assert secret not in rendered
    assert "…" in rendered

    dict_record = logging.LogRecord("nadare", logging.WARNING, __file__, 1, "%(token)s", None, None)
    dict_record.args = {"token": secret}
    MaskingFilter().filter(dict_record)
    assert secret not in dict_record.getMessage()


def test_keyring_diagnosis_reports_why(monkeypatch):
    from nadare.core import secrets as sec

    monkeypatch.setenv("NADARE_NO_KEYRING", "1")
    ok, why = sec.keyring_diagnosis()
    assert ok is False and "NADARE_NO_KEYRING" in why
    monkeypatch.delenv("NADARE_NO_KEYRING")

    import keyring

    class Boom:
        __module__ = "x"

        def get_password(self, *a):  # pragma: no cover - not used
            raise AssertionError

    def no_collection(service, user):
        raise RuntimeError("Failed to create the collection: Prompt dismissed.")

    monkeypatch.setattr(keyring, "get_keyring", lambda: Boom())
    monkeypatch.setattr(keyring, "get_password", no_collection)
    ok, why = sec.keyring_diagnosis()
    assert ok is False
    assert "cannot open a collection" in why and "Prompt dismissed" in why


def test_non_dict_secret_file_raises(tmp_path):
    """A truncated/tampered file holding valid-but-wrong JSON must not read as
    'no saved tokens' (#78): callers would believe the user has no login."""
    import pytest

    from nadare.core.secrets import SecretStore, SecretStoreError

    path = tmp_path / "secrets.json"
    for payload in ("[]", '"x"', "null", "42"):
        path.write_text(payload)
        store = SecretStore(path=path, allow_fallback=True)
        store._keyring = None  # force the file backend
        with pytest.raises(SecretStoreError):
            store._load_file()


def _fake_secretstorage(monkeypatch, *, default_exists: bool, locked: bool = False):
    """Install a stand-in ``secretstorage`` that records whether anything prompted."""
    import sys
    import types

    calls: list[str] = []

    class ItemNotFoundException(Exception):
        pass

    class Conn:
        def close(self):
            calls.append("close")

    class Collection:
        def __init__(self, connection, *a, **kw):
            if not default_exists:
                raise ItemNotFoundException("no default collection")

        def is_locked(self):
            return locked

        def unlock(self):  # pragma: no cover - must never be reached
            calls.append("unlock")

    mod = types.ModuleType("secretstorage")
    mod.dbus_init = lambda: Conn()
    mod.Collection = Collection
    mod.get_default_collection = lambda *a, **k: calls.append("create")  # must never run
    exc = types.ModuleType("secretstorage.exceptions")
    exc.ItemNotFoundException = ItemNotFoundException
    monkeypatch.setitem(sys.modules, "secretstorage", mod)
    monkeypatch.setitem(sys.modules, "secretstorage.exceptions", exc)
    return calls


def _secret_service_keyring(monkeypatch):
    import keyring

    class Keyring:
        __module__ = "keyring.backends.SecretService"

        def get_password(self, *a):  # pragma: no cover - the prompting probe must not run
            raise AssertionError("diagnosis must not call get_password on Secret Service")

    monkeypatch.setattr(keyring, "get_keyring", lambda: Keyring())
    monkeypatch.setattr(
        keyring,
        "get_password",
        lambda *a: (_ for _ in ()).throw(AssertionError("prompting probe ran")),
    )


def test_keyring_diagnosis_does_not_create_or_unlock_secret_service(monkeypatch):
    """#162: doctor must not pop 'Choose password for new keyring'."""
    from nadare.core import secrets as sec

    monkeypatch.delenv("NADARE_NO_KEYRING", raising=False)
    _secret_service_keyring(monkeypatch)

    calls = _fake_secretstorage(monkeypatch, default_exists=False)
    ok, why = sec.keyring_diagnosis()
    assert ok is False and "no default keyring" in why
    assert calls == ["close"]  # nothing created, nothing unlocked

    calls = _fake_secretstorage(monkeypatch, default_exists=True, locked=True)
    ok, why = sec.keyring_diagnosis()
    assert ok is True and "locked" in why
    assert "unlock" not in calls and "create" not in calls

    calls = _fake_secretstorage(monkeypatch, default_exists=True, locked=False)
    ok, why = sec.keyring_diagnosis()
    assert ok is True and "locked" not in why
    assert "unlock" not in calls and "create" not in calls
