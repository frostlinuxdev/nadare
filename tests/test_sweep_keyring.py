# SPDX-License-Identifier: GPL-3.0-or-later
"""#209: Remember me must not create a keyring, and a failed save must be said.

No test here touches a real keyring or Secret Service: the keyring module is a
fake and the no-prompt probe is stubbed (the probe itself is covered by
``test_secrets.py``).
"""

from __future__ import annotations

import pytest
from _backend_helpers import make_context

from nadare.backends.czn import backend as czn_backend
from nadare.backends.czn.backend import CznBackend
from nadare.core import secrets as core_secrets

SENTINEL = "SENTINEL-REFRESH-TOKEN-8f3a2b7c"


class _FakeKeyring:
    def __init__(self) -> None:
        self.store: dict[tuple[str, str], str] = {}
        self.deletes: list[tuple[str, str]] = []

    def get_password(self, service: str, account: str) -> str | None:
        return self.store.get((service, account))

    def set_password(self, service: str, account: str, value: str) -> None:
        self.store[(service, account)] = value

    def delete_password(self, service: str, account: str) -> None:
        self.deletes.append((service, account))
        del self.store[(service, account)]


class _Module:
    """A keyring-shaped module whose only useful method is ``get_keyring``."""

    def __init__(self, backend: object | None = None, error: Exception | None = None) -> None:
        self._backend = backend
        self._error = error

    def get_keyring(self) -> object:
        if self._error is not None:
            raise self._error
        return self._backend


@pytest.fixture(autouse=True)
def _reset_czn_process_state():
    czn_backend._reset_process_state()
    yield
    czn_backend._reset_process_state()


@pytest.fixture
def fake_keyring(monkeypatch: pytest.MonkeyPatch) -> _FakeKeyring:
    kr = _FakeKeyring()
    monkeypatch.setattr(core_secrets, "_keyring_module", lambda: kr)
    return kr


def _cred_backend(tmp_path, remember: bool = False) -> CznBackend:
    ctx = make_context(
        tmp_path,
        "stove-czn",
        {"remember_me": remember, "prefix": str(tmp_path / "prefix")},
    )
    return CznBackend(ctx)


def _notices(events: list) -> list:
    return [
        e
        for e in events
        if e.kind == "status" and (e.data or {}).get("remember_unavailable")
    ]


class TestDefaultCollectionExists:
    def test_none_without_a_keyring_module(self, monkeypatch):
        monkeypatch.setattr(core_secrets, "_keyring_module", lambda: None)
        assert core_secrets.default_collection_exists() is None

    def test_none_when_get_keyring_fails(self, monkeypatch):
        monkeypatch.setattr(
            core_secrets,
            "_keyring_module",
            lambda: _Module(error=RuntimeError("no backend")),
        )
        assert core_secrets.default_collection_exists() is None

    @pytest.mark.parametrize("state", [None, False, True])
    def test_maps_the_no_prompt_probe(self, monkeypatch, state):
        monkeypatch.setattr(core_secrets, "_keyring_module", lambda: _Module(object()))
        monkeypatch.setattr(core_secrets, "_secret_service_collection", lambda backend: state)
        assert core_secrets.default_collection_exists() is state

    def test_probe_does_not_call_a_prompting_keyring_method(self, monkeypatch):
        class Backend:
            __module__ = "keyring.backends.SecretService"

        class PromptingModule(_Module):
            def get_password(self, *args, **kwargs):  # pragma: no cover - assertion is the test
                raise AssertionError("default_collection_exists prompted via keyring")

        monkeypatch.setattr(
            core_secrets, "_keyring_module", lambda: PromptingModule(Backend())
        )
        monkeypatch.setattr(core_secrets, "_secret_service_collection", lambda backend: False)
        assert core_secrets.default_collection_exists() is False


