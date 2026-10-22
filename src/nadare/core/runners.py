# SPDX-License-Identifier: GPL-3.0-or-later
# Copyright (C) 2026 Nadare contributors
"""Proton runner manager, ported from czn-linux ``tools/czn_runners.py``.

A *runner* is an extracted Proton tree containing ``proton`` (and usually
``files/bin/wine``). Nadare looks for runners in its own data dir and in the
places Steam and Heroic already keep them. Pinned releases downloaded from each
project's GitHub Releases are verified against a sha256 recorded after in-game
testing; ``--latest`` (untested) falls back to the published ``.sha512sum``.
"""

from __future__ import annotations

import fcntl
import os
import re
import shutil
import tarfile
import tempfile
import threading
import time
import uuid
from collections.abc import Mapping
from dataclasses import dataclass
from datetime import UTC, datetime, timedelta
from pathlib import Path
from typing import Any
from urllib.parse import urlparse

from . import diskspace, download, paths
from .events import Cancel, Emitter, Event, emitter

GITHUB_API = "https://api.github.com/repos/{repo}/releases"

#: GitHub-controlled hosts a release asset may be fetched from (L6).
_GITHUB_ASSET_HOSTS = (
    "github.com",
    "objects.githubusercontent.com",
    "codeload.github.com",
    "github-releases.githubusercontent.com",
    # GitHub's current release-asset CDN (browser_download_url redirects here).
    "release-assets.githubusercontent.com",
)

#: How long an upstream release lookup is trusted (the release API is slow).
RELEASE_CACHE_SECONDS = 6 * 60 * 60

#: A runner family is "stale" when upstream has not released for this long.
STALE_DAYS = 90


@dataclass(frozen=True)
class RunnerSpec:
    id: str
    name: str
    repo: str
    asset: str  # regex matching the x86_64 tarball
    tested: tuple[str, str] | None  # (tag, sha256) proven in-game, else None


#: Tested tags/sha256s come from czn-linux; ``--latest`` is the only option for
#: runners without a tested pin (its integrity rests on the published .sha512sum).
RUNNERS: dict[str, RunnerSpec] = {
    "cachyos": RunnerSpec(
        "cachyos",
        "Proton-CachyOS",
        "CachyOS/proton-cachyos",
        r"proton-cachyos-[A-Za-z0-9._-]+-x86_64\.tar\.xz",
        (
            "cachyos-11.0-20260703-slr",
            "62ff4b2750180723cc00538608fe687e21d1d91a31ef64ce1a7c9f46c3db310b",
        ),
    ),
    "dwproton": RunnerSpec(
        "dwproton",
        "DW-Proton",
        "dawn-winery/dwproton-mirror",
        r"dwproton-[A-Za-z0-9._-]+-x86_64\.tar\.xz",
        ("dwproton-11.0-15", "8066dec3d7f532d55e5b6d697a61d6d4a3091f859a1ef9b2943895aa1b947afd"),
    ),
    "ge": RunnerSpec(
        "ge",
        "GE-Proton",
        "GloriousEggroll/proton-ge-custom",
        r"GE-Proton[A-Za-z0-9._-]+-x86_64\.tar\.gz",
        ("GE-Proton11-7", "c5448b76a230384e2d7bc6beb5ccb97bafb7e2c3b6c527cb03a1a546bbcb00a0"),
    ),
}

#: Extra directories (besides Nadare's own) that may hold Proton builds.
STEAM_TOOL_DIRS = (
    "~/.local/share/Steam/compatibilitytools.d",
    "~/.steam/root/compatibilitytools.d",
    "~/.steam/steam/compatibilitytools.d",
    "/usr/share/steam/compatibilitytools.d",
    "/usr/lib/steam/compatibilitytools.d",
    "/usr/local/share/steam/compatibilitytools.d",
)
STEAM_COMMON_DIRS = (
    "~/.local/share/Steam/steamapps/common",
    "~/.steam/steam/steamapps/common",
    "~/.steam/root/steamapps/common",
)
SYSTEM_PROTON_DIRS = (
    "/usr/share/proton",
    "/opt/proton",
)


