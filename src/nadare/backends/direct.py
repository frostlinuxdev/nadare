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

import contextlib
import dataclasses
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
    endfield_shim,
    graphics,
    kuropatch,
    monitors,
    nte_launcher,
    paths,
    process,
    removal,
    webview2,
)
from ..core.backends import PREFIX_HELP, BackendError, Option, Status
from ..core.events import Cancelled
from ..core.gameversion import versions_equal
from ..core.sources import (
    Fetch,
    aethergazer,
    bd2,
    endfield,
    gfl2,
    hoyo,
    mongil,
    nte,
    patch_list_fetch,
    pgr,
    wuwa,
)
from . import (
    bd2_news,
    endfield_news,
    gfl2_news,
    hoyo_art,
    hoyo_news,
    kuro_news,
    mongil_news,
    nte_news,
)
from .arknights import INSTALLED_PREFIX_KEY
from .nmlogin import NetmarbleLoginMixin
from .wine import profile_setting, scan_pids, stream
from .winegame import UPDATE_CHECK_INTERVAL, WineGameBackend, atomic_write_json

#: A version that may name a predownload cache folder ("3.4.0", "3.8.0+pack-3.8.1"):
#: no separators or leading dot. ``+`` is a valid filename character and keeps the name
#: exactly as the version record stores it, so a Kuro tier plan and its cache agree (#W10-F5).
_SAFE_VERSION = re.compile(r"[0-9A-Za-z][0-9A-Za-z._+-]{0,31}")

#: Sizes below are decimal gigabytes, as the publishers and archive tables report them.
GB = 10**9

#: Written into a Kuro cleanup mark only when this release completed the cleanup. b23 wrote an
#: empty mark even when it gave up, so an empty (or older) mark must not mean "done" (#1252).
TIDY_MARK_GENERATION = "2\n"

log = logging.getLogger(__name__)


def _base_version_key(version: str) -> tuple[int, ...]:
    """Numbers of a version's base ("3.8.0+hd-3.8.1" -> (3, 8, 0)); text parts count as 0."""
    base = str(version).split("+", 1)[0]
    return tuple(int(part) if part.isdigit() else 0 for part in base.split("."))


