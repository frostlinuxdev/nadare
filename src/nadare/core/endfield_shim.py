# SPDX-License-Identifier: GPL-3.0-or-later
# Copyright (C) 2026 Nadare contributors
"""Build and stage the Endfield launcher shim inside a Wine prefix.

Arknights: Endfield's launcher build only starts when its parent process is the
official launcher's ``Games.exe``. Nadare therefore starts the game through a
small Windows helper, ``suspend_start.exe --launcher <Games.exe> <game.exe>``,
built from ``shim_src/suspend_start.c`` with mingw-w64. This module builds that
helper, obtains ``Games.exe`` (from a cached copy, the prefix, or the
publisher's launcher installer) and stages both inside the prefix so the game
can be launched with plain ``C:\\`` paths.
"""

from __future__ import annotations

import filecmp
import hashlib
import os
import shutil
import subprocess
import tempfile
import urllib.parse
from collections.abc import Callable
from dataclasses import dataclass
from pathlib import Path
from typing import Any

from . import download as _dl
from . import sevenzip

#: Endpoint returning the current Gryphline launcher installer (``exe_url``).
LAUNCHER_API = (
    "https://launcher.gryphline.com/api/launcher/get_latest_launcher"
    "?appcode=CNKz1UJ80EQAzkPn&channel=6&sub_channel=6"
)

#: Hosts (and their subdomains) allowed to serve the launcher installer.
ALLOWED_INSTALLER_HOSTS = ("hg-cdn.com", "gryphline.com")

#: Prefix-relative directory where the staged shim and launcher live.
_STAGE_PARTS = ("drive_c", "Games", ".nadare", "endfield-shim")


class ShimError(RuntimeError):
    """The shim could not be built, or the launcher could not be obtained.

    ``step`` names what failed (``build``, ``fetch`` or ``extract``) and ``hint`` says what
    to do; both end up in the text the user sees.
    """

    def __init__(self, message: str, *, step: str = "", hint: str = "") -> None:
        text = f"{message} (step: {step})" if step else message
        if hint:
            text = f"{text}. {hint}"
        super().__init__(text)
        self.step = step
        self.hint = hint


_REPORT_HINT = (
    "The publisher may have changed its launcher; please report this "
    "(Report a problem, or `nadare report`)"
)
_NETWORK_HINT = "Check your network connection and try again"


@dataclass(frozen=True)
class ShimFiles:
    """The staged ``suspend_start.exe`` and ``Games.exe`` inside the prefix."""

    shim: Path
    games: Path


def installer_host_allowed(url: str) -> bool:
    """True when ``url`` is https on a publisher host (or one of their subdomains)."""
    parsed = urllib.parse.urlparse(str(url))
    host = (parsed.hostname or "").lower()
    return parsed.scheme == "https" and any(
        host == allowed or host.endswith("." + allowed) for allowed in ALLOWED_INSTALLER_HOSTS
    )


def win_path(prefix: Path, path: Path) -> str:
    """Return the Windows path Wine sees for ``path`` under ``prefix``."""
    drive_c = prefix / "drive_c"
    try:
        rest = path.relative_to(drive_c)
    except ValueError:
        parts = path.parts
        if path.is_absolute() and parts:
            parts = parts[1:]
        return "Z:\\" + "\\".join(parts)
    return "C:\\" + "\\".join(rest.parts)


def is_steam_build(install_dir: Path) -> bool:
    """True when ``install_dir`` looks like the Steam build of the game."""
    return (install_dir / "steam_api64.dll").is_file()


def launch_argv(shim_win: str, games_win: str, game_win: str, args: list[str]) -> list[str]:
    """The command line that starts the game through the shim."""
    return [shim_win, "--launcher", games_win, game_win, *args]


def _version_key(name: str) -> tuple[tuple[int, int], ...]:
    """Sort key for a version directory name; non-numeric parts sort lowest."""
    return tuple((1, int(part)) if part.isdigit() else (0, 0) for part in name.split("."))


