# SPDX-License-Identifier: GPL-3.0-or-later
# Copyright (C) 2026 Nadare contributors
"""Local, redacted support reports."""

from __future__ import annotations

import hashlib
import ipaddress
import json
import os
import re
import shutil
import socket
import subprocess
from collections.abc import Callable, Iterable
from datetime import date
from pathlib import Path
from typing import Any

from . import events, paths, pending, process, runlog, runners, sessionfiles
from .gameversion import read_czn_version, read_install_version

MAX_REPORT_BYTES = 300_000

#: Where the distro is read. Inside Flatpak ``/etc/os-release`` names the runtime
#: (Freedesktop SDK), so the host's copy that Flatpak exposes wins (#1875 review).
OS_RELEASE = Path("/etc/os-release")
HOST_OS_RELEASE = Path("/run/host/os-release")
STUB_SHIPPED_SHA256 = ("0a8cc27d2b6a7eb5097d65ca04214de997693da34483e11bc5d76211caddbdf7",)


def _managed_umu_text() -> str:
    """The managed umu a launch uses when the system has none (#1338), for support reports."""
    try:
        from . import umu

        found = umu.managed()
    except Exception:  # noqa: BLE001 - a report must never fail on this line
        return "unknown"
    return str(found) if found else "not set up yet"


#: The labels this report writes at the start of its own lines (see ``redact(labels=...)``).
REPORT_LABELS = (
    "Generated",
    "Nadare",
    "Run kind",
    "Distro",
    "Kernel",
    "Virtualisation",
    "Session",
    "GPU",
    "Language",
    "umu-run",
    "umu-run (Nadare's own)",
    "Doctor",
    "Profile",
    "Backend",
    "Title",
    "Settings",
    "Installed version",
    "Runner",
    "Free disk (game)",
    "Free disk (prefix)",
    "Free disk (logs)",
    "CZN stub",
    "CZN stub banner",
    "Warning",
    "Game file",
    "Game dump folder",
)


def redact(
    text: str,
    *,
    username: str | None = None,
    home: str | Path | None = None,
    hostname: str | None = None,
    mask: bool = True,
    labels: Iterable[str] = (),
) -> str:
    """Remove personal identifiers and public network addresses from text.

    ``labels`` are the report's own line labels ("Runner", "Kernel"...): a line that starts
    with one of them followed by ``:`` keeps the label even when the username is the same
    word (GitHub's runners log in as ``runner``); the rest of the line is still redacted.

    ``mask`` also collapses token-like strings (``events.mask``), which would shorten hashes and
    long file names, so the report applies it to free-form log text only.
    """
    home = str(home if home is not None else Path.home())
    if username is None:
        # USER is unset in some service and sandbox environments: the redaction must not
        # quietly stop then (#1875 review). The home folder's name is the last guess.
        username = (
            os.environ.get("USER")
            or os.environ.get("LOGNAME")
            or _login_name()
            or Path(home).name
        )
    hostname = hostname if hostname is not None else socket.gethostname()
    out = text
    if home:
        out = re.sub(re.escape(home), "~", out, flags=re.IGNORECASE)
        win_home = home.replace("/", "\\")
        out = re.sub(
            rf"(?i)(?<![A-Za-z0-9])(?:Z:)?{re.escape(win_home)}(?![A-Za-z0-9])",
            "~",
            out,
        )
    if username:
        pattern = re.compile(rf"(?i)(?<![A-Za-z0-9]){re.escape(username)}(?![A-Za-z0-9])")
        keep = sorted({f"{label}:" for label in labels}, key=len, reverse=True)
        if keep:
            kept = []
            for line in out.split("\n"):
                label = next((k for k in keep if line.startswith(k)), "")
                kept.append(label + pattern.sub("USER", line[len(label) :]))
            out = "\n".join(kept)
        else:
            out = pattern.sub("USER", out)
    out = re.sub(r"\b[A-Z0-9._%+-]+@[A-Z0-9.-]+\.[A-Z]{2,}\b", "<email>", out, flags=re.I)
    if mask:
        out = events.mask(out)

    def ip_replace(match: re.Match[str]) -> str:
        value = match.group(0).strip("[]")
        try:
            ip = ipaddress.ip_address(value)
        except ValueError:
            return value
        return "<ip>" if ip.is_global else value

    out = re.sub(
        r"(?<![\w:])(?:\[[0-9a-fA-F:]+\]|(?:\d{1,3}\.){3}\d{1,3}|[0-9a-fA-F]*:[0-9a-fA-F:]+)(?![\w:])",
        ip_replace,
        out,
    )
    if hostname:
        out = re.sub(rf"(?i)(?<![A-Za-z0-9]){re.escape(hostname)}(?![A-Za-z0-9])", "<host>", out)
    return out


