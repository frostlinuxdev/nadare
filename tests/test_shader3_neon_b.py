# SPDX-License-Identifier: GPL-3.0-or-later
"""Adversarial audit tests for #1364 shader cache phase 3-4 (round B).

Only the rows triaged as REAL are ported here; the two BOGUS rows (a leaf symlink
for Mesa that only appears in the environment string, and empty cache variables
that bypass ``prepare()``) are not requirements this branch makes.
"""

from __future__ import annotations

import inspect
import os
import stat
import sys
from pathlib import Path

import pytest

from nadare.backends.czn.backend import CznBackend
from nadare.core import paths, removal, shadercache, storagecard
from nadare.core.backends import BackendContext
from nadare.core.config import Config
from nadare.core.events import Cancel


def _make_runner(tmp_path: Path, name: str, *, dxvk: str, vkd3d: str) -> Path:
    runner = tmp_path / name
    for folder, value in (("dxvk", dxvk), ("vkd3d-proton", vkd3d)):
        target = runner / "files" / "lib" / "wine" / folder
        target.mkdir(parents=True, exist_ok=True)
        (target / "version").write_text(value + "\n", encoding="utf-8")
    (runner / "proton").write_text("#!/bin/sh\n", encoding="utf-8")
    return runner


# Deep enough to need more frames than the limit allows, but cheap: a recursive walker
# needs a frame per level, the stack-based one does not. 1,050 levels under the default
# limit proved the same thing but took 40 s and slowed the whole suite.
_DEEP = 400


@pytest.fixture
def low_recursion_limit():
    old = sys.getrecursionlimit()
    sys.setrecursionlimit(len(inspect.stack(0)) + 150)
    try:
        yield
    finally:
        sys.setrecursionlimit(old)


def _deep_tree(root: Path, depth: int) -> Path:
    """A one-child chain of ``depth`` folders, built through directory descriptors."""
    root.mkdir(parents=True)
    fd = os.open(root, os.O_RDONLY | os.O_DIRECTORY)
    try:
        for _ in range(depth):
            os.mkdir("a", dir_fd=fd)
            nxt = os.open("a", os.O_RDONLY | os.O_DIRECTORY, dir_fd=fd)
            os.close(fd)
            fd = nxt
        leaf = os.open("x", os.O_WRONLY | os.O_CREAT, 0o600, dir_fd=fd)
        try:
            os.write(leaf, b"1")
        finally:
            os.close(leaf)
    finally:
        os.close(fd)
    return root / ("a/" * depth + "x")


def test_prepare_must_not_follow_parent_symlink_in_sandbox_cache(tmp_path: Path) -> None:
    """A hostile Sandboxed game can replace sandbox-cache/shader-cache/games with a symlink
    to a hidden host directory; prepare() must not create directories outside cache_dir."""
    victim = tmp_path / "victim_host_dir"
    victim.mkdir()
    games_link = paths.shader_cache_dir() / "games"
    games_link.parent.mkdir(parents=True, exist_ok=True)
    games_link.symlink_to(victim, target_is_directory=True)

    env: dict[str, str] = {}
    messages: list[str] = []
    shadercache.prepare("other-game", env, messages.append)

    assert not (victim / "other-game").exists(), (
        "prepare() followed symlink in sandbox-cache and created directories inside host target"
    )


def test_clear_shader_must_not_delete_outside_target_when_parent_swapped_for_symlink(
    tmp_path: Path, monkeypatch: pytest.MonkeyPatch
) -> None:
    """While hostile Game A runs (with RW access to shared sandbox-cache), clearing idle
    Game B's VKD3D cache must not follow a parent symlink swapped between _safe_shader()
    and removal.delete_tree() to delete host files outside cache_dir."""
    prefix_a = tmp_path / "prefix_a"
    prefix_b = tmp_path / "prefix_b"
    prefix_a.mkdir()
    prefix_b.mkdir()

    vkd3d_b = shadercache.vkd3d_dir("game_b")
    vkd3d_b.mkdir(parents=True)
    (vkd3d_b / "shader.bin").write_bytes(b"cache")

    victim_parent = tmp_path / "victim_host"
    victim_dir = victim_parent / "vkd3d"
    victim_dir.mkdir(parents=True)
    precious = victim_dir / "precious.txt"
    precious.write_text("do not delete", encoding="utf-8")

    # Game A is running; Game B is idle.
    monkeypatch.setattr(storagecard, "is_running", lambda p: Path(p) == prefix_a)
    report = storagecard.scan(
        {
            "game_a": {"backend": "wine", "prefix": str(prefix_a)},
            "game_b": {"backend": "wine", "prefix": str(prefix_b)},
        },
        steamapps=[],
        running=lambda p: Path(p) == prefix_a,
    )
    vkd3d_row = next(row for row in report.shaders if row.key == "vkd3d:game_b")

    # Simulate concurrent hostile Game A swapping games/game_b -> victim_parent during the
    # window between _safe_shader() and the deletion (while the size is measured). The
    # first measurement after the scan is always this clear's: the scan sizes caches
    # through shadercache.cache_size(), never through tree_bytes().
    real_tree_bytes = storagecard.diskspace.tree_bytes
    swapped = False

    def swap_during_measure(path, *args, **kwargs) -> int:
        nonlocal swapped
        size = real_tree_bytes(path, *args, **kwargs)
        if not swapped:
            swapped = True
            parent = vkd3d_b.parent  # sandbox-cache/shader-cache/games/game_b
            aside = parent.with_name("game_b_aside")
            parent.rename(aside)
            parent.symlink_to(victim_parent, target_is_directory=True)
        return size

    monkeypatch.setattr(storagecard.diskspace, "tree_bytes", swap_during_measure)

    storagecard.clear_shader(vkd3d_row)

    assert swapped, "the clear never measured the cache, so the race was not simulated"
    assert precious.is_file(), (
        "clear_shader() followed a parent symlink swapped during clear and deleted host data"
    )


