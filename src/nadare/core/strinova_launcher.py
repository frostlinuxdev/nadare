# SPDX-License-Identifier: GPL-3.0-or-later
# Copyright (C) 2026 Nadare contributors
"""Obtain Strinova's official launcher and point it at Nadare's game install.

Strinova only signs in while its publisher's launcher is running, so Nadare keeps
downloading the game itself and starts ``C:\\Strinova\\Strinova.exe`` instead. The launcher's
NSIS installer unpacks straight into that directory with 7-Zip (no inner archive), the
game folder is linked as ``C:\\Strinova\\Game``, and ``config.ini`` records the version
Nadare installed so the launcher offers START instead of a download.
"""

from __future__ import annotations

import os
import re
import shutil
import stat
import subprocess
import tempfile
from collections.abc import Callable
from pathlib import Path
from typing import Any

from . import download as _dl
from . import process, sevenzip
from .events import Cancelled, Event

#: Official Strinova launcher installer: the ``downloadUrl`` of the publisher's
#: ``Launcher_Release/lastversion.json`` (what strinova.com's download button uses).
INSTALLER_URL = (
    "https://resource-download.strinova.com/Client/Win/GameDepot/Launcher_Release/"
    "1.1.19.667/Strinova_Installer_Release_1.1.19.667.exe"
)

#: Pinned from the installer fetched 2026-11-21 (144231424 bytes); update together
#: with :data:`INSTALLER_URL`.
INSTALLER_SHA256 = "7934ef7f681f486cc43048a514970af8b544beec0a2d48ad1508aaf445b87d22"

#: Size in bytes of the pinned installer; used by the maintainer canary.
INSTALLER_SIZE = 144231424

#: The launcher's executable name in the installer payload and under ``C:\Strinova``.
LAUNCHER_EXE = "Strinova.exe"

#: The launcher's own folder under the prefix.
LAUNCHER_DIR = Path("drive_c") / "Strinova"

#: The link the launcher follows to the game Nadare installed.
GAME_LINK = "Game"

#: The launcher's settings file, next to its executable.
CONFIG_NAME = "config.ini"

#: The section the launcher's version keys live in.
CONFIG_SECTION = "General"

#: 7-Zip drops the installer's temporary plugin folder here; the installed launcher has none.
_INSTALLER_TEMP = "$PLUGINSDIR"

#: Sibling folder the payload is staged in before it is copied into ``C:\Strinova``.
_STAGE_DIR = ".Strinova.nadare-tmp"


class StrinovaLauncherError(RuntimeError):
    """The Strinova launcher could not be downloaded, unpacked or set up."""


def launcher_dir(prefix: Path) -> Path:
    """Return the launcher's own folder inside ``prefix`` (``C:\\Strinova``)."""
    return Path(prefix) / LAUNCHER_DIR


def launcher_path(prefix: Path) -> Path:
    """Return the launcher executable inside ``prefix``."""
    return launcher_dir(prefix) / LAUNCHER_EXE


def game_link(prefix: Path) -> Path:
    """Return the launcher's ``Game`` link inside ``prefix``."""
    return launcher_dir(prefix) / GAME_LINK


def config_path(prefix: Path) -> Path:
    """Return the launcher's ``config.ini`` inside ``prefix``."""
    return launcher_dir(prefix) / CONFIG_NAME


def _sha256(path: Path, cancel: Any = None) -> str:
    """Return the hex SHA-256 of ``path`` (a cancel stops a large hash between blocks)."""
    return _dl.hash_file(path, cancel=cancel)["sha256"]


def _normalise_pin(value: str) -> str:
    """The pin as compared and reported: hex digits are case-insensitive."""
    return str(value or "").strip().lower()


def _pin_is_valid(value: str) -> bool:
    """A pinned installer hash is exactly 64 lowercase hex digits."""
    text = _normalise_pin(value)
    return len(text) == 64 and all(char in "0123456789abcdef" for char in text)


def _check_cancel(cancel: Any) -> None:
    """Raise :class:`Cancelled` when the player asked to stop."""
    if cancel is not None:
        cancel.raise_if_cancelled()


