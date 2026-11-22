# SPDX-License-Identifier: GPL-3.0-or-later
# Copyright (C) 2026 Nadare contributors
"""#1417 phase 2a: the start-up sweep of crash leftovers.

The names below are the ones a SIGKILL left on the KDE test desktop (2026-11-16, b24/next code):
they are what an upgraded install already has on disk.
"""

from __future__ import annotations

import fcntl
import os
import subprocess
import time
from pathlib import Path

import pytest

from nadare.core import leftovers, paths

DAY = 86400
DEAD_PID = 2_000_000_000  # above pid_max: never a live process


def _old(path: Path, age: float = 2 * DAY) -> Path:
    t = time.time() - age
    os.utime(path, (t, t), follow_symlinks=False)
    return path


def _file(path: Path, size: int = 10, age: float = 2 * DAY) -> Path:
    path.parent.mkdir(parents=True, exist_ok=True)
    path.write_bytes(b"x" * size)
    return _old(path, age)


def _dir(path: Path, age: float = 2 * DAY, files: int = 2) -> Path:
    path.mkdir(parents=True, exist_ok=True)
    for i in range(files):
        _file(path / f"f{i}", 100)
    return _old(path, age)


@pytest.fixture
def cache() -> Path:
    root = paths.cache_dir()
    root.mkdir(parents=True, exist_ok=True)
    return root


def _wv2_part(cache: Path, pid: int) -> Path:
    return cache / "czn" / f"MicrosoftEdgeWebView2RuntimeInstallerX64.exe.{pid}-{'a' * 32}.part"


def _wv2_run(cache: Path, pid: int) -> Path:
    return cache / "czn" / f".webview2-run-{pid}-{'b' * 32}.exe"


# ---- what the kill tests left: all removed -------------------------------------------------


def test_the_kill_test_leftovers_are_removed(cache: Path) -> None:
    part = _file(_wv2_part(cache, DEAD_PID), 80)
    run = _file(_wv2_run(cache, DEAD_PID), 215)
    run.chmod(0o400)  # the run copy is read-only, as webview2.ensure leaves it
    vc_part = _file(cache / "arknights-yostar" / f".vc_redist.x64.exe.{DEAD_PID}.{'c' * 32}.part")
    vc_run = _dir(cache / "czn" / ".run-fxmh0x_3")
    unpack = _dir(paths.runners_dir() / ".unpack-moa11md7")
    (unpack / ".nadare-owner-pid").write_text(str(DEAD_PID))
    _old(unpack / ".nadare-owner-pid")  # a crash leftover's marker is as old as it (#1494)
    _old(unpack)
    tarball = _file(cache / "runners" / "dwproton-11.0-17-x86_64.tar.xz", 342)
    tar_part = _file(cache / "runners" / "GE-Proton9-27.tar.gz.part", 120)

    result = leftovers.sweep()

    for gone in (part, run, vc_part, vc_run, unpack, tarball, tar_part):
        assert not os.path.lexists(gone), gone
    assert result.freed >= 80 + 215 + 10 + 200 + 200 + 342 + 120
    assert len(result.removed) == 7


def test_what_is_kept(cache: Path) -> None:
    """Installers in use, lock files, the runners themselves and unknown files stay."""
    keep = [
        _file(cache / "czn" / "MicrosoftEdgeWebView2RuntimeInstallerX64.exe"),
        _file(cache / "czn" / "vc_redist.x64.exe"),
        _file(cache / "runners" / ".dwproton-11.0-17-x86_64.tar.xz.lock", 0),
        _file(cache / "czn" / "notes.part"),  # no pid in the name: not ours to judge
        _file(cache / "czn" / "game.7z.part"),
        _dir(paths.runners_dir() / "dwproton-11.0-17-x86_64"),
        _file(paths.state_dir() / "locks" / "czn-kill-3ed971029086.ops.lock", 0),
    ]
    assert leftovers.sweep().removed == []
    for path in keep:
        assert path.exists(), path


# ---- the guards ------------------------------------------------------------------------------


def test_young_leftovers_are_kept(cache: Path) -> None:
    young = [
        _file(_wv2_part(cache, DEAD_PID), age=3600),
        _file(_wv2_run(cache, DEAD_PID), age=3600),
        _dir(cache / "czn" / ".run-young", age=3600),
        _file(cache / "runners" / "x.tar.xz", age=3600),
    ]
    assert leftovers.sweep().removed == []
    assert all(p.exists() for p in young)


def test_the_age_limit_is_one_day(cache: Path) -> None:
    just_under = _file(_wv2_part(cache, DEAD_PID), age=DAY - 600)
    just_over = _file(_wv2_run(cache, DEAD_PID), age=DAY + 600)
    leftovers.sweep()
    assert just_under.exists()
    assert not just_over.exists()


