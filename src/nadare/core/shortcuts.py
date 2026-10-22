# SPDX-License-Identifier: GPL-3.0-or-later
# Copyright (C) 2026 Nadare contributors
"""Per-profile launcher shortcuts: desktop entries and Steam non-Steam games.

Every generated shortcut starts Nadare (``nadare launch -p <profile>``) rather
than the game executable, so a game update, a moved install directory or a
renamed binary cannot break it. The module is pure Python: no Qt, no network.
"""

from __future__ import annotations

import hashlib
import os
import re
import shutil
import struct
import subprocess
import sys
import tempfile
from collections.abc import Mapping
from pathlib import Path
from typing import Any

from . import sandbox

#: Application id under which the Flatpak build is published.
FLATPAK_ID = "io.github.frostlinuxdev.Nadare"

#: Characters that force a Desktop Entry ``Exec`` argument to be quoted. The
#: spec reserves these; quoting them is always safe.
_EXEC_RESERVED = set(' \t\n"\'\\><~|&;$*?#()`')


class ShortcutError(RuntimeError):
    """A shortcut could not be created (for example, Steam is running)."""


# ---------------------------------------------------------------------------
# how to start Nadare
# ---------------------------------------------------------------------------
def launcher_argv(
    environ: Mapping[str, str] | None = None,
    in_flatpak: bool | None = None,
    exe: str | None = None,
) -> list[str]:
    """Return the argv that starts Nadare again (AppImage, Flatpak, PATH, module)."""
    env = os.environ if environ is None else environ
    appimage = env.get("APPIMAGE")
    if appimage:
        return [appimage]
    sandboxed = sandbox.in_flatpak() if in_flatpak is None else in_flatpak
    if sandboxed:
        return ["flatpak", "run", FLATPAK_ID]
    found = shutil.which("nadare")
    if found:
        return [found]
    return [exe or sys.executable, "-m", "nadare"]


def slug(profile: str) -> str:
    """Filesystem-safe lowercase slug for ``profile`` (``"game"`` if empty)."""
    slugged = re.sub(r"[^a-z0-9]+", "-", profile.lower()).strip("-")
    return slugged or "game"


# ---------------------------------------------------------------------------
# Desktop Entry
# ---------------------------------------------------------------------------
def _quote_exec(arg: str) -> str:
    """Quote one ``Exec`` argument per the Desktop Entry spec."""
    arg = _one_line(arg.replace("%", "%%"))
    if not any(ch in _EXEC_RESERVED for ch in arg):
        return arg
    escaped = (
        arg.replace("\\", "\\\\")
        .replace('"', '\\"')
        .replace("`", "\\`")
        .replace("$", "\\$")
    )
    return f'"{escaped}"'


def _one_line(value: str) -> str:
    """Replace control characters so a value can never start a new key."""
    return re.sub(r"[\x00-\x1f\x7f]", " ", value)


def desktop_entry(profile: str, title: str, argv: list[str], icon: str = "nadare") -> str:
    """Render a freedesktop entry that launches ``profile`` through Nadare."""
    exec_args = " ".join(_quote_exec(str(a)) for a in argv)
    exec_line = f"{exec_args} launch -p {_quote_exec(profile)}".strip()
    lines = [
        "[Desktop Entry]",
        "Type=Application",
        f"Name={_one_line(title)}",
        f"Exec={exec_line}",
        f"Icon={icon}",
        "Terminal=false",
        "Categories=Game;",
        f"X-Nadare-Profile={_one_line(profile)}",
        "StartupNotify=false",
    ]
    return "\n".join(lines) + "\n"


def _host_home() -> Path:
    """The desktop user's home, even when HOME points into a Flatpak sandbox."""
    if not sandbox.in_flatpak():
        return Path(os.path.expanduser("~"))
    host_home = os.environ.get("HOST_HOME")
    if host_home:
        return Path(host_home)
    import pwd

    try:
        return Path(pwd.getpwuid(os.getuid()).pw_dir)
    except (KeyError, OSError):
        return Path("/run/host/home")


