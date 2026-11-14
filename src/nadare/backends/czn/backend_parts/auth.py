# SPDX-License-Identifier: GPL-3.0-or-later
# Copyright (C) 2026 Nadare contributors
"""Signing in, the terms, and minting or restoring a game session.

Split out of :mod:`nadare.backends.czn.backend`; the code is unchanged.
"""

from __future__ import annotations

import json
import logging
import time
from typing import Any

from ....core.backends import (
    BackendError,
)
from ....core.events import Event
from .shared import (
    TERMS_URL,
    _live_set,
    _register_ephemeral,
    _unlink_quietly,
)

log = logging.getLogger("nadare.backends.czn.backend")

AUTH_MODULE = "nadare.backends.czn.auth"


class AuthMixin:
    """Mixin for :class:`CznBackend`; see the module text."""

    def _session_info(self) -> dict[str, Any] | None:
        try:
            d = json.loads(self._session().read_text())
            exp = int(d.get("expire_time", "0")) // 1000
            nickname = str(d.get("member_nickname") or "")
            if nickname == "0":
                nickname = ""
            return {
                "path": str(self._session()),
                "expires_at": exp,
                "valid": bool(d.get("access_token")) and exp > time.time(),
                "nickname": nickname,
            }
        except Exception:
            return None

    # ---- login -------------------------------------------------------------
    def login(self) -> None:
        with self._rotation_lock(wait=10):
            self._login_unlocked()

    def _login_unlocked(self) -> None:
        png = self._qr_png()
        png.unlink(missing_ok=True)
        self._auth_dir().mkdir(parents=True, exist_ok=True)
        # A successful login supersedes any startup restore for this process.
        self._restore_attempted = True
        session = self._session()
        # M2: auth must be able to write --out even before the stub exists.
        session.parent.mkdir(parents=True, exist_ok=True)
        # M1: the game token minted by login must not outlive the operation.
        _register_ephemeral(session)
        try:
            code, handoff = self._run_auth(
                ["login", "--out", str(session), "--qr-timeout", "300", "--qr-png", str(png)]
            )
            # M2: renew() already rotated and handed off the credential before a
            # later step (e.g. write_session_json) could fail; ingest it first so
            # a successful STOVE rotation is never dropped.
            if handoff:
                self._save_credential(handoff["refresh_token"], handoff.get("member_no"))
        finally:
            png.unlink(missing_ok=True)
            _unlink_quietly(session)
        if code != 0:
            self._fail_auth("login", code)
        self.ctx.emit(Event("done", "Logged in to STOVE"))

    def _require_login_for_terms(self) -> None:
        if not self._refresh_token:
            raise BackendError("Log in to STOVE first, then Nadare can show the terms.", 3)

    def _keep_terms_handoff(self, handoff: dict[str, str] | None) -> None:
        """Store the rotated credential, but never one that belongs to another account."""
        if not handoff:
            return
        member = handoff.get("member_no") or ""
        if self._member_no and member and member != self._member_no:
            self._forget_credential()
            raise BackendError(
                "The STOVE account changed since you logged in. Log in again.", 3
            )
        self._save_credential(handoff["refresh_token"], member or self._member_no)

    def terms(self) -> dict[str, Any]:
        """The terms this account still has to agree to.

        Returns ``{"pending", "documents", "missing", "member_no"}``: ``missing`` lists pending
        terms STOVE gave no text for (the caller must not ask the user to agree while any), and
        ``member_no`` names the account these terms were read for. Renews the login (STOVE
        rotates the token), so it runs under the same lock as a launch. Nothing is agreed here.
        """
        with self._rotation_lock(wait=30):
            self._adopt_newer_credential()
            self._require_login_for_terms()
            events: list[dict[str, Any]] = []
            code, handoff = self._run_auth(["terms"], collect=events)
            self._keep_terms_handoff(handoff)
        if code != 0:
            self._fail_auth("terms", code)
        found = [e for e in events if e.get("event") == "terms"]
        documents = found[-1].get("documents") if found else None
        if not found or not isinstance(documents, list):
            raise BackendError(
                "STOVE did not return the game's terms. Try again in a minute, or open "
                + TERMS_URL
                + " in your browser.",
                2,
            )
        missing = found[-1].get("missing")
        return {
            "pending": bool(found[-1].get("pending")),
            "documents": documents,
            "missing": missing if isinstance(missing, list) else [],
            "member_no": self._member_no,
        }

    def accept_terms(self, expect_member: str | None = None) -> None:
        """Agree to the game's terms for this account. Call only after the user said yes.

        ``expect_member`` is the account :meth:`terms` showed; if another account is logged in
        by now nothing is sent.
        """
        with self._rotation_lock(wait=30):
            self._adopt_newer_credential()
            self._require_login_for_terms()
            if expect_member and self._member_no and str(self._member_no) != str(expect_member):
                raise BackendError(
                    "A different STOVE account is logged in now. Open the terms again.", 3
                )
            code, handoff = self._run_auth(["accept-terms", "--agree"])
            self._keep_terms_handoff(handoff)
        if code != 0:
            self._fail_auth("accept the terms", code)
        self.ctx.emit(Event("done", "Terms accepted"))

    def _run_auth(
        self, args: list[str], collect: list[dict[str, Any]] | None = None
    ) -> tuple[int, dict[str, str] | None]:
        """Run the auth worker and read its 0600 credential handoff."""
        self._last_auth_error = None
        handoff = self._handoff_path()
        # ``credential.json.tmp`` is the worker's write-then-replace staging
        # file and holds the same rotating token: sweep it with the handoff.
        leftovers = (
            handoff,
            handoff.with_name(handoff.name + ".tmp"),
            self._input_path(),
        )
        for leftover in leftovers:
            _unlink_quietly(leftover)
        try:
            self._stage_input_credential()
            try:
                code = self._run_module(
                    AUTH_MODULE,
                    [*args, "--json"],
                    parse_json=True,
                    collect=collect,
                    extra_env=self._auth_env(),
                )
            except BaseException:
                # A rotation already handed off must survive a cancel/crash.
                data = self._read_handoff_credential()
                if data:
                    self._save_credential(data["refresh_token"], data.get("member_no"))
                raise
            return code, self._read_handoff_credential()
        finally:
            # Never leave the credential handoff on disk, even if the worker
            # crashed or we are cancelled mid-run.
            for leftover in leftovers:
                _unlink_quietly(leftover)

    def _mint_session(self) -> int:
        """One refresh + game-token exchange; writes the ephemeral session."""
        with self._rotation_lock(wait=60):
            self._adopt_newer_credential()
            code = self._mint_session_unlocked()
        if code != 0:
            # The worker may have written session.json before a later step failed; nothing
            # downstream registers it for cleanup yet, so a failed mint must not leave a live
            # game token behind (#916).
            _unlink_quietly(self._session())
        return code

    def _mint_session_unlocked(self) -> int:
        code, handoff = self._run_auth(["token", "--out", str(self._session())])
        if code == 3 and not handoff:
            # STOVE rejected the refresh token (44010): the stored chain is dead.
            # Drop it everywhere, or a fresh backend would adopt it again and
            # status() would keep reporting a login that every launch refutes.
            self._forget_credential()
            return code
        if not handoff:
            return code
        # M2: a rotation handed off before a later network step failed must
        # still be kept, or STOVE has already invalidated the previous token.
        member = handoff.get("member_no") or ""
        if self._member_no and member and member != self._member_no:
            # Checked before anything is adopted, also when the worker failed: a rotation that
            # names another account is never kept as this one (#959).
            self._forget_credential()
            # The worker wrote session.json before the mismatch was detected;
            # remove the game token so launch() cannot start with it.
            _unlink_quietly(self._session())
            self._notify_mismatch()
            return 3
        # A handoff without a member number keeps the one already known.
        self._save_credential(handoff["refresh_token"], member or self._member_no)
        return code

    def restore(self) -> bool:
        """Restore a remembered session, at most once per process start.

        A keyring-backed credential is refreshed exactly once and checked for
        identity continuity. A network error leaves the user logged out with
        no retry loop; a member-number mismatch wipes the credential and asks
        for a new QR login.
        """
        if self._restore_attempted:
            return self._restored
        self._restore_attempted = True
        if not self._refresh_token and self._remember_enabled():
            cred = self._load_credential()
            if cred:
                self._refresh_token = cred["refresh_token"]
                self._member_no = cred["member_no"] or None
                _live_set(self.ctx.name, cred["refresh_token"], cred["member_no"])
        if not self._refresh_token:
            self._restore_code = 0
            return False
        self._restore_code = self._mint_session()
        if self._restore_code != 0:
            # Network failure or an invalid chain: stay logged out, no retry.
            # Keep the keyring entry (and the worker's exit code) so the next
            # Launch can explain what happened instead of blaming the login.
            self._refresh_token = None
            return False
        self._restored = True
        return True

    def _raise_login_required(self) -> None:
        """Explain why a launch has no usable login, without a dead-end message."""
        if self._restore_code == 4:
            raise BackendError(
                "Could not refresh the saved STOVE login because there is no "
                "internet connection. Reconnect and press Launch again.",
                4,
            )
        if self._restore_code == 3:
            raise BackendError(
                "The saved STOVE login expired or was used elsewhere. Log in again.",
                3,
            )
        if self._restore_code == 5:
            raise BackendError(self.TERMS_MESSAGE, 5)
        raise BackendError("log in first, or turn on 'Remember me on this device'", 3)