def _step(emit: Callable[[Any], None] | None, message: str) -> None:
    """Name a silent stage on the Play page (the download and the unpack take a while)."""
    if emit is not None:
        emit(Event("step", message))


def _discard(*paths: Path) -> None:
    """Remove what a failed attempt left; a cleanup problem must not hide the real error."""
    for path in paths:
        try:
            path.unlink(missing_ok=True)
        except OSError:
            pass


def _fetch_installer(
    cache: Path, download: Callable[[str, Path], Any], cancel: Any = None
) -> Path:
    """Download the installer into ``cache`` once and verify its hash.

    The fixed ``cache.part`` name is the downloader's own staging file, so a failed
    attempt cannot leave a second ``.part.part`` file behind. A cancel keeps it: the
    next Play resumes from those bytes.
    """
    # The installer is extracted and its files are then run, so refuse a build with no usable
    # pin before asking the publisher for anything.
    sha = _normalise_pin(INSTALLER_SHA256)
    if not _pin_is_valid(sha):
        raise StrinovaLauncherError(
            "the Strinova installer has no valid checksum in this build; "
            "Nadare will not unpack an unpinned installer"
        )
    part = cache.with_name(cache.name + ".part")
    if cache.is_file():
        try:
            if _sha256(cache, cancel) == sha:
                return cache
        except OSError as exc:
            _discard(cache)
            raise StrinovaLauncherError(
                f"cannot read the cached Strinova installer: {exc}"
            ) from exc
        _discard(cache)

    try:
        cache.parent.mkdir(parents=True, exist_ok=True)
        download(INSTALLER_URL, cache)
        actual = _sha256(cache, cancel)
        if actual != sha:
            raise StrinovaLauncherError(
                f"{cache.name} SHA-256 mismatch: expected {sha}, got {actual}"
            )
        _check_cancel(cancel)
    except Cancelled:
        # Keep the partial download: the downloader's .part resumes on the next Play.
        raise
    except StrinovaLauncherError:
        _discard(cache, part)
        raise
    except (OSError, _dl.DownloadError) as exc:
        _discard(cache, part)
        raise StrinovaLauncherError(f"cannot download the Strinova launcher: {exc}") from exc
    except BaseException:
        _discard(cache, part)
        raise
    return cache


def _extract(
    sevenz: str, archive: Path, outdir: Path, run: Callable[..., Any]
) -> Path:
    """Unpack the NSIS installer into ``outdir``; the archive is the last argument."""
    try:
        outdir.mkdir(parents=True, exist_ok=True)
        result = run(
            [sevenz, "x", "-y", f"-o{outdir}", str(archive)],
            env=process.child_environ(),
            capture_output=True,
            text=True,
            check=False,
        )
    except (OSError, subprocess.SubprocessError) as exc:
        raise StrinovaLauncherError(
            f"cannot extract {archive.name} with {sevenz}: {exc}"
        ) from exc
    if result.returncode != 0:
        raise StrinovaLauncherError(
            f"cannot extract {archive.name} with {sevenz}: {result.stderr.strip()}"
        )
    return outdir


def _copy_payload(src: Path, dest: Path) -> None:
    """Copy the staged launcher into ``dest``, writing ``Strinova.exe`` last.

    If the copy is interrupted before the executable lands, the next Play sees no
    launcher and unpacks again instead of trusting a half-copied folder.
    """
    staged_exe = dest / (LAUNCHER_EXE + ".nadare-tmp")
    try:
        dest.mkdir(parents=True, exist_ok=True)
        entries = sorted(
            (entry for entry in src.iterdir() if entry.name != _INSTALLER_TEMP),
            key=lambda entry: (entry.name == LAUNCHER_EXE, entry.name),
        )
        for entry in entries:
            if entry.name == LAUNCHER_EXE:
                continue
            target = dest / entry.name
            if target.is_symlink():
                target.unlink()
            if entry.is_dir() and not entry.is_symlink():
                shutil.copytree(entry, target, dirs_exist_ok=True)
            else:
                shutil.copy2(entry, target)
        exe = dest / LAUNCHER_EXE
        if exe.is_symlink():
            # A dangling link would make copytree/copy2 write through it.
            exe.unlink()
        if os.path.lexists(staged_exe):
            # A stale link here would make copy2 write through it.
            staged_exe.unlink()
        shutil.copy2(src / LAUNCHER_EXE, staged_exe)
        os.replace(staged_exe, exe)
    except (OSError, shutil.Error) as exc:
        try:
            if os.path.lexists(staged_exe):
                staged_exe.unlink()
        except OSError:
            pass
        raise StrinovaLauncherError(f"cannot install the Strinova launcher: {exc}") from exc


