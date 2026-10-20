# SPDX-License-Identifier: GPL-3.0-or-later
"""Unit tests for nadare.core.process."""

from __future__ import annotations

import signal
import sys
import threading
import time
from pathlib import Path

import pytest

from nadare.core import paths, process
from nadare.core.events import Cancel, Cancelled, Event


# ---------------------------------------------------------------------------
# environment construction
# ---------------------------------------------------------------------------
def test_build_env_pins_xdg_roots(monkeypatch: pytest.MonkeyPatch) -> None:
    monkeypatch.setenv("XDG_DATA_HOME", "/tmp/xdg-data")
    monkeypatch.setenv("XDG_CONFIG_HOME", "/tmp/xdg-config")
    monkeypatch.setenv("XDG_STATE_HOME", "/tmp/xdg-state")
    monkeypatch.setenv("XDG_CACHE_HOME", "/tmp/xdg-cache")

    env = process.build_env(base={})

    assert env["XDG_DATA_HOME"] == "/tmp/xdg-data"
    assert env["XDG_CONFIG_HOME"] == "/tmp/xdg-config"
    assert env["XDG_STATE_HOME"] == "/tmp/xdg-state"
    assert env["XDG_CACHE_HOME"] == "/tmp/xdg-cache"
    # base={} means nothing else leaks in.
    assert set(env) >= {
        "XDG_DATA_HOME",
        "XDG_CONFIG_HOME",
        "XDG_STATE_HOME",
        "XDG_CACHE_HOME",
    }


def test_build_env_profile_prefix_and_runner() -> None:
    env = process.build_env({"prefix": "/games/pfx", "runner": "/opt/proton/GE"}, base={})
    assert env["WINEPREFIX"] == "/games/pfx"
    assert env["PROTONPATH"] == "/opt/proton/GE"
    assert env["WINEDEBUG"] == "-all"


def test_build_env_extra_overrides_everything() -> None:
    env = process.build_env(
        {"prefix": "/games/pfx"},
        {"WINEPREFIX": "/override", "CUSTOM": "1"},
        base={"WINEDEBUG": "warn"},
    )
    assert env["WINEPREFIX"] == "/override"
    assert env["CUSTOM"] == "1"
    assert env["WINEDEBUG"] == "warn"


def test_build_env_inherits_base_by_default(monkeypatch: pytest.MonkeyPatch) -> None:
    monkeypatch.setenv("NADARE_SENTINEL", "present")
    env = process.build_env()
    assert env["NADARE_SENTINEL"] == "present"


# ---------------------------------------------------------------------------
# spawning / running
# ---------------------------------------------------------------------------
def test_spawn_constructs_string_args(tmp_path: Path) -> None:
    exe = Path(sys.executable)
    proc = process.spawn([exe, "-c", "print('spawned')"], cwd=tmp_path)
    out, _ = proc.communicate(timeout=15)
    assert proc.returncode == 0
    assert isinstance(proc.args[0], str)
    assert out.strip() == "spawned"


def test_run_trivial_command_captures_output() -> None:
    events: list[Event] = []
    cp = process.run(
        [sys.executable, "-c", "print('hello nadare')"],
        capture=True,
        emit=events.append,
    )

    assert cp.returncode == 0
    assert cp.stdout == "hello nadare"
    assert any(e.kind == "status" and e.data.get("stage") == "spawn" for e in events)
    assert any(e.kind == "log" and "hello nadare" in e.message for e in events)


def test_run_check_raises_on_failure() -> None:
    import subprocess

    with pytest.raises(subprocess.CalledProcessError):
        process.run([sys.executable, "-c", "import sys; sys.exit(3)"], check=True)


def test_run_env_is_forwarded() -> None:
    cp = process.run(
        [sys.executable, "-c", "import os; print(os.environ['NADARE_TEST_VAR'])"],
        env={"NADARE_TEST_VAR": "forwarded"},
        capture=True,
    )
    assert cp.stdout == "forwarded"


def test_run_cancellation() -> None:
    cancel = Cancel()

    def _cancel_soon() -> None:
        time.sleep(0.3)
        cancel.cancel()

    timer = threading.Thread(target=_cancel_soon, daemon=True)
    timer.start()
    started = time.monotonic()
    try:
        with pytest.raises(Cancelled):
            process.run(
                [sys.executable, "-c", "import time; time.sleep(30)"],
                cancel=cancel,
            )
    finally:
        timer.join(timeout=2)

    assert time.monotonic() - started < process.GRACE_SECONDS + 5