def _command(runner: Callable[..., Any], args: list[str], *, timeout: float = 3) -> str:
    try:
        value = runner(
            args,
            env=process.child_environ(),
            capture_output=True,
            text=True,
            timeout=timeout,
            check=False,
        )
        if hasattr(value, "stdout"):
            return str(value.stdout).strip() or "unavailable: no output"
        return str(value).strip() or "unavailable: no output"
    except Exception as exc:  # noqa: BLE001
        return f"unavailable: {exc}"


#: Most bytes of a log read for the report: the newest 200 lines fit easily; a log of any size
#: is never loaded whole (#858).
_TAIL_BYTES = 256 * 1024

#: A cut log also shows its first lines, where a launch log says what ran (the ``$`` command
#: line, the runner): a crash loop would push them out of the tail (#1725).
_HEAD_BYTES = 8 * 1024
#: Enough for the setup lines a launch writes before ``runner:``/``exec:``/``env:``; the byte
#: bound above still holds.
_HEAD_LINES = 40


def _tail(path: Path, lines: int = 200, *, head: bool = True) -> str:
    """The newest ``lines`` of ``path``, masked; with ``head``, a cut log keeps its first lines."""
    try:
        with path.open("rb") as handle:
            size = handle.seek(0, 2)
            start = max(0, size - _TAIL_BYTES)
            handle.seek(start)
            data = handle.read(_TAIL_BYTES + 1)
            first_bytes = b""
            if start and head:
                handle.seek(0)
                first_bytes = handle.read(min(_HEAD_BYTES, start))
        if start:
            # Began in the middle of a line: that first piece is not a whole line.
            cut = data.find(b"\n")
            data = data[cut + 1 :] if cut >= 0 else b""
        all_lines = data.decode("utf-8", errors="replace").splitlines()
        kept = all_lines[-lines:]
        if head and (start or len(all_lines) > len(kept)):
            newline = first_bytes.rfind(b"\n")
            first = (
                first_bytes[: newline + 1].decode("utf-8", errors="replace").splitlines()
                if start
                else all_lines[: len(all_lines) - len(kept)]
            )[:_HEAD_LINES]
            if first:
                kept = [*first, "… (lines cut) …", *kept]
        return events.mask("\n".join(kept)) or "(empty)"
    except OSError as exc:
        return f"unavailable: {exc}"


def _newest_runs(folder: Path, limit: int = 8) -> list[Path]:
    """The newest run of each operation in a game's log folder (not the background file)."""
    newest: dict[str, Path] = {}
    try:
        files = [p for p in folder.iterdir() if p.is_file()]
    except OSError:
        return []
    for path in sorted(files, key=runlog.run_order):
        operation = runlog.operation_of(path)
        if operation is not None and operation != "background":
            newest[operation] = path  # in time order, so the last one wins
    return sorted(newest.values(), key=runlog.run_order, reverse=True)[:limit]


