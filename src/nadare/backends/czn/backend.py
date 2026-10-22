# SPDX-License-Identifier: GPL-3.0-or-later
# Copyright (C) 2026 Nadare contributors
"""Chaos Zero Nightmare backend (``stove-czn``), ported from czn-linux.

Drives the ported ``auth``/``update``/``runners`` modules (each keeps its own
``--json`` CLI) through the current interpreter, re-emitting their machine
events as :class:`nadare.core.events.Event` and mapping czn-linux exit codes to
:class:`BackendError`. Nothing here talks to the UI directly.

Sequence (mirrors ``czn_direct_launch.sh`` / ``czn_gui.py``):

* ``install``  — create the Wine prefix, install the WebView2 Runtime, download
  the game (dpms updater) and build the PCSDK stub.
* ``update``   — ``czn_update.py update --apply``.
* ``login``    — ``czn_auth.py login --json`` (QR shown by the UI; no tokens are
  ever emitted).
* ``launch``   — fresh game token, client update, then the stub runs the game in
  the prefix and reports ``RESULT ok|no-connect|handshake-rejected``.
* ``stop``     — terminate the game processes that live in *this* prefix.
"""

from __future__ import annotations

import atexit
import contextlib
import hashlib
import json
import logging
import os
import re
import select
import shutil
import signal
import subprocess
import sys
import threading
import time
from collections.abc import Callable
from pathlib import Path
from typing import Any

from ...core import download as net
from ...core import graphics, hosting, hostlibs, paths, prefixmove, process, sandbox, tls, vcredist
from ...core import runners as core_runners
from ...core import umu as umu_mod
from ...core.backends import (
    BackendError,
    GameBackend,
    Option,
    Status,
)
from ...core.events import Cancelled, Event, mask
from ...core.removal import tree_safety
from ...core.secrets import SecretStore, SecretStoreError, default_collection_exists
from .news import IMAGE_HOSTS, fetch_czn_news

KIND = "stove-czn"

log = logging.getLogger(__name__)

AUTH_MODULE = "nadare.backends.czn.auth"
UPDATE_MODULE = "nadare.backends.czn.update"
RUNNERS_MODULE = "nadare.backends.czn.runners"

GAME_SUBDIR = Path("drive_c/ProgramData/Smilegate/Games/ChaosZeroNightmare")
#: Written into a prefix Nadare created, so uninstall knows it may delete it.
PREFIX_MARKER = ".nadare-prefix"
SERVICE = "STOVE_CHAOSZERO"
SHIELD_NAME = "ssr-stove-shield.exe"
STUB_NAME = "czn_pcsdk_stub.exe"

#: Seconds of silence from a long-running child before a "still working" line.
HEARTBEAT_SECONDS = 15.0

#: Environment for silent installers run through Proton. The native Wayland
#: driver makes the WebView2 installer exit silently (exit 8, nothing installed)
#: on some setups; an installer needs no window, so it always runs via XWayland.
_INSTALLER_ENV = {"PROTON_ENABLE_WAYLAND": "0"}
#: The stub ships prebuilt (reproducible ``build_stub.sh --container`` output)
#: so users need no mingw-w64; it is used only when it matches this digest.
STUB_PREBUILT = Path(__file__).resolve().parents[2] / "assets" / STUB_NAME
STUB_PREBUILT_SHA256 = "fe4a5bad970750e974cf999e31e3b87132fbb25f9c04c9eea69e3acc2e243185"
DEFAULT_RUNNER = "dwproton"  # the tested DW-Proton release
WEBVIEW2_URL = "https://go.microsoft.com/fwlink/?linkid=2124701"
#: The bootstrapper is fetched through a mutable Microsoft redirector, so the
#: download is accepted only when it lands on a Microsoft host and matches a
#: pinned SHA-256. AUDIT H1: the hash is never guessed; it is resolved at run
#: time from the ``NADARE_WEBVIEW2_SHA256`` environment variable or the
#: profile's ``webview2_sha256`` setting, with this constant only as the final
#: fallback. When no pin is configured the installer is refused *before* it is
#: downloaded rather than run unverified (never silently bypassed).
#: Pinned 2026-10-13 from the Evergreen standalone x64 installer (Microsoft
#: Last-Modified 2026-10-08, 214090448 B). Microsoft re-issues this file with each
#: Edge release, so the pin goes stale; override with NADARE_WEBVIEW2_SHA256 or
#: the profile's ``webview2_sha256`` until it is bumped.
WEBVIEW2_SHA256: str | None = "2a6add76c37bfa872eb8c2b22d45b3216b8e2a1f193e0774006340ff62ac5fa0"
#: Microsoft re-issues the installer with every Edge release, so a built-in
#: pin alone would break installs on older Nadare builds. Hashes published in
#: the project's index (refreshed by ``scripts/publish_index.py``) are accepted
#: as well, unless the user set an explicit override.
WEBVIEW2_INDEX_MAX_HASHES = 8
#: Redirect targets of the fwlink that are allowed to serve the installer.
WEBVIEW2_HOST_SUFFIXES: tuple[str, ...] = (".microsoft.com",)
#: Backend-side exit codes for WebView2 failures (not czn-linux exit codes).
EXIT_WEBVIEW2_UNVERIFIED = 10
EXIT_WEBVIEW2_FAILED = 11

#: Visual C++ runtime constants live in :mod:`nadare.core.vcredist` so every
#: Wine-family backend shares one URL/pin. Re-exported here because existing CZN
#: tests (and callers) reference these names.
VCREDIST_URL = vcredist.VCREDIST_URL
VCREDIST_SHA256: str | None = vcredist.VCREDIST_SHA256
VCREDIST_INDEX_MAX_HASHES = vcredist.VCREDIST_INDEX_MAX_HASHES
VCREDIST_HOST_SUFFIXES: tuple[str, ...] = vcredist.VCREDIST_HOST_SUFFIXES

#: czn-linux subprocess exit codes -> a user-safe message. The backend-side
#: WebView2 codes live in their own map (below) so a helper that later returns
#: 10/11 cannot surface a WebView2-specific message for an unrelated failure.
EXIT_MESSAGES: dict[int, str] = {
    1: "setup or usage error",
    2: "cannot reach the update server",
    3: "the STOVE login expired — log in again",
    4: "cannot reach STOVE (network)",
    5: "the game update failed",
    6: "the game exited without contacting the launcher stub (the PCSDK "
    "handshake may have changed, or this runner cannot run the game)",
    7: "the game rejected the login session",
    8: "the game is already running",
    9: "a runner download failed its checksum",
}

#: ``czn_auth.py`` exit codes -> a user-safe message. Its own table: the auth
#: worker's 2 does not mean the updater's 2, so reusing ``EXIT_MESSAGES`` told
#: the user "cannot reach the update server" for an expired QR / rejected signin.
AUTH_EXIT_MESSAGES: dict[int, str] = {
    2: "STOVE login returned an unexpected error; try again in a minute "
    "(if it keeps happening, open the logs)",
    3: "STOVE rejected or expired the sign-in. Scan a fresh QR code and try again.",
    4: "Could not reach STOVE. Check your internet connection and try again.",
}

#: ``czn_update.py`` exit codes -> a user-safe message. Its own table: 1 is a
#: download failure and 2 is setup/network, matching neither ``EXIT_MESSAGES``
#: nor the auth table.
UPDATE_EXIT_MESSAGES: dict[int, str] = {
    1: "the game update failed (a download failed)",
    2: "cannot reach the update server",
    5: "the game files are missing or incomplete — press Install to repair them",
}

#: ``czn_runners.py`` exit codes -> a user-safe message. Its 4 means the GitHub
#: runner download failed, not that the game's STOVE server is unreachable.
RUNNER_EXIT_MESSAGES: dict[int, str] = {
    1: "Nadare could not install the required Proton runner. Try Install again, or "
    "choose a runner in Settings → Manage runners.",
    4: "Could not download the required Proton runner from GitHub. Check your internet "
    "connection and try again, or choose a runner in Settings → Manage runners.",
    9: "The downloaded Proton runner was damaged or changed. Try Install again.",
}

#: Backend-side (not czn-linux) exit codes for WebView2 failures.
WEBVIEW2_MESSAGES: dict[int, str] = {
    EXIT_WEBVIEW2_UNVERIFIED: "Nadare could not verify Microsoft's WebView2 installer. "
    "Try Install again; if it keeps failing, update Nadare and try again.",
    EXIT_WEBVIEW2_FAILED: "Microsoft's WebView2 installer did not pass a safety check. "
    "Try Install again; if it keeps failing, update Nadare and try again.",
}

