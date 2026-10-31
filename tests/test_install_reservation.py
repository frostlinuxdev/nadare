# SPDX-License-Identifier: GPL-3.0-or-later
"""#770: the install worker reserves its space for the length of the install."""

from __future__ import annotations

import os
from pathlib import Path

os.environ.setdefault("QT_QPA_PLATFORM", "offscreen")

import pytest

try:
    from PyQt6.QtWidgets import QApplication  # noqa: F401
except Exception:  # pragma: no cover - optional GUI dependencies
    pytest.skip("PyQt6 is not importable in this interpreter", allow_module_level=True)

from nadare.core import diskspace
from nadare.core.backends import BackendError
from nadare.core.config import Config
from nadare.gui import workers

GB = 1024**3


class FakeBackend:
    install_size_hint = 60 * GB

    def __init__(self, target: Path, on_install=None) -> None:
        self._target = target
        self._on_install = on_install

    def install_dir(self) -> Path:
        return self._target

    def install(self) -> None:
        if self._on_install:
            self._on_install()


@pytest.fixture
def disk(monkeypatch):
    state = {"free": 100 * GB}
    monkeypatch.setattr(diskspace, "free_bytes", lambda path: state["free"])
    monkeypatch.setattr(diskspace, "_device", lambda path: 1)
    diskspace._PROMISES.clear()
    yield state
    diskspace._PROMISES.clear()


def _run(tmp_path, monkeypatch, name, backend, operation="install"):
    config = Config.load(tmp_path / "config.json")
    config.add_profile(name, backend="wine", title=name)
    monkeypatch.setattr(workers, "create", lambda *a, **k: backend)
    worker = workers.BackendWorker(config, name, operation)
    outcome: list[tuple] = []
    worker.succeeded.connect(lambda code: outcome.append(("ok", code)))
    worker.failed.connect(lambda message: outcome.append(("failed", message)))
    worker.run()  # in this thread: the signals are direct
    return outcome


def test_the_install_holds_its_reservation_until_it_ends(disk, tmp_path, monkeypatch):
    seen: list[int] = []
    backend = FakeBackend(
        tmp_path / "game", on_install=lambda: seen.append(len(diskspace._PROMISES))
    )
    outcome = _run(tmp_path, monkeypatch, "a", backend)
    assert outcome == [("ok", 0)]
    assert seen == [1]  # held while installing
    assert not diskspace._PROMISES  # released afterwards


def test_a_second_install_that_cannot_fit_beside_a_running_one_fails_clearly(
    disk, tmp_path, monkeypatch
):
    outcome: list[tuple] = []

    def second_install_starts() -> None:
        other = FakeBackend(tmp_path / "other")
        outcome.extend(_run(tmp_path, monkeypatch, "b", other))

    first = FakeBackend(tmp_path / "game", on_install=second_install_starts)
    first_outcome = _run(tmp_path, monkeypatch, "a", first)
    assert first_outcome == [("ok", 0)]
    assert outcome and outcome[0][0] == "failed"
    assert "both" in outcome[0][1]


def test_the_reservation_is_released_when_the_install_fails(disk, tmp_path, monkeypatch):
    def boom() -> None:
        raise BackendError("download failed")

    outcome = _run(tmp_path, monkeypatch, "a", FakeBackend(tmp_path / "game", on_install=boom))
    assert outcome[0][0] == "failed"
    assert not diskspace._PROMISES


def test_a_resumed_install_only_reserves_what_is_still_missing(disk, tmp_path, monkeypatch):
    game = tmp_path / "game"
    game.mkdir()
    (game / "big.bin").write_bytes(b"x" * 1024)
    amounts: list[int] = []
    backend = FakeBackend(
        game,
        on_install=lambda: amounts.extend(p.amount for p in diskspace._PROMISES.values()),
    )
    backend.install_size_hint = 1024 + 4096
    _run(tmp_path, monkeypatch, "a", backend)
    assert amounts == [4096]


@pytest.mark.parametrize("operation", ["update", "repair"])
def test_updates_and_repairs_do_not_reserve(disk, tmp_path, monkeypatch, operation):
    seen: list[int] = []
    backend = FakeBackend(
        tmp_path / "game", on_install=lambda: seen.append(len(diskspace._PROMISES))
    )
    backend.update = backend.install
    backend.repair = backend.install
    _run(tmp_path, monkeypatch, "a", backend, operation)
    assert seen == [0]


def test_a_backend_without_a_size_or_folder_just_installs(disk, tmp_path, monkeypatch):
    class Plain:
        def install(self) -> None:
            pass

    assert _run(tmp_path, monkeypatch, "a", Plain()) == [("ok", 0)]
