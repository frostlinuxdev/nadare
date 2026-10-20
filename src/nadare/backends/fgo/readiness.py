# SPDX-License-Identifier: GPL-3.0-or-later
# Copyright (C) 2026 Nadare contributors
"""FGO host readiness report: exactly what is missing and the distro fix.

``fgo-lepton`` runs inside a podman/crun Android container and fails with vague
messages when the host is not prepared.  This module probes the host read-only
and turns each requirement into a :class:`Check` that carries a tier, a
displayable result and a copy-paste fix.  Every probe goes through :class:`Probe`
so the report can be tested without touching /proc, /dev or running a program.
"""

from __future__ import annotations

import getpass
import glob as _glob_module
import os
import platform
from collections.abc import Callable, Mapping
from dataclasses import dataclass, field

from ...core import diskspace, paths, sandbox
from ...core.backends import BackendError

__all__ = [
    "Check",
    "Distro",
    "Probe",
    "as_dict",
    "detect_distro",
    "format_report",
    "install_command",
    "run_checks",
    "summarize",
]

TIERS = ("blocking", "warning", "info")


# ---------------------------------------------------------------------------
# model
# ---------------------------------------------------------------------------
@dataclass(frozen=True)
class Check:
    """One readiness probe: a displayable result plus its fix (``""`` if none)."""

    name: str
    tier: str
    ok: bool
    detail: str
    fix: str = ""

    def __post_init__(self) -> None:
        if self.tier not in TIERS:
            raise ValueError(f"tier must be one of {TIERS}, got {self.tier!r}")


@dataclass(frozen=True)
class Distro:
    """The host distribution, reduced to an install-command family."""

    id: str
    id_like: str
    family: str


# ---------------------------------------------------------------------------
# injectable probes
# ---------------------------------------------------------------------------
def _host_os_release_text() -> str:
    """The host's os-release text (the host copy when sandboxed)."""
    candidates = (
        ("/run/host/os-release", "/etc/os-release")
        if sandbox.in_flatpak()
        else ("/etc/os-release",)
    )
    for path in candidates:
        text = _read_text(path)
        if text:
            return text
    return ""


def _host_path(path: str) -> str:
    """``path`` as seen on the host (under /run/host when sandboxed)."""
    return "/run/host" + path if sandbox.in_flatpak() else path


def _read_text(path: str) -> str:
    try:
        with open(path, encoding="utf-8", errors="replace") as handle:
            return handle.read()
    except OSError:
        return ""


def _glob(pattern: str) -> list[str]:
    return _glob_module.glob(pattern, recursive=True)


def _writable(path: str) -> bool:
    return os.access(path, os.R_OK | os.W_OK)


@dataclass(frozen=True)
class Probe:
    """Every host read the checks perform, injectable for hermetic tests."""

    which: Callable[[str], str | None] = lambda name: sandbox.host_which(name)
    read_text: Callable[[str], str] = _read_text
    exists: Callable[[str], bool] = os.path.exists
    glob: Callable[[str], list[str]] = _glob
    writable: Callable[[str], bool] = _writable
    uname_release: Callable[[], str] = platform.release
    os_release: Callable[[], str] = _host_os_release_text
    environ: Mapping[str, str] = field(default_factory=lambda: dict(os.environ))
    getuser: Callable[[], str] = getpass.getuser
    free_bytes: Callable[[], int] = lambda: diskspace.free_bytes(paths.data_dir())


# ---------------------------------------------------------------------------
# distro detection
# ---------------------------------------------------------------------------
_FAMILY_IDS = (
    ("arch", frozenset({"arch", "cachyos", "endeavouros", "manjaro", "artix", "garuda"})),
    (
        "debian",
        frozenset(
            {"debian", "ubuntu", "linuxmint", "mint", "pop", "elementary", "zorin", "kali",
             "raspbian", "neon"}
        ),
    ),
    (
        "fedora",
        frozenset({"fedora", "rhel", "centos", "rocky", "almalinux", "bazzite", "nobara"}),
    ),
    (
        "suse",
        frozenset({"suse", "opensuse", "opensuse-tumbleweed", "opensuse-leap",
                   "opensuse-slowroll", "sled"}),
    ),
)


def _family(ids: set[str]) -> str:
    for family, members in _FAMILY_IDS:
        if ids & members:
            return family
    return "unknown"


