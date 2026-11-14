# SPDX-License-Identifier: GPL-3.0-or-later
# Copyright (C) 2026 Nadare contributors
"""Choosing and installing the runner a game uses.

Split out of :mod:`nadare.backends.winegame`; the code is unchanged.
"""

from __future__ import annotations

import logging

from ...core import (
    download,
    runners,
)
from ...core.backends import (
    BackendError,
)
from ...core.events import Event
from ..wine import (
    profile_setting,
)

log = logging.getLogger("nadare.backends.winegame")


class RunnerMixin:
    """Mixin for :class:`WineGameBackend`; see the module text."""

    def runner_id(self) -> str:
        rid = runners.profile_runner_value(self.ctx.profile)
        return "" if rid.lower() in ("wine", "system", "none") else rid

    def _default_runner_applies(self) -> bool:
        """True when this profile should use the pinned DW-Proton by default.

        Whenever it names no runner (``wine``/``system``/``none`` count as unset)
        and no ``wine_binary``. An explicit ``wine_binary`` is still honoured.
        """
        # Parsed like the launch reads it: a whitespace-only value is "no custom Wine" (05-9), so
        # it must not switch the default Proton off and reach a random plain Wine (#1338).
        wine = str(profile_setting(self.ctx.profile, "wine_binary") or "").strip()
        if self.runner_id() or wine:
            return False
        return True

    def _wanted_family(self) -> str | None:
        """The runner family this profile gets from Nadare: a named family id
        (``dwproton``/``ge``/``cachyos``), or the default one; ``None`` otherwise."""
        rid = self.runner_id()
        if rid in runners.RUNNERS:
            return rid
        if not rid and self._default_runner_applies():
            return runners.DEFAULT_FAMILY
        return None

    def _ensure_default_runner(self, *, strict: bool) -> None:
        """Reuse the pinned build of the wanted family, or download it once.

        ``strict`` raises when it cannot be set up (launch needs it); otherwise a
        status notice is emitted and the caller carries on (a direct install only
        syncs files and needs no runtime until launch). A cancel passes through.
        """
        family = self._wanted_family()
        if family is None or runners.installed_pinned(family) is not None:
            self._runner_setup_error = None
            return
        self.ctx.emit(Event("step", "Setting up the Proton runner (one-time download)"))
        try:
            runners.ensure_pinned(family, emit=self.ctx.emit, cancel=self.ctx.cancel)
        except (runners.RunnerError, download.DownloadError, BackendError, OSError) as exc:
            self._runner_setup_error = exc
            if strict:
                if isinstance(exc, BackendError):
                    raise
                raise BackendError(f"could not set up the Proton runner: {exc}") from exc
            self.ctx.emit(
                Event(
                    "status",
                    f"Proton runner not set up yet ({exc}); it will be tried again at launch",
                )
            )
        else:
            self._runner_setup_error = None

    def resolve_runner(self) -> tuple[str | None, str]:
        """Return ``(path, kind)`` with kind ``proton`` or ``wine``.

        Never downloads (status polls call this): an unset runner resolves to the
        pinned DW-Proton only when it is already installed.
        """
        rid = self.runner_id()
        family = self._wanted_family()
        if family is not None:
            pinned = runners.installed_pinned(family)
            return (str(pinned), "proton") if pinned is not None else (None, "wine")
        if not rid:
            return None, "wine"
        try:
            found = runners.resolve_runner(rid)
        except runners.RunnerError as exc:
            raise BackendError(str(exc)) from exc
        if found is None:
            return None, "wine"
        return str(found), "proton"
