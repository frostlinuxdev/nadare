# SPDX-License-Identifier: GPL-3.0-or-later
# Copyright (C) 2026 Nadare contributors
"""Direct-download games (``direct-<game>`` kinds).

These games install straight from the publisher's public patch API, with no
launcher: a per-game source (:mod:`nadare.core.sources`) turns the API into a
:class:`~nadare.core.directdl.Plan` and :mod:`nadare.core.directdl` installs it.
Everything else (Wine/Proton runner, prefix, launch, process detection, status)
is inherited from :class:`~nadare.backends.winegame.WineGameBackend`, the neutral
Wine/Proton base shared with the Yostar backend; only the source of the file list differs.

Profile keys are the same as ``arknights-yostar`` minus the Yostar manifest and
installer keys.
"""

from __future__ import annotations

import dataclasses
import hashlib
import json
import logging
import os
import re
import shutil
import stat
import threading
import time
from collections.abc import Callable, Iterable, Mapping
from pathlib import Path
from types import SimpleNamespace
from typing import Any

from ..core import (
    archivepins,
    directdl,
    diskspace,
    gamedrive,
    graphics,
    installedfiles,
    kuropatch,
    monitors,
    process,
)
from ..core.backends import PREFIX_HELP, BackendError, BackendNotInstalled, Option, Status
from ..core.config import as_bool, profile_value
from ..core.events import Cancelled
from ..core.sandboxed import SANDBOX_EXEMPTION_OPTIONS
from ..core.sources import Fetch, fetch_cancel, patch_list_fetch
from .wine import profile_setting
from .winegame import (
    INSTALLED_PREFIX_KEY,
    UPDATE_CHECK_INTERVAL,
    WineGameBackend,
    atomic_write_json,
)

#: A version that may name a predownload cache folder ("3.4.0", "3.8.0+pack-3.8.1"):
#: no separators or leading dot. ``+`` is a valid filename character and keeps the name
#: exactly as the version record stores it, so a Kuro tier plan and its cache agree (#W10-F5).
_SAFE_VERSION = re.compile(r"[0-9A-Za-z][0-9A-Za-z._+-]{0,31}")


def _plan_identity(plan: directdl.Plan) -> str:
    """What a predownload of ``plan`` fetched: every file's path, size and hashes (#1497).

    Two plans with one version can need different files (WuWa HD and UHD), and a publisher can
    re-publish a version. Download addresses are left out: signed URLs change on every fetch.
    """

    def remote(rf: directdl.RemoteFile) -> list[object]:
        return [rf.path, rf.size, (rf.md5 or "").lower(), (rf.sha256 or "").lower(), rf.crc32]

    shape = {
        "game": plan.game_tag,
        "version": str(plan.version),
        "files": sorted(remote(rf) for rf in plan.files),
        "archives": [
            [a.name, remote(a.source), [remote(p) for p in a.parts], list(a.unwrap)]
            for a in plan.archives
        ],
        "sophon": [[p.name, p.manifest_id, p.size] for p in plan.sophon],
    }
    blob = json.dumps(shape, sort_keys=True, separators=(",", ":"), default=str)
    return hashlib.sha256(blob.encode("utf-8")).hexdigest()[:32]


def _selection_key(kwargs: Mapping[str, Any]) -> str:
    """The player's choices behind a plan (quality, token), hashed: a token is never stored."""
    blob = json.dumps({str(k): str(v) for k, v in kwargs.items()}, sort_keys=True)
    return hashlib.sha256(blob.encode("utf-8")).hexdigest()[:32]


#: Sizes below are decimal gigabytes, as the publishers and archive tables report them.
GB = 10**9

#: Written into a Kuro cleanup mark only when this release completed the cleanup. b23 wrote an
#: empty mark even when it gave up, so an empty (or older) mark must not mean "done" (#1252).
TIDY_MARK_GENERATION = "2\n"

#: Games whose own command line has a splash/intro switch: game tag -> the arguments that
#: skip it. Only a flag here is offered in Settings; every other game never sees the option.
SKIP_INTRO_ARGS: dict[str, tuple[str, ...]] = {
    "wuwa": ("-SkipSplash",),
    "mongil": ("-nosplash",),
}

#: The per-game "Skip intro videos" switch, offered only to the games in ``SKIP_INTRO_ARGS``.
SKIP_INTRO_OPTION = Option(
    "skip_intro",
    "Skip intro videos",
    "bool",
    "Starts the game without its logo videos. Turn it off if the game misbehaves after an "
    "update.",
    default=False,
)

log = logging.getLogger(__name__)


def _base_version_key(version: str) -> tuple[int, ...]:
    """Numbers of a version's base ("3.8.0+hd-3.8.1" -> (3, 8, 0)); text parts count as 0."""
    base = str(version).split("+", 1)[0]
    return tuple(int(part) if part.isdigit() else 0 for part in base.split("."))


