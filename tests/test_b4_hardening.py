# SPDX-License-Identifier: GPL-3.0-or-later
"""Hardening from the 0.1.0b4 audit: installer timeout and private copy, safe
download file, extraction caps, serialised runner installs, ldconfig location,
private logs, and a clean exit without a display."""

from __future__ import annotations

import hashlib
import io
import os
import tarfile
import threading
import time
from pathlib import Path

import pytest

from nadare.core import hostlibs, runners, vcredist
from nadare.core.backends import BackendError
from nadare.core.events import Cancel

# conftest replaces hostlibs.missing_32bit for every test; keep the real function.
_REAL_MISSING_32BIT = hostlibs.missing_32bit

PAYLOAD = b"MZ-fake-installer"


def _serve(payload: bytes):
    class Resp:
        headers = {"Content-Length": str(len(payload))}

        def __init__(self):
            self._done = False

        def __enter__(self):
            return self

        def __exit__(self, *_a):
            return False

        def geturl(self):
            return vcredist.VCREDIST_URL

        def read(self, _n=-1):
            if self._done:
                return b""
            self._done = True
            return payload

    return lambda *a, **k: Resp()


def _install(tmp_path, monkeypatch, run):
    monkeypatch.setattr(vcredist.urllib.request, "urlopen", _serve(PAYLOAD))
    monkeypatch.setattr(vcredist._net, "http_open", lambda *a, **k: _serve(PAYLOAD)())
    vcredist.install(
        tmp_path / "prefix",
        profile={},
        cache_dir=tmp_path / "cache",
        run_in_prefix=run,
        emit=lambda _e: None,
        cancel=Cancel(),
        constant=hashlib.sha256(PAYLOAD).hexdigest(),
    )


# ---- VC++ installer ----------------------------------------------------------------
def test_the_installer_runs_from_a_private_verified_copy_not_the_cache(tmp_path, monkeypatch):
    seen: list[Path] = []

    def run(argv):
        exe = Path(argv[0])
        seen.append(exe)
        assert exe.read_bytes() == PAYLOAD
        assert (exe.stat().st_mode & 0o777) == 0o500
        assert (exe.parent.stat().st_mode & 0o777) == 0o700
        return 0

    _install(tmp_path, monkeypatch, run)
    cache = tmp_path / "cache"
    assert seen and seen[0].parent != cache and seen[0].parent.parent == cache
    assert not seen[0].parent.exists(), "the private copy is removed afterwards"


def test_a_tampered_copy_is_never_run(tmp_path, monkeypatch):
    monkeypatch.setattr(
        vcredist.shutil, "copyfile", lambda src, dst: Path(dst).write_bytes(b"EVIL")
    )
    ran: list = []
    with pytest.raises(BackendError):
        _install(tmp_path, monkeypatch, lambda argv: ran.append(argv) or 0)
    assert ran == []


def test_a_planted_part_symlink_is_not_followed(tmp_path, monkeypatch):
    cache = tmp_path / "cache"
    cache.mkdir()
    victim = tmp_path / "victim.txt"
    victim.write_text("precious")
    (cache / "vc_redist.x64.part").symlink_to(victim)
    _install(tmp_path, monkeypatch, lambda argv: 0)
    assert victim.read_text() == "precious"


def test_a_hung_installer_is_stopped_after_the_timeout(tmp_path, monkeypatch):
    monkeypatch.setenv("NADARE_VCREDIST_TIMEOUT", "0.3")
    release = threading.Event()
    monkeypatch.setattr(vcredist, "_stop_installer", lambda *a, **k: release.set() or 1)

    def hang(_argv):
        release.wait(10)  # a stalled Wine installer: only the watchdog frees it
        return 1

    start = time.monotonic()
    with pytest.raises(BackendError, match="did not finish"):
        _install(tmp_path, monkeypatch, hang)
    assert time.monotonic() - start < 5


def test_a_normal_installer_is_not_touched_by_the_watchdog(tmp_path, monkeypatch):
    monkeypatch.setenv("NADARE_VCREDIST_TIMEOUT", "30")
    monkeypatch.setattr(vcredist, "_stop_installer", lambda *a, **k: pytest.fail("stopped"))
    _install(tmp_path, monkeypatch, lambda argv: 0)


def test_the_timeout_defaults_to_ten_minutes_and_ignores_garbage(monkeypatch):
    monkeypatch.delenv("NADARE_VCREDIST_TIMEOUT", raising=False)
    assert vcredist._install_timeout() == 600.0
    monkeypatch.setenv("NADARE_VCREDIST_TIMEOUT", "nonsense")
    assert vcredist._install_timeout() == 600.0
    monkeypatch.setenv("NADARE_VCREDIST_TIMEOUT", "-5")
    assert vcredist._install_timeout() == 600.0


def test_stop_installer_only_targets_the_redistributable(tmp_path, monkeypatch):
    proc = tmp_path / "proc"
    own = os.getpid()
    for pid, cmd in (
        ("101", b"python3\0proton\0run\0/x/vc_redist.x64.exe\0/install\0/quiet\0"),
        ("102", b"vim\0notes.txt\0"),
        ("103", b"steam.exe\0vc_redist.x64.exe\0/install\0/quiet\0/norestart\0"),
        ("104", b"wine\0/x/vc_redist.x64.exe\0/install\0/quiet\0"),
    ):
        (proc / pid).mkdir(parents=True)
        (proc / pid / "cmdline").write_bytes(cmd)
        # Only 101 and 103 are this launcher's descendants; 104 belongs to
        # another Nadare instance and must be left alone.
        ppid = "1" if pid == "104" else str(own)
        (proc / pid / "stat").write_text(f"{pid} (test) S {ppid} {pid} 0 0\n")
    killed: list = []
    monkeypatch.setattr(os, "killpg", lambda pid, sig: killed.append(pid))
    monkeypatch.setattr(vcredist.time, "sleep", lambda _s: None)
    assert vcredist._stop_installer(str(proc)) == 2
    assert sorted(set(killed)) == [101, 103]