def find_games_exe(prefix: Path) -> Path | None:
    """Return the newest ``Games.exe`` installed in the prefix, if any."""
    found: list[tuple[tuple[tuple[int, int], ...], Path]] = []
    for program_files in ("Program Files", "Program Files (x86)"):
        root = prefix / "drive_c" / program_files / "GRYPHLINK"
        if not root.is_dir():
            continue
        for child in root.iterdir():
            exe = child / "Games.exe"
            if exe.is_file():
                found.append((_version_key(child.name), exe))
    if not found:
        return None
    return max(found, key=lambda item: item[0])[1]


def build_shim(
    out: Path,
    *,
    source: Path | None = None,
    cc: str | None = None,
    which: Callable[[str], str | None] = shutil.which,
    run: Callable[..., subprocess.CompletedProcess[str]] = subprocess.run,
) -> Path:
    """Compile ``suspend_start.exe`` to ``out``, reusing a matching build."""
    src = source if source is not None else Path(__file__).with_name("shim_src") / "suspend_start.c"
    stamp = out.with_name(out.name + ".src-sha256")
    digest = hashlib.sha256(src.read_bytes()).hexdigest()
    if out.is_file() and stamp.is_file():
        try:
            if stamp.read_text(encoding="ascii").strip() == digest:
                return out
        except OSError:
            pass

    compiler = cc or os.environ.get("CC") or "x86_64-w64-mingw32-gcc"
    resolved = which(compiler)
    if not resolved:
        raise ShimError(
            f"mingw-w64 compiler {compiler!r} not found",
            step="build",
            hint="Install mingw-w64 (mingw-w64-gcc on Arch, gcc-mingw-w64-x86-64 on "
            "Debian/Ubuntu) or set CC to a mingw-w64 gcc",
        )

    out.parent.mkdir(parents=True, exist_ok=True)
    tmp = out.with_name(out.name + ".tmp")
    argv = [resolved, "-O2", "-s", "-municode", "-mwindows", "-o", str(tmp), str(src)]
    result = run(argv, capture_output=True, text=True, check=False)
    if result.returncode != 0:
        tmp.unlink(missing_ok=True)
        raise ShimError(
            f"cannot build {out.name} with {resolved}: {result.stderr.strip()}",
            step="build",
            hint=_REPORT_HINT,
        )
    os.replace(tmp, out)
    stamp.write_text(digest + "\n", encoding="ascii")
    return out


def _atomic_copy(src: Path, dest: Path) -> None:
    """Copy ``src`` to ``dest`` through a temporary sibling."""
    dest.parent.mkdir(parents=True, exist_ok=True)
    temporary = dest.with_name(dest.name + ".tmp")
    try:
        shutil.copyfile(src, temporary)
        os.replace(temporary, dest)
    except BaseException:
        temporary.unlink(missing_ok=True)
        raise


def extract_games_exe(
    installer: Path,
    dest: Path,
    *,
    which: Callable[[str], str | None] = shutil.which,
    run: Callable[..., subprocess.CompletedProcess[str]] = subprocess.run,
) -> Path:
    """Extract ``Games.exe`` from a launcher installer and copy it to ``dest``."""
    sevenz = sevenzip.find(which=which)
    if not sevenz:
        raise ShimError(
            "7-Zip not found",
            step="extract",
            hint="Install p7zip or 7zip so Games.exe can be extracted from the launcher installer",
        )

    with tempfile.TemporaryDirectory(prefix="endfield-7z-") as tmpdir:
        result = run(
            [sevenz, "x", "-y", f"-o{tmpdir}", str(installer), "-r", "Games.exe"],
            capture_output=True,
            text=True,
            check=False,
        )
        if result.returncode != 0:
            raise ShimError(
                f"cannot extract Games.exe with {sevenz}: {result.stderr.strip()}",
                step="extract",
                hint=_REPORT_HINT,
            )
        candidates = [path for path in Path(tmpdir).rglob("Games.exe") if path.is_file()]
        if not candidates:
            raise ShimError(
                "the launcher installer does not contain Games.exe",
                step="extract",
                hint=_REPORT_HINT,
            )
        extracted = candidates[0]
        with open(extracted, "rb") as fh:
            if fh.read(2) != b"MZ":
                raise ShimError(
                    f"{extracted.name} is not a Windows executable",
                    step="extract",
                    hint=_REPORT_HINT,
                )
        _atomic_copy(extracted, dest)
    return dest