def test_a_live_owner_keeps_its_files(cache: Path) -> None:
    me = os.getpid()
    mine = [_file(_wv2_part(cache, me)), _file(_wv2_run(cache, me))]
    unpack = _dir(paths.runners_dir() / ".unpack-live")
    (unpack / ".nadare-owner-pid").write_text(str(me))
    _old(unpack)
    assert leftovers.sweep().removed == []
    assert all(p.exists() for p in mine) and unpack.exists()


def test_an_unpack_dir_without_an_owner_marker_goes_when_old() -> None:
    unpack = _dir(paths.runners_dir() / ".unpack-nomarker")
    leftovers.sweep()
    assert not unpack.exists()


def test_a_locked_runner_download_is_kept(cache: Path) -> None:
    part = _file(cache / "runners" / "dwproton-11.0-18-x86_64.tar.xz.part")
    lock = cache / "runners" / ".dwproton-11.0-18-x86_64.tar.xz.lock"
    with open(lock, "w") as held:
        fcntl.flock(held, fcntl.LOCK_EX)
        # Held by another process: the sweep's own attempt must fail, as with a live install.
        assert leftovers.sweep().removed == []
        assert part.exists()
    leftovers.sweep()
    assert not part.exists()


def test_a_lock_held_by_another_process_keeps_the_download(cache: Path, tmp_path: Path) -> None:
    tarball = _file(cache / "runners" / "proton.tar.xz")
    lock = cache / "runners" / ".proton.tar.xz.lock"
    ready = tmp_path / "ready"
    holder = subprocess.Popen(
        [
            "python3",
            "-c",
            "import fcntl,sys,time,pathlib;f=open(sys.argv[1],'w');fcntl.flock(f,fcntl.LOCK_EX);"
            "pathlib.Path(sys.argv[2]).touch();time.sleep(30)",
            str(lock),
            str(ready),
        ]
    )
    try:
        deadline = time.time() + 10
        while not ready.exists() and time.time() < deadline:
            time.sleep(0.05)
        assert leftovers.sweep().removed == []
        assert tarball.exists()
    finally:
        holder.kill()
        holder.wait()


# ---- never through a link --------------------------------------------------------------------


def test_links_are_never_followed(cache: Path, tmp_path: Path) -> None:
    outside = tmp_path / "outside"
    victim_dir = _dir(outside / "precious")
    victim = _file(outside / f"save.{DEAD_PID}-{'d' * 32}.part")
    (cache / "czn").mkdir(parents=True)
    run_link = cache / "czn" / ".run-evil"
    run_link.symlink_to(victim_dir)
    _old(run_link)
    # A whole cache sub-folder that is a link is not descended into.
    (cache / "linked").symlink_to(outside)
    leftovers.sweep()
    assert victim_dir.is_dir() and (victim_dir / "f0").exists()
    assert victim.exists()
    assert not os.path.lexists(run_link)  # the dangling-style link itself may go; its target never


def test_a_runners_folder_that_is_a_link_is_not_swept(tmp_path: Path) -> None:
    real = tmp_path / "elsewhere"
    unpack = _dir(real / ".unpack-x")
    paths.runners_dir().parent.mkdir(parents=True, exist_ok=True)
    paths.runners_dir().symlink_to(real)
    leftovers.sweep()
    assert unpack.exists()


def test_errors_never_escape(cache: Path, monkeypatch: pytest.MonkeyPatch) -> None:
    _file(_wv2_run(cache, DEAD_PID))

    def boom(*_a: object, **_k: object) -> None:
        raise PermissionError("read-only")

    monkeypatch.setattr(leftovers.os, "unlink", boom)
    result = leftovers.sweep()
    assert result.removed == []


def test_nothing_on_disk_is_fine() -> None:
    assert leftovers.sweep().removed == []


# ---- Sandbox link relay (#1417: nadare-links-*) ---------------------------------------------


def test_link_relay_folders_carry_the_owner_pid(tmp_path: Path) -> None:
    from nadare.core import linkrelay

    relay = linkrelay.LinkRelay(tmp_path)
    try:
        pipe = relay.start()
        assert pipe.parent.name.startswith(f"nadare-links-{os.getpid()}-")
    finally:
        relay.stop()


