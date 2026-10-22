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

import inspect
import logging

from ...core.backends import BackendError, GameBackend, Option, Status
from ...core.events import Event, mask
from . import installer
from .lepton import FGO_PKG, LeptonError, LeptonFgo
from .news import IMAGE_HOSTS, fetch_fgo_news, hero_key

log = logging.getLogger(__name__)

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
    page_notice = (
        "Experimental: FGO runs in an Android container (Lepton). It needs podman and "
        "a kernel with binder support; run `nadare doctor --fgo` to check your host.",
        "https://github.com/frostlinuxdev/nadare-index/tree/main/lepton",
        "Setup notes",
    )

    def __init__(self, ctx) -> None:  # noqa: ANN001 - BackendContext
        super().__init__(ctx)
        profile = ctx.profile
        try:
            self.adapter = LeptonFgo(
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
        return Status(
            state=state, version=parsed.get("fgo"), running=running, message=message, data=parsed
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
            raise BackendError(
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

    def uninstall(self, purge: bool = False) -> None:
        profile = self.ctx.profile
        cmd = self.adapter.cmd_uninstall(
            purge=purge,
            all_=bool(profile.get("uninstall_all")),
            purge_cache=bool(profile.get("purge_cache")),
            yes=True,
        )
        self.ctx.emit(Event("step", "uninstalling context"))
        code = self._stream(cmd)
        if code != 0:
            self._log(f"FGO uninstall command exited with code {code}")
            raise BackendError(
                "FGO could not be removed. Close the game, then try again. Details are "
                "in Open logs.",
                code=code,
            )
