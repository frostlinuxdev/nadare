# SPDX-License-Identifier: GPL-3.0-or-later
# Copyright (C) 2026 Nadare contributors
"""Netmarble sign-in for Nadare games (see :mod:`nadare.core.netmarble`).

``login()`` opens Netmarble's own sign-in page in the user's browser and waits for the
redirect on a one-shot loopback server; no password passes through Nadare. The launcher
token it yields is kept in memory for the session and, with "Remember me", in the OS
keyring only (never in a plaintext file). ``launch()`` hands it to the game as
``NMAUTH_TOKEN`` and syncs the matching device key into the Wine prefix.
"""

from __future__ import annotations

import json
import secrets
import threading
import webbrowser
from typing import Any

from ..core import netmarble
from ..core.backends import BackendError
from ..core.events import Event
from ..core.netmarble import Session
from ..core.secrets import SecretStore, SecretStoreError

#: Why a launch has no usable login. Exit code 3 matches the other login-gated games.
LOGIN_REQUIRED = "Log in to Netmarble first (the Log in button), then launch again."
LOGIN_EXPIRED = "Your Netmarble sign-in has expired. Log in again."
KEYRING_NOTICE = (
    "Remember me needs a working system keyring (Secret Service / KWallet); "
    "this sign-in will not be kept after Nadare closes."
)

_LOCK = threading.Lock()
#: Sessions signed in during this process, by profile name.
_LIVE: dict[str, Session] = {}
#: Keyring reads, once per process (``status()`` polls; the keyring must not).
_CACHE: dict[str, Session | None] = {}


def _store() -> SecretStore:
    """Token storage that refuses the plaintext fallback."""
    return SecretStore(allow_fallback=False, kind="netmarble")


def _encode(session: Session) -> str:
    return json.dumps(
        {
            "token": session.token,
            "expires_at": session.expires_at,
            "player_id": session.player_id,
            "device_key": session.device_key,
            "name": session.name,
        }
    )


def _decode(raw: str | None) -> Session | None:
    if not raw:
        return None
    try:
        data = json.loads(raw)
        return Session(
            token=str(data["token"]),
            expires_at=int(data["expires_at"]),
            player_id=str(data.get("player_id", "")),
            device_key=str(data["device_key"]),
            name=str(data.get("name", "")),
        )
    except (ValueError, KeyError, TypeError):
        return None


def reset_process_state() -> None:
    """Forget the in-memory sessions and cached keyring reads (tests, logout everywhere)."""
    with _LOCK:
        _LIVE.clear()
        _CACHE.clear()


