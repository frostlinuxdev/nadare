# SPDX-License-Identifier: GPL-3.0-or-later
"""Executable characterization of the CLI/Qt boundary, before sharing policy (#1456)."""

from argparse import Namespace
from contextlib import nullcontext

import pytest

from nadare import app
from nadare.core import diskspace, process, vcredist
from nadare.core.backends import BackendError
from nadare.core.config import Config
from nadare.core.events import Cancel, Cancelled
from nadare.gui import workers
from nadare.gui.main_window_parts.confirm import NOTICE_OPERATIONS


@pytest.fixture
def scenario(tmp_path, monkeypatch):
    config = Config.load(tmp_path / "config.json")
    for name in ("a", "b"):
        config.add_profile(name, backend="wine", install_dir=str(tmp_path / "shared"))
    trace = []

    class Backend:
        install_size_hint = 100
        account_notice = "account notice"
        supports_predownload = True

        def requires_notice_ack(self):
            return False

        def install_dir(self):
            return tmp_path / "shared"

        def install(self):
            trace.append(("backend", process.operation_in_progress("a")))
            if outcome == "error":
                raise BackendError("fixture failure")
            if outcome == "cancel":
                raise Cancelled()

        repair = install
        update = install
        remake_prefix = install
        predownload = install

    outcome = "success"
    backend = Backend()
    monkeypatch.setattr(app, "create_backend", lambda *a, **k: backend)
    monkeypatch.setattr(workers, "create", lambda *a, **k: backend)
    monkeypatch.setattr(diskspace, "install_space", lambda b: trace.append("check"))
    monkeypatch.setattr(diskspace, "tree_bytes", lambda p: 25)

    class Reservation:
        def release(self):
            trace.append("release")

    def reserve(path, needed, *, owner):
        trace.append(("reserve", needed, owner))
        return Reservation()

    monkeypatch.setattr(diskspace, "reserve", reserve)
    monkeypatch.setattr(vcredist, "retry_after_repair", lambda b: trace.append("retry") or True)

    def run(adapter, operation, ending="success"):
        nonlocal outcome
        outcome = ending
        if adapter == "cli":
            args = Namespace(command=operation.replace("_", "-"), yes=True)
            try:
                with app._operation_lock(args.command, "a"):
                    return app._run_backend_command(args, config, "a", Cancel(), lambda e: None)
            except (BackendError, Cancelled) as exc:
                return type(exc).__name__
        worker = workers.BackendWorker(config, "a", operation)
        result = []
        worker.succeeded.connect(lambda code: result.append(code))
        worker.failed.connect(lambda message: result.append("BackendError"))
        worker.cancelled.connect(lambda: result.append("Cancelled"))
        worker.run()
        return result[0]

    return config, backend, trace, run


@pytest.mark.parametrize("adapter", ["cli", "gui"])
@pytest.mark.parametrize("operation", ["install", "repair"])
@pytest.mark.parametrize("ending", ["success", "error", "cancel"])
def test_install_repair_lifecycle(scenario, adapter, operation, ending):
    _, _, trace, run = scenario
    assert (
        run(adapter, operation, ending)
        == {"success": 0, "error": "BackendError", "cancel": "Cancelled"}[ending]
    )
    preflight = (
        ["retry"]
        if operation == "repair"
        else (["check"] if adapter == "cli" else [("reserve", 75, "a")])
    )
    assert trace == preflight + [("backend", True)] + (
        ["release"] if operation == "install" and adapter == "gui" else []
    )
    assert not process.operation_in_progress("a")
    with process.OperationLock("a"):
        pass


@pytest.mark.parametrize("adapter", ["cli", "gui"])
def test_two_profiles_sharing_an_installation_are_not_serialized(scenario, adapter):
    config, _, trace, run = scenario
    assert config.profile("a")["install_dir"] == config.profile("b")["install_dir"]
    with process.OperationLock("b"):
        assert run(adapter, "repair") == 0
    assert trace == ["retry", ("backend", True)]


@pytest.mark.parametrize("adapter", ["cli", "gui"])
@pytest.mark.parametrize("predownload", [False, True])
def test_play_checks_but_never_holds_a_lock(scenario, adapter, predownload):
    from nadare.core import launchgate

    _, _, _, _run = scenario
    held = []
    # Isolate the already shared Play guards; this test concerns resource identity only.
    with pytest.MonkeyPatch.context() as patch:
        patch.setattr(
            launchgate,
            "play",
            lambda b, **_kw: held.append(process.operation_in_progress("a")) or 7,
        )
        context = app._operation_lock("predownload", "a") if predownload else nullcontext()
        with context:
            assert _run(adapter, "launch") == 7
    assert held == [predownload]


