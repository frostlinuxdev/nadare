# SPDX-License-Identifier: GPL-3.0-or-later
# Copyright (C) 2026 Nadare contributors
"""Where the STOVE login is kept: the keyring, the session folder and the hand-off files.

Split out of :mod:`nadare.backends.czn.backend`; the code is unchanged.
"""

from __future__ import annotations

import contextlib
import hashlib
import json
import logging
import os
import re
from pathlib import Path

from ....core import (
    paths,
    process,
)
from ....core.backends import (
    BackendError,
)
from ....core.events import Event
from ....core.secrets import SecretStore, SecretStoreError, default_collection_exists
from .shared import (
    _KEYRING_CACHE,
    _LIVE_LOCK,
    _LIVE_SESSIONS,
    _live_get,
    _live_set,
    _remember_notice,
)

log = logging.getLogger("nadare.backends.czn.backend")

#: Emitted when the stored credential belongs to a different account.
MISMATCH_NOTICE = "Saved login no longer matches this account; log in again"

#: Emitted when log out could not remove the persisted credential from the OS
#: keyring; local (in-memory) state is still cleared, but the saved login may
#: survive a restart.
LOGOUT_KEYRING_NOTICE = (
    "Logged out, but the saved login could not be removed from the system keyring"
)

#: Temp file the auth worker uses to hand the rotating credential to us.
CREDENTIAL_HANDOFF = "credential.json"

CREDENTIAL_INPUT = "credential-in.json"

#: Bumped whenever a profile's saved login is wiped, so a keyring write that was already running
#: can tell it landed after the wipe and take it back (#956).
_WIPES: dict[str, int] = {}


def _live_clear(profile: str) -> None:
    with _LIVE_LOCK:
        _LIVE_SESSIONS.pop(profile, None)


def _cache_keyring(profile: str, cred: dict[str, str] | None) -> None:
    with _LIVE_LOCK:
        _KEYRING_CACHE[profile] = cred


def forget_saved_credential(profile: str) -> None:
    """Delete a profile's persisted credential without touching its live session.

    Used when Remember me is switched off while the profile is busy, so the
    keyring entry cannot be cleared by the normal ``set_remember`` worker.
    """
    try:
        SecretStore(allow_fallback=False).delete(profile)
    except SecretStoreError:
        # A locked/unavailable keyring is transient: propagate so the GUI can
        # report a failed wipe, and do not cache a "forgotten" state that would
        # only last until the process restarts (the entry is still there).
        raise
    _cache_keyring(profile, None)


def _read_keyring(profile: str) -> dict[str, str] | None:
    """Read the persisted credential, or ``None`` when there definitively is none.

    A :class:`SecretStoreError` (locked service, D-Bus hiccup) propagates so the
    caller can avoid caching a transient failure.
    """
    raw = SecretStore(allow_fallback=False).get(profile)
    if not raw:
        return None
    try:
        data = json.loads(raw)
    except ValueError:
        return None
    if not isinstance(data, dict) or not data.get("refresh_token"):
        return None
    return {
        "refresh_token": str(data["refresh_token"]),
        "member_no": str(data.get("member_no") or ""),
    }


def _keyring_credential(profile: str) -> dict[str, str] | None:
    with _LIVE_LOCK:
        if profile in _KEYRING_CACHE:
            return _KEYRING_CACHE[profile]
    try:
        cred = _read_keyring(profile)
    except SecretStoreError:
        # A locked/unavailable keyring is transient: report "no credential" now
        # but do not cache it, so a later poll can retry once it unlocks.
        return None
    with _LIVE_LOCK:
        return _KEYRING_CACHE.setdefault(profile, cred)


def _profile_slug(name: str) -> str:
    """A filesystem-safe, collision-free path component for a profile name.

    A name that is already safe keeps its readable form. Anything else is
    sanitised *and* suffixed with a short hash of the exact name, so two
    distinct valid names ("a b" / "a_b") can never share a handoff directory
    and cross-assign a rotating refresh token.
    """
    if re.fullmatch(r"[A-Za-z0-9_-][A-Za-z0-9._-]*", name):
        return name
    safe = re.sub(r"[^A-Za-z0-9._-]", "_", name).lstrip(".") or "profile"
    digest = hashlib.sha256(name.encode("utf-8", "surrogatepass")).hexdigest()[:8]
    return f"{safe}-{digest}"


def _write_private(path: Path, text: str) -> None:
    """Atomically write a file that is 0600 from creation."""
    path.parent.mkdir(parents=True, exist_ok=True)
    tmp = path.with_name(path.name + ".tmp")
    fd = os.open(tmp, os.O_WRONLY | os.O_CREAT | os.O_TRUNC, 0o600)
    with os.fdopen(fd, "w") as fh:
        fh.write(text)
    os.chmod(tmp, 0o600)
    os.replace(tmp, path)


