# SPDX-License-Identifier: GPL-3.0-or-later
# Copyright (C) 2026 Nadare contributors
"""Nadare's own pinned ``umu-run``, used before any system copy (see :func:`resolve`).

umu-launcher ships its own Steam Linux Runtime container, so it can run a Proton
build on a host that lacks the 32-bit libraries that Proton needs when it is
started directly. This module downloads the published zipapp once (pinned by
sha256), verifies it, and exposes the resulting executable. Nothing here runs at
import time or from a status poll: :func:`ensure_managed` is only reached from a
launch/install path via :func:`resolve`.
"""

from __future__ import annotations

import fcntl
import hashlib
import logging
import os
import re
import shutil
import subprocess
import sys
import tarfile
import tempfile
from collections.abc import Callable, Iterable
from pathlib import Path

from . import download, hosting, paths, process
from .events import Cancel, Emitter, Event, emitter
from .runners import _github_asset_host_allowed

#: Pinned release, verified against the published asset.
UMU_VERSION = "1.4.4"
UMU_TAR_SHA256 = "eb590691841f7fad3fc3ad8fd5db4ccb87849fe7948e62b28ece7a4ee48cc851"

#: Steam Runtime tool app ids (a runner's ``require_tool_appid``) that the pinned
#: ``UMU_VERSION`` can map to a container runtime. Update this together with
#: ``UMU_VERSION``; ``scripts/check_runtime_canary.py`` warns when a new runner asks
#: for an id that is not listed.
SUPPORTED_RUNTIME_APPIDS: dict[int, str] = {
    1070560: "Steam Linux Runtime 1.0 (scout)",
    1391110: "Steam Linux Runtime 2.0 (soldier)",
    1628350: "Steam Linux Runtime 3.0 (sniper)",
    4183110: "Steam Linux Runtime 4.0",
}

#: Oldest system umu-run that understands Steam Runtime 4 (the recommended Proton
#: builds need it; 1.2.x, as older Bazzite images shipped, fails with "Failed to match ...
#: with a container runtime"). Bazzite 45.20261112 ships 1.4.4 (checked 2026-11-14, #1341).
MIN_SYSTEM_VERSION = (1, 3, 0)

#: The interpreter the zipapp's shebang names (and the one it will run under).
SHEBANG_PYTHON = "/usr/bin/python3"

#: Hidden entry point that runs the managed zipapp with Nadare's own interpreter (#1341).
SUBCOMMAND = "--umu-run"

#: The pinned archive the AppImage carries (``scripts/fetch_umu.sh``): the managed copy is
#: installed from it with no network (#1338). Source installs have none and download.
BUNDLED_ARCHIVE = Path(__file__).resolve().parents[1] / "assets" / (
    f"umu-launcher-{UMU_VERSION}-zipapp.tar"
)

#: The archive member we install; everything else in the tar is ignored.
_MEMBER = "umu/umu-run"

log = logging.getLogger(__name__)

#: umu prints these when it cannot fetch the Steam Runtime, then exits 0.
_SETUP_FAILURE_LINES = (
    "umu has not been setup for the user",
    "an internet connection is required to setup umu",
)

#: Shown when a prefix step failed because umu could not set itself up.
SETUP_HINT = (
    "Nadare could not download the Steam Runtime that umu needs from Valve's server "
    "(repo.steampowered.com). Check your internet connection, or try again later if "
    "the server is down."
)


#: Shown when umu does not know the Steam Runtime the selected Proton asks for
#: (an old umu with a new runner, or a future runtime Nadare's pinned umu predates).
RUNTIME_MISMATCH_HINT = (
    "The Proton runner needs a newer umu: this one does not know the Steam Runtime "
    "it asks for, so the game's Windows environment cannot start. Update "
    "umu-launcher (Nadare brings its own, so updating Nadare also works), or pick an "
    "older runner in the game's settings."
)


#: Shown when no umu could be found or set up on a desktop install: Nadare never falls back to
#: running Proton without it (#1338), so say what to do.
UNAVAILABLE_HINT = (
    "Nadare could not set up umu, which runs your games in the Steam Runtime with their "
    "fixes. Check your internet connection and press Play again, or install your "
    "distribution's umu-launcher package."
)