def _resolved(path: Path) -> Path:
    """``path`` with every link resolved; the plain absolute path when that fails."""
    try:
        return Path(path).resolve()
    except OSError:
        return Path(os.path.abspath(Path(path)))


def _is_the_link_path(link: Path, game_dir: Path) -> bool:
    """Whether ``game_dir`` is the link's own path (an adopted official install).

    The link itself is never followed here: following it would make the normal case
    (the link points at the game) look the same as the adopted one.
    """
    link_path = _resolved(link.parent) / link.name
    game_path = _resolved(game_dir.parent) / game_dir.name
    return link_path == _resolved(game_dir) or link_path == game_path


def _link_target(link: Path, game_dir: Path, prefix: Path) -> str:
    """The link text: relative inside the prefix, otherwise the resolved absolute path."""
    resolved = _resolved(game_dir)
    try:
        resolved.relative_to(_resolved(Path(prefix) / "drive_c"))
    except ValueError:
        return str(resolved)
    # Relative links survive a cross-disk Move of the prefix (relocate.py). Both sides
    # are resolved so a symlinked prefix cannot make the link escape it.
    return os.path.relpath(resolved, _resolved(link.parent))


def _points_at(link: Path, target: str) -> bool:
    """Whether the link already carries exactly the wanted text."""
    try:
        return os.readlink(link) == target
    except OSError:
        return False


def _link_game(link: Path, game_dir: Path, prefix: Path) -> None:
    """Point ``link`` at ``game_dir``, repairing a wrong, broken or non-canonical link."""
    target = _link_target(link, game_dir, prefix)
    if link.is_symlink():
        if _points_at(link, target):
            return
        if _is_the_link_path(link, game_dir):
            # An adopted install whose own ``Game`` folder is Nadare's install dir: a
            # link here would point at itself.
            return
    elif _is_the_link_path(link, game_dir):
        return
    elif link.exists():
        raise StrinovaLauncherError(
            f"{link} already exists and is not a link; move it away so the launcher can "
            "use Nadare's game folder"
        )
    temporary = link.with_name(f".{link.name}.nadare-tmp")
    try:
        link.parent.mkdir(parents=True, exist_ok=True)
        if os.path.lexists(temporary):
            temporary.unlink()
        os.symlink(target, temporary, target_is_directory=True)
        os.replace(temporary, link)
    except OSError as exc:
        try:
            if os.path.lexists(temporary):
                temporary.unlink()
        except OSError:
            pass
        raise StrinovaLauncherError(f"cannot link Nadare's game folder as {link}: {exc}") from exc


def _refuse_launcher_overlap(prefix: Path, game_dir: Path) -> None:
    """Refuse a game folder that is inside the launcher's own folder.

    The one exception is the ``Game`` link itself: that is how an adopted official
    install looks, and the launcher already follows the link to the game. A folder
    below the link is refused because linking it would shift what the link resolves to
    (``Game`` would point at ``Game/sub`` inside itself).
    """
    launcher = _resolved(launcher_dir(prefix))
    game = _resolved(Path(game_dir))
    # The exception and the "below Game" case are decided on the lexical path: Game is a
    # symlink after the first Play, and following it would carry Game/sub (or a link that
    # points into C:\Strinova) out of the launcher folder, so linking it would break the
    # install. Only the link's own path is compared, never what it points at.
    lexical_launcher = Path(os.path.abspath(launcher_dir(prefix)))
    lexical_link = Path(os.path.abspath(game_link(prefix)))
    lexical_game = Path(os.path.abspath(Path(game_dir)))
    if lexical_game == lexical_link or _is_the_link_path(game_link(prefix), Path(game_dir)):
        return
    if (
        lexical_link in lexical_game.parents
        or lexical_game == lexical_launcher
        or lexical_launcher in lexical_game.parents
        or game == launcher
        or launcher in game.parents
        # /home vs /var/home: an ancestor that resolves to the launcher folder. Resolving an
        # ancestor never follows Game itself, so Game/sub is caught by any route.
        or any(_resolved(parent) == launcher for parent in lexical_game.parents)
    ):
        raise StrinovaLauncherError(
            f"the game folder {game_dir} is inside the Strinova launcher's own folder; "
            "choose another install folder"
        )


