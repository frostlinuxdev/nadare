# SPDX-License-Identifier: GPL-3.0-or-later
# Copyright (C) 2026 Nadare contributors
"""Operation state and replay, independent of widgets and scheduling.

The Qt adapter passes controller-validated inputs. Non-controller producers can carry
RunToken on completion to reject a superseded run or a mismatched profile explicitly.
Status/news/media caches remain separate from the operation lifecycle.
"""
from __future__ import annotations

from collections import deque
from collections.abc import Mapping
from dataclasses import dataclass
from types import MappingProxyType
from typing import Any

from ..core.events import Event
from ..core.operation_policy import operations_with

REPLAY_EVENTS = 400
_BACKGROUND = operations_with("background")


def _freeze(value: Any) -> Any:
    if isinstance(value, Mapping):
        return MappingProxyType({key: _freeze(item) for key, item in value.items()})
    if isinstance(value, (list, tuple)):
        return tuple(_freeze(item) for item in value)
    if isinstance(value, (set, frozenset)):
        return frozenset(_freeze(item) for item in value)
    return value


@dataclass(frozen=True)
class ReplayEvent:
    """An immutable copy of the backend's mutable event payload."""

    kind: str
    message: str
    progress: float | None
    data: Mapping[str, Any]


@dataclass(frozen=True)
class RunToken:
    profile: str
    operation: str
    serial: int


@dataclass(frozen=True)
class OperationView:
    profile: str
    busy: str | None
    queued: bool
    paused: str | None
    outcome: str | None
    failure: str | None
    events: tuple[ReplayEvent, ...]


class OperationPresentation:
    """Own per-profile operation state; never select a widget or schedule work.

    ``busy`` also serves legacy workflow adapters (notably removal).
    Rendering uses detached, immutable ``view`` values instead of those containers.
    A controller busy(False) precedes its terminal signal, so it keeps the run token.
    """

    def __init__(self) -> None:
        self.busy: dict[str, str] = {}
        self._waiting: set[str] = set()
        self._paused: dict[str, str] = {}
        self._outcomes: dict[str, str] = {}
        self._failures: dict[str, str] = {}
        self._events: dict[str, deque[ReplayEvent]] = {}
        self._runs: dict[str, RunToken] = {}
        self._serial = 0

    def busy_changed(self, profile: str, operation: str, active: bool) -> RunToken | None:
        if operation in _BACKGROUND:
            return None
        self._waiting.discard(profile)
        if active:
            self._serial += 1
            self._runs[profile] = RunToken(profile, operation, self._serial)
            self.busy[profile] = operation
            self._paused.pop(profile, None)
            self._outcomes.pop(profile, None)
            self._failures.pop(profile, None)
            self._events.pop(profile, None)
        else:
            self.busy.pop(profile, None)
        return self._runs.get(profile)

    def queued(self, profile: str, operation: str) -> None:
        self._waiting.add(profile)

    def paused(self, profile: str, operation: str) -> None:
        self.busy.pop(profile, None)
        self._waiting.discard(profile)
        self._paused[profile] = operation

    def sync_paused(self, profile: str, operation: str) -> None:
        """Reconcile scheduler state after shutdown, which silently drops paused jobs."""
        if operation:
            self._paused[profile] = operation
        else:
            self._paused.pop(profile, None)

    def event(self, profile: str, event: Event) -> None:
        if profile in self.busy:
            self._events.setdefault(profile, deque(maxlen=REPLAY_EVENTS)).append(
                ReplayEvent(event.kind, event.message, event.progress, _freeze(event.data))
            )

    def complete(
        self, profile: str, operation: str, outcome: str, *, message: str = "",
        token: RunToken | None = None,
    ) -> bool:
        """Accept a controller terminal signal, or validate a producer's run token."""
        if token is not None and (
            token.profile != profile or token.operation != operation
            or self._runs.get(profile) != token
        ):
            return False
        if operation in _BACKGROUND:
            return True
        self.busy.pop(profile, None)
        self._waiting.discard(profile)
        self._paused.pop(profile, None)
        self._outcomes[profile] = outcome
        if outcome == "failed":
            self._failures[profile] = message
        else:
            self._failures.pop(profile, None)
        return True

    def forget(self, profile: str) -> None:
        self.busy.pop(profile, None)
        self._waiting.discard(profile)
        for values in (self._paused, self._outcomes, self._failures, self._events, self._runs):
            values.pop(profile, None)

    def view(self, profile: str) -> OperationView:
        return OperationView(
            profile, self.busy.get(profile), profile in self._waiting,
            self._paused.get(profile), self._outcomes.get(profile), self._failures.get(profile),
            tuple(self._events.get(profile, ())),
        )