def test_today_policy_sets_and_notice_operations():
    assert workers.LOCKED_OPERATIONS == {
        "install",
        "update",
        "repair",
        "remake_prefix",
        "remove_qualities",
    }
    assert workers.PAUSABLE == workers.CAPPED == {"install", "update", "repair"}
    assert workers.MUTATING == workers.LOCKED_OPERATIONS | {
        "login",
        "launch",
        "set_remember",
        "logout",
    }
    # The GUI now also asks before Repair and Rebuild prefix, as the CLI does (#1567).
    assert NOTICE_OPERATIONS == {"install", "update", "repair", "remake_prefix", "launch"}


@pytest.mark.parametrize("operation", ["install", "update", "repair", "remake_prefix", "launch"])
def test_cli_account_notice_coverage(scenario, operation, monkeypatch, capsys):
    _, backend, trace, run = scenario
    monkeypatch.setattr(backend, "requires_notice_ack", lambda: True)
    assert run("cli", operation) == 2
    assert not trace
    output = capsys.readouterr()
    assert output.out == "account notice\n"
    assert "--accept-account-notice" in output.err


def test_shared_descriptors_cover_every_worker_operation():
    from nadare.core import operation_policy as policy

    assert tuple(policy.POLICIES) == workers.OPERATIONS
    assert policy.operations_with("locked") == workers.LOCKED_OPERATIONS
    assert policy.operations_with("pausable") == workers.PAUSABLE
    assert policy.operations_with("capped") == workers.CAPPED
    assert policy.operations_with("mutating") == workers.MUTATING
    assert policy.operations_with("gui_notice") == NOTICE_OPERATIONS
    # The GUI is the reference (#1567): both ask before the same operations.
    assert policy.operations_with("cli_notice") == NOTICE_OPERATIONS
    assert NOTICE_OPERATIONS == {"install", "update", "repair", "remake_prefix", "launch"}
    assert policy.POLICIES["install"].prerequisites == ("install_space",)
    assert policy.POLICIES["repair"].prerequisites == ("runtime_retry",)
    assert policy.POLICIES["launch"].prerequisites == ("launchgate",)
    assert policy.POLICIES["predownload"].resources == ("operation", "predownload")
    assert policy.POLICIES["launch"].resources == ()
    for operation in workers.LOCKED_OPERATIONS | {"predownload"}:
        assert policy.POLICIES[operation].lock_scope == "profile"


@pytest.mark.parametrize("adapter", ["cli", "gui"])
@pytest.mark.parametrize("operation", ["install", "repair"])
def test_adapters_enter_shared_preflight(scenario, monkeypatch, adapter, operation):
    from nadare.core import operation_policy as policy

    _, _, _, run = scenario
    seen = []
    original = policy.preflight

    def preflight(op, backend, **kwargs):
        seen.append((op, kwargs["adapter"]))
        return original(op, backend, **kwargs)

    monkeypatch.setattr(policy, "preflight", preflight)
    assert run(adapter, operation) == 0
    assert seen == [(operation, adapter)]


@pytest.mark.parametrize("adapter", ["cli", "gui"])
def test_second_predownload_lock_failure_releases_first(scenario, adapter):
    _, _, trace, run = scenario
    with process.PredownloadLock("a"):
        assert run(adapter, "predownload") == "BackendError"
        assert not process.operation_in_progress("a")
    assert trace == []


@pytest.mark.parametrize("adapter", ["cli", "gui"])
@pytest.mark.parametrize("advertised", [False, True])
def test_predownload_capability_difference(scenario, adapter, advertised):
    _, backend, trace, run = scenario
    backend.supports_predownload = advertised
    assert run(adapter, "predownload") == (
        "BackendError" if adapter == "gui" and not advertised else 0
    )
    assert trace == ([] if adapter == "gui" and not advertised else [("backend", True)])
    assert not process.operation_in_progress("a")
    with process.PredownloadLock("a"):
        pass


@pytest.mark.parametrize("adapter", ["cli", "gui"])
def test_repair_backend_error_preflight_difference(scenario, monkeypatch, adapter):
    _, _, trace, run = scenario

    def fail(backend):
        raise BackendError("prefix unavailable")

    monkeypatch.setattr(vcredist, "retry_after_repair", fail)
    assert run(adapter, "repair") == (0 if adapter == "cli" else "BackendError")
    assert trace == ([("backend", True)] if adapter == "cli" else [])
    assert not process.operation_in_progress("a")