_LINE_BREAK = re.compile(r"\r\n|\r|\n")


def _split_lines(text: str) -> list[str]:
    """Split INI text on CRLF/LF/CR only; ``str.splitlines`` also cuts 0x85 and friends."""
    lines = _LINE_BREAK.split(text)
    if lines and lines[-1] == "":
        lines.pop()  # the file ends with a line break: that is not an extra blank line
    return lines


#: config.ini is a few hundred bytes; anything this large is not the launcher's file.
_CONFIG_MAX_BYTES = 1 << 20


def _read_config_bytes(path: Path) -> bytes | None:
    """The bytes of ``path``, or ``None`` when it does not exist.

    The file sits where the game and its launcher can write. A link there is refused rather
    than followed (Nadare then replaces the file, and would read another file's text), and a
    FIFO or device must not block the launch.
    """
    flags = os.O_RDONLY | os.O_NOFOLLOW | os.O_NONBLOCK | getattr(os, "O_CLOEXEC", 0)
    try:
        fd = os.open(path, flags)
    except FileNotFoundError:
        return None
    except OSError as exc:
        if os.path.islink(path):
            raise StrinovaLauncherError(
                f"{path} is a link; move it away so Nadare and the launcher use a plain "
                f"{CONFIG_NAME}"
            ) from exc
        raise StrinovaLauncherError(f"cannot read {path}: {exc}") from exc
    try:
        info = os.fstat(fd)
        if not stat.S_ISREG(info.st_mode):
            raise StrinovaLauncherError(
                f"{path} is not a regular file; move it away so the launcher can write its "
                f"{CONFIG_NAME}"
            )
        if info.st_size > _CONFIG_MAX_BYTES:
            raise StrinovaLauncherError(
                f"{path} is {info.st_size} bytes, too large for the launcher's {CONFIG_NAME}"
            )
        chunks = []
        total = 0
        while chunk := os.read(fd, 65536):
            total += len(chunk)
            if total > _CONFIG_MAX_BYTES:
                raise StrinovaLauncherError(
                    f"{path} grew past {_CONFIG_MAX_BYTES} bytes while it was read"
                )
            chunks.append(chunk)
        return b"".join(chunks)
    except OSError as exc:
        raise StrinovaLauncherError(f"cannot read {path}: {exc}") from exc
    finally:
        os.close(fd)


def _config_lines(path: Path) -> tuple[list[str], str, bytes]:
    """Return ``path``'s lines, its encoding, and its BOM; a missing file is empty UTF-8."""
    raw = _read_config_bytes(path)
    if raw is None:
        return [], "utf-8", b""
    if raw.startswith(b"\xff\xfe"):
        try:
            return _split_lines(raw[2:].decode("utf-16-le")), "utf-16-le", raw[:2]
        except UnicodeDecodeError:
            pass
    if raw.startswith(b"\xfe\xff"):
        try:
            return _split_lines(raw[2:].decode("utf-16-be")), "utf-16-be", raw[:2]
        except UnicodeDecodeError:
            pass
    if raw.startswith(b"\xef\xbb\xbf"):
        try:
            return _split_lines(raw[3:].decode("utf-8")), "utf-8", raw[:3]
        except UnicodeDecodeError:
            pass
    try:
        return _split_lines(raw.decode("utf-8")), "utf-8", b""
    except UnicodeDecodeError:
        # Keep a genuine single-byte file (Windows-1252/GBK paths) byte-for-byte.
        return _split_lines(raw.decode("latin-1")), "latin-1", b""