def _host_path(path: Path) -> Path:
    """Where a host path appears inside the Flatpak sandbox (usually /run/host)."""
    if not sandbox.in_flatpak():
        return path
    path = Path(path)
    if not path.is_absolute() or path.is_relative_to("/run/host"):
        return path
    root = Path("/run/host")
    if not root.is_dir():
        return path
    try:
        return root / path.relative_to("/")
    except ValueError:
        return path


def desktop_dir() -> Path:
    """The user's Desktop directory (XDG when available, else ``~/Desktop``)."""
    flatpak = sandbox.in_flatpak()
    tool = sandbox.host_which("xdg-user-dir") if flatpak else shutil.which("xdg-user-dir")
    if tool:
        argv = sandbox.host_argv([tool, "DESKTOP"]) if flatpak else [tool, "DESKTOP"]
        try:
            result = subprocess.run(
                argv, capture_output=True, text=True, timeout=5, check=False
            )
            if result.returncode == 0 and result.stdout.strip():
                return _host_path(Path(result.stdout.strip()))
        except (OSError, subprocess.SubprocessError):
            pass
    if flatpak:
        return _host_path(_host_home() / "Desktop")
    return Path(os.path.expanduser("~/Desktop"))


def _applications_dir(applications_dir: str | os.PathLike[str] | None) -> Path:
    if applications_dir is not None:
        return Path(applications_dir)
    if sandbox.in_flatpak():
        base = os.environ.get("HOST_XDG_DATA_HOME") or str(
            _host_home() / ".local" / "share"
        )
        return _host_path(Path(base)) / "applications"
    base = os.environ.get("XDG_DATA_HOME") or os.path.expanduser("~/.local/share")
    return Path(base) / "applications"


def _atomic_write_bytes(path: Path, data: bytes, mode: int) -> None:
    path.parent.mkdir(parents=True, exist_ok=True)
    fd, tmp = tempfile.mkstemp(dir=str(path.parent), prefix=".nadare-", suffix=".tmp")
    try:
        with os.fdopen(fd, "wb") as handle:
            handle.write(data)
            handle.flush()
            os.fsync(handle.fileno())
        os.chmod(tmp, mode)
        os.replace(tmp, path)
    finally:
        if os.path.exists(tmp):
            os.unlink(tmp)


def _atomic_write(path: Path, text: str, mode: int) -> None:
    _atomic_write_bytes(path, text.encode("utf-8"), mode)


def _owned_profile(path: Path) -> str | None:
    """Return the ``X-Nadare-Profile`` value of a Nadare-owned entry, else None."""
    if not path.name.startswith("nadare-") or not path.name.endswith(".desktop"):
        return None
    try:
        lines = path.read_text(encoding="utf-8").splitlines()
    except (OSError, UnicodeDecodeError):
        return None
    for line in lines:
        if line.startswith("X-Nadare-Profile="):
            return line.split("=", 1)[1]
    return None


def install_desktop_entry(
    profile: str,
    title: str,
    *,
    applications_dir: str | os.PathLike[str] | None = None,
    desktop_dir: str | os.PathLike[str] | None = None,
    argv: list[str] | None = None,
) -> list[Path]:
    """Write ``nadare-<slug>.desktop`` to the menu (and optionally the desktop)."""
    resolved = list(argv) if argv is not None else launcher_argv()
    text = desktop_entry(profile, title, resolved)
    name = f"nadare-{slug(profile)}.desktop"
    dirs = [_applications_dir(applications_dir)]
    if desktop_dir is not None:
        dirs.append(Path(desktop_dir))
    for directory in dirs:
        owner = _owned_profile(directory / name)
        if owner is not None and owner != profile:
            # Another profile slugifies the same: keep both with a stable hash.
            digest = hashlib.sha1(profile.encode("utf-8")).hexdigest()[:8]
            name = f"nadare-{slug(profile)}-{digest}.desktop"
            break
    written = []
    menu = _applications_dir(applications_dir) / name
    _atomic_write(menu, text, 0o644)
    written.append(menu)
    if desktop_dir is not None:
        desk = Path(desktop_dir) / name
        _atomic_write(desk, text, 0o755)
        written.append(desk)
    return written