def _obtain_games(
    prefix: Path,
    cache: Path,
    *,
    fetch_json: Callable[[str], Any],
    download: Callable[[str, Path], Any],
    which: Callable[[str], str | None],
    extract_run: Callable[..., subprocess.CompletedProcess[str]],
) -> Path:
    """Return a local ``Games.exe``: the cache, the prefix, or a fresh download."""
    if cache.is_file():
        return cache

    installed = find_games_exe(prefix)
    if installed is not None:
        _atomic_copy(installed, cache)
        return cache

    try:
        data = fetch_json(LAUNCHER_API)
    except Exception as exc:  # noqa: BLE001 - network, TLS, HTTP and JSON errors all land here
        raise ShimError(
            f"cannot ask the launcher API for the installer: {exc}",
            step="fetch",
            hint=_NETWORK_HINT,
        ) from exc
    url = data.get("exe_url") if isinstance(data, dict) else None
    if not isinstance(url, str) or not installer_host_allowed(url):
        raise ShimError(
            "the launcher API did not return an exe_url on an allowed host",
            step="fetch",
            hint=_REPORT_HINT,
        )

    cache.parent.mkdir(parents=True, exist_ok=True)
    fd, name = tempfile.mkstemp(prefix="launcher-", suffix=".exe", dir=cache.parent)
    os.close(fd)
    installer = Path(name)
    try:
        try:
            download(url, installer)
        except Exception as exc:  # noqa: BLE001
            raise ShimError(
                f"cannot download the launcher installer: {exc}",
                step="fetch",
                hint=_NETWORK_HINT,
            ) from exc
        extract_games_exe(installer, cache, which=which, run=extract_run)
    finally:
        installer.unlink(missing_ok=True)
    return cache


def ensure_files(
    prefix: Path,
    *,
    data_dir: Path,
    fetch_json: Callable[[str], Any] | None = None,
    download: Callable[[str, Path], Any] | None = None,
    extract_run: Callable[..., subprocess.CompletedProcess[str]] = subprocess.run,
    source: Path | None = None,
    cc: str | None = None,
    which: Callable[[str], str | None] = shutil.which,
    run: Callable[..., subprocess.CompletedProcess[str]] = subprocess.run,
) -> ShimFiles:
    """Build, obtain and stage the shim and ``Games.exe`` for ``prefix``."""
    if fetch_json is None:
        fetch_json = _dl.fetch_json
    if download is None:

        def _download(url: str, dest: Path) -> Any:
            return _dl.download(url, dest, redirect_allowed=installer_host_allowed)

        download = _download

    shim_cache = data_dir / "shim" / "suspend_start.exe"
    build_shim(shim_cache, source=source, cc=cc, which=which, run=run)

    games_cache = data_dir / "shim" / "Games.exe"
    games = _obtain_games(
        prefix,
        games_cache,
        fetch_json=fetch_json,
        download=download,
        which=which,
        extract_run=extract_run,
    )

    stage = prefix.joinpath(*_STAGE_PARTS)
    staged_shim = stage / "suspend_start.exe"
    staged_games = stage / "Games.exe"
    stage.mkdir(parents=True, exist_ok=True)
    for src, dest in ((shim_cache, staged_shim), (games, staged_games)):
        if not dest.is_file() or not filecmp.cmp(src, dest, shallow=False):
            shutil.copyfile(src, dest)

    return ShimFiles(shim=staged_shim, games=staged_games)