class UmuError(RuntimeError):
    """The managed umu copy could not be set up."""


def is_setup_failure_line(line: str) -> bool:
    """Whether ``line`` is one of umu's own "could not set itself up" messages."""
    if not line:
        return False
    lowered = line.lower()
    return any(marker in lowered for marker in _SETUP_FAILURE_LINES)


def is_runtime_mismatch_line(line: str) -> bool:
    """Whether ``line`` is umu's "no container runtime for this Proton" error."""
    if not line:
        return False
    lowered = line.lower()
    return "failed to match" in lowered and "container runtime" in lowered


def managed_path() -> Path:
    """Where Nadare keeps its own umu for this pinned version."""
    return paths.data_dir() / "umu" / UMU_VERSION / "umu-run"


def runtime_base() -> Path:
    """The UMU_FOLDERS_PATH base, separate from the versioned zipapps."""
    return paths.data_dir() / "umu-runtime"


def shared_runtime_roots() -> list[Path]:
    roots = [Path.home() / ".local/share/umu"]
    xdg = os.environ.get("XDG_DATA_HOME", "")
    if os.path.isabs(xdg):
        roots.append(Path(xdg) / "umu")
    return list(dict.fromkeys(roots))


def runtime_roots() -> list[Path]:
    return [runtime_base() / "umu", *shared_runtime_roots()]


def uses_owned_runtime(executable: str) -> bool:
    from . import userns

    return Path(executable) == managed_path() and not userns.runtime_profile_outdated()


def _has_runtime(root: Path) -> bool:
    return any(root.glob("*/pressure-vessel/libexec/steam-runtime-tools-0/srt-bwrap"))


def _migrate_runtime(emit: Emitter | None = None) -> None:
    """Copy once while holding destination and source locks. Never remove shared data.

    Called on launch workers, never a status poll. A failed attempt leaves umu to download.
    The marker and lock live outside umu's tree so its updates cannot replace them.
    """
    from . import diskspace

    base = runtime_base()
    temporary = None
    try:
        base.mkdir(parents=True, exist_ok=True)
        fd = os.open(base / ".migration.lock", os.O_CREAT | os.O_RDWR | os.O_NOFOLLOW, 0o600)
        try:
            fcntl.flock(fd, fcntl.LOCK_EX)
            destination = base / "umu"
            marker = base / ".migration-attempted"
            if _has_runtime(destination) or marker.exists():
                return
            marker.touch()
            for source in shared_runtime_roots():
                if source.is_symlink() or not _has_runtime(source):
                    continue
                source_fd = os.open(
                    source / "umu.lock", os.O_CREAT | os.O_RDWR | os.O_NOFOLLOW, 0o600
                )
                try:
                    fcntl.flock(source_fd, fcntl.LOCK_EX | fcntl.LOCK_NB)
                    needed = diskspace.tree_bytes(source)
                    if shutil.disk_usage(base).free < needed * 1.05:
                        return
                    emitter(emit)(Event(
                        "step", "Copying the shared Steam Runtime for Nadare (one time)…"
                    ))
                    temporary = Path(tempfile.mkdtemp(prefix=".copy-", dir=base))
                    result = subprocess.run(
                        ["cp", "-a", "--reflink=auto", "--", str(source) + "/.", str(temporary)],
                        stdin=subprocess.DEVNULL, stdout=subprocess.DEVNULL,
                        stderr=subprocess.DEVNULL, check=False, env=process.child_environ(),
                    )
                    if result.returncode != 0 or not _has_runtime(temporary):
                        return
                    # An empty destination is harmless; a nonempty one is never overwritten.
                    os.replace(temporary, destination)
                    temporary = None
                    return
                finally:
                    os.close(source_fd)
        finally:
            os.close(fd)
    except OSError:
        log.info("Could not copy the shared umu runtime; umu will download it", exc_info=True)
    finally:
        if temporary is not None:
            shutil.rmtree(temporary, ignore_errors=True)


