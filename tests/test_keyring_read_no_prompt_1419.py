# SPDX-License-Identifier: GPL-3.0-or-later
"""#1419: reading a saved sign-in must never create a keyring.

``keyring.get_password`` creates the Secret Service default collection when it is missing, and
that opens gcr's "Choose password for new keyring" dialog and blocks until it is answered. Seen
on the KDE test desktop (no KWallet, gnome-keyring, no keyring yet): `nadare status -p CZN` hung
for minutes, and the game page sat on "CHECKING…". With no default collection nothing can have
been saved, so a read is "nothing saved" without any keyring call. Saving may still create it.

No test touches a real keyring: the keyring module is a fake and the no-prompt probe is stubbed.
"""

from __future__ import annotations

import pytest
from _backend_helpers import make_context

from nadare.backends.czn import backend as czn_backend
from nadare.backends.czn.backend import CznBackend
from nadare.backends.czn.backend_parts import credentials as czn_credentials
from nadare.core import secrets as core_secrets


class _FakeKeyring:
    def __init__(self) -> None:
        self.store: dict[tuple[str, str], str] = {}
        self.reads: list[tuple[str, str]] = []

    def get_password(self, service: str, account: str) -> str | None:
        self.reads.append((service, account))
        return self.store.get((service, account))

    def set_password(self, service: str, account: str, value: str) -> None:
        self.store[(service, account)] = value

    def delete_password(self, service: str, account: str) -> None:
        self.store.pop((service, account), None)


@pytest.fixture
def fake_keyring(monkeypatch: pytest.MonkeyPatch) -> _FakeKeyring:
    kr = _FakeKeyring()
    monkeypatch.setattr(core_secrets, "_keyring_module", lambda: kr)
    return kr


def test_a_read_with_no_default_collection_makes_no_keyring_call(monkeypatch, fake_keyring):
    monkeypatch.setattr(core_secrets, "default_collection_exists", lambda: False)

    assert core_secrets.SecretStore(allow_fallback=False).get("CZN") is None
    assert fake_keyring.reads == []


@pytest.mark.parametrize("exists", [True, None])
def test_an_existing_or_unknown_collection_is_still_read(monkeypatch, fake_keyring, exists):
    """Upgrade check: a sign-in saved by an older build still reads."""
    fake_keyring.store[(core_secrets.service_name(), "token:CZN")] = "saved"
    monkeypatch.setattr(core_secrets, "default_collection_exists", lambda: exists)

    assert core_secrets.SecretStore(allow_fallback=False).get("CZN") == "saved"
    assert fake_keyring.reads


def test_the_collection_is_checked_once_per_read_not_per_account(monkeypatch, fake_keyring):
    calls = []

    def probe():
        calls.append(1)
        return True

    monkeypatch.setattr(core_secrets, "default_collection_exists", probe)
    store = core_secrets.SecretStore(allow_fallback=False, kind="netmarble")

    store.get("p")  # two account ids (scoped + legacy)

    assert len(calls) == 1
    assert len(fake_keyring.reads) == 2


def test_saving_may_still_create_the_keyring(monkeypatch, fake_keyring):
    monkeypatch.setattr(core_secrets, "default_collection_exists", lambda: False)

    core_secrets.SecretStore(allow_fallback=False).set("CZN", "new")

    assert fake_keyring.store[(core_secrets.service_name(), "token:CZN")] == "new"


def test_czn_status_with_no_keyring_reads_nothing(tmp_path, monkeypatch, fake_keyring):
    """The reported case: the game page's status for a remembered CZN sign-in."""
    monkeypatch.setattr(core_secrets, "default_collection_exists", lambda: False)
    monkeypatch.setattr(czn_credentials, "_KEYRING_CACHE", {})
    ctx = make_context(
        tmp_path, "stove-czn", {"remember_me": True, "prefix": str(tmp_path / "prefix")}
    )
    backend = CznBackend(ctx)

    assert czn_backend._keyring_credential(backend.ctx.name) is None
    assert fake_keyring.reads == []