def build_report(
    config: dict[str, Any],
    *,
    profile_name: str | None = None,
    logs_path: str | Path | None = None,
    profiles_path: str | Path | None = None,
    username: str | None = None,
    home: str | Path | None = None,
    hostname: str | None = None,
    run_command: Callable[..., Any] = subprocess.run,
    proc_root: str | Path = "/proc",
    max_bytes: int = MAX_REPORT_BYTES,
    doctor_text: str | None = None,
    os_release_path: str | Path | None = None,
    sys_root: str | Path = "/sys",
    config_path: str | Path | None = None,
) -> str:
    """Build one plain-text report. System access points are injectable for tests."""
    logs = Path(logs_path) if logs_path is not None else paths.logs_dir()
    profiles = Path(profiles_path) if profiles_path is not None else paths.profiles_dir()
    proc = Path(proc_root)
    name = profile_name or str(config.get("active_profile") or "")
    profs = config.get("profiles") if isinstance(config.get("profiles"), dict) else {}
    profile = profs.get(name) if name else None
    session_note = _session_note(profs, profiles)
    lines = [
        "# Nadare support report",
        f"Generated: {date.today().isoformat()}",
        *session_note,
        "",
        "## Environment",
    ]
    from nadare import __version__

    from . import channel, sandbox

    flatpak = sandbox.in_flatpak()
    lines += [
        # An unstable build says so: its bugs may come from research code (core/channel.py).
        f"Nadare: {channel.title(__version__)}",
        # Not the path: a file name can say more about a person than the problem needs.
        f"Run kind: {_run_kind(flatpak)}",
    ]
    if os_release_path is not None:
        releases: tuple[Path, ...] = (Path(os_release_path),)
    else:
        releases = (HOST_OS_RELEASE, OS_RELEASE) if flatpak else (OS_RELEASE,)
    pretty = _pretty_name(releases)
    lines += [f"Distro: {pretty}", f"Kernel: {_command(run_command, ['uname', '-srmo'])}"]
    virt = _detect_virtualisation(run_command, proc, Path(sys_root))
    lines += [
        f"Virtualisation: {virt}",
        "Session: "
        f"{os.environ.get('XDG_SESSION_TYPE', 'unknown')} / "
        f"{_display_kind()} / "
        f"{os.environ.get('XDG_CURRENT_DESKTOP', 'unknown')}",
        f"GPU: {_graphics_cards(_command(run_command, ['lspci']))}",
        # The language helps with text problems in games; the timezone only says where someone is.
        f"Language: {os.environ.get('LANG', 'unknown')}",
        f"umu-run: {_command(run_command, ['umu-run', '--version'])}",
        f"umu-run (Nadare's own): {_managed_umu_text()}",
    ]
    doctor = events.mask(doctor_text) if doctor_text else "unavailable: doctor checks not supplied"
    lines += ["", "Doctor:", doctor or "unavailable: no output", "", "## Profile"]
    if not profile:
        lines.append(f"unavailable: profile {name or '(active)'} not found")
    else:
        safe: dict[str, Any] = {}
        for key, value in profile.items():
            cleaned = _safe_setting(key, value)
            if cleaned is not _DROP:
                safe[key] = cleaned
        lines += [
            f"Profile: {name}",
            f"Backend: {profile.get('backend', 'unknown')}",
            f"Title: {profile.get('title', name)}",
            "Settings:",
            json.dumps(safe, ensure_ascii=False, sort_keys=True, default=str),
        ]
        install_dir = profile.get("install_dir")
        installed_version = read_install_version(install_dir)
        if installed_version is None and "czn" in str(profile.get("backend", "")).lower():
            installed_version = read_czn_version(profile.get("prefix") or profile.get("wineprefix"))
        game_dir = Path(str(install_dir or ""))
        runner_value = _runner_label(profile)
        lines += [
            f"Installed version: {installed_version or 'unknown'}",
            # Top level or ``extra``, as the backends read it; redacted like every other line.
            f"Runner: {runner_value}",
        ]
        configured = profile.get("prefix") or profile.get("wineprefix")
        prefix_dir = Path(os.path.expanduser(str(configured))) if configured else profiles / name
        for label, folder in (("game", game_dir), ("prefix", prefix_dir), ("logs", logs)):
            try:
                while not folder.exists() and folder != folder.parent:
                    folder = folder.parent  # the nearest folder that exists
                free = shutil.disk_usage(folder).free
                lines.append(f"Free disk ({label}): {free / 2**30:.0f} GiB")
            except (OSError, TypeError, ValueError) as exc:
                lines.append(f"Free disk ({label}): unavailable: {exc}")
        if "czn" in str(profile.get("backend", "")).lower():
            stub = profiles / name / "stub" / "czn_pcsdk_stub.exe"
            try:
                digest = hashlib.sha256(stub.read_bytes()).hexdigest()
                lines.append(
                    f"CZN stub: {stub.name} sha256={digest[:12]} "
                    f"shipped={'yes' if digest in STUB_SHIPPED_SHA256 else 'no'}"
                )
                # A Sandboxed launch writes the stub log in the prefix (#1556); the newer one wins.
                raw_prefix = str(profile.get("prefix") or "").strip()
                prefix = Path(os.path.expanduser(raw_prefix)) if raw_prefix else (
                    profiles / name / "prefix"
                )
                stub_log = max(
                    (logs / f"{name}-czn-stub.log", prefix / f"{name}-czn-stub.log"),
                    key=lambda p: p.stat().st_mtime if p.is_file() else -1.0,
                )
                try:
                    lines.append(
                        "CZN stub banner: "
                        + events.mask(stub_log.read_text(errors="replace").splitlines()[0])
                    )
                except (OSError, IndexError):
                    lines.append("CZN stub banner: unavailable")
            except OSError as exc:
                lines.append(f"CZN stub: unavailable: {exc}")
        # Only a folder the profile names: an empty install_dir is Path(""), the current folder.
        if str(profile.get("install_dir") or "").strip():
            lines += _game_folder_notes(game_dir)
    lines += ["", "## Unfinished operations", *_unfinished_lines(profs, name, config_path)]
    lines += ["", "## Logs"]
    log_files = sorted(logs.glob("*.log"), key=_mtime, reverse=True) if logs.exists() else []
    if not logs.exists():
        lines.append(f"unavailable: logs directory missing ({logs})")
    for path in log_files:
        # The longest known name owns a flat log: "wuwa-2-launch.log" is not wuwa's (#1875).
        if path.name == "nadare.log" or (
            name and _flat_log_owner(path, profs) == name
        ):
            # The app log's first lines are just older app lines; a run log's say what ran.
            lines += [f"### {path.name}", _tail(path, head=path.name != "nadare.log")]
    own_folder = runlog.game_id(name) if name else ""
    for path in _newest_runs(logs / own_folder) if own_folder else []:
        lines += [f"### {own_folder}/{path.name}", _tail(path)]
    # Other games are counted, not named: which games someone plays, and when, is theirs.
    try:
        others = sum(1 for path in logs.iterdir() if path.is_dir() and path.name != own_folder)
    except OSError:
        others = 0
    if others:
        lines.append(f"Logs for {others} other game{'s' if others != 1 else ''} (not included).")
    result = redact(
        "\n".join(lines),
        username=username,
        home=home,
        hostname=hostname,
        mask=False,
        labels=REPORT_LABELS,
    )
    # A Linux log name is bytes and may be surrogate-escaped in Python: replace those so the
    # report can still be encoded and written as UTF-8.
    encoded = result.encode("utf-8", errors="replace")
    limit = max(0, min(max_bytes, MAX_REPORT_BYTES))
    kept = _cut_logs_only(encoded, limit)
    if kept is not None:
        result = kept
    elif len(encoded) > limit:
        note = "\n".join(session_note).strip()
        head = "# Nadare support report (truncated; newest content kept)\n\n"
        prefix = (head + (note + "\n\n" if note else "")).encode("utf-8")
        # Count the actual UTF-8 header and warning, including very small budgets.
        if len(prefix) >= limit:
            result = prefix[:limit].decode("utf-8", errors="ignore")
        else:
            suffix = encoded[-(limit - len(prefix)) :].decode("utf-8", errors="ignore")
            result = prefix.decode("utf-8") + suffix
    else:
        result = encoded.decode("utf-8")
    return result


