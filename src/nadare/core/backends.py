# SPDX-License-Identifier: GPL-3.0-or-later
# Copyright (C) 2026 Nadare contributors
"""The GameBackend contract, its context, and the registry.

A backend knows how to install, update, (optionally) log in to, launch and stop
one game on one runtime stack. The GUI and CLI only ever talk to this interface.
"""

from __future__ import annotations

import importlib
import logging
import threading
from abc import ABC, abstractmethod
from dataclasses import dataclass, field
from pathlib import Path
from typing import Any, ClassVar

from .config import Config, valid_profile_name
from .directkinds import CATALOG_BACKENDS
from .events import Cancel, Cancelled, Emitter, emitter


class BackendError(Exception):
    """A backend failed with a message safe to show the user."""

    def __init__(self, message: str, code: int = 1) -> None:
        super().__init__(message)
        self.message = message
        self.code = code


class BackendUnsupported(BackendError):
    """The operation is not applicable to this backend/profile."""


@dataclass
class Status:
    state: str  # not_installed | installed | running | error
    version: str | None = None
    running: bool = False
    message: str = ""
    data: dict[str, Any] = field(default_factory=dict)


@dataclass
class BackendContext:
    """Everything a backend is allowed to know about its environment."""

    name: str  # profile name
    profile: dict[str, Any]
    config: Config
    emit: Emitter = emitter(None)
    cancel: Cancel = field(default_factory=Cancel)

    def data_dir(self) -> Path:
        from . import paths

        root = paths.profiles_dir()
        # The profile name is user-controlled; a lexical check rejects
        # traversal, absolute paths and separators without forbidding a
        # symlinked profile directory (which lets the tree live on another
        # disk). The name is joined onto the trusted root, so it cannot escape.
        if not valid_profile_name(self.name):
            raise BackendError(f"unsafe profile name {self.name!r}")
        d = root / self.name
        d.mkdir(parents=True, exist_ok=True)
        return d

    def profile_dir(self, *parts: str) -> Path:
        p = self.data_dir().joinpath(*parts)
        p.mkdir(parents=True, exist_ok=True)
        return p

    def set(self, key: str, value: Any) -> None:
        """Persist a profile field (e.g. a resolved runner or prefix).

        Called from worker threads, so the mutation and the config write are
        taken under the config lock; otherwise a concurrent GUI-thread save
        could serialise the profile dict while it is being resized.
        """
        with self.config.locked():
            self.profile[key] = value
            self.config.save()


#: Widget kinds an :class:`Option` can request from the Settings page.
OPTION_KINDS = (
    "text",
    "path_dir",
    "path_file",
    "int",
    "bool",
    "choice",
    "runner",
    "args",
    "env",
)


@dataclass(frozen=True)
class Option:
    """One editable backend profile setting.

    ``key`` is the profile key (read from the top level or ``extra``, always
    written to the top level). ``kind`` selects the editor widget. ``choices``
    is a tuple of ``(label, value)`` pairs for ``choice`` options.
    """

    key: str
    label: str
    kind: str
    help: str = ""
    default: Any = None
    choices: tuple[tuple[str, str], ...] | None = None
    advanced: bool = False

    def __post_init__(self) -> None:
        if self.kind not in OPTION_KINDS:
            raise ValueError(f"unknown option kind: {self.kind!r}")
        if self.kind == "choice" and not self.choices:
            raise ValueError(f"choice option {self.key!r} needs choices")


