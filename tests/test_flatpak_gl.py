# SPDX-License-Identifier: GPL-3.0-or-later
"""Inside the Flatpak, the host's graphics driver must have matching Flatpak packages.

NVIDIA's userspace comes from a Flatpak extension named after the exact host driver version
(``GL.nvidia-615-78-08`` and the 32-bit ``GL32.nvidia-615-78-08``). On a rolling distro the
driver updates first and games fail until the matching extension is installed, so Nadare says
so plainly, with the command that fixes it. Layout measured on an NVIDIA CachyOS host,
2026-11-09: ``/usr/lib/x86_64-linux-gnu/GL/<ext>`` and the app's
``/app/lib/i386-linux-gnu/GL/<ext>``.
"""

from __future__ import annotations

from pathlib import Path

import pytest

from nadare.core import flatpakgl

OPEN = (
    "NVRM version: NVIDIA UNIX Open Kernel Module for x86_64  615.78.08  Release Build  "
    "(root@flxpc)  Mon Oct 20 12:00:00 UTC 2026\n"
)
CLOSED = "NVRM version: NVIDIA UNIX x86_64 Kernel Module  550.54.14  Thu Feb 22 01:44:30 UTC 2024\n"


@pytest.mark.parametrize(("text", "version"), [(OPEN, "615.78.08"), (CLOSED, "550.54.14")])
def test_the_host_driver_version_is_read(text, version):
    assert flatpakgl.parse_nvidia_version(text) == version


def test_no_version_in_garbage():
    assert flatpakgl.parse_nvidia_version("") is None
    assert flatpakgl.parse_nvidia_version("something else entirely") is None


def test_extension_names_follow_flathub():
    assert flatpakgl.extension_name("615.78.08") == "nvidia-615-78-08"
    assert flatpakgl.extension_name("550.54.14") == "nvidia-550-54-14"


def _root(
    tmp_path: Path, *, nvidia: str | None, gl64: list[str], gl32: list[str], compat: bool = True
) -> Path:
    if compat:  # Compat.i386 is mounted at the app's lib/i386-linux-gnu
        marker = tmp_path / "app" / "lib" / "i386-linux-gnu" / "libc.so.6"
        marker.parent.mkdir(parents=True, exist_ok=True)
        marker.write_bytes(b"")
    if nvidia is not None:
        p = tmp_path / "proc" / "driver" / "nvidia" / "version"
        p.parent.mkdir(parents=True)
        p.write_text(nvidia)
    for name in gl64:
        (tmp_path / "usr" / "lib" / "x86_64-linux-gnu" / "GL" / name).mkdir(parents=True)
    for name in gl32:
        (tmp_path / "app" / "lib" / "i386-linux-gnu" / "GL" / name).mkdir(parents=True)
    return tmp_path


def test_a_matching_nvidia_setup_is_fine(tmp_path):
    root = _root(
        tmp_path,
        nvidia=OPEN,
        gl64=["default", "nvidia-615-78-08"],
        gl32=["default", "nvidia-615-78-08"],
    )
    result = flatpakgl.check(root)
    assert result.ok
    assert "615.78.08" in result.summary


def test_a_driver_newer_than_its_flatpak_package_is_reported_with_the_fix(tmp_path):
    """The rolling-release case: the host updated to 615, the Flatpak still has 610."""
    root = _root(
        tmp_path,
        nvidia=OPEN,
        gl64=["default", "nvidia-610-10-01"],
        gl32=["default", "nvidia-610-10-01"],
    )
    result = flatpakgl.check(root)
    assert not result.ok
    assert "615.78.08" in result.summary
    assert result.fix == (
        "flatpak install flathub org.freedesktop.Platform.GL.nvidia-615-78-08 "
        "org.freedesktop.Platform.GL32.nvidia-615-78-08"
    )
    assert "AppImage" in result.summary  # what to do if Flathub has not published it yet