class DirectBackend(WineGameBackend):
    """Base for the per-game classes below; never registered on its own."""

    def _before_sync(self) -> None:
        """Hook run first in every Install/Update/Repair sync (WuWa records its tiers)."""

    def _tidy_allowed(self) -> bool:
        """Whether the retired-files tidy may run now (WuWa: not while a tier is pending)."""
        return True

    def _tidy_names(self, names: Iterable[str]) -> list[str]:
        """The retired files the tidy may delete (WuWa keeps other tiers' folders out)."""
        return list(names)

    #: umu game id (``GAMEID``/``UMU_ID``) for the runner's per-game fixes; ``None`` sends none
    #: (umu's default). A profile ``extra_env`` GAMEID/UMU_ID always wins (#1334).
    umu_id: str | None = None

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
        if getattr(cls, "default_display", "wayland") == "x11":
            cls.options = tuple(
                _x11_display_option(o) if o.key == "display" else o for o in cls.options
            )

    kind = "direct"
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
            "Parallel downloads (1-16).",
            default=6,
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
        Option("extra_env", "Environment", "env", "One KEY=VALUE per line.", advanced=True),
        monitors.OPTION,
        Option("process_match", "Process match", "text", advanced=True),
    )

    #: ``Plan`` factory for this game: ``(fetch) -> Plan``.
    plan_factory: Callable[[Fetch | None], directdl.Plan]
    #: The next-version plan when the publisher offers a predownload window, else ``None``.
    predownload_factory: Callable[..., directdl.Plan | None] | None = None

    def plan_kwargs(self) -> dict[str, Any]:
        """Extra keyword arguments for ``plan_factory`` (default: none)."""
        return {}

    def plan(self) -> directdl.Plan:
        try:
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
        return {
            "version": version,
            "checked_at": int(checked),
            # Only JSON true means ready: a hand-edited string "false" is truthy to bool().
            "ready": payload.get("ready") is True,
        }

    def _write_predownload_state(
        self, version: str | None, ready: bool, checked_at: int | None = None
    ) -> None:
        atomic_write_json(
            self._predownload_state_path(),
            {
                "version": version,
                "checked_at": int(time.time()) if checked_at is None else checked_at,
                "ready": bool(ready),
            },
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
        if old is not None:
            try:
                age = time.time() - float(old.get("checked_at", 0))
            except (TypeError, ValueError):
                age = UPDATE_CHECK_INTERVAL
            if 0 <= age < UPDATE_CHECK_INTERVAL:
                return
        try:
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
                same = old is not None and old.get("version") == str(plan.version)
                self._write_predownload_state(
                    str(plan.version), bool(same and old and old.get("ready"))
                )
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
            raise BackendError(f"{self.display_name} is not installed")
        try:
            plan = type(self).predownload_factory(None, **self.plan_kwargs())
        except directdl.DirectError as exc:
            raise BackendError(f"{self.display_name}: {exc}") from exc
        if plan is None or str(plan.version) == str(record["version"]):
            raise BackendError(
                f"{self.display_name}: no predownload is offered right now"
            )
        cache = self._predownload_cache(plan.version)
        self._remove_other_predownload_caches(cache)
        workers = int(
            self.ctx.profile.get("download_workers") or directdl.plan_workers(plan)
        )
        files = None
        if (
            not plan.sophon
            and isinstance(plan.extra, Mapping)
            and kuropatch.EXTRA_KEY in plan.extra
        ):
            try:
                patches = kuropatch.prepare(plan, str(record["version"]), patch_list_fetch)
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
        self._write_predownload_state(str(plan.version), True)
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
            additions["predownload_ready"] = bool(state and state.get("ready"))
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
        workers = int(
            self.ctx.profile.get("download_workers") or directdl.plan_workers(plan)
        )
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
            fetched = [0]

            def counted(event: Event) -> None:
                # The sync's overall download counter, for the update receipt (#1252).
                data = event.data if isinstance(event.data, dict) else {}
                if event.kind == "progress" and data.get("stage") == "download":
                    fetched[0] = max(fetched[0], int(data.get("done") or 0))
                self.ctx.emit(event)

            if chunk_cache is not None:
                summary = directdl.sync(
                    plan,
                    self.install_dir(),
                    emit=counted,
                    cancel=self.ctx.cancel,
                    workers=workers,
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
        self._remove_retired(plan, retired)
        if update is not None and update[0] and update[0] != update[1]:
            text, too_big = update_receipt(
                update[0], str(plan.version),
                patch_bytes=int(self._patch_numbers.get("patch_bytes", 0)),
                whole_bytes=fetched[0],
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
            patches = kuropatch.prepare(plan, installed, patch_list_fetch)
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
                patches, plan, self.install_dir(),
                emit=self.ctx.emit, cancel=self.ctx.cancel, changed=changed, cache=cache,
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

    def _game_running(self) -> bool:
        return bool(scan_pids(self._patterns()) or self._running_elsewhere())

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
            scan = kuropatch.retired_scan(plan, self.install_dir(), patch_list_fetch)
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
            scan = kuropatch.retired_scan(plan, self.install_dir(), patch_list_fetch)
            names = self._tidy_names(scan.names)
            if names:
                removed = kuropatch.delete_retired(self.install_dir(), names)
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
            scan = kuropatch.retired_scan(plan, self.install_dir(), patch_list_fetch)
            names.update(scan.names)
            names = set(self._tidy_names(names))
        except Cancelled:
            raise
        except Exception as exc:  # noqa: BLE001 - a cleanup problem is only logged
            self.info(f"{self.display_name}: could not check for left-over files ({exc})")
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

    # ---- lifecycle (same shape as Stella Sora's) --------------------------
    def install(self) -> None:
        self.ctx.cancel.raise_if_cancelled()
        self.ctx.set(INSTALLED_PREFIX_KEY, str(self.prefix()))
        self._ensure_default_runner(strict=False)
        self._ensure_direct_prefix()
        self._sync_direct()

    def update(self) -> None:
        self.ctx.cancel.raise_if_cancelled()
        target = self.direct_target()
        if target is None or not target.is_file():
            raise BackendError(f"{self.display_name} is not installed")
        self._sync_direct()

    def launch(self) -> int:
        self.ctx.cancel.raise_if_cancelled()
        target = self.direct_target()
        if target is None or not target.is_file():
            raise BackendError(f"game executable not found: {target}")
        self._ensure_default_runner(strict=True)
        return self._launch_direct()


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


def _x11_display_option(option: Option) -> Option:
    return dataclasses.replace(
        option,
        help="X11 (XWayland) is the default for this game: on native Wayland its windows "
        "misbehave (a sign-in window on another screen, or a blank window). Wayland is there "
        "to try.",
        choices=(("Auto (X11)", "auto"), ("Wayland", "wayland"), ("X11 (XWayland)", "x11")),
    )


def _subdir(name: str) -> Path:
    return Path("drive_c/Games") / name


class BrownDust2Backend(DirectBackend):
    kind = "direct-bd2"
    news_source = staticmethod(bd2_news.fetch_bd2_news)
    official_image_hosts = bd2_news.IMAGE_HOSTS
    game_tag = "bd2"
    display_name = "Brown Dust 2"
    install_subdir = _subdir("BrownDust2")
    direct_exe = "BrownDust II.exe"
    launcher_names = (direct_exe,)
    install_size_hint = int(1.4 * GB)
    plan_factory = staticmethod(bd2.resolve)


class AetherGazerBackend(DirectBackend):
    kind = "direct-aethergazer"
    game_tag = "aethergazer"
    display_name = "Aether Gazer"
    install_subdir = _subdir("AetherGazer")
    direct_exe = "AetherGazer.exe"
    launcher_names = (direct_exe,)
    install_size_hint = int(10.4 * GB)
    plan_factory = staticmethod(aethergazer.resolve)


class MongilBackend(NetmarbleLoginMixin, DirectBackend):
    kind = "direct-mongil"
    news_source = staticmethod(mongil_news.fetch_mongil_news)
    official_image_hosts = mongil_news.IMAGE_HOSTS
    vcredist_default = True
    game_tag = "mongil"
    display_name = "Mongil: Star Dive"
    install_subdir = _subdir("MongilStarDive")
    direct_exe = "STARDIVE.exe"
    launcher_names = (direct_exe,)
    install_size_hint = int(1.6 * GB)
    plan_factory = staticmethod(mongil.resolve)
    page_summary = (
        "Log in opens Netmarble's sign-in page in your browser (no password goes through "
        "Nadare); Play then hands the game its token."
    )
    page_notice = (
        "Mongil: Star Dive takes its login from Netmarble's launcher. Nadare does that step "
        "itself: Log in opens Netmarble's own sign-in page in your browser (no password "
        "goes through Nadare), then launch hands the game its token.",
        "https://stardive.netmarble.com/",
        "Official site",
    )


class WutheringWavesBackend(DirectBackend):
    kind = "direct-wuwa"
    #: The runner's WuWa fixes (gamefixes-umu/umu-3513350.py: fonts, no SteamOS/SteamDeck,
    #: the bitmap flag that ends 3.8's white web pages, one ``-krqlv``) only run under this id;
    #: with ``0`` they never did (#1334). A profile ``extra_env`` GAMEID still wins.
    umu_id = "umu-3513350"
    news_source = staticmethod(kuro_news.fetch_wuwa_news)
    official_image_hosts = kuro_news.IMAGE_HOSTS
    game_tag = "wuwa"
    display_name = "Wuthering Waves"
    tidies_retired = True
    #: On native Wayland a player's sign-in window opened on another screen; XWayland keeps the
    #: game's windows together (2026-11-12). ``display: wayland`` still wins.
    default_display = "x11"
    install_subdir = _subdir("WutheringWaves")
    direct_exe = "Wuthering Waves.exe"
    launcher_names = (direct_exe,)
    install_size_hint = int(85.9 * GB)
    plan_factory = staticmethod(wuwa.resolve)
    predownload_factory = staticmethod(wuwa.resolve_predownload)
    # Asked in the install confirm (#990), with the sizes from the option's help.
    install_choice = "wuwa_quality"
    install_choice_sizes = {"sd": 58 * 2**30, "hd": 80 * 2**30, "uhd": 99 * 2**30}
    options = DirectBackend.options + (
        Option(
            "wuwa_quality",
            "Resource quality",
            "choice",
            "The size of the game's textures, as in Kuro's launcher: SD about 58 GiB, HD about "
            "80 GiB, UHD about 99 GiB. Changing it downloads the missing files on the next "
            "Update; the folder of the old quality stays until you delete it.",
            default="hd",
            choices=(("SD", "sd"), ("HD", "hd"), ("UHD", "uhd")),
        ),
        Option(
            "kuro_token",
            "Kuro download token",
            "text",
            "Only needed if Kuro rotates the token Nadare ships "
            "(see core/sources/wuwa.py).",
            advanced=True,
        ),
    )

    def _latest_variant(self) -> str:
        # Each quality (and a custom download token) has its own published version (#975); the
        # default HD with the shipped token keeps the plain cache name.
        quality = wuwa.quality_of(profile_setting(self.ctx.profile, "wuwa_quality"))
        parts = [] if quality == wuwa.DEFAULT_QUALITY else [quality]
        if str(self.ctx.profile.get("kuro_token") or "").strip():
            parts.append("token")  # never the token itself
        return "-".join(parts)

    def plan_kwargs(self) -> dict[str, Any]:
        quality = wuwa.quality_of(profile_setting(self.ctx.profile, "wuwa_quality"))
        kwargs: dict[str, Any] = {"quality": quality}
        token = str(self.ctx.profile.get("kuro_token") or "").strip()
        if token:
            kwargs["token"] = token
        return kwargs

    def _quality_folder(self, quality: str) -> Path:
        return self.install_dir() / "Client" / "Content" / quality.upper()

    def other_quality_folders(self) -> list[tuple[str, Path, int]]:
        """The resource tiers this profile no longer uses, with their sizes (#990).

        Each tier is its own folder under ``Client/Content``. Nothing is offered until the
        chosen tier has files: before its update downloads them, the old tier is still the
        one the game plays. Symlinked folders are never followed.
        """
        chosen = wuwa.quality_of(profile_setting(self.ctx.profile, "wuwa_quality"))
        chosen_folder = self._quality_folder(chosen)

        root = self.install_dir()

        def safe(folder: Path) -> bool:
            """A real folder inside the install dir with no symlink below the install dir.

            Links above it are fine (``/home`` is a link to ``/var/home`` on Silverblue/Bazzite).
            """
            try:
                inside = folder.relative_to(root)
                return folder.resolve() == root.resolve() / inside
            except (OSError, ValueError):
                return False

        if not safe(chosen_folder) or not chosen_folder.is_dir():
            return []
        if self._install_marker_path().exists() or chosen not in self._complete_tiers():
            # Empty or partial (a cancelled Update): the other tier is still the one that plays,
            # so it must never be offered for deletion (#1335).
            return []
        found: list[tuple[str, Path, int]] = []
        for quality in wuwa.QUALITIES:
            if quality == chosen:
                continue
            folder = self._quality_folder(quality)
            if not safe(folder) or not folder.is_dir():
                continue
            size = diskspace.tree_bytes(folder)
            if size <= 0:
                continue  # nothing to free; a "0 B no longer needed" prompt helps no one
            found.append((quality, folder, size))
        return found

    def remove_other_qualities(
        self, only: Iterable[str] | None = None
    ) -> tuple[int, list[str]]:
        """Delete the unused resource tiers, never while the game runs (#990).

        ``only`` restricts the deletion to the tiers the user confirmed; each is still
        re-checked through :meth:`other_quality_folders` (symlinks and the chosen tier).
        """
        self._refuse_if_running("removing files")
        wanted = None if only is None else {str(name).lower() for name in only}
        freed = 0
        warnings: list[str] = []
        removed: list[str] = []
        for quality, folder, size in self.other_quality_folders():
            if wanted is not None and quality not in wanted:
                continue
            try:
                outcome = removal._rmtree_safely(folder)
            except OSError as exc:
                warnings.append(removal._leftover_warning(folder, exc))
                continue
            for mount in outcome.mounts:
                warnings.append(
                    f"{mount} is a separate mounted drive and was left alone"
                )
            for path, exc in outcome.leftovers:
                warnings.append(removal._leftover_warning(path, exc))
            if not folder.exists():
                freed += size
                removed.append(quality)
        recorded = self._recorded_tiers()
        if removed and recorded is not None:
            try:
                self._write_recorded_tiers(
                    {q: v for q, v in recorded.items() if q not in removed}
                )
            except OSError as exc:
                log.debug("%s: could not update the tier record: %s", self.display_name, exc)
        return freed, warnings

    #: The game binary. ``Wuthering Waves.exe`` (``direct_exe``) is Kuro's launcher stub: it
    #: runs ``ACE-Setup64.exe``, which installs the ACE kernel driver into the prefix, and Wine
    #: aborts on the kernel functions that driver calls. Starting the game binary itself
    #: reaches the game without that step (Twintail Launcher starts it the same way).
    GAME_EXE = "Client/Binaries/Win64/Client-Win64-Shipping.exe"

    def direct_target(self) -> Path | None:
        if profile_setting(self.ctx.profile, "exe_name") or profile_setting(
            self.ctx.profile, "launch_exe"
        ):
            return super().direct_target()
        return self.install_dir() / self.GAME_EXE

    def launch_extra_args(self) -> list[str]:
        # The launcher stub passes these; a profile that sets its own launch_args keeps control.
        if self._extra_args("launch_args"):
            return []
        return ["-dx12", f"-krqlv={self._launch_quality()}"]

    def _tier_installed(self, quality: str) -> bool:
        """Whether ``Client/Content/<TIER>`` holds at least one non-empty file."""
        folder = self._quality_folder(quality)
        try:
            for _dirpath, _dirs, files in os.walk(folder):
                for name in files:
                    if name.endswith(".part") or ".nadare-new" in name:
                        continue  # a download still in progress or cancelled (stress F1)
                    with contextlib.suppress(OSError):
                        if (Path(_dirpath) / name).stat().st_size > 0:
                            return True
        except OSError:
            return False
        return False

    def _recorded_tiers(self) -> dict[str, str] | None:
        """``{tier: version}`` a finished sync recorded (``qualities``); ``None`` when unusable.

        Each tier keeps the game version it was last synced at: an update downloads only the
        chosen tier, so the others stay at their own version (stress round 2, r3 F2). A list
        (the first #1335 build) means every listed tier is at the recorded version. A record
        with no known tier is treated like an old version file (r1: it switched off both the
        pending update and the Repair refusal).
        """
        record = self._read_version_file()
        if not isinstance(record, dict):
            return None
        listed = record.get("qualities")
        current = str(record.get("version") or "")
        found: dict[str, str] = {}
        if isinstance(listed, list):
            names = {str(x).strip().lower() for x in listed}
            found = {q: current for q in wuwa.QUALITIES if q in names}
        elif isinstance(listed, dict):
            lowered = {str(k).strip().lower(): str(v) for k, v in listed.items()}
            found = {q: lowered[q] for q in wuwa.QUALITIES if q in lowered}
        return found or None

    def _complete_tiers(self) -> list[str]:
        """Tiers fully installed at the current game version, and still on disk.

        A version file from before #1335 has no record; then every tier with real files counts,
        as #1333 did, except the chosen one while a sync is unfinished (it may be the partial
        one). A tier only partly downloaded is never recorded: the record is written from the
        disk before the first sync starts (:meth:`_before_sync`), and a sync adds only its own
        tier.
        """
        recorded = self._recorded_tiers()
        if recorded is None:
            candidates = list(wuwa.QUALITIES)
            if self._install_marker_path().exists():
                chosen = wuwa.quality_of(profile_setting(self.ctx.profile, "wuwa_quality"))
                candidates.remove(chosen)
        else:
            record = self._read_version_file() or {}
            current = str(record.get("version") or "")
            candidates = [
                q for q in wuwa.QUALITIES
                if q in recorded and versions_equal(recorded[q], current)
            ]
        return [q for q in candidates if self._tier_installed(q)]

    def _write_recorded_tiers(self, tiers: Mapping[str, str]) -> None:
        path = self._version_path()
        try:
            data = json.loads(path.read_text(encoding="utf-8"))
        except (OSError, ValueError):
            return
        if not isinstance(data, dict):
            return
        data["qualities"] = {q: str(tiers[q]) for q in wuwa.QUALITIES if q in tiers}
        atomic_write_json(path, data)

    def _before_sync(self) -> None:
        """Turn an old version file's disk guess into a record before anything downloads.

        Otherwise a tier download cancelled half way would be on disk when the next sync
        finishes, and the disk guess would record it as complete (stress round 2, r2).
        """
        if self._recorded_tiers() is not None:
            return
        record = self._read_version_file()
        version = str(record.get("version") or "") if isinstance(record, dict) else ""
        if not version:
            return  # a first install: nothing was complete before it
        try:
            self._write_recorded_tiers({q: version for q in self._complete_tiers()})
        except OSError as exc:
            log.debug("%s: could not record the tiers: %s", self.display_name, exc)

    def _record_synced_version(self, manifest: Any) -> None:
        """Record the tier this sync completed at its version; other tiers keep theirs."""
        before = dict(self._recorded_tiers() or {})  # read before the base class rewrites it
        super()._record_synced_version(manifest)
        chosen = wuwa.quality_of(profile_setting(self.ctx.profile, "wuwa_quality"))
        if self._tier_installed(chosen):  # r1: never a tier the sync left without files
            before[chosen] = str(manifest.version)
        try:
            self._write_recorded_tiers(before)
        except OSError as exc:
            log.debug("%s: could not record the tier: %s", self.display_name, exc)

    def _tidy_allowed(self) -> bool:
        """No Kuro tidy while the chosen tier is pending: its plan would call the tier that
        plays "retired" (stress round 2, r3 F3)."""
        complete = self._complete_tiers()
        chosen = wuwa.quality_of(profile_setting(self.ctx.profile, "wuwa_quality"))
        return chosen in complete or not complete

    def _tidy_names(self, names: Iterable[str]) -> list[str]:
        """Never tidy inside another tier's folder; that is the cleanup offer's job (#990)."""
        chosen = wuwa.quality_of(profile_setting(self.ctx.profile, "wuwa_quality"))
        others = tuple(
            f"client/content/{q}/" for q in wuwa.QUALITIES if q != chosen
        )
        return [
            n for n in names
            if not str(n).replace("\\", "/").lstrip("/").lower().startswith(others)
        ]

    def _direct_status_parts(
        self, target: Path | None, incomplete: bool
    ) -> tuple[dict[str, Any], str | None, Status | None]:
        additions, version, early = super()._direct_status_parts(target, incomplete)
        if not version:
            return additions, version, early
        chosen = wuwa.quality_of(profile_setting(self.ctx.profile, "wuwa_quality"))
        if early is not None:
            interrupted = self._interrupted_update() if incomplete else None
            if (
                interrupted is not None
                and versions_equal(str(interrupted[0]), str(interrupted[1]))
                and chosen not in self._complete_tiers()
            ):
                # Same version before and after: it was a quality download, not a game update
                # (stress round 2, r2: the page said "update to 3.8.0 was interrupted").
                additions["pending_quality"] = chosen
                early = dataclasses.replace(
                    early,
                    message=(
                        f"downloading the {chosen.upper()} files was interrupted — click "
                        "RESUME to finish it; only what is missing is downloaded"
                    ),
                )
            return additions, version, early
        complete = self._complete_tiers()
        if chosen not in complete and (complete or self._recorded_tiers()):
            # A switched quality is an update: the version is the same for every tier, so the
            # version check alone said "up to date" and the tier was never downloaded (#1335).
            # Name the setting: a profile set to SD by the old Settings bug (#1332) can be set
            # back instead of downloading a tier the player never chose.
            additions["update_available"] = True
            additions["pending_quality"] = chosen
            additions["latest_version"] = (
                f"{chosen.upper()} files (Resource quality in Settings)"
            )
        return additions, version, early

    def _launch_quality(self) -> str:
        """The tier to start: the chosen one, or an installed one until Update brings it.

        ``-krqlv`` naming a tier whose files were never downloaded crashes the client ("Fatal
        error", 2026-11-14). The saved choice is left alone, so Update still downloads it.
        With no tier folder at all there is nothing to compare, so the choice stands.
        """
        chosen = wuwa.quality_of(profile_setting(self.ctx.profile, "wuwa_quality"))
        complete = self._complete_tiers()
        if chosen in complete:
            return chosen
        if not complete:
            if self._recorded_tiers():
                # Tiers were installed and are all gone (deleted by hand or a cleanup tool):
                # starting would crash on files that are not there (stress W2).
                raise BackendError(
                    f"The {chosen.upper()} resource files are missing from the game folder. "
                    "Press Repair to download them again."
                )
            return chosen  # an older record with no tier folders: nothing to compare
        # HD first (the default and the usual install), then the others in list order.
        order = [wuwa.DEFAULT_QUALITY] + [q for q in wuwa.QUALITIES if q != wuwa.DEFAULT_QUALITY]
        installed = next(q for q in order if q in complete)
        from ..core.events import Event

        note = (
            f"{chosen.upper()} files are not downloaded yet, so the game starts with "
            f"{installed.upper()}. Press Update to download {chosen.upper()}."
        )
        log.info("WuWa: %s", note)
        self.ctx.emit(Event("step", f"{self.display_name}: {note}"))
        return installed


class Gfl2Backend(DirectBackend):
    kind = "direct-gfl2"
    news_source = staticmethod(gfl2_news.fetch_gfl2_news)
    official_image_hosts = gfl2_news.IMAGE_HOSTS
    game_tag = "gfl2"
    display_name = "Girls' Frontline 2: Exilium (Darkwinter build)"
    install_subdir = _subdir("GF2Exilium")
    direct_exe = "GF2_Exilium.exe"
    launcher_names = (direct_exe,)
    install_size_hint = int(88.2 * GB)
    plan_factory = staticmethod(gfl2.resolve)
    page_notice = (
        "This is the Darkwinter build, downloaded from Sunborn's CDN. Girls' Frontline 2 also "
        "has a separate Haoplay global build, listed on its own; the two are different "
        "installs, so pick the one you play.",
        "https://store.steampowered.com/app/3347400/",
        "Steam page",
    )


class Gfl2HaoplayBackend(Gfl2Backend):
    """The Haoplay global build: same client layout, downloaded from Haoplay's CDN."""

    kind = "direct-gfl2-haoplay"
    # Sunborn's feed is the Darkwinter servers' (its maintenance times may not match);
    # Haoplay's site has only fixed banner images, no feed (#535, checked 2026-11-08).
    news_source = None
    official_image_hosts = DirectBackend.official_image_hosts
    game_tag = "gfl2-haoplay"
    display_name = "Girls' Frontline 2 (Haoplay global build)"
    install_subdir = _subdir("GF2Haoplay")
    install_size_hint = int(1 * GB)  # the small client; the game downloads ~88 GB itself
    plan_factory = staticmethod(gfl2.resolve_haoplay)
    #: The game only starts under umu's id 0 (``wine start /wait /unix``); a named id or a Steam
    #: app id makes Proton run it through steam.exe and it hangs. A profile ``extra_env`` wins.
    umu_id = "0"

    def _ensure_webview2(self) -> None:
        # Share CZN's pinned Microsoft download, cache verification and silent
        # installer, while using the exact prefix and runner selected here.
        from .czn import backend as czn_backend

        profile = self.ctx.profile
        prefix = self.prefix()
        constant = czn_backend.WEBVIEW2_SHA256

        def run_in_prefix(args: list[str]) -> int:
            cmd, env = self._prefix_command(args, process.installer_env())
            return stream(
                cmd, env, on_line=self._on_line, cancel=self.ctx.cancel, cwd=prefix
            )

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

    def install(self) -> None:
        self.ctx.cancel.raise_if_cancelled()
        self.ctx.set(INSTALLED_PREFIX_KEY, str(self.prefix()))
        self._ensure_default_runner(strict=False)
        self._ensure_direct_prefix()
        self._ensure_webview2()
        self._sync_direct()

    def launch(self) -> int:
        self.ctx.cancel.raise_if_cancelled()
        target = self.direct_target()
        if target is None or not target.is_file():
            raise BackendError(f"game executable not found: {target}")
        self._ensure_default_runner(strict=True)
        self._ensure_webview2()
        return self._launch_direct()

    def launch_extra_env(self) -> dict[str, str]:
        configured = profile_setting(self.ctx.profile, "extra_env")
        browser_args = (
            configured.get("WEBVIEW2_ADDITIONAL_BROWSER_ARGUMENTS")
            if isinstance(configured, dict)
            else None
        )
        return {"WEBVIEW2_ADDITIONAL_BROWSER_ARGUMENTS": str(browser_args or "--no-sandbox")}

    page_summary = (
        "The game downloads and updates itself on first start (about 90 GB). If the sign-in "
        "page misbehaves, try the other Display backend in Settings."
    )
    page_notice = (
        "This is the Haoplay global build; the Darkwinter build is a separate listing. Nadare "
        "installs only the small Haoplay client straight from Haoplay's servers "
        "(no launcher); the game downloads and updates itself on first start, about 90 GB "
        "with one voice language (more with others). "
        "Nadare installs Microsoft's WebView2 Runtime into this game's prefix for sign-in. "
        "If the sign-in page misbehaves, try the other Display backend in Settings.",
        "https://gf2.haoplay.com/",
        "Official site",
    )


class EndfieldBackend(DirectBackend):
    kind = "direct-endfield"
    news_source = staticmethod(endfield_news.fetch_endfield_news)
    official_image_hosts = endfield_news.IMAGE_HOSTS
    game_tag = "endfield"
    display_name = "Arknights: Endfield"
    install_subdir = _subdir("ArknightsEndfield")
    direct_exe = "Endfield.exe"
    launcher_names = (direct_exe,)
    #: Streamed from the server: no archive on disk, only the 68 GB game (62 GB download).
    install_size_hint = int(68.3 * GB)
    plan_factory = staticmethod(endfield.resolve)
    page_notice = (
        "Nadare downloads Arknights: Endfield (62 GB, 68 GB installed) straight from the "
        "server. The game only starts when Hypergryph's launcher is its parent process, so "
        "Nadare starts it through a small bundled helper (source in the repository, built with "
        "mingw-w64) that sets the launcher's Games.exe as the parent; the launcher itself "
        "never runs. Steam installs start directly.",
        "https://endfield.gryphline.com/",
        "Official site",
    )

    def _direct_argv(self, target: Path) -> list[str]:
        argv = super()._direct_argv(target)
        if endfield_shim.is_steam_build(target.parent):
            return argv
        prefix = self.prefix()
        try:
            files = endfield_shim.ensure_files(prefix, data_dir=self._data_dir())
        except endfield_shim.ShimError as exc:
            raise BackendError(f"cannot prepare the Endfield launch helper: {exc}") from exc
        self.info("Endfield: starting through the launcher helper (Games.exe as parent)")
        return endfield_shim.launch_argv(
            str(files.shim),
            endfield_shim.win_path(prefix, files.games),
            endfield_shim.win_path(prefix, target),
            argv[1:],
        )


class NteDirectBackend(DirectBackend):
    kind = "direct-nte"
    news_source = staticmethod(nte_news.fetch_nte_news)
    official_image_hosts = nte_news.IMAGE_HOSTS
    game_tag = "nte"
    display_name = "Neverness to Everness"
    install_subdir = _subdir("NevernessToEverness")
    direct_exe = nte.EXE
    launcher_names = (direct_exe,)
    install_size_hint = int(98.0 * GB)
    plan_factory = staticmethod(nte.resolve)
    #: The launcher's embedded Chromium (login, news) and the game's login window draw blank
    #: under native Wayland on KDE; XWayland renders them. ``display: wayland`` still wins.
    default_display = "x11"
    page_summary = (
        "Perfect World's launcher starts with the game as a small bar and starts it by "
        "itself; that is expected."
    )
    page_notice = (
        "Installs straight from Perfect World's servers (about 98 GB). The game's Start and "
        "login only work when Perfect World's launcher is running, so Nadare also fetches the "
        "launcher files (7-Zip needed) and starts it with /autoplay: the launcher collapses to "
        "a small bar and starts the game by itself, like Steam. The very first start may show "
        "the launcher's Update button once, to register the install.",
        "https://nte.perfectworld.com/",
        "Official site",
    )

    def _direct_argv(self, target: Path) -> list[str]:
        try:
            launcher = nte_launcher.ensure_launcher(
                self.install_dir(), data_dir=self._data_dir()
            )
        except nte_launcher.NteLauncherError as exc:
            raise BackendError(f"cannot prepare the NTE launcher: {exc}") from exc
        self.info("NTE: starting the publisher launcher (/autoplay starts the game)")
        return [str(launcher), "/launcher", "/autoplay", *super()._direct_argv(target)[1:]]


class GenshinBackend(DirectBackend):
    kind = "direct-genshin"
    game_tag = "genshin"
    display_name = "Genshin Impact"
    install_subdir = _subdir("GenshinImpact")
    direct_exe = "GenshinImpact.exe"
    launcher_names = (direct_exe,)
    install_size_hint = int(100.8 * GB)
    news_source = staticmethod(hoyo_news.fetch_genshin_news)
    official_image_hosts = hoyo_news.IMAGE_HOSTS
    art_source = staticmethod(hoyo_art.fetch_genshin_art)
    art_image_hosts = hoyo_art.IMAGE_HOSTS
    plan_factory = staticmethod(lambda fetch=None: hoyo.resolve("hk4e_global", fetch))
    predownload_factory = staticmethod(
        lambda fetch=None: hoyo.resolve_predownload("hk4e_global", fetch)
    )


class PgrBackend(DirectBackend):
    kind = "direct-pgr"
    news_source = staticmethod(kuro_news.fetch_pgr_news)
    official_image_hosts = kuro_news.IMAGE_HOSTS
    game_tag = "pgr"
    display_name = "Punishing: Gray Raven"
    tidies_retired = True
    install_subdir = _subdir("PunishingGrayRaven")
    direct_exe = "PGR.exe"
    launcher_names = (direct_exe,)
    install_size_hint = int(71.7 * GB)
    plan_factory = staticmethod(pgr.resolve)
    predownload_factory = staticmethod(pgr.resolve_predownload)
    #: Native Wayland gave a white window in testing; the X11 path reached the game's own
    #: agreement screen. Not yet re-confirmed through Nadare with display set to x11.
    default_display = "x11"
    options = DirectBackend.options + (
        Option(
            "kuro_token",
            "Kuro download token",
            "text",
            "Only needed if Kuro rotates the token Nadare ships (see core/sources/pgr.py).",
            advanced=True,
        ),
    )
    page_notice = (
        "Nadare downloads Punishing: Gray Raven (71.7 GB) from Kuro's CDN using the token the "
        "official launcher uses. Pointing Game folder at an existing install (Steam, Heroic) "
        "checks every file against the index and downloads only what differs.",
        "https://pgr.kurogame.net/",
        "Official site",
    )

    def plan_kwargs(self) -> dict[str, Any]:
        token = str(self.ctx.profile.get("kuro_token") or "").strip()
        return {"token": token} if token else {}


class StarRailBackend(DirectBackend):
    kind = "direct-hsr"
    game_tag = "hsr"
    display_name = "Honkai: Star Rail"
    install_subdir = _subdir("HonkaiStarRail")
    direct_exe = "StarRail.exe"
    launcher_names = (direct_exe,)
    install_size_hint = int(107.6 * GB)
    news_source = staticmethod(hoyo_news.fetch_hsr_news)
    official_image_hosts = hoyo_news.IMAGE_HOSTS
    art_source = staticmethod(hoyo_art.fetch_hsr_art)
    art_image_hosts = hoyo_art.IMAGE_HOSTS
    plan_factory = staticmethod(lambda fetch=None: hoyo.resolve("hkrpg_global", fetch))
    predownload_factory = staticmethod(
        lambda fetch=None: hoyo.resolve_predownload("hkrpg_global", fetch)
    )


class Hi3Backend(DirectBackend):
    kind = "direct-hi3"
    game_tag = "hi3"
    display_name = "Honkai Impact 3rd"
    install_subdir = _subdir("HonkaiImpact3rd")
    direct_exe = "BH3.exe"
    launcher_names = (direct_exe,)
    install_size_hint = int(36.7 * GB)
    news_source = staticmethod(hoyo_news.fetch_hi3_news)
    official_image_hosts = hoyo_news.IMAGE_HOSTS
    art_source = staticmethod(hoyo_art.fetch_hi3_art)
    art_image_hosts = hoyo_art.IMAGE_HOSTS
    plan_factory = staticmethod(lambda fetch=None: hoyo.resolve("bh3_global", fetch))
    predownload_factory = staticmethod(
        lambda fetch=None, **kw: hoyo.resolve_predownload("bh3_global", fetch)
    )


class ZenlessBackend(DirectBackend):
    kind = "direct-zzz"
    game_tag = "zzz"
    display_name = "Zenless Zone Zero"
    install_subdir = _subdir("ZenlessZoneZero")
    direct_exe = "ZenlessZoneZero.exe"
    launcher_names = (direct_exe,)
    install_size_hint = int(85.9 * GB)
    news_source = staticmethod(hoyo_news.fetch_zzz_news)
    official_image_hosts = hoyo_news.IMAGE_HOSTS
    art_source = staticmethod(hoyo_art.fetch_zzz_art)
    art_image_hosts = hoyo_art.IMAGE_HOSTS
    plan_factory = staticmethod(lambda fetch=None: hoyo.resolve("nap_global", fetch))
    predownload_factory = staticmethod(
        lambda fetch=None: hoyo.resolve_predownload("nap_global", fetch)
    )


__all__ = ["DirectBackend"]
