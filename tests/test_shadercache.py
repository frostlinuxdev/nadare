# SPDX-License-Identifier: GPL-3.0-or-later
"""Managed shader cache defaults, diagnostics, and removal."""

from pathlib import Path

import pytest
from _backend_helpers import make_context

from nadare.backends.czn.backend import CznBackend
from nadare.backends.launcher import NikkeBackend
from nadare.backends.wine import WineBackend
from nadare.core import paths, removal


@pytest.fixture
def cachemod():
    from nadare.core import shadercache

    return shadercache


@pytest.fixture(params=[WineBackend, NikkeBackend, CznBackend])
def backend(request, tmp_path, monkeypatch):
    ctx = make_context(
        tmp_path,
        "wine",
        {
            "prefix": str(tmp_path / "prefix"),
            "wine_binary": "/bin/true",
        },
    )
    obj = request.param(ctx)
    monkeypatch.setattr(
        obj, "resolve_runner", lambda: (tmp_path / "proton", "proton"), raising=False
    )
    monkeypatch.setattr("nadare.core.protoncmd.proton_argv", lambda args, **kw: args)
    monkeypatch.setattr("nadare.core.protoncmd.note_prefix_upgrade", lambda *a, **kw: None)
    monkeypatch.setattr("nadare.core.protoncmd.play_speedups", lambda *a, **kw: None)
    return obj


def test_root():
    assert paths.shader_cache_dir() == paths.cache_dir() / "sandbox-cache/shader-cache"
    assert paths.shader_cache_dir().is_absolute()


def test_launch_defaults_survive_toggle_and_relaunch(backend):
    kwargs = {} if isinstance(backend, CznBackend) else {"sandboxed": False}
    _, env = backend._prefix_command(["game.exe"], **kwargs)
    root = paths.cache_dir() / "sandbox-cache/shader-cache"
    mesa, vkd3d = root / "mesa", root / "games/test/vkd3d"
    assert env["MESA_SHADER_CACHE_DIR"] == str(mesa)
    assert env["VKD3D_SHADER_CACHE_PATH"] == str(vkd3d)
    assert mesa.is_dir() and vkd3d.is_dir()
    marker = vkd3d / "vkd3d-proton.cache.game.exe"
    marker.write_bytes(b"warm")
    if kwargs:
        kwargs["sandboxed"] = True
    _, again = backend._prefix_command(["game.exe"], **kwargs)
    assert again["MESA_SHADER_CACHE_DIR"] == str(mesa)
    assert again["VKD3D_SHADER_CACHE_PATH"] == str(vkd3d)
    assert marker.read_bytes() == b"warm"
    assert "MESA_SHADER_CACHE_MAX_SIZE" not in env


def test_host_replaced_profile_override_wins_and_is_logged(backend, monkeypatch):
    for key in ("MESA_SHADER_CACHE_DIR", "VKD3D_SHADER_CACHE_PATH"):
        monkeypatch.setenv(key, "/host/cache")
    messages = []
    monkeypatch.setattr(backend, "info", messages.append)
    _, defaults = backend._prefix_command(["game.exe"])
    assert defaults["MESA_SHADER_CACHE_DIR"] != "/host/cache"
    assert defaults["VKD3D_SHADER_CACHE_PATH"] != "/host/cache"
    assert any("MESA_SHADER_CACHE_DIR" in m and "host" in m for m in messages)
    assert any("VKD3D_SHADER_CACHE_PATH" in m and "host" in m for m in messages)
    backend.ctx.profile["extra_env"] = {
        "MESA_SHADER_CACHE_DIR": "/custom/mesa",
        "VKD3D_SHADER_CACHE_PATH": "/custom/vkd3d",
    }
    messages.clear()
    _, env = backend._prefix_command(["game.exe"])
    assert env["MESA_SHADER_CACHE_DIR"] == "/custom/mesa"
    assert env["VKD3D_SHADER_CACHE_PATH"] == "/custom/vkd3d"
    lines = [m for m in messages if m.startswith("shader caches:")]
    assert len(lines) == 1
    assert "/custom/mesa" in lines[0] and "/custom/vkd3d" in lines[0]
    assert "bytes" in lines[0]


