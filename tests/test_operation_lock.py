# SPDX-License-Identifier: GPL-3.0-or-later
"""Mutating operations and Remove exclude each other per profile (#581, #613)."""

from __future__ import annotations

import pytest

from nadare import app
from nadare.core import process, removal
from nadare.core.backends import BackendError
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


def test_operation_lock_does_not_clash_with_the_login_lock(env):
    with process.ProfileLock("g"):
        with process.OperationLock("g"):
            pass


def test_operation_lock_is_exclusive(env):
    with process.OperationLock("g"):
        with pytest.raises(process.ProfileLockError):
            process.OperationLock("g").acquire()
    with process.OperationLock("g"):
        pass


def test_remove_game_refuses_while_an_operation_runs(env):
    cfg, tmp = env
    prefix = tmp / "prefix"
    prefix.mkdir()
    cfg.profile("g")["prefix"] = str(prefix)
    cfg.profile("g")["owned_trees"] = ["prefix"]
    with process.OperationLock("g"):
        with pytest.raises(BackendError, match="Another Nadare"):
            removal.remove_game(cfg, "g", delete_trees=["prefix"], applications_dir=tmp / "apps")
    assert "g" in cfg.profiles and prefix.exists()


def test_cli_install_refuses_while_another_process_holds_the_lock(env, monkeypatch, capsys):
    cfg, tmp = env
    ran = []

    class Fake:
        def requires_notice_ack(self):
            return False

        def install(self):
            ran.append("install")

    monkeypatch.setattr(app, "create_backend", lambda *a, **k: Fake())
    with process.OperationLock("g"):
        code = app.run_cli(["--config", str(tmp / "config.json"), "install", "-p", "g"])
    assert code != 0 and ran == []
    assert "Another Nadare" in capsys.readouterr().err
    assert app.run_cli(["--config", str(tmp / "config.json"), "install", "-p", "g"]) == 0
    assert ran == ["install"]


def test_worker_refuses_a_locked_profile_and_releases_the_lock(env, monkeypatch):
    from nadare.gui import workers

    cfg, _ = env
    ran = []

    class Fake:
        def install(self):
            ran.append("install")

    monkeypatch.setattr(workers, "create", lambda *a, **k: Fake())
    failures = []

    def run_worker():
        worker = workers.BackendWorker(cfg, "g", "install")
        worker.failed.connect(failures.append)
        worker.run()  # synchronous: same code path as the thread
        return worker

    with process.OperationLock("g"):
        run_worker()
    assert ran == [] and failures and "Another Nadare" in failures[0]
    run_worker()
    assert ran == ["install"]  # the lock was free again, and released afterwards
    with process.OperationLock("g"):
        pass