class RunnerError(RuntimeError):
    """A runner could not be listed, downloaded or resolved."""


# ---------------------------------------------------------------------------
# discovery
# ---------------------------------------------------------------------------
def _looks_like_runner(path: Path) -> bool:
    if not path.is_dir():
        return False
    if (path / "proton").is_file():
        return True
    return (path / "files" / "bin" / "wine").is_file()


def _expand(value: str) -> Path:
    return Path(os.path.expanduser(value))


def nadare_runners() -> list[Path]:
    root = paths.runners_dir()
    if not root.is_dir():
        return []
    # Skip our own hidden work trees (``.unpack-*`` staging and ``.<name>.old-*``
    # crash leftovers); a leftover must never be discovered or selected (NV-3).
    return sorted(
        p for p in root.iterdir() if not p.name.startswith(".") and _looks_like_runner(p)
    )


def steam_runners() -> list[Path]:
    """Runners installed under Steam's compatibilitytools.d / common dirs."""
    found: list[Path] = []
    for base in STEAM_TOOL_DIRS:
        d = _expand(base)
        if d.is_dir():
            found.extend(p for p in d.iterdir() if _looks_like_runner(p))
    for base in STEAM_COMMON_DIRS:
        d = _expand(base)
        if d.is_dir():
            found.extend(p for p in d.glob("Proton*") if _looks_like_runner(p))
    for base in SYSTEM_PROTON_DIRS:
        d = Path(base)
        if d.is_dir():
            found.extend(p for p in d.iterdir() if _looks_like_runner(p))
    return _unique(found)


def all_runners() -> list[Path]:
    """Every locally available runner, Nadare's own first, de-duplicated."""
    return _unique(nadare_runners() + steam_runners())


def _unique(paths_in: list[Path]) -> list[Path]:
    seen: set[str] = set()
    out: list[Path] = []
    for p in paths_in:
        key = str(p.resolve()) if p.exists() else str(p)
        if key not in seen:
            seen.add(key)
            out.append(p)
    return out


def runner_version(runner: Path) -> str:
    """``version`` file contents from a runner, else the directory name."""
    try:
        return (runner / "version").read_text(encoding="utf-8").strip() or runner.name
    except (OSError, ValueError):
        return runner.name


def proton_binary(runner: Path) -> Path:
    """The ``proton`` launcher inside a runner."""
    return runner / "proton"


def wine_binary(runner: Path) -> Path | None:
    """The ``wine`` binary inside a runner, when the layout has one."""
    for rel in ("files/bin/wine", "bin/wine"):
        candidate = runner / rel
        if candidate.is_file():
            return candidate
    return None


def _runner_rank(path: Path) -> tuple[int, tuple[int, ...], str]:
    """Sort key for ``latest``/``auto``: Nadare-installed, then newest version.

    ``all_runners()`` is path-sorted, so picking the last entry silently chose a
    lexicographically greatest (not newest) build.  Rank explicitly instead (L4).
    """
    own = 1 if path in nadare_runners() else 0
    numbers = tuple(int(part) for part in re.findall(r"\d+", runner_version(path)))
    return (own, numbers, path.name)


def resolve_runner(value: str | os.PathLike[str] | None) -> Path | None:
    """Resolve a profile ``runner`` value.

    Accepts an absolute/relative path, a directory name under
    ``paths.runners_dir()``, or ``"latest"``/``"auto"``. Returns ``None`` when
    the value is empty; raises :class:`RunnerError` when it cannot be found.
    """
    if value is None:
        return None
    text = str(value).strip()
    if not text:
        return None
    if text in RUNNERS:
        # A family id means that family's tested, pinned build.
        pinned = installed_pinned(text)
        if pinned is not None:
            return pinned
        tag = pinned_tag(text) or text
        raise RunnerError(f"the tested {RUNNERS[text].name} ({tag}) is not installed yet")
    if text in ("latest", "auto"):
        candidates = all_runners()
        if not candidates:
            raise RunnerError("no Proton runner installed; install one first")
        return max(candidates, key=_runner_rank)
    candidate = Path(text).expanduser()
    if not candidate.is_absolute():
        candidate = paths.runners_dir() / text
    if _looks_like_runner(candidate):
        return candidate
    available = ", ".join(p.name for p in all_runners()) or "none"
    raise RunnerError(f"runner {text!r} not found (installed: {available})")


