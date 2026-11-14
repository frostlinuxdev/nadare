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

# Names that tests and callers reach through this module; the code using them moved to
# backend_parts/ (#1248). Re-exported so ``backend.X`` is the same object.
import atexit as atexit  # noqa: E402, F401
import json
import logging
import os
import select as select  # noqa: E402, F401
import shutil as shutil  # noqa: E402, F401
import time as time  # noqa: E402, F401
from pathlib import Path
from typing import Any

from ...core import (
    gameversion,
    graphics,
    monitors,
    paths,
    prefixmove,
    process,
    removal,
    vcredist,
    webview2,
)
from ...core import hostlibs as hostlibs  # noqa: E402, F401
from ...core import sandbox as sandbox  # noqa: E402, F401
from ...core import umu as umu_mod  # noqa: E402, F401
from ...core.backends import (
    PREFIX_HELP,
    BackendError,
    GameBackend,
    Option,
    Status,
)
from ...core.events import Event
from ...core.removal import tree_safety
from ...core.runtimewatch import RuntimeWatch
from .backend_parts.auth import (
    AUTH_MODULE as AUTH_MODULE,
)
from .backend_parts.auth import (
    AuthMixin,
)
from .backend_parts.credentials import (
    _WIPES as _WIPES,
)
from .backend_parts.credentials import (
    CREDENTIAL_HANDOFF as CREDENTIAL_HANDOFF,
)
from .backend_parts.credentials import (
    CREDENTIAL_INPUT as CREDENTIAL_INPUT,
)
from .backend_parts.credentials import (
    LOGOUT_KEYRING_NOTICE as LOGOUT_KEYRING_NOTICE,
)
from .backend_parts.credentials import (
    MISMATCH_NOTICE as MISMATCH_NOTICE,
)
from .backend_parts.credentials import (
    CredentialsMixin,
)
from .backend_parts.credentials import (
    _cache_keyring as _cache_keyring,
)
from .backend_parts.credentials import (
    _keyring_credential as _keyring_credential,
)
from .backend_parts.credentials import (
    _live_clear as _live_clear,
)
from .backend_parts.credentials import (
    _profile_slug as _profile_slug,
)
from .backend_parts.credentials import (
    _read_keyring as _read_keyring,
)
from .backend_parts.credentials import (
    _write_private as _write_private,
)
from .backend_parts.credentials import (
    forget_saved_credential as forget_saved_credential,
)
from .backend_parts.launching import (
    _INSTALLER_ENV as _INSTALLER_ENV,
)
from .backend_parts.launching import (
    _RESULT as _RESULT,
)
from .backend_parts.launching import (
    _STOPPED_EXIT_CODES as _STOPPED_EXIT_CODES,
)
from .backend_parts.launching import (
    GAME_HELPER_EXES as GAME_HELPER_EXES,
)
from .backend_parts.launching import (
    LaunchingMixin,
)
from .backend_parts.launching import (
    _cmdline_has_exe as _cmdline_has_exe,
)
from .backend_parts.prereqs import (
    STUB_C_NAME as STUB_C_NAME,
)
from .backend_parts.prereqs import (
    STUB_C_PREBUILT as STUB_C_PREBUILT,
)
from .backend_parts.prereqs import (
    STUB_C_PREBUILT_SHA256 as STUB_C_PREBUILT_SHA256,
)
from .backend_parts.prereqs import (
    STUB_NAME as STUB_NAME,
)
from .backend_parts.prereqs import (
    STUB_PREBUILT as STUB_PREBUILT,
)
from .backend_parts.prereqs import (
    STUB_PREBUILT_SHA256 as STUB_PREBUILT_SHA256,
)
from .backend_parts.prereqs import (
    VCREDIST_SHA256 as VCREDIST_SHA256,
)
from .backend_parts.prereqs import (
    WEBVIEW2_SHA256 as WEBVIEW2_SHA256,
)
from .backend_parts.prereqs import (
    WEBVIEW2_URL as WEBVIEW2_URL,
)
from .backend_parts.prereqs import (
    PrereqsMixin,
)
from .backend_parts.prereqs import (
    _stub_label as _stub_label,
)
from .backend_parts.prereqs import (
    stub_identity as stub_identity,
)
from .backend_parts.runner import (
    _RUNNER_INTEGRITY_EXIT as _RUNNER_INTEGRITY_EXIT,
)
from .backend_parts.runner import (
    _RUNNER_MATCH as _RUNNER_MATCH,
)
from .backend_parts.runner import (
    DEFAULT_RUNNER as DEFAULT_RUNNER,
)
from .backend_parts.runner import (
    RUNNER_EXIT_MESSAGES as RUNNER_EXIT_MESSAGES,
)
from .backend_parts.runner import (
    RunnerMixin,
)
from .backend_parts.shared import (
    _KEYRING_CACHE as _KEYRING_CACHE,
)
from .backend_parts.shared import (
    _LIVE_LOCK as _LIVE_LOCK,
)
from .backend_parts.shared import (
    _LIVE_SESSIONS as _LIVE_SESSIONS,
)
from .backend_parts.shared import (
    AUTH_EXIT_MESSAGES as AUTH_EXIT_MESSAGES,
)
from .backend_parts.shared import (
    PREFIX_MARKER as PREFIX_MARKER,
)
from .backend_parts.shared import (
    TERMS_URL as TERMS_URL,
)
from .backend_parts.shared import (
    UPDATE_MODULE as UPDATE_MODULE,
)
from .backend_parts.shared import (
    _live_get as _live_get,
)
from .backend_parts.shared import (
    _live_set as _live_set,
)
from .backend_parts.shared import (
    _profile_setting as _profile_setting,
)
from .backend_parts.shared import (
    _register_ephemeral as _register_ephemeral,
)
from .backend_parts.shared import (
    _registered_ephemeral as _registered_ephemeral,
)
from .backend_parts.shared import (
    _remember_notice as _remember_notice,
)
from .backend_parts.shared import (
    _unlink_quietly as _unlink_quietly,
)
from .backend_parts.streaming import (
    _FILE_PROGRESS as _FILE_PROGRESS,
)
from .backend_parts.streaming import (
    _QR_MESSAGE as _QR_MESSAGE,
)
from .backend_parts.streaming import (
    _QR_STATE as _QR_STATE,
)
from .backend_parts.streaming import (
    EXIT_MESSAGES as EXIT_MESSAGES,
)
from .backend_parts.streaming import (
    HEARTBEAT_SECONDS as HEARTBEAT_SECONDS,
)
from .backend_parts.streaming import (
    StreamingMixin,
)
from .news import IMAGE_HOSTS, fetch_czn_news