class GameBackend(ABC):
    kind: ClassVar[str] = ""

    #: Editable profile knobs for the Settings page's "Game options" card.
    options: ClassVar[tuple[Option, ...]] = ()

    #: True when the backend can persist a login between runs (and clear it).
    #: The GUI uses this to show the Remember me / Log out controls.
    supports_remember_me: ClassVar[bool] = False

    @staticmethod  # noqa: B027 - intentional optional hook; default is a no-op
    def forget_saved_login(profile: str) -> None:
        """Wipe a persisted login without touching a live session (default: no-op).

        Backends with ``supports_remember_me`` override this. It may block on the
        keyring/DBus, so callers run it off the GUI thread.
        """

    #: True when the backend has a login step (e.g. a QR account link). The GUI
    #: uses this to show the Log in / Log out controls.
    supports_login: ClassVar[bool] = False

    #: True when ``update()`` can bring the game up to date. The GUI disables
    #: the Update button for backends that leave updates to the game/Steam.
    supports_update: ClassVar[bool] = True

    #: Callable returning this backend's official news feed (``NewsResult`` or a
    #: list of items), or ``None`` when the backend has no news source.
    news_source: ClassVar[Any] = None

    #: Hostnames allowed for official publisher image downloads.
    official_image_hosts: ClassVar[tuple[str, ...]] = ()

    #: True when the backend can supply official launcher media (background +
    #: operation banners) through a CI-published media document.
    official_media: ClassVar[bool] = False

    #: Categories preferred for the news hero image. Empty means "newest item
    #: with an image"; when set, the newest item in one of these categories is
    #: used, falling back to the newest with an image.
    news_hero_categories: ClassVar[tuple[str, ...]] = ()

    #: First-run account-safety notice. When non-``None`` the user must
    #: acknowledge it (per profile) before ``install``/``launch`` are allowed.
    account_notice: ClassVar[str | None] = None

    #: Optional informational note for the game page as ``(text, url)`` or
    #: ``(text, url, link_label)``, e.g. "also coming to Steam". The label
    #: defaults to "Learn more". Purely informational; never blocks anything.
    page_notice: ClassVar[tuple[str, str] | tuple[str, str, str] | None] = None

    #: True for games Nadare runs without the publisher's blessing: the user is
    #: told once (per game type) before the first install, login or launch.
    unofficial_notice: ClassVar[bool] = False

    #: Rough size of a fresh install in bytes, so the GUI can ask before a very
    #: large download. ``None`` means unknown (no prompt).
    install_size_hint: ClassVar[int | None] = None

    def __init__(self, ctx: BackendContext) -> None:
        self.ctx = ctx

    def requires_notice_ack(self, profile: dict[str, Any] | None = None) -> bool:
        """True when this backend has a notice the profile has not accepted."""
        if not self.account_notice:
            return False
        data = self.ctx.profile if profile is None else profile
        return not bool(data.get("accept_account_notice", False))

    # Convenience for subclasses.
    def info(self, message: str, **data: Any) -> None:
        self.ctx.emit(Event("log", message, data=data))

    # ---- lifecycle ---------------------------------------------------------
    @abstractmethod
    def status(self) -> Status:
        """Cheap, read-only. Never mutates anything."""

    @abstractmethod
    def install(self) -> None:
        """Set up prefix/runtime and download the game."""

    @abstractmethod
    def update(self) -> None:
        """Bring the game up to date."""

    @abstractmethod
    def launch(self) -> int:
        """Run the game. Return the process exit code."""

    @abstractmethod
    def stop(self) -> None:
        """Stop a running game for this profile."""

    def login(self) -> None:
        """Optional; raise BackendUnsupported if the backend has no login step."""
        raise BackendUnsupported("this backend has no login step")

    def set_remember(self, enabled: bool) -> None:
        """Optional; persist/surrender this profile's login. Default: unsupported."""
        raise BackendUnsupported("this backend cannot remember a login")

    def logout(self) -> None:
        """Optional; forget any saved and in-memory login. Default: unsupported."""
        raise BackendUnsupported("this backend has no login to clear")

    @staticmethod  # noqa: B027 - intentional optional hook; default is a no-op
    def check_update() -> None:
        """Optional background update check (default: no-op).

        Backends with a published version cache may refresh it here. It must
        never be required for :meth:`status` and must not raise for an ordinary
        network failure.
        """

    def verify(self) -> dict[str, Any]:
        """Optional integrity report. Default: nothing to verify."""
        return {"verified": False, "message": "nothing to verify"}

    def repair(self) -> None:
        """Optional; re-check every game file and download damaged ones."""
        raise BackendUnsupported("Repair is not available for this game")

    def remake_prefix(self) -> None:
        """Optional; rebuild the Wine prefix while keeping the game files."""
        raise BackendUnsupported("Rebuilding the Wine prefix is not available for this game")

    def uninstall(self, purge: bool = False) -> None:
        """Remove the game tree / prefix. Keep user data unless purge."""
        raise BackendUnsupported("this backend cannot uninstall")


# --------------------------------------------------------------------------
# registry
# --------------------------------------------------------------------------
log = logging.getLogger(__name__)

_REGISTRY: dict[str, type[GameBackend]] = {}

#: Guards lazy registry mutation; ``get()`` may run from UI/doctor threads.
_REGISTRY_LOCK = threading.Lock()

# Import path per kind. Imported lazily so a missing optional dep (e.g. PyQt6 in
# a headless CLI) never breaks unrelated backends.
_BUILTINS = {
    "stove-czn": "nadare.backends.czn.backend:CznBackend",
    "fgo-lepton": "nadare.backends.fgo.backend:FgoLeptonBackend",
    "wine": "nadare.backends.wine:WineBackend",
    "steam": "nadare.backends.steam:SteamBackend",
    "script": "nadare.backends.script:ScriptBackend",
    "arknights-yostar": "nadare.backends.arknights:ArknightsBackend",
    "stellasora-yostar": "nadare.backends.stellasora:StellaSoraBackend",
    **CATALOG_BACKENDS,
}


#: Built-in kinds a build may deliberately leave out (``NADARE_WITHOUT_FGO=1``).
#: ``nadare doctor`` reports these as "not in this build", not as a fault.
OPTIONAL_KINDS = frozenset({"fgo-lepton"})