def _section_bounds(lines: list[str]) -> tuple[int, int] | None:
    """Return ``(header, end)`` for ``[General]``; ``end`` is the next section or EOF."""
    start = None
    for index, line in enumerate(lines):
        if line.strip().lower() == f"[{CONFIG_SECTION.lower()}]":
            start = index
            break
    if start is None:
        return None
    for index in range(start + 1, len(lines)):
        stripped = lines[index].strip()
        if stripped.startswith("[") and stripped.endswith("]"):
            return start, index
    return start, len(lines)


def _key_name(line: str) -> str:
    """The key of an INI line, or ``""`` for a comment/blank/malformed line."""
    stripped = line.strip()
    if not stripped or stripped.startswith((";", "#")) or "=" not in line:
        return ""
    return line.split("=", 1)[0].strip().lower()


def _clean_version(version: str) -> str:
    """A version safe to write on one INI line."""
    text = str(version).strip()
    if not text or "\r" in text or "\n" in text:
        raise StrinovaLauncherError(
            "the installed game version is empty or malformed; "
            "cannot tell the launcher which version is installed"
        )
    return text


_STRICT_VERSION = re.compile(r"[0-9]{1,9}(?:\.[0-9]{1,9}){0,7}")
#: The launcher's idle stage; PATCHSTAGE and VERIFYSTAGE mean a patch is under way.
_IDLE_STAGE = "INITSTAGE"


def version_key(text: object) -> tuple[int, ...] | None:
    """``1.12.10.0`` as numbers, so it sorts after ``1.12.9.0``; ``None`` when malformed.

    Trailing zero parts do not count: ``1.12.2`` and ``1.12.2.0`` are the same version.
    """
    if not isinstance(text, str) or not _STRICT_VERSION.fullmatch(text.strip()):
        return None
    parts = [int(part) for part in text.strip().split(".")]
    while len(parts) > 1 and parts[-1] == 0:
        parts.pop()
    return tuple(parts)


class LauncherState:
    """What the launcher's ``[General]`` section says about the game it manages.

    A duplicated key counts with its last value, as a last-value-wins INI reader sees it.
    """

    def __init__(self, values: dict[str, str]) -> None:
        self.current = values.get("currentgameversion", "").strip()
        self.target = values.get("targetgameversion", "").strip()
        self.transition = values.get("transitiongameversion", "").strip()
        self.stage = values.get("currentstage", "").strip()

    @property
    def patching(self) -> bool:
        """The launcher is part-way through a patch (or about to start one)."""
        return bool(
            (self.stage and self.stage.upper() != _IDLE_STAGE)
            or self.transition
            or (self.target and self.target != self.current)
        )

    def heading_past(self, version: tuple[int, ...]) -> bool:
        """A patch in progress whose destination is newer than ``version``."""
        for value in (self.target, self.transition):
            key = version_key(value)
            if key is not None and key > version:
                return True
        return False


def _state_from_lines(lines: list[str]) -> LauncherState:
    values: dict[str, str] = {}
    bounds = _section_bounds(lines)
    if bounds is not None:
        start, end = bounds
        for line in lines[start + 1 : end]:
            key = _key_name(line)
            if key:
                values[key] = line.split("=", 1)[1]
    return LauncherState(values)


def _links_to(prefix: Path, game_dir: Path) -> bool:
    """Whether the launcher's ``Game`` folder is ``game_dir`` (so its config describes it)."""
    link = game_link(prefix)
    game_dir = Path(os.path.abspath(Path(game_dir).expanduser()))
    try:
        if link.is_symlink():
            return _resolved(link) == _resolved(game_dir)
        return _is_the_link_path(link, game_dir)
    except OSError:
        return False