class DirectBackend(WineGameBackend):
    """Base for the per-game classes below; never registered on its own."""

    def _before_sync(self) -> None:
        """Hook run first in every Install/Update/Repair sync (WuWa records its tiers)."""

    def _direct_extra_plan(self, plan: directdl.Plan) -> directdl.Plan | None:
        """A plan to sync once before the file plan (BPSR: the whole-game zip on a fresh
        install). ``None`` means the file plan alone."""
        return None

    def _direct_file_check_hashes(self, extra: directdl.Plan | None) -> bool:
        """Whether the file pass compares hashes too.

        Files an extra plan just wrote came from the publisher's verified archive, so only
        their sizes need checking; Verify still hashes every file."""
        return extra is None

    def _tidy_allowed(self) -> bool:
        """Whether the retired-files tidy may run now (WuWa: not while a tier is pending)."""
        return True

    def _tidy_names(self, names: Iterable[str]) -> list[str]:
        """The retired files the tidy may delete (WuWa keeps other tiers' folders out)."""
        return list(names)

    #: umu game id (``GAMEID``/``UMU_ID``) for the runner's per-game fixes; ``None`` sends none
    #: (umu's default). A profile ``extra_env`` GAMEID/UMU_ID always wins (#1334).
    umu_id: str | None = None
    #: Drive letter for a game folder outside ``drive_c``, made at every launch; ``None`` keeps
    #: Wine's ``Z:`` path (see :mod:`nadare.core.gamedrive`, #1608).
    game_drive_letter: str | None = None

    def _prefix_command(
        self,
        args: list[str],
        extra_env: dict[str, str] | None = None,
        *,
        sandboxed: bool = False,
    ) -> tuple[list[str], dict[str, str]]:
        argv, env = super()._prefix_command(args, extra_env, sandboxed=sandboxed)
        if self.umu_id is None:
            return argv, env  # umu's default, as before: no id is sent
        profile_env = profile_setting(self.ctx.profile, "extra_env")
        explicit = {**(profile_env if isinstance(profile_env, dict) else {}), **(extra_env or {})}

        def given(key: str) -> str:
            # By presence, not truthiness: a hand-edited JSON 0 is a real id (stress F2).
            value = explicit.get(key)
            return "" if value is None else str(value).strip()

        env["GAMEID"] = given("GAMEID") or self.umu_id
        env["UMU_ID"] = given("UMU_ID") or env["GAMEID"]
        return argv, env

    def __init_subclass__(cls, **kwargs: Any) -> None:
        super().__init_subclass__(**kwargs)
        # Settings must name the real default: an X11-default game is not "Auto (Wayland)".
        # A game whose own help already names X11 (Stella Sora's swipe) keeps it.
        if getattr(cls, "default_display", "wayland") == "x11":
            cls.options = tuple(
                _x11_display_option(o) if _says_auto_wayland(o) else o for o in cls.options
            )
        # One table entry is all a new game needs: the same id gets the Settings switch and
        # the launch flag (see ``skip_intro_args``).
        if getattr(cls, "game_tag", None) in SKIP_INTRO_ARGS and not any(
            option.key == "skip_intro" for option in cls.options
        ):
            cls.options = cls.options + (SKIP_INTRO_OPTION,)

    kind = "direct"
    supports_resume = True
    supports_secure_mode = True
    install_size_hint = 20 * 1024**3
    #: The community publishes these without the vendor's blessing.
    unofficial_notice = True
    news_source = None
    official_media = False
    options = (
        Option("prefix", "Wine prefix", "path_dir", PREFIX_HELP),
        Option("install_dir", "Game folder", "path_dir", "Where the game lives."),
        Option("exe_name", "Game executable", "text", advanced=True),
        Option(
            "runner",
            "Runner",
            "runner",
            "Proton build/tag; empty uses the tested DW-Proton (downloaded once).",
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
            "Wayland is the default. X11 (XWayland) can help if the window misbehaves.",
            default="auto",
            choices=(
                ("Auto (Wayland)", "auto"),
                ("Wayland", "wayland"),
                ("X11 (XWayland)", "x11"),
            ),
        ),
        Option(
            "download_workers",
            "Download workers",
            "int",
            "Maximum parallel downloads (1-32); the plan may use fewer.",
            default=32,
            advanced=True,
        ),
        Option("launch_args", "Launch arguments", "args"),
        Option(
            "secure_mode",
            "Sandbox",
            "bool",
            "Hides your home folder from the game (needs bubblewrap). The game can still use "
            "the network. See docs/SECURE-MODE.md.",
            default=True,
        ),
        Option("wine_binary", "Wine binary", "path_file", advanced=True),
        Option("umu_binary", "umu-run binary", "path_file", advanced=True),
        Option(
            "stop_grace_seconds",
            "Stop grace (seconds)",
            "int",
            "How long Stop asks the game to close before it is forced; 0 skips the request. "
            "Only for the games Nadare launches itself.",
            default=8,
            advanced=True,
        ),
        Option("extra_env", "Environment", "env", "One KEY=VALUE per line.", advanced=True),
        monitors.OPTION,
        Option("process_match", "Process match", "text", advanced=True),
        *SANDBOX_EXEMPTION_OPTIONS,
    )

    def skip_intro_args(self) -> list[str]:
        """The splash-skip switch for this game, when the player turned it on (#1427)."""
        args = SKIP_INTRO_ARGS.get(self.game_tag, ())
        if not args or not as_bool(profile_setting(self.ctx.profile, "skip_intro"), False):
            return []
        given = self._extra_args("launch_args")
        return [arg for arg in args if arg not in given]

    #: ``Plan`` factory for this game: ``(fetch) -> Plan``.
    plan_factory: Callable[[Fetch | None], directdl.Plan]
    #: The next-version plan when the publisher offers a predownload window, else ``None``.
    predownload_factory: Callable[..., directdl.Plan | None] | None = None

    def plan_kwargs(self) -> dict[str, Any]:
        """Extra keyword arguments for ``plan_factory`` (default: none)."""
        return {}

    def _patch_fetch(self, url: str, headers: Mapping[str, str] | None = None) -> bytes:
        with fetch_cancel(self.ctx.cancel):  # Stop ends a retry wait (#1816)
            return patch_list_fetch(url, headers)

    def plan(self) -> directdl.Plan:
        try:
            with fetch_cancel(self.ctx.cancel):
                plan = type(self).plan_factory(None, **self.plan_kwargs())
        except directdl.DirectError as exc:
            raise BackendError(
                f"{self.display_name}: could not read the publisher's download "
                f"information ({exc}). Check your connection or try again later."
            ) from exc
        try:
            loaded = archivepins.load_state(self.game_tag)
        except Exception:  # noqa: BLE001 - published pins are optional
            return plan
        try:
            plan, notes = archivepins.check(plan, loaded)
        except archivepins.PinMismatch as exc:
            # #1158: the file at a pinned address changed. Refuse before anything is written.
            self.info(str(exc))
            raise BackendError(f"{self.display_name}: {exc}") from exc
        # Logged on every read; shown on the page only by an install/update (_sync_direct), so a
        # background update check does not repeat it every few hours.
        self._pin_notes = notes
        for note in notes:
            self.info(note)
        return plan

    # ---- hooks the Arknights machinery calls ------------------------------
    def manifest_source(self) -> str:
        return self.game_tag

    def direct_mode(self) -> bool:
        return True

    def _require_manifest(self) -> str:
        return self.game_tag

    def _load_manifest(self) -> directdl.Plan:  # type: ignore[override]
        return self.plan()

    def _missing_entries(self, *, verify: bool = True) -> list[Any]:
        plan = self.plan()
        try:
            files, archives = directdl.plan_missing(plan, self.install_dir(), verify=verify)
        except directdl.DirectError as exc:
            raise BackendError(f"{self.display_name}: {exc}") from exc
        return [
            *files,
            *(SimpleNamespace(path=archive.name) for archive in archives),
            *(
                SimpleNamespace(path=pkg.name)
                for pkg in directdl.sophon_missing(plan, self.install_dir())
            ),
        ]

    # ---- leftover cleanup state -------------------------------------------
    def _leftover_state_path(self) -> Path:
        return self.install_dir() / directdl.STATE_DIR / "leftovers.json"

    def _read_leftover_state(self) -> list[str]:
        """Retired names this install still owes, or ``[]`` when absent, stale or unsafe."""
        try:
            fd = os.open(self._leftover_state_path(), os.O_RDONLY | os.O_NOFOLLOW)
        except OSError:
            return []
        try:
            with os.fdopen(fd, "rb") as handle:
                if not stat.S_ISREG(os.fstat(handle.fileno()).st_mode):
                    return []
                raw = handle.read(64 * 1024)
        except OSError:
            return []
        try:
            payload = json.loads(raw)
        except (TypeError, ValueError):
            return []
        if not isinstance(payload, dict):
            return []
        version = payload.get("version")
        names = payload.get("names")
        if not isinstance(version, str) or not version or not isinstance(names, list):
            return []
        if not all(isinstance(name, str) and name for name in names):
            return []
        record = self._read_version_file()
        installed = str(record.get("version") or "") if record else ""
        return names if version == installed else []

    def _write_leftover_state(self, version: str, names: Iterable[str]) -> None:
        atomic_write_json(
            self._leftover_state_path(),
            {"version": str(version), "names": sorted(set(names))},
        )

    def _clear_leftover_state(self) -> None:
        try:
            self._leftover_state_path().unlink(missing_ok=True)
        except OSError as exc:
            log.debug("%s: could not remove leftover state: %s", self.display_name, exc)

    def _leftovers_on_disk(self, names: Iterable[str]) -> list[str]:
        """The names still present; a link counts, since deleting one never follows it."""
        root = self.install_dir()
        found: list[str] = []
        for name in names:
            try:
                path = root / directdl.safe_relative_path(name)
            except directdl.DirectError:
                continue
            if os.path.lexists(path):
                found.append(name)
        return found

    # ---- predownload state and cache --------------------------------------
    def _predownload_state_path(self) -> Path:
        return self.install_dir() / directdl.STATE_DIR / "predownload.json"

    def _predownload_cache_root(self) -> Path:
        return self.install_dir() / directdl.STATE_DIR / "predownload"

    def _predownload_cache(self, version: str) -> Path:
        """The cache folder for ``version``; a path-like version is refused, never mapped."""
        name = str(version)
        if not _SAFE_VERSION.fullmatch(name):
            raise BackendError(f"{self.display_name}: unusable version name {name!r}")
        return self._predownload_cache_root() / name

    def _read_predownload_state(self) -> dict[str, Any] | None:
        """The last predownload check, or ``None`` when absent or malformed."""
        try:
            fd = os.open(self._predownload_state_path(), os.O_RDONLY | os.O_NOFOLLOW)
        except OSError:
            return None
        try:
            with os.fdopen(fd, "rb") as handle:
                raw = handle.read(64 * 1024)
        except OSError:
            return None
        try:
            payload = json.loads(raw)
        except (TypeError, ValueError):
            return None
        if not isinstance(payload, dict):
            return None
        version = payload.get("version")
        if version is not None and (not isinstance(version, str) or not version):
            return None
        checked = payload.get("checked_at", 0)
        if not isinstance(checked, (int, float)):
            checked = 0
        plan_id, selection = payload.get("plan"), payload.get("selection")
        return {
            "version": version,
            "checked_at": int(checked),
            # Only JSON true means ready: a hand-edited string "false" is truthy to bool().
            "ready": payload.get("ready") is True,
            # What the ready receipt is for (#1497); absent in a version-only receipt.
            "plan": plan_id if isinstance(plan_id, str) and plan_id else None,
            "selection": selection if isinstance(selection, str) and selection else None,
        }

    def _write_predownload_state(
        self,
        version: str | None,
        ready: bool,
        checked_at: int | None = None,
        *,
        plan_id: str | None = None,
    ) -> None:
        atomic_write_json(
            self._predownload_state_path(),
            {
                "version": version,
                "checked_at": int(time.time()) if checked_at is None else checked_at,
                "ready": bool(ready),
                "plan": plan_id,
                "selection": self._predownload_selection(),
            },
        )

    def _legacy_receipt_holds(self, plan: directdl.Plan) -> bool:
        """Whether a version-only receipt's cache provably holds ``plan`` (#1497 upgrade).

        HoYo's Sophon packages mark each finished package with its manifest id, so that cache
        answers exactly. Kuro's marker records only the version (not the quality), so a
        Kuro receipt is not trusted: Predownload is offered again and keeps verified files.
        """
        if not plan.sophon:
            return False
        cache = self._usable_predownload_cache(str(plan.version))
        return cache is not None and directdl.predownload_ready(plan, cache)

    def _predownload_selection(self) -> str:
        return _selection_key(self.plan_kwargs())

    def _predownload_ready(self, state: Mapping[str, Any] | None) -> bool:
        """A receipt is ready only for the plan it fetched under today's choices (#1497).

        The plan check is in the refresh (it needs the publisher); this one is local, so a
        quality switch shows at once, even offline. A version-only receipt is not trusted."""
        return bool(
            state
            and state.get("ready")
            and state.get("plan")
            and state.get("selection") == self._predownload_selection()
        )

    def _refresh_predownload_state(self) -> None:
        """Refresh the next-version notice; a background check only ever logs."""
        if self.predownload_factory is None:
            return
        record = self._read_version_file()
        target = self.direct_target()
        if record is None or target is None or not target.is_file():
            return
        try:
            if not self.ctx.config.data.get("check_updates", True):
                return
        except AttributeError:
            pass
        old = self._read_predownload_state()
        # A receipt from 0.1.0b24 (version only) is checked at once, not after the interval:
        # until then it cannot say which files it fetched (#1497).
        if old is not None and old.get("plan") is not None:
            try:
                age = time.time() - float(old.get("checked_at", 0))
            except (TypeError, ValueError):
                age = UPDATE_CHECK_INTERVAL
            if 0 <= age < UPDATE_CHECK_INTERVAL:
                return
        try:
            with fetch_cancel(self.ctx.cancel):
                plan = type(self).predownload_factory(None, **self.plan_kwargs())
        except Cancelled:
            raise
        except (directdl.DirectError, BackendError, OSError) as exc:
            log.debug("%s: predownload check failed: %s", self.display_name, exc)
            return
        try:
            if plan is None:
                self._write_predownload_state(None, False)
            else:
                plan_id = _plan_identity(plan)
                same_version = old is not None and old.get("version") == str(plan.version)
                if same_version and old is not None and old.get("plan") is None:
                    ready = bool(old.get("ready")) and self._legacy_receipt_holds(plan)
                else:
                    ready = bool(
                        same_version
                        and old is not None
                        and old.get("plan") == plan_id
                        and self._predownload_ready(old)
                    )
                self._write_predownload_state(str(plan.version), ready, plan_id=plan_id)
        except OSError as exc:
            log.debug("%s: could not cache the predownload state: %s", self.display_name, exc)

    def _usable_predownload_cache(self, version: str) -> Path | None:
        """The real cache folder for ``version``, never a symlink or a path outside its root."""
        root = self._predownload_cache_root()
        try:
            root_info = os.lstat(root)
        except OSError:
            return None
        if not stat.S_ISDIR(root_info.st_mode):
            return None
        try:
            cache = self._predownload_cache(version)
        except BackendError:
            return None
        try:
            info = os.lstat(cache)
        except OSError:
            return None
        return cache if stat.S_ISDIR(info.st_mode) else None

    def _predownload_cache_for(self, plan: directdl.Plan) -> Path | None:
        """The predownload cache a sync of ``plan`` may use (and remove when it succeeds).

        Its own version first. A file plan (Kuro) may also take an older predownload: its files
        are kept by md5, so a hotfix released before the player updated still uses every cached
        file that did not change, and the old cache does not stay behind. A predownload for a
        version newer than ``plan`` (a repair before the release) is never touched.
        """
        exact = self._usable_predownload_cache(plan.version)
        if exact is not None or plan.sophon:
            return exact
        target = _base_version_key(plan.version)
        root = self._predownload_cache_root()
        try:
            names = sorted(entry.name for entry in root.iterdir())
        except OSError:
            return None
        older = [n for n in names if _base_version_key(n) <= target]
        for name in sorted(older, key=_base_version_key, reverse=True):
            cache = self._usable_predownload_cache(name)
            if cache is not None:
                return cache
        return None

    def _remove_other_predownload_caches(self, keep: Path) -> None:
        """Drop caches of other versions; never follow a planted link."""
        root = self._predownload_cache_root()
        try:
            root_info = os.lstat(root)
        except OSError:
            return
        if stat.S_ISLNK(root_info.st_mode):
            try:
                root.unlink()
            except OSError:
                pass
            return
        if not stat.S_ISDIR(root_info.st_mode):
            return
        try:
            entries = list(root.iterdir())
        except OSError:
            return
        for entry in entries:
            try:
                info = os.lstat(entry)
            except OSError:
                continue
            if entry.name == keep.name and not stat.S_ISLNK(info.st_mode):
                continue  # this version's own cache; a link planted at its name goes below
            try:
                if stat.S_ISLNK(info.st_mode) or not stat.S_ISDIR(info.st_mode):
                    entry.unlink()
                elif stat.S_ISDIR(info.st_mode):
                    shutil.rmtree(entry)
            except OSError:
                log.debug("%s: could not remove %s", self.display_name, entry, exc_info=True)

    def _remove_outdated_predownloads(self, plan: directdl.Plan) -> None:
        """After a sync that used no cache, drop caches no newer than what is now installed.

        Sophon takes only its exact version's ready cache, so a hotfix released after the
        predownload (or a rebuilt manifest) left 10-30 GiB behind until the next one (#1828).
        A cache for a newer version (a repair before the release) stays, with its state.
        """
        target = _base_version_key(plan.version)
        root = self._predownload_cache_root()
        try:
            root_info = os.lstat(root)
        except OSError:
            root_info = None
        if root_info is not None and stat.S_ISDIR(root_info.st_mode):
            try:
                entries = list(root.iterdir())
            except OSError:
                entries = []
            for entry in entries:
                if _base_version_key(entry.name) > target:
                    continue
                try:
                    info = os.lstat(entry)
                    if stat.S_ISDIR(info.st_mode):
                        shutil.rmtree(entry)
                    else:
                        entry.unlink()  # a file or a planted link: never what it points at
                except OSError:
                    log.debug("%s: could not remove %s", self.display_name, entry, exc_info=True)
            try:
                root.rmdir()  # only when nothing newer is left
            except OSError:
                pass
        state = self._read_predownload_state()
        version = state.get("version") if state else None
        if version and _base_version_key(str(version)) <= target:
            try:
                self._predownload_state_path().unlink(missing_ok=True)
            except OSError:
                log.debug("%s: could not remove predownload state", self.display_name)

    def _clear_predownload_cache(self) -> None:
        """Remove the whole predownload cache and its state after a successful update."""
        root = self._predownload_cache_root()
        try:
            info = os.lstat(root)
        except OSError:
            pass
        else:
            try:
                if stat.S_ISLNK(info.st_mode):
                    root.unlink()  # only the link, never what it points at
                elif stat.S_ISDIR(info.st_mode):
                    shutil.rmtree(root)
            except OSError:
                log.debug("%s: could not remove %s", self.display_name, root, exc_info=True)
        try:
            self._predownload_state_path().unlink(missing_ok=True)
        except OSError:
            log.debug("%s: could not remove predownload state", self.display_name, exc_info=True)

    def predownload(self) -> dict[str, Any]:
        """Fetch the next version into its cache while the game stays playable."""
        self.ctx.cancel.raise_if_cancelled()
        if self.predownload_factory is None:
            raise BackendError(f"{self.display_name} has no predownload")
        target = self.direct_target()
        record = self._read_version_file()
        if target is None or not target.is_file() or record is None:
            raise BackendNotInstalled(f"{self.display_name} is not installed")
        try:
            with fetch_cancel(self.ctx.cancel):
                plan = type(self).predownload_factory(None, **self.plan_kwargs())
        except directdl.DirectError as exc:
            raise BackendError(f"{self.display_name}: {exc}") from exc
        if plan is None or str(plan.version) == str(record["version"]):
            raise BackendError(f"{self.display_name}: no predownload is offered right now")
        cache = self._predownload_cache(plan.version)
        self._remove_other_predownload_caches(cache)
        workers = directdl.worker_cap(plan, profile_value(self.ctx.profile, "download_workers"))
        files = None
        if (
            not plan.sophon
            and isinstance(plan.extra, Mapping)
            and kuropatch.EXTRA_KEY in plan.extra
        ):
            try:
                patches = kuropatch.prepare(plan, str(record["version"]), self._patch_fetch)
            except directdl.DirectError:
                patches = None
            if patches is not None and patches.found:
                files = kuropatch.predownload_files(patches, plan)
        try:
            kwargs = {} if plan.sophon else {"files": files}
            result = directdl.predownload(
                plan,
                self.install_dir(),
                cache,
                emit=self.ctx.emit,
                cancel=self.ctx.cancel,
                workers=workers,
                **kwargs,
            )
        except directdl.DirectError as exc:
            raise BackendError(f"{self.display_name}: {exc}") from exc
        self._write_predownload_state(str(plan.version), True, plan_id=_plan_identity(plan))
        self.info(
            f"{self.display_name}: downloaded {diskspace.human_size(result.get('bytes', 0))} "
            f"for {plan.version} ahead of its release"
        )
        return result

    def _direct_status_parts(
        self, target: Path | None, incomplete: bool
    ) -> tuple[dict[str, Any], str | None, Status | None]:
        additions, version, early = super()._direct_status_parts(target, incomplete)
        leftovers = self._leftovers_on_disk(self._read_leftover_state())
        if leftovers:
            additions["leftover_files"] = len(leftovers)
        if target is None or not target.is_file() or not version:
            return additions, version, early
        state = self._read_predownload_state()
        predownload = state.get("version") if state else None
        if not predownload or predownload == version:
            return additions, version, early
        latest = self._read_latest()
        published = str(latest["version"]) if latest else None
        if predownload != published:
            additions["predownload_version"] = predownload
            additions["predownload_ready"] = self._predownload_ready(state)
        return additions, version, early

    def _sync_direct(self) -> dict[str, Any]:
        self._refuse_if_running("installing, updating or repairing it")
        self._before_sync()
        self._pin_notes: list[str] = []
        plan = self._load_manifest()
        chunk_cache = self._predownload_cache_for(plan)
        from ..core.events import Event  # local: threading.Event is used below too

        for note in self._pin_notes:
            self.ctx.emit(Event("step", f"{self.display_name}: {note}"))
        workers = directdl.worker_cap(plan, profile_value(self.ctx.profile, "download_workers"))
        marker = self._install_marker_path()
        # An install that finished before (a version is recorded, no marker) stays usable if an
        # update fails before it replaced anything. Once a file was replaced, files may already have
        # been replaced one by one (there is no rollback), so the marker stays and the page says
        # RESUME instead of promising the old version still works (#856).
        record = self._read_version_file()
        was_complete = not marker.exists() and record is not None
        # Say what this sync is, so a restart after it stops can too (#831); resuming an
        # interrupted update keeps its old version and aims at the current one.
        earlier = None if was_complete else self._interrupted_update()
        update = None
        if was_complete:
            update = (str(record["version"]), str(plan.version))
        elif earlier is not None:
            update = (earlier[0], str(plan.version))
        self._write_install_marker(update)
        from ..core.events import Event

        changed = threading.Event()

        retired: tuple[str, ...] = ()
        self._patch_numbers: dict[str, int] = {}
        try:
            # Kuro's update patches first (#1252): a speed-up only. The whole-file sync below
            # always runs and fetches whatever a patch did not produce, exactly as before.
            if update is not None and update[0] and update[0] != update[1]:
                try:
                    retired = self._apply_update_patches(
                        plan, update[0], changed, cache=chunk_cache
                    )
                except Cancelled:
                    raise  # the user's Cancel stops the update; it is not a patch problem
                except Exception as exc:  # noqa: BLE001 - a patch problem never fails an update
                    self.info(f"{self.display_name}: update patches not used ({exc})")
            self.ctx.emit(Event("step", f"{self.display_name}: syncing {plan.version} directly"))
            extra = self._direct_extra_plan(plan)
            # Only a size-only pass is passed on; the default call stays as every game's was.
            size_only = {} if self._direct_file_check_hashes(extra) else {"verify": False}
            if extra is not None:
                directdl.sync(
                    extra,
                    self.install_dir(),
                    emit=self.ctx.emit,
                    cancel=self.ctx.cancel,
                    workers=workers,
                    **size_only,
                    changed=changed,
                )
            fetched = [0]

            def counted(event: Event) -> None:
                # The sync's overall download counter, for the update receipt (#1252).
                data = event.data if isinstance(event.data, dict) else {}
                if event.kind == "progress" and data.get("stage") == "download":
                    fetched[0] = max(fetched[0], int(data.get("done") or 0))
                elif event.kind == "log" and data.get("download_summary"):
                    # Every byte read from the network: a streamed pack only reports its
                    # progress as "install", which left Endfield's 52 GiB at "0 B".
                    fetched[0] = max(fetched[0], int(data.get("bytes") or 0))
                self.ctx.emit(event)

            if chunk_cache is not None:
                summary = directdl.sync(
                    plan,
                    self.install_dir(),
                    emit=counted,
                    cancel=self.ctx.cancel,
                    workers=workers,
                    **size_only,
                    changed=changed,
                    chunk_cache=chunk_cache,
                )
            else:
                summary = directdl.sync(
                    plan,
                    self.install_dir(),
                    emit=counted,
                    cancel=self.ctx.cancel,
                    workers=workers,
                    **size_only,
                    changed=changed,
                )
        except directdl.DirectError as exc:
            if was_complete and not changed.is_set():
                marker.unlink(missing_ok=True)
                raise BackendError(
                    f"{self.display_name}: {exc}. Nothing was left half-installed; try again."
                ) from exc
            raise BackendError(
                f"{self.display_name}: {exc}. The update stopped part-way, so some files may be "
                "new and some old. Press RESUME (or Repair files) to finish it."
            ) from exc
        except BaseException:
            if was_complete and not changed.is_set():
                marker.unlink(missing_ok=True)
            raise
        marker.unlink(missing_ok=True)
        self._record_synced_version(plan)
        if chunk_cache is not None:
            self._clear_predownload_cache()
        else:
            self._remove_outdated_predownloads(plan)
        self._remove_retired(plan, retired)
        if update is not None and update[0] and update[0] != update[1]:
            text, too_big = update_receipt(
                update[0],
                str(plan.version),
                patch_bytes=int(self._patch_numbers.get("patch_bytes", 0)),
                # What came over the network; a predownloaded file is not counted (#1830).
                whole_bytes=int(getattr(summary, "fetched_bytes", fetched[0])),
                publisher_bytes=int(self._patch_numbers.get("publisher", 0)),
            )
            self.info(f"{self.display_name}: {text}")
            if too_big:
                self.ctx.emit(Event("step", f"{self.display_name}: {too_big}"))
        self.info(
            f"{self.display_name}: synced {summary.get('downloaded', 0)} item(s), "
            f"{summary.get('skipped', 0)} already up to date"
        )
        return summary

    def _apply_update_patches(
        self,
        plan: directdl.Plan,
        installed: str,
        changed: threading.Event,
        cache: Path | None = None,
    ) -> tuple[str, ...]:
        """Apply the publisher's update patches from ``installed``; return the files it retired.

        Any problem only means "no patch": the whole-file sync that follows covers it.
        """
        from ..core.events import Event

        if not isinstance(plan.extra, dict) or kuropatch.EXTRA_KEY not in plan.extra:
            return ()
        try:
            patches = kuropatch.prepare(plan, installed, self._patch_fetch)
        except directdl.DirectError as exc:
            self.info(f"{self.display_name}: no update patch ({exc}); downloading whole files")
            return ()
        self._patch_numbers["publisher"] = patches.publisher_bytes
        if patches.groups:
            self.ctx.emit(
                Event(
                    "step",
                    f"{self.display_name}: updating {installed} -> {plan.version} with patches "
                    f"({diskspace.human_size(patches.download_bytes)} to download)",
                )
            )
            outcome = kuropatch.apply(
                patches,
                plan,
                self.install_dir(),
                emit=self.ctx.emit,
                cancel=self.ctx.cancel,
                changed=changed,
                cache=cache,
            )
            self._patch_numbers["patch_bytes"] = outcome.downloaded_bytes
            self.info(
                f"{self.display_name}: patched {outcome.applied} group(s); "
                f"{outcome.skipped} left to the full download"
            )
        return patches.deletes

    #: Kuro games: remove files an older Nadare left after an update, once per version (#1252).
    tidies_retired = False
    _TIDY_MARK = "tidied-{version}"

    def _tidy_mark_is_current(self, mark: Path) -> bool:
        try:
            return mark.read_text(encoding="utf-8") == TIDY_MARK_GENERATION
        except OSError:
            return False

    def check_update(self) -> None:
        super().check_update()
        try:
            self._refresh_predownload_state()
        except Cancelled:
            raise
        except Exception as exc:  # noqa: BLE001 - a background check must never fail on this
            log.debug("%s: predownload check skipped: %s", self.display_name, exc)
        try:
            self._tidy_retired_once()
        except Cancelled:
            raise
        except Exception as exc:  # noqa: BLE001 - a background check must never fail on this
            log.debug("%s: tidy-up skipped: %s", self.display_name, exc)

    def _tidy_retired_once(self) -> None:
        """Remove retired files once per installed version, with no click needed (#1252).

        Players who updated with Nadare 0.1.0b22 or older keep the files Kuro retired (26 GB
        after WuWa 3.8.0). Only a complete install whose version is the published one is
        tidied, never while the game runs or another operation holds it.
        """
        if not self.tidies_retired or not self._tidy_allowed():
            return
        record = self._read_version_file()
        if record is None or self._install_incomplete():
            return
        version = str(record.get("version") or "")
        state = self.install_dir() / directdl.STATE_DIR
        mark = state / self._TIDY_MARK.format(version=version.replace("/", "_"))
        if not version or self._tidy_mark_is_current(mark):
            return
        plan = self._load_manifest()
        if str(plan.version) != version or self._game_running():
            return  # an update is waiting (it tidies itself), or the game is open
        lock = process.OperationLock(self.ctx.name)
        try:
            lock.acquire()
        except process.ProfileLockError:
            # Another operation has the game: say what it will need to tidy, without deleting.
            scan = kuropatch.retired_scan(plan, self.install_dir(), self._patch_fetch)
            postponed = self._tidy_names(scan.names)
            if postponed:
                self._write_leftover_state(version, postponed)
                self.info(
                    f"{self.display_name}: {len(postponed)} file(s) an earlier update left "
                    "behind; cleanup postponed while another operation has the game"
                )
            return
        try:
            # The game may start between the cheap pre-check above and the lock.
            if self._game_running():
                return
            scan = kuropatch.retired_scan(plan, self.install_dir(), self._patch_fetch)
            names, _ = installedfiles.split_changed(  # a file the player changed stays (#1837)
                self.install_dir(), self._tidy_names(scan.names), self._installed_files()
            )
            if names:
                removed = kuropatch.delete_retired(self.install_dir(), tuple(names))
                if removed:
                    self.info(
                        f"{self.display_name}: removed {removed} file(s) an earlier update "
                        "left behind"
                    )
            remaining = self._leftovers_on_disk(names)
            if scan.unreadable:
                versions = ", ".join(scan.unreadable)
                self.info(
                    f"{self.display_name}: could not read Kuro patch list(s) for {versions}; "
                    "the cleanup will try again at the next update check"
                )
            if remaining:
                self.info(
                    f"{self.display_name}: {len(remaining)} file(s) from an older version are "
                    "still in the game folder. Press Repair files to remove them."
                )
            if remaining:
                self._write_leftover_state(version, remaining)
            else:
                self._clear_leftover_state()
            if scan.complete and not remaining:
                state.mkdir(parents=True, exist_ok=True)
                mark.write_text(TIDY_MARK_GENERATION, encoding="utf-8")
        finally:
            lock.release()

    def _remove_retired(self, plan: directdl.Plan, retired: tuple[str, ...]) -> None:
        """Remove files the publisher retired: from this update's patch list, and any an
        earlier update left behind that Kuro's older lists name (#1252). Never fails the sync."""
        names = set(retired)
        try:
            scan = kuropatch.retired_scan(plan, self.install_dir(), self._patch_fetch)
            names.update(scan.names)
            names = set(self._tidy_names(names))
        except Cancelled:
            raise
        except Exception as exc:  # noqa: BLE001 - a cleanup problem is only logged
            self.info(f"{self.display_name}: could not check for left-over files ({exc})")
        # A dropped file the player changed is not removed by name (#1837).
        removable, kept = installedfiles.split_changed(
            self.install_dir(), sorted(names), self._installed_files()
        )
        if kept:
            self.info(installedfiles.kept_note(self.display_name, kept))
        names = set(removable)
        if names:
            removed = kuropatch.delete_retired(self.install_dir(), tuple(sorted(names)))
            if removed:
                self.info(
                    f"{self.display_name}: removed {removed} file(s) the publisher no longer uses"
                )
        remaining = self._leftovers_on_disk(sorted(names))
        try:
            if remaining:
                self._write_leftover_state(str(plan.version), remaining)
            else:
                self._clear_leftover_state()
        except OSError as exc:  # a read-only install must not turn a good sync into a failure
            log.debug("%s: could not record leftover state: %s", self.display_name, exc)
        installedfiles.write(self.install_dir(), self._installed_tag(), plan, kept)

    def _installed_tag(self) -> str:
        return str(getattr(self, "game_tag", "") or self.kind)

    def _installed_files(self) -> dict[str, dict[str, Any]]:
        return installedfiles.read(self.install_dir(), self._installed_tag())

    # ---- lifecycle (same shape as Stella Sora's) --------------------------
    def install(self) -> None:
        self.ctx.cancel.raise_if_cancelled()
        self.ctx.set(INSTALLED_PREFIX_KEY, str(self.prefix()))
        self._ensure_default_runner(strict=False)
        self._ensure_direct_prefix()
        self._ensure_sign_in_runtime()
        self._sync_direct()

    def _ensure_sign_in_runtime(self) -> None:
        """Set up what the game's own sign-in needs in the prefix (default: nothing)."""

    def update(self) -> None:
        self.ctx.cancel.raise_if_cancelled()
        target = self.direct_target()
        if target is None or not target.is_file():
            raise BackendNotInstalled(f"{self.display_name} is not installed")
        self._sync_direct()

    def launch(self) -> int:
        self.ctx.cancel.raise_if_cancelled()
        target = self.direct_target()
        if target is None or not target.is_file():
            raise BackendError(f"game executable not found: {target}")
        self._ensure_default_runner(strict=True)
        self._ensure_sign_in_runtime()
        self._ensure_game_drive()
        return self._launch_direct()

    def _ensure_game_drive(self) -> None:
        if not self.game_drive_letter:
            return
        letter = gamedrive.ensure(
            self.prefix(), self.install_dir(), self.game_drive_letter, exe=self.direct_exe or ""
        )
        if letter:
            self.info(f"game folder is drive {letter.upper()}: in the prefix")


