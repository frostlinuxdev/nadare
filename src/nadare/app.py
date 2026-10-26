# SPDX-License-Identifier: GPL-3.0-or-later
# Copyright (C) 2026 Nadare contributors
"""Application entry points: the headless CLI and the GUI hook.

``run_cli`` parses the subcommands, loads the config, builds a backend through
``core.backends.create`` and streams its ``Event``s to stdout. ``run_gui`` is a
thin, lazy hook into ``nadare.gui`` so importing this module never pulls PyQt6
into a headless process.
"""

from __future__ import annotations

import argparse
import contextlib
import io
import logging
import os
import signal
import sys
from collections.abc import Sequence
from datetime import date
from pathlib import Path
from typing import Any

from . import __version__
from .core import i18n, paths, tls
from .core import logging as nadare_logging
from .core.backends import BackendError, BackendUnsupported
from .core.backends import create as create_backend
from .core.config import Config, ConfigReadError
from .core.events import Cancel, Cancelled, Event

log = nadare_logging.get_logger("app")

#: Set after the root warning has been shown, so a CLI ``gui`` command does not
#: print it twice.
_root_warned = False


def warn_if_root() -> None:
    """Warn once, in plain words, when Nadare runs as root.

    Running the launcher with sudo is a common "fix" that quietly moves every
    setting, game and Wine prefix under ``/root``, where the desktop user's
    later launch cannot see them.
    """
    global _root_warned
    if _root_warned or not hasattr(os, "geteuid") or os.geteuid() != 0:
        return
    _root_warned = True
    message = (
        "Nadare is running as root. Its settings, games and prefixes would be saved "
        "under /root instead of your own home folder. Run Nadare as your normal "
        "desktop user instead."
    )
    log.warning(message)
    print(f"[warn] {message}", file=sys.stderr)


#: Backend operations the CLI can invoke by name.
COMMANDS = (
    "gui",
    "list",
    "doctor",
    "report",
    "status",
    "install",
    "update",
    "repair",
    "remake-prefix",
    "login",
    "launch",
    "stop",
    "verify",
)


# ---------------------------------------------------------------------------
# output
# ---------------------------------------------------------------------------
def _make_printer(quiet: bool = False):
    last_was_progress = False

    def printer(event: Event) -> None:
        nonlocal last_was_progress
        if event.kind == "progress" and event.progress is not None:
            pct = max(0.0, min(1.0, event.progress)) * 100
            sys.stdout.write(f"\r  {pct:5.1f}%  {event.message}")
            sys.stdout.flush()
            last_was_progress = True
            return
        if last_was_progress:
            sys.stdout.write("\n")
            last_was_progress = False
        if quiet and event.kind in ("log", "status"):
            return
        line = event.message or event.kind
        prefix = {
            "error": "error",
            "done": "done",
            "status": "status",
            "log": "log",
            "step": "step",
            "progress": "progress",
        }.get(event.kind, event.kind)
        print(f"[{prefix}] {line}", flush=True)
        _print_qr(event)

    return printer


def _print_qr(event: Event) -> None:
    """Draw a login QR in an interactive terminal (never when piped/logged)."""
    png = (event.data or {}).get("png") if event.kind == "step" else None
    if not png or not sys.stdout.isatty():
        return
    from .core import qr_text

    art = qr_text.render_png(str(png))
    if art:
        print(art, flush=True)