def test_a_missing_32_bit_nvidia_package_alone_is_reported(tmp_path):
    root = _root(tmp_path, nvidia=OPEN, gl64=["default", "nvidia-615-78-08"], gl32=["default"])
    result = flatpakgl.check(root)
    assert not result.ok
    assert "32-bit" in result.summary
    assert result.fix == "flatpak install flathub org.freedesktop.Platform.GL32.nvidia-615-78-08"


def test_mesa_needs_the_32_bit_default_package(tmp_path):
    root = _root(tmp_path, nvidia=None, gl64=["default"], gl32=[])
    result = flatpakgl.check(root)
    assert not result.ok
    assert "32-bit" in result.summary
    assert result.fix == "flatpak install flathub org.freedesktop.Platform.GL32.default//25.08"


def test_a_complete_mesa_setup_is_fine(tmp_path):
    root = _root(tmp_path, nvidia=None, gl64=["default"], gl32=["default"])
    assert flatpakgl.check(root).ok


def test_outside_the_flatpak_nothing_is_checked(tmp_path, monkeypatch):
    monkeypatch.setattr(flatpakgl.sandbox, "in_flatpak", lambda: False)
    assert flatpakgl.problem() is None


def test_inside_the_flatpak_a_problem_is_returned(tmp_path, monkeypatch):
    monkeypatch.setattr(flatpakgl.sandbox, "in_flatpak", lambda: True)
    root = _root(tmp_path, nvidia=OPEN, gl64=["default"], gl32=["default"])
    monkeypatch.setattr(flatpakgl, "ROOT", root)
    found = flatpakgl.problem()
    assert found is not None and not found.ok


def test_an_unreadable_system_never_raises(tmp_path, monkeypatch):
    monkeypatch.setattr(flatpakgl.sandbox, "in_flatpak", lambda: True)
    monkeypatch.setattr(flatpakgl, "ROOT", tmp_path / "does-not-exist")
    flatpakgl.problem()  # no exception


# ---------------------------------------------------------------------------
# doctor and the startup banner
# ---------------------------------------------------------------------------
def _doctor(monkeypatch, capsys, result):
    from nadare.app import run_cli
    from nadare.core import gamesandbox, graphics, runners, sandbox, secrets

    monkeypatch.setattr(sandbox, "in_flatpak", lambda: True)
    monkeypatch.setattr(graphics, "vulkan_info", lambda **kw: graphics.VulkanInfo(False))
    monkeypatch.setattr(graphics, "dxvk_ok", lambda: True)
    monkeypatch.setattr(runners, "all_runners", list)
    monkeypatch.setattr(secrets, "keyring_diagnosis", lambda: (False, "test"))
    monkeypatch.setattr(gamesandbox, "locate", lambda: gamesandbox.Located(None))
    monkeypatch.setattr(flatpakgl, "problem", lambda: result)
    assert run_cli(["doctor"]) == 0
    return capsys.readouterr().out


def test_doctor_names_a_driver_mismatch_and_its_fix(monkeypatch, capsys):
    bad = flatpakgl.GlCheck(
        False, "Your NVIDIA driver is 615.78.08 but ... AppImage", "flatpak install flathub X"
    )
    out = _doctor(monkeypatch, capsys, bad)
    assert "Flatpak graphics:" in out and "615.78.08" in out
    assert "fix: flatpak install flathub X" in out
    assert "verdict: ready" not in out  # a game would not start: never say ready


def test_doctor_says_when_the_flatpak_graphics_match(monkeypatch, capsys):
    out = _doctor(monkeypatch, capsys, None)
    assert "Flatpak graphics: OK" in out


def test_the_window_shows_a_banner_with_the_fix(qtbot, monkeypatch):
    from nadare.gui import main_window

    bad = flatpakgl.GlCheck(False, "Your NVIDIA driver is 615.78.08 ...", "flatpak install x")
    monkeypatch.setattr(flatpakgl, "problem", lambda: bad)
    shown = main_window.graphics_banner_text(flatpakgl.problem())
    assert shown is not None
    text, action = shown
    assert "615.78.08" in text and action == "Copy command"  # no refs: nothing to install


