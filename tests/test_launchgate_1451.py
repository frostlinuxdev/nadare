# SPDX-License-Identifier: GPL-3.0-or-later
"""One way into Play: every pre-Play check runs in one place, for the GUI and the CLI (#1451).

The Flatpak graphics check and the NVIDIA restart check were each called twice (the GUI worker
and ``nadare launch``); a new check added to one caller only would let the other Play past it.
"""

from __future__ import annotations

import ast
from pathlib import Path

import pytest

from nadare.core import flatpakgl, launchgate, nvidiarestart
from nadare.core.backends import BackendError

SRC = Path(launchgate.__file__).resolve().parents[1]


class _Backend:
    kind = "direct-bd2"

    def __init__(self, log):
        self.log = log

    def launch(self):
        self.log.append("launch")
        return 7


def test_every_check_runs_before_the_game_in_order(monkeypatch):
    log: list[str] = []
    monkeypatch.setattr(flatpakgl, "guard_launch", lambda b: log.append("flatpakgl"))
    monkeypatch.setattr(nvidiarestart, "guard_launch", lambda b: log.append("nvidia"))
    assert launchgate.play(_Backend(log)) == 7
    assert log == ["flatpakgl", "nvidia", "launch"]


@pytest.mark.parametrize("module", [flatpakgl, nvidiarestart])
def test_a_refusing_check_stops_play(monkeypatch, module):
    log: list[str] = []
    monkeypatch.setattr(flatpakgl, "guard_launch", lambda b: None)
    monkeypatch.setattr(nvidiarestart, "guard_launch", lambda b: None)

    def refuse(backend):
        raise BackendError("no")

    monkeypatch.setattr(module, "guard_launch", refuse)
    with pytest.raises(BackendError):
        launchgate.play(_Backend(log))
    assert log == []


def _launch_calls(path: Path) -> list[int]:
    """Lines that call ``<something>.launch()`` with no arguments."""
    tree = ast.parse(path.read_text(encoding="utf-8"))
    return [
        node.lineno
        for node in ast.walk(tree)
        if isinstance(node, ast.Call)
        and isinstance(node.func, ast.Attribute)
        and node.func.attr == "launch"
        and not node.args
        and not node.keywords
        and not _is_super(node.func.value)
    ]


def _is_super(node: ast.expr) -> bool:
    return isinstance(node, ast.Call) and getattr(node.func, "id", "") == "super"


def test_nothing_outside_the_gate_starts_a_game():
    """Backends call their own ``launch`` (``super().launch()``); everyone else goes through
    :func:`launchgate.play`."""
    offenders = []
    for path in sorted(SRC.rglob("*.py")):
        rel = path.relative_to(SRC).as_posix()
        if rel.startswith("backends/") or rel == "core/launchgate.py":
            continue
        for line in _launch_calls(path):
            offenders.append(f"{rel}:{line}")
    assert offenders == []


@pytest.mark.parametrize("rel", ["gui/workers.py", "app.py"])
def test_both_callers_use_the_gate(rel):
    assert "launchgate.play(" in (SRC / rel).read_text(encoding="utf-8")