# ---------------------------------------------------------------------------
# parser
# ---------------------------------------------------------------------------
def build_parser() -> argparse.ArgumentParser:
    parser = argparse.ArgumentParser(
        prog="nadare",
        description="Nadare — open-source Linux launcher for anime gacha games",
    )
    parser.add_argument("--version", action="version", version=f"nadare {__version__}")
    parser.add_argument(
        "-c", "--config", type=Path, default=None, help="path to config.json (default: XDG config)"
    )
    parser.add_argument("-v", "--verbose", action="store_true", help="debug logging")
    parser.add_argument(
        "-q", "--quiet", action="store_true", help="only show progress, done and errors"
    )
    sub = parser.add_subparsers(dest="command")

    main_help = {
        "gui": "open the graphical launcher",
        "list": "list configured profiles",
        "doctor": "report GPU/Vulkan, DXVK readiness and runner freshness",
        "report": "build a redacted local support report",
        "status": "show a profile's status",
        "install": "install the game for a profile",
        "update": "update the game for a profile",
        "repair": "re-check every game file and re-download damaged or missing ones",
        "remake-prefix": "move the Wine prefix aside and rebuild it, keeping game files",
        "login": "run the profile's login step",
        "launch": "launch the game and stream its log",
        "stop": "stop a running game",
        "verify": "verify a profile's files",
    }
    for command in COMMANDS:
        sp = sub.add_parser(command, help=main_help[command])
        if command not in ("list", "doctor"):
            sp.add_argument(
                "-p", "--profile", default=None, help="profile name (default: active profile)"
            )
        sp.add_argument(
            "-q",
            "--quiet",
            action="store_true",
            default=argparse.SUPPRESS,
            help="only show progress, done and errors",
        )
        sp.add_argument(
            "-v",
            "--verbose",
            action="store_true",
            default=argparse.SUPPRESS,
            help="debug logging",
        )
        if command in ("install", "update", "repair", "remake-prefix", "launch"):
            sp.add_argument(
                "--accept-account-notice",
                action="store_true",
                help="acknowledge the account-safety notice and remember it for this profile",
            )
        if command == "remake-prefix":
            sp.add_argument(
                "--yes",
                action="store_true",
                help="confirm rebuilding the prefix (game files are kept)",
            )
        if command == "list":
            sp.add_argument("--json", action="store_true", help="machine-readable output")
        if command == "doctor":
            sp.add_argument(
                "--fgo", action="store_true", help="also report FGO host readiness"
            )
            sp.add_argument(
                "--strict",
                action="store_true",
                help="exit non-zero when the host is not ready to run games",
            )
            sp.add_argument(
                "--tls",
                action="store_true",
                help="check whether this system's CA store trusts forged certificate chains",
            )
        if command == "report":
            sp.add_argument(
                "-o", "--output", type=Path, default=None, help="write report to this file"
            )
            sp.add_argument("--stdout", action="store_true", help="print only the report")

    art = sub.add_parser("fetch-art", help="download cached cover art for a profile")
    art.add_argument("-p", "--profile", default=None, help="profile name (default: active profile)")
    art.add_argument("--all", action="store_true", help="fetch art for every profile")
    art.add_argument(
        "-q",
        "--quiet",
        action="store_true",
        default=argparse.SUPPRESS,
        help="only show progress, done and errors",
    )
    art.add_argument(
        "-v", "--verbose", action="store_true", default=argparse.SUPPRESS, help="debug logging"
    )

    shortcut = sub.add_parser(
        "shortcut", help="create or manage launcher and Steam shortcuts for a profile"
    )
    shortcut.add_argument(
        "-q",
        "--quiet",
        action="store_true",
        default=argparse.SUPPRESS,
        help="only show progress, done and errors",
    )
    shortcut.add_argument(
        "-v", "--verbose", action="store_true", default=argparse.SUPPRESS, help="debug logging"
    )
    actions = shortcut.add_subparsers(dest="shortcut_action")
    add = actions.add_parser("add", help="install a shortcut for a profile")
    add.add_argument("-p", "--profile", default=None, help="profile name (default: active profile)")
    add.add_argument("--desktop", action="store_true", help="also put the entry on the Desktop")
    add.add_argument("--steam", action="store_true", help="also add a Steam non-Steam game")
    remove = actions.add_parser("remove", help="remove a profile's shortcuts")
    remove.add_argument(
        "-p", "--profile", default=None, help="profile name (default: active profile)"
    )
    list_cmd = actions.add_parser("list", help="list installed shortcuts")
    refresh = actions.add_parser("refresh", help="rewrite shortcuts for the current profiles")
    for action_parser in (add, remove, list_cmd, refresh):
        action_parser.add_argument(
            "-q",
            "--quiet",
            action="store_true",
            default=argparse.SUPPRESS,
            help="only show progress, done and errors",
        )
        action_parser.add_argument(
            "-v",
            "--verbose",
            action="store_true",
            default=argparse.SUPPRESS,
            help="debug logging",
        )

    remove_cmd = sub.add_parser(
        "remove",
        help="remove a game from the library (keeps game files unless --delete-data)",
    )
    remove_cmd.add_argument("-p", "--profile", default=None, help="profile name (required)")
    remove_cmd.add_argument(
        "--delete-data",
        action="store_true",
        help="also delete the profile's game data folder",
    )
    remove_cmd.add_argument(
        "--delete-install-dir",
        action="store_true",
        help="also delete the game's install folder",
    )
    remove_cmd.add_argument(
        "--delete-prefix",
        action="store_true",
        help="also delete the game's Wine prefix",
    )
    remove_cmd.add_argument("--yes", action="store_true", help="confirm the removal")
    remove_cmd.add_argument(
        "-q",
        "--quiet",
        action="store_true",
        default=argparse.SUPPRESS,
        help="only show progress, done and errors",
    )
    remove_cmd.add_argument(
        "-v", "--verbose", action="store_true", default=argparse.SUPPRESS, help="debug logging"
    )
    return parser