def test_no_banner_when_all_is_well(monkeypatch):
    from nadare.gui import main_window

    assert main_window.graphics_banner_text(None) is None


# ---------------------------------------------------------------------------
# Compat.i386, and Nadare installing what is missing itself
# ---------------------------------------------------------------------------
def test_missing_32_bit_system_libraries_are_reported(tmp_path):
    root = _root(tmp_path, nvidia=None, gl64=["default"], gl32=["default"], compat=False)
    result = flatpakgl.check(root)
    assert not result.ok and "32-bit" in result.summary
    assert result.refs == ("org.freedesktop.Platform.Compat.i386//25.08",)
    assert result.fix == "flatpak install flathub org.freedesktop.Platform.Compat.i386//25.08"


def test_everything_missing_is_listed_once(tmp_path):
    root = _root(tmp_path, nvidia=OPEN, gl64=["default"], gl32=[], compat=False)
    result = flatpakgl.check(root)
    assert result.refs == (
        "org.freedesktop.Platform.Compat.i386//25.08",
        "org.freedesktop.Platform.GL.nvidia-615-78-08",
        "org.freedesktop.Platform.GL32.nvidia-615-78-08",
    )
    assert result.fix == "flatpak install flathub " + " ".join(result.refs)


def test_nvidia_refs_match_the_fix_command(tmp_path):
    root = _root(tmp_path, nvidia=OPEN, gl64=["default"], gl32=["default"])
    result = flatpakgl.check(root)
    assert result.refs == (
        "org.freedesktop.Platform.GL.nvidia-615-78-08",
        "org.freedesktop.Platform.GL32.nvidia-615-78-08",
    )


class _Run:
    def __init__(self, codes=(0, 0), raise_on=None):
        self.calls: list[list[str]] = []
        self.codes = list(codes)
        self.raise_on = raise_on

    def __call__(self, argv, **kwargs):
        self.calls.append(list(argv))
        if self.raise_on is not None and len(self.calls) == self.raise_on:
            raise FileNotFoundError("flatpak-spawn")
        code = self.codes.pop(0) if self.codes else 0

        class R:
            returncode = code
            stdout = ""
            stderr = "error: No remote refs found for 'nvidia-999'" if code else ""

        return R()


def test_install_missing_asks_the_host_flatpak_per_user():
    refs = ("org.freedesktop.Platform.GL32.default//25.08",)
    run = _Run()
    ok, message = flatpakgl.install_missing(refs, run=run)
    assert ok and "Restart" in message
    assert run.calls == [
        [
            "flatpak-spawn",
            "--host",
            "flatpak",
            "remote-add",
            "--user",
            "--if-not-exists",
            "flathub",
            "https://dl.flathub.org/repo/flathub.flatpakrepo",
        ],
        [
            "flatpak-spawn",
            "--host",
            "flatpak",
            "install",
            "--user",
            "-y",
            "--noninteractive",
            "flathub",
            *refs,
        ],
    ]


def test_a_failed_install_says_why_and_never_raises():
    ok, message = flatpakgl.install_missing(("x",), run=_Run(codes=(0, 1)))
    assert not ok and "nvidia-999" in message and "AppImage" in message
    ok, message = flatpakgl.install_missing(("x",), run=_Run(raise_on=1))
    assert not ok


def test_nothing_to_install_runs_nothing():
    run = _Run()
    assert flatpakgl.install_missing((), run=run)[0] is True
    assert run.calls == []


def test_the_banner_offers_to_install_when_it_can(monkeypatch):
    from nadare.gui import main_window

    bad = flatpakgl.GlCheck(
        False, "Your NVIDIA driver is 615.78.08 ...", "flatpak install flathub x", ("x",)
    )
    text, action = main_window.graphics_banner_text(bad)
    assert action == "Install"
