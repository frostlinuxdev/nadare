# SPDX-License-Identifier: GPL-3.0-or-later
# Copyright (C) 2026 Nadare contributors
"""``fgo-lepton`` backend: maps Nadare's GameBackend contract onto lepton-fgo.

All product logic lives in the lepton-fgo CLIs; this class only decides which
verb to run, streams its output as ``Event``s and turns the parsed results into
``Status``/``verify`` reports.

Profile keys (all optional):

``context``
    lepton Android context name (default ``fgo``).
``prefix``
    install prefix (default ``~/.local/share/lepton-nv``).
``repo`` / ``repo_path``
    lepton-fgo source checkout; falls back to ``<prefix>/installer`` and
    ``$LEPTON_FGO_REPO``.
``mode`` / ``size``
    installer mode (``strict``/``normal``) and window size (``WxH``/``auto``).
``adopt``
    pass ``--adopt`` (take over an existing context).
``accept_account_notice``
    pass ``--accept-account-notice`` (default false; the user must acknowledge
    the account-safety notice first).
``force_update``
    always reinstall the newest XAPK when updating.
"""

from __future__ import annotations

import contextlib
import inspect
import json
import logging
import os
import time
from pathlib import Path

from ...core.backends import BackendError, BackendNotInstalled, GameBackend, Option, Status
from ...core.events import Event, mask
from ...core.shortcuts import command_hint
from . import installer
from .lepton import FGO_PKG, LeptonError, LeptonFgo
from .news import IMAGE_HOSTS, fetch_fgo_news, hero_key

log = logging.getLogger(__name__)

#: How long a ``lepton-fgo check-update`` answer counts before the background check runs again.
UPDATE_CHECK_INTERVAL = 6 * 60 * 60
#: The last answer, per profile (#1831): ``status()`` reads it, never the network.
UPDATE_CHECK_FILE = "fgo-update-check.json"

#: First-run account-safety notice shown before install/launch.
ACCOUNT_NOTICE = """\
BEFORE YOU PLAY: SECURE YOUR ACCOUNT

This launcher is unofficial and not supported by Aniplex, TYPE-MOON or
DELIGHTWORKS. Using it may be against the game's terms of service, and your
account could be restricted. Do not use an account you cannot afford to lose.

Secure your account from inside the game first (My Room):
- US and Canada: link an Aniplex Online account (Account Linking).
- Elsewhere: use "Issue Transfer Number" and keep the number somewhere safe.

Never run scripts or "fixes" for FGO from people you do not trust, and keep
any backups private."""

#: Raised when install/launch is attempted before the notice is acknowledged.
_NOTICE_ERROR = "Read and accept the account notice first"


def _page_notice() -> tuple[str, str, str]:
    return (
        "Experimental: FGO runs in an Android container (Lepton). It needs podman and "
        f"a kernel with binder support; run `{command_hint('doctor --fgo')}` to check "
        "your host.",
        "https://github.com/frostlinuxdev/nadare-index/tree/main/lepton",
        "Setup notes",
    )


class _PageNotice:
    """Compute the notice on access so it names the command this build has."""

    def __get__(self, instance, owner):  # noqa: ANN001, ANN201 - descriptor protocol
        return _page_notice()