def list_runners() -> list[dict[str, Any]]:
    """Metadata for the UI: Nadare-installed runners plus system/Steam ones."""
    own = {str(p.resolve()): p for p in nadare_runners() if p.exists()}
    rows: list[dict[str, Any]] = []
    for path in all_runners():
        key = str(path.resolve()) if path.exists() else str(path)
        rows.append(
            {
                "name": path.name,
                "path": str(path),
                "version": runner_version(path),
                "source": "nadare" if key in own else "system",
                "tested": False,
            }
        )
    return rows


# ---------------------------------------------------------------------------
# GitHub release resolution
# ---------------------------------------------------------------------------
def _release(spec: RunnerSpec, tag: str | None, *, timeout: int = 30) -> dict[str, Any]:
    url = GITHUB_API.format(repo=spec.repo) + (f"/tags/{tag}" if tag else "/latest")
    try:
        rel = download.fetch_json(
            url, headers={"Accept": "application/vnd.github+json"}, timeout=timeout
        )
    except Exception as exc:  # noqa: BLE001
        raise RunnerError(f"cannot query {spec.repo}: {exc}") from exc
    raw_assets = rel.get("assets", []) if isinstance(rel, dict) else None
    if not isinstance(raw_assets, list):
        raise RunnerError(f"unexpected release data from {spec.repo}")
    assets = {a.get("name"): a for a in raw_assets if isinstance(a, dict)}
    tar = next((a for name, a in assets.items() if name and re.fullmatch(spec.asset, name)), None)
    if tar is None:
        raise RunnerError(f"{spec.name} {rel.get('tag_name')} has no x86_64 tarball")
    # The asset name is network-controlled and becomes a path under cache_dir();
    # require a single, safe filename component (H1).
    asset_name = str(tar.get("name") or "")
    if Path(asset_name).name != asset_name or not re.fullmatch(r"[A-Za-z0-9._-]+", asset_name):
        raise RunnerError(f"unsafe runner asset name: {asset_name!r}")
    stem = re.sub(r"\.tar\.(xz|gz)$", "", asset_name)
    return {
        "tag": rel.get("tag_name", tag or "latest"),
        "published_at": rel.get("published_at"),
        "tar": tar,
        "sum": assets.get(stem + ".sha512sum"),
    }


def latest_tag(runner_id: str) -> str | None:
    spec = _spec(runner_id)
    try:
        return str(_release(spec, None)["tag"])
    except RunnerError:
        return None


#: repo -> (monotonic fetched_at, payload|None); ``None`` is a cached failure.
_LATEST_CACHE: dict[str, tuple[float, dict[str, Any] | None]] = {}
_LATEST_LOCK = threading.Lock()


def _latest_release(spec: RunnerSpec, *, now: float | None = None) -> dict[str, Any] | None:
    """Latest upstream release for ``spec``, cached for six hours.

    Returns ``None`` (also cached) when the network or API is unavailable, so
    an offline doctor run reports "unknown" instead of hanging.  The cache is
    locked: ``runner_info`` may be called from UI/doctor threads (L5).
    """
    stamp = time.monotonic() if now is None else now
    with _LATEST_LOCK:
        cached = _LATEST_CACHE.get(spec.repo)
        if cached is not None and stamp - cached[0] < RELEASE_CACHE_SECONDS:
            return cached[1]
    payload: dict[str, Any] | None
    try:
        rel = _release(spec, None)
        payload = {"tag": rel["tag"], "published_at": rel.get("published_at")}
    except RunnerError:
        payload = None
    with _LATEST_LOCK:
        _LATEST_CACHE[spec.repo] = (stamp, payload)
    return payload


def clear_release_cache() -> None:
    """Drop the cached upstream release lookups (tests, or a forced refresh)."""
    with _LATEST_LOCK:
        _LATEST_CACHE.clear()


