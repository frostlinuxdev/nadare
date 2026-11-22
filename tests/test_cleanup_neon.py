# SPDX-License-Identifier: GPL-3.0-or-later
"""Cleanup refuses ambiguous ownership and rechecks live prefixes."""

import os
import time
from pathlib import Path

import pytest

from nadare.core import installercache, paths, runlock, shadercache, storagecard, umu


@pytest.mark.parametrize("start_after_scan", [False, True])
@pytest.mark.parametrize("explicit_prefix", [False, True])
def test_linked_prefix_keeps_busy_shader_caches(tmp_path, start_after_scan, explicit_prefix):
    prefix = tmp_path / "prefix"
    prefix.mkdir()
    link = tmp_path / "linked" if explicit_prefix else paths.profiles_dir() / "game/prefix"
    link.parent.mkdir(parents=True, exist_ok=True)
    link.symlink_to(prefix)
    dxvk = prefix / "drive_c/users/steamuser/AppData/Local/dxvk"
    dxvk.mkdir(parents=True)
    (dxvk / "shader.bin").write_bytes(b"dxvk")
    files = []
    for folder in (shadercache.mesa_dir(), shadercache.vkd3d_dir("game")):
        folder.mkdir(parents=True)
        file = folder / "shader.bin"
        file.write_bytes(b"shader")
        files.append(file)
    profiles = {"game": {"prefix": str(link)} if explicit_prefix else {}}
    idle = storagecard.scan(profiles, steamapps=[])
    with runlock.hold(prefix):
        report = idle if start_after_scan else storagecard.scan(profiles, steamapps=[])
        results = [storagecard.clear_shader(row) for row in report.shaders]
        assert all(file.read_bytes() == b"shader" for file in files)
        assert all(not result.removed for result in results)
        assert all(row.running == (not start_after_scan) for row in report.shaders)
    assert {row.key for row in report.shaders} == {"mesa", "vkd3d:game"}
    # The linked prefix is retained for busy checks, but never scanned for DXVK.
    assert all(storagecard.clear_shader(row).removed for row in idle.shaders)


@pytest.mark.parametrize("protection", ["live-link", "busy-root", "busy-pfx"])
def test_steam_backup_rechecks_live_or_busy_prefix(tmp_path, protection):
    steamapps = tmp_path / "steamapps"
    backup = steamapps / "compatdata/12345.bak-pre-webview2"
    save = backup / "pfx/drive_c/save.dat"
    save.parent.mkdir(parents=True)
    save.write_bytes(b"save")
    (row,) = storagecard.scan({}, steamapps=[steamapps]).backups
    if protection == "live-link":
        (backup.parent / "12345").symlink_to(backup)
        result = storagecard.clear_backup(row)
    else:
        with runlock.hold(backup if protection == "busy-root" else backup / "pfx"):
            result = storagecard.clear_backup(row)
    assert save.read_bytes() == b"save"
    assert not result.removed
    assert backup in result.kept


def test_installer_sweep_refuses_linked_cache_root(tmp_path):
    outside = tmp_path / "downloads"
    installer = outside / "wine/tool.exe"
    installer.parent.mkdir(parents=True)
    installer.write_bytes(b"MZ installer")
    old = time.time() - 5 * 86400
    os.utime(installer, (old, old))
    cache = paths.cache_dir()
    cache.parent.mkdir(parents=True, exist_ok=True)
    cache.symlink_to(outside)
    assert installercache.installers() == []
    result = installercache.sweep({})
    assert installer.read_bytes() == b"MZ installer"
    assert not result.removed


@pytest.mark.parametrize("linked_component", ["child", "root"])
def test_runtime_removal_refuses_linked_lock_parent(tmp_path, linked_component):
    outside = tmp_path / "outside"
    (outside / "umu").mkdir(parents=True)
    root = umu.runtime_base()
    if linked_component == "child":
        root.mkdir(parents=True)
        (root / "umu").symlink_to(outside)
    else:
        root.parent.mkdir(parents=True, exist_ok=True)
        root.symlink_to(outside)
    assert not umu.remove_runtime(root)
    assert not list(outside.rglob("*.lock"))
    assert (outside / "umu").is_dir()


def test_runtime_removal_works_below_a_linked_data_folder(tmp_path):
    """/home -> /var/home (Fedora Atomic, Bazzite) or a data folder moved to another disk."""
    root = umu.runtime_base()
    disk = tmp_path / "disk"
    (disk / root.name / "umu").mkdir(parents=True)
    root.parent.parent.mkdir(parents=True, exist_ok=True)
    root.parent.symlink_to(disk)
    assert umu.remove_runtime(root)
    assert not (disk / root.name).exists()


@pytest.mark.parametrize(
    "marker", ["Preferences", "Cookies", "Local State", "Network Persistent State", "LOCK"]
)
@pytest.mark.parametrize("after_scan", [False, True])
def test_web_cache_requires_browser_marker_file(tmp_path, marker, after_scan):
    prefix = tmp_path / "prefix"
    app = prefix / "drive_c/users/steamuser/AppData/Local/MyGame"
    data = app / "Cache/downloaded-assets.pak"
    data.parent.mkdir(parents=True)
    data.write_bytes(b"user data")
    profiles = {"game": {"prefix": str(prefix)}}
    if after_scan:
        (app / "Preferences").write_text("{}")
        report = storagecard.scan(profiles, steamapps=[], running=lambda _: False)
        assert len(report.webcaches) == 1
        (app / "Preferences").unlink()
    if marker == "LOCK":
        (app / marker).touch()
    else:
        (app / marker).mkdir()
    if not after_scan:
        report = storagecard.scan(profiles, steamapps=[], running=lambda _: False)
    for row in report.webcaches:
        assert not storagecard.clear_webcache(row).removed
    assert data.read_bytes() == b"user data"


@pytest.mark.parametrize(
    "key,layer", [("MESA_SHADER_CACHE_DIR", "Mesa"), ("VKD3D_SHADER_CACHE_PATH", "VKD3D")]
)
def test_empty_shader_override_is_unknown_without_scanning_cwd(tmp_path, monkeypatch, key, layer):
    cwd = tmp_path / "cwd"
    cwd.mkdir()
    (cwd / "data").write_bytes(b"x" * 4096)
    monkeypatch.chdir(cwd)
    scanned = []
    original = shadercache.cache_size

    def measure(folder):
        scanned.append(folder)
        return original(folder)

    monkeypatch.setattr(shadercache, "cache_size", measure)
    result = shadercache.cache_layers("game", tmp_path / "prefix", {key: ""})
    assert Path(".") not in scanned
    assert result[layer] == (None, None)
    lines = []
    shadercache.log_caches("game", tmp_path / "prefix", {key: ""}, lines.append)
    assert f"{layer}=unknown (unknown bytes)" in lines[0]