# ---------------------------------------------------------------------------
# CLI
# ---------------------------------------------------------------------------
def _resolve_profile(args: argparse.Namespace, config: Config) -> str:
    name = args.profile or config.active
    if not name:
        raise BackendError(i18n.translate("no profile selected; pass --profile"))
    if name not in config.profiles:
        raise BackendError(i18n.translate("no such profile: {name}", name=name))
    return name


def _run_list(args: argparse.Namespace, config: Config) -> int:
    profiles = config.profiles
    if args.json:
        import json

        rows = [
            {
                "name": name,
                "active": name == config.active,
                "backend": prof.get("backend", ""),
                "title": prof.get("title", ""),
            }
            for name, prof in profiles.items()
        ]
        print(json.dumps({"active": config.active, "profiles": rows}, indent=2))
        return 0
    if not profiles:
        print(i18n.translate("no profiles configured"))
        return 0
    print(f"{'PROFILE':<20} {'BACKEND':<14} {'TITLE'}")
    for name, prof in profiles.items():
        marker = "*" if name == config.active else " "
        print(f"{marker}{name:<19} {prof.get('backend', ''):<14} {prof.get('title', '')}")
    return 0


def _disk_space_report(config: Config) -> list[str]:
    """One line per filesystem mount used by the data dir and every profile."""
    from .core import diskspace

    candidates: list[object] = [paths.data_dir()]
    for name, profile in config.profiles.items():
        base = paths.profiles_dir() / name
        candidates.append(profile.get("install_dir") or base)
        prefix = profile.get("prefix") or profile.get("wineprefix")
        if prefix:
            candidates.append(prefix)

    lines: list[str] = []
    seen: set[str] = set()
    for candidate in candidates:
        try:
            mount = diskspace.mount_point(candidate)
        except OSError:
            continue
        if mount in seen:
            continue
        seen.add(mount)
        try:
            free = diskspace.human_size(diskspace.free_bytes(candidate))
        except (OSError, BackendError):
            free = "unknown"
        lines.append(f"  {free} free on {mount}")
    return lines


def _print_fgo_readiness(config: Config, args: argparse.Namespace | None) -> None:
    """Print the FGO readiness section when an FGO profile exists or ``--fgo``."""
    wanted = bool(args is not None and getattr(args, "fgo", False)) or any(
        profile.get("backend") == "fgo-lepton"
        for profile in (config.data.get("profiles") or {}).values()
    )
    if not wanted:
        return
    try:
        from .backends.fgo import readiness
    except ImportError:
        print("FGO support is not included in this build.")
        return
    checks = readiness.run_checks()
    print(readiness.format_report(checks, readiness.detect_distro()))


def _print_tls_diagnosis() -> None:
    """Opt-in check that the host CA store rejects known forged chains."""
    from .core import tls

    accepted = False
    for host, rejected in tls.check_forged_chain_hosts():
        if rejected is True:
            print(f"{host}: rejected (good)")
        elif rejected is False:
            print(
                f"{host}: ACCEPTED: your system trusts a certificate chain it should not"
            )
            accepted = True
        else:
            print(f"{host}: could not check (network error)")
    if not accepted:
        return
    print("  fix: remove the emSign* roots and Comodo_AAA_Services_root from the host CA store,")
    print("       then run `update-ca-certificates --fresh`.")
    print(
        '       In Chromium-based browsers, add an NSS distrust for the intermediate '
        '"EM DV TLS CA - G3B".'
    )
    print("       For Node, pass --use-openssl-ca so it uses the system CA store.")


def _print_optional_tools(which) -> None:
    """Informational lines for host tools only a few games need; never a doctor failure."""
    from .core import sevenzip

    kind, path = sevenzip.describe()
    games = "Arknights: Endfield and Neverness to Everness installs"
    if kind == "missing":
        print(f"7-Zip: not found (needed by {games})")
    else:
        print(f"7-Zip: {kind} {path}")
    compiler = os.environ.get("CC") or "x86_64-w64-mingw32-gcc"
    found = which(compiler)
    if found:
        print(f"mingw-w64: {found}")
    else:
        print(
            f"mingw-w64: not found ({compiler}; only Arknights: Endfield needs it, to build its "
            "launch helper)"
        )