_FAMILY_NEEDLES: tuple[tuple[str, tuple[str, ...]], ...] = (
    ("ge", ("ge-proton", "proton-ge")),
    ("dwproton", ("dwproton", "dw-proton")),
    ("cachyos", ("proton-cachyos", "cachyos")),
)


def runner_family(runner: str | os.PathLike[str]) -> str | None:
    """Map an installed runner path/name to a :data:`RUNNERS` id."""
    # A path is judged by its folder name: a parent called "ge-proton-tests" must not
    # make an unrelated runner look like GE-Proton.
    text = (Path(str(runner)).name if os.sep in str(runner) else str(runner)).lower()
    for runner_id, needles in _FAMILY_NEEDLES:
        if any(needle in text for needle in needles):
            return runner_id
    return None


def _parse_date(value: str | None) -> datetime | None:
    if not value:
        return None
    try:
        parsed = datetime.fromisoformat(str(value).replace("Z", "+00:00"))
    except ValueError:
        return None
    if parsed.tzinfo is None:
        parsed = parsed.replace(tzinfo=UTC)
    return parsed


def is_stale(published_at: str | None, *, now: datetime | None = None) -> bool:
    """True when an upstream release is older than :data:`STALE_DAYS`."""
    released = _parse_date(published_at)
    if released is None:
        return False
    moment = now or datetime.now(UTC)
    if moment.tzinfo is None:
        moment = moment.replace(tzinfo=UTC)
    return moment - released > timedelta(days=STALE_DAYS)


def runner_info(
    runner: str | os.PathLike[str], *, now: datetime | None = None
) -> dict[str, Any]:
    """Metadata for one installed runner, including upstream freshness.

    ``latest_tag``/``latest_published`` come from the GitHub releases API
    (cached six hours); both stay ``None`` when offline. ``stale`` means the
    family's newest upstream release is older than 90 days; ``outdated`` means
    the installed version differs from that latest tag.
    """
    path = Path(runner)
    version = runner_version(path)
    # Proton's ``version`` file is "<unix time> <name>"; the name is the tag.
    tag = version.split()[-1] if version.split() else version
    family = runner_family(path)
    latest_tag_value: str | None = None
    latest_published: str | None = None
    stale: bool | None = None
    outdated: bool | None = None
    if family is not None:
        payload = _latest_release(_spec(family))
        if payload is not None:
            latest_tag_value = str(payload.get("tag") or "") or None
            latest_published = payload.get("published_at")
            stale = is_stale(latest_published, now=now)
            if latest_tag_value:
                outdated = tag.strip() != latest_tag_value.strip()
    recommended = is_recommended(path)
    if recommended:
        # The tested pin is the one to use; a newer upstream build is untested,
        # so the pin is not "outdated" just because something newer exists.
        outdated = False
    return {
        "name": path.name,
        "path": str(path),
        "version": version,
        "tag": tag,
        "family": family,
        "latest_tag": latest_tag_value,
        "latest_published": latest_published,
        "stale": stale,
        "outdated": outdated,
        "recommended": recommended,
        "pinned_tag": pinned_tag(family) if family else None,
    }


def stale_suggestion(info: Mapping[str, Any]) -> str | None:
    """A short upgrade hint for a stale runner family, or ``None``."""
    if not info.get("stale"):
        return None
    family = info.get("family")
    date = str(info.get("latest_published") or "")[:10]
    if family == "cachyos":
        newer = _latest_release(_spec("ge"))
        if newer and newer.get("tag"):
            return (
                f"Proton-CachyOS has not released since {date}; "
                f"GE-Proton {newer['tag']} is newer"
            )
        return f"Proton-CachyOS has not released since {date}; check the GE-Proton runner"
    return f"{info.get('name')} looks stale (last upstream release {date})"


def _spec(runner_id: str) -> RunnerSpec:
    if runner_id not in RUNNERS:
        raise RunnerError(f"unknown runner {runner_id!r}; have: {', '.join(RUNNERS)}")
    return RUNNERS[runner_id]


