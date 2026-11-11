# SPDX-License-Identifier: GPL-3.0-or-later
# Copyright (C) 2026 Nadare contributors
"""Run NIKKE without ``nikke_launcher.exe``: the two Windows helpers and their files.

NIKKE's game expects its publisher launcher at start-up, and the sign-in session
it needs comes from the publisher's INTL service. This module supplies both
pieces so the launcher never runs:

* ``nikke_intlsvc_host.exe`` starts the installed ``intl_service.exe`` (not the
  launcher) and signs in with the saved session. No password, no browser when a
  saved session exists.
* ``nikke_sail_host.exe`` starts ``nikke.exe`` and gives it that session, as the
  launcher would.

Both helpers are built from ``hosts_src/`` (``build_hosts.sh``) and ship
prebuilt; a helper is used only when it matches its pinned digest.

The account session never leaves the profile: the INTL cache lives in the
profile's data directory (files 0600) and the Sail session JSON in a private
directory inside the prefix for the length of one run.
"""

from __future__ import annotations

import hashlib
import json
import os
import stat
import tempfile
from dataclasses import dataclass
from pathlib import Path
from typing import Any

from .session import DEFAULT_GAME_RESOURCE_PATH, GAME_ID, build_session

#: Re-exported for the command-line entry point (``scripts/nikke_launcherless``).
DEFAULT_RESOURCE_PATH = DEFAULT_GAME_RESOURCE_PATH

#: ``--ParentName`` of the launcher whose cache the INTL service reads.
PARENT_NAME = "nikke_launcher"
#: Login channel offered by ``--login``: 6 is the generic third-party OAuth page.
LOGIN_CHANNEL = 6

INTL_HOST_NAME = "nikke_intlsvc_host.exe"
SAIL_HOST_NAME = "nikke_sail_host.exe"
_ASSETS = Path(__file__).resolve().parents[2] / "assets"
INTL_HOST = _ASSETS / INTL_HOST_NAME
SAIL_HOST = _ASSETS / SAIL_HOST_NAME
INTL_HOST_SHA256 = "3a442d1a045d5dca04c25fec7512f591bc9e6589cb95a239f8f7984f31e8ea76"
SAIL_HOST_SHA256 = "21b4305cfdd78de23a0ce85828c022f6cbdc0519318e95c5680af02f77afc9c5"
INTL_SOURCE_SHA256 = "09437533adc13721e68889594fed60bc8df3980ffb091ad0b20c8c2f2f1a3990"
SAIL_SOURCE_SHA256 = "8febc84135ac0795b1cd122706b3554292ba4524fd0193141718ffb8ebf3f18c"

#: Files the INTL service needs to auto-login. Logs and reports are excluded.
#: Without this the copy is not a sign-in (a fresh Google sign-in may have no mmkv yet).
_REQUIRED_CACHE_FILES = ("intl_auth.txt",)

CACHE_FILES = (
    "intl_auth.txt",
    "mmkv/mmkv.default",
    "mmkv/mmkv.default.crc",
    "IntlRemoteConfig",
    "RtRemoteConfig",
    "ApasRemoteConfig",
    "LipassRemoteConfig",
    "IntlCustomConfig.tmp",
)

#: Where the helpers are staged inside the prefix (a path no game code owns).
STAGE_PARTS = ("drive_c", "Games", ".nadare", "nikke")

_SEARCH_MAX_DEPTH = 4
_SEARCH_MAX_DIRS = 20_000


class HostError(RuntimeError):
    """A helper is missing, does not match its pin, or could not be prepared."""


@dataclass(frozen=True)
class GameFiles:
    """The installed game as the Sail host needs it (Windows paths are made later)."""

    launcher_dir: Path
    game_exe: Path
    game_cwd: Path
    resource_dir: Path
    service: Path


# ---------------------------------------------------------------- path helpers


def win_path(prefix: str | Path, path: str | Path) -> str:
    """The Windows path Wine sees for ``path`` under ``prefix``.

    ``drive_c`` is ``C:``. A game installed through a ``dosdevices`` drive
    (the launcher's ``S:``, or a folder the user added) keeps that letter, so
    the game's own configuration still resolves; anything else is ``Z:``.
    """
    prefix = Path(prefix)
    target = Path(path).resolve()
    try:
        inside = target.relative_to((prefix / "drive_c").resolve())
    except ValueError:
        inside = None
    if inside is not None:
        return "C:\\" + "\\".join(inside.parts)

    best: tuple[str, Path, Path] | None = None
    try:
        links = list((prefix / "dosdevices").iterdir())
    except OSError:
        links = []
    for link in links:
        if len(link.name) != 2 or not link.name.endswith(":"):
            continue
        letter = link.name[0].lower()
        if letter in "cz":
            continue
        try:
            root = link.resolve()
            rest = target.relative_to(root)
        except (OSError, ValueError):
            continue
        if best is None or len(root.parts) > len(best[1].parts):
            best = (letter.upper(), root, rest)
    if best is not None:
        return f"{best[0]}:\\" + "\\".join(best[2].parts)
    return "Z:\\" + "\\".join(target.parts[1:])


