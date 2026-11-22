# SPDX-License-Identifier: GPL-3.0-or-later
"""Owned runtime routing and b24/b25 upgrade regression tests (#1443)."""

import fcntl
import os
from pathlib import Path
from types import SimpleNamespace

import pytest

from nadare.core import (
    gamesandbox,
    leftovers,
    paths,
    protoncmd,
    pvlocales,
    umu,
    uninstall_all,
    userns,
)
from nadare.core.config import Config


def runtime(root):
    bwrap = root / "steamrt3/pressure-vessel/libexec/steam-runtime-tools-0/srt-bwrap"
    bwrap.parent.mkdir(parents=True)
    bwrap.write_text("runtime")
    bwrap.chmod(0o755)
    return bwrap


def shared():
    return Path(os.environ["XDG_DATA_HOME"]) / "umu"


@pytest.mark.parametrize("kind", ["managed", "system", "configured", "named-managed"])
def test_launch_routes_only_managed_runtime(kind, monkeypatch, tmp_path):
    executable = (
        str(umu.managed_path()) if kind in ("managed", "named-managed") else "/usr/bin/custom-umu"
    )
    monkeypatch.setattr(umu, "resolve", lambda *a, **kw: executable)
    # This test is about runtime routing; keep the direct zipapp argv on every host.
    monkeypatch.setattr(umu, "shebang_python_ok", lambda: True)
    monkeypatch.setattr(userns, "restricted", lambda: False)
    monkeypatch.setattr(pvlocales, "prepare", lambda *a: {})
    monkeypatch.setattr(protoncmd.sandbox, "in_flatpak", lambda: False)
    env = {}
    argv = protoncmd.proton_argv(
        ["game.exe"],
        proton=tmp_path,
        prefix=tmp_path,
        env=env,
        configured=(
            executable
            if kind == "named-managed"
            else "custom"
            if kind == "configured"
            else "umu-run"
        ),
        emit=lambda e: None,
    )
    assert argv[0] == executable
    assert env.get("UMU_FOLDERS_PATH") == (
        str(paths.data_dir() / "umu-runtime") if kind == "managed" else None
    )


def test_managed_first_download_is_announced(monkeypatch):
    """A managed launch with no runtime (fresh install, or a failed copy) says so first."""
    monkeypatch.setattr(userns, "restricted", lambda: False)
    steps: list[str] = []
    routed = umu.prepare_runtime(
        str(umu.managed_path()), {}, emit=lambda event: steps.append(event.message)
    )
    assert routed is True
    assert umu.FIRST_RUNTIME_NOTE in steps


def test_system_umu_keeps_default_folder(monkeypatch, tmp_path):
    """A system umu is still told about its one-time download; it gets no override."""
    monkeypatch.setattr(userns, "restricted", lambda: False)
    monkeypatch.setattr(umu, "resolve", lambda *a, **kw: "/usr/bin/umu-run")
    monkeypatch.setattr(protoncmd.pvlocales, "prepare", lambda *a: {})
    monkeypatch.setattr(protoncmd.sandbox, "in_flatpak", lambda: False)
    steps = []
    env: dict[str, str] = {}
    argv = protoncmd.proton_argv(
        ["game.exe"],
        proton=tmp_path,
        prefix=tmp_path,
        env=env,
        emit=lambda event: steps.append(event.message),
    )
    assert argv[0] == "/usr/bin/umu-run"
    assert "UMU_FOLDERS_PATH" not in env
    assert umu.FIRST_RUNTIME_NOTE in steps


def test_migration_copies_once_and_keeps_shared(monkeypatch):
    monkeypatch.setattr(userns, "restricted", lambda: False)
    original = runtime(shared())
    umu.prepare_runtime(str(umu.managed_path()), {})
    copied = paths.data_dir() / "umu-runtime/umu" / original.relative_to(shared())
    assert copied.read_text() == "runtime"
    assert original.read_text() == "runtime"
    copied.write_text("ours")
    umu.prepare_runtime(str(umu.managed_path()), {})
    assert copied.read_text() == "ours"


@pytest.mark.parametrize("failure", ["copy", "space", "source-lock"])
def test_migration_falls_back_to_download(failure, monkeypatch):
    monkeypatch.setattr(userns, "restricted", lambda: False)
    original = runtime(shared())
    lock = None
    if failure == "copy":

        def fail_after_copy(argv, **kw):
            runtime(Path(argv[-1]))
            return SimpleNamespace(returncode=1)

        monkeypatch.setattr(umu.subprocess, "run", fail_after_copy)
    elif failure == "space":
        monkeypatch.setattr(umu.shutil, "disk_usage", lambda p: SimpleNamespace(free=0))
    else:
        lock = (shared() / "umu.lock").open("w")
        fcntl.flock(lock, fcntl.LOCK_EX | fcntl.LOCK_NB)
    try:
        env = {}
        umu.prepare_runtime(str(umu.managed_path()), env)
        assert env["UMU_FOLDERS_PATH"] == str(paths.data_dir() / "umu-runtime")
        assert not list((paths.data_dir() / "umu-runtime/umu").glob("*/pressure-vessel"))
        assert original.read_text() == "runtime"
        assert not list((paths.data_dir() / "umu-runtime").glob(".copy-*"))
    finally:
        if lock:
            lock.close()


