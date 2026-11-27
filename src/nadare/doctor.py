# SPDX-License-Identifier: GPL-3.0-or-later
# Copyright (C) 2026 Nadare contributors
"""The ``doctor`` command: host diagnostics and the support-report text.

``run_cli`` calls these through :mod:`nadare.app`, which re-exports every name
here for callers and tests (#1601).
"""

from __future__ import annotations

import argparse
import contextlib
import io
import os
import threading
from typing import Any

from .core import paths, tls
from .core.backends import BackendError
from .core.config import Config


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