def _login_name() -> str:
    try:
        import pwd

        return pwd.getpwuid(os.getuid()).pw_name
    except (ImportError, KeyError, OSError):
        return ""


def _cut_logs_only(encoded: bytes, limit: int) -> str | None:
    """The report cut inside ``## Logs`` only, or None when that cannot fit ``limit``.

    The tail used to win, so a busy game's logs pushed out the version, distro, doctor verdict
    and profile, the part a triager reads first (#1875 review). The sections above the logs stay
    whole and the newest log lines fill what is left.
    """
    if len(encoded) <= limit:
        return None
    marker = b"\n## Logs\n"
    at = encoded.find(marker)
    if at < 0:
        return None
    title = b"# Nadare support report"
    preamble = encoded[: at + len(marker)]
    if preamble.startswith(title):
        preamble = title + b" (truncated; newest log lines kept)" + preamble[len(title) :]
    room = limit - len(preamble)
    if room < 512:  # too small to say anything useful about the logs: the old cut
        return None
    body = encoded[at + len(marker) :]
    parts = re.split(rb"(?=^### )", body, flags=re.M)
    return (preamble + b"".join(_fair_cut(parts, room))).decode("utf-8", errors="ignore")


def _fair_cut(parts: list[bytes], room: int) -> list[bytes]:
    """Each log section cut to its share of ``room``; a short one leaves its share to the rest.

    The sections are in report order (the app log, then the game's runs, newest first), so a
    cut at the report's end would drop the newest run. Each keeps its header and newest lines.
    """
    note = "… (older lines cut to fit) …\n".encode()
    budget = dict.fromkeys(range(len(parts)), 0)
    left, open_ = room, sorted(range(len(parts)), key=lambda i: len(parts[i]))
    while open_:
        share = left // len(open_)
        index = open_.pop(0)
        budget[index] = min(len(parts[index]), share)
        left -= budget[index]
    out = []
    for index, part in enumerate(parts):
        size = budget[index]
        if size >= len(part):
            out.append(part)
            continue
        header, _, text = part.partition(b"\n")
        keep = size - len(header) - 1 - len(note)
        if keep <= 0:
            continue
        tail = text[-keep:]
        newline = tail.find(b"\n")
        out.append(header + b"\n" + note + (tail[newline + 1 :] if newline >= 0 else b""))
    return out


