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
import threading
from collections.abc import Sequence
from datetime import date
from pathlib import Path
from typing import Any

from . import __version__
from .core import channel, flatpakreport, i18n, operation_policy, paths, tls, vkprobe
from .core import logging as nadare_logging
from .core.backends import BackendError, BackendNotInstalled, BackendUnsupported
from .core.backends import create as create_backend
from .core.config import Config, ConfigReadError
from .core.events import Cancel, Cancelled, Event

log = nadare_logging.get_logger("app")

#: Set after the root warning has been shown, so a CLI ``gui`` command does not
#: print it twice.
_root_warned = False


def _make_output_encoding_safe() -> None:
    """Never crash a CLI command because stdout cannot encode a character.

    Under ``LC_ALL=C`` (or a pipe with an ASCII encoding) ``print`` would raise
    ``UnicodeEncodeError`` on titles and reports; backslash-replace instead.
    """
    for stream in (sys.stdout, sys.stderr):
        reconfigure = getattr(stream, "reconfigure", None)
        if reconfigure is None:
            continue
        with contextlib.suppress(Exception):
            reconfigure(errors="backslashreplace")


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
    "predownload",
    "repair",
    "remake-prefix",
    "login",
    "launch",
    "stop",
    "verify",
)

#: Commands named in ``--help`` (#1567): what every desktop/Steam shortcut runs, the
#: app's own entry point, the diagnostics support asks for and the automation the
#: release matrix, the upgrade gate and the test harness call. The GUI is the product.
KEEP_COMMANDS = (
    "launch",
    "gui",
    "doctor",
    "report",
    "status",
    "list",
    "install",
    "update",
    "repair",
    "verify",
    "stop",
)

#: Everything else stays callable and unchanged, for scripts and deep support work,
#: but the help only names it on the ``Advanced:`` line (#1567).
ADVANCED_COMMANDS = (
    "predownload",
    "remake-prefix",
    "login",
    "fetch-art",
    "shortcut",
    "remove",
    "move-to-flatpak",
    "uninstall-all",
)