def remove_desktop_entries(
    profile: str,
    *,
    applications_dir: str | os.PathLike[str] | None = None,
    desktop_dir: str | os.PathLike[str] | None = None,
) -> list[Path]:
    """Delete Nadare-owned entries for ``profile``; never touch foreign files."""
    dirs = [_applications_dir(applications_dir)]
    if desktop_dir is not None:
        dirs.append(Path(desktop_dir))
    removed: list[Path] = []
    for directory in dirs:
        if not directory.is_dir():
            continue
        for path in sorted(directory.glob("nadare-*.desktop")):
            if _owned_profile(path) != profile:
                continue
            try:
                path.unlink()
            except FileNotFoundError:
                continue
            removed.append(path)
    return removed


def installed_desktop_entries(
    *,
    applications_dir: str | os.PathLike[str] | None = None,
    desktop_dir: str | os.PathLike[str] | None = None,
) -> list[tuple[Path, str]]:
    """List ``(path, profile)`` for every Nadare-owned entry on disk."""
    dirs = [_applications_dir(applications_dir)]
    if desktop_dir is not None:
        dirs.append(Path(desktop_dir))
    found: list[tuple[Path, str]] = []
    seen: set[Path] = set()
    for directory in dirs:
        if not directory.is_dir():
            continue
        for path in sorted(directory.glob("nadare-*.desktop")):
            owner = _owned_profile(path)
            if owner is None or path in seen:
                continue
            seen.add(path)
            found.append((path, owner))
    return found


def refresh_desktop_entries(
    profiles: Mapping[str, Mapping[str, Any]],
    *,
    applications_dir: str | os.PathLike[str] | None = None,
    desktop_dir: str | os.PathLike[str] | None = None,
    argv: list[str] | None = None,
) -> dict[str, list[Path]]:
    """Rewrite existing entries with current data and drop deleted profiles."""
    resolved = list(argv) if argv is not None else launcher_argv()
    dirs: list[tuple[Path, int]] = [(_applications_dir(applications_dir), 0o644)]
    if desktop_dir is not None:
        dirs.append((Path(desktop_dir), 0o755))
    updated: list[Path] = []
    removed: list[Path] = []
    for directory, mode in dirs:
        if not directory.is_dir():
            continue
        for path in sorted(directory.glob("nadare-*.desktop")):
            owner = _owned_profile(path)
            if owner is None:
                continue
            if owner in profiles:
                title = str(profiles[owner].get("title") or owner)
                text = desktop_entry(owner, title, resolved)
                try:
                    if path.read_text(encoding="utf-8") != text:
                        _atomic_write(path, text, mode)
                        updated.append(path)
                except OSError:
                    continue
            else:
                try:
                    path.unlink()
                except FileNotFoundError:
                    continue
                removed.append(path)
    return {"updated": updated, "removed": removed}


# ---------------------------------------------------------------------------
# Steam shortcuts.vdf
# ---------------------------------------------------------------------------
_STRING_KEYS = ("AppName", "Exe", "StartDir", "icon", "ShortcutPath", "LaunchOptions")
_INT_KEYS = (
    "IsHidden",
    "AllowDesktopConfig",
    "AllowOverlay",
    "OpenVR",
    "Devkit",
    "DevkitOverrideAppID",
    "LastPlayTime",
)
_SHORTCUTS_HEADER = b"\x00shortcuts\x00"


