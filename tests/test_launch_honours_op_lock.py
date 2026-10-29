# SPDX-License-Identifier: GPL-3.0-or-later
"""Launch refuses to start while another operation holds the per-profile lock."""

from __future__ import annotations

import pytest

from nadare import app
from nadare.core import process
from nadare.core.config import Config


@pytest.fixture
def env(tmp_path, monkeypatch):
    monkeypatch.setenv("XDG_DATA_HOME", str(tmp_path / "data"))
    monkeypatch.setenv("XDG_CONFIG_HOME", str(tmp_path / "config"))
    monkeypatch.setenv("XDG_STATE_HOME", str(tmp_path / "state"))
    monkeypatch.setenv("XDG_CACHE_HOME", str(tmp_path / "cache"))
    cfg = Config.load(tmp_path / "config.json")
    cfg.add_profile("g", backend="wine", title="G")
    cfg.save()
    return cfg, tmp_path


class FakeLaunch:
    def __init__(self, ran: list[str]) -> None:
        self.ran = ran
        self.lock_held_during_launch: list[bool] = []

    def requires_notice_ack(self) -> bool:
        return False

    def launch(self) -> int:
        self.ran.append("launch")
        self.lock_held_during_launch.append(process.operation_in_progress("g"))
        return 0


def test_operation_in_progress_sees_a_held_lock_and_leaves_it_alone(env):
    with process.OperationLock("g"):
        assert process.operation_in_progress("g") is True
        assert process.operation_in_progress("g") is True
        with pytest.raises(process.ProfileLockError):
            process.OperationLock("g").acquire()
    assert process.operation_in_progress("g") is False
    with process.OperationLock("g"):
        pass


def test_operation_in_progress_is_false_without_a_lock_file(env):
    assert process.operation_in_progress("never-used") is False


def test_cli_launch_refuses_while_an_operation_holds_the_lock(env, monkeypatch, capsys):
    cfg, tmp = env
    ran: list[str] = []
    backend = FakeLaunch(ran)
    monkeypatch.setattr(app, "create_backend", lambda *a, **k: backend)
    argv = ["--config", str(tmp / "config.json"), "launch", "-p", "g"]

    with process.OperationLock("g"):
        code = app.run_cli(argv)
        assert process.operation_in_progress("g") is True  # the refusal released nothing

    assert code != 0 and ran == []
    err = capsys.readouterr().err
    assert "An install/update/repair for g is in progress" in err
    assert "wait for it to finish, then launch again" in err
    assert app.run_cli(argv) == 0
    assert ran == ["launch"]
    assert backend.lock_held_during_launch == [False]  # launch itself holds no lock


def test_worker_refuses_to_launch_a_locked_profile(env, monkeypatch):
    from nadare.gui import workers

    cfg, _ = env
    ran: list[str] = []
    backend = FakeLaunch(ran)
    monkeypatch.setattr(workers, "create", lambda *a, **k: backend)
    failures: list[str] = []
    successes: list[int] = []

    def run_worker() -> None:
        worker = workers.BackendWorker(cfg, "g", "launch")
        worker.failed.connect(failures.append)
        worker.succeeded.connect(successes.append)
        worker.run()  # synchronous: same code path as the thread

    with process.OperationLock("g"):
        run_worker()
        assert process.operation_in_progress("g") is True  # the refusal released nothing

    assert ran == [] and failures
    assert "An install/update/repair for g is in progress" in failures[0]
    run_worker()
    assert ran == ["launch"] and successes == [0]
    assert backend.lock_held_during_launch == [False]  # launch itself holds no lock
    with process.OperationLock("g"):
        pass


def test_a_broken_locking_filesystem_does_not_block_launch_forever(monkeypatch, tmp_path):
    """ENOLCK/ENOTSUP from flock mean 'cannot tell', not 'an install is running'."""
    import errno
    import fcntl

    from nadare.core import process

    process.OperationLock("odd").acquire()  # creates the lock file, then drop it again
    lock_path = process.OperationLock("odd").path
    assert lock_path.exists()

    def broken(fd, op):
        raise OSError(errno.ENOLCK, "No locks available")

    monkeypatch.setattr(fcntl, "flock", broken)
    assert process.operation_in_progress("odd") is False