def _find_named(
    root: Path,
    name: str,
    *,
    want_dir: bool = False,
    skip: Path | None = None,
    max_depth: int = _SEARCH_MAX_DEPTH,
) -> Path | None:
    """First ``name`` under ``root`` (bounded breadth-first).

    Folder links are followed: a game folder moved to another disk and linked back is a normal
    setup (round 5). A linked file is never returned as the program to start (round 6). The
    search is bounded in depth and in folders, so a link loop ends.
    """
    if not root.is_dir():
        return None
    wanted = name.lower()
    seen = 0
    queue: list[tuple[Path, int]] = [(root, 0)]
    while queue and seen < _SEARCH_MAX_DIRS:
        directory, depth = queue.pop(0)
        try:
            entries = list(directory.iterdir())
        except OSError:
            continue
        for entry in entries:
            seen += 1
            try:
                is_dir = entry.is_dir()
                is_file = entry.is_file()
            except OSError:
                continue
            if entry.name.lower() == wanted and (is_dir if want_dir else is_file):
                if not want_dir and entry.is_symlink():
                    continue  # a moved folder may be a link; the program we start may not
                return entry
            if is_dir and depth < max_depth and entry != skip:
                queue.append((entry, depth + 1))
    return None


def find_game(launcher_exe: str | Path, prefix: str | Path) -> GameFiles | None:
    """Locate the installed game and the INTL service next to ``launcher_exe``.

    The launcher puts the game under its own parent folder (``<root>/NIKKE/game``)
    and the resources under ``<root>/Unity/com_proximabeta_NIKKE``. A game on an
    added drive is found through the prefix's ``dosdevices``.
    """
    launcher_exe = Path(launcher_exe)
    prefix = Path(prefix)
    launcher_dir = launcher_exe.parent
    service = launcher_dir / "intl_service" / "intl_service.exe"
    if not launcher_exe.is_file() or not service.is_file():
        return None

    roots = [launcher_dir.parent]
    try:
        roots.extend(
            link.resolve()
            for link in sorted((prefix / "dosdevices").iterdir())
            if len(link.name) == 2 and link.name.endswith(":") and link.name[0].lower() not in "cz"
        )
    except OSError:
        pass

    game_exe = None
    for root in roots:
        game_exe = _find_named(root, "nikke.exe", skip=launcher_dir)
        if game_exe is not None:
            break
    if game_exe is None:
        return None

    resource_dir = None
    for ancestor in (game_exe.parent, *game_exe.parents[:3]):
        candidate = ancestor / "Unity" / "com_proximabeta_NIKKE"
        if candidate.is_dir():
            resource_dir = candidate
            break
    if resource_dir is None:
        for root in roots:
            resource_dir = _find_named(root, "com_proximabeta_NIKKE", want_dir=True)
            if resource_dir is not None:
                break
    if resource_dir is None:
        # A client-only install has no resources yet: the game creates this folder on its
        # first run. Returning it (which need not exist) keeps the launcherless launch
        # possible instead of falling back to the launcher. The game lives at
        # ``<install root>/NIKKE/game/nikke.exe``, so walk up to that install root.
        install_root = game_exe.parents[2] if len(game_exe.parents) > 2 else game_exe.parent
        resource_dir = install_root / "Unity" / "com_proximabeta_NIKKE"

    return GameFiles(
        launcher_dir=launcher_dir,
        game_exe=game_exe,
        game_cwd=game_exe.parent,
        resource_dir=resource_dir,
        service=service,
    )


# ----------------------------------------------------------------- no links
# The game can write anywhere in its prefix, even in the Sandbox, while Nadare runs outside it:
# nothing Nadare writes or reads below the prefix may follow a link the game left there
# (the game search follows folder links on purpose, never a linked program: see _find_named).


def _plain_dirs(root: Path, parts: tuple[str, ...] | list[str]) -> Path:
    """``root/parts...`` made as real folders; a link (or file) on the way is refused."""
    here = Path(root)
    for part in parts:
        here = here / part
        if here.is_symlink():
            raise HostError(f"{here} is a link; refusing to use it")
        try:
            here.mkdir(exist_ok=True)
        except FileExistsError:
            raise HostError(f"{here} is not a folder") from None
        if not here.is_dir():
            raise HostError(f"{here} is not a folder")
    return here