def test_dead_link_relay_folders_are_removed(tmp_path: Path) -> None:
    dead = _dir(tmp_path / f"nadare-links-{DEAD_PID}-abc")
    live = _dir(tmp_path / f"nadare-links-{os.getpid()}-abc")
    legacy_old = _dir(tmp_path / "nadare-links-k2j3h4")  # b24 name, no pid
    legacy_young = _dir(tmp_path / "nadare-links-q9w8e7", age=3600)
    other = _dir(tmp_path / "something-else")
    leftovers.sweep(temp_dir=tmp_path)
    assert not dead.exists() and not legacy_old.exists()
    assert live.exists() and legacy_young.exists() and other.exists()


def test_the_temp_dir_defaults_to_the_system_one(monkeypatch: pytest.MonkeyPatch) -> None:
    seen: list[Path] = []
    monkeypatch.setattr(leftovers, "_sweep_link_relays", lambda d, *a: seen.append(d) or [])
    leftovers.sweep()
    # conftest points the system temp folder at a per-test folder: the real /tmp is never swept.
    assert seen == [leftovers._system_temp()]
    assert str(seen[0]) != leftovers.tempfile.gettempdir()
    monkeypatch.undo()
    assert str(leftovers._system_temp()) == leftovers.tempfile.gettempdir()


# ---- GUI wiring ------------------------------------------------------------------------------


class _Win:
    from nadare.gui.main_window_parts.cleanup import CleanupMixin as _M

    _start_leftover_sweep = _M._start_leftover_sweep
    _run_leftover_sweep = _M._run_leftover_sweep

    def __init__(self) -> None:
        import types

        self.config = types.SimpleNamespace(profiles={}, notice=None, data={})


def _sync_threads(monkeypatch: pytest.MonkeyPatch) -> None:
    import threading

    class T:
        def __init__(self, target, args=(), **_k):
            self.target, self.args = target, args

        def start(self):
            self.target(*self.args)

    monkeypatch.setattr(threading, "Thread", T)


def test_gui_start_up_sweeps(monkeypatch: pytest.MonkeyPatch, cache: Path) -> None:
    _sync_threads(monkeypatch)
    run = _file(_wv2_run(cache, DEAD_PID))
    _Win()._start_leftover_sweep()
    assert not run.exists()


def test_gui_sweep_failure_never_escapes(monkeypatch: pytest.MonkeyPatch) -> None:
    _sync_threads(monkeypatch)

    def boom(**_k: object) -> None:
        raise RuntimeError("bug")

    monkeypatch.setattr(leftovers, "sweep", boom)
    _Win()._start_leftover_sweep()


def test_main_window_schedules_the_sweep() -> None:
    src = Path(__file__).resolve().parents[1] / "src/nadare/gui/main_window.py"
    assert "self._start_leftover_sweep)" in src.read_text()


# ---- a verified runner tarball left by a crash during unpack is reused -----------------------


def _runner_release(monkeypatch: pytest.MonkeyPatch, body: bytes) -> str:
    import hashlib

    from nadare.core import runners

    runners.clear_release_cache()
    tag = "GE-Proton11-7"
    name = f"{tag}-x86_64.tar.gz"
    payload = {
        "tag_name": tag,
        "published_at": "2026-01-01T00:00:00Z",
        "assets": [
            {
                "name": name,
                "size": len(body),
                "digest": "sha256:" + hashlib.sha256(body).hexdigest(),
                "browser_download_url": f"https://github.com/x/{tag}/releases/download/{tag}/{name}",
            }
        ],
    }
    monkeypatch.setattr(runners.download, "fetch_json", lambda *a, **k: payload)
    return name


def test_a_verified_tarball_in_the_cache_is_not_downloaded_again(
    monkeypatch: pytest.MonkeyPatch, cache: Path
) -> None:
    from nadare.core import runners

    body = b"r" * 1024
    name = _runner_release(monkeypatch, body)
    tarball = cache / "runners" / name
    tarball.parent.mkdir(parents=True)
    tarball.write_bytes(body)
    monkeypatch.setattr(
        runners.download, "download", lambda *a, **k: pytest.fail("downloaded again")
    )
    extracted: list[Path] = []
    monkeypatch.setattr(
        runners, "_extract", lambda p, **k: extracted.append(p) or paths.runners_dir() / "x"
    )
    runners.install("ge", latest=True)
    assert extracted == [tarball]
    assert not tarball.exists(), "the tarball is removed after the unpack, as after a download"