class CredentialsMixin:
    """Mixin for :class:`CznBackend`; see the module text."""

    @staticmethod
    def forget_saved_login(profile: str) -> None:
        forget_saved_credential(profile)

    @staticmethod
    def drop_live_session(profile: str) -> None:
        """Forget the login held in memory, so a profile added later under this name cannot
        adopt the old account (#931). Removal calls it; turning Remember me off does not."""
        _live_clear(profile)

    # ---- paths -------------------------------------------------------------
    def _auth_dir(self) -> Path:
        return self.ctx.profile_dir("auth")

    def _session_dir(self) -> Path:
        """A private, host-only dir for the auth credential handoff.

        ``$XDG_RUNTIME_DIR/nadare/<profile>/`` (0700) when the runtime dir is
        set, otherwise the profile data dir. This is *not* used for
        ``session.json``: the umu/Steam-Runtime sandbox does not mount
        ``$XDG_RUNTIME_DIR`` (only a few sockets), so the game cannot read a
        session there.
        """
        runtime = os.environ.get("XDG_RUNTIME_DIR")
        if runtime:
            base = Path(runtime) / paths.app_name() / _profile_slug(self.ctx.name)
        else:
            base = self._data_dir()
        try:
            base.mkdir(parents=True, exist_ok=True)
            os.chmod(base, 0o700)
        except OSError:
            pass
        return base

    def _session(self) -> Path:
        """The game session file, placed next to the stub executable.

        The sandbox can read the directory the stub runs from (the proven
        czn-linux ``tools/session.json`` layout); ``$XDG_RUNTIME_DIR`` is only
        partially bind-mounted and is invisible to the game.
        """
        return self._stub().parent / "session.json"

    # ---- credentials (keyring only, never plaintext) -----------------------
    def _store(self) -> SecretStore:
        """Token storage that refuses the plaintext fallback."""
        return SecretStore(allow_fallback=False)

    def _remember_enabled(self) -> bool:
        return bool(self.ctx.profile.get("remember_me", False))

    def set_remember(self, enabled: bool, persist_flag: bool = True) -> None:
        """Toggle per-profile "Remember me"; turning it off wipes the keyring.

        ``persist_flag=False`` keeps the caller (the Settings page) as the
        single owner of the ``remember_me`` config write, while the credential
        side effect still happens here.
        """
        enabled = bool(enabled)
        if persist_flag:
            self.ctx.set("remember_me", enabled)
        if not enabled:
            if not self._delete_credential():
                # A locked keyring keeps the saved login; surface the failure
                # instead of pretending Remember me (and the token) is gone.
                raise BackendError(LOGOUT_KEYRING_NOTICE, 1)
            return
        # Persist the current live session immediately (if any): enabling the
        # toggle must not require a fresh login/launch to save the token.
        live = _live_get(self.ctx.name)
        if live:
            self._save_credential(live["refresh_token"], live.get("member_no"))
        elif self._refresh_token:
            self._save_credential(self._refresh_token, self._member_no)
        elif not self._store().secure:
            # Nothing can be saved until the keyring works; do not leave the
            # box ticked without telling the user why the login will not last.
            self._notify_remember_unavailable()

    def _load_credential(self) -> dict[str, str] | None:
        """Read the persisted credential, at most once per process.

        ``status()`` polls frequently; the keyring must not be unlocked on
        every call, so the first read is cached for the life of the process.
        """
        return _keyring_credential(self.ctx.name)

    def _save_credential(self, refresh_token: str, member_no: str | None) -> None:
        """Keep the live session in memory; persist only if remember is on.

        The keyring entry is a single JSON blob holding *only* the launcher
        refresh token and the member number. Access and game tokens are never
        persisted.
        """
        if not refresh_token:
            return
        member = str(member_no or "")
        self._refresh_token = refresh_token
        self._member_no = member or None
        _live_set(self.ctx.name, refresh_token, member)
        if not self._remember_enabled():
            return
        store = self._store()
        if not store.secure:
            self._notify_remember_unavailable()
            return
        cred = {"refresh_token": refresh_token, "member_no": member}
        wipes = _WIPES.get(self.ctx.name, 0)
        try:
            store.set(self.ctx.name, json.dumps(cred))
        except SecretStoreError:
            # STOVE has already rotated the token, so any entry still in the
            # keyring is superseded: restoring it next start would only hit
            # 44010. Drop it rather than leave a dead credential behind.
            self._delete_credential()
            self._notify_remember_unavailable()
            return
        if _WIPES.get(self.ctx.name, 0) != wipes:
            # Remember me was turned off (or the account forgotten) while the write ran.
            self._delete_credential()
            return
        _cache_keyring(self.ctx.name, cred)

    def _delete_credential(self) -> bool:
        """Delete the persisted credential; report whether the keyring refused.

        A locked/unavailable keyring raises :class:`SecretStoreError`. The
        in-memory state is still cleared by the caller (we are logged out), but
        the persisted entry may survive, so the failure is surfaced rather than
        silently reported as a successful removal.
        """
        deleted = True
        _WIPES[self.ctx.name] = _WIPES.get(self.ctx.name, 0) + 1
        # No default collection means nothing was ever stored; deleting would
        # create the collection and pop the "Choose password for new keyring"
        # dialog. Only False proves that, so None/True keep deleting as before.
        if default_collection_exists() is not False:
            try:
                self._store().delete(self.ctx.name)
            except SecretStoreError:
                deleted = False
        _cache_keyring(self.ctx.name, None)
        return deleted

    def logout(self) -> None:
        """Forget the account everywhere: keyring, cache and process memory.

        Runs under the rotation lock, so a renewal in progress (here or in another Nadare
        command) cannot save the login again after it (#957).
        """
        with self._rotation_lock(wait=30):
            deleted = self._delete_credential()
            _live_clear(self.ctx.name)
            self._refresh_token = None
            self._member_no = None
            self._restore_attempted = True
            self._restored = False
        if not deleted:
            self.ctx.emit(Event("status", LOGOUT_KEYRING_NOTICE))
            self.info(LOGOUT_KEYRING_NOTICE)
        self.ctx.emit(Event("done", "Logged out of STOVE"))

    def _notify_remember_unavailable(self) -> None:
        if self._remember_notice_shown:
            return
        self._remember_notice_shown = True
        self.ctx.emit(Event("status", _remember_notice(), data={"remember_unavailable": True}))

    def _notify_mismatch(self) -> None:
        self.ctx.emit(Event("status", MISMATCH_NOTICE))

    def _handoff_path(self) -> Path:
        return self._session_dir() / CREDENTIAL_HANDOFF

    def _input_path(self) -> Path:
        return self._session_dir() / CREDENTIAL_INPUT

    def _read_handoff_credential(self) -> dict[str, str] | None:
        path = self._handoff_path()
        try:
            raw = path.read_text(encoding="utf-8")
        except OSError:
            return None
        try:
            path.unlink()
        except OSError:
            pass
        try:
            data = json.loads(raw)
        except ValueError:
            return None
        if not isinstance(data, dict) or not data.get("refresh_token"):
            return None
        return {
            "refresh_token": str(data["refresh_token"]),
            "member_no": str(data.get("member_no") or ""),
        }

    def _cleanup_stale_session(self) -> None:
        """Drop session files left by a crash — never while our game runs."""
        if self._running():
            return
        for path in (
            self._session(),
            self._data_dir() / "session.json",
            self._session_dir() / "session.json",
        ):
            try:
                path.unlink()
            except OSError:
                pass

    def _migrate_legacy_state(self) -> None:
        """Move a legacy plaintext ``launcher_refresh`` out of czn_auth.json."""
        path = self._auth_dir() / "czn_auth.json"
        try:
            state = json.loads(path.read_text(encoding="utf-8"))
        except (OSError, ValueError):
            return
        if not isinstance(state, dict) or not state.get("launcher_refresh"):
            return
        refresh = str(state.pop("launcher_refresh"))
        member_no = str(state.get("member_no") or "")
        # Only keep it if the user opted in *and* the keyring is usable;
        # otherwise the plaintext token is dropped (never remembered).
        if self._remember_enabled():
            try:
                store = self._store()
                if store.secure:
                    cred = {"refresh_token": refresh, "member_no": member_no}
                    store.set(self.ctx.name, json.dumps(cred))
                    _cache_keyring(self.ctx.name, cred)
            except SecretStoreError:
                # A transient keyring failure must not destroy the only copy of
                # the saved login: leave the plaintext token for a later attempt.
                self._notify_remember_unavailable()
                return
        try:
            _write_private(path, json.dumps(state, separators=(",", ":")))
        except OSError:
            pass

    def _stage_input_credential(self) -> None:
        """Write the live token to a 0600 file the worker reads and deletes."""
        if not self._refresh_token:
            return
        path = self._input_path()
        path.parent.mkdir(parents=True, exist_ok=True)
        fd = os.open(path, os.O_WRONLY | os.O_CREAT | os.O_TRUNC, 0o600)
        with os.fdopen(fd, "w", encoding="utf-8") as f:
            json.dump({"refresh_token": self._refresh_token}, f)

    def _forget_credential(self) -> None:
        """Drop the stored and in-memory credential (keyring, live cache, ids)."""
        self._delete_credential()
        _live_clear(self.ctx.name)
        self._refresh_token = None
        self._member_no = None

    @contextlib.contextmanager
    def _rotation_lock(self, wait: float):
        """Serialise token rotation for this profile across processes (#28).

        STOVE rotates the refresh token on every sign-in, so two processes
        renewing the same chain would leave one with a dead token.
        """
        lock = process.ProfileLock(self.ctx.name, wait=wait)
        try:
            lock.acquire()
        except process.ProfileLockError:
            raise BackendError(
                "Another Nadare window or command is signing this account in. "
                "Wait a moment and try again.",
                1,
            ) from None
        try:
            yield
        finally:
            lock.release()

    def _adopt_newer_credential(self) -> None:
        """Under the lock, prefer the keyring token another process just rotated."""
        if not self._remember_enabled():
            return
        try:
            cred = _read_keyring(self.ctx.name)
        except SecretStoreError:
            return
        if cred and cred["refresh_token"] != self._refresh_token:
            self._refresh_token = cred["refresh_token"]
            self._member_no = cred["member_no"] or None
            _live_set(self.ctx.name, cred["refresh_token"], cred["member_no"])
            _cache_keyring(self.ctx.name, cred)