def test_cache_layers_must_not_follow_parent_symlink_outside_cache_or_prefix(
    tmp_path: Path,
) -> None:
    """Hostile game replacing games/<profile> or AppData/Local with a symlink to a host dir
    must not make cache_layers() walk and size files outside the cache/prefix."""
    secret_parent = tmp_path / "secret_host"
    (secret_parent / "vkd3d").mkdir(parents=True)
    (secret_parent / "vkd3d" / "private.dat").write_bytes(b"S" * 4321)
    (secret_parent / "dxvk").mkdir(parents=True)
    (secret_parent / "dxvk" / "private.dat").write_bytes(b"D" * 1234)

    vkd3d_parent = shadercache.vkd3d_dir("g").parent
    vkd3d_parent.parent.mkdir(parents=True, exist_ok=True)
    vkd3d_parent.symlink_to(secret_parent, target_is_directory=True)

    prefix = tmp_path / "prefix"
    appdata = prefix / "drive_c" / "users" / "steamuser" / "AppData"
    appdata.mkdir(parents=True)
    (appdata / "Local").symlink_to(secret_parent, target_is_directory=True)

    layers = shadercache.cache_layers("g", prefix, {"PROTONPATH": "/fake/proton"})

    assert layers["VKD3D"][1] is None, (
        f"cache_layers followed parent symlink for VKD3D and leaked size {layers['VKD3D'][1]}"
    )
    assert layers["DXVK"][1] is None, (
        f"cache_layers followed parent symlink for DXVK and leaked size {layers['DXVK'][1]}"
    )


def test_clear_shader_must_not_crash_on_deep_directory_tree(
    tmp_path: Path, monkeypatch: pytest.MonkeyPatch, low_recursion_limit: None
) -> None:
    """A hostile Sandboxed game can plant a 1,050-level nested directory inside its writable
    vkd3d cache (under PATH_MAX); clear_shader() must not crash with RecursionError."""
    prefix = tmp_path / "prefix"
    prefix.mkdir()
    vkd3d = shadercache.vkd3d_dir("deep")
    _deep_tree(vkd3d, _DEEP)

    monkeypatch.setattr(storagecard, "is_running", lambda _p: False)
    report = storagecard.scan(
        {"deep": {"backend": "wine", "prefix": str(prefix)}},
        steamapps=[],
        running=lambda _p: False,
    )
    vkd3d_row = next(row for row in report.shaders if row.key == "vkd3d:deep")

    result = storagecard.clear_shader(vkd3d_row)
    assert not vkd3d.exists() and result.removed == [vkd3d]


def test_mount_scan_must_not_recurse_on_a_deep_directory_tree(
    tmp_path: Path, low_recursion_limit: None
) -> None:
    """The removal helpers that walk a tree for mounts use a stack, not Python frames."""
    root = tmp_path / "deep"
    _deep_tree(root, _DEEP)
    mounts = removal._Mounts(root.stat().st_dev, removal._mountinfo_points() or frozenset())

    assert removal._has_mount_below(root, mounts) is False


def test_kept_tree_delete_must_not_recurse_on_a_deep_directory_tree(
    tmp_path: Path, low_recursion_limit: None
) -> None:
    """Deleting around a kept tree (the mount/keep path) also uses a stack."""
    root = tmp_path / "deep"
    _deep_tree(root, _DEEP)
    keep = root / "keep"
    keep.mkdir()
    (keep / "mine.txt").write_text("keep", encoding="utf-8")

    outcome = removal.delete_tree(root, keep=[keep])

    assert outcome.leftovers == []
    assert (keep / "mine.txt").read_text(encoding="utf-8") == "keep"
    assert not (root / "a").exists()


