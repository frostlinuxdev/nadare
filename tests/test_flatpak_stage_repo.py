# SPDX-License-Identifier: GPL-3.0-or-later
"""The self-hosted Flatpak repo is staged from the trimmed AppImage payload, per the manifest."""

from __future__ import annotations

import importlib.util
from pathlib import Path

import pytest

yaml = pytest.importorskip("yaml")

REPO = Path(__file__).resolve().parents[1]
SCRIPT = REPO / "packaging" / "flatpak" / "stage_repo.py"
MANIFEST = REPO / "packaging" / "flatpak" / "io.github.frostlinuxdev.Nadare.yml"


@pytest.fixture(scope="module")
def stage():
    spec = importlib.util.spec_from_file_location("stage_repo_under_test", SCRIPT)
    module = importlib.util.module_from_spec(spec)
    spec.loader.exec_module(module)
    return module


def test_finish_arguments_match_the_manifest(stage):
    manifest = yaml.safe_load(MANIFEST.read_text())
    argv = stage.finish_argv(manifest, "root")
    assert argv[:3] == ["flatpak", "build-finish", "--command=nadare"]
    assert argv[-1] == "root"
    for arg in manifest["finish-args"]:
        assert arg in argv
    assert "--talk-name=org.freedesktop.Flatpak" in argv  # Nadare installs missing extensions
    assert any(a.startswith("--extension=org.freedesktop.Platform.GL32=") for a in argv)
    assert any(a.startswith("--extension=org.freedesktop.Platform.Compat.i386=") for a in argv)


def test_the_runtime_provided_libraries_are_removed(stage):
    names = stage.removed_libraries(yaml.safe_load(MANIFEST.read_text()))
    assert "libvulkan.so.1" in names and "libfontconfig.so.1" in names
    assert all("/" not in name for name in names)


def test_staging_keeps_symlinks_and_never_mirrors_flathub(stage, tmp_path):
    appdir = tmp_path / "AppDir" / "usr" / "bin"
    internal = appdir / "_internal"
    (internal / "PyQt6" / "Qt6" / "lib").mkdir(parents=True)
    (internal / "PyQt6" / "Qt6" / "lib" / "libQt6Core.so.6").write_bytes(b"qt")
    (internal / "libQt6Core.so.6").symlink_to("PyQt6/Qt6/lib/libQt6Core.so.6")
    (internal / "libvulkan.so.1").write_bytes(b"old vulkan")
    (appdir / "nadare").write_bytes(b"\x7fELF")
    out = tmp_path / "root"
    stage.stage(tmp_path / "AppDir", out, proton_libs=False)
    staged = out / "files" / "lib" / "nadare" / "_internal"
    assert (staged / "libQt6Core.so.6").is_symlink()  # Qt stored once
    assert not (staged / "libvulkan.so.1").exists()  # the runtime's copy is used
    assert (
        out / "files" / "share" / "metainfo" / "io.github.frostlinuxdev.Nadare.metainfo.xml"
    ).is_file()
    assert not (out / "files" / "lib" / "i386-linux-gnu" / "libc.so.6").exists()


def test_the_desktop_entry_has_each_key_once(stage, tmp_path):
    # desktop-file-validate (run by flatpak build-finish/export) rejects a repeated key.
    appdir = tmp_path / "AppDir" / "usr" / "bin"
    (appdir / "_internal").mkdir(parents=True)
    (appdir / "nadare").write_bytes(b"\x7fELF")
    out = tmp_path / "root"
    stage.stage(tmp_path / "AppDir", out, proton_libs=False)
    entry = (
        out / "files" / "share" / "applications" / "io.github.frostlinuxdev.Nadare.desktop"
    ).read_text()
    keys = [line.split("=", 1)[0] for line in entry.splitlines() if "=" in line]
    assert len(keys) == len(set(keys)), sorted(k for k in keys if keys.count(k) > 1)
    assert "StartupWMClass=nadare" in entry.splitlines()
    assert "Icon=io.github.frostlinuxdev.Nadare" in entry.splitlines()


def test_a_used_staging_directory_is_refused(stage, tmp_path):
    (tmp_path / "out").mkdir()
    with pytest.raises(SystemExit):
        stage.stage(tmp_path / "AppDir", tmp_path / "out")


def test_the_app_gets_x11_on_a_wayland_desktop_too():
    """`fallback-x11` means no X11 when Wayland is there (measured: no DISPLAY, no socket), so
    a game set to Display backend X11 silently ran on Wayland. Adding `x11` next to it is not
    enough: the fallback rule still wins. Games need XWayland as the escape hatch."""
    args = yaml.safe_load(MANIFEST.read_text())["finish-args"]
    assert "--socket=x11" in args
    assert "--socket=fallback-x11" not in args
    assert "--socket=wayland" in args