def test_which_finds_executable() -> None:
    env = {"PATH": str(Path(sys.executable).parent)}
    assert process.which(Path(sys.executable).name, env=env) is not None
    assert process.which("definitely-not-a-real-binary-xyz", env=env) is None


# ---------------------------------------------------------------------------
# profile lock
# ---------------------------------------------------------------------------
def test_profile_lock_is_exclusive(tmp_path: Path, monkeypatch: pytest.MonkeyPatch) -> None:
    monkeypatch.setattr(paths, "state_dir", lambda: tmp_path / "state")

    first = process.ProfileLock("game")
    first.acquire()
    try:
        assert first.held is True
        second = process.ProfileLock("game", wait=0.0)
        with pytest.raises(process.ProfileLockError):
            second.acquire()
    finally:
        first.release()
    assert first.held is False

    # After release the lock can be taken again.
    with process.ProfileLock("game"):
        assert process.ProfileLock("game").held is False


def test_safe_profile_name() -> None:
    assert process._safe("my game!").startswith("my_game-")
    assert process._safe("").startswith("profile-")
    assert len(process._safe("x" * 500)) < 120


def test_safe_profile_names_do_not_alias() -> None:
    # Distinct profiles mapping to the same slug must not share a lock file.
    assert process._safe("a b") != process._safe("a_b")
    assert process._safe("a/b") != process._safe("a:b")
    assert process._safe("x" * 200 + "1") != process._safe("x" * 200 + "2")


def test_find_processes_prefix_requires_a_separator_boundary(
    monkeypatch: pytest.MonkeyPatch,
) -> None:
    procs = [
        process.ProcessInfo(pid=1, name="foo", cmdline=["/games/foo/foo"], exe="/games/foo/foo"),
        process.ProcessInfo(
            pid=2, name="foobar", cmdline=["/games/foobar/foobar"], exe="/games/foobar/foobar"
        ),
    ]
    monkeypatch.setattr(process, "iter_processes", lambda: iter(procs))
    assert [p.pid for p in process.find_processes(prefix="/games/foo")] == [1]


def test_terminate_aborts_escalation_on_pid_reuse(monkeypatch: pytest.MonkeyPatch) -> None:
    calls: list[tuple[int, int]] = []
    monkeypatch.setattr(process.os, "kill", lambda pid, sig: calls.append((pid, sig)))
    starts = iter(["100", "200"])  # identity changes between SIGTERM and SIGKILL
    monkeypatch.setattr(process, "_pid_starttime", lambda pid: next(starts))

    assert process.terminate(4321, grace=0) is False
    assert (4321, signal.SIGTERM) in calls
    assert (4321, signal.SIGKILL) not in calls


def test_terminate_escalates_when_identity_is_stable(monkeypatch: pytest.MonkeyPatch) -> None:
    calls: list[tuple[int, int]] = []
    monkeypatch.setattr(process.os, "kill", lambda pid, sig: calls.append((pid, sig)))
    monkeypatch.setattr(process, "_pid_starttime", lambda pid: "100")

    assert process.terminate(4321, grace=0) is True
    assert (4321, signal.SIGKILL) in calls


def test_terminate_escalates_when_identity_is_unreadable(monkeypatch: pytest.MonkeyPatch) -> None:
    calls: list[tuple[int, int]] = []
    monkeypatch.setattr(process.os, "kill", lambda pid, sig: calls.append((pid, sig)))
    monkeypatch.setattr(process, "_pid_starttime", lambda pid: None)

    assert process.terminate(4321, grace=0) is True
    assert (4321, signal.SIGKILL) in calls


def test_run_masks_long_secrets_in_spawn_event() -> None:
    events: list[Event] = []
    secret = "S" * 40
    process.run([sys.executable, "-c", "pass", secret], emit=events.append)

    spawn = next(e for e in events if e.data.get("stage") == "spawn")
    assert secret not in spawn.message


def test_restore_host_env_puts_back_callers_ld_library_path() -> None:
    env = {"LD_LIBRARY_PATH": "/app/lib/nadare/_internal", "LD_LIBRARY_PATH_ORIG": "/host/lib"}
    process.restore_host_env(env)
    assert env == {"LD_LIBRARY_PATH": "/host/lib"}