def _below(root: Path, path: Path) -> tuple[str, ...]:
    try:
        return Path(path).relative_to(root).parts
    except ValueError:
        raise HostError(f"{path} is not inside {root}") from None


def _write_plain(path: Path, data: bytes, mode: int) -> None:
    """Replace ``path`` with a new regular file; a link there is removed, never followed."""
    if path.is_symlink() or path.exists():
        if path.is_dir() and not path.is_symlink():
            raise HostError(f"{path} is a folder")
        path.unlink()
    fd = os.open(path, os.O_WRONLY | os.O_CREAT | os.O_EXCL | os.O_NOFOLLOW, mode)
    with os.fdopen(fd, "wb") as handle:
        os.fchmod(handle.fileno(), mode)  # O_CREAT's mode is cut by the umask
        handle.write(data)


def _keep_mtime(source: Path, target: Path) -> None:
    """Carry the source's times over (the newer sign-in wins by mtime), not its mode."""
    info = source.stat(follow_symlinks=False)
    os.utime(target, ns=(info.st_atime_ns, info.st_mtime_ns), follow_symlinks=False)


def _read_plain(path: Path) -> bytes:
    """The bytes of a regular file at ``path``; a link raises ``OSError`` (ELOOP)."""
    fd = os.open(path, os.O_RDONLY | os.O_NOFOLLOW)
    with os.fdopen(fd, "rb") as handle:
        if not stat.S_ISREG(os.fstat(handle.fileno()).st_mode):
            raise OSError(f"{path} is not a regular file")
        return handle.read()


# ----------------------------------------------------------------- pinned hosts


def _digest(path: Path) -> str | None:
    try:
        return hashlib.sha256(path.read_bytes()).hexdigest()
    except OSError:
        return None


def stage_hosts(prefix: str | Path) -> Path:
    """Copy both pinned helpers into the prefix; return their folder there.

    A helper that is missing or does not match its pin is refused rather than
    run: these are the only Nadare binaries that touch the game's session.
    """
    folder = _plain_dirs(Path(prefix), STAGE_PARTS)
    os.chmod(folder, 0o700)
    for source, name, want in (
        (INTL_HOST, INTL_HOST_NAME, INTL_HOST_SHA256),
        (SAIL_HOST, SAIL_HOST_NAME, SAIL_HOST_SHA256),
    ):
        data = source.read_bytes() if source.is_file() else b""
        if not data:
            raise HostError(f"the NIKKE helper {name} is missing; reinstall Nadare")
        if hashlib.sha256(data).hexdigest() != want:
            raise HostError(f"the NIKKE helper {name} does not match its pin; reinstall Nadare")
        _write_plain(folder / name, data, 0o700)
    return folder


# ---------------------------------------------------------------- INTL cache


def cache_storage_dir(
    prefix: str | Path, parent_name: str = PARENT_NAME, game_id: int = GAME_ID
) -> Path:
    """The INTL service's own storage folder inside a Wine prefix."""
    users = Path(prefix) / "drive_c" / "users"
    preferred = users / "steamuser"
    if preferred.is_dir():
        user = preferred
    else:
        try:
            user = next(
                p
                for p in sorted(users.iterdir())
                if p.is_dir() and not p.is_symlink() and p.name != "Public"
            )
        except (OSError, StopIteration):
            user = users / "steamuser"
    return user / "AppData" / "Roaming" / parent_name / str(game_id) / "intl_service"


def has_cache(cache_dir: str | Path) -> bool:
    """Whether ``cache_dir`` holds a saved INTL sign-in."""
    cache = Path(cache_dir)
    auth = cache / "intl_auth.txt"
    try:
        # An empty file is what a killed or failed sign-in leaves: not a sign-in.
        return auth.is_file() and auth.stat().st_size > 0 and (
            cache / "mmkv" / "mmkv.default"
        ).is_file()
    except OSError:
        return False


