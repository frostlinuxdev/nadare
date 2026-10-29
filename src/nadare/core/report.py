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
from collections.abc import Callable
from datetime import date
from pathlib import Path
from typing import Any

from . import events, paths, process

MAX_REPORT_BYTES = 300_000
STUB_PREBUILT_SHA256 = "fe4a5bad970750e974cf999e31e3b87132fbb25f9c04c9eea69e3acc2e243185"


def redact(
    text: str,
    *,
    username: str | None = None,
    home: str | Path | None = None,
    hostname: str | None = None,
    mask: bool = True,
) -> str:
    """Remove personal identifiers and public network addresses from text.

    ``mask`` also collapses token-like strings (``events.mask``), which would shorten hashes and
    long file names, so the report applies it to free-form log text only.
    """
    username = username if username is not None else os.environ.get("USER", "")
    home = str(home if home is not None else Path.home())
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
        out = re.sub(
            rf"(?i)(?<![A-Za-z0-9]){re.escape(username)}(?![A-Za-z0-9])",
            "USER",
            out,
        )
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


def _tail(path: Path, lines: int = 200) -> str:
    try:
        data = path.read_bytes().decode("utf-8", errors="replace")
        return events.mask("\n".join(data.splitlines()[-lines:])) or "(empty)"
    except OSError as exc:
        return f"unavailable: {exc}"


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
    os_release_path: str | Path = "/etc/os-release",
    sys_root: str | Path = "/sys",
) -> str:
    """Build one plain-text report. System access points are injectable for tests."""
    logs = Path(logs_path) if logs_path is not None else paths.logs_dir()
    profiles = Path(profiles_path) if profiles_path is not None else paths.profiles_dir()
    proc = Path(proc_root)
    name = profile_name or str(config.get("active_profile") or "")
    profs = config.get("profiles") if isinstance(config.get("profiles"), dict) else {}
    profile = profs.get(name) if name else None
    lines = [
        "# Nadare support report",
        f"Generated: {date.today().isoformat()}",
        "",
        "## Environment",
    ]
    from nadare import __version__

    appimage = os.environ.get("APPIMAGE")
    lines += [
        f"Nadare: {__version__}",
        f"Run kind: AppImage ({appimage})" if appimage else "Run kind: source/pip",
    ]
    try:
        release = Path(os_release_path).read_text(encoding="utf-8")
        pretty = next(
            (
                x.partition("=")[2].strip().strip('"')
                for x in release.splitlines()
                if x.startswith("PRETTY_NAME=")
            ),
            "unknown",
        )
    except OSError as exc:
        pretty = f"unavailable: {exc}"
    lines += [f"Distro: {pretty}", f"Kernel: {_command(run_command, ['uname', '-srmo'])}"]
    virt = _detect_virtualisation(run_command, proc, Path(sys_root))
    lines += [
        f"Virtualisation: {virt}",
        "Session: "
        f"{os.environ.get('XDG_SESSION_TYPE', 'unknown')} / "
        f"{_display_kind()} / "
        f"{os.environ.get('XDG_CURRENT_DESKTOP', 'unknown')}",
        f"GPU: {_command(run_command, ['lspci'])}",
        f"Locale: {os.environ.get('LANG', 'unknown')}; timezone: {os.environ.get('TZ', 'system')}",
        f"umu-run: {_command(run_command, ['umu-run', '--version'])}",
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
        game_dir = Path(str(profile.get("install_dir") or ""))
        lines += [
            f"Installed version: {profile.get('version') or 'unknown'}",
            f"Runner: {profile.get('runner') or 'unknown'}",
        ]
        for label, folder in (("game", game_dir), ("prefix", profiles / name), ("logs", logs)):
            try:
                free = shutil.disk_usage(folder if folder.exists() else folder.parent).free
                lines.append(f"Free disk ({label}): {free} bytes")
            except OSError as exc:
                lines.append(f"Free disk ({label}): unavailable: {exc}")
        if "czn" in str(profile.get("backend", "")).lower():
            stub = profiles / name / "stub" / "czn_pcsdk_stub.exe"
            try:
                digest = hashlib.sha256(stub.read_bytes()).hexdigest()
                lines.append(
                    f"CZN stub: {stub.name} sha256={digest[:12]} "
                    f"shipped={'yes' if digest == STUB_PREBUILT_SHA256 else 'no'}"
                )
                stub_log = logs / f"{name}-czn-stub.log"
                try:
                    lines.append(
                        "CZN stub banner: "
                        + events.mask(stub_log.read_text(errors="replace").splitlines()[0])
                    )
                except (OSError, IndexError):
                    lines.append("CZN stub banner: unavailable")
            except OSError as exc:
                lines.append(f"CZN stub: unavailable: {exc}")
        if game_dir.is_dir():
            for candidate in game_dir.rglob("*"):
                if candidate.is_file() and (
                    "cheat" in candidate.name.lower() or candidate.name.lower().startswith("ucld")
                ):
                    try:
                        lines.append(f"Game file: {candidate.name} size={candidate.stat().st_size}")
                    except OSError:
                        pass
                elif candidate.is_dir() and (
                    "crashpad" in candidate.name.lower() or "dump" in candidate.name.lower()
                ):
                    lines.append(f"Game dump folder: {candidate.name}")
    lines += ["", "## Logs"]
    log_files = (
        sorted(
            logs.glob("*.log"), key=lambda p: p.stat().st_mtime if p.exists() else 0, reverse=True
        )
        if logs.exists()
        else []
    )
    if not logs.exists():
        lines.append(f"unavailable: logs directory missing ({logs})")
    for path in log_files:
        if path.name == "nadare.log" or (
            name
            and path.name.startswith(name + "-")
            and (
                "-launch" in path.name
                or "-install" in path.name
                or "-czn-stub" in path.name
                or path.name.endswith(".log")
            )
        ):
            lines += [f"### {path.name}", _tail(path)]
    try:
        for path in sorted(logs.iterdir(), key=lambda p: p.name):
            if path not in log_files:
                stat = path.stat()
                lines.append(f"Other: {path.name} size={stat.st_size} mtime={stat.st_mtime}")
    except OSError:
        pass
    result = redact(
        "\n".join(lines), username=username, home=home, hostname=hostname, mask=False
    )
    encoded = result.encode("utf-8")
    if len(encoded) > min(max_bytes, MAX_REPORT_BYTES):
        suffix = encoded[-(min(max_bytes, MAX_REPORT_BYTES) - 80) :].decode(
            "utf-8", errors="replace"
        )
        result = "# Nadare support report (truncated; newest content kept)\n\n" + suffix
    return result


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
        return [child for item in value if (child := _safe_setting("", item)) is not _DROP]
    if isinstance(value, str) and ("/" in value or value.startswith("~")):
        return Path(value).name
    return value
