# SPDX-License-Identifier: GPL-3.0-or-later
# Copyright (C) 2026 Nadare contributors
"""Optional OS-keyring token storage with a documented plaintext fallback.

Tokens are never logged and never written to ``config.json``. When the
``keyring`` package and a working backend are present, tokens live in the
system keyring (Secret Service / KWallet / macOS Keychain / Windows Credential
Manager). Otherwise :class:`SecretStore` falls back to ``secrets.json`` beside
the config, created ``0600``: file permissions are the *only* protection there,
so the fallback is best-effort and is logged once at WARNING level.

Account credentials (the CZN launcher refresh token) must not use that
fallback: they construct ``SecretStore(allow_fallback=False)``, for which a
missing/failed keyring means *nothing is persisted*. Set
``NADARE_NO_KEYRING=1`` to disable the keyring entirely, which then means
"never remember" for account credentials; the plaintext fallback stays
available only for non-account data.
"""

from __future__ import annotations

import json
import os
import stat
import tempfile
import threading
from pathlib import Path

from . import paths

SERVICE = "nadare-launcher"
FALLBACK_NAME = "secrets.json"

#: Account-id namespace used before accounts were scoped per consumer/kind.
#: Kept so a token written by an older Nadare is still readable.
LEGACY_KIND = "token"


class SecretStoreError(RuntimeError):
    """Storing or reading a token failed."""


def _log() -> object:
    # Imported lazily so importing this module never requires logging setup.
    try:
        from .logging import get_logger

        return get_logger("nadare.secrets")
    except Exception:  # pragma: no cover - logging is stdlib, this is belt+braces
        import logging

        return logging.getLogger("nadare.secrets")


def _keyring_module() -> object | None:
    """Return the ``keyring`` module if usable, else None."""
    if os.environ.get("NADARE_NO_KEYRING"):
        return None
    try:
        import keyring  # type: ignore
        from keyring.backends.fail import Keyring as FailKeyring  # type: ignore

        backend = keyring.get_keyring()
        if isinstance(backend, FailKeyring):
            return None
        if hasattr(backend, "backends") and not backend.backends:
            return None
        return keyring
    except Exception:
        return None


def _is_missing_secret(exc: BaseException) -> bool:
    """True for the keyring's "no such entry" error, which delete may ignore.

    Anything else (a locked keyring, a backend failure) must surface so
    callers do not believe a token was removed when it was not.
    """
    try:
        from keyring.errors import PasswordDeleteError  # type: ignore

        if isinstance(exc, PasswordDeleteError):
            return True
    except Exception:  # pragma: no cover - keyring is optional
        pass
    return isinstance(exc, KeyError)