class NetmarbleLoginMixin:
    """Mix into a backend (before its base class) to give it a Netmarble login."""

    supports_login = True
    supports_remember_me = True
    #: Seconds the sign-in page is waited for.
    login_timeout = 300.0

    # Provided by the backend this is mixed into.
    ctx: Any

    @staticmethod
    def forget_saved_login(profile: str) -> None:
        with _LOCK:
            _CACHE.pop(profile, None)
            _LIVE.pop(profile, None)
        _store().delete(profile)

    # ---- session ----------------------------------------------------------
    def _remember_enabled(self) -> bool:
        return bool(self.ctx.profile.get("remember_me", False))

    def _device_key(self) -> str:
        saved = str(self.ctx.profile.get("nm_device_key") or "")
        try:
            netmarble.auth_url(1, saved)  # validates the shape
            return saved
        except netmarble.NetmarbleError:
            key = netmarble.new_device_key()
            self.ctx.set("nm_device_key", key)
            return key

    def _session(self) -> Session | None:
        name = self.ctx.name
        with _LOCK:
            live = _LIVE.get(name)
            if live is not None:
                return live
            if not self._remember_enabled():
                return None
            if name not in _CACHE:
                try:
                    saved = _decode(_store().get(name))
                except SecretStoreError:
                    return None
                _CACHE[name] = saved
            return _CACHE[name]

    def _keep(self, session: Session) -> None:
        name = self.ctx.name
        with _LOCK:
            _LIVE[name] = session
        if not self._remember_enabled():
            return
        store = _store()
        if not store.secure:
            self.ctx.emit(Event("status", KEYRING_NOTICE, data={"remember_unavailable": True}))
            return
        try:
            store.set(name, _encode(session))
        except SecretStoreError:
            self.ctx.emit(Event("status", KEYRING_NOTICE, data={"remember_unavailable": True}))
            return
        with _LOCK:
            _CACHE[name] = session

    # ---- GameBackend contract ----------------------------------------------
    def status(self):  # type: ignore[no-untyped-def]
        result = super().status()  # type: ignore[misc]
        session = self._session()
        expired = bool(session and netmarble.is_expired(session))
        result.data["logged_in"] = bool(session) and not expired
        # The game cannot start without a token: ask for the login before Launch.
        result.data["login_required"] = result.state != "not_installed" and (
            not session or expired
        )
        result.data["login_expired"] = expired
        if session is not None:
            result.data["account"] = session.name or session.player_id
        return result

    def _open_browser(self, url: str) -> bool:
        try:
            return bool(webbrowser.open(url))
        except Exception:  # noqa: BLE001 - a missing browser must not abort the sign-in
            return False

    def login(self) -> None:
        device_key = self._device_key()
        language = str(getattr(self.ctx.config, "data", {}).get("language") or "en")
        state = secrets.token_urlsafe(16)
        with netmarble.CallbackServer(state=state) as server:
            url = netmarble.auth_url(server.port, device_key, language, state=state)
            self.ctx.emit(Event("step", "Opening the Netmarble sign-in in your browser…"))
            if not self._open_browser(url):
                self.ctx.emit(
                    Event(
                        "status",
                        f"Open this link in your browser to sign in: {url}",
                        data={"login_url": url},
                    )
                )
            try:
                token = server.wait(self.login_timeout, self.ctx.cancel)
            except netmarble.NetmarbleError as exc:
                raise BackendError(f"Netmarble sign-in failed: {exc}", 3) from exc
        name = netmarble.fetch_user_name(token)
        try:
            session = netmarble.session_from_token(token, device_key, name=name)
        except netmarble.NetmarbleError as exc:
            raise BackendError(f"Netmarble sign-in failed: {exc}", 3) from exc
        self._keep(session)
        who = f" as {session.name}" if session.name else ""
        self.ctx.emit(Event("done", f"Logged in to Netmarble{who}"))

    def logout(self) -> None:
        name = self.ctx.name
        with _LOCK:
            _LIVE.pop(name, None)
            _CACHE[name] = None
        failed = False
        try:
            _store().delete(name)
        except SecretStoreError:
            failed = True
        if failed:
            self.ctx.emit(
                Event("status", "The saved Netmarble login could not be removed from the keyring.")
            )
            self.ctx.emit(
                Event(
                    "done",
                    "Logged out of Netmarble for this session, but the saved login could not "
                    "be removed from the keyring and will return after a restart.",
                )
            )
            return
        self.ctx.emit(Event("done", "Logged out of Netmarble"))

    def set_remember(self, enabled: bool, persist_flag: bool = True) -> None:
        enabled = bool(enabled)
        if persist_flag:
            self.ctx.set("remember_me", enabled)
        name = self.ctx.name
        if not enabled:
            with _LOCK:
                _CACHE[name] = None
            try:
                _store().delete(name)
            except SecretStoreError as exc:
                raise BackendError(
                    "Could not remove the saved Netmarble login from the keyring.", 1
                ) from exc
            return
        with _LOCK:
            live = _LIVE.get(name)
        if live is not None:
            self._keep(live)

    # ---- launch ------------------------------------------------------------
    def launch_extra_args(self) -> list[str]:
        # The game reads NMAUTH_TOKEN from argv, so the token appears in
        # /proc/<pid>/cmdline while it runs (see core/netmarble.launch_args).
        session = self._session()
        return netmarble.launch_args(session) if session else []

    def launch_extra_env(self) -> dict[str, str]:
        session = self._session()
        return netmarble.launch_env(session) if session else {}

    def launch_secrets(self) -> tuple[str, ...]:
        session = self._session()
        return (session.token,) if session else ()

    def launch(self) -> int:
        target = self.direct_target()  # type: ignore[attr-defined]
        if target is None or not target.is_file():
            raise BackendError(f"game executable not found: {target}")
        session = self._session()
        if session is None:
            raise BackendError(LOGIN_REQUIRED, 3)
        if netmarble.is_expired(session):
            raise BackendError(LOGIN_EXPIRED, 3)
        try:
            if netmarble.ensure_wine_device_key(self.prefix(), session.device_key):  # type: ignore[attr-defined]
                self.info("Netmarble device key synced into the Wine prefix")
        except (netmarble.NetmarbleError, OSError) as exc:
            self.info(f"could not sync the Netmarble device key: {exc}")
        return super().launch()  # type: ignore[misc]