def test_note_runner_change_must_not_inherit_unreadable_mode_from_symlink_or_record(
    tmp_path: Path,
) -> None:
    """When shader-versions.json is replaced by a symlink to a mode-0000 file (or chmod 0000),
    note_runner_change() must write the fresh baseline with readable private mode (0600) so
    subsequent runner changes are detected."""
    r1 = _make_runner(tmp_path, "r1", dxvk="dxvk 1", vkd3d="vkd3d 1")
    r2 = _make_runner(tmp_path, "r2", dxvk="dxvk 2", vkd3d="vkd3d 1")
    data = tmp_path / "profile"
    data.mkdir()

    unreadable = tmp_path / "unreadable_target"
    unreadable.write_text('{"DXVK": "dxvk 0"}\n', encoding="utf-8")
    os.chmod(unreadable, 0o000)
    record = data / shadercache.VERSIONS_FILE
    record.symlink_to(unreadable)

    messages: list[str] = []
    try:
        # First play: ignores symlink and replaces it with baseline for r1.
        shadercache.note_runner_change("g", r1, messages.append, data_dir=data)
        assert not record.is_symlink()
        mode = stat.S_IMODE(record.lstat().st_mode)
        assert mode == 0o600, f"shader-versions.json inherited mode {oct(mode)} instead of 0o600"

        # Second play on r2 must read the r1 baseline and emit the DXVK upgrade note.
        shadercache.note_runner_change("g", r2, messages.append, data_dir=data)
        assert any("New DXVK version" in msg for msg in messages), (
            "subsequent runner change was missed because replacement record was unreadable"
        )
    finally:
        os.chmod(unreadable, 0o600)
        if record.exists() and not record.is_symlink():
            os.chmod(record, 0o600)


def test_total_warning_must_not_double_count_shared_prefix_dxvk_cache(
    tmp_path: Path, monkeypatch: pytest.MonkeyPatch
) -> None:
    """Two profiles sharing one Wine prefix with a 5 GiB DXVK cache have 5 GiB on disk
    (under the 8 GiB limit), not 10 GiB; scan() must not emit a false total warning."""
    gib = 1024**3
    prefix = tmp_path / "shared_prefix"
    dxvk = prefix / "drive_c" / "users" / "steamuser" / "AppData" / "Local" / "dxvk"
    dxvk.mkdir(parents=True)

    monkeypatch.setattr(
        storagecard, "_cache_bytes", lambda folder: 5 * gib if folder == dxvk else None
    )
    monkeypatch.setattr(storagecard.diskspace, "free_bytes", lambda _p: 1 << 50)
    monkeypatch.setattr(storagecard, "steamapps_dirs", lambda: [])

    report = storagecard.scan(
        {
            "game1": {"backend": "wine", "prefix": str(prefix)},
            "game2": {"backend": "wine", "prefix": str(prefix)},
        },
        steamapps=[],
        running=lambda _p: False,
    )

    assert report.shader_warning == "", (
        "shared DXVK cache (5 GiB) was double-counted and triggered warning: "
        f"{report.shader_warning!r}"
    )


def test_czn_sandboxed_launch_logs_sandbox_nvidia_cache_not_host(
    tmp_path: Path, monkeypatch: pytest.MonkeyPatch
) -> None:
    """CznBackend._prefix_command(..., sandboxed=True) must pass sandboxed=True to
    shadercache.log_caches() so it reports the Sandbox NVIDIA cache, not the host's."""
    cfg_path = tmp_path / "config.json"
    profile = {
        "backend": "stove-czn",
        "prefix": str(tmp_path / "prefix"),
        "install_dir": str(tmp_path / "install"),
        "secure_mode": True,
    }
    cfg = Config(path=cfg_path, data=Config._normalise({"profiles": {"czn": profile}}))
    ctx = BackendContext(
        name="czn", profile=cfg.profile("czn"), config=cfg, emit=lambda _e: None, cancel=Cancel()
    )
    backend = CznBackend(ctx)

    runner = _make_runner(tmp_path, "proton", dxvk="dxvk 1", vkd3d="vkd3d 1")
    monkeypatch.setattr(backend, "resolve_runner", lambda: (str(runner), "proton"))
    monkeypatch.setattr("nadare.core.protoncmd.proton_argv", lambda args, **kw: args)
    monkeypatch.setattr("nadare.core.protoncmd.note_prefix_upgrade", lambda *a, **kw: None)
    monkeypatch.setattr("nadare.core.protoncmd.play_speedups", lambda *a, **kw: None)

    messages: list[str] = []
    monkeypatch.setattr(backend, "info", messages.append)

    backend._prefix_command(["game.exe"], sandboxed=True)

    line = next(m for m in messages if m.startswith("shader caches:"))
    expected_nvidia = paths.cache_dir() / "sandbox-cache" / "nvidia"
    assert f"NVIDIA={expected_nvidia}" in line, (
        f"CznBackend sandboxed launch logged host NVIDIA cache instead of {expected_nvidia}: {line}"
    )