def test_unwritable_cache_does_not_abort_launch(backend, monkeypatch):
    original = Path.mkdir

    def denied(self, *args, **kwargs):
        if "shader-cache" in self.parts:
            raise PermissionError("read-only cache")
        return original(self, *args, **kwargs)

    monkeypatch.setattr(Path, "mkdir", denied)
    messages = []
    monkeypatch.setattr(backend, "info", messages.append)
    argv, env = backend._prefix_command(["game.exe"])
    assert argv and env["VKD3D_SHADER_CACHE_PATH"]
    assert any("read-only cache" in line for line in messages)


def test_inventory_reports_layers_without_following_links(cachemod, tmp_path):
    root = paths.shader_cache_dir()
    vkd3d = root / "games/test/vkd3d"
    mesa = root / "mesa"
    dxvk = tmp_path / "prefix/drive_c/users/steamuser/AppData/Local/dxvk"
    nvidia = paths.cache_dir() / "sandbox-cache/nvidia"
    for directory in (vkd3d, mesa, dxvk, nvidia):
        directory.mkdir(parents=True)
        (directory / "cache").write_bytes(b"12345")
    outside = tmp_path / "outside"
    outside.mkdir()
    (outside / "large").write_bytes(b"x" * 100)
    (mesa / "link").symlink_to(outside, target_is_directory=True)
    layers = cachemod.cache_layers("test", tmp_path / "prefix", {}, sandboxed=True)
    assert layers == {
        "Mesa": (mesa, 5),
        "VKD3D": (vkd3d, 5),
        "DXVK": (dxvk, 5),
        "NVIDIA": (nvidia, 5),
    }
    other = cachemod.cache_layers("other", tmp_path / "prefix", {})["VKD3D"]
    assert other == (root / "games/other/vkd3d", 0)


def test_size_walk_has_time_and_entry_limits(cachemod, tmp_path, monkeypatch):
    (tmp_path / "cache").write_bytes(b"123")
    assert cachemod.cache_size(tmp_path, max_entries=0) is None
    ticks = iter([0.0, 1.0])
    monkeypatch.setattr(cachemod.time, "monotonic", lambda: next(ticks))
    assert cachemod.cache_size(tmp_path, max_seconds=0.01) is None


@pytest.mark.parametrize("limit", ["entries", "time"])
def test_size_walk_limits_apply_inside_a_single_directory(cachemod, tmp_path, monkeypatch, limit):
    folder = tmp_path / "flat-cache"
    folder.mkdir()
    for name in ("first", "second"):
        (folder / name).write_bytes(b"123")
    if limit == "entries":
        assert cachemod.cache_size(folder, max_entries=1) is None
        return
    ticks = iter([0.0, 0.0, 1.0])  # start, directory check, first entry check
    monkeypatch.setattr(cachemod.time, "monotonic", lambda: next(ticks))
    assert cachemod.cache_size(folder, max_seconds=0.01) is None


def test_existing_unwritable_cache_warns_without_aborting(backend, monkeypatch):
    from nadare.core import shadercache

    messages = []
    monkeypatch.setattr(backend, "info", messages.append)
    original = shadercache.os.access

    def denied(folder, mode, **kwargs):
        if "shader-cache" in Path(folder).parts:
            return False
        return original(folder, mode, **kwargs)

    monkeypatch.setattr(shadercache.os, "access", denied)
    argv, env = backend._prefix_command(["game.exe"])
    assert argv
    for key in ("MESA_SHADER_CACHE_DIR", "VKD3D_SHADER_CACHE_PATH"):
        folder = Path(env[key])
        assert folder.is_dir()
        assert f"shader cache: cannot write to {folder}" in messages