def _run_doctor(config: Config, args: argparse.Namespace | None = None) -> int:
    """Print GPU/Vulkan capability, DXVK readiness and runner freshness."""
    from .core import backends, graphics, hostlibs, process, runners, sandbox, umu, userns

    reasons: list[str] = []

    def finish() -> int:
        if bool(getattr(args, "strict", False)) and reasons:
            print(f"doctor: not ready ({'; '.join(reasons)})")
            return 1
        return 0

    info = graphics.vulkan_info()
    if not info.available:
        reasons.append("Vulkan unavailable")
    if info.available and info.loader_only:
        detail = (
            f"Vulkan loader {info.api_version or '?'} (device version not verified; "
            "install vulkan-tools for an exact check)"
        )
    elif info.available:
        detail = f"Vulkan {info.api_version or '?'}"
        if info.device:
            detail += f" on {info.device}"
        if info.driver:
            detail += f" ({info.driver})"
    else:
        detail = (
            "Vulkan software-only (no hardware GPU)"
            if info.driver == "software"
            else "Vulkan unavailable"
        )
    dxvk_ready = graphics.dxvk_ok()
    if dxvk_ready:
        ready = "DXVK assumed ready" if info.loader_only else "DXVK-ready"
    else:
        ready = "DXVK needs Vulkan >= 1.3"
    print(f"GPU/Vulkan: {detail} [{ready}]")
    if not dxvk_ready:
        family = hostlibs.distro_family()
        fix = {
            "debian": (
                "sudo dpkg --add-architecture i386 && sudo apt update && "
                "sudo apt install mesa-vulkan-drivers:i386"
            ),
            "fedora": "sudo dnf install mesa-vulkan-drivers.i686",
            "arch": "sudo pacman -S --needed lib32-vulkan-icd-loader lib32-vulkan-radeon",
        }.get(family or "")
        if fix:
            print(f"  fix: {fix}")
        else:
            print(
                "  fix: install your GPU driver's 32-bit Vulkan support "
                "(see docs/TESTERS.md), then run nadare doctor again"
            )
    if sandbox.in_flatpak():
        print("umu-run: not needed (Flatpak runs Proton directly)")
    else:
        found = umu.system("umu-run", which=process.which)
        old_system = None if found else process.which("umu-run")
        if found:
            print(f"umu-run: {found}")
        else:
            if old_system:
                print(
                    f"umu-run: {old_system} is older than "
                    f"{'.'.join(map(str, umu.MIN_SYSTEM_VERSION))}, which the recommended "
                    "Proton needs; Nadare skips it and sets up its own copy at launch"
                )
            else:
                print(
                    "umu-run: not found (Nadare can set up its own copy at launch "
                    "if 32-bit libraries are missing)"
                )
            managed = umu.managed()
            if managed is not None:
                print(f"umu-run: managed copy {managed}")
        missing_libs = None if found else hostlibs.missing_32bit()
        # The managed runtime brings its own 32-bit libraries, so missing host
        # libs only matter when that runtime cannot start (#429).
        sandbox_works = None
        if userns.restricted() and not userns.profile_installed():
            sandbox_works = userns.probe()
        runtime_blocked = (
            userns.restricted() and not userns.profile_installed() and sandbox_works is not True
        )
        if found:
            print("32-bit libs: not needed (umu-run supplies the Steam Linux Runtime)")
        elif missing_libs is None:
            print("32-bit libs: unknown")
        elif missing_libs:
            print(f"32-bit libs: missing {', '.join(missing_libs)}")
            print(
                "  not needed while Nadare's own runtime works: it downloads one at "
                "launch and brings its own libraries"
            )
            if runtime_blocked:
                reasons.append("32-bit libs missing and the runtime sandbox is blocked")
            fix = hostlibs.install_command(missing_libs, hostlibs.distro_family())
            print(
                f"  or install them: {fix}" if fix else f"  {hostlibs.hint(missing_libs)}"
            )
        else:
            print("32-bit libs: OK")
        if not found:
            missing_gl = hostlibs.missing_gl()
            if missing_gl:
                print(f"GL libs: missing {', '.join(missing_gl)} (no window can open without them)")
                fix = hostlibs.gl_install_command(missing_gl, hostlibs.distro_family())
                if fix:
                    print(f"  fix: {fix}")
                reasons.append("GL libraries missing")
        if not userns.restricted():
            print("user namespaces: OK")
        elif userns.profile_installed():
            print(
                "user namespaces: restricted, Nadare's AppArmor profile is installed"
            )
        elif sandbox_works:
            print(
                "user namespaces: restricted by AppArmor, but the game's runtime "
                "sandbox starts fine (checked)"
            )
        else:
            if sandbox_works is False:
                print(
                    "user namespaces: blocked by AppArmor (Ubuntu 24.04+); "
                    "the game's runtime sandbox cannot start"
                )
            else:
                print(
                    "user namespaces: restricted by AppArmor (Ubuntu 24.04+); not "
                    "verified yet (the game's runtime is not downloaded), so its "
                    "sandbox may fail to start"
                )
            print("  fix: run these two commands (this also avoids the 32-bit libraries)")
            for line in userns.install_command().splitlines():
                print(f"    {line}")
    from .core import secrets as _secrets

    ok, why = _secrets.keyring_diagnosis()
    print(f"keyring: {'OK' if ok else 'unavailable'} ({why})")
    print(f"tls: CA list = {tls.ca_source()}")
    _print_optional_tools(process.which)
    unavailable = backends.unavailable()
    # A backend a build leaves out on purpose is not a fault; only report the
    # others as MISSING.
    optional = [k for k in unavailable if k in backends.OPTIONAL_KINDS]
    missing = [k for k in unavailable if k not in backends.OPTIONAL_KINDS]
    expected = len(backends.kinds()) - len(optional)  # kinds() includes omitted ones
    if missing:
        loaded = expected - len(missing)
        print(f"backends: {loaded}/{expected} loaded, MISSING: {', '.join(missing)}")
        if loaded == 0:
            reasons.append("no backends loaded")
    else:
        print(f"backends: {expected}/{expected} loaded")
    if optional:
        print(f"  not in this build (optional): {', '.join(optional)}")

    print("disk space:")
    for line in _disk_space_report(config):
        print(line)

    if bool(getattr(args, "tls", False)):
        _print_tls_diagnosis()

    found = runners.all_runners()
    if not found:
        print("runners: none found")
        print(
            "  next: run nadare install -p <profile> (or INSTALL in the GUI); "
            "it downloads the recommended Proton runner (~320 MB)"
        )
        _print_fgo_readiness(config, args)
        return finish()
    print("runners:")
    stale: list[dict[str, Any]] = []
    for path in found:
        row = runners.runner_info(path)
        latest = "unknown"
        if row["latest_tag"]:
            latest = str(row["latest_tag"])
            if row["latest_published"]:
                latest += f" ({str(row['latest_published'])[:10]})"
        flags = []
        if row["stale"]:
            flags.append("STALE")
        if row.get("recommended"):
            flags.append("RECOMMENDED")
        elif row.get("family"):
            flags.append("untested")
        if row["outdated"]:
            flags.append("OUTDATED")
        suffix = f"  [{', '.join(flags)}]" if flags else ""
        installed = row.get("tag") or row["version"]
        print(f"  {row['name']}  installed={installed}  upstream={latest}{suffix}")
        if row["stale"]:
            stale.append(row)
    for row in stale:
        hint = runners.stale_suggestion(row)
        if hint:
            print(f"  suggestion: {hint}")
    _print_fgo_readiness(config, args)
    return finish()


