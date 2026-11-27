# SPDX-License-Identifier: GPL-3.0-or-later
"""NVIDIA driver updated but the PC not restarted: say "restart your PC", not a game crash.

Seen 2026-11-15 (NVIDIA PC): pacman updated nvidia-utils 615.78.08 -> 620.70.06 with no reboot; the
loaded kernel module stayed 615.78.08, ``vkCreateInstance`` failed host-wide and Aether Gazer
crashed three times in DXVK. The library files keep the package's build time as mtime (Nov 7), so
"installed since boot" is read from their ctime.
"""

from __future__ import annotations

import time
from pathlib import Path

import pytest

from nadare.core import nvidiarestart
from nadare.core.backends import BackendError

OPEN = "NVRM version: NVIDIA UNIX Open Kernel Module for x86_64  {v}  Release Build  (root@pc)\n"
CLOSED = "NVRM version: NVIDIA UNIX x86_64 Kernel Module  {v}  Wed Oct  1 00:00:00 UTC 2026\n"


def _root(
    tmp_path: Path,
    *,
    kernel: str | None = "615.78.08",
    libs: tuple[str, ...] = ("620.70.06",),
    libdir: str = "usr/lib",
    booted_ago: float | None = 3600.0,
    banner: str = OPEN,
) -> Path:
    root = tmp_path / "root"
    proc = root / "proc"
    (proc / "driver" / "nvidia").mkdir(parents=True)
    if kernel is not None:
        (proc / "driver" / "nvidia" / "version").write_text(banner.format(v=kernel))
    else:
        (proc / "driver" / "nvidia").rmdir()
    if booted_ago is not None:
        btime = int(time.time() - booted_ago)
        (proc / "stat").write_text(f"cpu  1 2 3\nbtime {btime}\nprocesses 9\n")
    lib = root / libdir
    lib.mkdir(parents=True, exist_ok=True)
    for version in libs:
        (lib / f"libnvidia-glcore.so.{version}").write_bytes(b"\x7fELF")
        (lib / f"libGLX_nvidia.so.{version}").write_bytes(b"\x7fELF")
    (lib / "libGLX_nvidia.so.0").symlink_to(f"libGLX_nvidia.so.{libs[0]}") if libs else None
    return root


def test_an_update_since_boot_is_found(tmp_path):
    found = nvidiarestart.check(_root(tmp_path))
    assert found is not None
    assert found.running == "615.78.08"
    assert found.installed == ("620.70.06",)
    assert found.since_boot is True
    text = nvidiarestart.message(found)
    assert "restart" in text.lower() and "620.70.06" in text and "615.78.08" in text


def test_the_closed_driver_banner_is_read_too(tmp_path):
    found = nvidiarestart.check(_root(tmp_path, banner=CLOSED))
    assert found is not None and found.running == "615.78.08"


def test_matching_versions_are_fine(tmp_path):
    assert nvidiarestart.check(_root(tmp_path, kernel="620.70.06")) is None


def test_a_stale_extra_library_next_to_the_right_one_is_fine(tmp_path):
    root = _root(tmp_path, kernel="620.70.06", libs=("620.70.06", "615.78.08"))
    assert nvidiarestart.check(root) is None


@pytest.mark.parametrize(
    "libdir",
    [
        "usr/lib",  # Arch, CachyOS
        "usr/lib64",  # Fedora, openSUSE
        "usr/lib/x86_64-linux-gnu",  # Debian, Ubuntu
        "usr/lib/x86_64-linux-gnu/nvidia/current",  # Debian's packaged driver
    ],
)
def test_the_library_folders_of_the_big_distros(tmp_path, libdir):
    found = nvidiarestart.check(_root(tmp_path, libdir=libdir))
    assert found is not None and found.installed == ("620.70.06",)


def test_libraries_older_than_the_boot_are_not_a_restart_case(tmp_path):
    # A module that did not rebuild (DKMS failed) also mismatches: a restart will not fix it.
    found = nvidiarestart.check(_root(tmp_path, booted_ago=-3600.0))
    assert found is not None and found.since_boot is False
    assert "restart" not in nvidiarestart.message(found).lower()


def test_no_nvidia_driver_means_nothing(tmp_path):
    assert nvidiarestart.check(_root(tmp_path, kernel=None)) is None