def prepare_runtime(
    executable: str, env: dict[str, str], emit: Emitter | None = None
) -> bool:
    """Route only Nadare's umu to its runtime; slow work belongs on a launch worker.

    Returns True when the executable is Nadare's own copy (its runtime was routed here).
    """
    if not uses_owned_runtime(executable):
        return False
    _migrate_runtime(emit)
    env["UMU_FOLDERS_PATH"] = str(runtime_base())
    if not runtime_downloaded(executable):
        emitter(emit)(Event("step", FIRST_RUNTIME_NOTE))
    return True


def shebang_python_ok() -> bool:
    """Whether the interpreter the zipapp's shebang names (:data:`SHEBANG_PYTHON`) can run.

    NixOS has none, not even inside ``appimage-run``'s environment (#1599).
    """
    return os.path.isfile(SHEBANG_PYTHON) and os.access(SHEBANG_PYTHON, os.X_OK)


def command() -> list[str]:
    """How to start this build's hidden ``--umu-run`` (frozen bundle or source)."""
    if getattr(sys, "frozen", False):
        return [sys.executable, SUBCOMMAND]
    return [sys.executable, "-m", "nadare", SUBCOMMAND]


def run_argv(executable: str | Path, args: Iterable[str]) -> list[str]:
    """The argv that runs umu with ``args``.

    Normally the zipapp itself. When its shebang interpreter is missing (NixOS), Nadare's
    own copy runs it in-process through the hidden ``--umu-run`` instead (#1341 item 3).
    """
    if Path(executable) == managed_path() and not shebang_python_ok():
        return [*command(), *args]
    return [str(executable), *args]


def _run_zipapp(zipapp: Path, args: list[str]) -> None:
    """Run the zipapp's ``__main__`` in this process, with ``sys.argv`` as a script gets it."""
    import runpy

    saved = sys.argv
    saved_path = list(sys.path)
    sys.argv = [str(zipapp), *args]
    try:
        runpy.run_path(str(zipapp), run_name="__main__")
    finally:
        sys.argv = saved
        sys.path[:] = saved_path


def main(argv: list[str] | None = None, *, _zipapp: Path | None = None) -> int:
    """Run the managed umu zipapp with Nadare's interpreter (hidden :data:`SUBCOMMAND`)."""
    zipapp = managed_path() if _zipapp is None else _zipapp
    if not (zipapp.is_file() and os.access(zipapp, os.X_OK)):
        print("nadare: umu is not set up yet", file=sys.stderr)
        return 1
    args = list(sys.argv[1:] if argv is None else argv)
    try:
        _run_zipapp(zipapp, args)
    except SystemExit as exc:
        code = exc.code
        if code is None:
            return 0
        if isinstance(code, int):
            return code
        print(code, file=sys.stderr)
        return 1
    return 0


def managed() -> Path | None:
    """The installed managed umu, or ``None``. Pure lookup, never downloads.

    A copy is usable without the interpreter its shebang names: :func:`run_argv` then
    starts Nadare's own ``--umu-run`` instead (#1341 item 3).
    """
    path = managed_path()
    if not (path.is_file() and os.access(path, os.X_OK)):
        return None
    return path


def _extract_member(
    archive: Path, destination: Path, check: Callable[[Path], None] | None = None
) -> None:
    """Copy only the regular ``umu/umu-run`` member to ``destination``.

    ``check`` runs on the private candidate before it is published: ``managed()`` trusts any
    executable file at ``destination``, so nothing unchecked may ever appear there (#974).
    """
    tmp: Path | None = None
    try:
        with tarfile.open(archive, "r:*") as tar:
            try:
                member = tar.getmember(_MEMBER)
            except KeyError:
                raise UmuError(f"the umu archive has no {_MEMBER}") from None
            if (
                not member.isfile()
                or member.issym()
                or member.islnk()
                or member.name.startswith(("/", "\\"))
                or ".." in Path(member.name).parts
            ):
                raise UmuError("the umu archive entry is not a safe regular file")
            source = tar.extractfile(member)
            if source is None:
                raise UmuError("the umu archive entry cannot be read")
            destination.parent.mkdir(parents=True, exist_ok=True)
            fd, name = tempfile.mkstemp(prefix=".umu-run-", dir=destination.parent)
            tmp = Path(name)
            with os.fdopen(fd, "wb") as out, source:
                shutil.copyfileobj(source, out)
        tmp.chmod(0o755)
        if check is not None:
            check(tmp)
        os.replace(tmp, destination)
        tmp = None
    except UmuError:
        raise
    except (tarfile.TarError, OSError) as exc:
        raise UmuError(f"could not extract umu: {exc}") from exc
    finally:
        if tmp is not None:
            try:
                tmp.unlink(missing_ok=True)
            except OSError:
                pass


