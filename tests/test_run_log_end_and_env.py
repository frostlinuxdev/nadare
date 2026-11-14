# SPDX-License-Identifier: GPL-3.0-or-later
"""Every run log says how the run ended; a launch log also says what the game got (#1126).

A bundle is often the only thing a report contains, and a launch log used to stop mid-stream:
a crash (SIGSEGV), a clean quit and "Nadare was closed" looked the same, and the environment
the game was started with was never written.
"""

from __future__ import annotations

import os

os.environ.setdefault("QT_QPA_PLATFORM", "offscreen")

import pytest
from _backend_helpers import make_context, write_exe
from _parts_patch import patch_everywhere

from nadare.core.backends import BackendError
from nadare.core.config import Config
from nadare.core.events import Cancelled
from nadare.gui import workers


class _FakeBackend:
    def __init__(self, result):
        self._result = result

    def launch(self):
        if isinstance(self._result, BaseException):
            raise self._result
        return self._result

    def install(self):
        if isinstance(self._result, BaseException):
            raise self._result


def _run(tmp_path, monkeypatch, result, operation="launch") -> str:
    monkeypatch.setattr(workers.paths, "logs_dir", lambda: tmp_path / "logs")
    monkeypatch.setattr(workers, "create", lambda *a, **k: _FakeBackend(result))
    worker = workers.BackendWorker(Config.load(tmp_path / "config.json"), "nikke", operation)
    worker.run()
    (path,) = (tmp_path / "logs" / "nikke").glob(f"{operation}-*.log")
    return path.read_text(encoding="utf-8")


def _end_line(text: str) -> str:
    ends = [line for line in text.splitlines() if line.startswith("[end] ")]
    assert len(ends) == 1, text
    return ends[0]


def test_a_crash_is_named_with_its_signal(tmp_path, monkeypatch):
    line = _end_line(_run(tmp_path, monkeypatch, 139))
    assert "exit=139" in line and "SIGSEGV" in line and "after " in line


def test_a_raw_negative_signal_is_named_too(tmp_path, monkeypatch):
    line = _end_line(_run(tmp_path, monkeypatch, -6))
    assert "SIGABRT" in line


def test_a_clean_exit_says_so(tmp_path, monkeypatch):
    line = _end_line(_run(tmp_path, monkeypatch, 0))
    assert "exit=0" in line and "SIG" not in line


def test_a_cancelled_run_says_so(tmp_path, monkeypatch):
    text = _run(tmp_path, monkeypatch, Cancelled())
    assert "cancelled" in _end_line(text)


def test_a_failed_run_says_why(tmp_path, monkeypatch):
    line = _end_line(_run(tmp_path, monkeypatch, BackendError("game executable not found")))
    assert "failed" in line and "game executable not found" in line


def test_an_unexpected_error_still_ends_the_log(tmp_path, monkeypatch):
    line = _end_line(_run(tmp_path, monkeypatch, RuntimeError("boom")))
    assert "failed" in line and "boom" in line


def test_other_operations_end_their_log_too(tmp_path, monkeypatch):
    line = _end_line(_run(tmp_path, monkeypatch, None, operation="install"))
    assert "exit=0" in line


# ---- what the game got --------------------------------------------------------------------
def _nikke(tmp_path, **profile):
    from nadare.backends.launcher import NikkeBackend

    values = {"prefix": str(tmp_path / "prefix"), "wine_binary": "/usr/bin/wine", **profile}
    write_exe(tmp_path / "prefix" / "drive_c" / "NIKKE" / "Launcher" / "nikke_launcher.exe", "MZ")
    ctx = make_context(tmp_path, "launcher-nikke", values)
    events: list = []
    ctx.emit = events.append
    return NikkeBackend(ctx), events


@pytest.fixture
def quiet_launch(monkeypatch):
    from nadare.backends import winegame as wg

    patch_everywhere(monkeypatch, wg, "stream", lambda *a, **k: 0)
    monkeypatch.setattr(
        wg.WineGameBackend, "_ensure_default_runner", lambda self, strict=False: None
    )


def _messages(events) -> list[str]:
    return [str(getattr(e, "message", "")) for e in events]


def test_a_launch_logs_the_environment_the_game_got(tmp_path, quiet_launch):
    backend, events = _nikke(tmp_path, secure_mode=False)
    backend.launch()
    env_lines = [m for m in _messages(events) if m.startswith("env: ")]
    assert len(env_lines) == 1
    assert f"WINEPREFIX={tmp_path / 'prefix'}" in env_lines[0]


def test_the_logged_environment_hides_secrets(tmp_path, quiet_launch, monkeypatch):
    backend, events = _nikke(
        tmp_path, secure_mode=False, extra_env={"NIKKE_DEBUG": "tok-12345-secret"}
    )
    monkeypatch.setattr(backend, "launch_secrets", lambda: ("tok-12345-secret",))
    backend.launch()
    joined = "\n".join(_messages(events))
    assert "tok-12345-secret" not in joined


def test_a_launch_logs_the_gpu_and_driver(tmp_path, quiet_launch, monkeypatch):
    from nadare.core import graphics

    info = graphics.VulkanInfo(
        available=True, api_version="1.4.305", device="AMD Radeon RX 7800 XT", driver="radv"
    )
    monkeypatch.setattr(graphics, "vulkan_info", lambda **_k: info)
    backend, events = _nikke(tmp_path, secure_mode=False)
    backend.launch()
    gpu = [m for m in _messages(events) if m.startswith("gpu: ")]
    assert gpu and "RX 7800 XT" in gpu[0] and "radv" in gpu[0]
