# SPDX-License-Identifier: GPL-3.0-or-later
"""#1825 guard: a new game that installs loose files must say how an update removes old ones.

#854 taught archive games to remove dropped members; Snowbreak (11-16) and ToF (11-23) were added
after it as loose-file games and nothing removed their dropped files. This test reads every
``Plan(...)`` built in ``src/nadare`` with a ``files=`` list: it must pass ``prune_files`` or be
listed below with the place its cleanup lives. A new one fails here until someone decides.
"""

from __future__ import annotations

import ast
from pathlib import Path

SRC = Path(__file__).resolve().parents[1] / "src" / "nadare"

#: (module path, function) -> where the dropped files of that plan are removed instead.
ELSEWHERE = {
    ("backends/czn/update.py", "plan"): "check-only plan, never synced",
    ("backends/czn/update.py", "to_plan"): "partial sync; czn.update._remove_dropped",
    ("core/railrepo.py", "_record_and_prune"): "is the cleanup (directdl.prune_dropped_files)",
    ("core/sources/bpsrglobal.py", "resolve"): "direct_bpsr manifest-files.json",
    ("core/sources/kuro.py", "resolve_game"): "Kuro retired lists (#1252)",
    ("core/sources/kuro.py", "_bundle_files"): "Kuro retired lists (#1252)",
    ("core/yostar.py", "to_plan"): "arknights _remove_dropped (#1452 step 3d)",
}


def _plans_with_files():
    for path in sorted(SRC.rglob("*.py")):
        tree = ast.parse(path.read_text(encoding="utf-8"))
        for func in ast.walk(tree):
            if not isinstance(func, (ast.FunctionDef, ast.AsyncFunctionDef)):
                continue
            for node in ast.walk(func):
                if not isinstance(node, ast.Call):
                    continue
                name = getattr(node.func, "attr", getattr(node.func, "id", ""))
                if name != "Plan":
                    continue
                keywords = {k.arg for k in node.keywords}
                if "files" in keywords or len(node.args) >= 3:
                    yield path.relative_to(SRC).as_posix(), func.name, keywords


def test_every_plan_with_loose_files_removes_dropped_ones():
    undecided = [
        f"{module}:{func}"
        for module, func, keywords in _plans_with_files()
        if "prune_files" not in keywords and (module, func) not in ELSEWHERE
    ]
    assert not undecided, f"say how these remove a dropped file (#1825): {undecided}"


def test_the_exception_list_has_no_dead_entries():
    found = {(module, func) for module, func, _kw in _plans_with_files()}
    assert set(ELSEWHERE) <= found, sorted(set(ELSEWHERE) - found)