def _smoke_test(python: str | None, executable: Path) -> None:
    # Keep imports, environment changes and a hung probe out of the GUI process. The frozen
    # entry point also supports ``-m``; this checks the private candidate before publishing it.
    argv = (
        [sys.executable, "-m", "nadare.core.umu", str(executable)]
        if python is None
        else [python, str(executable), "--version"]
    )
    try:
        result = subprocess.run(
            argv,
            env=process.sanitize_game_env(process.child_environ()),
            capture_output=True,
            text=True,
            timeout=30,
            check=False,
        )
    except (OSError, subprocess.SubprocessError) as exc:
        raise UmuError(f"the downloaded umu does not run: {exc}") from exc
    if result.returncode != 0:
        raise UmuError("the downloaded umu does not run")


def _bundled_archive() -> Path | None:
    """The AppImage's own umu archive when it is there and matches the pin, else ``None``."""
    try:
        if not BUNDLED_ARCHIVE.is_file():
            return None
        digest = hashlib.sha256(BUNDLED_ARCHIVE.read_bytes()).hexdigest()
    except OSError:
        return None
    if digest != UMU_TAR_SHA256:
        log.warning("bundled umu archive does not match the pinned sha256; not using it")
        return None
    return BUNDLED_ARCHIVE


def ensure_managed(
    *, emit: Emitter | None = None, cancel: Cancel | None = None
) -> Path:
    """Return an installed managed umu, downloading and verifying it once.

    Raises :class:`UmuError` when the download fails its pinned hash, the archive is
    malformed, or the resulting zipapp cannot run. A failed install leaves nothing at
    :func:`managed_path`. Without ``python3`` the zipapp is checked with Nadare's own
    interpreter (#1341 item 3), which is also how the launch runs it.
    """
    existing = managed()
    if existing is not None:
        return existing

    python = shutil.which("python3") if shebang_python_ok() else None

    say = emitter(emit)
    destination = managed_path()
    try:
        destination.parent.mkdir(parents=True, exist_ok=True)
    except OSError as exc:
        raise UmuError(f"cannot create {destination.parent}: {exc}") from exc
    bundled = _bundled_archive()
    if bundled is not None:
        say(Event("step", "Setting up umu (bundled with Nadare)"))
        _extract_member(
            bundled, destination, check=lambda candidate: _smoke_test(python, candidate)
        )
        say(Event("done", "umu is ready", data={"path": str(destination)}))
        return destination
    cache = paths.cache_dir() / "umu"
    try:
        cache.mkdir(parents=True, exist_ok=True)
        # One archive per call: two first launches at once must not delete each other's download.
        fd, name = tempfile.mkstemp(prefix=f"umu-launcher-{UMU_VERSION}-", suffix=".tar", dir=cache)
        os.close(fd)
    except OSError as exc:
        raise UmuError(f"cannot use the cache folder {cache}: {exc}") from exc
    archive = Path(name)

    say(Event("step", "Setting up umu (one-time, <1 MB)"))
    try:
        result = download.download(
            hosting.umu_zipapp_url(),
            archive,
            expected_sha256=UMU_TAR_SHA256,
            headers={"Accept": "application/octet-stream"},
            emit=emit,
            cancel=cancel,
            redirect_allowed=_github_asset_host_allowed,
        )
    except (download.DownloadError, OSError) as exc:
        try:
            archive.unlink(missing_ok=True)
        except OSError:
            pass
        raise UmuError(f"could not download umu: {exc}") from exc

    try:
        _extract_member(
            result.path, destination, check=lambda candidate: _smoke_test(python, candidate)
        )
    finally:
        try:
            result.path.unlink(missing_ok=True)
        except OSError:
            pass

    say(Event("done", "umu is ready", data={"path": str(destination)}))
    return destination