def test_gui_lifecycle_sets_match_characterized_table():
    from nadare.gui.game_view_parts import busy
    from nadare.gui.main_window_parts import operations

    assert (
        operations.SILENT_OPERATIONS
        == workers.BACKGROUND
        == {"status", "news", "media", "check_update"}
    )
    assert operations.UNCANCELLABLE_OPERATIONS == {"verify", "remove_qualities"}
    assert operations.REPROBE_OPERATIONS == {
        "install",
        "update",
        "launch",
        "verify",
        "remove_qualities",
    }
    assert busy._STATE_CHANGING == {"install", "update", "repair", "remake_prefix", "uninstall"}
    assert busy._ANNOUNCED_ON_SUCCESS == {"install", "update", "repair", "verify", "remake_prefix"}
    assert busy._RETRYABLE == {
        "install",
        "update",
        "repair",
        "remake_prefix",
        "verify",
        "login",
        "launch",
    }


def test_running_and_resume_are_explicit_in_descriptors():
    from nadare.core import operation_policy as policy

    for operation, descriptor in policy.POLICIES.items():
        assert descriptor.running == (
            "allowed" if operation in workers.BACKGROUND | {"stop", "predownload"} else "backend"
        )
        assert descriptor.resume == (
            "supports_resume"
            if operation in workers.PAUSABLE
            else "backend_cache"
            if operation == "predownload"
            else "none"
        )


@pytest.mark.parametrize("operation", ["install", "repair"])
@pytest.mark.parametrize("ending,expected", [("success", 0), ("error", 1), ("cancel", 130)])
def test_cli_entrypoint_keeps_exit_codes(scenario, operation, ending, expected, monkeypatch):
    config, backend, _, _ = scenario
    config.save()
    if ending != "success":

        def fail():
            if ending == "cancel":
                raise Cancelled()
            raise BackendError("fixture failure")

        monkeypatch.setattr(backend, operation, fail)
    assert app.run_cli(["--config", str(config.path), operation, "-p", "a"]) == expected
    assert not process.operation_in_progress("a")


@pytest.mark.parametrize("adapter", ["cli", "gui"])
def test_install_preflight_refusal_releases_resources(scenario, monkeypatch, adapter):
    _, _, trace, run = scenario

    def fail(*args, **kwargs):
        raise BackendError("preflight refusal", code=4)

    monkeypatch.setattr(diskspace, "install_space", fail)
    monkeypatch.setattr(diskspace, "reserve", fail)
    assert run(adapter, "install") == "BackendError"
    assert trace == []
    assert not process.operation_in_progress("a")


@pytest.mark.parametrize("adapter", ["cli", "gui"])
@pytest.mark.parametrize("operation", ["update", "remake_prefix", "predownload"])
@pytest.mark.parametrize("ending", ["success", "error", "cancel"])
def test_other_shared_lock_lifetimes(scenario, adapter, operation, ending):
    _, _, trace, run = scenario
    assert (
        run(adapter, operation, ending)
        == {"success": 0, "error": "BackendError", "cancel": "Cancelled"}[ending]
    )
    assert trace == [("backend", True)]
    assert not process.operation_in_progress("a")
    assert not process.predownload_in_progress("a")


def test_a_shortcut_launch_is_refused_while_an_install_holds_the_lock():
    """``nadare launch -p`` (every desktop/Steam shortcut) must not start a game mid-update."""
    import pytest as _pytest

    from nadare.core import operation_policy, process
    from nadare.core.backends import BackendError

    lock = process.OperationLock("shortcut-game")
    lock.acquire()
    try:
        with _pytest.raises(BackendError):
            with operation_policy.operation_lock("launch", "shortcut-game"):
                _pytest.fail("launch ran while an install held the lock")
    finally:
        lock.release()
    # Once the install is done, the same launch goes through.
    with operation_policy.operation_lock("launch", "shortcut-game"):
        pass


def test_a_predownload_alone_does_not_block_a_shortcut_launch():
    from nadare.core import operation_policy, process

    lock = process.PredownloadLock("shortcut-game")
    lock.acquire()
    try:
        with operation_policy.operation_lock("launch", "shortcut-game"):
            pass
    finally:
        lock.release()
