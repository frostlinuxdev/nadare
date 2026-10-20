# SPDX-License-Identifier: GPL-3.0-or-later
"""The FGO readiness report (injected probes; no real /proc, /dev or programs)."""

from __future__ import annotations

import dataclasses

from nadare.backends.fgo import readiness
from nadare.backends.fgo.readiness import (
    Check,
    Distro,
    Probe,
    as_dict,
    detect_distro,
    format_report,
    run_checks,
    summarize,
)
from nadare.core import sandbox


def make_probe(
    *,
    missing=(),
    files=None,
    existing=None,
    modules=None,
    gpu_vendors=("0x10de",),
    environ=None,
    writable=True,
    user="tester",
    free=20 * (1 << 30),
    release="6.12.0-test",
    os_release_text="ID=arch\n",
    which=None,
) -> Probe:
    """A healthy host whose pieces tests can break one at a time."""
    files_map = {
        "/proc/filesystems": "nodev\tbinder\n",
        "/proc/sys/user/max_user_namespaces": "15000\n",
        "/etc/subuid": f"{user}:100000:65536\n",
        "/etc/subgid": f"{user}:100000:65536\n",
    }
    for index, vendor in enumerate(gpu_vendors):
        files_map[f"/sys/class/drm/renderD{128 + index}/device/vendor"] = f"{vendor}\n"
    files_map.update(files or {})

    env = {"XDG_RUNTIME_DIR": "/run/user/1000", "WAYLAND_DISPLAY": "wayland-0"}
    env.update(environ or {})
    runtime = env.get("XDG_RUNTIME_DIR", "")
    display = env.get("WAYLAND_DISPLAY", "")
    wayland_socket = f"{runtime.rstrip('/')}/{display}" if runtime and display else ""

    base_existing = {"/dev/udmabuf", "/sys/fs/cgroup/cgroup.controllers"}
    if wayland_socket:
        base_existing.add(wayland_socket)
    existing_set = base_existing if existing is None else set(existing)

    missing_set = set(missing)

    def _which(name: str) -> str | None:
        if which is not None:
            return which(name)
        if name in ("setfattr", "getfattr") and "attr" in missing_set:
            return None
        return None if name in missing_set else f"/usr/bin/{name}"

    def _glob(pattern: str) -> list[str]:
        if "renderD" in pattern:
            return [p for p in files_map if p.endswith("/device/vendor")]
        if "binder_linux" in pattern:
            return list(modules or [])
        return []

    def _free() -> int:
        return free() if callable(free) else free

    return Probe(
        which=_which,
        read_text=lambda path: files_map.get(path, ""),
        exists=lambda path: path in existing_set,
        glob=_glob,
        writable=lambda path: writable,
        uname_release=lambda: release,
        os_release=lambda: os_release_text,
        environ=env,
        getuser=lambda: user,
        free_bytes=_free,
    )


def _by_name(checks: list[Check]) -> dict[str, Check]:
    return {check.name: check for check in checks}


# ---- distro detection ------------------------------------------------------
def test_detect_arch() -> None:
    assert detect_distro("ID=arch\n").family == "arch"


def test_detect_cachyos_like_arch() -> None:
    distro = detect_distro('ID=cachyos\nID_LIKE="arch"\n')
    assert distro.family == "arch"
    assert distro.id == "cachyos"


def test_detect_ubuntu_like_debian() -> None:
    assert detect_distro('ID=ubuntu\nID_LIKE="debian"\n').family == "debian"


def test_detect_fedora() -> None:
    assert detect_distro("ID=fedora\n").family == "fedora"


def test_detect_opensuse_tumbleweed() -> None:
    assert detect_distro("ID=opensuse-tumbleweed\n").family == "suse"


def test_detect_unknown() -> None:
    assert detect_distro("ID=plan9\n").family == "unknown"


# ---- tool fixes ------------------------------------------------------------
def test_missing_tools_one_combined_command_fedora() -> None:
    probe = make_probe(missing={"podman", "crun"}, os_release_text="ID=fedora\n")
    checks = _by_name(run_checks(probe))
    assert checks["podman"].fix == "dnf install podman crun"
    assert checks["crun"].fix == "dnf install podman crun"
    assert checks["podman"].ok is False