def test_restore_host_env_drops_bundle_path_when_caller_had_none() -> None:
    env = {"LD_LIBRARY_PATH": "/app/lib/nadare/_internal", "HOME": "/h"}
    process.restore_host_env(env)
    assert env == {"HOME": "/h"}


def test_restore_host_env_drops_bundle_qt_vars() -> None:
    env = {
        "QT_PLUGIN_PATH": "/_MEI/qt/plugins",
        "QML2_IMPORT_PATH": "/_MEI/qml",
        "QT_QPA_PLATFORM_PLUGIN_PATH": "/_MEI/qt/plugins/platforms",
        "HOME": "/h",
    }
    process.restore_host_env(env)
    assert env == {"HOME": "/h"}


def test_build_env_does_not_invent_xdg_runtime_dir(monkeypatch, tmp_path) -> None:
    monkeypatch.setattr(process.os, "getuid", lambda: 4242424)
    env = process.build_env(base={})
    assert "XDG_RUNTIME_DIR" not in env
    assert process.build_env(base={"XDG_RUNTIME_DIR": "/x"})["XDG_RUNTIME_DIR"] == "/x"


# ---------------------------------------------------------------------------
# game/Proton child environment (#125, #13)
# ---------------------------------------------------------------------------
class TestGameChildEnv:
    @staticmethod
    def _fake_system_python(monkeypatch, present: bool = True):
        monkeypatch.setattr(process, "_system_python_exists", lambda: present)

    def test_python_launcher_variables_are_dropped(self, monkeypatch):
        self._fake_system_python(monkeypatch)
        env = {
            "PYTHONPATH": "/src",
            "PYTHONHOME": "/opt/py",
            "PYTHONSTARTUP": "/x.py",
            "VIRTUAL_ENV": "/venv",
            "PIP_REQUIRE_VIRTUALENV": "1",
            "PYTHONUNBUFFERED": "1",
            "DISPLAY": ":0",
            "WAYLAND_DISPLAY": "wayland-0",
            "PATH": "/usr/bin",
        }
        out = process.sanitize_game_env(env)
        for gone in (
            "PYTHONPATH",
            "PYTHONHOME",
            "PYTHONSTARTUP",
            "VIRTUAL_ENV",
            "PIP_REQUIRE_VIRTUALENV",
        ):
            assert gone not in out
        assert out["DISPLAY"] == ":0" and out["WAYLAND_DISPLAY"] == "wayland-0"
        assert out["PYTHONUNBUFFERED"] == "1"  # unrelated variables are untouched

    def test_version_manager_shims_leave_the_path(self, monkeypatch):
        self._fake_system_python(monkeypatch)
        env = {
            "PATH": "/home/u/.pyenv/shims:/home/u/.asdf/shims:/home/u/.local/share/mise/shims"
            ":/home/u/bin:/usr/local/bin:/usr/bin:/bin"
        }
        assert process.sanitize_game_env(env)["PATH"] == "/home/u/bin:/usr/local/bin:/usr/bin:/bin"

    def test_active_virtualenv_bin_leaves_the_path(self, monkeypatch):
        self._fake_system_python(monkeypatch)
        env = {"VIRTUAL_ENV": "/home/u/venv", "PATH": "/home/u/venv/bin:/usr/bin:/bin"}
        out = process.sanitize_game_env(env)
        assert out["PATH"] == "/usr/bin:/bin"
        assert "VIRTUAL_ENV" not in out

    def test_system_dirs_are_added_when_missing(self, monkeypatch):
        self._fake_system_python(monkeypatch)
        out = process.sanitize_game_env({"PATH": "/home/u/.pyenv/shims"})
        assert out["PATH"].split(":") == ["/usr/bin", "/bin"]

    def test_nothing_is_stripped_without_a_system_python(self, monkeypatch):
        """Never remove the only python3 the child could find."""
        self._fake_system_python(monkeypatch, present=False)
        env = {"PATH": "/home/u/.pyenv/shims:/usr/bin", "VIRTUAL_ENV": "/v"}
        out = process.sanitize_game_env(env)
        assert out["PATH"] == "/home/u/.pyenv/shims:/usr/bin"

    def test_input_is_not_mutated(self, monkeypatch):
        self._fake_system_python(monkeypatch)
        env = {"PYTHONPATH": "/src", "PATH": "/usr/bin"}
        process.sanitize_game_env(env)
        assert env == {"PYTHONPATH": "/src", "PATH": "/usr/bin"}