def save_cache(source: str | Path, cache_dir: str | Path) -> None:
    """Copy the service cache into Nadare's profile (directory 0700, files 0600)."""
    src = Path(source)
    auth = src / "intl_auth.txt"
    if auth.is_symlink() or not auth.is_file():
        raise HostError(f"no intl_auth.txt in {src}")
    dst = Path(cache_dir)
    dst.mkdir(parents=True, exist_ok=True)
    os.chmod(dst, 0o700)
    for rel in CACHE_FILES:
        source_file = src / rel
        required = rel in _REQUIRED_CACHE_FILES
        # A link anywhere on the way (a linked mmkv folder, say) would copy a file from outside
        # the prefix into Nadare's cache and later back into the prefix (final round).
        on_the_way = [src.joinpath(*Path(rel).parts[:i]) for i in range(1, len(Path(rel).parts))]
        if any(folder.is_symlink() for folder in on_the_way) or (
            source_file.is_symlink() or not source_file.is_file()
        ):
            if required:  # a link the game left could point at any file in home
                raise HostError(f"{source_file} is missing or is a link")
            continue
        try:
            data = _read_plain(source_file)
        except OSError as exc:
            if required:
                raise HostError(f"cannot read {source_file}: {exc}") from exc
            continue
        target = dst / rel
        target.parent.mkdir(parents=True, exist_ok=True)
        _write_plain(target, data, 0o600)
        _keep_mtime(source_file, target)


def restore_cache(
    cache_dir: str | Path, destination: str | Path, *, root: str | Path | None = None
) -> None:
    """Put a saved INTL sign-in back into the prefix (files 0600).

    With ``root`` (the prefix), every folder from ``root`` down to each file is checked to be a
    real folder, and no file is written through a link.
    """
    src = Path(cache_dir)
    if not has_cache(src):
        raise HostError(f"no saved NIKKE sign-in in {src}")
    dst = Path(destination)
    if root is not None:
        dst = _plain_dirs(Path(root), _below(Path(root), dst))
    else:
        dst.mkdir(parents=True, exist_ok=True)
    for rel in CACHE_FILES:
        source_file = src / rel
        if not source_file.is_file():
            continue
        target_dir = _plain_dirs(dst, Path(rel).parent.parts)
        target = target_dir / Path(rel).name
        _write_plain(target, source_file.read_bytes(), 0o600)
        _keep_mtime(source_file, target)


# ------------------------------------------------------------- command lines


def intl_command(
    host: str,
    service: str,
    out: str,
    *,
    parent_name: str = PARENT_NAME,
    game_id: int = GAME_ID,
    login_channel: int = LOGIN_CHANNEL,
    auto_login: int = 1,
    rsp_timeout_ms: int | None = None,
) -> list[str]:
    """Arguments for the INTL service host (``--out`` is a Windows path)."""
    command = [
        host,
        "--service",
        service,
        "--out",
        out,
        "--parent-name",
        parent_name,
        "--game-id",
        str(game_id),
        "--login-channel",
        str(login_channel),
        "--auto-login",
        str(auto_login),
    ]
    if rsp_timeout_ms is not None:
        command.extend(["--rsp-timeout-ms", str(rsp_timeout_ms)])
    return command


def sail_command(
    host: str,
    session: str,
    game_exe: str,
    game_cwd: str,
    extra_args: list[str] | None = None,
) -> list[str]:
    """Arguments for the Sail host (all paths are Windows paths)."""
    return [host, session, "--launch", game_exe, game_cwd, *(extra_args or [])]


def host_env(
    prefix: str | Path,
    *,
    launcher_dir: str,
    log: str | None = None,
    base: dict[str, str] | None = None,
) -> dict[str, str]:
    """Environment only the Sail host needs; the runner supplies the rest."""
    env = dict(base if base is not None else os.environ)
    env["WINEPREFIX"] = str(prefix)
    env["SAIL_LAUNCHER_DIR"] = launcher_dir
    if log:
        env["SAIL_HOST_LOG"] = log
    return env


# --------------------------------------------------------------- session work


def read_auth(path: str | Path) -> dict[str, Any]:
    """Read the ``session.enc.json`` the INTL host wrote."""
    try:
        auth = json.loads(_read_plain(Path(path)).decode("utf-8"))
    except (OSError, ValueError) as exc:
        raise HostError(f"the NIKKE sign-in returned no session: {exc}") from exc
    if not isinstance(auth, dict):
        raise HostError("the NIKKE sign-in returned an unexpected session")
    return auth


def make_session_file(
    auth: dict[str, Any], path: str | Path, *, game_resource_path: str
) -> Path:
    """Write the Sail session JSON with 0600 permissions and return it."""
    target = Path(path)
    session = build_session(auth, game_resource_path=game_resource_path)
    target.write_text(json.dumps(session, ensure_ascii=False), encoding="utf-8")
    os.chmod(target, 0o600)
    return target


def private_workdir(parent: str | Path) -> Path:
    """A fresh 0700 directory for one run's session files."""
    return Path(tempfile.mkdtemp(prefix="session-", dir=str(parent)))
