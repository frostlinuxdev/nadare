# SPDX-License-Identifier: GPL-3.0-or-later
# Copyright (C) 2026 Nadare contributors
"""WebView2 for direct games whose own sign-in page is a WebView2 window.

Its own module (not ``direct_games``) so the game modules can share it whatever is imported
first: ``direct`` imports the game modules at its end.
"""

from __future__ import annotations

import contextlib
from pathlib import Path

from ..core import paths, process, webview2, winereg
from .wine import profile_setting, stream


class WebView2SignInMixin:
    """For a direct game whose own sign-in page is a WebView2 window (GFL2 Haoplay, Reverse:
    1999): Nadare installs Microsoft's WebView2 Runtime into the game's prefix on install and
    before each launch. The game's own "download WebView2" offer does nothing under Proton."""

    #: Windows version Wine reports to the game exe only (``None``: the prefix default).
    #: Reverse: 1999's plugin gives its sign-in window WS_EX_LAYERED on Windows 8.1 and
    #: later, and Wine leaves WebView2's child windows inside it undrawn (a white page).
    sign_in_windows_version: str | None = None

    def _ensure_sign_in_runtime(self) -> None:
        # Before WebView2's installer runs: a wineserver still running when this edits
        # user.reg would write its own copy back over the change.
        self._ensure_sign_in_windows_version()
        self._ensure_webview2()

    def _ensure_sign_in_windows_version(self) -> None:
        version = self.sign_in_windows_version
        if not version:
            return
        exe = Path(str(self.direct_exe)).name  # type: ignore[attr-defined]
        try:
            if winereg.set_app_version(self.prefix(), exe, version):
                self.info(f"Wine reports Windows {version.removeprefix('win')} to {exe}")  # type: ignore[attr-defined]
        except OSError as exc:
            self.info(f"could not set the Windows version for {exe}: {exc}")  # type: ignore[attr-defined]

    def _ensure_webview2(self) -> None:
        # Share CZN's pinned Microsoft download, cache verification and silent
        # installer, while using the exact prefix and runner selected here.
        from .czn import backend as czn_backend

        profile = self.ctx.profile
        prefix = self.prefix()
        constant = czn_backend.WEBVIEW2_SHA256

        def run_in_prefix(args: list[str]) -> int:
            cmd, env = self._prefix_command(args, process.installer_env())
            return stream(cmd, env, on_line=self._on_line, cancel=self.ctx.cancel, cwd=prefix)

        def verify(path: Path) -> None:
            webview2.verify(
                path,
                profile=profile,
                cancel=self.ctx.cancel,
                constant=constant,
                index_pins_fn=webview2.index_pins,
                overridden_fn=lambda: webview2.overridden(profile),
            )

        def download(url: str, tmp: Path, inst: Path) -> None:
            webview2.download(
                url, tmp, inst, verify_fn=verify, emit=self.ctx.emit, cancel=self.ctx.cancel
            )

        webview2.ensure(
            prefix,
            profile=profile,
            # the CZN cache: the ~215 MB installer is downloaded once for both games
            cache_dir_fn=lambda: paths.cache_dir() / "czn",
            run_in_prefix=run_in_prefix,
            emit=self.ctx.emit,
            cancel=self.ctx.cancel,
            constant=constant,
            url=czn_backend.WEBVIEW2_URL,
            installed_fn=lambda: webview2.installed(prefix) and webview2.registered(prefix),
            pin_fn=lambda: webview2.resolve_pin(profile, constant),
            index_pins_fn=webview2.index_pins,
            index_source_fn=webview2.index_source,
            overridden_fn=lambda: webview2.overridden(profile),
            verify_fn=verify,
            download_fn=download,
            installer_environment=contextlib.nullcontext,
            activity_label=lambda label: contextlib.nullcontext(),
            explain=self._watch.explain,
        )

    def launch_extra_env(self) -> dict[str, str]:
        configured = profile_setting(self.ctx.profile, "extra_env")
        browser_args = (
            configured.get("WEBVIEW2_ADDITIONAL_BROWSER_ARGUMENTS")
            if isinstance(configured, dict)
            else None
        )
        return {
            **super().launch_extra_env(),
            "WEBVIEW2_ADDITIONAL_BROWSER_ARGUMENTS": str(browser_args or "--no-sandbox"),
        }