def test_missing_all_tools_arch_command() -> None:
    probe = make_probe(missing=set(readiness._TOOL_ORDER))
    checks = _by_name(run_checks(probe))
    assert checks["podman"].fix == (
        "pacman -S --needed podman crun catatonit attr inotify-tools python curl tar zstd"
    )


def test_missing_tools_unknown_family_lists_packages() -> None:
    probe = make_probe(missing={"curl"}, os_release_text="ID=plan9\n")
    checks = _by_name(run_checks(probe))
    assert checks["curl"].fix == "Install these packages: curl"


def test_tool_present_reports_path() -> None:
    probe = make_probe()
    checks = _by_name(run_checks(probe))
    assert checks["attr"].ok is True
    assert "setfattr" in checks["attr"].detail and "getfattr" in checks["attr"].detail


# ---- binder ----------------------------------------------------------------
def test_binder_binderfs_present() -> None:
    check = _by_name(run_checks(make_probe()))["kernel binder"]
    assert check.ok is True
    assert "binder" in check.detail


def test_binder_legacy_nodes_present() -> None:
    probe = make_probe(
        files={"/proc/filesystems": ""},
        existing={"/dev/binder", "/dev/hwbinder", "/dev/vndbinder"},
    )
    check = _by_name(run_checks(probe))["kernel binder"]
    assert check.ok is True
    assert "legacy" in check.detail


def test_binder_module_only_prints_load_fix() -> None:
    probe = make_probe(
        files={"/proc/filesystems": ""},
        modules=["/lib/modules/6.12.0-test/kernel/drivers/android/binder_linux.ko"],
    )
    check = _by_name(run_checks(probe))["kernel binder"]
    assert check.ok is False
    assert "binder_linux" in check.detail
    assert "options binder_linux devices=binder,hwbinder,vndbinder" in check.fix
    assert 'MODE="0666"' in check.fix


def test_binder_nothing_prints_kernel_message() -> None:
    probe = make_probe(files={"/proc/filesystems": ""})
    check = _by_name(run_checks(probe))["kernel binder"]
    assert check.ok is False
    assert "CONFIG_ANDROID_BINDERFS" in check.fix
    assert "no binder" in check.detail


# ---- other probes ----------------------------------------------------------
def test_userns_disabled() -> None:
    probe = make_probe(files={"/proc/sys/user/max_user_namespaces": "0\n"})
    check = _by_name(run_checks(probe))["unprivileged user namespaces"]
    assert check.ok is False
    assert "max_user_namespaces=0" in check.detail


def test_userns_clone_disabled_when_present() -> None:
    probe = make_probe(
        files={"/proc/sys/kernel/unprivileged_userns_clone": "0\n"},
        existing={"/proc/sys/kernel/unprivileged_userns_clone"},
    )
    check = _by_name(run_checks(probe))["unprivileged user namespaces"]
    assert check.ok is False
    assert "unprivileged_userns_clone=0" in check.detail


def test_subuid_missing_is_a_warning() -> None:
    probe = make_probe(files={"/etc/subuid": ""})
    check = _by_name(run_checks(probe))["subuid/subgid"]
    assert check.tier == "warning"
    assert check.ok is False
    assert "/etc/subuid" in check.detail


def test_selinux_enforcing_is_a_warning_with_setup_fix() -> None:
    probe = make_probe(
        files={"/sys/fs/selinux/enforce": "1\n"},
        existing={"/sys/fs/selinux/enforce"},
    )
    check = _by_name(run_checks(probe))["SELinux"]
    assert check.tier == "warning"
    assert check.ok is False
    assert "setup-host-selinux.sh" in check.fix


def test_only_intel_gpu_is_a_warning() -> None:
    check = _by_name(run_checks(make_probe(gpu_vendors=("0x8086",))))["GPU"]
    assert check.tier == "warning"
    assert check.ok is False
    assert "Intel" in check.detail


def test_nvidia_gpu_is_info() -> None:
    check = _by_name(run_checks(make_probe()))["GPU"]
    assert check.tier == "info"
    assert check.ok is True
    assert "NVIDIA proven" in check.detail