# ---------------------------------------------------------------------------
# the recommended (tested, pinned) runner
# ---------------------------------------------------------------------------
#: The runner family a profile uses when it names none.
DEFAULT_FAMILY = "dwproton"


def pinned_tag(family: str) -> str | None:
    """The tested release tag for ``family`` (its sha256 is pinned), if any."""
    spec = RUNNERS.get(family)
    return spec.tested[0] if spec is not None and spec.tested is not None else None


def matches_tag(name: str, tag: str) -> bool:
    """True when ``name`` is that exact release (not ``-14`` inside ``-140``/``-14.1``)."""
    pattern = r"(?<![0-9A-Za-z])" + re.escape(tag) + r"(?![0-9A-Za-z.])"
    return re.search(pattern, name, re.IGNORECASE) is not None


def is_recommended(runner: str | os.PathLike[str]) -> bool:
    """True when ``runner`` is the tested pinned release of its family."""
    family = runner_family(runner)
    tag = pinned_tag(family) if family else None
    return bool(tag) and matches_tag(Path(str(runner)).name, str(tag))


def installed_pinned(family: str = DEFAULT_FAMILY) -> Path | None:
    """The already-installed pinned release of ``family``, or ``None``.

    Looks in Nadare's own folder first, then Steam's tool dirs, so a pinned build
    someone already has is reused and never downloaded again.
    """
    tag = pinned_tag(family)
    if not tag:
        return None
    for path in all_runners():
        if runner_family(path) == family and matches_tag(path.name, tag):
            return path
    return None


def ensure_pinned(
    family: str = DEFAULT_FAMILY,
    *,
    emit: Emitter | None = None,
    cancel: Cancel | None = None,
) -> Path:
    """The pinned release of ``family``: reuse an installed copy, else download it once."""
    found = installed_pinned(family)
    if found is not None:
        return found
    return install(family, latest=False, emit=emit, cancel=cancel)


def _github_asset_host_allowed(url: str) -> bool:
    """True when ``url`` is an ``https`` GitHub-controlled asset location.

    Used as the per-hop ``redirect_allowed`` callback so the ``.sha512sum``
    trust anchor cannot be redirected off GitHub (NV-2).
    """
    try:
        parsed = urlparse(url)
        host = (parsed.hostname or "").lower()
    except ValueError:
        return False
    return parsed.scheme == "https" and host in _GITHUB_ASSET_HOSTS


def _published_sha512(sum_asset: Mapping[str, Any]) -> str:
    url = str(sum_asset.get("browser_download_url") or "")
    if not url:
        raise RunnerError("release has no .sha512sum asset")
    # The sum URL comes from the same JSON as the artifact, so pin it to a
    # GitHub-controlled host rather than trusting an arbitrary redirect target (L6);
    # every redirect hop must stay on that host too (NV-2).
    if not _github_asset_host_allowed(url):
        raise RunnerError(f"refusing .sha512sum from untrusted host: {url}")
    try:
        text = download.http_get(
            url, timeout=30, redirect_allowed=_github_asset_host_allowed
        ).decode("utf-8", "replace")
    except Exception as exc:  # noqa: BLE001
        raise RunnerError(f"cannot fetch .sha512sum: {exc}") from exc
    parts = text.split()
    if not parts:
        raise RunnerError("release .sha512sum asset is empty")
    return parts[0].lower()