def test_no_libraries_found_means_nothing(tmp_path):
    # NixOS, a container, an unusual layout: never guess.
    assert nvidiarestart.check(_root(tmp_path, libs=())) is None


def test_an_unreadable_boot_time_is_not_a_restart_case(tmp_path):
    found = nvidiarestart.check(_root(tmp_path, booted_ago=None))
    assert found is not None and found.since_boot is False


def test_names_that_are_not_versions_are_ignored(tmp_path):
    root = _root(tmp_path, kernel="620.70.06")
    (root / "usr/lib/libnvidia-glcore.so.1").write_bytes(b"")
    (root / "usr/lib/libnvidia-glcore.so.615.78.08.bak").write_bytes(b"")
    assert nvidiarestart.check(root) is None


# ---- the launch guard -------------------------------------------------------------------------
class _Backend:
    kind = "direct-wuwa"


def test_the_guard_refuses_a_launch_that_would_crash(tmp_path, monkeypatch):
    monkeypatch.setattr(nvidiarestart, "ROOT", _root(tmp_path))
    with pytest.raises(BackendError, match="(?i)restart your PC"):
        nvidiarestart.guard_launch(_Backend())


def test_the_guard_refuses_steam_games_too(tmp_path, monkeypatch):
    # Steam runs on the host's driver as well.
    monkeypatch.setattr(nvidiarestart, "ROOT", _root(tmp_path))
    backend = _Backend()
    backend.kind = "steam"
    with pytest.raises(BackendError):
        nvidiarestart.guard_launch(backend)


def test_the_guard_only_logs_a_mismatch_a_restart_cannot_fix(tmp_path, monkeypatch, caplog):
    monkeypatch.setattr(nvidiarestart, "ROOT", _root(tmp_path, booted_ago=-3600.0))
    nvidiarestart.guard_launch(_Backend())
    assert "615.78.08" in caplog.text


def test_the_guard_is_off_inside_the_flatpak(tmp_path, monkeypatch):
    # The Flatpak's own driver libraries follow the running module (flatpakgl checks those).
    monkeypatch.setattr(nvidiarestart, "ROOT", _root(tmp_path))
    monkeypatch.setattr(nvidiarestart.sandbox, "in_flatpak", lambda: True)
    nvidiarestart.guard_launch(_Backend())


def test_the_guard_never_breaks_a_launch_on_its_own_error(monkeypatch):
    def boom(_root):
        raise RuntimeError("unexpected")

    monkeypatch.setattr(nvidiarestart, "check", boom)
    nvidiarestart.guard_launch(_Backend())


def test_launch_paths_run_the_guard():
    # Both the GUI worker and `nadare launch` go through the one pre-Play gate (#1451).
    src = Path(nvidiarestart.__file__).resolve().parents[1]
    assert "nvidiarestart.guard_launch(backend)" in (src / "core/launchgate.py").read_text()


def _fake_times(monkeypatch, times):
    """``times``: version -> (mtime, ctime) for the library files of that version."""
    real = nvidiarestart.os.lstat

    def lstat(path):
        info = real(path)
        for version, (mtime, ctime) in times.items():
            if str(path).endswith(version):
                return type("St", (), {"st_mtime": mtime, "st_ctime": ctime})()
        return info

    monkeypatch.setattr(nvidiarestart.os, "lstat", lstat)


def test_a_package_keeps_its_build_time_as_mtime(tmp_path, monkeypatch):
    # an NVIDIA PC: libnvidia-glcore.so.620.70.06 says Nov 7 (built), installed Nov 15 after boot.
    now = time.time()
    _fake_times(monkeypatch, {"620.70.06": (now - 8 * 86400, now - 60)})
    found = nvidiarestart.check(_root(tmp_path))
    assert found is not None and found.since_boot is True


def test_an_old_leftover_next_to_the_new_libraries_still_says_restart(tmp_path, monkeypatch):
    now = time.time()
    _fake_times(
        monkeypatch,
        {"620.70.06": (now - 60, now - 60), "600.10.01": (now - 90 * 86400, now - 90 * 86400)},
    )
    found = nvidiarestart.check(_root(tmp_path, libs=("620.70.06", "600.10.01")))
    assert found is not None and found.since_boot is True