def _doctor_text(config: Config) -> str:
    """Capture the doctor output for embedding in a support report."""
    capture = io.StringIO()
    try:
        with contextlib.redirect_stdout(capture):
            _run_doctor(config)
    except Exception as exc:  # noqa: BLE001 - a partial report is still useful
        return f"unavailable: {exc}"
    return capture.getvalue().strip()


def _run_backend_command(
    args: argparse.Namespace, config: Config, name: str, cancel: Cancel, printer: Any
) -> int:
    backend = create_backend(name, config, emit=printer, cancel=cancel)
    command = args.command
    if command == "remake-prefix" and not getattr(args, "yes", False):
        print(
            i18n.translate(
                "would move the current Wine prefix aside, create a fresh one and set "
                "Windows components up again; your game files are kept and the old "
                "prefix is not deleted"
            )
        )
        print(
            i18n.translate("re-run with --yes to rebuild the Wine prefix"),
            file=sys.stderr,
        )
        return 2
    if command in ("install", "update", "repair", "remake-prefix", "launch") and (
        backend.requires_notice_ack()
    ):
        if getattr(args, "accept_account_notice", False):
            config.profile(name)["accept_account_notice"] = True
            config.save()
        else:
            print(backend.account_notice)
            print(
                i18n.translate(
                    "rerun with --accept-account-notice to accept the account notice"
                ),
                file=sys.stderr,
            )
            return 2
    if command == "status":
        status = backend.status()
        if status.state == "not_installed":
            print(f"{name}: not installed")
        else:
            print(f"{name}: {status.state}{' ' + status.version if status.version else ''}")
        if "logged_in" in status.data:
            print(f"  login: {'logged in' if status.data['logged_in'] else 'not logged in'}")
        printer(
            Event(
                "error" if status.state == "error" else "done",
                status.message or status.state,
                data={
                    "state": status.state,
                    "version": status.version,
                    "running": status.running,
                    **status.data,
                },
            )
        )
        return 1 if status.state == "error" else 0
    if command == "install":
        backend.install()
        return 0
    if command == "update":
        backend.update()
        return 0
    if command == "repair":
        backend.repair()
        return 0
    if command == "remake-prefix":
        backend.remake_prefix()
        return 0
    if command == "login":
        backend.login()
        return 0
    if command == "stop":
        was_running = bool(backend.status().running)
        backend.stop()
        if not was_running:
            print(f"{name}: not running")
            return 1
        print(f"{name}: stopped")
        return 0
    if command == "launch":
        try:
            return int(backend.launch() or 0)
        except BackendError as exc:
            if "not installed" in exc.message.lower():
                raise BackendError(
                    f"Not installed. Run: nadare install -p {name}", exc.code
                ) from exc
            raise
    if command == "verify":
        try:
            report = backend.verify()
        except BackendError as exc:
            if "not installed" in exc.message.lower():
                raise BackendError(
                    f"Not installed. Run: nadare install -p {name}", exc.code
                ) from exc
            raise
        message = str(report.get("message", ""))
        if not message:
            message = i18n.translate(
                "all checks passed" if report.get("verified", True) else "nothing to verify"
            )
        if not report.get("verified", True):
            if "not installed" in message.lower():
                message = f"Not installed. Run: nadare install -p {name}"
            elif "integrity check failed" in message.lower():
                message = f"{message}; Run: nadare install -p {name}"
        printer(
            Event(
                "done",
                message,
                data=dict(report),
            )
        )
        return 0 if report.get("verified", True) else 1
    raise BackendError(f"unhandled command: {command}")