def sync_verified_version(prefix: Path, game_dir: Path, version: str) -> bool:
    """Tell the launcher the version Nadare just verified in ``game_dir``.

    After an install, update or repair every file matches ``version``, so a launcher patch
    that stopped part-way is moot. Only an existing config whose ``Game`` folder is
    ``game_dir`` is touched; nothing is created. Returns whether the file was written.
    """
    prefix = Path(prefix)
    path = config_path(prefix)
    if not _links_to(prefix, Path(game_dir)) or not os.path.lexists(path):
        return False
    return _merge_config(path, version, verified=True)


def newer_installed_version(prefix: Path, game_dir: Path, recorded: object) -> str | None:
    """The version the launcher finished installing, when it is newer than ``recorded``.

    The launcher patches the game in place and then says so in ``config.ini``. Only an idle
    launcher whose ``Game`` folder is ``game_dir`` counts, and only over a record Nadare made
    itself: without one there is nothing to compare, and Nadare never trusts the launcher for
    a first install. A config that cannot be read safely counts as no answer.
    """
    known = version_key(recorded)
    if known is None or not _links_to(Path(prefix), Path(game_dir)):
        return None
    try:
        lines, _encoding, _bom = _config_lines(config_path(Path(prefix)))
    except StrinovaLauncherError:
        return None
    state = _state_from_lines(lines)
    current = version_key(state.current)
    if state.patching or current is None or current <= known:
        return None
    return state.current


def _merge_config(path: Path, version: str, *, verified: bool = False) -> bool:
    """Tell the launcher which version Nadare installed, unless that would undo its own work.

    The launcher updates the game itself (Strinova, 2026-11-27), so its ``config.ini`` may be
    ahead of Nadare's record. Writing the record back would make it patch files it already
    patched. The keys are written only when Nadare's version is newer than the launcher's (or
    the launcher has none), and never under a launcher patch heading past Nadare's version.
    When Nadare's newer, verified tree replaces a launcher patch that stopped part-way, the
    launcher's stage is set back to idle as well. Every other line is kept as written.

    ``verified`` says the folder is known to hold exactly ``version`` (Nadare just checked
    every file, or the config described another folder): the keys are then written whatever
    the launcher said before.

    Returns whether the file was written.
    """
    version = _clean_version(version)
    lines, encoding, bom = _config_lines(path)
    state = _state_from_lines(lines)
    ours = version_key(version)
    theirs = version_key(state.current)
    if ours is None:
        # Nothing to compare: only a launcher that names no version is told ours.
        if state.current or state.patching:
            return False
    elif verified:
        if theirs == ours and not state.patching and state.target in ("", state.current):
            return False  # already says so
    elif (theirs is not None and theirs >= ours) or state.heading_past(ours):
        return False
    wanted = [
        ("CurrentGameVersion", version),
        ("TargetGameVersion", version),
        ("bPatchFinished", "true"),
    ]
    if state.patching:
        wanted += [("TransitionGameVersion", ""), ("CurrentStage", _IDLE_STAGE)]
    bounds = _section_bounds(lines)
    if bounds is None:
        if lines and lines[-1].strip():
            lines.append("")
        lines.append(f"[{CONFIG_SECTION}]")
        lines.extend(f"{key}={value}" for key, value in wanted)
    else:
        start, end = bounds
        # Keep the keys next to the section's own values, above any blank separator.
        insert_at = end
        while insert_at > start + 1 and not lines[insert_at - 1].strip():
            insert_at -= 1
        missing = []
        for key, value in wanted:
            found = False
            for index in range(start + 1, end):
                if _key_name(lines[index]) == key.lower():
                    # Every duplicate: a last-value-wins parser would otherwise keep the old one.
                    lines[index] = f"{key}={value}"
                    found = True
            if not found:
                missing.append(f"{key}={value}")
        if missing:
            lines[insert_at:insert_at] = missing

    try:
        # The launcher writes CRLF; keep one style throughout, including a new/empty file.
        payload = bom + ("\r\n".join(lines) + "\r\n").encode(encoding)
        path.parent.mkdir(parents=True, exist_ok=True)
        fd, name = tempfile.mkstemp(prefix=path.name + ".", dir=path.parent)
        try:
            with os.fdopen(fd, "wb") as handle:
                handle.write(payload)
            os.replace(name, path)
        except BaseException:
            _discard(Path(name))
            raise
    except UnicodeEncodeError as exc:
        raise StrinovaLauncherError(
            f"cannot write {path}: the installed game version has characters the file's "
            f"{encoding} encoding cannot store"
        ) from exc
    except OSError as exc:
        raise StrinovaLauncherError(f"cannot write {path}: {exc}") from exc
    return True


