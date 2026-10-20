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
import logging
import signal
import sys
from collections.abc import Sequence
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

#: Backend operations the CLI can invoke by name.
COMMANDS = (
    "gui",
    "list",
    "doctor",
    "status",
    "install",
    "update",
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
    sub = parser.add_subparsers(dest="command")

    main_help = {
        "gui": "open the graphical launcher",
        "list": "list configured profiles",
        "doctor": "report GPU/Vulkan, DXVK readiness and runner freshness",
        "status": "show a profile's status",
        "install": "install the game for a profile",
        "update": "update the game for a profile",
        "login": "run the profile's login step",
        "launch": "launch the game and stream its log",
        "stop": "stop a running game",
        "verify": "verify a profile's files",
    }
    for command in COMMANDS:
        sp = sub.add_parser(command, help=main_help[command])
        sp.add_argument(
            "-p", "--profile", default=None, help="profile name (default: active profile)"
        )
        sp.add_argument(
            "-q", "--quiet", action="store_true", help="only show progress, done and errors"
        )
        if command in ("install", "update", "launch"):
            sp.add_argument(
                "--accept-account-notice",
                action="store_true",
                help="acknowledge the account-safety notice and remember it for this profile",
            )
        if command == "list":
            sp.add_argument("--json", action="store_true", help="machine-readable output")
        if command == "doctor":
            sp.add_argument(
                "--fgo", action="store_true", help="also report FGO host readiness"
            )

    art = sub.add_parser("fetch-art", help="download cached cover art for a profile")
    art.add_argument("-p", "--profile", default=None, help="profile name (default: active profile)")
    art.add_argument("--all", action="store_true", help="fetch art for every profile")

    shortcut = sub.add_parser(
        "shortcut", help="create or manage launcher and Steam shortcuts for a profile"
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
    actions.add_parser("list", help="list installed shortcuts")
    actions.add_parser("refresh", help="rewrite shortcuts for the current profiles")

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


def _run_doctor(config: Config, args: argparse.Namespace | None = None) -> int:
    """Print GPU/Vulkan capability, DXVK readiness and runner freshness."""
    from .core import backends, graphics, hostlibs, process, runners, sandbox

    info = graphics.vulkan_info()
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
    if graphics.dxvk_ok():
        ready = "DXVK assumed ready" if info.loader_only else "DXVK-ready"
    else:
        ready = "DXVK needs Vulkan >= 1.3"
    print(f"GPU/Vulkan: {detail} [{ready}]")
    if sandbox.in_flatpak():
        print("umu-run: not needed (Flatpak runs Proton directly)")
    else:
        found = process.which("umu-run")
        if found:
            print(f"umu-run: {found}")
        else:
            print("umu-run: not found (Proton then runs directly and needs 32-bit host libraries)")
        missing_libs = None if found else hostlibs.missing_32bit()
        if found:
            print("32-bit libs: not needed (umu-run supplies the Steam Linux Runtime)")
        elif missing_libs is None:
            print("32-bit libs: unknown")
        elif missing_libs:
            print(f"32-bit libs: missing {', '.join(missing_libs)}")
            fix = hostlibs.install_command(missing_libs, hostlibs.distro_family())
            print(f"  fix: {fix}" if fix else f"  {hostlibs.hint(missing_libs)}")
        else:
            print("32-bit libs: OK")
    from .core import secrets as _secrets

    ok, why = _secrets.keyring_diagnosis()
    print(f"keyring: {'OK' if ok else 'unavailable'} ({why})")
    unavailable = backends.unavailable()
    # A backend a build leaves out on purpose is not a fault; only report the
    # others as MISSING.
    optional = [k for k in unavailable if k in backends.OPTIONAL_KINDS]
    missing = [k for k in unavailable if k not in backends.OPTIONAL_KINDS]
    expected = len(backends.kinds()) - len(optional)  # kinds() includes omitted ones
    if missing:
        loaded = expected - len(missing)
        print(f"backends: {loaded}/{expected} loaded, MISSING: {', '.join(missing)}")
    else:
        print(f"backends: {expected}/{expected} loaded")
    if optional:
        print(f"  not in this build (optional): {', '.join(optional)}")

    print("disk space:")
    for line in _disk_space_report(config):
        print(line)

    found = runners.all_runners()
    if not found:
        print("runners: none found")
        _print_fgo_readiness(config, args)
        return 0
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
    return 0


def _run_backend_command(
    args: argparse.Namespace, config: Config, name: str, cancel: Cancel, printer: Any
) -> int:
    backend = create_backend(name, config, emit=printer, cancel=cancel)
    command = args.command
    if command in ("install", "update", "launch") and backend.requires_notice_ack():
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
                "done",
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
        return int(backend.launch() or 0)
    if command == "verify":
        report = backend.verify()
        printer(
            Event(
                "done",
                str(report.get("message", "")) or i18n.translate("nothing to verify"),
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
        if not entries:
            print(i18n.translate("no shortcuts installed"))
            return 0
        for path, owner in entries:
            print(f"{owner}\t{path}")
        return 0
    if action == "refresh":
        result = shortcuts.refresh_desktop_entries(
            config.profiles, desktop_dir=_desktop_dir()
        )
        for path in result["updated"]:
            print(f"updated {path}")
        for path in result["removed"]:
            print(f"removed {path}")
        return 0

    name = args.profile or config.active
    if not name or name not in config.profiles:
        raise shortcuts.ShortcutError(
            i18n.translate("no such profile: {name}", name=name or "")
        )
    profile = config.profiles[name]
    title = str(profile.get("title") or name)

    if action == "remove":
        removed = shortcuts.remove_desktop_entries(name, desktop_dir=_desktop_dir())
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
            print(i18n.translate("no Steam shortcut files found"))
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
    tls.ensure_ca_bundle()
    try:
        paths.ensure_dirs()
    except OSError as exc:
        print(f"[error] cannot create data directories: {exc}", file=sys.stderr)
        return 1
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
        return run_gui(config)

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
def run_gui(config: Config | None = None) -> int:
    """Start the PyQt6 GUI. Imported lazily so the CLI stays headless."""
    cfg = config or Config.load()
    try:
        from nadare.gui import run as gui_run  # type: ignore
    except Exception as exc:  # noqa: BLE001
        raise SystemExit(
            f"Nadare GUI is unavailable ({exc}). Install the GUI extras and retry; "
            f"the CLI is still available via `nadare --help`."
        ) from exc
    return int(gui_run(cfg) or 0)