def test_migration_serializes_launches(monkeypatch):
    import threading
    from concurrent.futures import ThreadPoolExecutor

    monkeypatch.setattr(userns, "restricted", lambda: False)
    runtime(shared())
    real_run = umu.subprocess.run
    entered, release = threading.Event(), threading.Event()
    calls = []

    def copy(*args, **kwargs):
        with (paths.data_dir() / "umu-runtime/.migration.lock").open("r+") as lock:
            with pytest.raises(BlockingIOError):
                fcntl.flock(lock, fcntl.LOCK_EX | fcntl.LOCK_NB)
        calls.append(args)
        entered.set()
        assert release.wait(5)
        return real_run(*args, **kwargs)

    monkeypatch.setattr(umu.subprocess, "run", copy)
    with ThreadPoolExecutor(2) as pool:
        one = pool.submit(umu.prepare_runtime, str(umu.managed_path()), {})
        assert entered.wait(5)
        two = pool.submit(umu.prepare_runtime, str(umu.managed_path()), {})
        release.set()
        one.result(timeout=5)
        two.result(timeout=5)
    assert len(calls) == 1


def test_old_profile_keeps_shared_and_offers_fix(monkeypatch, tmp_path):
    profile = tmp_path / "profile"
    profile.write_text(
        "profile nadare-srt-bwrap @{HOME}/.local/share/umu/*/pressure-vessel/"
        "libexec/steam-runtime-tools-0/srt-bwrap flags=(unconfined) { userns, }"
    )
    monkeypatch.setattr(userns, "PROFILE_PATH", str(profile))
    monkeypatch.setattr(userns, "restricted", lambda: True)
    runtime(shared())
    assert userns.runtime_profile_outdated()
    env = {}
    umu.prepare_runtime(str(umu.managed_path()), env)
    assert "UMU_FOLDERS_PATH" not in env
    assert not (paths.data_dir() / "umu-runtime/umu").exists()
    assert "Nadare moved its runtime" in userns.RUNTIME_MOVED_MESSAGE
    text = userns.profile_text()
    assert "@{HOME}/.local/share/umu/*/pressure-vessel" in text
    assert str(paths.data_dir() / "umu-runtime/umu") in text
    profile.write_text(text)
    assert not userns.runtime_profile_outdated()
    umu.prepare_runtime(str(umu.managed_path()), env)
    assert env["UMU_FOLDERS_PATH"] == str(paths.data_dir() / "umu-runtime")


def test_all_runtime_consumers_include_owned_root():
    root = paths.data_dir() / "umu-runtime/umu"
    bwrap = runtime(root)
    assert str(bwrap) in userns._srt_bwrap_paths()
    assert root in pvlocales._umu_roots()
    assert root in leftovers._umu_folders()
    assert umu.runtime_downloaded(str(umu.managed_path()))


def test_managed_first_note_ignores_unmigrated_shared_runtime(monkeypatch):
    monkeypatch.setattr(userns, "restricted", lambda: False)
    runtime(shared())
    assert not umu.runtime_downloaded(str(umu.managed_path()))
    assert umu.runtime_downloaded("/usr/bin/umu-run")


@pytest.mark.parametrize("with_shared", [False, True])
def test_uninstall_owned_by_default_shared_only_by_flag(with_shared, tmp_path):
    own = paths.data_dir() / "umu-runtime"
    runtime(own / "umu")
    runtime(shared())
    cfg = Config.load(paths.config_file())
    uninstall_all.run(
        cfg, umu=with_shared, applications_dir=tmp_path / "apps", desktop_dir=tmp_path / "desk"
    )
    assert not own.exists()
    assert shared().exists() is not with_shared


def test_keep_runtimes_keeps_the_owned_runtime(tmp_path):
    own = paths.data_dir() / "umu-runtime"
    runtime(own / "umu")
    cfg = Config.load(paths.config_file())
    uninstall_all.run(
        cfg,
        runtimes=False,
        applications_dir=tmp_path / "apps",
        desktop_dir=tmp_path / "desk",
    )
    assert own.is_dir()


def test_sandbox_binds_owned_base_without_exposing_other_data(tmp_path):
    base = paths.data_dir() / "umu-runtime"
    base.mkdir(parents=True)
    prefix = Path.home() / "prefix"
    prefix.mkdir()
    flags, _ = gamesandbox.wrap(
        ["umu-run"], {"UMU_FOLDERS_PATH": str(base)}, prefix=prefix, home=Path.home()
    )
    assert ["--bind", str(base), str(base)] == flags[
        flags.index(str(base)) - 1 : flags.index(str(base)) + 2
    ]
    assert str(paths.data_dir()) not in flags