def _run_fetch_art(args: argparse.Namespace, config: Config, printer: Any) -> int:
    from .core import artwork

    names = (
        list(config.profiles)
        if getattr(args, "all", False)
        else [_resolve_profile(args, config)]
    )
    if not names:
        print(i18n.translate("no profiles configured"))
        return 0
    changed = False
    for name in names:
        profile = config.profile(name)
        title = str(profile.get("title") or name)
        found = artwork.fetch_all(profile, title=title)
        if found:
            changed = True
            printer(Event("done", f"{name}: " + ", ".join(str(p) for p in found.values())))
        else:
            printer(Event("log", i18n.translate("no artwork found for {name}", name=name)))
    if changed:
        try:
            config.save()
        except OSError as exc:
            printer(Event("error", i18n.translate("operation failed: {message}", message=exc)))
            return 1
    return 0


def _desktop_dir() -> Path:
    """The user's Desktop directory (XDG when available, else ``~/Desktop``)."""
    from .core import shortcuts

    return shortcuts.desktop_dir()


def _run_shortcut(args: argparse.Namespace, config: Config) -> int:
    from .core import shortcuts

    action = getattr(args, "shortcut_action", None)
    if action is None:
        print("usage: nadare shortcut {add,remove,list,refresh}", file=sys.stderr)
        return 1
    if action == "list":
        entries = shortcuts.installed_desktop_entries(desktop_dir=_desktop_dir())
        steam_entries = shortcuts.installed_steam_shortcuts()
        if not entries and not steam_entries:
            print(i18n.translate("no shortcuts installed"))
            return 0
        for path, owner in entries:
            print(f"{owner}\t{path}")
        for path, owner, title in steam_entries:
            print(f"{owner}\t{path}\tSteam: {title}")
        return 0
    if action == "refresh":
        result = shortcuts.refresh_desktop_entries(
            config.profiles, desktop_dir=_desktop_dir()
        )
        steam = shortcuts.refresh_steam_shortcuts(config.profiles)
        for path in result["updated"]:
            print(f"updated {path}")
        for path in result["removed"]:
            print(f"removed {path}")
        for path in steam["updated"]:
            print(f"updated {path}")
        for item in steam["removed"]:
            print(f"removed {item}")
        for warning in steam["warnings"]:
            print(f"[warn] {warning}", file=sys.stderr)
        return 0

    name = args.profile or config.active
    if not name:
        raise shortcuts.ShortcutError(
            i18n.translate("no profile given; use -p <profile>")
        )
    if name not in config.profiles:
        known = ", ".join(config.profiles)
        message = i18n.translate("no such profile: {name}", name=name)
        if known:
            message += f"; configured profiles: {known}"
        raise shortcuts.ShortcutError(message)
    profile = config.profiles[name]
    title = str(profile.get("title") or name)

    if action == "remove":
        removed = shortcuts.remove_desktop_entries(name, desktop_dir=_desktop_dir())
        steam_removed, steam_warnings = shortcuts.remove_steam_shortcuts(name)
        removed += steam_removed
        for warning in steam_warnings:
            print(warning, file=sys.stderr)
        for path in removed:
            print(f"removed {path}")
        if not removed:
            print(i18n.translate("no shortcuts for {name}", name=name))
        return 0

    argv = shortcuts.launcher_argv()
    written = shortcuts.install_desktop_entry(
        name, title, desktop_dir=_desktop_dir() if args.desktop else None, argv=argv
    )
    for path in written:
        print(f"installed {path}")
    if args.steam:
        steam_files = shortcuts.find_steam_shortcut_files()
        if not steam_files:
            print(i18n.translate("no Steam shortcut files found"), file=sys.stderr)
            print(
                "[warn] start Steam once so it creates shortcuts.vdf, then retry",
                file=sys.stderr,
            )
            return 1
        for path in steam_files:
            if shortcuts.add_steam_shortcut(path, name, title, argv):
                print(f"installed {path}")
            else:
                print(f"already present {path}")
    return 0