def _parse_os_release(text: str) -> dict[str, str]:
    values: dict[str, str] = {}
    for raw in text.splitlines():
        line = raw.strip()
        if not line or line.startswith("#") or "=" not in line:
            continue
        key, _, val = line.partition("=")
        val = val.strip()
        if len(val) >= 2 and val[0] == val[-1] and val[0] in ("'", '"'):
            val = val[1:-1]
        values[key.strip()] = val
    return values


def detect_distro(os_release_text: str | None = None) -> Distro:
    """Parse ``ID``/``ID_LIKE`` into a :class:`Distro` (family ``unknown`` if none)."""
    text = _host_os_release_text() if os_release_text is None else os_release_text
    values = _parse_os_release(text)
    distro_id = values.get("ID", "")
    id_like = values.get("ID_LIKE", "")
    ids = set(distro_id.lower().split()) | set(id_like.lower().split())
    return Distro(id=distro_id, id_like=id_like, family=_family(ids))


# ---------------------------------------------------------------------------
# tool fixes
# ---------------------------------------------------------------------------
_TOOL_ORDER = (
    "podman",
    "crun",
    "catatonit",
    "attr",
    "inotifywait",
    "python3",
    "curl",
    "tar",
    "zstd",
)

_PACKAGE = {
    "podman": "podman",
    "crun": "crun",
    "catatonit": "catatonit",
    "attr": "attr",
    "inotifywait": "inotify-tools",
    "python3": "python3",
    "curl": "curl",
    "tar": "tar",
    "zstd": "zstd",
}

_INSTALL_COMMAND = {
    "arch": "pacman -S --needed",
    "debian": "apt install",
    "fedora": "dnf install",
    "suse": "zypper install",
}


def _package(tool: str, family: str) -> str:
    if tool == "python3" and family == "arch":
        return "python"
    return _PACKAGE[tool]


def install_command(missing_tools: list[str], family: str) -> str:
    """ONE combined install command for every missing tool (empty if none)."""
    packages: list[str] = []
    for tool in _TOOL_ORDER:
        if tool in missing_tools:
            package = _package(tool, family)
            if package not in packages:
                packages.append(package)
    if not packages:
        return ""
    if family in _INSTALL_COMMAND:
        return f"{_INSTALL_COMMAND[family]} {' '.join(packages)}"
    return "Install these packages: " + " ".join(packages)


# ---------------------------------------------------------------------------
# checks
# ---------------------------------------------------------------------------
_BINDER_LOAD_FIX = """\
# Load the binder kernel modules and recreate the legacy device nodes:
sudo tee /etc/modules-load.d/lepton-binder.conf >/dev/null <<'EOF'
binder_linux
EOF
sudo tee /etc/modprobe.d/lepton-binder.conf >/dev/null <<'EOF'
options binder_linux devices=binder,hwbinder,vndbinder
EOF
sudo modprobe binder_linux
sudo tee /etc/udev/rules.d/99-lepton-binder.rules >/dev/null <<'EOF'
KERNEL=="binder*", SUBSYSTEM=="misc", MODE="0666"
EOF
sudo udevadm control --reload-rules && sudo udevadm trigger"""

_BINDER_KERNEL_FIX = (
    "This kernel has no binder support. Use a kernel built with "
    "CONFIG_ANDROID_BINDERFS (stock CachyOS, Arch, Fedora and Ubuntu kernels all "
    "have it) and reboot."
)

_FREE_DISK_MIN = 15 * (1 << 30)


def _tool_checks(probe: Probe, family: str) -> list[Check]:
    found: dict[str, list[str]] = {}
    missing: list[str] = []
    for tool in _TOOL_ORDER:
        names = ("setfattr", "getfattr") if tool == "attr" else (tool,)
        locations = [probe.which(name) for name in names]
        if all(locations):
            found[tool] = [str(loc) for loc in locations]
        else:
            missing.append(tool)
    combined = install_command(missing, family)
    checks: list[Check] = []
    for tool in _TOOL_ORDER:
        if tool in found:
            checks.append(Check(tool, "blocking", True, "found at " + ", ".join(found[tool]), ""))
        else:
            checks.append(Check(tool, "blocking", False, "not found in PATH", combined))
    return checks