def _advanced_epilog() -> str:
    """The one place ``--help`` names the commands it does not list (#1567)."""
    return (
        "Advanced: "
        + ", ".join(ADVANCED_COMMANDS)
        + " — still available and unchanged, but not part of the supported CLI"
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
        epilog=_advanced_epilog(),
    )
    parser.add_argument(
        "--version", action="version", version=channel.title(f"nadare {__version__}")
    )
    parser.add_argument(
        "-c", "--config", type=Path, default=None, help="path to config.json (default: XDG config)"
    )
    parser.add_argument("-v", "--verbose", action="store_true", help="debug logging")
    parser.add_argument(
        "-q", "--quiet", action="store_true", help="only show progress, done and errors"
    )
    # Only the supported commands are advertised. The advanced ones stay in the
    # subparser map (callable as before) and are named on the epilog line (#1567).
    sub = parser.add_subparsers(
        dest="command", metavar="{" + ",".join(KEEP_COMMANDS) + "}"
    )

    main_help = {
        "gui": "open the graphical launcher",
        "list": "list configured profiles",
        "doctor": "report GPU/Vulkan, DXVK readiness and runner freshness",
        "report": "build a redacted local support report",
        "status": "show a profile's status",
        "install": "install the game for a profile",
        "update": "update the game for a profile",
        "predownload": (
            "download the next version ahead of its release, when the publisher offers it"
        ),
        "repair": "re-check every game file and re-download damaged or missing ones",
        "remake-prefix": "move the Wine prefix aside and rebuild it, keeping game files",
        "login": "run the profile's login step",
        "launch": "launch the game and stream its log",
        "stop": "stop a running game",
        "verify": "verify a profile's files",
    }
    for command in COMMANDS:
        if command in ADVANCED_COMMANDS:
            # Hidden from ``--help`` (#1567); ``--help`` on the command still
            # explains what it does, and the command runs exactly as before.
            sp = sub.add_parser(command, description=main_help[command])
        else:
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
        if command == "launch":
            sp.add_argument(
                "--ignore-hold",
                action="store_true",
                help="start even when the maintainers marked this game dangerous",
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

    art = sub.add_parser(
        "fetch-art", description="download cached cover art for a profile"
    )
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
        "shortcut",
        description="create or manage launcher and Steam shortcuts for a profile",
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
        description=(
            "remove a game from your library (files are kept unless you ask to delete them)"
        ),
    )
    remove_cmd.add_argument("-p", "--profile", default=None, help="profile name (required)")
    remove_cmd.add_argument(
        "--delete-data",
        action="store_true",
        help="also delete Nadare's profile data (settings, saved login, cached files)",
    )
    remove_cmd.add_argument(
        "--delete-install-dir",
        action="store_true",
        help="also delete the game's install folder (these are the actual game files)",
    )
    remove_cmd.add_argument(
        "--delete-prefix",
        action="store_true",
        help=(
            "also delete the game's Wine/Proton prefix (this holds the game's "
            "Windows files and save data)"
        ),
    )
    remove_cmd.add_argument(
        "--delete-backups",
        action="store_true",
        help="also delete the old prefix backups recorded after rebuilding the prefix",
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

    to_flatpak = sub.add_parser(
        "move-to-flatpak",
        description=(
            "move games and settings into the Nadare Flatpak, once (renames, never copies)"
        ),
    )
    to_flatpak.add_argument("--yes", action="store_true", help="confirm the move")

    uninstall_cmd = sub.add_parser(
        "uninstall-all",
        description=(
            "uninstall every game and remove Nadare's own folders (show the plan first)"
        ),
    )
    uninstall_cmd.add_argument(
        "--with-games",
        action="store_true",
        help="also delete game folders and prefixes Nadare made",
    )
    uninstall_cmd.add_argument(
        "--with-backups",
        action="store_true",
        help="also delete old prefix backups recorded after rebuilding the prefix",
    )
    uninstall_cmd.add_argument(
        "--keep-runtimes",
        action="store_true",
        help="keep downloaded Proton/Wine runners, umu-launcher and Nadare's umu runtime",
    )
    uninstall_cmd.add_argument(
        "--with-umu",
        action="store_true",
        help="also delete shared umu runtime downloads (other apps may use them, including "
        "Steam, Lutris, Heroic and other umu apps)",
    )
    uninstall_cmd.add_argument(
        "--remove-selinux-module",
        action="store_true",
        help="also remove FGO's SELinux policy module (lepton_binder), if it is loaded; "
        "this asks for your password",
    )
    uninstall_cmd.add_argument("--yes", action="store_true", help="confirm the uninstall")
    uninstall_cmd.add_argument(
        "-q",
        "--quiet",
        action="store_true",
        default=argparse.SUPPRESS,
        help="only show progress, done and errors",
    )
    uninstall_cmd.add_argument(
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
            f"mingw-w64: not found ({compiler}; only Arknights: Endfield could use it, and it "
            "falls back to its bundled launch helper)"
        )


def _print_sandbox_status() -> None:
    """Say whether the Sandbox can run here: bubblewrap, or Flatpak's own sandbox."""
    from .core import gamesandbox, sandbox

    if sandbox.in_flatpak():
        if gamesandbox._flatpak_spawn() is None:
            print("Sandbox: unavailable, flatpak-spawn is missing")
        else:
            print("Sandbox: available (Flatpak sandbox, flatpak-spawn --sandbox)")
        return
    # Only look (#965): recording here would pin, and then run, whatever a game left in the
    # runtime folder before the Sandbox's first real start.
    located = gamesandbox.locate(record=False)
    if located.binary is not None:
        kind = "the game runtime's copy" if located.source == "runtime" else "the system's copy"
        print(f"Sandbox: bubblewrap found ({kind}: {located.binary})")
    elif located.found:
        print(f"Sandbox: unavailable, bubblewrap cannot start here ({located.why})")
    else:
        print("Sandbox: unavailable, bubblewrap is not installed and no game runtime yet")


def _run_doctor(config: Config, args: argparse.Namespace | None = None) -> int:
    """Print a one-line verdict, then GPU/Vulkan capability, DXVK readiness and the rest.

    The verdict comes first so a newcomer reads the answer before twenty lines of detail
    (#1002); it names only what stops games from running. ``--strict`` is unchanged.
    """
    blockers: list[str] = []
    body = io.StringIO()
    completed = False
    try:
        with contextlib.redirect_stdout(body):
            code = _doctor_report(config, args, blockers)
        completed = True
    finally:
        if not completed:
            # A later probe failed: the diagnostics already collected are still useful.
            print(body.getvalue(), end="")
    if blockers:
        print(f"verdict: not ready: {'; '.join(blockers)} (the fix is under that line below)")
    else:
        print("verdict: ready to install and play (the lines below are details)")
    print(body.getvalue(), end="")
    return code


def _doctor_report(
    config: Config, args: argparse.Namespace | None, blockers: list[str]
) -> int:
    from .core import (
        backends,
        flatpakgl,
        graphics,
        hostlibs,
        process,
        runners,
        sandbox,
        umu,
        userns,
    )

    reasons: list[str] = []

    def finish() -> int:
        blockers.extend(reasons)
        if not dxvk_ready and info.available:
            # Not a --strict reason (never was), but games cannot start without it.
            blockers.append("the 32-bit Vulkan driver is missing or older than Vulkan 1.3")
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
        if info.driver == "software":
            # No GPU driver is in use at all (a VM without one, or a broken driver):
            # neither 32-bit packages nor a Flatpak update would help.
            print(
                "  fix: no GPU driver is in use. Install or repair your GPU's driver; in a "
                "virtual machine, games need a GPU passed through"
            )
        elif sandbox.in_flatpak():
            print(
                "  fix: run flatpak update to update its runtimes and graphics extensions; "
                "also check that the host GPU driver works"
            )
        else:
            family = hostlibs.distro_family()
            image = hostlibs.image_based()
            fix = {
                "debian": (
                    "sudo dpkg --add-architecture i386 && sudo apt update && "
                    "sudo apt install mesa-vulkan-drivers:i386"
                ),
                "fedora": "sudo dnf install mesa-vulkan-drivers.i686",
                "arch": "sudo pacman -S --needed lib32-vulkan-icd-loader lib32-vulkan-radeon",
            }.get(family or "")
            if image == "nixos":
                print(
                    "  fix: set hardware.graphics.enable32Bit = true; in your NixOS "
                    "configuration, then run sudo nixos-rebuild switch"
                )
            elif image == "steamos":
                print(
                    "  fix: SteamOS ships its GPU drivers with the system: update SteamOS, "
                    "or use the Flatpak"
                )
            elif fix:
                fix = hostlibs.for_host(fix, image) or fix
                print(f"  fix: {fix}")
                if fix.startswith("rpm-ostree "):
                    print("  (rpm-ostree packages apply after a restart)")
            else:
                print(
                    "  fix: install your GPU driver's 32-bit Vulkan support "
                    "(see docs/TESTERS.md), then run nadare doctor again"
                )
    if sandbox.in_flatpak():
        graphics_problem = flatpakgl.problem()
        if graphics_problem is None:
            summary = flatpakgl.check(flatpakgl.ROOT).summary
            print(f"Flatpak graphics: OK ({summary})")
        else:
            print(f"Flatpak graphics: {graphics_problem.summary}")
            print(f"  fix: {graphics_problem.fix}")
            reasons.append(
                "games will not start until the graphics package is installed"
            )
        managed = umu.managed()
        if managed is not None:
            print(f"umu-run: managed copy {managed} (games run in the Steam Runtime)")
        else:
            print("umu-run: set up at the first launch (games run in the Steam Runtime)")
        print("  Sandboxed games run Proton directly, without the Steam Runtime")
        print(f"umu runtime: {umu.runtime_base() / 'umu'} (Nadare's own)")
    else:
        # The order the launch uses (umu.resolve): Nadare's own pinned copy first.
        own = umu.managed()
        has_archive = own is None and umu._bundled_archive() is not None
        # Without /usr/bin/python3 the launch runs Nadare's own copy through the hidden
        # `nadare --umu-run` (#1341 item 3), so that interpreter is no longer a blocker.
        no_python = not umu.shebang_python_ok()
        bundled = has_archive
        found = (
            str(own)
            if own is not None
            else ("bundled" if bundled else umu.system("umu-run", which=process.which))
        )
        old_system = None if found else process.which("umu-run")
        if own is not None:
            how = (
                "runs with Nadare's own Python"
                if no_python
                else f"umu-launcher {umu.UMU_VERSION}"
            )
            print(f"umu-run: Nadare's own copy {own} ({how})")
        elif bundled:
            how = "runs with Nadare's own Python" if no_python else "no download"
            print(
                "umu-run: Nadare's own copy (set up at the first launch from the bundled "
                f"umu-launcher {umu.UMU_VERSION}, {how})"
            )
        elif found:
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
                    "umu-run: not found on the system (Nadare sets up its own copy at the first "
                    "launch; games run in the Steam Runtime)"
                )
            managed = umu.managed()
            if managed is not None:
                print(f"umu-run: managed copy {managed}")
        missing_libs = None if found else hostlibs.missing_32bit()
        outdated = own is not None and userns.runtime_profile_outdated()
        if outdated:
            print(userns.RUNTIME_MOVED_MESSAGE)
            print(userns.install_command())
        if outdated or (found and own is None and not bundled):
            print("umu runtime: shared (" + ", ".join(map(str, umu.shared_runtime_roots())) + ")")
        else:
            print(f"umu runtime: {umu.runtime_base() / 'umu'} (Nadare's own)")
        # The managed runtime brings its own 32-bit libraries, so missing host
        # libs only matter when that runtime cannot start (#429).
        sandbox_works = None
        if userns.restricted() and not userns.profile_installed():
            sandbox_works = userns.probe()
        # The same rule the launch uses (#1338): when it would refuse, the verdict says so.
        runtime_blocked = userns.blocks_runtime()
        if runtime_blocked:
            reasons.append(
                "Ubuntu's AppArmor blocks the Steam Runtime every Proton game runs in "
                "(one-time fix below)"
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
            fix = hostlibs.install_command(missing_libs, hostlibs.distro_family())
            print(
                f"  or install them: {fix}" if fix else f"  {hostlibs.hint(missing_libs)}"
            )
            if fix and fix.startswith("rpm-ostree "):
                print("  (rpm-ostree packages apply after a restart)")
        else:
            print("32-bit libs: OK")
        if not found:
            missing_gl = hostlibs.missing_gl()
            if missing_gl:
                print(f"GL libs: missing {', '.join(missing_gl)} (no window can open without them)")
                fix = hostlibs.gl_install_command(missing_gl, hostlibs.distro_family())
                if fix:
                    print(f"  fix: {fix}")
                    if fix.startswith("rpm-ostree "):
                        print("  (rpm-ostree packages apply after a restart)")
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
            print("  fix: run this command (this also avoids the 32-bit libraries)")
            for line in userns.install_command().splitlines():
                print(f"    {line}")
    _print_sandbox_status()
    from .core import holds as _holds

    hold_line = _holds.system_check_line()
    if hold_line:
        print(hold_line)
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
            flags.append(runners.standing(path))  # "older" or "untested"
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


#: ``redirect_stdout`` swaps the process-wide ``sys.stdout``, so two captures at once would
#: write into each other's buffer and restore it in the wrong order.
_DOCTOR_CAPTURE_LOCK = threading.Lock()


def _doctor_text(config: Config) -> str:
    """Capture the doctor output for embedding in a support report."""
    capture = io.StringIO()
    try:
        with _DOCTOR_CAPTURE_LOCK, contextlib.redirect_stdout(capture):
            _run_doctor(config)
    except Exception as exc:  # noqa: BLE001 - a partial report is still useful
        partial = capture.getvalue().strip()
        error = f"unavailable: {exc}"
        return f"{partial}\n{error}" if partial else error
    return capture.getvalue().strip()


#: CLI installation commands; the resource policy itself lives in core.
_LOCKED_COMMANDS = frozenset(
    command for command in COMMANDS
    if command.replace("-", "_") in operation_policy.POLICIES
    and operation_policy.policy_for(command).resources
)


def _operation_lock(command: str, name: str):
    """Use the shared profile resource lifetime (CLI spelling is accepted there)."""
    return operation_policy.operation_lock(command, name)


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
    if operation_policy.policy_for(command).cli_notice and backend.requires_notice_ack():
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
        # Refresh a stale published version with the GUI check's own interval, then report the
        # cached answer (status itself stays offline). Only that refresh: check_update also
        # tidies files, which a read-only command must not do.
        refresh = getattr(backend, "refresh_published_version", None)
        if callable(refresh):
            refresh()
        status = flatpakreport.status_of(backend)
        from .core.gamefacts import CommonStatus

        facts = CommonStatus.from_status(status)
        if facts.state == "not_installed":
            print(f"{name}: not installed")
        else:
            print(f"{name}: {facts.state}{' ' + facts.version if facts.version else ''}")
        if facts.logged_in is not None:
            print(f"  login: {'logged in' if facts.logged_in else 'not logged in'}")
        from .core import pending

        profiles = getattr(config, "profiles", None)
        profile = profiles.get(name, {}) if isinstance(profiles, dict) else {}
        leftovers = pending.removal_leftovers(profile)
        if leftovers and facts.state == "not_installed":  # a reinstall keeps the old record
            # r3b01 F1: a crash mid-Uninstall otherwise looks like a game never installed.
            print("  uninstall did not finish; still on disk: " + ", ".join(leftovers))
            record = profile.get(pending.REMOVAL)
            keys = set(record) if isinstance(record, dict) else set()
            flags = [
                flag
                for flag, wanted in (
                    ("--delete-install-dir", {"install_dir"}),
                    ("--delete-prefix", {"prefix", "wineprefix"}),
                )
                if keys & wanted
            ]
            command = " ".join(["nadare remove -p", name, *flags])
            print(f"  finish it in Settings > Uninstall or remove, or run: {command}")
        from .core import process

        message = facts.message or facts.state
        busy = {}
        if process.operation_in_progress(name):
            # r3b05/r3b07 F2: a half-downloaded game another process is installing right now
            # is not "incomplete, click RESUME"; every such advice ends in the busy error.
            print(
                "  busy: another Nadare window or command is installing, updating or removing "
                "it right now"
            )
            busy = {"busy_elsewhere": True}
            if facts.state == "not_installed" or facts.install.offers_resume():
                message = process.OPERATION_BUSY.format(name=name)
        printer(
            Event(
                "error" if facts.state == "error" else "done",
                message,
                data={
                    **(status.data if isinstance(status.data, dict) else {}),
                    "state": facts.state,
                    "version": facts.version,
                    "running": facts.running,
                    **busy,
                },
            )
        )
        return 1 if facts.state == "error" else 0
    if command == "install":
        with operation_policy.preflight("install", backend, name=name, adapter="cli"):
            backend.install()
        return 0
    if command == "update":
        backend.update()
        return 0
    if command == "predownload":
        if callable(getattr(backend, "predownload", None)):
            backend.predownload()
            return 0
        raise BackendError(f"{name}: this game has no predownload")
    if command == "repair":
        with operation_policy.preflight("repair", backend, name=name, adapter="cli"):
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
        return _launch_backend(backend, name, ignore_hold=getattr(args, "ignore_hold", False))
    if command == "verify":
        from .core import process

        if process.operation_in_progress(name):
            # Mid-install files are missing by design; "integrity check failed ... Run: nadare
            # install" would send the user into the busy error (r3b07 F2).
            raise BackendError(process.OPERATION_BUSY.format(name=name))
        try:
            report = backend.verify()
        except BackendError as exc:
            if isinstance(exc, BackendNotInstalled):
                raise BackendError(
                    f"Not installed. Run: nadare install -p {name}", exc.code
                ) from exc
            raise
        from .core.verification import VerifyResult

        result = VerifyResult.from_report(report)
        message = result.message
        if not message:
            if result.outcome == "passed":
                message = i18n.translate("all checks passed")
            else:
                message = i18n.translate("nothing to verify")
        if result.outcome == "not_installed":
            message = f"Not installed. Run: nadare install -p {name}"
        elif result.outcome == "failed" and "integrity check failed" in message.lower():
            message = f"{message}; Run: nadare install -p {name}"
        printer(Event("done", message, data=result.as_report()))
        return 0 if result.outcome == "passed" else 1
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


def _shortcut_refresh(config: Config) -> int:
    from .core import shortcuts

    # A recovered or empty config has no trustworthy profile list: every shortcut would
    # look orphaned, so refuse (same rule as the GUI) instead of deleting them.
    if config.notice or not config.profiles:
        print(
            "cannot refresh shortcuts: the config could not be read or has no profiles",
            file=sys.stderr,
        )
        return 1
    result = shortcuts.refresh_desktop_entries(
        config.profiles, desktop_dir=_desktop_dir()
    )
    steam = shortcuts.refresh_steam_shortcuts(
        config.profiles, argv=shortcuts.launcher_argv()
    )
    for path in result["updated"]:
        print(f"updated {path}")
    for path in result["removed"]:
        print(f"removed {path}")
    for warning in result.get("warnings", []):
        print(f"[warn] {warning}", file=sys.stderr)
    for path in steam["updated"]:
        print(f"updated {path}")
    for item in steam["removed"]:
        print(f"removed {item}")
    for warning in steam.get("warnings", []):
        print(f"[warn] {warning}", file=sys.stderr)
    return 1 if result.get("warnings") or steam.get("warnings") else 0


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
        return _shortcut_refresh(config)

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
        name,
        title,
        desktop_dir=_desktop_dir() if args.desktop else None,
        argv=argv,
        profiles=config.profiles,
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
            if shortcuts.add_steam_shortcut(
                path, name, title, argv, profiles=config.profiles
            ):
                print(f"installed {path}")
            else:
                print(f"already present {path}")
    return 0


def _check_install_space(backend: Any) -> None:
    """Refuse a fresh install the disk cannot hold, as the GUI does (#339 F-10).

    What is already in the folder (a resumed install) counts as written. A backend without a
    size or a folder installs as before.
    """
    from .core import diskspace

    diskspace.install_space(backend)


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
    profile = dict(config.profiles[name])
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
        config,
        name,
        delete_data=getattr(args, "delete_data", False),
        delete_trees=delete_trees,
        delete_backups=getattr(args, "delete_backups", False),
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
    from .core import fgo_selinux

    note = fgo_selinux.single_game_note(profile) if not result.profile_kept else ""
    for line in note.splitlines():
        print(f"  {line}")
    return 1 if result.profile_kept else 0


def _run_uninstall_all(args: argparse.Namespace, config: Config) -> int:
    from .core import fgo_selinux, uninstall_all

    remove_selinux = bool(getattr(args, "remove_selinux_module", False))
    selinux_loaded = fgo_selinux.available()
    fgo_profile = next(
        (
            profile
            for profile in config.profiles.values()
            if profile.get("backend") == "fgo-lepton"
        ),
        None,
    )
    selinux_command = (
        fgo_selinux.removal_command(fgo_profile) if selinux_loaded else ""
    )

    groups = uninstall_all.plan(config)
    if not getattr(args, "yes", False):
        states = {
            "nadare": True,
            "runtimes": not getattr(args, "keep_runtimes", False),
            "games": getattr(args, "with_games", False),
            "backups": getattr(args, "with_backups", False),
            "umu": getattr(args, "with_umu", False),
        }
        for group in groups:
            state = "on" if states.get(group.key, group.default) else "off"
            print(f"{state} {group.label}")
            if group.note:
                print(f"  ({group.note})")
            for path in group.paths:
                print(f"  {path}")
        if selinux_loaded:
            state = "on" if remove_selinux else "off"
            print(f"{state} Also remove {fgo_selinux.NOTE}: {selinux_command}")
        elif remove_selinux:
            print(f"off {fgo_selinux.NOTE} is not loaded; nothing to remove")
        print("nothing was deleted; add --yes to do it")
        return 0

    before = set(config.profiles)
    report = uninstall_all.run(
        config,
        games=getattr(args, "with_games", False),
        backups=getattr(args, "with_backups", False),
        runtimes=not getattr(args, "keep_runtimes", False),
        umu=getattr(args, "with_umu", False),
        applications_dir=uninstall_all._applications_dir(),
        desktop_dir=uninstall_all._desktop_dir(),
    )
    for name in report.profiles_removed:
        print(i18n.translate("removed {name}", name=name))
    for path in report.removed:
        print(f"  removed: {path}")
    for warning in report.warnings:
        print(f"  warning: {warning}")
    kept = any(name not in report.profiles_removed for name in before)
    selinux_failed = False
    if selinux_loaded:
        if report.refused or kept:
            kept_command = fgo_selinux.removal_command(fgo_profile)
            print(f"  kept {fgo_selinux.NOTE}; to remove it, run: {kept_command}")
        elif remove_selinux:
            ok, message = fgo_selinux.remove(fgo_profile)
            if ok:
                print(f"  {message}")
            else:
                print(f"  warning: {message}")
                selinux_failed = True
        else:
            kept_command = fgo_selinux.removal_command(fgo_profile)
            print(f"  kept {fgo_selinux.NOTE}; to remove it, run: {kept_command}")
    elif remove_selinux:
        print(f"  {fgo_selinux.NOTE} is not loaded; nothing to remove")
    return 1 if report.refused or kept or selinux_failed else 0


def _launch_backend(backend, name: str, *, ignore_hold: bool = False) -> int:
    """``nadare launch``: every pre-Play check, then the backend's own launch (#1451)."""
    from .core import launchgate

    try:
        return launchgate.play(backend, ignore_hold=ignore_hold)
    except BackendError as exc:
        if isinstance(exc, BackendNotInstalled):
            raise BackendError(
                f"Not installed. Run: nadare install -p {name}", exc.code
            ) from exc
        raise


def run_cli(argv: Sequence[str] | None = None) -> int:
    """Parse ``argv`` and run one command. Returns a process exit code."""
    items = list(argv) if argv is not None else sys.argv[1:]
    if items[:1] == ["--umu-run"]:
        # Hidden helper (#1341 item 3): run the managed umu zipapp with Nadare's own
        # interpreter, for hosts without /usr/bin/python3. Never in ``--help``.
        from .core import umu

        return umu.main(items[1:])
    if items[:1] == [vkprobe.SUBCOMMAND]:
        # The GPU probe's helper process (core/vkprobe.py): no config, logs or parser.
        return vkprobe.main()
    _make_output_encoding_safe()
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
    nadare_logging.install_stack_dump()
    if args.config is not None:
        try:
            found = args.config.exists()
        except OSError as exc:
            # exists() raises for some errnos, such as a name over 255 bytes (r3b07 F1).
            print(
                f"[error] cannot use config file {args.config}: {exc.strerror or exc}",
                file=sys.stderr,
            )
            return 2
        if not found:
            print(f"[error] config file not found: {args.config}", file=sys.stderr)
            return 2
    if args.config is None:
        # Before the settings are read: a move to the Flatpak a crash interrupted (#1353).
        from .core import flatpakmove

        try:
            notes = flatpakmove.finish_interrupted()
        except OSError as exc:
            notes = [f"An interrupted move to the Flatpak could not be finished: {exc}"]
        note = flatpakmove.moved_note()
        for line in [*notes, *([note] if note else [])]:
            print(f"[note] {line}", file=sys.stderr)
    try:
        config = Config.load(args.config)
    except ConfigReadError as exc:
        print(f"[error] {exc}", file=sys.stderr)
        return 1
    if config.notice:
        print(f"[warn] {config.notice}", file=sys.stderr)
    language = config.data.get("language")
    i18n.set_locale(i18n.resolve_locale(language if isinstance(language, str) else None))
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
            config_path=config.path,
        )
        if args.stdout:
            try:
                print(report)
                sys.stdout.flush()
            except BrokenPipeError:
                # A reader such as `| head` closed the pipe. Redirect stdout to /dev/null so the
                # interpreter's final flush does not print an "Exception ignored" either.
                try:
                    devnull = os.open(os.devnull, os.O_WRONLY)
                    try:
                        os.dup2(devnull, sys.stdout.fileno())
                    finally:
                        os.close(devnull)
                except (OSError, ValueError, AttributeError):
                    try:
                        sys.stdout = open(os.devnull, "w", encoding="utf-8")
                    except OSError:
                        pass
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

    if args.command == "move-to-flatpak":
        from .core import flatpakmove

        why = flatpakmove.refusal(config)
        if why:
            print(f"[error] {why}", file=sys.stderr)
            return 1
        data, _conf = flatpakmove.targets()
        if not args.yes:
            print(f"Would move your games and settings to {data.parent.parent} (renames "
                  "only). Run again with --yes to do it.")
            return 0
        try:
            notes = flatpakmove.move(config)
        except (flatpakmove.MoveRefused, OSError) as exc:
            print(f"[error] {exc}", file=sys.stderr)
            return 1
        for note in notes:
            print(note)
        return 0

    if args.command == "uninstall-all":
        try:
            return _run_uninstall_all(args, config)
        except BackendError as exc:
            print(f"[error] {exc.message}", file=sys.stderr)
            return 1
        except Exception as exc:  # noqa: BLE001
            log.error("uninstall-all failed: %s", exc, exc_info=True)
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
        with _operation_lock(args.command, name):
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
