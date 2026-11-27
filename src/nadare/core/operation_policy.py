# SPDX-License-Identifier: GPL-3.0-or-later
# Copyright (C) 2026 Nadare contributors
"""Operation metadata and resource lifetime shared by CLI and Qt adapters.

Dispatch, output, confirmations and queue scheduling belong to the adapters. See
``docs/operation-policy.md`` for the characterized differences retained here.
"""

from __future__ import annotations

from contextlib import ExitStack, contextmanager
from dataclasses import dataclass
from pathlib import Path

from . import diskspace, execmount, process, vcredist
from .backends import BackendError


@dataclass(frozen=True)
class OperationPolicy:
    """Facts about one operation, independent of a Qt worker or a CLI command."""

    resources: tuple[str, ...] = ()
    running: str = "backend"
    resume: str = "none"
    mutating: bool = False
    pausable: bool = False
    capped: bool = False
    background: bool = False
    cancellable: bool = True
    prerequisites: tuple[str, ...] = ()
    cli_notice: bool = False
    gui_notice: bool = False
    wait_for_paused: bool = False
    reprobe: bool = False
    state_changing: bool = False
    announce_success: bool = False
    retryable: bool = False

    @property
    def locked(self) -> bool:
        """Whether the ordinary installation operation lock is held."""
        return self.resources == ("operation",)

    @property
    def lock_scope(self) -> str:
        """Locks remain keyed by profile, even for profiles sharing a folder."""
        return "profile" if self.resources else "none"


POLICIES = {
    "status": OperationPolicy(background=True, running="allowed"),
    "install": OperationPolicy(
        ("operation",),
        mutating=True,
        pausable=True,
        capped=True,
        resume="supports_resume",
        prerequisites=("install_space",),
        cli_notice=True,
        gui_notice=True,
        reprobe=True,
        state_changing=True,
        announce_success=True,
        retryable=True,
    ),
    "update": OperationPolicy(
        ("operation",),
        mutating=True,
        pausable=True,
        capped=True,
        resume="supports_resume",
        cli_notice=True,
        gui_notice=True,
        wait_for_paused=True,
        reprobe=True,
        state_changing=True,
        announce_success=True,
        retryable=True,
    ),
    "repair": OperationPolicy(
        ("operation",),
        mutating=True,
        pausable=True,
        capped=True,
        resume="supports_resume",
        prerequisites=("runtime_retry",),
        cli_notice=True,
        gui_notice=True,
        wait_for_paused=True,
        state_changing=True,
        announce_success=True,
        retryable=True,
    ),
    "remake_prefix": OperationPolicy(
        ("operation",),
        mutating=True,
        cli_notice=True,
        gui_notice=True,
        state_changing=True,
        announce_success=True,
        retryable=True,
    ),
    "login": OperationPolicy(mutating=True, wait_for_paused=True, retryable=True),
    "launch": OperationPolicy(
        mutating=True,
        prerequisites=("launchgate",),
        cli_notice=True,
        gui_notice=True,
        reprobe=True,
        retryable=True,
    ),
    "stop": OperationPolicy(running="allowed"),
    "verify": OperationPolicy(
        cancellable=False,
        wait_for_paused=True,
        reprobe=True,
        announce_success=True,
        retryable=True,
    ),
    "set_remember": OperationPolicy(mutating=True, wait_for_paused=True),
    "logout": OperationPolicy(mutating=True, wait_for_paused=True),
    "news": OperationPolicy(background=True, running="allowed"),
    "media": OperationPolicy(background=True, running="allowed"),
    "check_update": OperationPolicy(background=True, running="allowed"),
    "predownload": OperationPolicy(
        ("operation", "predownload"),
        running="allowed",
        resume="backend_cache",
    ),
    "remove_qualities": OperationPolicy(
        ("operation",),
        mutating=True,
        cancellable=False,
        reprobe=True,
    ),
    # A declared overlay (#1866): the download can stop, the swap itself is journaled.
    "apply_patch": OperationPolicy(
        ("operation",),
        mutating=True,
        wait_for_paused=True,
        reprobe=True,
    ),
    "update_patch": OperationPolicy(
        ("operation",),
        mutating=True,
        wait_for_paused=True,
        reprobe=True,
    ),
    "remove_patch": OperationPolicy(
        ("operation",),
        mutating=True,
        cancellable=False,
        wait_for_paused=True,
        reprobe=True,
    ),
}


def operations_with(flag: str) -> frozenset[str]:
    """Select an adapter's lifecycle set from the shared descriptors."""
    return frozenset(op for op, policy in POLICIES.items() if getattr(policy, flag))


def policy_for(operation: str) -> OperationPolicy:
    """Accept the CLI's hyphenated spelling at the boundary."""
    return POLICIES[operation.replace("-", "_")]


@contextmanager
def operation_lock(operation: str, name: str):
    """Acquire profile resources and release them on every exit, including partial acquire."""
    policy = policy_for(operation)
    if operation == "launch" and process.launch_blocked(name):
        raise BackendError(process.OPERATION_IN_PROGRESS.format(name=name))
    with ExitStack() as resources:
        for resource in policy.resources:
            lock = (
                process.PredownloadLock(name)
                if resource == "predownload"
                else process.OperationLock(name)
            )
            try:
                lock.acquire()
            except process.ProfileLockError:
                raise BackendError(process.OPERATION_BUSY.format(name=name)) from None
            resources.callback(lock.release)
        yield


def reserve_install_space(backend: object, name: str) -> diskspace.Reservation | None:
    """Promise the missing install bytes; an unknown folder retains the old best effort rule."""
    execmount.guard_install(backend)  # games cannot run from a noexec drive (#1861)
    size = getattr(backend, "install_space_hint", None) or getattr(
        backend, "install_size_hint", None
    )
    folder = getattr(backend, "install_dir", None)
    if not size or not callable(folder):
        return None
    try:
        path = Path(folder())
    except Exception:  # noqa: BLE001 - a failed size estimate must not block an install
        return None
    needed = max(0, int(size) - diskspace.tree_bytes(path))
    return diskspace.reserve(path, needed, owner=name)


def retry_runtime(backend: object, *, adapter: str) -> bool:
    """Forget a remembered runtime failure; preserve each adapter's exception boundary."""
    try:
        return bool(vcredist.retry_after_repair(backend))
    except (AttributeError, OSError, RuntimeError, TypeError, ValueError):
        return False
    except BackendError:
        if adapter == "cli":
            return False
        raise


@contextmanager
def preflight(operation: str, backend: object, *, name: str, adapter: str, on_retry=None):
    """Hold install promises or perform repair preflight, without dispatching the operation.

    CLI checks the selected install size; Qt reserves concurrent download space (its size
    confirmation stays in the GUI). Play's prerequisites stay together in launchgate.play.
    """
    with ExitStack() as resources:
        prerequisites = policy_for(operation).prerequisites
        if "install_space" in prerequisites:
            if adapter == "cli":
                diskspace.install_space(backend)
            else:
                reservation = reserve_install_space(backend, name)
                if reservation is not None:
                    resources.callback(reservation.release)
        if "runtime_retry" in prerequisites:
            forgotten = retry_runtime(backend, adapter=adapter)
            if forgotten and on_retry is not None:
                on_retry()
        yield