def _run_remove(args: argparse.Namespace, config: Config) -> int:
    from .core import removal

    name = args.profile
    if not name:
        print(i18n.translate("pass --profile NAME"), file=sys.stderr)
        return 2
    if name not in config.profiles:
        print(
            f"[error] {i18n.translate('no such profile: {name}', name=name)}",
            file=sys.stderr,
        )
        return 1
    if not getattr(args, "yes", False):
        print(
            i18n.translate(
                "would remove {name}; re-run with --yes to confirm", name=name
            ),
            file=sys.stderr,
        )
        return 1
    delete_trees: list[str] = []
    if getattr(args, "delete_install_dir", False):
        delete_trees.append("install_dir")
    if getattr(args, "delete_prefix", False):
        delete_trees.extend(["prefix", "wineprefix"])
    result = removal.remove_game(
        config, name, delete_data=getattr(args, "delete_data", False),
        delete_trees=delete_trees,
        desktop_dir=_desktop_dir(),
    )
    if result.profile_kept:
        print(f"kept {name}: a requested deletion was refused or failed")
    else:
        print(i18n.translate("removed {name}", name=name))
    for path in result.shortcuts_removed:
        print(f"  shortcut: {path}")
    if result.data_dir_removed is not None:
        print(f"  data: {result.data_dir_removed}")
    for path in result.trees_removed:
        print(f"  deleted: {path}")
    for warning in result.warnings:
        print(f"  warning: {warning}")
    return 1 if result.profile_kept else 0