class FgoLeptonBackend(GameBackend):
    kind = "fgo-lepton"
    account_notice = ACCOUNT_NOTICE

    options = (
        Option("context", "Android context", "text", "lepton context name.", default="fgo"),
        Option("prefix", "Install prefix", "path_dir", "lepton install prefix."),
        Option("repo", "lepton-fgo checkout", "path_dir", advanced=True),
        Option(
            "mode",
            "Installer mode",
            "choice",
            "Installer strictness.",
            choices=(("Strict", "strict"), ("Normal", "normal")),
            advanced=True,
        ),
        Option("size", "Window size", "text", "WxH or 'auto'.", advanced=True),
        Option("adopt", "Adopt existing context", "bool", advanced=True),
        Option("force_update", "Force update", "bool", advanced=True),
        Option("release", "Release", "text", advanced=True),
        Option("data_dir", "Data dir", "path_dir", advanced=True),
        Option("logs_dir", "Logs dir", "path_dir", advanced=True),
        Option("desktop_dir", "Desktop dir", "path_dir", advanced=True),
        Option("cache", "Cache dir", "path_dir", advanced=True),
        Option("purge_cache", "Purge cache on uninstall", "bool", advanced=True),
        Option("uninstall_all", "Uninstall all contexts", "bool", advanced=True),
    )

    #: Official FGO NA news (the Atom feed behind the in-game webview); only the
    #: publisher's webview host is accepted for event banners.
    news_source = staticmethod(fetch_fgo_news)
    official_image_hosts = IMAGE_HOSTS
    #: The event banner ("News") is the hero, not a maintenance notice.
    news_hero_categories = ("News",)
    #: Prefer the newest event by its article date; the evergreen tips page
    #: carries a fake ``0_tips`` path and is skipped.
    news_hero_key = staticmethod(hero_key)

    #: FGO is the one game that needs a container stack; say so up front.
    page_notice = _PageNotice()

    def __init__(self, ctx) -> None:  # noqa: ANN001 - BackendContext
        super().__init__(ctx)
        profile = ctx.profile
        try:
            self.adapter = LeptonFgo(
                profile=profile,
                context=profile.get("context") or "fgo",
                prefix=profile.get("prefix") or None,
                repo=profile.get("repo") or profile.get("repo_path") or None,
            )
        except LeptonError as exc:
            raise BackendError(str(exc)) from exc

    # ---- helpers -----------------------------------------------------------
    def _log(self, line: str) -> None:
        self.info(mask(line))

    def _stream(self, cmd: list[str]) -> int:
        try:
            return self.adapter.stream(cmd, on_line=self._log, cancel=self.ctx.cancel)
        except LeptonError as exc:
            raise BackendError(str(exc)) from exc

    @staticmethod
    def _is_running(status: dict[str, str | None]) -> bool:
        if status.get("venus") == "running":
            return True
        container = status.get("container") or ""
        return container.endswith("(running)")

    # ---- update check (#1831) ----------------------------------------------
    def _update_check_path(self) -> Path:
        return self.ctx.data_path() / UPDATE_CHECK_FILE

    def _read_update_check(self) -> dict | None:
        try:
            blob = json.loads(self._update_check_path().read_text(encoding="utf-8"))
        except (OSError, ValueError):
            return None
        return blob if isinstance(blob, dict) else None

    def check_update(self) -> None:  # type: ignore[override]
        """Ask lepton-fgo whether a newer FGO is out and remember the answer (background).

        Never raises for an ordinary failure; an unreadable answer is not stored, so it can
        never hide an update as "up to date".
        """
        try:
            if not self.ctx.config.data.get("check_updates", True):
                return
        except AttributeError:
            pass
        if not getattr(self.adapter, "installed", False):
            return
        cached = self._read_update_check()
        if cached is not None:
            try:
                age = time.time() - float(cached.get("checked_at", 0))
            except (TypeError, ValueError):
                age = -1
            if 0 <= age < UPDATE_CHECK_INTERVAL:
                return
        try:
            report = self.adapter.check_update()
        except Exception as exc:  # noqa: BLE001 - a background check must never fail
            log.debug("FGO: update check failed: %s", exc)
            return
        available, installed = report.get("available"), report.get("installed")
        if not isinstance(available, bool) or not isinstance(installed, str) or not installed:
            log.debug("FGO: update check gave no usable answer")
            return
        blob = {
            "installed": installed,
            "newest": report.get("newest"),
            "available": available,
            "checked_at": time.time(),
        }
        path = self.ctx.data_dir() / UPDATE_CHECK_FILE
        tmp = path.with_name(f".{path.name}.{os.getpid()}.tmp")
        try:
            tmp.write_text(json.dumps(blob), "utf-8")
            os.replace(tmp, path)
        except OSError as exc:
            with contextlib.suppress(OSError):
                tmp.unlink()
            log.debug("FGO: could not keep the update check: %s", exc)

    def _update_available(self, version: str | None) -> bool:
        """The last check's answer, only while it was made for the version installed now."""
        cached = self._read_update_check()
        if not cached or not version:
            return False
        return cached.get("available") is True and cached.get("installed") == version

    # ---- contract ----------------------------------------------------------
    def status(self) -> Status:
        try:
            parsed = self.adapter.status()
        except LeptonError as exc:
            return Status(state="error", message=str(exc))
        running = self._is_running(parsed)
        if self.adapter.installed:
            state = "running" if running else "installed"
        elif parsed.get("container"):
            # A context can exist without a config if it was hand-built; report it
            # as installed rather than lying about a clean not_installed.
            state = "running" if running else "installed"
        else:
            state = "not_installed"
        message = parsed.get("fgo") or {
            "not_installed": "lepton-fgo is not installed for this context",
            "installed": "installed",
            "running": "running",
        }.get(state, "")
        data = dict(parsed)
        if state != "not_installed":
            data["update_available"] = self._update_available(parsed.get("fgo"))
        return Status(
            state=state, version=parsed.get("fgo"), running=running, message=message, data=data
        )

    def install(self) -> None:
        self.ctx.cancel.raise_if_cancelled()
        if self.requires_notice_ack():
            raise BackendError(_NOTICE_ERROR, code=2)
        try:
            repo = installer.ensure_installer(
                self.adapter, emit=self.ctx.emit, cancel=self.ctx.cancel
            )
        except LeptonError as exc:
            raise BackendError(str(exc)) from exc
        self.adapter.set_repo(repo)
        try:
            # Real adapters accept the cancel token; small test/third-party
            # adapters may not, so only pass it when the signature supports it.
            if "cancel" in inspect.signature(self.adapter.check_host).parameters:
                report = self.adapter.check_host(cancel=self.ctx.cancel)
            else:
                report = self.adapter.check_host()
        except LeptonError as exc:
            raise BackendError(str(exc)) from exc
        self.ctx.emit(Event("step", "host readiness"))
        for line in str(report.get("output") or "").splitlines():
            self._log(line)
        if not report.get("ok"):
            missing_list = list(report.get("missing") or [])
            missing = "; ".join(missing_list)
            if any("check-host.sh" in str(item) for item in missing_list):
                raise BackendError(
                    "lepton-fgo's check-host.sh was not found in "
                    f"{self.adapter.repo}. Re-run the FGO install so Nadare downloads "
                    "the bundled lepton-fgo release again, or point the profile's "
                    "'repo' option (or $LEPTON_FGO_REPO) at a complete lepton-fgo checkout."
                )
            message = (
                f"host is missing required pieces: {missing}"
                if missing
                else "host readiness check failed"
            )
            hint = installer.install_hint(missing_list, installer.os_release())
            if hint:
                message = f"{message}\n{hint}"
            raise BackendError(message)
        if report.get("selinux_enforcing"):
            self._log("SELinux is enforcing; run this once as root, then retry:")
            self._log("  " + self.adapter.selinux_root_command())

        profile = self.ctx.profile
        cmd = self.adapter.cmd_install(
            mode=str(profile.get("mode") or "strict"),
            size=str(profile.get("size") or "1280x720"),
            adopt=bool(profile.get("adopt")),
            yes=True,
            accept_notice=bool(profile.get("accept_account_notice", False)),
            data_dir=profile.get("data_dir") or None,
            logs_dir=profile.get("logs_dir") or None,
            desktop_dir=profile.get("desktop_dir") or None,
            cache=profile.get("cache") or None,
        )
        self.ctx.emit(Event("step", f"installing FGO context {self.adapter.context!r}"))
        code = self._stream(cmd)
        if code != 0:
            self._log(f"install.sh exited with code {code}")
            raise BackendError(
                "FGO setup did not finish. Check that podman is installed and there is "
                "enough free disk, then try again. Details are in Open logs.",
                code=code,
            )
        self.ctx.emit(Event("done", "install finished"))

    def update(self) -> None:
        self.ctx.cancel.raise_if_cancelled()
        if self.requires_notice_ack():
            raise BackendError(_NOTICE_ERROR, code=2)
        force = bool(self.ctx.profile.get("force_update"))
        self.ctx.emit(Event("step", "updating FGO"))
        code = self._stream(self.adapter.cmd_update(force=force))
        if code != 0:
            self._log(f"FGO update command exited with code {code}")
            raise BackendError(
                "The FGO update did not finish. Check your internet connection and "
                "free disk space, then try again. Details are in Open logs.",
                code=code,
            )
        self.ctx.emit(Event("done", "update finished"))

    def launch(self) -> int:
        self.ctx.cancel.raise_if_cancelled()
        if self.requires_notice_ack():
            raise BackendError(_NOTICE_ERROR, code=2)
        if self.adapter.lepton_game.is_file():
            cmd = self.adapter.cmd_game_launch()
        elif self.adapter.lepton_fgo.is_file():
            cmd = self.adapter.cmd_launch()
        else:
            log.info("FGO is not installed under %s", self.adapter.prefix)
            raise BackendNotInstalled(
                "FGO is not installed for this profile. Press Install to set it up again."
            )
        self.ctx.emit(Event("step", f"launching {FGO_PKG}"))
        return self._stream(cmd)

    def stop(self) -> None:
        self.ctx.emit(Event("step", "stopping context"))
        code = self._stream(self.adapter.cmd_stop())
        if code != 0:
            self._log(f"FGO stop command exited with code {code}")
            raise BackendError(
                "FGO could not be stopped. Try again; if it keeps happening, restart "
                "Nadare. Details are in Open logs.",
                code=code,
            )

    def verify(self) -> dict:
        try:
            result = self.adapter.verify(
                release=self.ctx.profile.get("release") or None,
                cancel=self.ctx.cancel,
            )
        except LeptonError as exc:
            raise BackendError(str(exc)) from exc
        output = str(result.get("output") or "")
        return {
            "verified": result.get("returncode") == 0,
            "returncode": result.get("returncode"),
            "output": output,
            "message": output.strip().splitlines()[-1] if output.strip() else "",
        }