# ---------------------------------------------------------------------------
# install
# ---------------------------------------------------------------------------
def install(
    runner_id: str,
    *,
    latest: bool = False,
    emit: Emitter | None = None,
    cancel: Cancel | None = None,
) -> Path:
    """Download, verify and extract one runner. Returns its directory."""
    say = emitter(emit)
    spec = _spec(runner_id)
    if not latest and spec.tested is None:
        raise RunnerError(f"{spec.name} has no tested release yet; use latest=True")
    tag, pinned = (None, None) if latest else spec.tested

    rel = _release(spec, tag)
    tar = rel["tar"]
    size = int(tar.get("size") or 0)
    say(
        Event(
            "status",
            f"{spec.name} {rel['tag']} ({size >> 20} MB) from github.com/{spec.repo}",
            data={"runner": runner_id, "tag": rel["tag"], "tested": not latest},
        )
    )

    expected_sha512: str | None = None
    expected_sha256: str | None = None
    if pinned is None:
        # GitHub's per-asset ``digest`` is a generic fallback when a project
        # does not publish a .sha512sum file. Refuse only when neither exists.
        gh_digest = str(tar.get("digest") or "").removeprefix("sha256:").strip().lower()
        if rel["sum"]:
            expected_sha512 = _published_sha512(rel["sum"])
        elif gh_digest:
            expected_sha256 = gh_digest
        else:
            raise RunnerError(
                f"{spec.name} {rel['tag']} publishes no .sha512sum or asset digest; cannot verify"
            )

    cache = paths.cache_dir() / "runners"
    cache.mkdir(parents=True, exist_ok=True)
    tar_path = cache / str(tar["name"])
    if size:
        diskspace.check_space(cache, size)
        runners_root = paths.runners_dir()
        runners_root.mkdir(parents=True, exist_ok=True)
        diskspace.check_space(runners_root, size * 3)  # extracted > compressed

    # The artifact URL comes from the same untrusted JSON as the checksum, so
    # pin its host too rather than trusting an arbitrary redirect target (NV-2).
    tar_url = str(tar.get("browser_download_url") or "")
    if not _github_asset_host_allowed(tar_url):
        raise RunnerError(f"refusing runner tarball from untrusted host: {tar_url}")

    # Two installs of the same runner must not share (and unlink) one cache file:
    # serialise them on a per-asset lock. The loser then finds the runner installed.
    with _asset_lock(cache, str(tar["name"])):
        if installed_pinned(runner_id) is not None and not latest:
            found = installed_pinned(runner_id)
            assert found is not None
            say(Event("done", f"installed {found.name}", data={"path": str(found)}))
            return found
        result = download.download(
            tar_url,
            tar_path,
            expected_sha256=pinned or expected_sha256,
            expected_sha512=expected_sha512,
            expected_size=size or None,
            headers={"Accept": "application/octet-stream"},
            emit=say,
            cancel=cancel,
            redirect_allowed=_github_asset_host_allowed,
        )

        try:
            dest = _extract(result.path, cancel=cancel, emit=say)
        finally:
            try:
                result.path.unlink(missing_ok=True)
            except OSError:
                pass

    source = "pinned sha256" if pinned else ("asset digest" if expected_sha256 else "sha512")
    say(Event("log", f"checksum ok ({source})"))
    say(
        Event(
            "done",
            f"installed {dest.name}",
            data={"path": str(dest), "tag": rel["tag"], "tested": not latest},
        )
    )
    return dest


#: A ``.unpack-*`` directory younger than this may belong to a live installer.
_STALE_UNPACK_SECONDS = 6 * 3600
_OWNER_MARKER = ".nadare-owner-pid"


def _owned_by_live_process(directory: Path) -> bool:
    try:
        pid = int((directory / _OWNER_MARKER).read_text().strip())
        os.kill(pid, 0)
    except PermissionError:
        return True  # exists, owned by someone else
    except (OSError, ValueError):
        return False
    return True


def _prune_stale(root: Path) -> None:
    """Remove hidden ``.unpack-*``/``.<name>.old-*`` crash leftovers (NV-3).

    A hidden work tree is only pruned when it is not owned by a live process;
    an ``.unpack-*`` directory must also be old. The owner check keeps a
    concurrent installer's ``.old-*`` backup safe during its swap window, and
    old ``.unpack-*`` staging trees are never wiped while in use.
    """
    for pattern in (".*.old-*", ".unpack-*"):
        for leftover in root.glob(pattern):
            if _owned_by_live_process(leftover):
                continue
            if pattern == ".unpack-*":
                try:
                    age = time.time() - leftover.stat().st_mtime
                except OSError:
                    continue
                if age < _STALE_UNPACK_SECONDS:
                    continue
            shutil.rmtree(leftover, ignore_errors=True)