def test_wayland_socket_missing_is_blocking() -> None:
    probe = make_probe(existing=set())
    check = _by_name(run_checks(probe))["Wayland session"]
    assert check.tier == "blocking"
    assert check.ok is False


# ---- summarize / format ----------------------------------------------------
def test_summarize_counts() -> None:
    checks = [
        Check("a", "blocking", False, "x", "f"),
        Check("b", "blocking", True, "x"),
        Check("c", "warning", False, "x", "f"),
        Check("d", "info", True, "x"),
    ]
    ready, blocking, warnings = summarize(checks)
    assert (ready, blocking, warnings) == (False, 1, 1)


def test_summarize_ready_when_no_blocking() -> None:
    assert summarize([Check("a", "warning", False, "x", "f")]) == (True, 0, 1)


def test_format_report_orders_and_prints_every_fix() -> None:
    checks = [
        Check("tool", "blocking", False, "missing", "BLOCKING-FIX"),
        Check("selinux", "warning", False, "enforcing", "WARNING-FIX"),
        Check("gpu", "info", True, "NVIDIA", ""),
        Check("python3", "blocking", True, "found at /usr/bin/python3"),
    ]
    text = format_report(checks, Distro("cachyos", "arch", "arch"))
    assert text.index("BLOCKING") < text.index("WARNING") < text.index("OK / INFO")
    assert "BLOCKING-FIX" in text
    assert "WARNING-FIX" in text
    assert "[FAIL] tool" in text
    assert "[warn] selinux" in text
    assert "[info] gpu" in text
    assert "[ok] python3" in text


def test_as_dict_round_trips_fields() -> None:
    check = Check("x", "warning", False, "detail", "fix")
    assert as_dict([check]) == [
        {"name": "x", "tier": "warning", "ok": False, "detail": "detail", "fix": "fix"}
    ]


def test_invalid_tier_is_rejected() -> None:
    import pytest

    with pytest.raises(ValueError):
        Check("x", "nope", True, "detail")


# ---- flatpak ---------------------------------------------------------------
def test_flatpak_tool_lookup_uses_host_which(monkeypatch) -> None:
    monkeypatch.setattr(sandbox, "in_flatpak", lambda: True)
    monkeypatch.setattr(sandbox, "host_which", lambda name: f"/run/host/usr/bin/{name}")

    probe = dataclasses.replace(make_probe(), which=lambda name: sandbox.host_which(name))
    checks = _by_name(run_checks(probe))
    assert checks["podman"].ok is True
    assert "/run/host/usr/bin/podman" in checks["podman"].detail


def test_default_probe_which_routes_through_sandbox(monkeypatch) -> None:
    monkeypatch.setattr(sandbox, "host_which", lambda name: f"/host/{name}")
    # The default Probe field must call sandbox.host_which at probe time, not bind early.
    assert Probe().which("zstd") == "/host/zstd"


def test_disk_check_survives_backend_error() -> None:
    from nadare.core.backends import BackendError

    def boom() -> int:
        raise BackendError("cannot read free space")

    check = _by_name(run_checks(make_probe(free=boom)))["free disk"]
    assert check.ok is False and check.tier == "warning"


def test_flatpak_reads_modules_and_subids_from_the_host(monkeypatch) -> None:
    monkeypatch.setattr(sandbox, "in_flatpak", lambda: True)
    host_files = {
        "/run/host/etc/subuid": "tester:100000:65536\n",
        "/run/host/etc/subgid": "tester:100000:65536\n",
    }
    base = make_probe(existing=set(), files={"/proc/filesystems": ""})
    probe = dataclasses.replace(
        base,
        read_text=lambda p: (
            host_files.get(p, base.read_text(p)) if "/etc/sub" not in p or p in host_files else ""
        ),
        glob=lambda p: ["/x/binder_linux.ko"] if p.startswith("/run/host/usr/lib/modules/") else [],
    )
    checks = _by_name(run_checks(probe))
    assert checks["subuid/subgid"].ok is True
    assert "not active" in checks["kernel binder"].detail