#: auth.py ``qr_status`` states grouped into the coarse state the GUI renders.
_QR_STATE = {
    "init": "waiting",
    "ongoing": "waiting",
    "success": "approved",
    "expired": "expired",
    "close": "expired",
    "closed": "expired",
    "cancel": "expired",
    "cancelled": "expired",
}
_QR_MESSAGE = {
    "waiting": "Waiting for you to approve the scan in the STOVE app…",
    "approved": "Approved — finishing login…",
    "expired": "QR code expired — log in again for a fresh code.",
}

#: Exit code the runner downloader uses for a checksum mismatch.
_RUNNER_INTEGRITY_EXIT = 9
_RUNNER_MATCH = {"cachyos": "cachyos", "ge": "GE-Proton", "dwproton": "dwproton"}
_FILE_PROGRESS = re.compile(r"^\s*\[(\d+)/(\d+)\]\s*(.*)$")
_RESULT = re.compile(r"RESULT\s+([a-z-]+)")
#: A pinned WebView2 installer hash must be exactly 64 lowercase hex digits.
_SHA256_RE = re.compile(r"^[0-9a-f]{64}$")

#: Emitted once when "Remember me" is on but no OS keyring is available.
#: Exit codes a game child reports when it was terminated on purpose
#: (-SIGTERM, 128+SIGTERM, and the 256-15 wrap umu/proton produce; likewise SIGINT).
_STOPPED_EXIT_CODES = frozenset({-15, 143, 241, -2, 130, 254})

REMEMBER_NOTICE = "Remember me unavailable: system keyring not usable (run: nadare doctor)"
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

#: Process-wide live sessions, keyed by profile name. The GUI builds a fresh
#: backend object for every operation (``gui/workers.py``), so a QR login must
#: outlive the object that performed it; a separate process cannot share this
#: and must rely on ``remember_me`` + the keyring.
_LIVE_LOCK = threading.Lock()
_LIVE_SESSIONS: dict[str, dict[str, str]] = {}
#: Keyring reads are cached once per process so ``status()`` does not unlock the
#: user's keyring on every poll.
_KEYRING_CACHE: dict[str, dict[str, str] | None] = {}


def _reset_process_state() -> None:
    """Forget live sessions and cached keyring reads (tests/restart)."""
    with _LIVE_LOCK:
        _LIVE_SESSIONS.clear()
        _KEYRING_CACHE.clear()


def _live_get(profile: str) -> dict[str, str] | None:
    with _LIVE_LOCK:
        cred = _LIVE_SESSIONS.get(profile)
        return dict(cred) if cred else None


def _live_set(profile: str, refresh_token: str, member_no: str) -> None:
    with _LIVE_LOCK:
        _LIVE_SESSIONS[profile] = {"refresh_token": refresh_token, "member_no": member_no}


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


def _unlink_quietly(path: Path) -> None:
    try:
        Path(path).unlink()
    except OSError:
        pass


def _sha256_file(path: Path) -> str:
    """Streamed SHA-256 of a file, so large installers are not read at once."""
    h = hashlib.sha256()
    with open(path, "rb") as f:
        for chunk in iter(lambda: f.read(1 << 20), b""):
            h.update(chunk)
    return h.hexdigest()


def _profile_setting(profile: dict[str, Any], key: str) -> Any:
    """Read a profile key from the top level or its ``extra`` mapping."""
    value = profile.get(key)
    if value not in (None, ""):
        return value
    extra = profile.get("extra")
    if isinstance(extra, dict):
        return extra.get(key)
    return None


#: Helper processes the game leaves around; they are not "the game running"
#: (a crash handler can linger after the game has exited) but Stop kills them.
GAME_HELPER_EXES = frozenset({"crashpad_handler.exe"})


def _cmdline_has_exe(raw: bytes, prefix: str, *, exclude: frozenset[str] = frozenset()) -> bool:
    """Whether any NUL-separated argv token names an exe under ``prefix``.

    Matching whole argv tokens instead of the raw cmdline stops an unrelated
    process that merely mentions the path inside one argument (an editor, a
    log, ``ps``) from being mistaken for the game. Tokens whose file name is in
    ``exclude`` are ignored."""
    for token in raw.split(b"\0"):
        if not token:
            continue
        text = token.decode("utf-8", "replace").lower()
        if not text.startswith(prefix):
            continue
        if exclude and text.replace("/", "\\").rsplit("\\", 1)[-1] in exclude:
            continue
        return True
    return False


#: Session paths already handed to ``atexit`` this process (one hook each).
_registered_ephemeral: set[Path] = set()


def _register_ephemeral(path: Path) -> None:
    """Ensure ``session.json`` is removed on process exit as a backstop.

    The launch path deletes it in a ``finally``; this covers the case where the
    whole launcher is closed while that thread is still unwinding (or blocked on
    a detached child's inherited stdio). Each path is registered once per
    process, so repeated launches do not pile up exit hooks.
    """
    target = Path(path)
    if target in _registered_ephemeral:
        return
    _registered_ephemeral.add(target)
    atexit.register(_unlink_quietly, target)


def _write_private(path: Path, text: str) -> None:
    """Atomically write a file that is 0600 from creation."""
    path.parent.mkdir(parents=True, exist_ok=True)
    tmp = path.with_name(path.name + ".tmp")
    fd = os.open(tmp, os.O_WRONLY | os.O_CREAT | os.O_TRUNC, 0o600)
    with os.fdopen(fd, "w") as fh:
        fh.write(text)
    os.chmod(tmp, 0o600)
    os.replace(tmp, path)