def _run_kind(flatpak: bool) -> str:
    if flatpak:
        return "Flatpak"
    return "AppImage" if os.environ.get("APPIMAGE") else "source/pip"


def _pretty_name(candidates: Iterable[Path]) -> str:
    """``PRETTY_NAME`` from the first readable os-release file."""
    error: OSError | None = None
    for candidate in candidates:
        try:
            release = candidate.read_text(encoding="utf-8")
        except OSError as exc:
            error = error or exc
            continue
        return next(
            (
                x.partition("=")[2].strip().strip('"')
                for x in release.splitlines()
                if x.startswith("PRETTY_NAME=")
            ),
            "unknown",
        )
    return f"unavailable: {error}" if error else "unknown"


def _runner_label(profile: dict[str, Any]) -> str:
    """The runner a launch uses: the setting, else what the backend takes by default."""
    value = runners.profile_runner_value(profile)
    if value:
        return value
    wine = str(runners.profile_value(profile, "wine_binary") or "").strip()
    if wine:
        return f"wine_binary {wine}"
    backend = profile.get("backend")
    if backend in runners.PROTON_DEFAULT_BACKENDS:
        if backend in runners.GE_DEFAULT_KINDS:
            return "default (GE-Proton or DW-Proton, by Vulkan support)"
        tag = runners.pinned_tag(runners.DEFAULT_FAMILY)
        return f"default ({runners.DEFAULT_FAMILY} {tag})" if tag else "default"
    return "unknown"


def _flat_log_owner(path: Path, profiles: Any) -> str | None:
    """The profile an old ``<profile>-<operation>.log`` belongs to (the longest name wins)."""
    from . import logview

    known = list(profiles) if isinstance(profiles, dict) else []
    owner, operation = logview._parse_name(path.stem, known)
    return owner if operation and owner in known else None


def _mtime(path: Path) -> float:
    try:
        return path.stat().st_mtime
    except OSError:
        return 0.0


def _unfinished_lines(
    profiles: dict[str, Any], name: str, config_path: str | Path | None
) -> list[str]:
    """Storage operations that did not finish (#1372 phase 3), read only.

    This game's are described; other games' are counted, not named.
    """
    try:
        journal = pending.import_journal_path(
            config_path if config_path is not None else paths.config_file()
        )
        found = pending.unfinished(profiles, journal)
    except Exception as exc:  # noqa: BLE001 - the report must still be built
        return [f"unavailable: {exc}"]
    mine = [u for u in found if name and u.profile == name]
    others = len(found) - len(mine)
    lines = [f"{pending.LABELS.get(u.kind, u.kind)}: {u.detail}" for u in mine]
    if others:
        what = "operation" if others == 1 else "operations"
        games = "another game" if others == 1 else "other games"
        lines.append(f"{others} unfinished {what} for {games} (not included).")
    return lines or ["None."]