def _encode_value(key: str, value: Any) -> bytes:
    if isinstance(value, bool):
        value = int(value)
    if isinstance(value, Mapping):
        buf = bytearray(b"\x00" + key.encode("utf-8") + b"\x00")
        buf += _encode_map(value)
        buf += b"\x08"
        return bytes(buf)
    if isinstance(value, int):
        return b"\x02" + key.encode("utf-8") + b"\x00" + struct.pack("<i", value)
    return b"\x01" + key.encode("utf-8") + b"\x00" + str(value).encode("utf-8") + b"\x00"


def _encode_map(mapping: Mapping[str, Any]) -> bytes:
    out = bytearray()
    for key, value in mapping.items():
        out += _encode_value(str(key), value)
    return bytes(out)


def vdf_dumps(shortcuts: list[dict[str, Any]]) -> bytes:
    """Serialise the ``shortcuts`` map to Steam's binary VDF format."""
    out = bytearray(_SHORTCUTS_HEADER)
    for index, entry in enumerate(shortcuts):
        out += b"\x00" + str(index).encode("ascii") + b"\x00"
        out += _encode_map(entry)
        out += b"\x08"
    out += b"\x08"  # end of the "shortcuts" map
    out += b"\x08"  # end of the root map
    return bytes(out)


def _read_cstr(data: bytes, pos: int) -> tuple[str, int]:
    end = data.index(b"\x00", pos)
    return data[pos:end].decode("utf-8", errors="replace"), end + 1


def _read_map(data: bytes, pos: int) -> tuple[dict[str, Any], int]:
    result: dict[str, Any] = {}
    while pos < len(data):
        kind = data[pos]
        pos += 1
        if kind == 0x08:
            return result, pos
        key, pos = _read_cstr(data, pos)
        if kind == 0x01:
            value, pos = _read_cstr(data, pos)
        elif kind == 0x02:
            value = struct.unpack_from("<i", data, pos)[0]
            pos += 4
        elif kind == 0x00:
            value, pos = _read_map(data, pos)
        else:  # pragma: no cover - malformed input
            break
        result[key] = value
    return result, pos


def vdf_loads(data: bytes, *, strict: bool = False) -> list[dict[str, Any]]:
    """Parse a ``shortcuts.vdf``; empty or unrecognised data yields ``[]``.

    A truncated/corrupt file also yields ``[]`` unless ``strict``: a caller that
    rewrites the file must not mistake corruption for "no shortcuts".
    """
    if not data:
        return []
    raw = bytes(data)
    if not raw.startswith(_SHORTCUTS_HEADER):
        return []
    try:
        pos = len(_SHORTCUTS_HEADER)
        shortcuts: list[dict[str, Any]] = []
        while pos < len(raw):
            kind = raw[pos]
            pos += 1
            if kind == 0x08:
                break
            if kind != 0x00:  # pragma: no cover - malformed input
                break
            _key, pos = _read_cstr(raw, pos)
            entry, pos = _read_map(raw, pos)
            shortcuts.append(entry)
        return shortcuts
    except (ValueError, struct.error, IndexError, RecursionError):
        # A truncated/corrupt file (including absurdly deep nesting) is
        # "unrecognised data": degrade to [].
        if strict:
            raise ValueError("corrupt shortcuts.vdf") from None
        return []


def steam_running() -> bool:
    """True when a process named ``steam`` currently exists."""
    proc = Path("/proc")
    if not proc.is_dir():
        return False
    try:
        entries = list(proc.iterdir())
    except OSError:
        return False
    for entry in entries:
        if not entry.name.isdigit():
            continue
        try:
            comm = (entry / "comm").read_text(encoding="utf-8", errors="replace").strip()
        except OSError:
            continue
        if comm == "steam":
            return True
    return False


def _steam_quote(value: str) -> str:
    escaped = value.replace("\\", "\\\\").replace('"', '\\"')
    return f'"{escaped}"'


def _steam_launch_options(profile: str, argv: list[str]) -> str:
    parts = [str(a) for a in argv[1:]]
    parts += ["launch", "-p", _steam_quote(profile)]
    return " ".join(parts)