def _binder_check(probe: Probe) -> Check:
    filesystems = probe.read_text("/proc/filesystems")
    if "binder" in filesystems.split():
        return Check(
            "kernel binder", "blocking", True, "binderfs available (/proc/filesystems lists binder)"
        )
    legacy = ("/dev/binder", "/dev/hwbinder", "/dev/vndbinder")
    if all(probe.exists(node) for node in legacy):
        return Check(
            "kernel binder",
            "blocking",
            True,
            "legacy binder nodes present at /dev/binder, /dev/hwbinder, /dev/vndbinder",
        )
    release = probe.uname_release()
    modules_root = _host_path("/usr/lib/modules" if sandbox.in_flatpak() else "/lib/modules")
    modules = probe.glob(f"{modules_root}/{release}/**/binder_linux.ko*")
    if modules:
        return Check(
            "kernel binder",
            "blocking",
            False,
            f"binder_linux module available at {modules[0]} but not active",
            _BINDER_LOAD_FIX,
        )
    return Check(
        "kernel binder",
        "blocking",
        False,
        f"no binder support: no binder_linux module under /lib/modules/{release}/",
        _BINDER_KERNEL_FIX,
    )


def _udmabuf_check(probe: Probe) -> Check:
    path = "/dev/udmabuf"
    if probe.exists(path) and probe.writable(path):
        return Check("udmabuf", "warning", True, f"{path} present and read/write", "")
    if probe.exists(path):
        return Check(
            "udmabuf",
            "warning",
            False,
            f"{path} present but not read/write for the user",
            "sudo chmod 666 /dev/udmabuf",
        )
    return Check(
        "udmabuf",
        "warning",
        False,
        f"{path} missing",
        "Use a kernel built with CONFIG_UDMABUF and reboot.",
    )


def _cgroups_check(probe: Probe) -> Check:
    path = "/sys/fs/cgroup/cgroup.controllers"
    if probe.exists(path):
        return Check("cgroups v2", "blocking", True, "unified hierarchy at /sys/fs/cgroup", "")
    return Check(
        "cgroups v2",
        "blocking",
        False,
        "cgroup v2 not mounted (missing cgroup.controllers)",
        "Boot the unified cgroup hierarchy (systemd.unified_cgroup_hierarchy=1).",
    )


def _userns_check(probe: Probe) -> Check:
    raw = probe.read_text("/proc/sys/user/max_user_namespaces").strip()
    try:
        maximum = int(raw)
    except ValueError:
        return Check(
            "unprivileged user namespaces",
            "blocking",
            False,
            "could not read /proc/sys/user/max_user_namespaces",
            "sudo sysctl -w user.max_user_namespaces=15000",
        )
    if maximum <= 0:
        return Check(
            "unprivileged user namespaces",
            "blocking",
            False,
            f"max_user_namespaces={maximum}",
            "sudo sysctl -w user.max_user_namespaces=15000",
        )
    clone_path = "/proc/sys/kernel/unprivileged_userns_clone"
    if probe.exists(clone_path) and probe.read_text(clone_path).strip() != "1":
        clone = probe.read_text(clone_path).strip() or "unreadable"
        return Check(
            "unprivileged user namespaces",
            "blocking",
            False,
            f"unprivileged_userns_clone={clone}",
            "sudo sysctl -w kernel.unprivileged_userns_clone=1",
        )
    return Check(
        "unprivileged user namespaces", "blocking", True, f"max_user_namespaces={maximum}", ""
    )


def _has_subid(text: str, user: str) -> bool:
    return any(line.split(":", 1)[0].strip() == user for line in text.splitlines())


def _subid_check(probe: Probe) -> Check:
    user = probe.getuser()
    missing = [
        path
        for path in ("/etc/subuid", "/etc/subgid")
        if not _has_subid(probe.read_text(_host_path(path)), user)
    ]
    if missing:
        return Check(
            "subuid/subgid",
            "warning",
            False,
            f"no {user!r} entry in {', '.join(missing)}",
            "sudo usermod --add-subuids 100000-165535 --add-subgids 100000-165535 " + user,
        )
    return Check(
        "subuid/subgid",
        "warning",
        True,
        f"{user} has a range in /etc/subuid and /etc/subgid",
        "",
    )


def _wayland_check(probe: Probe) -> Check:
    runtime = probe.environ.get("XDG_RUNTIME_DIR", "")
    display = probe.environ.get("WAYLAND_DISPLAY", "")
    if runtime and display:
        socket = f"{runtime.rstrip('/')}/{display}"
        if probe.exists(socket):
            return Check("Wayland session", "blocking", True, f"socket {socket}", "")
        detail = f"WAYLAND_DISPLAY={display} socket not found under {runtime}"
    else:
        detail = "WAYLAND_DISPLAY/XDG_RUNTIME_DIR not set"
    return Check(
        "Wayland session",
        "blocking",
        False,
        detail,
        "Start FGO from a Wayland session (the container needs the compositor socket).",
    )


_GPU_VENDORS = {"0x10de": "NVIDIA", "0x1002": "AMD", "0x8086": "Intel"}
_GPU_NOTE = "NVIDIA proven; AMD plausible; Intel not supported"


