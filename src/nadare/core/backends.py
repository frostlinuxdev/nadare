# SPDX-License-Identifier: GPL-3.0-or-later
# Copyright (C) 2026 Nadare contributors
"""The GameBackend contract, its context, and the registry.

A backend knows how to install, update, (optionally) log in to, launch and stop
one game on one runtime stack. The GUI and CLI only ever talk to this interface.
"""

from __future__ import annotations

import importlib
import logging
import re
import threading
from abc import ABC, abstractmethod
from collections.abc import Mapping
from dataclasses import dataclass, field
from pathlib import Path
from typing import Any, ClassVar

from .config import Config, valid_profile_name
from .directkinds import CATALOG_BACKENDS, RETIRED_KINDS
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
    "monitor",
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


#: Settings help for every game's "Wine prefix" folder, in a player's words (it used to say
#: "WINEPREFIX used for this game").
PREFIX_HELP = (
    "The folder that acts as this game's Windows: its C: drive, settings and saves. "
    "Nadare makes it; change it only to use one you already have."
)


#: Real profile settings a backend honours that Settings does not show: installer and
#: manifest overrides, a download token, the Fate/Grand Order installer internals, and the
#: Wine, umu-run and download-worker overrides almost nobody needs.
#: Nothing is deleted; a value already in the profile keeps working and survives a Save.
#: ``docs/CONFIG-ONLY-OPTIONS.md`` documents each one (a test keeps the two in step).
CONFIG_ONLY_KEYS = frozenset(
    {
        "installer_url",
        "installer_sha256",
        "installer_args",
        "manifest_index",
        "manifest_file",
        "media_file",
        "kuro_token",
        "repo",
        "mode",
        "size",
        "release",
        "data_dir",
        "logs_dir",
        "desktop_dir",
        "cache",
        "adopt",
        "force_update",
        "purge_cache",
        "uninstall_all",
        "wine_binary",
        "umu_binary",
        "download_workers",
    }
)

#: Backends where Process match is the only way to find the game, so it is shown.
PROCESS_MATCH_SHOWN = frozenset({"script"})

#: Backends where the user picks the executable (nothing else knows it), so it is shown.
EXE_NAME_SHOWN = frozenset({"wine", "script"})

#: Kinds where each profile is a different game (the user's own .exe, script, or Steam app),
#: so any number of profiles is allowed.
UNLIMITED_KINDS = frozenset({"wine", "script", "steam"})


def config_only(kind: str, key: str) -> bool:
    """True when option ``key`` of backend ``kind`` is honoured but not shown in Settings."""
    if key == "process_match":
        return kind not in PROCESS_MATCH_SHOWN
    if key == "exe_name":
        return kind not in EXE_NAME_SHOWN
    return key in CONFIG_ONLY_KEYS


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

    #: The key of one ``choice`` option the Store's install confirm asks for before the first
    #: download (#990), e.g. a resource quality; ``install_choice_sizes`` gives each value's
    #: download size so the confirm can say how much it fetches.
    install_choice: ClassVar[str | None] = None
    install_choice_sizes: ClassVar[dict[str, int]] = {}

    #: One short line for the game page once the game is installed (#992); the whole
    #: ``page_notice`` stays one click away. ``None`` uses the notice's first sentence.
    page_summary: ClassVar[str | None] = None

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


#: Builtin kinds whose import failed, with the reason (shown by :func:`get`).
_FAILED_IMPORTS: dict[str, str] = {}


def _load_builtin(kind: str) -> type[GameBackend] | None:
    """Import and register one builtin backend, leaving its siblings untouched."""
    if not isinstance(kind, str):
        return None
    with _REGISTRY_LOCK:
        registered = _REGISTRY.get(kind)
        target = _BUILTINS.get(kind)
    if registered is not None or target is None:
        return registered
    mod_name, _, attr = target.partition(":")
    try:
        mod = importlib.import_module(mod_name)
        cls = getattr(mod, attr)
    except Exception as exc:
        with _REGISTRY_LOCK:
            first = kind not in _FAILED_IMPORTS
            _FAILED_IMPORTS[kind] = f"{type(exc).__name__}: {exc}"
        if first:
            # A kind left out on purpose is not a fault; anything else is a broken build.
            level = logging.DEBUG if kind in OPTIONAL_KINDS else logging.WARNING
            log.log(level, "backend %s failed to load: %s", kind, exc, exc_info=True)
        return None
    with _REGISTRY_LOCK:
        return _REGISTRY.setdefault(kind, cls)


def load_builtins() -> None:
    """Import all builtins for explicit inventory checks such as ``doctor``."""
    with _REGISTRY_LOCK:
        pending = [kind for kind in _BUILTINS if kind not in _REGISTRY]
    for kind in pending:
        _load_builtin(kind)


def unavailable() -> list[str]:
    """Built-in kinds whose module failed to import (e.g. left out of a bundle)."""
    load_builtins()
    return sorted(k for k in _BUILTINS if k not in _REGISTRY)


def is_available(kind: str) -> bool:
    """True when ``kind`` resolved to a registered backend (e.g. not omitted)."""
    if not isinstance(kind, str):
        return False
    with _REGISTRY_LOCK:
        if kind in _REGISTRY:
            return True
    return _load_builtin(kind) is not None