#: A runner tarball unpacks to ~1.6 GB in a few tens of thousands of files; these
#: caps stop a malicious or corrupt archive from filling the disk.
_MAX_UNPACKED_BYTES = 16 * 1024**3
_MAX_MEMBERS = 300_000


class _asset_lock:  # noqa: N801 - used like a context manager function
    """An exclusive file lock for one cached asset (blocks until it is free)."""

    def __init__(self, cache: Path, name: str) -> None:
        self._path = cache / f".{name}.lock"
        self._handle: Any = None

    def __enter__(self) -> _asset_lock:
        self._handle = open(self._path, "w")  # noqa: SIM115 - held until __exit__
        fcntl.flock(self._handle, fcntl.LOCK_EX)
        return self

    def __exit__(self, *_exc: object) -> None:
        try:
            fcntl.flock(self._handle, fcntl.LOCK_UN)
        finally:
            self._handle.close()


def _extract(tar_path: Path, *, cancel: Cancel | None, emit: Emitter) -> Path:
    root = paths.runners_dir()
    root.mkdir(parents=True, exist_ok=True)
    _prune_stale(root)
    tmp = Path(tempfile.mkdtemp(prefix=".unpack-", dir=str(root)))
    try:
        (tmp / _OWNER_MARKER).write_text(str(os.getpid()))
        emit(Event("status", "unpacking runner", data={"stage": "extract"}))
        if cancel is not None:
            cancel.raise_if_cancelled()
        try:
            with tarfile.open(tar_path) as archive:
                # Extract member-by-member so a large archive can be cancelled
                # mid-way (L7); filter="data" rejects absolute paths, escaping
                # links, devices and setuid bits (Python 3.12+).
                expected: list[str] = []
                unpacked = 0
                for member in archive:
                    if cancel is not None:
                        cancel.raise_if_cancelled()
                    unpacked += max(member.size, 0)
                    if len(expected) >= _MAX_MEMBERS or unpacked > _MAX_UNPACKED_BYTES:
                        raise RunnerError("unpack refused: the archive is far larger than a runner")
                    archive.extract(member, tmp, filter="data")
                    expected.append(member.name)
        except tarfile.TarError as exc:
            raise RunnerError(f"unpack failed: {exc}") from exc
        # The tree must match the archive's member list, so a tree that was
        # truncated underneath us is never installed.
        missing = [n for n in expected if not os.path.lexists(tmp / n)]
        if missing:
            raise RunnerError(
                f"unpack incomplete: {len(missing)} of {len(expected)} entries missing"
            )
        tops = [d for d in tmp.iterdir() if d.is_dir()]
        if len(tops) != 1 or not _looks_like_runner(tops[0]):
            raise RunnerError("unexpected tarball layout (no <dir>/proton)")
        if cancel is not None:
            cancel.raise_if_cancelled()  # a cancel during unpacking must not still install
        dest = root / tops[0].name
        # Never delete the working install before the replacement is in place
        # (M4): move the old tree aside, swap the new one in, and only then
        # remove the old copy. If the swap fails, restore the old tree.
        stale: Path | None = None
        if dest.exists():
            stale = root / f".{dest.name}.old-{uuid.uuid4().hex}"
            os.replace(dest, stale)
            # Mark the backup as owned by this live install so a concurrent
            # _prune_stale cannot delete it during the swap window (NV-3).
            try:
                (stale / _OWNER_MARKER).write_text(str(os.getpid()))
            except OSError:
                pass
        try:
            os.replace(tops[0], dest)
        except BaseException:
            if stale is not None and not dest.exists():
                os.replace(stale, dest)
                try:
                    (dest / _OWNER_MARKER).unlink()
                except OSError:
                    pass
            raise
        if stale is not None:
            shutil.rmtree(stale, ignore_errors=True)
        return dest
    finally:
        shutil.rmtree(tmp, ignore_errors=True)


#: Backends whose profiles use :data:`DEFAULT_FAMILY` when they name no runner.
PROTON_DEFAULT_BACKENDS = frozenset({"stove-czn", "arknights-yostar", "stellasora-yostar"})