def parse_version(text: str) -> tuple[int, int, int] | None:
    """Read ``(major, minor, patch)`` from ``umu-run --version`` output, else ``None``."""
    match = re.search(r"version\s+(\d+)\.(\d+)\.(\d+)", text or "")
    if not match:
        return None
    return int(match.group(1)), int(match.group(2)), int(match.group(3))


def system(
    configured: str = "umu-run",
    *,
    which=None,  # noqa: ANN001
    run=None,  # noqa: ANN001
) -> str | None:
    """The system ``umu-run`` to use, or ``None`` when absent or too old.

    A binary the profile names explicitly is the user's choice and always wins. The
    default ``umu-run`` is skipped when its version is below
    :data:`MIN_SYSTEM_VERSION`, so Nadare's managed copy is used instead. An
    unreadable version is trusted. Never raises.
    """
    found = (which or shutil.which)(configured)
    if not found:
        return None
    if configured != "umu-run":
        return found
    try:
        result = (run or subprocess.run)(
            [found, "--version"],
            env=process.child_environ(),
            capture_output=True,
            text=True,
            check=False,
            stdin=subprocess.DEVNULL,
            timeout=15,
        )
        version = parse_version((result.stdout or "") + (result.stderr or ""))
    except (OSError, subprocess.SubprocessError):
        return found
    if getattr(result, "returncode", 0) != 0:
        # It does not run at all (a broken install), unlike an unreadable banner: use ours.
        log.info(
            "system umu-run %s does not run (exit %s); using the managed copy",
            found,
            result.returncode,
        )
        return None
    if version is not None and version < MIN_SYSTEM_VERSION:
        log.info("system umu-run %s is too old (%s); using the managed copy", found, version)
        return None
    return found


def resolve(
    configured: str = "umu-run",
    *,
    allow_download: bool,
    emit: Emitter | None = None,
    cancel: Cancel | None = None,
) -> str | None:
    """Resolve a usable umu argv[0].

    Order (user, 2026-11-16: one tested umu everywhere; a system umu-run is uncommon):

    1. a umu-run the profile names explicitly (anything but the default ``umu-run``);
    2. Nadare's own pinned copy: already installed, or unpacked from the AppImage's
       bundled archive (no network);
    3. a usable system ``umu-run`` (not older than :data:`MIN_SYSTEM_VERSION`), so a source
       or AUR install with umu-launcher does not download a second copy;
    4. Nadare's own copy, downloaded once.

    Steps that write files (2's unpack, 4) run only when ``allow_download`` is true. Any
    failure is logged and reported as ``None`` so the caller can explain it.
    """
    if configured and configured != "umu-run":
        found = system(configured)
        if found:
            return found

    existing = managed()
    if existing is not None:
        return str(existing)

    global _last_setup_error
    if allow_download and _bundled_archive() is not None:
        try:
            found = str(ensure_managed(emit=emit, cancel=cancel))
        except UmuError as exc:
            log.info("bundled umu could not be set up: %s", exc)
            _last_setup_error = str(exc)
        else:
            _last_setup_error = ""
            return found

    found = system("umu-run")
    if found:
        return found

    if not allow_download:
        return None

    try:
        found = str(ensure_managed(emit=emit, cancel=cancel))
    except UmuError as exc:
        log.info("managed umu unavailable: %s", exc)
        _last_setup_error = str(exc)
        return None
    _last_setup_error = ""
    return found


#: Why the last :func:`resolve` could not set umu up ("" when it could).
_last_setup_error = ""


def unavailable_message() -> str:
    """:data:`UNAVAILABLE_HINT`, plus the reason the last setup gave (python3 missing, a folder
    that cannot be written...): the generic "check your internet" alone misled (stress u1)."""
    if _last_setup_error:
        return f"{UNAVAILABLE_HINT} ({_last_setup_error})"
    return UNAVAILABLE_HINT