def ensure_launcher(
    prefix: Path,
    game_dir: Path,
    *,
    version: str | None,
    data_dir: Path,
    download: Callable[[str, Path], Any] | None = None,
    which: Callable[[str], str | None] | None = None,
    run: Callable[..., subprocess.CompletedProcess[str]] | None = None,
    emit: Callable[[Any], None] | None = None,
    cancel: Any = None,
) -> Path:
    """Make sure Strinova's launcher is installed, linked to ``game_dir`` and up to date.

    Returns the launcher executable Nadare should start.
    """
    prefix = Path(prefix)
    game_dir = Path(os.path.abspath(Path(game_dir).expanduser()))
    _refuse_launcher_overlap(prefix, game_dir)
    launcher = launcher_path(prefix)
    try:
        if not (prefix / "drive_c").is_dir():
            # Never create the prefix here: a failed first launch would otherwise leave a
            # drive_c without system.reg that later looks like a complete prefix. The
            # backend creates it (see ``_ensure_game_drive``) before this call.
            raise StrinovaLauncherError(
                "the Wine prefix is not ready; set up Wine or Proton for this game in "
                "Settings, then press Play"
            )
        if not launcher.is_file():
            find_which = which or shutil.which
            run_it = run or subprocess.run
            sevenz = sevenzip.find(which=find_which)
            if not sevenz:
                raise StrinovaLauncherError(
                    "7-Zip not found; install p7zip or 7zip so the Strinova launcher can be "
                    "extracted from the official installer"
                )

            sha = _normalise_pin(INSTALLER_SHA256)
            if download is None:

                def _download(url: str, dest: Path) -> Any:
                    return _dl.download(
                        url,
                        dest,
                        expected_sha256=sha,
                        expected_size=INSTALLER_SIZE,
                        emit=emit,
                        cancel=cancel,
                    )

                download = _download

            _step(emit, "Downloading the Strinova launcher (one time)")
            cache = _fetch_installer(
                data_dir / "strinova" / "Strinova_Launcher_setup.exe", download, cancel
            )
            _check_cancel(cancel)
            _step(emit, "Installing the Strinova launcher")
            stage = launcher_dir(prefix).with_name(_STAGE_DIR)
            try:
                shutil.rmtree(stage, ignore_errors=True)
                payload = _extract(sevenz, cache, stage, run_it)
                if not (payload / LAUNCHER_EXE).is_file():
                    raise StrinovaLauncherError(
                        f"the official installer does not contain {LAUNCHER_EXE}"
                    )
                _check_cancel(cancel)
                _copy_payload(payload, launcher_dir(prefix))
            finally:
                shutil.rmtree(stage, ignore_errors=True)
            if not launcher.is_file():
                raise StrinovaLauncherError(
                    f"the official installer did not install {LAUNCHER_EXE}"
                )

        # A config written while Game pointed elsewhere describes that other folder.
        link = game_link(prefix)
        relinked = not _links_to(prefix, game_dir)
        if relinked and version is not None and (link.is_symlink() or not link.exists()):
            # The config first: if it cannot be written, Game still points where its
            # config says, and nothing records the other folder's version here.
            _merge_config(config_path(prefix), version, verified=True)
            _link_game(link, game_dir, prefix)
        else:
            _link_game(link, game_dir, prefix)  # refuses a real folder before any write
            if version is not None:
                _merge_config(config_path(prefix), version, verified=relinked)
        return launcher
    except StrinovaLauncherError:
        raise
    except (OSError, shutil.Error, subprocess.SubprocessError, _dl.DownloadError) as exc:
        raise StrinovaLauncherError(f"cannot prepare the Strinova launcher: {exc}") from exc