class SecretStore:
    """A tiny ``profile -> token`` store.

    ``available`` is True when the OS keyring is in use. The fallback is still
    fully functional; callers that prefer not to persist plaintext can check
    :attr:`secure` and ask the user to log in each time.
    """

    def __init__(
        self,
        path: Path | None = None,
        *,
        allow_fallback: bool = True,
        kind: str = LEGACY_KIND,
    ) -> None:
        self._keyring = _keyring_module()
        self._path = path or (paths.config_dir() / FALLBACK_NAME)
        self._allow_fallback = allow_fallback
        #: Scopes the account id per consumer/backend so two stores sharing a
        #: profile name (and the one plaintext file) cannot collide.
        self._kind = str(kind) or LEGACY_KIND
        self._lock = threading.Lock()
        self._warned = False

    # ---- capability --------------------------------------------------------
    @property
    def secure(self) -> bool:
        return self._keyring is not None

    @property
    def available(self) -> bool:
        return self.secure or self._allow_fallback

    def _warn_fallback(self) -> None:
        if self._warned:
            return
        self._warned = True
        _log().warning(
            "keyring unavailable; storing tokens in %s with mode 0600 (plaintext). "
            "Install python-keyring and a Secret Service backend for real protection.",
            self._path,
        )

    def _account(self, profile: str) -> str:
        return f"{self._kind}:{profile}"

    @staticmethod
    def _legacy_account(profile: str) -> str:
        return f"{LEGACY_KIND}:{profile}"

    def _accounts(self, profile: str) -> list[str]:
        """The scoped account id, plus the legacy one when it differs."""
        account = self._account(profile)
        legacy = self._legacy_account(profile)
        return [account] if account == legacy else [account, legacy]

    # ---- keyring path ------------------------------------------------------
    def _kget(self, account: str) -> str | None:
        try:
            value = self._keyring.get_password(SERVICE, account)  # type: ignore[attr-defined]
        except Exception as exc:  # noqa: BLE001
            raise SecretStoreError(f"keyring read failed: {exc}") from exc
        return value or None

    def _kset(self, account: str, token: str) -> None:
        try:
            self._keyring.set_password(SERVICE, account, token)  # type: ignore[attr-defined]
        except Exception as exc:  # noqa: BLE001
            raise SecretStoreError(f"keyring write failed: {exc}") from exc

    def _kdel(self, account: str) -> None:
        try:
            self._keyring.delete_password(SERVICE, account)  # type: ignore[attr-defined]
        except Exception as exc:  # noqa: BLE001
            if _is_missing_secret(exc):
                # Deleting a token that is not there is success.
                return
            raise SecretStoreError(f"keyring delete failed: {exc}") from exc

    # ---- plaintext fallback ------------------------------------------------
    def _load_file(self) -> dict[str, str]:
        self._ensure_private()
        try:
            raw = json.loads(self._path.read_text(encoding="utf-8"))
        except FileNotFoundError:
            return {}
        except Exception as exc:  # noqa: BLE001
            raise SecretStoreError(f"cannot read {self._path}: {exc}") from exc
        if not isinstance(raw, dict):
            # Valid JSON that is not an object (a truncated write, a tampered
            # file, a bad backup restore) must not read as "no saved tokens":
            # callers would believe the user has no login and overwrite it.
            raise SecretStoreError(
                f"cannot read {self._path}: expected a JSON object, got {type(raw).__name__}"
            )
        return {str(k): str(v) for k, v in raw.items()}

    def _ensure_private(self) -> None:
        """Re-assert 0600 on an existing fallback file before reading it.

        The file may predate this store or have been restored by a backup tool
        with laxer permissions; reading (and later writing) it world-readable
        would defeat the only protection the fallback has.
        """
        want = stat.S_IRUSR | stat.S_IWUSR
        try:
            mode = stat.S_IMODE(self._path.stat().st_mode)
        except OSError:
            return
        if mode == want:
            return
        try:
            os.chmod(self._path, want)
        except OSError:
            return
        _log().warning("tightened %s permissions to 0600 (was %s)", self._path, oct(mode))

    def _save_file(self, data: dict[str, str]) -> None:
        self._path.parent.mkdir(parents=True, exist_ok=True)
        fd, tmp = tempfile.mkstemp(dir=str(self._path.parent), prefix=".secrets-", suffix=".tmp")
        try:
            with os.fdopen(fd, "w", encoding="utf-8") as fh:
                json.dump(data, fh, indent=2, sort_keys=True)
                fh.write("\n")
            os.chmod(tmp, stat.S_IRUSR | stat.S_IWUSR)  # 0600
            os.replace(tmp, self._path)
        finally:
            if os.path.exists(tmp):
                os.unlink(tmp)

    # ---- public API --------------------------------------------------------
    def get(self, profile: str) -> str | None:
        accounts = self._accounts(profile)
        if self.secure:
            for account in accounts:
                value = self._kget(account)
                if value is not None:
                    return value
            return None
        if not self._allow_fallback:
            return None
        self._warn_fallback()
        with self._lock:
            data = self._load_file()
            for account in accounts:
                if account in data:
                    return data[account]
            return None

    def set(self, profile: str, token: str) -> None:
        if not token:
            self.delete(profile)
            return
        account = self._account(profile)
        if self.secure:
            self._kset(account, token)
            return
        if not self._allow_fallback:
            raise SecretStoreError("no keyring available and plaintext fallback disabled")
        self._warn_fallback()
        with self._lock:
            data = self._load_file()
            data[account] = token
            self._save_file(data)

    def delete(self, profile: str) -> None:
        accounts = self._accounts(profile)
        if self.secure:
            # With no default collection there is nothing to delete, and any
            # keyring call would create it (and pop a password dialog) first.
            if default_collection_exists() is False:
                return
            for account in accounts:
                self._kdel(account)
            return
        if not self._allow_fallback:
            return
        with self._lock:
            data = self._load_file()
            removed = False
            for account in accounts:
                if data.pop(account, None) is not None:
                    removed = True
            if removed:
                self._save_file(data)

    def profiles(self) -> list[str]:
        """Profile names that currently have a stored token (fallback only)."""
        if self.secure:
            return []
        prefixes = (f"{self._kind}:", f"{LEGACY_KIND}:")
        with self._lock:
            return [
                key.split(":", 1)[1]
                for key in self._load_file()
                if any(key.startswith(prefix) for prefix in prefixes)
            ]


#: Module-level convenience store mirroring the old function style.
_default = SecretStore()


def get_token(profile: str) -> str | None:
    return _default.get(profile)


def set_token(profile: str, token: str) -> None:
    _default.set(profile, token)


def delete_token(profile: str) -> None:
    _default.delete(profile)


def keyring_available() -> bool:
    return _default.secure