class TestRememberOffWithoutCollection:
    def test_skips_the_keyring_delete_entirely(self, tmp_path, monkeypatch, fake_keyring):
        backend = _cred_backend(tmp_path, remember=True)
        fake_keyring.store = {("nadare-launcher", "token:test"): "{}"}

        def refuse(*args, **kwargs):
            raise AssertionError("keyring.delete_password must not run with no collection")

        monkeypatch.setattr(fake_keyring, "delete_password", refuse)
        monkeypatch.setattr(core_secrets, "default_collection_exists", lambda: False)

        backend.set_remember(False)

        assert backend.ctx.profile["remember_me"] is False
        assert czn_backend._keyring_credential("test") is None

    @pytest.mark.parametrize("exists", [None, True])
    def test_none_or_true_still_deletes(self, tmp_path, monkeypatch, fake_keyring, exists):
        backend = _cred_backend(tmp_path, remember=True)
        fake_keyring.store = {("nadare-launcher", "token:test"): "{}"}
        monkeypatch.setattr(core_secrets, "default_collection_exists", lambda: exists)

        backend.set_remember(False)

        assert fake_keyring.deletes, "delete must still be attempted"

    def test_logout_and_removal_also_skip(self, tmp_path, monkeypatch, fake_keyring):
        backend = _cred_backend(tmp_path, remember=True)
        fake_keyring.store = {("nadare-launcher", "token:test"): "{}"}
        monkeypatch.setattr(core_secrets, "default_collection_exists", lambda: False)

        backend.logout()
        czn_backend.forget_saved_credential("test")

        assert fake_keyring.deletes == []


class TestRememberOnSaveFailure:
    def test_a_raising_store_keeps_the_flag_and_notices(self, tmp_path, monkeypatch, fake_keyring):
        backend = _cred_backend(tmp_path, remember=False)
        backend._save_credential(SENTINEL, "42")  # live session, remember off

        def refuse(service, account, value):
            raise RuntimeError("dialog dismissed")

        monkeypatch.setattr(fake_keyring, "set_password", refuse)
        events: list = []
        backend.ctx.emit = events.append

        backend.set_remember(True)

        assert backend.ctx.profile["remember_me"] is True
        assert fake_keyring.store == {}
        assert _notices(events), events
        assert "Remember me unavailable" in _notices(events)[0].message

    def test_an_unusable_store_keeps_the_flag_and_notices(self, tmp_path, monkeypatch):
        monkeypatch.setattr(core_secrets, "_keyring_module", lambda: None)
        backend = _cred_backend(tmp_path, remember=False)
        backend._save_credential(SENTINEL, "42")
        events: list = []
        backend.ctx.emit = events.append

        backend.set_remember(True)

        assert backend.ctx.profile["remember_me"] is True
        assert _notices(events), events


class TestSecretServiceCollectionProbe:
    """Only a provably missing collection reads as False; errors are unknown."""

    @staticmethod
    def _backend():
        class Backend:
            __module__ = "keyring.backends.SecretService"

        return Backend()

    def _install(self, monkeypatch, *, dbus=None, collection=None):
        import sys
        import types

        exceptions = types.ModuleType("secretstorage.exceptions")

        class ItemNotFoundException(Exception):
            pass

        exceptions.ItemNotFoundException = ItemNotFoundException
        module = types.ModuleType("secretstorage")
        module.exceptions = exceptions

        class Bus:
            def close(self):
                pass

        def dbus_init():
            if dbus is not None:
                raise dbus
            return Bus()

        def Collection(bus):  # noqa: N802 - mirrors secretstorage
            if collection == "missing":
                raise ItemNotFoundException("no default")
            if isinstance(collection, Exception):
                raise collection
            return object()

        module.dbus_init = dbus_init
        module.Collection = Collection
        monkeypatch.setitem(sys.modules, "secretstorage", module)
        monkeypatch.setitem(sys.modules, "secretstorage.exceptions", exceptions)

    def test_missing_collection_is_false(self, monkeypatch):
        self._install(monkeypatch, collection="missing")
        assert core_secrets._secret_service_collection(self._backend()) is False

    def test_existing_collection_is_true(self, monkeypatch):
        self._install(monkeypatch)
        assert core_secrets._secret_service_collection(self._backend()) is True

    def test_unreachable_bus_is_unknown(self, monkeypatch):
        self._install(monkeypatch, dbus=RuntimeError("no bus"))
        assert core_secrets._secret_service_collection(self._backend()) is None

    def test_unexpected_error_is_unknown(self, monkeypatch):
        self._install(monkeypatch, collection=RuntimeError("boom"))
        assert core_secrets._secret_service_collection(self._backend()) is None

    def test_other_backend_is_unknown(self):
        class Other:
            __module__ = "keyring.backends.kwallet"

        assert core_secrets._secret_service_collection(Other()) is None