def add_steam_shortcut(
    shortcuts_vdf: str | os.PathLike[str],
    profile: str,
    title: str,
    argv: list[str],
    *,
    icon: str = "",
) -> bool:
    """Append a non-Steam-game entry for ``profile``; ``False`` if it exists."""
    if steam_running():
        raise ShortcutError("Steam is running; close it before adding a shortcut")
    path = Path(shortcuts_vdf)
    existing: list[dict[str, Any]] = []
    if path.exists():
        try:
            existing = vdf_loads(path.read_bytes(), strict=True)
        except (OSError, ValueError, struct.error) as exc:
            # Never rewrite a file we could not read back: that would drop
            # every shortcut the user already has.
            raise ShortcutError(
                f"{path} is unreadable or corrupt ({exc}); leaving it untouched"
            ) from exc
    launch_options = _steam_launch_options(profile, argv)
    for entry in existing:
        if entry.get("AppName") == title and entry.get("LaunchOptions") == launch_options:
            return False
    record: dict[str, Any] = {
        "AppName": title,
        "Exe": _steam_quote(str(argv[0])),
        "StartDir": os.path.dirname(str(argv[0])) or ".",
        "icon": icon,
        "ShortcutPath": "",
        "LaunchOptions": launch_options,
        "IsHidden": 0,
        "AllowDesktopConfig": 1,
        "AllowOverlay": 1,
        "OpenVR": 0,
        "Devkit": 0,
        "DevkitOverrideAppID": 0,
        "LastPlayTime": 0,
        "tags": {},
    }
    existing.append(record)
    backup = path.with_name(path.name + ".nadare-bak")
    if path.exists() and not backup.exists():
        backup.write_bytes(path.read_bytes())
    _atomic_write_bytes(path, vdf_dumps(existing), 0o644)
    return True


def remove_steam_shortcut(shortcuts_vdf: str | os.PathLike[str], profile: str) -> bool:
    """Drop the non-Steam-game entries ``add_steam_shortcut`` made for ``profile``.

    Returns ``False`` when the file has none. Steam must be closed, as for adding.
    """
    path = Path(shortcuts_vdf)
    if not path.exists():
        return False
    try:
        existing = vdf_loads(path.read_bytes(), strict=True)
    except (OSError, ValueError, struct.error) as exc:
        raise ShortcutError(
            f"{path} is unreadable or corrupt ({exc}); leaving it untouched"
        ) from exc
    suffix = f'launch -p "{profile}"'
    kept = [e for e in existing if not str(e.get("LaunchOptions", "")).endswith(suffix)]
    if len(kept) == len(existing):
        return False
    if steam_running():
        raise ShortcutError("Steam is running; close it before removing a shortcut")
    backup = path.with_name(path.name + ".nadare-bak")
    if not backup.exists():
        backup.write_bytes(path.read_bytes())
    _atomic_write_bytes(path, vdf_dumps(kept), 0o644)
    return True


def remove_steam_shortcuts(profile: str) -> tuple[list[Path], list[str]]:
    """Remove ``profile``'s entry from every Steam ``shortcuts.vdf``.

    Returns the files changed and a warning per file that could not be.
    """
    changed: list[Path] = []
    warnings: list[str] = []
    for path in find_steam_shortcut_files():
        try:
            if remove_steam_shortcut(path, profile):
                changed.append(path)
        except ShortcutError as exc:
            warnings.append(str(exc))
    return changed, warnings


_LAUNCH_PROFILE = re.compile(r'(?:^|\s)launch\s+-p\s+(?:"([^"]*)"|(\S+))\s*$')


def _steam_shortcut_profile(entry: Mapping[str, Any]) -> str | None:
    """The profile a Nadare Steam shortcut launches, or ``None`` for another entry."""
    match = _LAUNCH_PROFILE.search(str(entry.get("LaunchOptions") or ""))
    if not match:
        return None
    return match.group(1) if match.group(1) is not None else match.group(2)