KIND = "stove-czn"

log = logging.getLogger(__name__)

# One source with core.gameversion (the support report and Find games read the UPF too).
GAME_SUBDIR = gameversion.CZN_GAME_SUBDIR
SERVICE = gameversion.CZN_SERVICE
SHIELD_NAME = "ssr-stove-shield.exe"
#: The WebView2 pipeline lives in :mod:`nadare.core.webview2` so every
#: Wine-family backend shares one verifier and installer. Re-exported here
#: because existing CZN tests (and callers) reference these names.
WEBVIEW2_INDEX_MAX_HASHES = webview2.WEBVIEW2_INDEX_MAX_HASHES
EXIT_WEBVIEW2_UNVERIFIED = webview2.EXIT_WEBVIEW2_UNVERIFIED
EXIT_WEBVIEW2_FAILED = webview2.EXIT_WEBVIEW2_FAILED

#: Visual C++ runtime constants live in :mod:`nadare.core.vcredist` so every
#: Wine-family backend shares one URL/pin. Re-exported here because existing CZN
#: tests (and callers) reference these names.
VCREDIST_URL = vcredist.VCREDIST_URL
VCREDIST_INDEX_MAX_HASHES = vcredist.VCREDIST_INDEX_MAX_HASHES
VCREDIST_HOST_SUFFIXES: tuple[str, ...] = vcredist.VCREDIST_HOST_SUFFIXES

#: ``czn_update.py`` exit codes -> a user-safe message. Its own table: 1 is a
#: download failure and 2 is setup/network, matching neither ``EXIT_MESSAGES``
#: nor the auth table.
UPDATE_EXIT_MESSAGES: dict[int, str] = {
    1: "the game update failed (a download failed)",
    2: "cannot reach the update server",
    5: "the game files are missing or incomplete — press Install to repair them",
}

