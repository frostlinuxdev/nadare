# SPDX-License-Identifier: GPL-3.0-or-later
"""#1455 R4-4 / #1304: one rule for "is the game running", used everywhere.

Remove, Move and the prefix tools looked only at ``state == "running"`` while the game page
also looked at ``Status.running``; a backend that set only the flag would have let Remove
delete a running game's files. Now every caller asks :func:`gamefacts.is_running`.
"""

from __future__ import annotations

import ast
from pathlib import Path
from types import SimpleNamespace

import pytest

from nadare.core import gamefacts
from nadare.core.backends import Status

SRC = Path(__file__).resolve().parents[1] / "src" / "nadare"


@pytest.mark.parametrize(
    "status,expected",
    [
        (None, False),
        (Status(state="installed"), False),
        (Status(state="running"), True),
        (Status(state="installed", running=True), True),
        (Status(state="Running "), True),
        (SimpleNamespace(state="running"), True),  # duck-typed results from workers
        (SimpleNamespace(running=True), True),
        (SimpleNamespace(), False),
        (SimpleNamespace(state=None, running=None), False),
    ],
)
def test_is_running(status, expected):
    assert gamefacts.is_running(status) is expected


def test_remove_refuses_a_game_running_by_flag_only(monkeypatch):
    from nadare.core import removal

    class Backend:
        def status(self):
            return Status(state="installed", running=True)

    monkeypatch.setattr(removal, "create_backend", lambda *a, **k: Backend(), raising=False)
    problem = removal._running_problem_for(Backend())
    assert problem and "running" in problem


def _compares_state_to_running(node: ast.AST) -> bool:
    if not isinstance(node, ast.Compare):
        return False
    parts = [node.left, *node.comparators]
    has_running = any(isinstance(p, ast.Constant) and p.value == "running" for p in parts)
    has_state = any(
        (isinstance(p, ast.Attribute) and p.attr == "state")
        or (isinstance(p, ast.Name) and p.id == "state")
        for p in parts
    )
    return has_running and has_state


def test_nobody_writes_their_own_running_check():
    allowed = {
        SRC / "core" / "gamefacts.py",
        # a download row's own state ("this download is running"), not a game's status
        SRC / "gui" / "downloads_page.py",
    }
    offenders = []
    for path in SRC.rglob("*.py"):
        if path in allowed or "fgo" in path.parts:
            continue
        tree = ast.parse(path.read_text(encoding="utf-8"))
        for node in ast.walk(tree):
            if _compares_state_to_running(node):
                offenders.append(f"{path.relative_to(SRC)}:{node.lineno}")
    assert not offenders, f"use gamefacts.is_running: {offenders}"


def test_wine_backends_share_one_process_check():
    from nadare.backends.direct import DirectBackend
    from nadare.backends.winegame import WineGameBackend

    assert "_game_running" in vars(WineGameBackend)
    assert "_game_running" not in vars(DirectBackend)
