# SPDX-License-Identifier: GPL-3.0-or-later
"""Regression tests for the backend findings of review 1108b (items 1-7)."""

from __future__ import annotations

import json
import threading
from pathlib import Path

import pytest
from _backend_helpers import make_context, write_exe
from _parts_patch import patch_everywhere

from nadare.backends import arknights as ak
from nadare.backends import direct, launcher
from nadare.backends import winegame as wg
from nadare.backends.launcher import NikkeBackend
from nadare.backends.winegame import WineGameBackend
from nadare.core import railrepo, sevenzip
from nadare.core.backends import BackendError
from nadare.core.sources import nikke as nikke_source

BWRAP_FAILURE = (
    "pressure-vessel-wrap[1778]: E: Child process exited with code 1: "
    "bwrap: setting up uid map: Permission denied"
)


class _Probe:
    result = None

    def start(self):
        return self

    def stop(self):
        pass

    def join(self, timeout=None):
        pass


def _nikke(tmp_path: Path, events: list | None = None) -> NikkeBackend:
    prefix = tmp_path / "prefix"
    write_exe(prefix / "drive_c" / "NIKKE" / "Launcher" / "nikke_launcher.exe", "MZ")
    ctx = make_context(
        tmp_path,
        "launcher-nikke",
        {"prefix": str(prefix), "wine_binary": "/usr/bin/wine"},
    )
    backend = NikkeBackend(ctx)
    seen = events if events is not None else []
    backend.ctx.emit = seen.append
    return backend


@pytest.fixture
def launch_rig(tmp_path, monkeypatch):
    """A configured NIKKE launcher whose runner, probe and stream are faked."""
    events: list = []
    backend = _nikke(tmp_path, events)
    seen: list[list[str]] = []
    monkeypatch.setattr(WineGameBackend, "_ensure_default_runner", lambda self, strict: None)
    monkeypatch.setattr(WineGameBackend, "_run_vcredist", lambda self, created=False: None)
    monkeypatch.setattr(backend, "_renderer_probe", lambda: _Probe())
    return backend, events, seen


# ---- 1: the VC++ step's output must not feed the launch verdict -------------------------
def test_vcredist_output_does_not_make_a_good_launch_look_failed(
    launch_rig, tmp_path, monkeypatch
):
    backend, events, _seen = launch_rig

    def fake_vcredist(self, created=False):
        self._on_line(BWRAP_FAILURE)  # best effort: they carry on and the game runs

    def fake_stream(argv, env, on_line=None, cancel=None):
        return 0

    monkeypatch.setattr(WineGameBackend, "_run_vcredist", fake_vcredist)
    patch_everywhere(monkeypatch, wg, "stream", fake_stream)

    assert backend.launch() == 0
    assert backend._watch.userns_blocked is False
    assert not any("could not start" in event.message for event in events)


# ---- 2: exit code 0 is a good session, even if a child printed a runtime failure --------
def test_a_runtime_failure_after_a_long_session_with_exit_0_is_a_status(
    launch_rig, monkeypatch
):
    from nadare.core import userns

    backend, events, _seen = launch_rig
    clock = [1000.0]
    monkeypatch.setattr(wg.time, "monotonic", lambda: clock[0])

    def fake_stream(argv, env, on_line=None, cancel=None):
        on_line(BWRAP_FAILURE)
        clock[0] += 3600  # an hour of play
        return 0

    patch_everywhere(monkeypatch, wg, "stream", fake_stream)

    assert backend.launch() == 0
    status = [event.message for event in events if event.kind == "status"]
    assert status and userns.HINT_HEAD in status[-1]
    assert not any(event.kind == "error" for event in events)


def test_a_runtime_failure_with_a_nonzero_exit_still_fails(launch_rig, monkeypatch):
    backend, _events, _seen = launch_rig

    def fake_stream(argv, env, on_line=None, cancel=None):
        on_line(BWRAP_FAILURE)
        return 1

    patch_everywhere(monkeypatch, wg, "stream", fake_stream)
    with pytest.raises(BackendError, match="could not start"):
        backend.launch()


def test_a_quick_exit_0_with_a_runtime_failure_still_fails(launch_rig, monkeypatch):
    """umu exits 0 when it could not set up the runtime: that start never happened."""
    backend, _events, _seen = launch_rig

    def fake_stream(argv, env, on_line=None, cancel=None):
        on_line(BWRAP_FAILURE)
        return 0

    patch_everywhere(monkeypatch, wg, "stream", fake_stream)
    with pytest.raises(BackendError, match="could not start"):
        backend.launch()


def test_update_by_starting_the_launcher_survives_exit_code_0(launch_rig, monkeypatch):
    backend, _events, _seen = launch_rig
    clock = [1000.0]
    monkeypatch.setattr(wg.time, "monotonic", lambda: clock[0])

    def fake_stream(argv, env, on_line=None, cancel=None):
        on_line(BWRAP_FAILURE)
        clock[0] += 600  # the launcher downloaded the update and quit
        return 0

    patch_everywhere(monkeypatch, wg, "stream", fake_stream)
    backend.update()  # used to raise although the launcher updated and quit normally