@pytest.mark.parametrize("kind", ["wrong", "short", "link"])
def test_a_tarball_that_does_not_verify_is_downloaded(
    monkeypatch: pytest.MonkeyPatch, cache: Path, tmp_path: Path, kind: str
) -> None:
    from nadare.core import runners

    body = b"r" * 1024
    name = _runner_release(monkeypatch, body)
    tarball = cache / "runners" / name
    tarball.parent.mkdir(parents=True)
    if kind == "wrong":
        tarball.write_bytes(b"w" * 1024)
    elif kind == "short":
        tarball.write_bytes(body[:100])
    else:
        real = tmp_path / "elsewhere.tar.gz"
        real.write_bytes(body)
        tarball.symlink_to(real)
    calls: list[Path] = []

    def fake_download(url, dest, **_k):
        calls.append(dest)
        dest.unlink(missing_ok=True)
        dest.write_bytes(body)
        return runners.download.DownloadResult(dest, len(body), "s", "t")

    monkeypatch.setattr(runners.download, "download", fake_download)
    monkeypatch.setattr(runners, "_extract", lambda p, **k: paths.runners_dir() / "x")
    runners.install("ge", latest=True)
    assert calls == [tarball]


# ---- guards found weak by the mutation round -------------------------------------------------


def test_only_the_runners_cache_drops_tarballs(cache: Path) -> None:
    other = _file(cache / "czn" / "something.tar.xz")
    top = _file(cache / "x.tar.gz")
    leftovers.sweep()
    assert other.exists() and top.exists()


def test_a_young_unpack_without_a_live_owner_is_kept() -> None:
    unpack = _dir(paths.runners_dir() / ".unpack-young", age=3600)
    (unpack / ".nadare-owner-pid").write_text(str(DEAD_PID))
    _old(unpack, 3600)
    leftovers.sweep()
    assert unpack.exists()


def test_a_lock_on_a_filesystem_without_locks_counts_as_free(
    cache: Path, monkeypatch: pytest.MonkeyPatch
) -> None:
    import errno

    part = _file(cache / "runners" / "p.tar.xz.part")
    _file(cache / "runners" / ".p.tar.xz.lock", 0)

    def no_locks(*_a: object) -> None:
        raise OSError(errno.ENOLCK, "no locks")

    monkeypatch.setattr(leftovers.fcntl, "flock", no_locks)
    leftovers.sweep()
    assert not part.exists(), "an old download is not 'in use' only because locks are unsupported"


@pytest.mark.parametrize("code", ["EIO", "EBADF", "EINVAL", "EPERM"])
def test_an_unexpected_lock_error_is_not_proof_the_download_is_idle(
    cache: Path, monkeypatch: pytest.MonkeyPatch, code: str
) -> None:
    """#1468: only "locks are unsupported" counts as free; any other failure keeps the file."""
    import errno

    part = _file(cache / "runners" / "p.tar.xz.part")
    _file(cache / "runners" / ".p.tar.xz.lock", 0)

    def broken(*_a: object) -> None:
        raise OSError(getattr(errno, code), "lock inspection failed")

    monkeypatch.setattr(leftovers.fcntl, "flock", broken)
    leftovers.sweep()
    assert part.exists(), f"{code} while checking the lock removed a download that may be in use"


@pytest.mark.parametrize("code", ["ENOLCK", "EOPNOTSUPP", "ENOTSUP"])
def test_every_unsupported_lock_error_counts_as_free(
    cache: Path, monkeypatch: pytest.MonkeyPatch, code: str
) -> None:
    import errno

    part = _file(cache / "runners" / "p.tar.xz.part")
    _file(cache / "runners" / ".p.tar.xz.lock", 0)

    def unsupported(*_a: object) -> None:
        raise OSError(getattr(errno, code), "no locks")

    monkeypatch.setattr(leftovers.fcntl, "flock", unsupported)
    leftovers.sweep()
    assert not part.exists()


def test_one_failed_removal_does_not_stop_the_others(
    cache: Path, monkeypatch: pytest.MonkeyPatch
) -> None:
    files = [_file(_wv2_run(cache, DEAD_PID + i)) for i in range(3)]
    real_unlink = os.unlink
    failed: list[str] = []

    def first_fails(path: object, *a: object, **k: object) -> None:
        if not failed:  # whichever leftover the listing gives first
            failed.append(str(path))
            raise PermissionError("busy")
        real_unlink(path, *a, **k)

    monkeypatch.setattr(leftovers.os, "unlink", first_fails)
    result = leftovers.sweep()
    assert [str(p) for p in files if p.exists()] == failed
    assert len(result.removed) == 2


def test_a_failing_step_does_not_stop_the_next(monkeypatch: pytest.MonkeyPatch) -> None:
    unpack = _dir(paths.runners_dir() / ".unpack-after")

    def boom(*_a: object) -> None:
        raise RuntimeError("bug in the cache step")

    monkeypatch.setattr(leftovers, "_sweep_cache", boom)
    leftovers.sweep()
    assert not unpack.exists()