class CznBackend(GameBackend):
    unofficial_notice = True

    kind = KIND
    supports_remember_me = True
    supports_login = True

    @staticmethod
    def forget_saved_login(profile: str) -> None:
        forget_saved_credential(profile)

    options = (
        Option("prefix", "Wine prefix", "path_dir", "WINEPREFIX used for this game."),
        Option(
            "runner",
            "Runner",
            "runner",
            "Proton runner id or path (cachyos, ge, dwproton).",
        ),
        Option(
            "renderer",
            "Renderer",
            "choice",
            graphics.RENDERER_HELP,
            default="auto",
            choices=graphics.RENDERER_CHOICES,
        ),
        Option(
            "display",
            "Display backend",
            "choice",
            "Wayland is the tested mode; X11 (XWayland) is experimental. "
            "X11 (XWayland) shows a blank window with NVIDIA on some systems; "
            "leave on Auto unless Wayland misbehaves.",
            default="auto",
            choices=(
                ("Auto (Wayland)", "auto"),
                ("Wayland", "wayland"),
                ("X11 (XWayland)", "x11"),
            ),
        ),
    )

    #: Official STOVE/CZN news (publisher's own board); image host is the CZN CDN.
    news_source = staticmethod(fetch_czn_news)
    official_image_hosts = IMAGE_HOSTS
    #: Promote real updates/announcements to the hero instead of coupon posts.
    news_hero_categories = ("Updates", "Announcements")

    def __init__(self, ctx) -> None:  # noqa: ANN001 - matches GameBackend
        super().__init__(ctx)
        # Credentials live in memory for the process; the keyring is opt-in via
        # the per-profile ``remember_me`` flag (default off).
        self._refresh_token: str | None = None
        self._member_no: str | None = None
        self._restore_attempted = False
        self._restored = False
        #: Exit code from the last restore attempt, so launch can explain *why*
        #: a saved login could not be refreshed (offline, expired, ...).
        self._restore_code = 0
        self._remember_notice_shown = False
        self._renderer: str | None = None
        # Cached host 32-bit library preflight (see _missing_hostlibs). ``None``
        # means "unknown/not checked yet"; a list means a checked result.
        self._hostlibs_checked = False
        #: Extra env for prefix commands while inside _installer_environment().
        self._run_env: dict[str, str] | None = None
        #: What a silent child is doing, for the progress heartbeat (None = no beat).
        self._activity: str | None = None
        self._hostlibs_result: list[str] | None = None
        #: Message from the auth worker's last ``error`` event, if any.
        self._last_auth_error: str | None = None
        # Adopt a login performed by an earlier backend object in this process
        # (the GUI builds a fresh backend per operation).
        live = _live_get(self.ctx.name)
        if live:
            self._refresh_token = live["refresh_token"]
            self._member_no = live["member_no"] or None
        # One-time: move a legacy plaintext launcher_refresh into the keyring
        # (or drop it) and rewrite czn_auth.json without the secret.
        self._migrate_legacy_state()

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
            base = Path(runtime) / "nadare" / _profile_slug(self.ctx.name)
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
        try:
            store.set(self.ctx.name, json.dumps(cred))
        except SecretStoreError:
            # STOVE has already rotated the token, so any entry still in the
            # keyring is superseded: restoring it next start would only hit
            # 44010. Drop it rather than leave a dead credential behind.
            self._delete_credential()
            self._notify_remember_unavailable()
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
        """Forget the account everywhere: keyring, cache and process memory."""
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
        self.ctx.emit(
            Event("status", REMEMBER_NOTICE, data={"remember_unavailable": True})
        )

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
        if self._game_running():
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

    def _qr_png(self) -> Path:
        return self._data_dir() / "czn_qr.png"

    def _stub(self) -> Path:
        return self.ctx.profile_dir("stub") / STUB_NAME

    def _data_dir(self) -> Path:
        return self.ctx.data_dir()

    def _runners_dir(self) -> Path:
        return paths.runners_dir()

    def _cache_dir(self) -> Path:
        return paths.cache_dir() / "czn"

    def _stub_log(self) -> Path:
        d = paths.logs_dir()
        d.mkdir(parents=True, exist_ok=True)
        return d / f"{self.ctx.name}-czn-stub.log"

    def prefix(self) -> Path:
        raw = str(self.ctx.profile.get("prefix") or "").strip()
        return Path(os.path.expanduser(raw)) if raw else self._data_dir() / "prefix"

    def game_dir(self) -> Path:
        return self.prefix() / GAME_SUBDIR

    def shield(self) -> Path:
        return self.game_dir() / "bin" / SHIELD_NAME

    def upf(self) -> Path:
        return self.game_dir() / "combinedata_manifest" / f"GameManifest_{SERVICE}.upf"

    def installed(self) -> bool:
        return self.shield().is_file()

    # ---- env / process helpers --------------------------------------------
    def _env(self) -> dict[str, str]:
        env = dict(os.environ)
        env["CZN_STATE_DIR"] = str(self._auth_dir())
        env["CZN_PREFIX"] = str(self.prefix())
        env["CZN_RUNNERS_DIR"] = str(self._runners_dir())
        env["CZN_CACHE_DIR"] = str(self._cache_dir())
        env["PYTHONUNBUFFERED"] = "1"
        return env

    def _auth_env(self) -> dict[str, str]:
        """Env for the auth worker: paths only, never the token itself."""
        env = {"CZN_CREDENTIAL_OUT": str(self._handoff_path())}
        if self._refresh_token:
            env["CZN_CREDENTIAL_IN"] = str(self._input_path())
        return env

    def _stage_input_credential(self) -> None:
        """Write the live token to a 0600 file the worker reads and deletes."""
        if not self._refresh_token:
            return
        path = self._input_path()
        path.parent.mkdir(parents=True, exist_ok=True)
        fd = os.open(path, os.O_WRONLY | os.O_CREAT | os.O_TRUNC, 0o600)
        with os.fdopen(fd, "w", encoding="utf-8") as f:
            json.dump({"refresh_token": self._refresh_token}, f)

    @staticmethod
    def _win(path: Path) -> str:
        return "Z:" + str(path).replace("/", os.sep).replace("/", "\\")

    def _win_cwd(self, path: Path) -> str:
        """The game's working directory as Wine sees it, on ``C:`` when it lives in the prefix.

        The STOVE SDK sometimes (about 1 start in 4 on a slow machine) comes up with an
        empty AppData base and falls back to ``\\STOVEPCSDK3\\logs`` at the root of the
        *current drive*. On ``Z:`` that is ``/`` and cannot be created, so the game stops
        with "Please run the game through the launcher" (#173). On ``C:`` the same
        fallback just creates a folder inside the prefix and the game carries on.
        """
        try:
            rel = Path(path).relative_to(self.prefix() / "drive_c")
        except ValueError:
            return self._win(path)
        return "C:\\" + "\\".join(rel.parts)

    def _fail(self, what: str, code: int, messages: dict[int, str] | None = None) -> None:
        table = EXIT_MESSAGES if messages is None else messages
        raise BackendError(table.get(code, f"{what} failed (exit {code})"), code)

    def _fail_auth(self, what: str, code: int) -> None:
        """Report an auth-worker failure, keeping its real message when useful."""
        if code == 2 and self._last_auth_error:
            raise BackendError(
                f"STOVE returned an unexpected error: {self._last_auth_error}. "
                "Try again in a minute; if it keeps happening, open the logs.",
                code,
            )
        self._fail(what, code, AUTH_EXIT_MESSAGES)

    # ---- event plumbing ----------------------------------------------------
    def _emit_json_event(self, obj: dict[str, Any]) -> None:
        name = obj.get("event")
        if name == "qr":
            self.ctx.emit(
                Event(
                    "step",
                    "Scan the QR code with the STOVE app",
                    data={
                        "png": obj.get("png"),
                        "url": obj.get("url"),
                        "timeout": obj.get("timeout"),
                    },
                )
            )
        elif name == "qr_status":
            state = str(obj.get("state") or "")
            qr_state = _QR_STATE.get(state, "waiting")
            self.ctx.emit(
                Event(
                    "step",
                    _QR_MESSAGE[qr_state],
                    data={"qr_state": qr_state, "state": state},
                )
            )
        elif name == "logged_in":
            self.ctx.emit(Event("step", "Logged in to STOVE"))
        elif name == "credential":
            # Internal handoff only: the launcher refresh token must never
            # reach the UI. The backend reads it from the 0600 handoff file.
            return
        elif name == "session":
            self.ctx.emit(
                Event(
                    "step",
                    "Game token ready",
                    data={
                        "path": obj.get("path"),
                        "expires_at": obj.get("expires_at"),
                        "expires_in": obj.get("expires_in"),
                    },
                )
            )
        elif name == "status":
            self.ctx.emit(Event("status", "STOVE status", data=obj))
        elif name == "progress":
            done, total = obj.get("done"), obj.get("total")
            frac = (
                (done / total)
                if isinstance(done, int) and isinstance(total, int) and total
                else None
            )
            self.ctx.emit(
                Event(
                    "progress",
                    str(obj.get("stage", "download")),
                    progress=frac,
                    data={"done": done, "total": total},
                )
            )
        elif name == "installed":
            self.ctx.emit(
                Event(
                    "step",
                    f"Runner installed: {obj.get('tag', '')}",
                    data={"path": obj.get("path"), "tag": obj.get("tag")},
                )
            )
        elif name == "error":
            message = mask(str(obj.get("message", "")))
            # ``_run_auth`` uses this to tell an expired QR / rejected sign-in
            # (exit 3) from a STOVE/API error (exit 2) in the final dialog.
            self._last_auth_error = message
            self.ctx.emit(Event("error", message, data={"kind": obj.get("kind")}))
        else:
            self.info(mask(str(obj)))

    def _emit_text(self, line: str) -> None:
        line = line.rstrip()
        if not line.strip():
            return
        m = _FILE_PROGRESS.match(line)
        if m:
            done, total = int(m.group(1)), int(m.group(2))
            self.ctx.emit(
                Event(
                    "progress",
                    line.strip(),
                    progress=(done / total if total else None),
                    data={"done": done, "total": total},
                )
            )
            return
        tag = line.lstrip()[:3]
        kind = "step" if tag in ("[*]", "[+]") else ("error" if tag == "[!]" else "log")
        self.ctx.emit(Event(kind, mask(line.strip())))

    def _stream(
        self,
        cmd: list[str],
        env: dict[str, str],
        *,
        parse_json: bool = False,
        collect: list[dict[str, Any]] | None = None,
        cwd: Path | None = None,
        kill_game: bool = False,
        beat_as_log: bool = False,
        beat_until: Callable[[], bool] | None = None,
    ) -> int:
        """Run ``cmd``, streaming its output as events.

        Under :meth:`_activity_label` a silent child gets a heartbeat line every
        ``HEARTBEAT_SECONDS``. ``beat_as_log`` emits plain log lines instead of
        busy-bar progress events (the game page must not show a busy bar while
        a game runs); ``beat_until`` stops the beats once it returns true.
        """
        self.ctx.emit(Event("log", "$ " + " ".join(cmd)))
        try:
            proc = subprocess.Popen(
                cmd,
                stdout=subprocess.PIPE,
                stderr=subprocess.STDOUT,
                stdin=subprocess.DEVNULL,
                text=True,
                bufsize=1,
                env=env,
                cwd=str(cwd) if cwd else None,
                start_new_session=True,
            )
        except OSError as e:
            raise BackendError(f"cannot start {cmd[0]!r}: {e}") from e
        if proc.stdout is None:
            raise BackendError("child process has no stdout pipe")
        fd = proc.stdout.fileno()
        pending = b""
        label = self._activity
        started = last_beat = time.monotonic()
        if label and not beat_as_log:
            # A bar that visibly moves, so a silent installer is not mistaken for a hang.
            self.ctx.emit(
                Event("progress", f"{label}…", data={"indeterminate": True})
            )
        try:
            # Poll with select so Cancel is honoured while the child is silent
            # (a running game prints nothing). Raw reads keep select reliable:
            # a buffered text wrapper can hold lines select cannot see.
            while True:
                if self.ctx.cancel.cancelled:
                    self._abort_child(proc, kill_game=kill_game)
                    raise Cancelled()
                try:
                    ready, _, _ = select.select([fd], [], [], 0.25)
                except (OSError, ValueError):
                    break
                if not ready:
                    # Plain Wine leaves wineserver/service processes running
                    # that inherit our pipe, so EOF may never come. Once the
                    # process we started has exited we are done.
                    if proc.poll() is not None:
                        break
                    now = time.monotonic()
                    if label and now - last_beat >= HEARTBEAT_SECONDS:
                        last_beat = now
                        if beat_until is not None and beat_until():
                            label = None  # up and running: no more "still starting" lines
                        elif beat_as_log:
                            self.ctx.emit(
                                Event(
                                    "log",
                                    f"{label}… {int(now - started)} s "
                                    "(the first launch through umu downloads the Steam "
                                    "Linux Runtime and can take a few minutes)",
                                )
                            )
                        else:
                            self.ctx.emit(
                                Event(
                                    "progress",
                                    f"{label}… {int(now - started)} s "
                                    "(the installer prints nothing while it works)",
                                    data={"indeterminate": True},
                                )
                            )
                    continue
                try:
                    chunk = os.read(fd, 65536)
                except OSError:
                    break
                if not chunk:
                    break  # EOF: every writer closed the pipe
                last_beat = time.monotonic()
                pending += chunk
                *lines, pending = pending.split(b"\n")
                for raw in lines:
                    self._handle_line(
                        raw.decode("utf-8", "replace").rstrip("\r"), parse_json, collect
                    )
            if pending:
                tail = pending.decode("utf-8", "replace").rstrip("\r")
                self._handle_line(tail, parse_json, collect)
        finally:
            if proc.stdout is not None:
                proc.stdout.close()
        return proc.wait()

    def _handle_line(
        self, line: str, parse_json: bool, collect: list[dict[str, Any]] | None
    ) -> None:
        if parse_json and line.lstrip().startswith("{"):
            try:
                obj = json.loads(line)
            except ValueError:
                obj = None
            if isinstance(obj, dict) and "event" in obj:
                if collect is not None:
                    collect.append(obj)
                self._emit_json_event(obj)
                return
        self._emit_text(line)

    def _abort_child(self, proc: subprocess.Popen, *, kill_game: bool = False) -> None:
        """Stop a cancelled child *and* the game it started.

        ``proton run`` hands the real game to a wineserver that lives in its own
        session, so killing the wrapper's process group alone leaves it running.
        """
        self._kill(proc)
        if not kill_game:
            return
        try:
            self.stop()
        except Exception:  # noqa: BLE001 - cancelling must always finish
            pass

    def _run_module(
        self,
        module: str,
        args: list[str],
        *,
        parse_json: bool = False,
        collect: list[dict[str, Any]] | None = None,
        extra_env: dict[str, str] | None = None,
    ) -> int:
        env = self._env()
        if extra_env:
            env.update(extra_env)
        return self._stream(
            [sys.executable, "-m", module, *args],
            env,
            parse_json=parse_json,
            collect=collect,
        )

    @staticmethod
    def _kill(proc: subprocess.Popen) -> None:
        try:
            os.killpg(os.getpgid(proc.pid), signal.SIGTERM)
        except (ProcessLookupError, PermissionError):
            proc.terminate()
        try:
            proc.wait(timeout=5)
        except subprocess.TimeoutExpired:
            try:
                os.killpg(os.getpgid(proc.pid), signal.SIGKILL)
            except (ProcessLookupError, PermissionError):
                proc.kill()

    # ---- runner ------------------------------------------------------------
    def _installed_runner(self, rid: str) -> Path | None:
        root = self._runners_dir()
        if not root.is_dir():
            return None
        exact = root / rid
        if rid and not rid.startswith(".") and (exact / "proton").is_file():
            return exact  # "GE-Proton9-1" must not resolve to GE-Proton9-10
        want = _RUNNER_MATCH.get(rid, rid).lower()
        cands = [
            d
            for d in root.iterdir()
            if d.is_dir()
            and not d.name.startswith(".")
            and (d / "proton").is_file()
            and want in d.name.lower()
        ]
        def _key(d: Path) -> list[int]:
            return [int(n) for n in re.findall(r"\d+", d.name)]

        return max(cands, key=lambda d: (_key(d), d.name)) if cands else None

    def _installed_pinned(self, rid: str) -> Path | None:
        """The already-installed *tested* release of ``rid``, if there is one."""
        tag = core_runners.pinned_tag(rid)
        root = self._runners_dir()
        if not tag or not root.is_dir():
            return None
        for d in sorted(root.iterdir()):
            if (
                d.is_dir()
                and not d.name.startswith(".")
                and (d / "proton").is_file()
                and core_runners.matches_tag(d.name, tag)
            ):
                return d
        return core_runners.installed_pinned(rid)

    def _pinned_runner(self, rid: str) -> Path:
        """The tested release of ``rid``: reuse it when installed, else download it once.

        A newer or older untested build that happens to be installed is not used
        in its place. Only when the pinned release cannot be downloaded (offline)
        does an installed build of the same family stand in, with a notice.
        """
        pinned = self._installed_pinned(rid)
        if pinned is not None:
            return pinned
        try:
            return self._download_runner(rid, latest=False)
        except BackendError as exc:
            if exc.code == _RUNNER_INTEGRITY_EXIT:
                raise  # a tampered or corrupt download must not fall back quietly
            fallback = self._installed_runner(rid)
            if fallback is None:
                raise
            self.ctx.emit(
                Event(
                    "status",
                    f"could not download the tested {rid} runner; using the installed "
                    f"{fallback.name} (untested)",
                )
            )
            return fallback

    def _download_runner(self, rid: str, latest: bool) -> Path:
        self.ctx.emit(Event("step", f"Downloading runner {rid}…"))
        got: list[dict[str, Any]] = []
        args = ["install", rid, "--json"] + (["--latest"] if latest else [])
        code = self._run_module(RUNNERS_MODULE, args, parse_json=True, collect=got)
        dest = next((e.get("path") for e in got if e.get("event") == "installed"), None)
        if code != 0 or not dest:
            self._fail(f"runner {rid}", code or 1, RUNNER_EXIT_MESSAGES)
        return Path(dest)

    def resolve_runner(self) -> tuple[str, str]:
        """Return ``(protonpath, kind)`` where kind is ``proton`` or ``wine``.

        ``profile['runner']`` may be a Proton directory, a runner id
        (``dwproton``/``cachyos``/``ge``) or ``GE-Proton``; empty falls back to
        the pinned tested release (DW-Proton).
        """
        rid = str(self.ctx.profile.get("runner") or "").strip() or DEFAULT_RUNNER
        if rid in ("wine", "system", "none"):
            return "", "wine"
        if rid in ("dwproton", "cachyos", "ge"):
            return str(self._pinned_runner(rid)), "proton"
        if rid.upper().startswith("GE-PROTON"):
            found = self._installed_runner(rid)
            return (str(found) if found is not None else rid), "proton"
        p = Path(os.path.expanduser(rid))
        if p.is_dir() and (p / "proton").is_file():
            return str(p), "proton"
        raise BackendError(f"unknown or missing runner: {rid!r}")

    def runner_name(self) -> str:
        try:
            proton, kind = self.resolve_runner()
        except BackendError:
            return ""
        if kind == "wine":
            return "wine"
        try:
            return (Path(proton) / "version").read_text().split()[1]
        except (OSError, IndexError):
            return Path(proton).name

    # ---- running-state -----------------------------------------------------
    def _game_pids(self, *, helpers: bool = True) -> list[int]:
        prefix = ("Z:" + str(self.game_dir() / "bin").replace("/", "\\") + "\\").lower()
        uid = os.getuid()
        pids: list[int] = []
        for d in Path("/proc").iterdir():
            if not d.name.isdigit():
                continue
            try:
                # Only processes owned by this user can be the game; this stops
                # another user from planting a matching argv and being counted.
                if d.stat().st_uid != uid:
                    continue
                raw = (d / "cmdline").read_bytes()
            except OSError:
                continue
            exclude = frozenset() if helpers else GAME_HELPER_EXES
            if _cmdline_has_exe(raw, prefix, exclude=exclude):
                pids.append(int(d.name))
        return pids

    def _game_running(self) -> bool:
        """The game itself is running (a lingering crash handler is not)."""
        return bool(self._game_pids(helpers=False))

    # ---- status ------------------------------------------------------------
    def status(self) -> Status:
        installed = self.installed()
        running = self._game_running()
        version = None
        try:
            version = "v" + str(json.loads(self.upf().read_text())["local_version"])
        except Exception:
            pass
        logged_in = bool(self._refresh_token)
        if not logged_in and self._remember_enabled():
            logged_in = self._load_credential() is not None
        session = self._session_info()
        if running:
            state, message = "running", "Chaos Zero Nightmare is running"
        elif installed:
            state, message = "installed", f"Installed{(' ' + version) if version else ''}"
        else:
            state, message = "not_installed", "Not installed"
        note = graphics.vulkan_note()
        if note and state in ("installed", "running"):
            message = f"{message} — {note}" if message else note
        return Status(
            state=state,
            version=version,
            running=running,
            message=message,
            data={
                "logged_in": logged_in,
                "login_required": installed and not logged_in,
                "session": session,
                "prefix": str(self.prefix()),
                "installed": installed,
                "can_repair": installed,
                "renderer": self._renderer or "",
            },
        )

    def _session_info(self) -> dict[str, Any] | None:
        try:
            d = json.loads(self._session().read_text())
            exp = int(d.get("expire_time", "0")) // 1000
            return {
                "path": str(self._session()),
                "expires_at": exp,
                "valid": bool(d.get("access_token")) and exp > time.time(),
            }
        except Exception:
            return None

    # ---- install / update --------------------------------------------------
    def _ensure_prefix(self) -> None:
        prefix = self.prefix()
        if (prefix / "drive_c").is_dir():
            return
        prefix.mkdir(parents=True, exist_ok=True)
        self.ctx.emit(Event("step", f"Creating the Wine prefix at {prefix}…"))
        with self._activity_label("Creating the Wine prefix"):
            code = self._run_in_prefix(["wineboot", "-u"])
        if code != 0 or not (prefix / "drive_c").is_dir():
            raise BackendError(f"prefix creation failed (exit {code})", 1)
        try:
            (prefix / PREFIX_MARKER).write_text("created by Nadare\n")
            (prefix / "czn-runner").write_text(self.runner_name() + "\n")
        except OSError:
            pass

    @staticmethod
    def _stub_connected(log: Path) -> bool:
        """True once the stub reports the game opened the launcher pipe."""
        try:
            text = log.read_text(errors="replace")
        except OSError:
            return False
        return "game connected" in text or "heartbeats started" in text

    def _webview2_installed(self) -> bool:
        root = self.prefix() / "drive_c/Program Files (x86)/Microsoft/EdgeWebView/Application"
        return any(root.glob("*/msedgewebview2.exe"))

    def _webview2_pin(self) -> str:
        """Resolve the expected installer SHA-256 from a trusted source.

        Precedence: the ``NADARE_WEBVIEW2_SHA256`` environment variable, then
        the profile's ``webview2_sha256`` setting (top level or ``extra``), then
        the module constant. The hash is never hardcoded, so a shipped build
        cannot silently skip verification; an empty result means "not pinned".
        A whitespace-only value is treated as unset and falls through rather
        than masking a lower-precedence pin.
        """
        for value in (
            os.environ.get("NADARE_WEBVIEW2_SHA256"),
            _profile_setting(self.ctx.profile, "webview2_sha256"),
            WEBVIEW2_SHA256,
        ):
            if value:
                text = str(value).strip().lower()
                if text:
                    return text
        return ""

    def _webview2_pin_error(self, pin: str) -> BackendError:
        """A fail-fast :class:`BackendError` for an absent/malformed pin."""
        if pin and not _SHA256_RE.fullmatch(pin):
            return BackendError(
                "the configured WebView2 SHA-256 is malformed (expected 64 hex "
                "digits); refusing before the download",
                EXIT_WEBVIEW2_UNVERIFIED,
            )
        return BackendError(
            WEBVIEW2_MESSAGES[EXIT_WEBVIEW2_UNVERIFIED], EXIT_WEBVIEW2_UNVERIFIED
        )

    def _webview2_overridden(self) -> bool:
        """True when the user pinned a hash explicitly (env or profile)."""
        for value in (
            os.environ.get("NADARE_WEBVIEW2_SHA256"),
            _profile_setting(self.ctx.profile, "webview2_sha256"),
        ):
            if value and str(value).strip():
                return True
        return False

    @staticmethod
    def _webview2_index_pins() -> set[str]:
        """Installer hashes published in the Nadare index (empty on any error).

        Best effort and offline-safe: a missing, slow or malformed index just
        means the built-in pin is the only accepted hash.
        """
        import urllib.request

        url = hosting.webview2_index_url()
        if not url.startswith("https://"):
            return set()
        try:
            req = urllib.request.Request(url, headers={"User-Agent": "nadare"})
            with tls.urlopen(
                req, timeout=10, context=tls.ssl_context()
            ) as r:  # noqa: S310
                payload = json.loads(r.read(65536).decode("utf-8"))
            hashes = payload["sha256"]
        except Exception:
            return set()
        if not isinstance(hashes, list):
            return set()
        return {
            h.strip().lower()
            for h in hashes[:WEBVIEW2_INDEX_MAX_HASHES]
            if isinstance(h, str) and _SHA256_RE.fullmatch(h.strip().lower())
        }

    @staticmethod
    def _webview2_index_source() -> str:
        """The immutable per-build installer URL published in the index.

        Microsoft's fwlink always serves the newest build, so its hash can
        change under us. The index records the exact build URL the published
        hash belongs to; downloading that keeps hash and bytes consistent even
        right after a Microsoft release. Empty on any problem (https and a
        Microsoft host are required).
        """
        import urllib.parse
        import urllib.request

        url = hosting.webview2_index_url()
        if not url.startswith("https://"):
            return ""
        try:
            req = urllib.request.Request(url, headers={"User-Agent": "nadare"})
            with tls.urlopen(
                req, timeout=10, context=tls.ssl_context()
            ) as r:  # noqa: S310
                payload = json.loads(r.read(65536).decode("utf-8"))
            source = payload["url"]
        except Exception:
            return ""
        if not isinstance(source, str):
            return ""
        parts = urllib.parse.urlparse(source)
        host = (parts.hostname or "").lower()
        if parts.scheme != "https" or not host.endswith(WEBVIEW2_HOST_SUFFIXES):
            return ""
        return source

    def _verify_webview2(self, path: Path) -> None:
        """Refuse a WebView2 installer that does not match the pinned SHA-256.

        Every failure is a :class:`BackendError`, so the cached-copy re-verify
        is mapped to an exit code exactly like the download path (no raw
        ``OSError`` escapes).
        """
        pin = self._webview2_pin()
        if not pin:
            log.debug("WebView2 installer has no pinned SHA-256")
            raise BackendError(
                WEBVIEW2_MESSAGES[EXIT_WEBVIEW2_UNVERIFIED], EXIT_WEBVIEW2_UNVERIFIED
            )
        if not _SHA256_RE.fullmatch(pin):
            raise self._webview2_pin_error(pin)
        try:
            digest = _sha256_file(path)
        except OSError as e:
            raise BackendError(
                f"cannot read the WebView2 installer: {e}", EXIT_WEBVIEW2_FAILED
            ) from e
        if digest != pin:
            log.debug(
                "WebView2 installer digest mismatch for %s: got %s, expected %s",
                path,
                digest,
                pin,
            )
            if self._webview2_overridden():
                path.unlink(missing_ok=True)
                raise BackendError(
                    WEBVIEW2_MESSAGES[EXIT_WEBVIEW2_FAILED],
                    EXIT_WEBVIEW2_FAILED,
                )
            if self.ctx.cancel.cancelled:
                raise Cancelled()
            pins = self._webview2_index_pins()
            if self.ctx.cancel.cancelled:
                raise Cancelled()
            if digest in pins:
                return
            path.unlink(missing_ok=True)
            raise BackendError(
                WEBVIEW2_MESSAGES[EXIT_WEBVIEW2_FAILED],
                EXIT_WEBVIEW2_FAILED,
            )

    def _download_webview2(self, url: str, tmp: Path, inst: Path) -> None:
        """Download ``url`` to ``tmp``, verify it, and move it to ``inst``."""
        import urllib.parse

        def _hop_ok(target: str) -> bool:
            hop = urllib.parse.urlparse(target)
            return hop.scheme == "https" and (hop.hostname or "").lower().endswith(
                WEBVIEW2_HOST_SUFFIXES
            )

        try:
            response = net.http_open(url, timeout=60, redirect_allowed=_hop_ok)
        except net.DownloadError as e:
            raise BackendError(
                f"WebView2 download failed: {e}", EXIT_WEBVIEW2_FAILED
            ) from e
        with response as r, open(tmp, "wb") as f:
            if not _hop_ok(r.geturl()):
                raise BackendError(
                    f"WebView2 download redirected off Microsoft: {r.geturl()}",
                    EXIT_WEBVIEW2_FAILED,
                )
            total = int(r.headers.get("Content-Length") or 0)
            done = 0
            while chunk := r.read(1 << 20):
                if self.ctx.cancel.cancelled:
                    raise Cancelled()
                f.write(chunk)
                done += len(chunk)
                if done > (total or net.MAX_DOWNLOAD_BYTES):
                    f.close()
                    tmp.unlink(missing_ok=True)
                    raise BackendError(
                        "WebView2 download exceeded its declared size",
                        EXIT_WEBVIEW2_FAILED,
                    )
                if total:
                    self.ctx.emit(Event("progress", "WebView2", progress=done / total))
        self._verify_webview2(tmp)
        tmp.replace(inst)

    def _ensure_webview2(self) -> None:
        if self._webview2_installed():
            return
        # Fail fast before spending the ~215 MB download: without a valid pin
        # there is nothing to verify against, so refuse up front.
        pin = self._webview2_pin()
        if not pin or not _SHA256_RE.fullmatch(pin):
            raise self._webview2_pin_error(pin)

        # FRESH-2: an unwritable/full cache dir must map to a BackendError like
        # every other WebView2 failure, not escape as a raw OSError.
        try:
            self._cache_dir().mkdir(parents=True, exist_ok=True)
            inst = self._cache_dir() / "MicrosoftEdgeWebView2RuntimeInstallerX64.exe"
            cached = inst.is_file() and bool(inst.stat().st_size)
        except OSError as e:
            raise BackendError(
                f"cannot access the WebView2 cache: {e}", EXIT_WEBVIEW2_FAILED
            ) from e
        if not cached:
            self.ctx.emit(Event("step", "Downloading the Microsoft WebView2 Runtime (~215 MB)…"))
            tmp = inst.with_suffix(".part")
            urls = [WEBVIEW2_URL]
            if not self._webview2_overridden():
                if self.ctx.cancel.cancelled:
                    raise Cancelled()
                indexed = self._webview2_index_source()
                if self.ctx.cancel.cancelled:
                    raise Cancelled()
                if indexed and indexed != WEBVIEW2_URL:
                    urls.insert(0, indexed)
            failure: BackendError | None = None
            for url in urls:
                try:
                    self._download_webview2(url, tmp, inst)
                    failure = None
                    break
                except Cancelled:
                    tmp.unlink(missing_ok=True)
                    raise
                except BackendError as e:
                    tmp.unlink(missing_ok=True)
                    failure = e
                except Exception as e:
                    tmp.unlink(missing_ok=True)
                    failure = BackendError(
                        f"WebView2 download failed: {e}", EXIT_WEBVIEW2_FAILED
                    )
            if failure is not None:
                raise failure
        else:
            # N3: a cached installer that no longer matches the pinned hash must
            # be evicted here, or it would be re-verified as a throwaway copy on
            # every attempt and wedge install() forever. Removing it makes the
            # next attempt re-download a fresh copy.
            try:
                self._verify_webview2(inst)
            except BackendError as exc:
                _unlink_quietly(inst)
                raise BackendError(
                    f"{exc.message} (the cached copy was removed; retry the install "
                    "to download a fresh one)",
                    EXIT_WEBVIEW2_FAILED,
                ) from None
        # FRESH-6: verify a private, read-only copy and run *that*, so the
        # verified bytes cannot be swapped between the hash check and the exec.
        run_path = self._cache_dir() / f".webview2-run-{os.getpid()}.exe"
        try:
            try:
                shutil.copyfile(inst, run_path)
                os.chmod(run_path, 0o400)
            except OSError as e:
                raise BackendError(
                    f"cannot stage the WebView2 installer: {e}", EXIT_WEBVIEW2_FAILED
                ) from e
            self._verify_webview2(run_path)
            self.ctx.emit(Event("step", "Installing the WebView2 Runtime into the prefix…"))
            with self._installer_environment(), self._activity_label(
                "Installing the WebView2 Runtime"
            ):
                installer_code = self._run_in_prefix([str(run_path), "/silent", "/install"])
        finally:
            _unlink_quietly(run_path)
        if not self._webview2_installed():
            raise BackendError(
                f"WebView2 Runtime install failed (installer exit code {installer_code}). "
                "Check the install log; a missing GPU/Vulkan driver (for example a VM), "
                "a full disk or a blocked download are the usual causes",
                EXIT_WEBVIEW2_FAILED,
            )

    # ---- Visual C++ runtime (shared in nadare.core.vcredist) ---------------
    def _vcredist_installed(self) -> bool:
        """Whether the real (not Wine-builtin) VC++ runtime is in the prefix."""
        return vcredist.installed(self.prefix())

    def _vcredist_pin(self) -> str:
        """Resolve the expected installer SHA-256 from a trusted source."""
        return vcredist.resolve_pin(self.ctx.profile, VCREDIST_SHA256)

    def _vcredist_overridden(self) -> bool:
        """True when the user pinned a hash explicitly (env or profile)."""
        return vcredist.overridden(self.ctx.profile)

    @staticmethod
    def _vcredist_index_pins() -> set[str]:
        """Installer hashes published in the Nadare index (empty on any error)."""
        return vcredist.index_pins()

    @staticmethod
    def _vcredist_index_source() -> str:
        """The immutable per-build installer URL published in the index."""
        return vcredist.index_source()

    def _verify_vcredist(self, path: Path) -> None:
        """Refuse an installer that matches no accepted pin (deletes it)."""
        vcredist.verify(
            path,
            profile=self.ctx.profile,
            emit=self.ctx.emit,
            cancel=self.ctx.cancel,
            constant=VCREDIST_SHA256,
            index_pins_fn=self._vcredist_index_pins,
            overridden_fn=self._vcredist_overridden,
        )

    def _download_vcredist(self, url: str, tmp: Path, inst: Path) -> None:
        """Download ``url`` to ``tmp``, verify it, and move it to ``inst``."""
        vcredist.download(
            url,
            tmp,
            inst,
            verify_fn=self._verify_vcredist,
            emit=self.ctx.emit,
            cancel=self.ctx.cancel,
        )

    def _install_vcredist(self) -> None:
        """Download, verify and run the VC++ redistributable (may raise)."""
        with self._installer_environment(), self._activity_label(
            "Installing the Visual C++ runtime"
        ):
            self._install_vcredist_inner()

    def _install_vcredist_inner(self) -> None:
        vcredist.install(
            self.prefix(),
            profile=self.ctx.profile,
            cache_dir=self._cache_dir(),
            run_in_prefix=self._run_in_prefix,
            emit=self.ctx.emit,
            cancel=self.ctx.cancel,
            constant=VCREDIST_SHA256,
            index_pins_fn=self._vcredist_index_pins,
            index_source_fn=self._vcredist_index_source,
            overridden_fn=self._vcredist_overridden,
            verify_fn=self._verify_vcredist,
            download_fn=self._download_vcredist,
        )

    def _ensure_vcredist(self) -> None:
        """Best-effort VC++ runtime install; never aborts the caller.

        Always runs for CZN (it is not a user setting; an old ``vcredist: false`` in
        a profile is ignored). Only a user cancellation propagates.
        """
        with self._installer_environment():
            vcredist.ensure(
                self.prefix(),
                profile=self.ctx.profile,
                cache_dir=self._cache_dir(),
                run_in_prefix=self._run_in_prefix,
                emit=self.ctx.emit,
                cancel=self.ctx.cancel,
                constant=VCREDIST_SHA256,
                index_pins_fn=self._vcredist_index_pins,
                index_source_fn=self._vcredist_index_source,
                overridden_fn=self._vcredist_overridden,
                verify_fn=self._verify_vcredist,
                download_fn=self._download_vcredist,
                install_fn=self._install_vcredist,
            )

    def _ensure_stub(self) -> Path:
        stub = self._stub()
        if stub.is_file():
            return stub
        if self._install_prebuilt_stub(stub):
            return stub
        script = Path(__file__).with_name("build_stub.sh")
        if not script.is_file():
            raise BackendError("PCSDK stub is missing and build_stub.sh was not found", 1)
        stub.parent.mkdir(parents=True, exist_ok=True)
        env = self._env()
        env["CZN_STUB_OUT"] = str(stub)
        self.ctx.emit(Event("step", "Building the PCSDK stub (mingw-w64)…"))
        code = self._stream(["bash", str(script)], env)
        if code != 0 or not stub.is_file():
            raise BackendError("PCSDK stub build failed (install mingw-w64)", 1)
        return stub

    @staticmethod
    def _install_prebuilt_stub(dest: Path) -> bool:
        """Copy the bundled stub to ``dest`` if present and digest-verified."""
        try:
            data = STUB_PREBUILT.read_bytes()
        except OSError:
            return False
        if hashlib.sha256(data).hexdigest() != STUB_PREBUILT_SHA256:
            return False
        try:
            dest.parent.mkdir(parents=True, exist_ok=True)
            tmp = dest.with_name(dest.name + ".part")
            tmp.write_bytes(data)
            tmp.chmod(0o755)
            tmp.replace(dest)
        except OSError:
            return False
        return True

    def install(self) -> None:
        if self._game_running():
            self._fail("install", 8)
        self.ctx.set("prefix", str(self.prefix()))
        self._ensure_prefix()
        self._ensure_webview2()
        self._ensure_vcredist()
        self._ensure_stub()
        self.ctx.emit(Event("step", "Installing / verifying game files…"))
        code = self._run_module(UPDATE_MODULE, ["update", "--apply"])
        if code == 3:
            self._fail("install", 8)
        if code != 0:
            self._fail("install", code, UPDATE_EXIT_MESSAGES)
        self.ctx.emit(Event("done", "Chaos Zero Nightmare is installed"))

    def update(self) -> None:
        if self._game_running():
            self._fail("update", 8)
        code = self._run_module(UPDATE_MODULE, ["update", "--apply"])
        if code == 3:
            self._fail("update", 8)
        if code != 0:
            self._fail("update", code, UPDATE_EXIT_MESSAGES)
        self.ctx.emit(Event("done", "Game is up to date"))

    def repair(self) -> None:
        if self._game_running():
            self._fail("repair", 8)
        self.ctx.emit(Event("step", "Checking every game file"))
        self.update()
        bad = self._differing_files()
        if bad:
            shown = ", ".join(bad[:5])
            raise BackendError(
                f"{len(bad)} game file(s) are still damaged: {shown}. "
                "Try again, or reinstall the game."
            )
        self.ctx.emit(Event("done", "All game files check out"))

    def remake_prefix(self) -> None:
        """Move the corrupted prefix aside and rebuild it, keeping the game files."""
        if self._game_running():
            self._fail("remake_prefix", 8)
        self.ctx.emit(Event("step", "Moving the old Wine prefix aside"))
        backup = prefixmove.move_aside(self.prefix())
        moved: list[Path] = []
        try:
            # _ensure_prefix only runs wineboot when drive_c is missing, so the
            # game files must not be restored before it has built the new prefix.
            self._ensure_prefix()
            moved = prefixmove.restore_inside(backup, self.prefix(), [self.game_dir()])
            self.install()
        except BaseException:
            prefixmove.roll_back(backup, self.prefix(), moved)
            raise
        self.ctx.emit(
            Event(
                "done",
                f"The Wine prefix was rebuilt. The old one was kept at {backup}; "
                "delete it when you are sure everything works.",
            )
        )

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

    def _run_auth(self, args: list[str]) -> tuple[int, dict[str, str] | None]:
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

    def _mint_session(self) -> int:
        """One refresh + game-token exchange; writes the ephemeral session."""
        with self._rotation_lock(wait=60):
            self._adopt_newer_credential()
            return self._mint_session_unlocked()

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
        if code != 0:
            self._save_credential(handoff["refresh_token"], handoff.get("member_no"))
            return code
        member = handoff.get("member_no") or ""
        if self._member_no and member and member != self._member_no:
            self._forget_credential()
            # The worker wrote session.json before the mismatch was detected;
            # remove the game token so launch() cannot start with it.
            _unlink_quietly(self._session())
            self._notify_mismatch()
            return 3
        self._save_credential(handoff["refresh_token"], member)
        return 0

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
        raise BackendError("log in first, or enable remember_me", 3)

    # ---- launch ------------------------------------------------------------
    def _prefix_command(
        self, args: list[str], extra_env: dict[str, str] | None = None
    ) -> tuple[list[str], dict[str, str]]:
        """Build the umu-run/wine command and environment for running `args` in
        this profile's prefix with the selected runner."""
        prefix = self.prefix()
        proton, kind = self.resolve_runner()
        env = self._env()
        env["WINEPREFIX"] = str(prefix)
        env.update(graphics.renderer_env(self.ctx.profile, kind=kind, environ=env))
        # CZN is an OpenGL game whose render thread is CPU-bound under Wine. Driver-side
        # GL threading looked helpful on one AMD box (p99 ~140 -> ~50 ms) but the A/B was
        # not controlled, so it is opt-in: ``gl_threading: true`` in the profile.
        if self.ctx.profile.get("gl_threading", False) is True:
            env.setdefault("mesa_glthread", "true")  # Mesa (AMD/Intel/nouveau)
            env.setdefault("__GL_THREADED_OPTIMIZATIONS", "1")  # NVIDIA proprietary
        if extra_env:
            env.update(extra_env)
        if kind == "proton":
            umu = shutil.which("umu-run")
            if umu is None and not sandbox.in_flatpak():
                missing = self._missing_hostlibs()
                if missing:
                    umu = umu_mod.resolve(
                        "umu-run",
                        allow_download=True,
                        emit=self.ctx.emit,
                        cancel=self.ctx.cancel,
                    )
            # umu treats the prefix as Proton's compat-data dir and expects the
            # Wine prefix at <prefix>/pfx; a symlink pfx -> . lets one dir serve both.
            pfx = prefix / "pfx"
            if not pfx.exists():
                try:
                    pfx.symlink_to(".", target_is_directory=True)
                except OSError:
                    pass
            env["PROTONPATH"] = proton
            env.setdefault("GAMEID", "0")
            env["PROTON_ENABLE_WAYLAND"] = os.environ.get(
                "PROTON_ENABLE_WAYLAND", self._wayland_default()
            )
            if extra_env and "PROTON_ENABLE_WAYLAND" in extra_env:
                env["PROTON_ENABLE_WAYLAND"] = extra_env["PROTON_ENABLE_WAYLAND"]
            env.pop("UMU_NO_PROTON", None)
            if umu:
                return [umu, *args], process.sanitize_game_env(env)
            # No umu-run (or inside Flatpak): run the Proton tree directly, so
            # umu-launcher is not a hard dependency.
            if not (Path(proton) / "proton").is_file():
                raise BackendError("the selected runner needs umu-run (umu-launcher)", 1)
            # Running a Steam-Runtime Proton build directly makes it load the
            # host's 32-bit libraries; without them wineboot cannot find
            # FreeType and the WebView2 installer hangs silently. Fail fast
            # (once per backend) unless we are inside the Flatpak runtime, which
            # ships its own libraries.
            if not sandbox.in_flatpak():
                missing = self._missing_hostlibs()
                if missing:
                    raise BackendError(hostlibs.hint(missing), 1)
            argv, env_updates = sandbox.proton_direct(proton, prefix, args)
            env.update(env_updates)
            return argv, process.sanitize_game_env(env)
        wine = os.environ.get("CZN_WINE") or shutil.which("wine")
        if not wine:
            raise BackendError("no wine found; pick a Proton runner or set CZN_WINE", 1)
        # A fresh plain-Wine prefix otherwise pops "Wine could not find a
        # wine-mono / wine-gecko package" dialogs. The game and WebView2 need
        # neither, so suppress both prompts (Proton handles this itself).
        parts = [p for p in str(env.get("WINEDLLOVERRIDES") or "").split(";") if p.strip()]
        if "mscoree,mshtml=" not in parts:
            parts.append("mscoree,mshtml=")
        env["WINEDLLOVERRIDES"] = ";".join(parts)
        return [wine, *args], process.sanitize_game_env(env)

    def _missing_hostlibs(self) -> list[str] | None:
        """The missing host 32-bit sonames, checked at most once per object.

        Honours ``NADARE_SKIP_HOSTLIBS_CHECK=1`` as an escape hatch, and returns
        ``None`` when the check is skipped or inconclusive. Never raises.
        """
        if not self._hostlibs_checked:
            self._hostlibs_checked = True
            if os.environ.get("NADARE_SKIP_HOSTLIBS_CHECK") != "1":
                self._hostlibs_result = hostlibs.missing_32bit()
        return self._hostlibs_result

    def _wayland_default(self) -> str:
        """``1`` (native Wayland) unless the profile asks for X11/XWayland."""
        choice = str(_profile_setting(self.ctx.profile, "display") or "auto").strip().lower()
        return "0" if choice in ("x11", "xwayland") else "1"

    def _mangohud_wrap(self, full: list[str], env: dict[str, str]) -> list[str]:
        """Prefix the game command with ``mangohud`` when ``NADARE_MANGOHUD=1``
        asks for the FPS overlay and it is installed.

        The old per-profile ``mangohud`` key is deliberately ignored: older
        betas could leave it on with no Settings control to turn it off, and
        MangoHud hangs CZN under ``umu-run``.
        """
        if os.environ.get("NADARE_MANGOHUD") != "1":
            return full
        binary = shutil.which("mangohud")
        if not binary:
            self.info("MangoHud is enabled for this profile but is not installed")
            return full
        env.setdefault("MANGOHUD", "1")
        env.setdefault(
            "MANGOHUD_CONFIG", "fps,frametime,frame_timing,gpu_stats,cpu_stats,position=top-left"
        )
        # --dlsym makes MangoHud hook GL/EGL through dlsym, which a Wine/Proton
        # OpenGL game needs.
        return [binary, "--dlsym", *full]

    @contextlib.contextmanager
    def _activity_label(self, label: str):
        """Name the silent work inside the block so ``_stream`` can show it is alive."""
        previous = self._activity
        self._activity = label
        try:
            yield
        finally:
            self._activity = previous

    @contextlib.contextmanager
    def _installer_environment(self):
        """Run prefix commands made inside the block as silent installers.

        They get ``_INSTALLER_ENV`` (no native Wayland: an installer needs no
        window and the Wayland driver breaks the WebView2 one).
        """
        previous = self._run_env
        self._run_env = _INSTALLER_ENV
        try:
            yield
        finally:
            self._run_env = previous

    def _run_in_prefix(self, args: list[str]) -> int:
        cmd, env = self._prefix_command(args, self._run_env)
        cwd = self.game_dir() if self.game_dir().is_dir() else self.prefix()
        return self._stream(cmd, env, cwd=cwd)

    def launch(self) -> int:
        if not self.installed():
            raise BackendError("the game is not installed", 1)
        if self._game_running():
            self._fail("launch", 8)
        if os.environ.get("CZN_SKIP_UPDATE") != "1":
            code = self._run_module(UPDATE_MODULE, ["update", "--apply"])
            if code == 3:
                self._fail("launch", 8)
            if code == 2:
                self.info("could not check for updates (offline?); launching the installed version")
            elif code != 0:
                self._fail("launch update", 5)
        # Restore a remembered/live session once, then mint the game token
        # right before launch so session.json is fresh and short-lived. Clear a
        # stale session only immediately before minting (a session we already
        # minted in this process must survive).
        if not self._restore_attempted:
            self._cleanup_stale_session()
            if not self.restore():
                self._raise_login_required()
        else:
            # A login in this process set the token, or an earlier launch
            # already restored it; (re-)mint the game session right before
            # every launch, since the previous run deletes session.json on exit.
            self._cleanup_stale_session()
            if not self._refresh_token:
                self._raise_login_required()
            auth_code = self._mint_session()
            if auth_code != 0:
                self._fail_auth("token refresh", auth_code)
        session = self._session()
        try:
            stub = self._ensure_stub()
            self._ensure_vcredist()
            log = self._stub_log()
            log.unlink(missing_ok=True)
            session.parent.mkdir(parents=True, exist_ok=True)
            _register_ephemeral(session)
            self.ctx.emit(Event("step", "Starting Chaos Zero Nightmare…"))
            cmd = [
                str(stub),
                self._win(session),
                "--launch",
                self._win(self.shield()),
                self._win_cwd(self.game_dir()),
            ]
            full, env = self._prefix_command(cmd, {"CZN_STUB_LOG": self._win(log)})
            full = self._mangohud_wrap(full, env)
            cwd = self.game_dir() if self.game_dir().is_dir() else self.prefix()
            _renderer, note = graphics.effective_renderer(self.ctx.profile)
            if note:
                self.info(note)
            probe = graphics.RendererProbe(
                self._game_pids, on_result=lambda label: self.info(f"renderer: {label}")
            ).start()
        except BaseException:
            # The minted game token must not outlive a failed pre-launch step.
            session.unlink(missing_ok=True)
            raise
        try:
            with self._activity_label("Starting the game"):
                code = self._stream(
                    full,
                    env,
                    cwd=cwd,
                    kill_game=True,
                    beat_as_log=True,
                    beat_until=lambda: self._stub_connected(log),
                )
        finally:
            # session.json holds a live game token; it must not outlive the run.
            session.unlink(missing_ok=True)
            probe.stop()
            probe.join(timeout=1.0)
        self._renderer = probe.result
        result = self._stub_result(log)
        if result == "ok":
            self.ctx.emit(Event("done", "Game exited normally"))
            return 0
        if result == "no-connect":
            self._fail("launch", 6)
        if result == "handshake-rejected":
            self._fail("launch", 7)
        if code in _STOPPED_EXIT_CODES:
            # SIGTERM/SIGINT through umu/proton (``nadare stop``, or the user closing
            # the window): a deliberate stop, not a failed launch.
            self.ctx.emit(Event("done", "Game stopped"))
            return 0
        self._fail("launch", code or 1)
        return code  # unreachable

    @staticmethod
    def _stub_result(log: Path) -> str | None:
        try:
            m = None
            for line in log.read_text(errors="replace").splitlines():
                hit = _RESULT.search(line)
                if hit:
                    m = hit.group(1)
            return m
        except OSError:
            return None

    def stop(self) -> None:
        pids = self._game_pids()
        if not pids:
            return
        for pid in pids:
            try:
                os.kill(pid, signal.SIGTERM)
            except (ProcessLookupError, PermissionError):
                pass
        deadline = time.time() + 10
        while time.time() < deadline and self._game_running():
            time.sleep(0.25)
        for pid in self._game_pids():
            try:
                os.kill(pid, signal.SIGKILL)
            except (ProcessLookupError, PermissionError):
                pass

    # ---- verify / uninstall ------------------------------------------------
    def verify(self) -> dict[str, Any]:
        checks = {
            "prefix": (self.prefix() / "drive_c").is_dir(),
            "game": self.installed(),
            "stub": self._stub().is_file(),
            "session": bool((info := self._session_info()) and info.get("valid")),
            "webview2": self._webview2_installed(),
            "vcredist": self._vcredist_installed(),
        }
        bad_files = self._differing_files()
        if bad_files is not None:
            checks["files"] = not bad_files
        # A live game session is not expected while logged out (install() never
        # mints one), and the VC++ runtime is an optimisation, so both are
        # informational: reported but must not fail an otherwise-healthy install.
        informational = [name for name in ("session", "vcredist") if not checks.get(name)]
        failed = [name for name, ok in checks.items() if not ok and name not in informational]
        status = self.status()
        if failed:
            message = "integrity check failed: " + ", ".join(failed)
            if bad_files:
                shown = ", ".join(bad_files[:5])
                more = f" (+{len(bad_files) - 5} more)" if len(bad_files) > 5 else ""
                message += f" [{shown}{more}; click Repair files]"
        elif informational:
            if status.data.get("logged_in"):
                message = "all checks passed (the game token is created at launch)"
            else:
                message = "all checks passed (not logged in; log in before launching)"
        else:
            message = "all checks passed"
        if not checks["prefix"]:
            message += " Rebuild the Wine prefix in Settings."
        return {
            "verified": not failed,
            "message": message,
            "checks": checks,
            "failed": failed,
            "informational": informational,
            "bad_files": bad_files or [],
            "prefix": str(self.prefix()),
            "version": status.version,
        }

    def _differing_files(self) -> list[str] | None:
        """Game files that differ from the last applied manifest.

        ``None`` = not checked (no cached manifest yet: it is written by the
        next ``update``/launch, or the install is not there at all).
        """
        cache = self.game_dir() / "combinedata_manifest" / f"nadare-manifest-{SERVICE}.json"
        if not self.installed() or not cache.is_file():
            return None
        got: list[dict[str, Any]] = []
        if self._run_module(UPDATE_MODULE, ["verify"], parse_json=True, collect=got) != 0:
            return None
        for obj in got:
            if obj.get("event") == "verify" and obj.get("checked"):
                return [str(x) for x in obj.get("differ", [])]
        return None

    def uninstall(self, purge: bool = False) -> None:
        if self._game_running():
            self._fail("uninstall", 8)
        prefix = self.prefix()
        left: list[str] = []
        failed: list[str] = []
        if (prefix / "drive_c").is_dir():
            why = self._prefix_not_deletable(prefix)
            if why:
                left.append(f"{prefix} ({why})")
            else:
                self.ctx.emit(Event("step", f"Removing the game prefix {prefix}…"))
                try:
                    shutil.rmtree(prefix)
                except OSError as exc:
                    failed.append(f"{prefix}: {exc}")
        if purge:
            data = self._data_dir()
            safe, reason = tree_safety(self.ctx.config.profiles, self.ctx.name, data)
            if not safe:
                left.append(f"{data} ({reason})")
            else:
                self.ctx.emit(Event("step", "Purging profile data (login, session, stub)…"))
                try:
                    shutil.rmtree(data)
                except OSError as exc:
                    failed.append(f"{data}: {exc}")
        for item in left:
            self.ctx.emit(Event("step", f"Left in place: {item}"))
        if failed:
            raise BackendError("could not remove: " + "; ".join(failed))
        if left:
            self.ctx.emit(Event("done", "Finished; left in place: " + "; ".join(left)))
            return
        self.ctx.emit(Event("done", "Uninstalled"))

    def _prefix_not_deletable(self, prefix: Path) -> str:
        """Why ``prefix`` must be left alone ("" when it is clearly Nadare's to delete)."""
        safe, reason = tree_safety(self.ctx.config.profiles, self.ctx.name, prefix)
        if not safe:
            return reason
        try:
            own = (paths.profiles_dir() / self.ctx.name).resolve()
            resolved = prefix.resolve()
        except OSError:
            return "cannot be resolved"
        if resolved == own or own in resolved.parents:
            return ""
        if (prefix / PREFIX_MARKER).is_file():
            return ""
        return "not created by Nadare"