# ---- 3: a failed direct NIKKE download must not vouch for the silent installer ----------
def _install_rig(tmp_path, monkeypatch):
    prefix = tmp_path / "prefix"
    (prefix / "drive_c").mkdir(parents=True)
    backend = NikkeBackend(
        make_context(
            tmp_path,
            "launcher-nikke",
            {"prefix": str(prefix), "wine_binary": "/usr/bin/wine"},
        )
    )
    events: list = []
    backend.ctx.emit = events.append
    calls: list[list[str]] = []
    launcher_dir = prefix / "drive_c" / "NIKKE" / "Launcher"

    def failing_direct(folder, emit=None, cancel=None):
        folder.mkdir(parents=True, exist_ok=True)
        (folder / "nikke_launcher.exe").write_text("MZ", encoding="utf-8")
        raise railrepo.RailError("the CDN dropped the connection")

    def silent_that_writes_nothing(argv, env, on_line=None, cancel=None):
        calls.append(list(argv))
        return 0

    def fake_extract(sevenz, archive, outdir, run=None):
        outdir.mkdir(parents=True, exist_ok=True)
        (outdir / "nikkeminiloader.exe").write_text("MZ", encoding="utf-8")

    installer = write_exe(tmp_path / "nikke-wrapper.exe", "MZ")
    monkeypatch.setattr(nikke_source, "install_launcher", failing_direct)
    monkeypatch.setattr(WineGameBackend, "_ensure_default_runner", lambda self, strict: None)
    monkeypatch.setattr(WineGameBackend, "_ensure_prefix", lambda self: None)
    monkeypatch.setattr(WineGameBackend, "_run_vcredist", lambda self, created=False: None)
    monkeypatch.setattr(WineGameBackend, "_download_installer", lambda self, url: installer)
    monkeypatch.setattr(launcher, "_extract_nsis", fake_extract)
    monkeypatch.setattr(sevenzip, "find", lambda **kw: "/usr/bin/7z")
    monkeypatch.setattr(ak, "stream", silent_that_writes_nothing)
    patch_everywhere(monkeypatch, wg, "stream", silent_that_writes_nothing)
    monkeypatch.delenv("NADARE_NIKKE_DIRECT", raising=False)
    monkeypatch.delenv("NADARE_NIKKE_SILENT", raising=False)
    return backend, events, calls, launcher_dir


def test_a_failed_direct_download_does_not_make_a_silent_noop_look_installed(
    tmp_path, monkeypatch
):
    backend, events, calls, launcher_dir = _install_rig(tmp_path, monkeypatch)
    with pytest.raises(BackendError):
        backend.install()
    assert calls, "the fallback installer still runs"
    assert (launcher_dir / "nikke_launcher.exe").is_file()  # left by the failed attempt
    assert not any("launcher installed" in event.message for event in events)


# ---- 4: the marker's update line normalises the versions it writes ---------------------
def _marker_backend(tmp_path: Path) -> direct.BrownDust2Backend:
    backend = direct.BrownDust2Backend(
        make_context(tmp_path, "direct-bd2", {"prefix": str(tmp_path / "pfx")})
    )
    backend.install_dir().mkdir(parents=True, exist_ok=True)
    return backend


def test_the_marker_normalises_whitespace_and_cuts_long_versions(tmp_path):
    backend = _marker_backend(tmp_path)
    backend._write_install_marker(("1.0 beta", "2" * 80))
    assert backend._interrupted_update() == ("1.0_beta", "2" * 64)


def test_a_normal_version_survives_the_round_trip_unchanged(tmp_path):
    backend = _marker_backend(tmp_path)
    backend._write_install_marker(("1.0", "2.0"))
    assert backend._interrupted_update() == ("1.0", "2.0")


# ---- 5: the version file is read as defensively as the marker --------------------------
def test_a_linked_version_file_is_not_read(tmp_path):
    backend = _marker_backend(tmp_path)
    elsewhere = tmp_path / "elsewhere.json"
    elsewhere.write_text(json.dumps({"version": "9.9", "game_tag": "bd2"}), encoding="utf-8")
    backend._version_path().symlink_to(elsewhere)
    assert backend._read_version_file() is None


def test_a_huge_version_file_is_not_read_into_memory(tmp_path):
    backend = _marker_backend(tmp_path)
    backend._version_path().write_text(
        '{"version":"' + "x" * 70_000 + '"}', encoding="utf-8"
    )
    assert backend._read_version_file() is None


def test_a_normal_version_file_still_reads(tmp_path):
    backend = _marker_backend(tmp_path)
    backend._write_version_file("1.2.3", "bd2")
    assert backend._read_version_file()["version"] == "1.2.3"


# ---- 6: the progress watcher stops touching the finished run's log ---------------------
def test_the_watcher_does_not_emit_after_stop_was_set_during_the_measurement(
    tmp_path, monkeypatch
):
    backend = _nikke(tmp_path)
    events: list = []
    backend.ctx.emit = events.append
    stop = threading.Event()
    target = tmp_path / "target"
    target.mkdir()

    def slow_measure(root, limit=50_000):
        stop.set()  # the operation ended while a blocking stat was running
        return 1000

    monkeypatch.setattr(launcher, "_folder_bytes", slow_measure)
    monkeypatch.setattr(launcher, "_SILENT_POLL_SECONDS", 0.001)
    backend._watch_silent_install(target, stop, 0)
    assert events == []


def test_the_watcher_survives_a_log_that_was_already_closed(tmp_path, monkeypatch):
    backend = _nikke(tmp_path)
    stop = threading.Event()
    target = tmp_path / "target"
    target.mkdir()
    calls = 0

    def growing_measure(root, limit=50_000):
        nonlocal calls
        calls += 1
        if calls >= 2:
            stop.set()
        return 1000

    def closed_log(event):
        raise ValueError("I/O operation on closed file")

    monkeypatch.setattr(launcher, "_folder_bytes", growing_measure)
    monkeypatch.setattr(launcher, "_SILENT_POLL_SECONDS", 0.001)
    backend.ctx.emit = closed_log
    backend._watch_silent_install(target, stop, 0)  # used to raise in the watcher thread