def register(kind: str, cls: type[GameBackend]) -> None:
    with _REGISTRY_LOCK:
        _REGISTRY[kind] = cls


def kinds() -> list[str]:
    return sorted(set(_REGISTRY) | set(_BUILTINS))


def load_builtins() -> None:
    with _REGISTRY_LOCK:
        for kind, target in _BUILTINS.items():
            if kind in _REGISTRY:
                continue
            mod_name, _, attr = target.partition(":")
            try:
                mod = importlib.import_module(mod_name)
                _REGISTRY[kind] = getattr(mod, attr)
            except Exception:
                # A backend whose dependencies are absent simply is not offered.
                log.debug("backend %s unavailable", kind, exc_info=True)
                continue


def unavailable() -> list[str]:
    """Built-in kinds whose module failed to import (e.g. left out of a bundle)."""
    load_builtins()
    return sorted(k for k in _BUILTINS if k not in _REGISTRY)


def is_available(kind: str) -> bool:
    """True when ``kind`` resolved to a registered backend (e.g. not omitted)."""
    load_builtins()
    return kind in _REGISTRY


def get(kind: str) -> type[GameBackend]:
    load_builtins()
    if kind not in _REGISTRY:
        raise BackendError(f"unknown backend: {kind!r} (have: {', '.join(kinds())})")
    return _REGISTRY[kind]


def supports_remember_me(kind: str) -> bool:
    """Whether ``kind`` can persist a login (safe for unknown kinds)."""
    try:
        return bool(get(kind).supports_remember_me)
    except BackendError:
        return False


def supports_login(kind: str) -> bool:
    """Whether ``kind`` has a login step (safe for unknown kinds)."""
    try:
        return bool(get(kind).supports_login)
    except BackendError:
        return False


#: Shown once per game type before the first install, login or launch.
UNOFFICIAL_NOTICE = (
    "It is not made, supported or approved by the game's publisher, and the "
    "publisher's terms of service may not allow playing this way. Using it is your "
    "own choice and your own risk, including the risk to your account.\n\n"
    "Nadare never asks for your password and does not change the game's files to cheat."
)


def unofficial_notice(kind: str) -> bool:
    """Whether ``kind`` needs the one-time unofficial-launcher notice (safe for unknown kinds)."""
    try:
        return bool(get(kind).unofficial_notice)
    except BackendError:
        return False


def install_size_hint(kind: str) -> int | None:
    """Rough fresh-install size of ``kind`` in bytes, or ``None`` when unknown."""
    try:
        return get(kind).install_size_hint
    except BackendError:
        return None


def supports_update(kind: str) -> bool:
    """Whether ``kind`` can update its game (safe for unknown kinds)."""
    try:
        return bool(get(kind).supports_update)
    except BackendError:
        return False


def supports_news(kind: str) -> bool:
    """Whether ``kind`` has an official news source (safe for unknown kinds)."""
    try:
        return getattr(get(kind), "news_source", None) is not None
    except BackendError:
        return False


def supports_media(kind: str) -> bool:
    """Whether ``kind`` can supply official launcher media (safe for unknown kinds)."""
    try:
        return bool(getattr(get(kind), "official_media", False))
    except BackendError:
        return False


def page_notice(kind: str) -> tuple[str, str] | tuple[str, str, str] | None:
    """The backend's informational ``(text, url[, link_label])`` page note, if any."""
    try:
        return getattr(get(kind), "page_notice", None)
    except BackendError:
        return None


def create(
    profile_name: str, config: Config, emit: Emitter | None = None, cancel: Cancel | None = None
) -> GameBackend:
    try:
        profile = config.profile(profile_name)
    except KeyError as exc:
        raise BackendError(f"unknown profile: {profile_name!r}") from exc
    kind = profile.get("backend") or ""
    if not kind:
        raise BackendError(f"profile {profile_name!r} has no backend set")
    ctx = BackendContext(
        name=profile_name,
        profile=profile,
        config=config,
        emit=emitter(emit),
        cancel=cancel or Cancel(),
    )
    return get(kind)(ctx)


# Re-export so backends can `from nadare.core.backends import Cancelled`.
__all__ = [
    "BackendError",
    "BackendUnsupported",
    "Status",
    "BackendContext",
    "GameBackend",
    "Option",
    "OPTION_KINDS",
    "register",
    "get",
    "create",
    "kinds",
    "load_builtins",
    "supports_remember_me",
    "supports_login",
    "supports_update",
    "unofficial_notice",
    "install_size_hint",
    "UNOFFICIAL_NOTICE",
    "page_notice",
    "supports_news",
    "supports_media",
    "unavailable",
    "is_available",
    "Cancelled",
]

from .events import Event  # noqa: E402  (used by GameBackend.info)