def _session_note(profiles: dict[str, Any], profiles_root: Path) -> list[str]:
    """A warning when a sign-in session file is still on disk (#1231); never its content.

    Counted, not located: the paths would name the games.
    """
    try:
        count = len(sessionfiles.find_leftovers(profiles, profiles_root))
    except Exception:  # noqa: BLE001 - the report must still be built
        return []
    if not count:
        return []
    files = "session file" if count == 1 else "session files"
    return [
        "",
        "## Session files",
        f"Warning: {count} sign-in {files} from an interrupted game run "
        f"{'is' if count == 1 else 'are'} still on disk. Not part of this report. "
        "Before you share Nadare's data folder or a game's folder, delete the files named "
        "session.json, session.enc.json or session.json.expired-bak in it.",
    ]


#: ``lspci`` classes that are graphics cards; everything else in the machine stays out.
_GPU_CLASSES = ("VGA compatible controller", "3D controller", "Display controller")


def _graphics_cards(lspci: str) -> str:
    """The graphics cards from ``lspci``, by name only (no bus address, no other devices)."""
    if lspci.startswith("unavailable"):
        return lspci
    cards = []
    for line in lspci.splitlines():
        _address, _, rest = line.partition(" ")
        kind, _, name = rest.partition(": ")
        if kind.strip() in _GPU_CLASSES and name.strip():
            cards.append(name.strip())
    return "; ".join(cards) if cards else "none found"


def _display_kind() -> str:
    if os.environ.get("WAYLAND_DISPLAY"):
        return "Wayland"
    if os.environ.get("DISPLAY"):
        return "X11"
    return "unknown"


def _detect_virtualisation(runner: Callable[..., Any], proc: Path, sys_root: Path) -> str:
    detected = _command(runner, ["systemd-detect-virt"])
    if detected not in ("none", "unavailable: no output") and not detected.startswith(
        "unavailable"
    ):
        return detected
    try:
        proc_text = (proc / "version").read_text(errors="replace")
        cpu = (proc / "cpuinfo").read_text(errors="replace")
        dmi = " ".join(
            path.read_text(errors="replace")
            for path in (
                sys_root / "class/dmi/id/sys_vendor",
                sys_root / "class/dmi/id/product_name",
            )
            if path.exists()
        )
    except OSError:
        return "unavailable: virtualization probes failed"
    text = (cpu + dmi).lower()
    if "microsoft" in proc_text.lower():
        return "wsl"
    if re.search(r"hypervisor.*kvm", cpu, re.I):
        return "kvm"
    if "vmware" in text:
        return "vmware"
    if any(value in text for value in ("qemu", "virtual machine")):
        return "qemu"
    return "none"


_DROP = object()


def _game_folder_notes(game_dir: Path) -> list[str]:
    """Anti-cheat files and crash-dump folders inside the game folder, for the report.

    Folders that cannot be read (container storage, permissions) are skipped, never fatal."""
    notes: list[str] = []
    try:
        if not game_dir.is_dir():
            return notes
    except (OSError, TypeError, ValueError):
        return notes
    for root, dirs, files in os.walk(game_dir, onerror=lambda _exc: None):
        for name in files:
            if "cheat" in name.lower() or name.lower().startswith("ucld"):
                try:
                    notes.append(
                        f"Game file: {name} size={os.stat(os.path.join(root, name)).st_size}"
                    )
                except OSError:
                    pass
        for name in dirs:
            if "crashpad" in name.lower() or "dump" in name.lower():
                notes.append(f"Game dump folder: {name}")
    return notes


def _safe_setting(key: str, value: Any) -> Any:
    if events.is_secret_key(key) or key.lower() in {"extra_env", "cookies", "cookie"}:
        return _DROP
    if isinstance(value, dict):
        result = {}
        for child_key, child_value in value.items():
            child = _safe_setting(str(child_key), child_value)
            if child is not _DROP:
                result[str(child_key)] = child
        return result
    if isinstance(value, list):
        result = []
        for index, item in enumerate(value):
            if index and isinstance(item, str) and not item.startswith("-"):
                if events.is_secret_flag(value[index - 1]):
                    # An argument list keeps a flag and its value apart (#1592).
                    result.append(events._REDACTED)
                    continue
            child = _safe_setting("", item)
            if child is not _DROP:
                result.append(child)
        return result
    if isinstance(value, str):
        value = events.mask_known(value)  # free-form text such as launch_args (#968)
    if isinstance(value, str) and ("/" in value or value.startswith("~")):
        return Path(value).name
    return value