def retired_replacement(kind: object) -> str | None:
    """The direct kind that replaced a retired ``kind``, or ``None`` when it is not retired."""
    row = RETIRED_KINDS.get(kind) if isinstance(kind, str) else None
    return row[1] if row else None


def get(kind: str) -> type[GameBackend]:
    if isinstance(kind, str) and kind in RETIRED_KINDS:
        title, _direct = RETIRED_KINDS[kind]
        raise BackendError(
            f"Nadare no longer runs {title} through the publisher's launcher; it installs "
            f"{title} itself now. Remove this game here (its files stay unless you choose to "
            f"delete them), then install {title} from the Store."
        )
    backend = _load_builtin(kind)
    if backend is None:
        reason = _FAILED_IMPORTS.get(kind) if isinstance(kind, str) else None
        if reason:
            raise BackendError(f"backend {kind!r} failed to load: {reason}")
        raise BackendError(f"unknown backend: {kind!r} (have: {', '.join(kinds())})")
    return backend


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


def install_choice(kind: str) -> tuple[Option, dict[str, int]] | None:
    """The option ``kind`` asks for before its first download, and each value's size."""
    try:
        backend = get(kind)
    except BackendError:
        return None
    key = getattr(backend, "install_choice", None)
    if not key:
        return None
    option = next((o for o in backend.options if o.key == key and o.kind == "choice"), None)
    if option is None:
        return None
    return option, dict(getattr(backend, "install_choice_sizes", {}) or {})


def install_choice_label(kind: str, profile: Mapping[str, Any]) -> str:
    """``"Resource quality: HD"`` for a game with an install choice, else ``""`` (#990).

    The value is read like the backend reads it (top level, then ``extra``); an unknown value
    shows the option's default, which is what the backend falls back to.
    """
    asked = install_choice(kind) if kind else None
    if asked is None:
        return ""
    option = asked[0]
    value = profile.get(option.key)
    if value in (None, ""):
        extra = profile.get("extra")
        value = extra.get(option.key) if isinstance(extra, Mapping) else None
    # Matched trimmed and case-blind, as the backends read it ("UHD" is the uhd tier; #1207 b047).
    labels = {str(v).strip().lower(): str(label) for label, v in (option.choices or ())}
    shown = labels.get(str(value).strip().lower()) or labels.get(
        str(option.default).strip().lower(), str(option.default)
    )
    return f"{option.label}: {shown}"


def supports_update(kind: str) -> bool:
    """Whether ``kind`` can update its game (safe for unknown kinds)."""
    try:
        return bool(get(kind).supports_update)
    except BackendError:
        return False


def supports_sandbox(kind: str) -> bool:
    """Whether ``kind`` can run its game in the Sandbox (safe for unknown kinds)."""
    try:
        return bool(getattr(get(kind), "supports_secure_mode", False))
    except BackendError:
        return False


def one_per_game(kind: str) -> bool:
    """Whether ``kind`` is a catalog game with only one profile (safe for unknown kinds)."""
    if not isinstance(kind, str) or not kind or kind in UNLIMITED_KINDS:  # #1207 b046
        return False
    try:
        get(kind)
    except BackendError:
        return False
    return True


def existing_profile(kind: str, profiles: dict[str, Any]) -> str | None:
    """The first profile (in dict order) already using ``kind``, when only one is allowed."""
    if not one_per_game(kind):
        return None
    for name, data in profiles.items():
        if isinstance(data, dict) and data.get("backend") == kind:
            return name
    return None


def sandbox_default(kind: str) -> bool:
    """Whether the Sandbox is on by default for ``kind`` (safe for unknown kinds)."""
    if not supports_sandbox(kind):
        return False
    try:
        return bool(getattr(get(kind), "sandbox_default_on", True))
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


_SENTENCE_END = re.compile(r"(?<=[.!?])\s+(?=[A-Z])")


def page_summary(kind: str) -> str:
    """The short line for ``kind``'s page notice: its ``page_summary``, else the first sentence."""
    notice = page_notice(kind)
    if not notice:
        return ""
    try:
        explicit = getattr(get(kind), "page_summary", None)
    except BackendError:
        explicit = None
    if isinstance(explicit, str) and explicit:
        return explicit
    return _SENTENCE_END.split(notice[0].strip(), maxsplit=1)[0]


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
    if not isinstance(kind, str):
        # A hand-edited or damaged config: say so instead of a TypeError (#1207 b045).
        raise BackendError(f"profile {profile_name!r} has an invalid backend value {kind!r}")
    if kind in RETIRED_KINDS:
        try:
            get(kind)
        except BackendError as exc:
            message = str(exc)
        where = str(profile.get("prefix") or profile.get("wineprefix") or "").strip()
        if where:
            message += (
                f" Its old files are in {where}; delete them yourself once the new install "
                "works if you need the space."
            )
        raise BackendError(message)
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
    "UNLIMITED_KINDS",
    "one_per_game",
    "existing_profile",
    "unofficial_notice",
    "install_size_hint",
    "install_choice_label",
    "install_choice",
    "UNOFFICIAL_NOTICE",
    "page_notice",
    "PREFIX_HELP",
    "page_summary",
    "supports_news",
    "supports_media",
    "sandbox_default",
    "unavailable",
    "is_available",
    "Cancelled",
]

from .events import Event  # noqa: E402  (used by GameBackend.info)