def runner_size(runner: str | os.PathLike[str]) -> int:
    """Bytes a runner folder occupies (symlinks are not followed)."""
    total = 0
    stack = [str(runner)]
    while stack:
        current = stack.pop()
        try:
            with os.scandir(current) as entries:
                for entry in entries:
                    try:
                        if entry.is_symlink():
                            continue
                        if entry.is_dir(follow_symlinks=False):
                            stack.append(entry.path)
                        else:
                            total += entry.stat(follow_symlinks=False).st_size
                    except OSError:
                        continue
        except OSError:
            continue
    return total


def runner_usage(profiles: Mapping[str, Mapping[str, Any]]) -> dict[str, list[str]]:
    """Which profiles use which Nadare-installed runner (path -> profile names).

    A profile uses a runner when it names it by path or folder, by family id
    (``dwproton`` means that family's pinned build), by ``latest``/``auto``, or
    names nothing on a backend that defaults to the pinned DW-Proton.
    """
    # Several entries can resolve to one folder (a symlink and its target): a game
    # that uses it uses every alias, so none of them may be offered for deletion.
    installed: dict[str, Path] = {}
    by_resolved: dict[str, list[str]] = {}
    for p in nadare_runners():
        key = str(p.resolve())
        installed.setdefault(key, p)
        by_resolved.setdefault(key, []).append(str(p))
    usage: dict[str, list[str]] = {
        alias: [] for aliases in by_resolved.values() for alias in aliases
    }

    for name, profile in profiles.items():
        value = str(profile.get("proton_runner") or profile.get("runner") or "").strip()
        targets: list[Path | None] = []
        if not value:
            if profile.get("backend") in PROTON_DEFAULT_BACKENDS:
                targets.append(installed_pinned(DEFAULT_FAMILY))
        elif value in RUNNERS:
            targets.append(installed_pinned(value))
        elif value in ("latest", "auto"):
            try:
                targets.append(resolve_runner(value))
            except RunnerError:
                pass
        elif value.lower() not in ("wine", "system", "none"):
            candidate = Path(value).expanduser()
            if not candidate.is_absolute():
                candidate = paths.runners_dir() / value
            targets.append(candidate)
            # Some backends match a tag by substring ("GE-Proton11-7" finds
            # "GE-Proton11-7-x86_64"). Count every installed runner that matches, so
            # a runner that is really in use is never offered for deletion.
            wanted = value.lower()
            if not candidate.exists() and os.sep not in value:
                targets.extend(p for p in installed.values() if wanted in p.name.lower())
        for target in targets:
            if target is None:
                continue
            try:
                key = str(target.resolve())
            except OSError:
                continue
            for alias in by_resolved.get(key, []):
                usage[alias].append(str(name))
    return usage


def unused_old_runners(profiles: Mapping[str, Mapping[str, Any]]) -> list[Path]:
    """Nadare-installed runners no profile uses, leaving the recommended builds alone.

    These are safe to delete: nothing needs them, and the recommended (tested)
    build is kept so no game has to download it again.
    """
    usage = runner_usage(profiles)
    return sorted(
        Path(key) for key, users in usage.items() if not users and not is_recommended(key)
    )


def remove(runner_id_or_path: str | os.PathLike[str]) -> bool:
    """Remove an Nadare-installed runner by id or path. Returns True if removed."""
    root = paths.runners_dir().resolve()
    candidate = Path(runner_id_or_path).expanduser()
    if not candidate.is_absolute():
        candidate = root / str(runner_id_or_path)
    # Judge the entry by where it lives, not by where it points: resolve only its
    # parent, so a symlink inside the runners folder is removed as a link and never
    # takes its target (possibly the recommended build) with it.
    try:
        parent = candidate.parent.resolve()
    except OSError:
        return False
    if parent != root and root not in parent.parents:
        return False
    entry = parent / candidate.name
    if entry.is_symlink():
        try:
            entry.unlink()
        except OSError:
            return False
        return True
    if not entry.is_dir():
        return False
    shutil.rmtree(entry)
    return True