def test_old_profile_does_not_probe_or_select_blocked_owned_bwrap(monkeypatch):
    from types import SimpleNamespace

    own = runtime(paths.data_dir() / "umu-runtime/umu")
    old = runtime(shared())
    monkeypatch.setattr(userns, "runtime_profile_outdated", lambda: True)
    monkeypatch.setattr(userns, "restricted", lambda: True)
    monkeypatch.setattr(userns, "profile_installed", lambda: True)
    monkeypatch.setattr(gamesandbox, "recorded_runtime_copies", lambda: [str(own), str(old)])
    seen = []

    def run(argv, **kwargs):
        seen.append(argv[0])
        return SimpleNamespace(returncode=0)

    assert userns.probe(run=run) is True
    assert seen == [str(old)]
    assert str(own) not in gamesandbox.runtime_copies()
    monkeypatch.setattr(userns, "probe", lambda: True)
    assert not userns.blocks_runtime()


def test_migration_does_not_retry_failed_copy(monkeypatch):
    monkeypatch.setattr(userns, "restricted", lambda: False)
    runtime(shared())
    calls = []

    def fail(*args, **kw):
        calls.append(args[0])
        return SimpleNamespace(returncode=1)

    monkeypatch.setattr(umu.subprocess, "run", fail)
    umu.prepare_runtime(str(umu.managed_path()), {})
    umu.prepare_runtime(str(umu.managed_path()), {})
    assert len(calls) == 1
    assert "--reflink=auto" in calls[0]


def test_steam_installer_uses_owned_runtime(monkeypatch, tmp_path):
    from nadare.core import steamrun

    monkeypatch.setattr(userns, "restricted", lambda: False)
    _, env = steamrun.umu_run_command(
        umu_run=str(umu.managed_path()),
        proton=tmp_path / "proton",
        compat_data=tmp_path,
        client_path=tmp_path,
        args=["installer.exe"],
        base_env={},
    )
    assert env["UMU_FOLDERS_PATH"] == str(paths.data_dir() / "umu-runtime")


def test_killed_migration_is_swept_only_when_old_and_unlocked():
    import time

    base = paths.data_dir() / "umu-runtime"
    old = base / ".copy-abcdefgh"
    new = base / ".copy-fresh123"
    for folder in (old, new):
        folder.mkdir(parents=True)
        (folder / "partial").write_text("copy")
    before = time.time() - 2 * leftovers.MIN_AGE
    os.utime(old, (before, before))
    with (base / ".migration.lock").open("w") as lock:
        fcntl.flock(lock, fcntl.LOCK_EX | fcntl.LOCK_NB)
        leftovers.sweep()
        assert old.exists()
    leftovers.sweep()
    assert not old.exists()
    assert new.exists()


def test_failed_profile_load_retains_shared_location(monkeypatch, tmp_path):
    import subprocess

    profile = tmp_path / "nadare-srt-bwrap"
    profile.write_text("old profile")
    parser = tmp_path / "reject-profile"
    parser.write_text("#!/bin/sh\nexit 1\n")
    parser.chmod(0o755)
    monkeypatch.setattr(userns, "PROFILE_PATH", str(profile))
    monkeypatch.setattr(userns, "restricted", lambda: True)
    result = subprocess.run(["/bin/sh", "-c", userns.fix_script(str(profile), str(parser))])
    assert result.returncode != 0
    assert profile.read_text() == "old profile"
    assert userns.runtime_profile_outdated()


def test_existing_owned_runtime_is_kept_without_migration_marker(monkeypatch):
    monkeypatch.setattr(userns, "restricted", lambda: False)
    owned = runtime(paths.data_dir() / "umu-runtime/umu")
    owned.write_text("already owned")
    runtime(shared())

    def unexpected(*args, **kwargs):
        pytest.fail("An existing runtime must not be copied over")

    monkeypatch.setattr(umu.subprocess, "run", unexpected)
    umu.prepare_runtime(str(umu.managed_path()), {})
    assert owned.read_text() == "already owned"
    assert not (paths.data_dir() / "umu-runtime/.migration-attempted").exists()


def test_migration_tells_the_user_before_copying(monkeypatch):
    """The one-time copy can take a while on a disk without reflinks: say so first."""
    monkeypatch.setattr(userns, "restricted", lambda: False)
    runtime(shared())
    seen = []
    real_run = umu.subprocess.run

    def run(argv, **kw):
        seen.append(("cp", list(steps)))
        return real_run(argv, **kw)

    steps: list[str] = []
    monkeypatch.setattr(umu.subprocess, "run", run)
    umu.prepare_runtime(str(umu.managed_path()), {}, emit=lambda e: steps.append(e.message))
    assert seen and any("Copying" in s for s in seen[0][1])
    steps.clear()
    umu.prepare_runtime(str(umu.managed_path()), {}, emit=lambda e: steps.append(e.message))
    assert steps == []