#: Shown before a launch through umu when its Steam Runtime is not on disk yet: umu downloads
#: it before the game starts, and the page otherwise said only "launching" for minutes.
FIRST_RUNTIME_NOTE = (
    "umu needs to download the Steam Runtime once (a few "
    "hundred MB). The game starts when it is done; this can take a few minutes."
)


def runtime_downloaded(executable: str | None = None) -> bool:
    """Whether umu's Steam Runtime is already on disk (any variant). Never raises."""
    from . import userns

    try:
        if executable is not None:
            roots = ([runtime_base() / "umu"] if uses_owned_runtime(executable)
                     else shared_runtime_roots())
            return any(_has_runtime(root) for root in roots)
        return bool(userns._srt_bwrap_paths())
    except Exception:  # noqa: BLE001 - unknown: say nothing rather than fail a launch
        return True


def note_first_runtime(emit: Emitter | None, executable: str | None = None) -> None:
    """Emit :data:`FIRST_RUNTIME_NOTE` when the launch about to run will download the runtime."""
    if not runtime_downloaded(executable):
        emitter(emit)(Event("step", FIRST_RUNTIME_NOTE))


__all__ = [
    "FIRST_RUNTIME_NOTE",
    "MIN_SYSTEM_VERSION",
    "RUNTIME_MISMATCH_HINT",
    "SUPPORTED_RUNTIME_APPIDS",
    "SETUP_HINT",
    "SUBCOMMAND",
    "UMU_TAR_SHA256",
    "UMU_VERSION",
    "UmuError",
    "command",
    "ensure_managed",
    "is_runtime_mismatch_line",
    "is_setup_failure_line",
    "main",
    "managed",
    "managed_path",
    "note_first_runtime",
    "parse_version",
    "resolve",
    "run_argv",
    "shebang_python_ok",
    "runtime_downloaded",
    "system",
]


def runtime_storage() -> list[tuple[Path, int]]:
    """Measure on the Disk space worker, never while drawing Settings."""
    from . import diskspace

    return [(root, diskspace.tree_bytes(root)) for root in
            [runtime_base(), *shared_runtime_roots()]
            if root.is_dir() and not root.is_symlink()]


def remove_runtime(root: Path) -> bool:
    """Ask-first storage removal: recheck the path, processes and update locks."""
    from . import uninstall_all

    if root not in [runtime_base(), *shared_runtime_roots()] or root.is_symlink():
        return False
    if not root.is_dir() or uninstall_all._running_from(root):
        return False
    lock_paths = ([root / ".migration.lock", root / "umu/umu.lock"]
                  if root == runtime_base() else [root / "umu.lock"])
    descriptors = []
    try:
        # Open the runtime folder and every folder below it without following links, then
        # open each lock relative to them: O_NOFOLLOW on a lock alone cannot protect its
        # parents. Folders above the runtime may be links (/home -> /var/home, a data
        # folder moved to another disk); only a link from the runtime on is refused.
        directory_flags = os.O_RDONLY | os.O_DIRECTORY | os.O_NOFOLLOW
        root_fd = os.open(root, directory_flags)
        descriptors.append(root_fd)
        for lock in lock_paths:
            parent_fd = root_fd
            for part in lock.relative_to(root).parts[:-1]:
                try:
                    parent_fd = os.open(part, directory_flags, dir_fd=parent_fd)
                except FileNotFoundError:
                    break
                descriptors.append(parent_fd)
            else:
                fd = os.open(lock.name, os.O_CREAT | os.O_RDWR | os.O_NOFOLLOW,
                             0o600, dir_fd=parent_fd)
                descriptors.append(fd)
                fcntl.flock(fd, fcntl.LOCK_EX | fcntl.LOCK_NB)
        shutil.rmtree(root)
        return True
    except OSError:
        return False
    finally:
        for fd in descriptors:
            os.close(fd)


if __name__ == "__main__":
    # Private pre-install check. Both CPython and packaging/nadare_entry.py support -m.
    raise SystemExit(main(["--version"], _zipapp=Path(sys.argv[1])))