#: Backend-side (not czn-linux) exit codes for WebView2 failures.
WEBVIEW2_MESSAGES: dict[int, str] = webview2.MESSAGES

def __getattr__(name: str) -> Any:
    if name == "REMEMBER_NOTICE":
        return _remember_notice()
    raise AttributeError(f"module {__name__!r} has no attribute {name!r}")


def _reset_process_state() -> None:
    """Forget live sessions and cached keyring reads (tests/restart)."""
    with _LIVE_LOCK:
        _LIVE_SESSIONS.clear()
        _KEYRING_CACHE.clear()


class CznBackend(
    AuthMixin,
    CredentialsMixin,
    LaunchingMixin,
    PrereqsMixin,
    RunnerMixin,
    StreamingMixin,
    GameBackend,
):
    unofficial_notice = True

    kind = KIND
    supports_remember_me = True
    supports_login = True

    options = (
        Option("prefix", "Wine prefix", "path_dir", PREFIX_HELP),
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
        Option(
            "extra_env",
            "Environment",
            "env",
            "One KEY=VALUE per line, added to the game's environment. To open the "
            "game on a given monitor (native Wayland only) add "
            "WAYLANDDRV_PRIMARY_MONITOR=DP-1 with your monitor's connector name.",
            advanced=True,
        ),
        monitors.OPTION,
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
        #: Remembers runtime failures seen in the output (AppArmor userns block
        #: #429, failed umu Steam Runtime download) to explain the final error.
        self._watch = RuntimeWatch()
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

    def _qr_png(self) -> Path:
        return self._data_dir() / "czn_qr.png"

    def _data_dir(self) -> Path:
        return self.ctx.data_dir()

    def _runners_dir(self) -> Path:
        return paths.runners_dir()

    def _cache_dir(self) -> Path:
        return paths.cache_dir() / "czn"

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
        env = process.child_environ()
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

    # ---- status ------------------------------------------------------------
    def status(self) -> Status:
        installed = self.installed()
        elsewhere = self._running_elsewhere()
        running = self._game_running() or elsewhere
        version = None
        try:
            version = "v" + str(json.loads(self.upf().read_text())["local_version"])
        except Exception:
            pass
        logged_in = bool(self._refresh_token)
        if not logged_in and self._remember_enabled():
            logged_in = self._load_credential() is not None
        session = self._session_info()
        nickname = ""
        if session:
            nickname = str(session.get("nickname") or "")
        if running:
            state, message = "running", "Chaos Zero Nightmare is running"
        elif installed:
            state, message = "installed", f"Installed{(' ' + version) if version else ''}"
        else:
            state, message = "not_installed", "Not installed"
        # No Vulkan driver warning here: the game itself is OpenGL, so a missing
        # Vulkan driver does not stop it (Arknights and Stella Sora keep theirs).
        return Status(
            state=state,
            version=version,
            running=running,
            message=message,
            data={
                "logged_in": logged_in,
                "account": nickname,
                "login_required": installed and not logged_in,
                "session": session,
                "prefix": str(self.prefix()),
                "installed": installed,
                "can_repair": installed,
                "renderer": self._renderer or "",
                **({"running_elsewhere": True} if elsewhere else {}),
            },
        )

    def install(self) -> None:
        if self._running():
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

    def update(self, *, full: bool = False) -> None:
        if self._running():
            self._fail("update", 8)
        args = ["update", "--apply", *(["--full"] if full else [])]
        code = self._run_module(UPDATE_MODULE, args)
        if code == 3:
            self._fail("update", 8)
        if code != 0:
            self._fail("update", code, UPDATE_EXIT_MESSAGES)
        self.ctx.emit(Event("done", "Game is up to date"))

    def repair(self) -> None:
        if self._running():
            self._fail("repair", 8)
        self.ctx.emit(Event("step", "Checking every game file"))
        self.update(full=True)
        bad = self._differing_files()
        if bad:
            shown = ", ".join(bad[:5])
            raise BackendError(
                f"{len(bad)} game file(s) are still damaged: {shown}. "
                "Try again, or reinstall the game."
            )
        if self.installed():
            # The parts Verify names must be rebuilt too, not only the downloaded files.
            # Checked directly: verify() would hash every game file again.
            if not self._stub().is_file():
                self._ensure_stub()
            if not self._webview2_installed():
                self._ensure_webview2()
            remaining = [
                name
                for name, ok in (
                    ("stub", self._stub().is_file()),
                    ("webview2", self._webview2_installed()),
                )
                if not ok
            ]
            if remaining:
                self.ctx.emit(
                    Event(
                        "done",
                        "Repair finished, but these checks still fail: "
                        + ", ".join(remaining),
                    )
                )
                return
        self.ctx.emit(Event("done", "All game files check out"))

    def remake_prefix(self) -> None:
        """Move the corrupted prefix aside and rebuild it, keeping the game files."""
        if self._running():
            self._fail("remake_prefix", 8)
        # Only this game's folder is restored, so another profile's game in the same prefix
        # would be left in the backup (#919).
        from ...core.removal import _shared_with

        other = _shared_with(self.ctx.config.profiles, self.ctx.name, self.prefix().resolve())
        if other:
            raise BackendError(
                f"refusing to rebuild {self.prefix()}: it is shared with profile {other!r}"
            )
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
        backups = self.ctx.profile.get("prefix_backups")
        if not isinstance(backups, list):
            backups = []
        self.ctx.set("prefix_backups", [*backups, str(backup.resolve())])
        self.ctx.emit(
            Event(
                "done",
                f"The Wine prefix was rebuilt. The old one was kept at {backup}; "
                "delete it when you are sure everything works.",
            )
        )

    # ---- the game's terms of service --------------------------------------
    #: Shown by the GUI when a login or launch stops because the terms are not accepted.
    TERMS_MESSAGE = AUTH_EXIT_MESSAGES[5]
    #: STOVE's page for agreeing to them, for users who prefer their browser.
    TERMS_URL = TERMS_URL

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
        if self._running():
            self._fail("uninstall", 8)
        prefix = self.prefix()
        left: list[str] = []
        failed: list[str] = []
        # A prefix Nadare stamped but whose wineboot never finished has no drive_c: still ours
        # to remove (#1207 b019). The deletability check below stays the gate.
        if prefix.is_dir() and (
            (prefix / "drive_c").is_dir() or (prefix / PREFIX_MARKER).is_file()
        ):
            why = self._prefix_not_deletable(prefix)
            if why:
                left.append(f"{prefix} ({why})")
            else:
                self.ctx.emit(Event("step", f"Removing the game prefix {prefix}…"))
                try:
                    outcome = removal._rmtree_safely(prefix)
                except OSError as exc:
                    failed.append(f"{prefix}: {exc}")
                else:
                    left.extend(
                        f"{m} is a separate mounted drive, left in place"
                        for m in outcome.mounts
                    )
                    failed.extend(
                        removal._leftover_warning(p, e) for p, e in outcome.leftovers
                    )
        if purge:
            data = self._data_dir()
            safe, reason = tree_safety(self.ctx.config.profiles, self.ctx.name, data)
            if not safe:
                left.append(f"{data} ({reason})")
            else:
                self.ctx.emit(Event("step", "Purging profile data (login, session, stub)…"))
                try:
                    outcome = removal._rmtree_safely(data)
                except OSError as exc:
                    failed.append(f"{data}: {exc}")
                else:
                    left.extend(
                        f"{m} is a separate mounted drive, left in place"
                        for m in outcome.mounts
                    )
                    failed.extend(
                        removal._leftover_warning(p, e) for p, e in outcome.leftovers
                    )
        for item in left:
            self.ctx.emit(Event("step", f"Left in place: {item}"))
        if failed:
            raise BackendError(
                "Could not remove these items (permissions or in use?): " + "; ".join(failed)
            )
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
        if resolved == own:
            # The game's own Nadare folder also holds its login, session and stub: a plain
            # uninstall must not take them with the prefix (final round).
            return "is this game's own Nadare folder, which also holds its login"
        if own in resolved.parents:
            return ""
        if (prefix / PREFIX_MARKER).is_file():
            return ""
        return "not created by Nadare"