@pytest.mark.parametrize("explicit_root", [False, True])
def test_host_nvidia_inventory_uses_the_child_cache_root(cachemod, tmp_path, explicit_root):
    root = tmp_path / "child cache" if explicit_root else paths.cache_dir().parent
    nvidia = root / "nvidia"
    nvidia.mkdir(parents=True)
    (nvidia / "cache").write_bytes(b"driver cache")
    env = {"XDG_CACHE_HOME": str(root)} if explicit_root else {}
    assert cachemod.cache_layers("test", tmp_path / "prefix", env)["NVIDIA"] == (
        nvidia, len(b"driver cache")
    )


def test_owned_paths_allow_spaces(cachemod, tmp_path, monkeypatch):
    monkeypatch.setenv("XDG_CACHE_HOME", str(tmp_path / "cache root"))
    env = {}
    cachemod.prepare("My Game", env, lambda line: None)
    assert Path(env["MESA_SHADER_CACHE_DIR"]) == paths.shader_cache_dir() / "mesa"
    assert Path(env["VKD3D_SHADER_CACHE_PATH"]) == (
        paths.shader_cache_dir() / "games/My Game/vkd3d"
    )
    assert all(Path(value).is_dir() for value in env.values())


def test_remove_deletes_only_owned_vkd3d_even_when_data_kept(tmp_path):
    ctx = make_context(tmp_path, "wine", name="gone")
    root = paths.cache_dir() / "sandbox-cache/shader-cache"
    own = root / "games/gone/vkd3d"
    kept = [
        root / "mesa",
        root / "games/keep/vkd3d",
        paths.cache_dir().parent / "nvidia",
        paths.profiles_dir() / "gone",
    ]
    for directory in [own, *kept]:
        directory.mkdir(parents=True)
        (directory / "keep").write_bytes(b"123")
    result = removal.remove_game(
        ctx.config,
        "gone",
        delete_data=False,
        save=False,
        applications_dir=tmp_path / "apps",
    )
    assert not result.profile_kept and not own.exists()
    assert all((directory / "keep").exists() for directory in kept)


@pytest.mark.parametrize("component", ["games", "gone", "vkd3d"])
def test_remove_never_follows_cache_parent_links(tmp_path, component):
    ctx = make_context(tmp_path, "wine", name="gone")
    outside = tmp_path / "outside"
    outside.mkdir()
    (outside / "precious").write_bytes(b"keep")
    root = paths.cache_dir() / "sandbox-cache/shader-cache"
    target = {
        "games": root / "games",
        "gone": root / "games/gone",
        "vkd3d": root / "games/gone/vkd3d",
    }[component]
    target.parent.mkdir(parents=True)
    target.symlink_to(outside, target_is_directory=True)
    result = removal.remove_game(ctx.config, "gone", save=False, applications_dir=tmp_path / "apps")
    assert (outside / "precious").read_bytes() == b"keep"
    assert any("shader cache" in warning for warning in result.warnings)


def test_native_script_uses_owned_caches_before_profile_env(tmp_path, monkeypatch):
    from nadare.backends import script

    launch = tmp_path / "game.sh"
    launch.write_text("exit 0\n")
    ctx = make_context(tmp_path, "script", {"script": str(launch)})
    backend = script.ScriptBackend(ctx)
    monkeypatch.setattr(backend, "_script", lambda: launch)
    captured, messages = [], []
    monkeypatch.setattr(script, "stream", lambda argv, env, **kw: captured.append(env) or 0)
    monkeypatch.setattr(backend, "info", messages.append)
    assert backend.launch() == 0
    assert captured[-1]["MESA_SHADER_CACHE_DIR"] == str(paths.shader_cache_dir() / "mesa")
    assert captured[-1]["VKD3D_SHADER_CACHE_PATH"] == str(
        paths.shader_cache_dir() / "games/test/vkd3d"
    )
    assert any(line.startswith("shader caches:") for line in messages)
    ctx.profile["extra_env"] = {"MESA_SHADER_CACHE_DIR": "/custom/mesa"}
    assert backend.launch() == 0
    assert captured[-1]["MESA_SHADER_CACHE_DIR"] == "/custom/mesa"
