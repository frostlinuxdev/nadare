# SPDX-License-Identifier: GPL-3.0-or-later
# Copyright (C) 2026 Nadare contributors
"""Finding, checking, downloading and running a publisher installer.

Split out of :mod:`nadare.backends.winegame`; the code is unchanged.
"""

from __future__ import annotations

import logging
import shlex
import urllib.parse
from pathlib import Path

from ...core import (
    diskspace,
    download,
    installerpins,
    process,
)
from ...core.backends import (
    BackendError,
)
from ...core.events import Event
from ..wine import (
    profile_setting,
    stream,
)

log = logging.getLogger("nadare.backends.winegame")


class InstallerMixin:
    """Mixin for :class:`WineGameBackend`; see the module text."""

    def _profile_installer_url(self) -> str | None:
        raw = profile_setting(self.ctx.profile, "installer_url") or profile_setting(
            self.ctx.profile, "installer"
        )
        if raw in (None, ""):
            return None
        return str(raw).strip() or None

    @staticmethod
    def _is_direct_https_exe(url: str) -> bool:
        try:
            parsed = urllib.parse.urlparse(str(url))
        except ValueError:
            return False
        if parsed.scheme != "https" or not parsed.netloc:
            return False
        return Path(urllib.parse.unquote(parsed.path)).name.lower().endswith(".exe")

    def installer_url(self, *, resolve: bool = False) -> str:
        """Return the installer URL (profile override, resolved API, default)."""
        configured = self._profile_installer_url()
        if configured:
            return configured
        if resolve:
            fetched = self._fetch_installer_url()
            if fetched:
                return fetched
        return self.default_installer_url

    def _installer_sha256(self) -> str | None:
        """The pinned installer digest, validated, or ``None`` when unset."""
        raw = profile_setting(self.ctx.profile, "installer_sha256")
        if raw in (None, ""):
            return None
        text = str(raw).strip().lower()
        if len(text) != 64 or any(c not in "0123456789abcdef" for c in text):
            raise BackendError(
                f"installer_sha256 must be a 64-character hex SHA-256, got {raw!r}"
            )
        return text

    def _installer_is_usable(self, url: str) -> bool:
        """Whether ``url`` may be executed: an https .exe that is pinned or on
        Yostar's own hosts. Without a pinned digest an arbitrary host is refused.
        """
        if not self._is_direct_https_exe(url):
            return False
        if self._installer_sha256() is not None:
            return True
        return self._installer_host_trusted(url)

    def require_installer_url(self, *, resolve: bool = False) -> str:
        url = self.installer_url(resolve=resolve)
        if not self._installer_is_usable(url):
            raise BackendError(
                f"no usable {self.display_name} installer URL: set profile 'installer_url' to "
                "the official Windows client .exe link on the publisher's host (or pin its "
                "'installer_sha256'); the pinned default may be stale (see TODO)"
            )
        return url

    def _fetch_installer_url(self, *, timeout: int = 30) -> str | None:
        if not self.installer_api:
            return None
        try:
            payload = download.fetch_json(self.installer_api, timeout=timeout)
        except Exception as exc:  # noqa: BLE001 - fall back to the pinned URL
            self.info(f"could not resolve the installer link ({exc}); using the pinned URL")
            return None
        try:
            url = str(payload["data"]["rows"][0]["client"]).strip()
        except (KeyError, IndexError, TypeError):
            return None
        return url if self._installer_is_usable(url) else None

    @staticmethod
    def _installer_filename(url: str) -> str:
        name = Path(urllib.parse.unquote(urllib.parse.urlparse(url).path)).name
        return name or "Arknights_EN_Gamelauncher-setup.exe"

    def _download_installer(self, url: str) -> Path:
        dest = self.cache_dir() / self._installer_filename(url)
        pinned = self._installer_sha256()
        # An unpinned installer is only trusted by its host, so a redirect must
        # stay on Yostar's hosts; a pinned one is verified by its digest.
        redirect_allowed = None if pinned else self._installer_host_trusted
        size = download.remote_size(url, redirect_allowed=redirect_allowed)
        if size:
            diskspace.check_space(dest.parent, size)
        self.ctx.emit(
            Event("step", f"Downloading the {self.display_name} PC client installer ({dest.name})")
        )
        result = download.download(
            url,
            dest,
            expected_sha256=pinned,
            redirect_allowed=redirect_allowed,
            emit=self.ctx.emit,
            cancel=self.ctx.cancel,
        )
        if pinned is None and self.installer_pin_tag:
            try:
                installerpins.check(self.installer_pin_tag, url, result.path, info=self.info)
            except installerpins.InstallerPinError as exc:
                raise BackendError(
                    f"{self.display_name}: the downloaded installer does not match Nadare's "
                    f"published checksum list ({exc}). Try again later, and report it if it "
                    "keeps happening."
                ) from exc
        return result.path

    def _installer_host_trusted(self, url: str) -> bool:
        """Only a game with its own installer trusts hosts; direct mode never reaches this."""
        raise NotImplementedError

    def _install_launcher_directly(self) -> bool:
        """A backend that fetches its launcher files itself does so here and returns True.

        Returning True means the publisher's installer must not run at all.
        """
        return False

    def _run_installer(self, installer: Path) -> int:
        """Run the publisher's installer in the prefix and wait for it; its exit code."""
        argv, env = self._prefix_command(
            [str(installer), *self._extra_args("installer_args")],
            process.installer_env(),
            sandboxed=self.secure_mode_enabled(),
        )
        plain = argv
        argv, env = self._secure_wrap(
            argv, env, subject="installer", read_only=(installer,), links=False
        )
        self._installer_sandboxed = argv is not plain
        self.ctx.emit(
            Event("step", f"Running the {self.display_name} installer in {self.prefix()}")
        )
        self.ctx.emit(
            Event(
                "step",
                f"Finish the installer window; the {self.display_name} launcher "
                "will then download the game",
            )
        )
        self.info("exec: " + " ".join(shlex.quote(part) for part in argv))
        return stream(argv, env, on_line=self._on_line, cancel=self.ctx.cancel)