#: An update this many times the publisher's own size (plus 1 GiB) is reported (#1252).
RECEIPT_LIMIT = 2


def update_receipt(
    old: str, new: str, *, patch_bytes: int, whole_bytes: int, publisher_bytes: int
) -> tuple[str, str]:
    """``(log line, warning or "")`` for an update: what it downloaded vs the publisher's size."""
    total = patch_bytes + whole_bytes
    text = (
        f"update {old} -> {new}: downloaded {diskspace.human_size(total)} "
        f"(patches {diskspace.human_size(patch_bytes)}, whole files "
        f"{diskspace.human_size(whole_bytes)})"
    )
    if publisher_bytes <= 0:
        return text, ""
    text += f"; the publisher's own update is about {diskspace.human_size(publisher_bytes)}"
    if total > RECEIPT_LIMIT * publisher_bytes + 2**30:
        return text, (
            f"this update downloaded {diskspace.human_size(total)}, far more than the "
            f"publisher's own {diskspace.human_size(publisher_bytes)}. Please send a Support "
            "report so we can fix it."
        )
    return text, ""


def _says_auto_wayland(option: Option) -> bool:
    return option.key == "display" and ("Auto (Wayland)", "auto") in (option.choices or ())


def _x11_display_option(option: Option) -> Option:
    return dataclasses.replace(
        option,
        help="X11 (XWayland) is the default for this game: on native Wayland its windows "
        "misbehave (a sign-in window on another screen, or a blank window). Wayland is there "
        "to try.",
        choices=(("Auto (X11)", "auto"), ("Wayland", "wayland"), ("X11 (XWayland)", "x11")),
    )