# ---- runner extraction -----------------------------------------------------------------
def _tarball(tmp_path: Path, names: list[str], payload: bytes = b"x") -> Path:
    path = tmp_path / "r.tar"
    with tarfile.open(path, "w") as archive:
        for name in names:
            info = tarfile.TarInfo(name)
            info.size = len(payload)
            archive.addfile(info, io.BytesIO(payload))
    return path


def test_extraction_refuses_an_archive_with_too_many_members(tmp_path, monkeypatch):
    monkeypatch.setattr(runners.paths, "runners_dir", lambda: tmp_path / "runners")
    monkeypatch.setattr(runners, "_MAX_MEMBERS", 2)
    tar = _tarball(tmp_path, ["r/proton", "r/a", "r/b"])
    with pytest.raises(runners.RunnerError, match="far larger"):
        runners._extract(tar, cancel=None, emit=lambda _e: None)
    assert not any(p.name == "r" for p in (tmp_path / "runners").iterdir())


def test_extraction_refuses_an_archive_that_unpacks_too_large(tmp_path, monkeypatch):
    monkeypatch.setattr(runners.paths, "runners_dir", lambda: tmp_path / "runners")
    monkeypatch.setattr(runners, "_MAX_UNPACKED_BYTES", 10)
    tar = _tarball(tmp_path, ["r/proton", "r/big"], payload=b"y" * 8)
    with pytest.raises(runners.RunnerError, match="far larger"):
        runners._extract(tar, cancel=None, emit=lambda _e: None)


def test_a_normal_archive_still_extracts(tmp_path, monkeypatch):
    monkeypatch.setattr(runners.paths, "runners_dir", lambda: tmp_path / "runners")
    tar = _tarball(tmp_path, ["r/proton", "r/files/x"])
    dest = runners._extract(tar, cancel=None, emit=lambda _e: None)
    assert (dest / "proton").is_file()


def test_a_cancel_after_unpacking_does_not_install(tmp_path, monkeypatch):
    monkeypatch.setattr(runners.paths, "runners_dir", lambda: tmp_path / "runners")
    tar = _tarball(tmp_path, ["r/proton"])

    class LateCancel:
        def __init__(self):
            self.calls = 0

        cancelled = False

        def raise_if_cancelled(self):
            self.calls += 1
            if self.calls >= 3:  # after the member loop, before the swap
                from nadare.core.events import Cancelled

                raise Cancelled()

    from nadare.core.events import Cancelled

    with pytest.raises(Cancelled):
        runners._extract(tar, cancel=LateCancel(), emit=lambda _e: None)
    assert not (tmp_path / "runners" / "r").exists()


def test_the_asset_lock_serialises_two_installs(tmp_path):
    order: list[str] = []
    gate = threading.Event()

    def first():
        with runners._asset_lock(tmp_path, "asset.tar.xz"):
            order.append("first-in")
            gate.set()
            time.sleep(0.3)
            order.append("first-out")

    def second():
        gate.wait(5)
        with runners._asset_lock(tmp_path, "asset.tar.xz"):
            order.append("second-in")

    threads = [threading.Thread(target=first), threading.Thread(target=second)]
    for t in threads:
        t.start()
    for t in threads:
        t.join(10)
    assert order == ["first-in", "first-out", "second-in"]


# ---- small items -------------------------------------------------------------------------
def test_ldconfig_is_tried_from_system_locations_before_path(monkeypatch):
    tried: list[str] = []

    class Result:
        stdout = ""

    def fake_run(cmd, **_k):
        tried.append(cmd[0])
        return Result()

    _REAL_MISSING_32BIT(run=fake_run)
    assert tried[:3] == ["/usr/sbin/ldconfig", "/sbin/ldconfig", "/usr/bin/ldconfig"]
    assert tried[-1] == "ldconfig"


def test_operation_logs_are_private(tmp_path, monkeypatch):
    pytest.importorskip("PyQt6.QtWidgets")
    from nadare.gui import workers

    monkeypatch.setattr(workers.paths, "logs_dir", lambda: tmp_path / "logs")
    old = os.umask(0o022)
    try:
        holder = type("T", (), {"_log": None, "_profile": "p", "_operation": "install"})()
        handle = workers.BackendWorker._open_log(holder)  # type: ignore[attr-defined]
        handle.close()
    finally:
        os.umask(old)
    mode = (tmp_path / "logs" / "p-install.log").stat().st_mode & 0o777
    assert mode == 0o600


def test_the_gui_exits_cleanly_without_a_display(monkeypatch):
    pytest.importorskip("PyQt6.QtWidgets")
    from nadare import gui

    for name in ("DISPLAY", "WAYLAND_DISPLAY", "QT_QPA_PLATFORM"):
        monkeypatch.delenv(name, raising=False)
    with pytest.raises(SystemExit) as exc:
        gui._require_display()
    assert "no display" in str(exc.value)
    monkeypatch.setenv("QT_QPA_PLATFORM", "offscreen")
    gui._require_display()  # an explicit platform skips the check
    monkeypatch.delenv("QT_QPA_PLATFORM")
    monkeypatch.setenv("WAYLAND_DISPLAY", "wayland-0")
    gui._require_display()
