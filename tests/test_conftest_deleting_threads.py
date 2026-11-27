# SPDX-License-Identifier: GPL-3.0-or-later
"""Every GUI thread that deletes files is waited for at the end of its test (see conftest)."""

from __future__ import annotations

import re
from pathlib import Path

import conftest

GUI = Path(__file__).resolve().parents[1] / "src" / "nadare" / "gui"
DELETES = re.compile(r"\b(sweep|delete|remove|rmtree|unlink|cleanup|repair)\b", re.I)


def test_every_deleting_thread_is_joined():
    names = set()
    for path in GUI.rglob("*.py"):
        for m in re.finditer(r'threading\.Thread\((?:[^()]|\([^()]*\))*?name="([a-z0-9-]+)"',
                             path.read_text(encoding="utf-8"), re.S):
            if DELETES.search(m.group(1)) or m.group(1) in conftest._DELETING_THREAD_NAMES:
                names.add(m.group(1))
    assert names <= conftest._DELETING_THREAD_NAMES | conftest._BACKGROUND_THREAD_NAMES, names
    assert "leftover-sweep" in conftest._DELETING_THREAD_NAMES


def test_a_windows_startup_sweep_does_not_run_in_unit_tests():
    """The 5 s timer's sweep is stubbed on MainWindow only; the mixin keeps the real one."""
    from nadare.gui.main_window import MainWindow
    from nadare.gui.main_window_parts.cleanup import CleanupMixin

    assert MainWindow._start_leftover_sweep is not CleanupMixin._start_leftover_sweep
    assert MainWindow._start_leftover_sweep(object()) is None