def game_subdir(name: str) -> Path:
    return Path("drive_c/Games") / name


__all__ = ["DirectBackend"]

# Per-game backends live in separate modules (#1601). These names stay
# importable from here: each of those modules calls publish_game_exports when it has
# finished, so the names are set whichever module is imported first.
_GAME_EXPORTS = {
    name: "direct_games"
    for name in (
        "DNA_ACCOUNT_NOTICE",
        "AetherGazerBackend",
        "BrownDust2Backend",
        "DnaBackend",
        "EndfieldBackend",
        "GenshinBackend",
        "Gfl2Backend",
        "Gfl2HaoplayBackend",
        "Hi3Backend",
        "MongilBackend",
        "NteDirectBackend",
        "PgrBackend",
        "SnowbreakBackend",
        "StarRailBackend",
        "ZenlessBackend",
    )
} | {
    "WutheringWavesBackend": "direct_wuwa",
    "TofBackend": "direct_tof",
    "R1999Backend": "direct_r1999",
    "StrinovaBackend": "direct_strinova",
    "BpsrBackend": "direct_bpsr",
}


def publish_game_exports(module_name: str, names: Mapping[str, Any]) -> None:
    home = module_name.rpartition(".")[2]
    for name, where in _GAME_EXPORTS.items():
        if where == home:
            globals()[name] = names[name]


# Last: the game modules import the base and its helpers from this module.
from . import (  # noqa: E402, F401
    direct_bpsr,
    direct_games,
    direct_r1999,
    direct_strinova,
    direct_tof,
    direct_wuwa,
)
