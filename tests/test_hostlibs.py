# SPDX-License-Identifier: GPL-3.0-or-later
"""Tests for the host 32-bit library preflight."""

from __future__ import annotations

import pytest

from nadare.core import hostlibs

_SAMPLE = """\
\tlibfreetype.so.6 (libc6,x86-64) => /usr/lib/x86_64-linux-gnu/libfreetype.so.6
\tlibfreetype.so.6 (libc6) => /usr/lib/i386-linux-gnu/libfreetype.so.6
\tlibudev.so.1 (libc6) => /usr/lib/i386-linux-gnu/libudev.so.1
\tlibc.so.6 (libc6,AArch64) => /lib/aarch64-linux-gnu/libc.so.6
\tlibz.so.1 (libc6,x86-64) => /lib/x86_64-linux-gnu/libz.so.1
"""


def test_parse_ldconfig_splits_tags() -> None:
    sonames_32bit, sonames_64bit = hostlibs.parse_ldconfig(_SAMPLE)
    assert sonames_32bit == {"libfreetype.so.6", "libudev.so.1"}
    assert sonames_64bit == {"libfreetype.so.6", "libz.so.1"}
    # The AArch64 entry is ignored entirely.
    assert "libc.so.6" not in sonames_32bit
    assert "libc.so.6" not in sonames_64bit


def test_parse_ldconfig_empty() -> None:
    assert hostlibs.parse_ldconfig("") == (set(), set())


class _Result:
    def __init__(self, stdout: str) -> None:
        self.stdout = stdout
        self.returncode = 0


def _fake_run(stdout: str):
    calls: list[list[str]] = []

    def run(cmd, **kwargs):  # noqa: ANN001, ANN003
        calls.append(cmd)
        return _Result(stdout)

    return run, calls


def test_missing_32bit_reports_absent_in_order() -> None:
    text = (
        "\tlibfreetype.so.6 (libc6) => /usr/lib/i386-linux-gnu/libfreetype.so.6\n"
        "\tlibudev.so.1 (libc6) => /usr/lib/i386-linux-gnu/libudev.so.1\n"
    )
    run, _ = _fake_run(text)
    assert hostlibs.missing_32bit(run) == ["libXext.so.6", "libxkbcommon.so.0"]


def test_missing_32bit_all_present() -> None:
    text = "\n".join(
        f"\t{name} (libc6) => /usr/lib/i386-linux-gnu/{name}"
        for name in hostlibs.REQUIRED_32BIT
    )
    run, _ = _fake_run(text)
    assert hostlibs.missing_32bit(run) == []


def test_missing_32bit_empty_output_is_unknown() -> None:
    run, _ = _fake_run("")
    assert hostlibs.missing_32bit(run) is None


def test_missing_32bit_file_not_found_is_unknown() -> None:
    def run(cmd, **kwargs):  # noqa: ANN001, ANN003
        raise FileNotFoundError(cmd[0])

    assert hostlibs.missing_32bit(run) is None


def test_hint_contains_sonames_and_advice() -> None:
    text = hostlibs.hint(["libfreetype.so.6", "libudev.so.1"], os_release="ID=gentoo")
    assert "libfreetype.so.6" in text
    assert "libudev.so.1" in text
    assert "umu-launcher" in text
    assert "libfreetype6:i386" in text


_MISSING = ["libfreetype.so.6", "libXext.so.6", "libxkbcommon.so.0"]


@pytest.mark.parametrize(
    ("os_release", "expected"),
    [
        (
            "ID=fedora\nVERSION_ID=44",
            "sudo dnf install freetype.i686 libXext.i686 libxkbcommon.i686",
        ),
        (
            'ID=linuxmint\nID_LIKE="ubuntu debian"',
            "sudo dpkg --add-architecture i386 && sudo apt update && sudo apt install "
            "libfreetype6:i386 libxext6:i386 libxkbcommon0:i386",
        ),
        (
            "ID=cachyos\nID_LIKE=arch",
            "sudo pacman -S --needed lib32-freetype2 lib32-libxext lib32-libxkbcommon",
        ),
        (
            'ID=opensuse-tumbleweed\nID_LIKE="opensuse suse"',
            "sudo zypper install libfreetype6-32bit libXext6-32bit libxkbcommon0-32bit",
        ),
    ],
)
def test_hint_has_a_copy_paste_command_per_distro(os_release: str, expected: str) -> None:
    text = hostlibs.hint(_MISSING, os_release=os_release)
    assert expected in text
    assert "umu-launcher" in text  # the alternative is still offered


def test_hint_falls_back_for_an_unknown_distro() -> None:
    text = hostlibs.hint(_MISSING, os_release="ID=gentoo")
    assert "sudo " not in text
    assert "freetype.i686" in text and "lib32-freetype2" in text


def test_fedora_hint_warns_about_version_skew() -> None:
    assert "dnf upgrade" in hostlibs.hint(_MISSING, os_release="ID=fedora")


def test_install_command_ignores_unknown_sonames() -> None:
    assert hostlibs.install_command(["libnope.so.1"], "fedora") is None
    assert hostlibs.install_command(["libXext.so.6"], None) is None


def test_missing_gl_checks_the_64bit_entries_only() -> None:
    text = (
        "\tlibGL.so.1 (libc6) => /usr/lib/i386-linux-gnu/libGL.so.1\n"
        "\tlibEGL.so.1 (libc6,x86-64) => /usr/lib64/libEGL.so.1\n"
        "\tlibz.so.1 (libc6,x86-64) => /usr/lib64/libz.so.1\n"
    )
    run, _ = _fake_run(text)
    assert hostlibs.missing_gl(run) == ["libGL.so.1"]


def test_missing_gl_all_present_and_unknown() -> None:
    text = (
        "\tlibGL.so.1 (libc6,x86-64) => /usr/lib64/libGL.so.1\n"
        "\tlibEGL.so.1 (libc6,x86-64) => /usr/lib64/libEGL.so.1\n"
    )
    run, _ = _fake_run(text)
    assert hostlibs.missing_gl(run) == []
    run, _ = _fake_run("")
    assert hostlibs.missing_gl(run) is None


def test_gl_install_command_names_the_glvnd_packages() -> None:
    both = ["libGL.so.1", "libEGL.so.1"]
    assert hostlibs.gl_install_command(both, "fedora") == (
        "sudo dnf install libglvnd-glx libglvnd-egl"
    )
    assert hostlibs.gl_install_command(both, "arch") == "sudo pacman -S --needed libglvnd"
    # no i386 dance for 64-bit packages
    assert hostlibs.gl_install_command(both, "debian") == "sudo apt install libgl1 libegl1"
    assert hostlibs.gl_install_command(both, None) is None