def _gpu_check(probe: Probe) -> Check:
    vendors: list[str] = []
    for node in sorted(probe.glob("/sys/class/drm/renderD*/device/vendor")):
        name = _GPU_VENDORS.get(probe.read_text(node).strip().lower())
        if name and name not in vendors:
            vendors.append(name)
    if not vendors:
        return Check("GPU", "info", True, f"no DRM render nodes found ({_GPU_NOTE})", "")
    if vendors == ["Intel"]:
        return Check(
            "GPU",
            "warning",
            False,
            f"only Intel detected ({_GPU_NOTE})",
            "FGO needs an NVIDIA or AMD GPU; Intel is not supported.",
        )
    return Check("GPU", "info", True, f"{', '.join(vendors)} ({_GPU_NOTE})", "")


def _selinux_check(probe: Probe) -> Check:
    path = "/sys/fs/selinux/enforce"
    if probe.exists(path) and probe.read_text(path).strip() == "1":
        return Check(
            "SELinux",
            "warning",
            False,
            "SELinux is enforcing",
            "Run the FGO setup (setup-host-selinux.sh) once as root.",
        )
    return Check("SELinux", "info", True, "not enforcing", "")


def _disk_check(probe: Probe) -> Check:
    try:
        free = probe.free_bytes()
    except (OSError, BackendError):
        return Check(
            "free disk",
            "warning",
            False,
            "could not determine free space on the data dir",
            "Make sure the data directory is on a writable filesystem.",
        )
    gib = free / (1 << 30)
    if free >= _FREE_DISK_MIN:
        return Check("free disk", "warning", True, f"{gib:.1f} GiB free on the data dir", "")
    return Check(
        "free disk",
        "warning",
        False,
        f"only {gib:.1f} GiB free on the data dir (< 15 GiB)",
        "Free at least 15 GiB on the data directory filesystem.",
    )


def run_checks(probe: Probe | None = None) -> list[Check]:
    """Run every readiness probe against ``probe`` (the real host by default)."""
    probe = probe or Probe()
    distro = detect_distro(probe.os_release())
    checks: list[Check] = list(_tool_checks(probe, distro.family))
    checks.append(_binder_check(probe))
    checks.append(_udmabuf_check(probe))
    checks.append(_cgroups_check(probe))
    checks.append(_userns_check(probe))
    checks.append(_subid_check(probe))
    checks.append(_wayland_check(probe))
    checks.append(_gpu_check(probe))
    checks.append(_selinux_check(probe))
    checks.append(_disk_check(probe))
    return checks


# ---------------------------------------------------------------------------
# presentation
# ---------------------------------------------------------------------------
def summarize(checks: list[Check]) -> tuple[bool, int, int]:
    """Return ``(ready, blocking_failures, warning_failures)``."""
    blocking = sum(1 for c in checks if c.tier == "blocking" and not c.ok)
    warnings = sum(1 for c in checks if c.tier == "warning" and not c.ok)
    return (blocking == 0, blocking, warnings)


def format_report(checks: list[Check], distro: Distro) -> str:
    """Human report: BLOCKING, then WARNINGS, then one line per OK/INFO check."""
    blocking = [c for c in checks if c.tier == "blocking" and not c.ok]
    warnings = [c for c in checks if c.tier == "warning" and not c.ok]
    fine = [c for c in checks if c.ok or c.tier == "info"]

    lines = [f"FGO readiness: {distro.id or 'unknown'} ({distro.family})"]
    if blocking:
        lines.append(f"BLOCKING ({len(blocking)}):")
        for check in blocking:
            lines.append(f"  [FAIL] {check.name}: {check.detail}")
            lines.extend(f"    {line}" for line in check.fix.splitlines())
    if warnings:
        lines.append(f"WARNINGS ({len(warnings)}):")
        for check in warnings:
            lines.append(f"  [warn] {check.name}: {check.detail}")
            lines.extend(f"    {line}" for line in check.fix.splitlines())
    lines.append("OK / INFO:")
    for check in fine:
        mark = "info" if check.tier == "info" and check.ok else "ok"
        lines.append(f"  [{mark}] {check.name}: {check.detail}")
    return "\n".join(lines)


def as_dict(checks: list[Check]) -> list[dict[str, object]]:
    """JSON-serialisable view of the checks."""
    return [
        {"name": c.name, "tier": c.tier, "ok": c.ok, "detail": c.detail, "fix": c.fix}
        for c in checks
    ]