def _secret_service_state(backend: object, name: str) -> tuple[bool, str] | None:
    """Look at the Secret Service default collection *without* prompting.

    ``keyring.get_password`` creates the default collection when it is missing
    and unlocks it when locked, which opens a password dialog on GNOME/Cinnamon
    and blocks until it is answered. A diagnostic must not do that (#162), so
    this only reads whether the collection exists and whether it is locked.
    Returns None when the backend is not the Secret Service one, so the caller
    falls back to the generic probe.
    """
    members = getattr(backend, "backends", None) or [backend]
    if not any(
        type(b).__module__.startswith("keyring.backends.SecretService") for b in members
    ):
        return None
    try:
        import secretstorage  # type: ignore
        from secretstorage.exceptions import ItemNotFoundException  # type: ignore
    except Exception:  # noqa: BLE001 - let the generic probe report it
        return None
    try:
        bus = secretstorage.dbus_init()
    except Exception as exc:  # noqa: BLE001
        return False, f"{name} cannot reach the Secret Service ({exc.__class__.__name__}: {exc})"
    try:
        try:
            collection = secretstorage.Collection(bus)
        except ItemNotFoundException:
            return False, (
                f"{name}: no default keyring exists yet; it is created, with a password "
                "prompt, the first time a login is remembered"
            )
        locked = collection.is_locked()
    except Exception as exc:  # noqa: BLE001
        return False, f"{name} cannot read the default collection ({exc.__class__.__name__}: {exc})"
    finally:
        try:
            bus.close()
        except Exception:  # noqa: BLE001
            pass
    if locked:
        return True, f"{name} (default keyring is locked; you will be asked to unlock it)"
    return True, name


def _secret_service_collection(backend: object) -> bool | None:
    """Exactly whether the Secret Service default collection exists, no prompting.

    ``True``/``False`` only when the service answered; ``None`` for anything else
    (not the Secret Service backend, no ``secretstorage``, bus unreachable, or an
    unexpected error), because those do not prove that nothing is stored.
    """
    members = getattr(backend, "backends", None) or [backend]
    if not any(
        type(b).__module__.startswith("keyring.backends.SecretService") for b in members
    ):
        return None
    try:
        import secretstorage  # type: ignore
        from secretstorage.exceptions import ItemNotFoundException  # type: ignore
    except Exception:  # noqa: BLE001
        return None
    try:
        bus = secretstorage.dbus_init()
    except Exception:  # noqa: BLE001
        return None
    try:
        try:
            secretstorage.Collection(bus)
        except ItemNotFoundException:
            return False
        return True
    except Exception:  # noqa: BLE001
        return None
    finally:
        try:
            bus.close()
        except Exception:  # noqa: BLE001
            pass


def default_collection_exists() -> bool | None:
    """Whether the Secret Service default collection exists, without prompting.

    ``True`` when it exists (locked or not), ``False`` only when the service is
    reachable and has no default collection yet, and ``None`` when this cannot
    be told. Callers must treat ``None`` like "unknown" and keep their old
    behaviour; only ``False`` proves there is no entry to read or delete.
    """
    module = _keyring_module()
    if module is None:
        return None
    try:
        backend = module.get_keyring()  # type: ignore[attr-defined]
    except Exception:  # noqa: BLE001 - unknown, not proof of absence
        return None
    return _secret_service_collection(backend)


def keyring_diagnosis() -> tuple[bool, str]:
    """Why the OS keyring is (not) usable, in one line for ``doctor``/bug reports.

    Distinguishes the cases users hit: the package missing from the bundle, no
    Secret Service/KWallet on the session bus, and a service that is running but
    has no default collection (e.g. KWallet with no wallet created).
    """
    if os.environ.get("NADARE_NO_KEYRING"):
        return False, "disabled by NADARE_NO_KEYRING"
    try:
        import keyring  # type: ignore
        from keyring.backends.fail import Keyring as FailKeyring  # type: ignore
    except Exception as exc:  # noqa: BLE001
        return False, f"keyring package not available ({exc.__class__.__name__}: {exc})"
    try:
        backend = keyring.get_keyring()
    except Exception as exc:  # noqa: BLE001
        return False, f"no keyring backend ({exc.__class__.__name__}: {exc})"
    name = f"{backend.__class__.__module__}.{backend.__class__.__name__}"
    if isinstance(backend, FailKeyring) or (
        hasattr(backend, "backends") and not backend.backends
    ):
        return False, "no usable backend: is a Secret Service / KWallet running? (" + name + ")"
    state = _secret_service_state(backend, name)
    if state is not None:
        return state
    try:
        keyring.get_password("nadare-keyring-probe", "probe")
    except Exception as exc:  # noqa: BLE001
        return False, (
            f"{name} cannot open a collection ({exc.__class__.__name__}: {exc}); "
            "create/unlock the default wallet (e.g. KWallet Manager or Seahorse)"
        )
    return True, name