def installed_steam_shortcuts() -> list[tuple[Path, str, str]]:
    """List ``(file, profile, title)`` for every Nadare Steam non-Steam entry."""
    found: list[tuple[Path, str, str]] = []
    for path in find_steam_shortcut_files():
        try:
            entries = vdf_loads(path.read_bytes(), strict=True)
        except (OSError, ValueError, struct.error):
            continue
        for entry in entries:
            profile = _steam_shortcut_profile(entry)
            if profile is None:
                continue
            found.append((path, profile, str(entry.get("AppName") or profile)))
    return found


def refresh_steam_shortcuts(
    profiles: Mapping[str, Mapping[str, Any]],
) -> dict[str, list[str] | list[Path]]:
    """Update Nadare Steam shortcut titles and drop deleted profiles.

    Files that cannot be read or written are reported in ``warnings`` and left
    untouched, as is every file while Steam is running.
    """
    updated: list[Path] = []
    removed: list[str] = []
    warnings: list[str] = []
    for path in find_steam_shortcut_files():
        try:
            existing = vdf_loads(path.read_bytes(), strict=True)
        except (OSError, ValueError, struct.error) as exc:
            warnings.append(f"could not refresh {path}: {exc}")
            continue
        kept: list[dict[str, Any]] = []
        changed = False
        gone: list[str] = []
        for entry in existing:
            profile = _steam_shortcut_profile(entry)
            if profile is None:
                kept.append(entry)
                continue
            if profile not in profiles:
                changed = True
                gone.append(profile)
                continue
            title = str(profiles[profile].get("title") or profile)
            if entry.get("AppName") != title:
                entry["AppName"] = title
                changed = True
            kept.append(entry)
        if not changed:
            continue
        if steam_running():
            warnings.append(f"Steam is running; close it to refresh {path}")
            continue
        backup = path.with_name(path.name + ".nadare-bak")
        try:
            if not backup.exists():
                backup.write_bytes(path.read_bytes())
            _atomic_write_bytes(path, vdf_dumps(kept), 0o644)
        except OSError as exc:
            warnings.append(f"could not refresh {path}: {exc}")
            continue
        updated.append(path)
        removed.extend(f"{profile} ({path})" for profile in gone)
    return {"updated": updated, "removed": removed, "warnings": warnings}


def find_steam_shortcut_files() -> list[Path]:
    """Every existing Steams ``shortcuts.vdf`` (native and Flatpak), de-duplicated."""
    home = _host_path(_host_home()) if sandbox.in_flatpak() else Path(
        os.path.expanduser("~")
    )
    roots = [
        home / ".steam" / "steam" / "userdata",
        home / ".local" / "share" / "Steam" / "userdata",
        home
        / ".var"
        / "app"
        / "com.valvesoftware.Steam"
        / ".local"
        / "share"
        / "Steam"
        / "userdata",
    ]
    found: list[Path] = []
    seen: set[Path] = set()
    for root in roots:
        if not root.is_dir():
            continue
        for userdir in sorted(root.iterdir()):
            candidate = userdir / "config" / "shortcuts.vdf"
            if not candidate.parent.is_dir():
                continue
            resolved = candidate.resolve()
            if resolved in seen:
                continue
            seen.add(resolved)
            found.append(candidate)
    return found


__all__ = [
    "FLATPAK_ID",
    "ShortcutError",
    "add_steam_shortcut",
    "desktop_entry",
    "find_steam_shortcut_files",
    "install_desktop_entry",
    "installed_desktop_entries",
    "installed_steam_shortcuts",
    "launcher_argv",
    "refresh_desktop_entries",
    "refresh_steam_shortcuts",
    "remove_desktop_entries",
    "slug",
    "steam_running",
    "vdf_dumps",
    "vdf_loads",
]