def run_cli(argv: Sequence[str] | None = None) -> int:
    """Parse ``argv`` and run one command. Returns a process exit code."""
    parser = build_parser()
    args = parser.parse_args(list(argv) if argv is not None else None)
    if args.command is None:
        args.command = "list"
        args.profile = None
        args.quiet = False
        args.json = False

    nadare_logging.setup(level=logging.DEBUG if args.verbose else logging.INFO)
    warn_if_root()
    tls.setup()
    try:
        paths.ensure_dirs()
    except OSError as exc:
        print(f"[error] cannot create data directories: {exc}", file=sys.stderr)
        return 1
    if args.config is not None and not args.config.exists():
        print(f"[error] config file not found: {args.config}", file=sys.stderr)
        return 2
    try:
        config = Config.load(args.config)
    except ConfigReadError as exc:
        print(f"[error] {exc}", file=sys.stderr)
        return 1
    if config.notice:
        print(f"[warn] {config.notice}", file=sys.stderr)
    language = config.data.get("language", "en")
    i18n.set_locale(language if isinstance(language, str) else "en")
    printer = _make_printer(quiet=getattr(args, "quiet", False))

    if args.command == "gui":
        profile = getattr(args, "profile", None)
        if profile and profile not in config.profiles:
            print(
                i18n.translate("no such profile: {name}", name=profile),
                file=sys.stderr,
            )
            return 2
        return run_gui(config, profile=profile)

    if args.command == "list":
        try:
            return _run_list(args, config)
        except Exception as exc:  # noqa: BLE001
            log.debug("list failed", exc_info=True)
            print(i18n.translate("operation failed: {message}", message=exc), file=sys.stderr)
            return 1

    if args.command == "doctor":
        try:
            return _run_doctor(config, args)
        except Exception as exc:  # noqa: BLE001
            log.debug("doctor failed", exc_info=True)
            print(i18n.translate("operation failed: {message}", message=exc), file=sys.stderr)
            return 1

    if args.command == "report":
        from .core.report import build_report

        report = build_report(
            config.data,
            profile_name=args.profile,
            doctor_text=_doctor_text(config),
        )
        if args.stdout:
            print(report)
            return 0
        target = args.output or Path.cwd() / (
            f"nadare-report-{args.profile or config.active or 'system'}-"
            f"{date.today().isoformat()}.txt"
        )
        try:
            target.parent.mkdir(parents=True, exist_ok=True)
            fd = os.open(target, os.O_WRONLY | os.O_CREAT | os.O_TRUNC, 0o600)
            with os.fdopen(fd, "w", encoding="utf-8") as stream:
                stream.write(report)
            os.chmod(target, 0o600)
        except OSError as exc:
            print(f"[error] cannot write report: {exc}", file=sys.stderr)
            return 1
        print(target)
        return 0

    if args.command == "fetch-art":
        try:
            return _run_fetch_art(args, config, printer)
        except BackendError as exc:
            print(f"[error] {exc.message}", file=sys.stderr)
            return exc.code
        except Exception as exc:  # noqa: BLE001
            log.error("fetch-art failed: %s", exc, exc_info=True)
            print(
                i18n.translate("operation failed: {message}", message=exc), file=sys.stderr
            )
            return 1

    if args.command == "shortcut":
        from .core import shortcuts

        try:
            return _run_shortcut(args, config)
        except shortcuts.ShortcutError as exc:
            print(f"[error] {exc}", file=sys.stderr)
            return 1
        except KeyError as exc:
            print(
                f"[error] {i18n.translate('no such profile: {name}', name=exc.args[0])}",
                file=sys.stderr,
            )
            return 1
        except Exception as exc:  # noqa: BLE001
            log.error("shortcut failed: %s", exc, exc_info=True)
            print(
                i18n.translate("operation failed: {message}", message=exc), file=sys.stderr
            )
            return 1

    if args.command == "remove":
        try:
            return _run_remove(args, config)
        except BackendError as exc:
            print(f"[error] {exc.message}", file=sys.stderr)
            return 1
        except Exception as exc:  # noqa: BLE001
            log.error("remove failed: %s", exc, exc_info=True)
            print(
                i18n.translate("operation failed: {message}", message=exc), file=sys.stderr
            )
            return 1

    cancel = Cancel()
    interrupts = 0
    old_handler = signal.getsignal(signal.SIGINT)

    def _on_sigint(_signum: int, _frame: Any) -> None:
        nonlocal interrupts
        interrupts += 1
        cancel.cancel()
        if interrupts == 1:
            print("\n[status] cancelling…", file=sys.stderr, flush=True)
        else:
            raise KeyboardInterrupt

    try:
        signal.signal(signal.SIGINT, _on_sigint)
    except ValueError:  # not on the main thread
        old_handler = None

    code = 0
    try:
        name = _resolve_profile(args, config)
        code = _run_backend_command(args, config, name, cancel, printer)
    except Cancelled:
        print(f"\n[{i18n.translate('cancelled')}]", file=sys.stderr)
        code = 130
    except KeyboardInterrupt:
        cancel.cancel()
        print(f"\n[{i18n.translate('cancelled')}]", file=sys.stderr)
        code = 130
    except BackendUnsupported as exc:
        print(f"[unsupported] {exc.message}", file=sys.stderr)
        code = exc.code
    except BackendError as exc:
        print(f"[error] {exc.message}", file=sys.stderr)
        code = exc.code
    except KeyError as exc:
        print(
            f"[error] {i18n.translate('no such profile: {name}', name=exc.args[0])}",
            file=sys.stderr,
        )
        code = 1
    except Exception as exc:  # noqa: BLE001
        log.error("command %s failed: %s", args.command, exc, exc_info=True)
        print(
            f"[error] {i18n.translate('operation failed: {message}', message=exc)}", file=sys.stderr
        )
        code = 1
    finally:
        if old_handler is not None:
            try:
                signal.signal(signal.SIGINT, old_handler)
            except (ValueError, TypeError):
                pass
    return code


# ---------------------------------------------------------------------------
# GUI
# ---------------------------------------------------------------------------
def run_gui(config: Config | None = None, *, profile: str | None = None) -> int:
    """Start the PyQt6 GUI. Imported lazily so the CLI stays headless."""
    cfg = config or Config.load()
    try:
        from nadare.gui import run as gui_run  # type: ignore
    except Exception as exc:  # noqa: BLE001
        raise SystemExit(
            f"Nadare GUI is unavailable ({exc}). Install the GUI extras and retry; "
            f"the CLI is still available via `nadare --help`."
        ) from exc
    return int(gui_run(cfg, profile=profile) or 0)
