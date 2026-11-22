# SPDX-License-Identifier: GPL-3.0-or-later
"""doctor's install hints on image-based systems and without a GPU driver.

Found on a Bazzite 44 (Kinoite) VM, 2026-11-18: doctor printed
``sudo dnf install mesa-vulkan-drivers.i686``. dnf cannot install on an image-based
system, Bazzite already ships that package, and the real cause was that no hardware
Vulkan driver was in use (a VM without a GPU).
"""

from __future__ import annotations

import pytest

from nadare import app
from nadare.core import graphics, hostlibs, process, runners

BAZZITE = 'NAME="Bazzite"\nID=bazzite\nID_LIKE="fedora"\nVARIANT_ID=bazzite\n'
KINOITE = "NAME=Fedora\nID=fedora\nVARIANT_ID=kinoite\n"
FEDORA = "NAME=Fedora\nID=fedora\nVARIANT_ID=workstation\n"
STEAMOS = 'NAME="SteamOS"\nID=steamos\nID_LIKE=arch\nVARIANT_ID=steamdeck\n'
ARCH = "NAME=Arch Linux\nID=arch\n"


@pytest.mark.parametrize(
    ("text", "booted", "kind"),
    [
        (BAZZITE, False, "ostree"),
        (KINOITE, False, "ostree"),
        (FEDORA, True, "ostree"),  # /run/ostree-booted is the authority
        (FEDORA, False, None),
        (STEAMOS, False, "steamos"),
        (ARCH, False, None),
    ],
)
def test_image_based_systems_are_recognised(text, booted, kind) -> None:
    assert hostlibs.image_based(text, ostree_booted=booted) == kind


def test_on_ostree_a_dnf_command_becomes_rpm_ostree() -> None:
    got = hostlibs.for_host("sudo dnf install a.i686 b.i686", "ostree")
    assert got == "rpm-ostree install a.i686 b.i686"


def test_on_steamos_there_is_no_package_command() -> None:
    assert hostlibs.for_host("sudo pacman -S --needed lib32-x", "steamos") is None


def test_elsewhere_the_command_is_unchanged() -> None:
    assert hostlibs.for_host("sudo dnf install x", None) == "sudo dnf install x"
    assert hostlibs.for_host(None, "ostree") is None


def test_library_commands_follow_the_host(monkeypatch) -> None:
    monkeypatch.setattr(hostlibs, "image_based", lambda *a, **k: "ostree")
    cmd = hostlibs.install_command(["libXext.so.6"], "fedora")
    assert cmd == "rpm-ostree install libXext.i686"
    gl = hostlibs.gl_install_command(["libGL.so.1"], "fedora")
    assert gl == "rpm-ostree install libglvnd-glx"
    monkeypatch.setattr(hostlibs, "image_based", lambda *a, **k: "steamos")
    assert hostlibs.install_command(["libXext.so.6"], "arch") is None


def _doctor(monkeypatch, capsys, *, info, family="fedora", image=None) -> str:
    monkeypatch.setattr(graphics, "vulkan_info", lambda **k: info)
    monkeypatch.setattr(graphics, "dxvk_ok", lambda: False)
    monkeypatch.setattr(hostlibs, "distro_family", lambda *a: family)
    monkeypatch.setattr(hostlibs, "image_based", lambda *a, **k: image)
    monkeypatch.setattr(process, "which", lambda name, **k: "/usr/bin/umu-run")
    monkeypatch.setattr(runners, "all_runners", list)
    app.run_cli(["doctor"])
    return capsys.readouterr().out


def test_software_only_vulkan_asks_for_a_gpu_driver_not_32_bit_packages(
    monkeypatch, capsys
) -> None:
    out = _doctor(monkeypatch, capsys, info=graphics.VulkanInfo(False, driver="software"))
    fix = [line for line in out.splitlines() if line.strip().startswith("fix:")][0]
    assert "mesa-vulkan-drivers" not in fix and "dnf" not in fix
    assert "GPU" in fix and "virtual machine" in fix


def test_a_missing_vulkan_driver_on_bazzite_gets_rpm_ostree(monkeypatch, capsys) -> None:
    out = _doctor(monkeypatch, capsys, info=graphics.VulkanInfo(False), image="ostree")
    assert "fix: rpm-ostree install mesa-vulkan-drivers.i686\n" in out
    assert "after a restart" in out and "sudo dnf" not in out


def test_the_console_copy_button_gets_a_clean_rpm_ostree_command(monkeypatch) -> None:
    monkeypatch.setattr(hostlibs, "image_based", lambda *a, **k: "ostree")
    message = hostlibs.hint(["libXext.so.6"], os_release=BAZZITE)
    assert hostlibs.install_command_in(message) == "rpm-ostree install libXext.i686"
    assert "after a restart" in message


def test_a_missing_vulkan_driver_on_steamos_points_at_the_os(monkeypatch, capsys) -> None:
    out = _doctor(
        monkeypatch, capsys, info=graphics.VulkanInfo(False), family="arch", image="steamos"
    )
    assert "pacman" not in out
    assert "SteamOS" in out


def test_software_only_vulkan_in_the_flatpak_does_not_say_flatpak_update(
    monkeypatch, capsys
) -> None:
    from nadare.core import sandbox

    monkeypatch.setattr(sandbox, "in_flatpak", lambda: True)
    out = _doctor(monkeypatch, capsys, info=graphics.VulkanInfo(False, driver="software"))
    fix = [line for line in out.splitlines() if line.strip().startswith("fix:")][0]
    assert "flatpak update" not in fix and "GPU passed through" in fix


NIXOS = 'NAME=NixOS\nID=nixos\nPRETTY_NAME="NixOS 26.05 (Yarara)"\n'


def test_nixos_is_recognised_and_gets_no_package_command() -> None:
    assert hostlibs.image_based(NIXOS, ostree_booted=False) == "nixos"
    assert hostlibs.for_host("sudo pacman -S x", "nixos") is None


def test_a_missing_vulkan_driver_on_nixos_names_the_config_option(monkeypatch, capsys) -> None:
    # Found on a NixOS 26.05 VM (appimage-run): doctor gave only the generic hint.
    out = _doctor(monkeypatch, capsys, info=graphics.VulkanInfo(False), family=None, image="nixos")
    fix = [line for line in out.splitlines() if line.strip().startswith("fix:")][0]
    assert "hardware.graphics.enable32Bit = true;" in fix and "nixos-rebuild" in fix
