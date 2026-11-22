# SPDX-License-Identifier: GPL-3.0-or-later
"""No new "same device and inode" proof outside :mod:`nadare.core.folderproof` (b25 public CI).

b25 deleted or adopted folders after a restart on the strength of a recorded ``[dev, ino]``.
ext4 hands a deleted folder's inode number to the next folder made, so that proof was fakeable
by accident, and tmpfs (where the suite ran here) hid it. Every place that reads ``st_ino`` or
calls an inode-identity helper is listed below with why it is safe; a new one fails this test
until it is either moved onto ``folderproof`` (records kept across a restart) or listed with a
reason (checks inside one running operation, where the folder cannot be swapped unseen).
"""

from __future__ import annotations

import ast
from pathlib import Path

SRC = Path(__file__).resolve().parents[1] / "src"

#: Inode-identity helpers: a call to one is the same as reading ``st_ino``.
HELPERS = frozenset({"_identity", "_folder_identity"})

IN_PROCESS = "compared within one running operation, never kept across a restart"

#: ``(file under src/, enclosing function)`` -> why an inode number is fine there.
ALLOWED: dict[tuple[str, str], str] = {
    ("nadare/core/relocate.py", "_identity"): "the in-process helper itself (folderproof.identity)",
    ("nadare/core/relocate.py", "move_tree"): IN_PROCESS + " (the partial during one copy)",
    ("nadare/core/relocate.py", "_check_move_identity"): IN_PROCESS + " (right before a Move)",
    ("nadare/core/relocate.py", "_anchor_matches"): (
        "only lets a recovery go on; anything it then deletes or adopts needs folderproof"
    ),
    ("nadare/core/relocate.py", "begin_move"): (
        "the source pair comes from the stamped proof; the fallback (no marker) and the "
        "anchor never prove a folder on their own"
    ),
    ("nadare/core/relocate.py", "record_partial"): (
        "fallback for a record without a token: such a partial is never removed or adopted"
    ),
    ("nadare/core/relocate.py", "finish_interrupted_moves"): (
        "src still in place means nothing is moved or deleted but the proven partial"
    ),
    ("nadare/core/importmove.py", "_source_ids"): IN_PROCESS + " (plan; run() stamps)",
    ("nadare/core/importmove.py", "_stamp_sources"): (
        "fallback for a source that cannot take a marker: such a tree is never adopted or "
        "removed by a recovery"
    ),
    ("nadare/core/importmove.py", "run"): (
        "the copy's pair goes into the journal with the source's token, which the copy "
        "carries; the unstamped fallback is never adopted or removed"
    ),
    ("nadare/core/app_uninstall.py", "_identity"): IN_PROCESS + " (with size and mtime)",
    ("nadare/core/app_uninstall.py", "preview"): IN_PROCESS + " (the plan shown, then trash)",
    ("nadare/core/app_uninstall.py", "trash"): IN_PROCESS,
    ("nadare/core/psoguard.py", "stamp"): "a different _identity: a cache header, no inode",
    ("nadare/core/safetycheck.py", "open"): IN_PROCESS + " (descriptor walk)",
    ("nadare/core/safetycheck.py", "_entries.walk"): IN_PROCESS + " (descriptor walk)",
    ("nadare/core/logging.py", "_reopen_if_rotated"): "only decides to reopen a log file",
    ("nadare/gui/logs_dialog.py", "_load_selected"): "only decides to reload a log view",
    ("nadare/gui/logs_dialog.py", "_tick"): "only decides to reload a log view",
    ("nadare/backends/czn/update.py", "plan"): (
        "a hash cache key with size, mtime_ns and ctime_ns: a wrong hit only skips a "
        "re-hash, and the file is still checked before use; deletes nothing"
    ),
    ("nadare/backends/winegame_parts/launching.py", "_stop_request_identity"): (
        IN_PROCESS + " (with mtime_ns)"
    ),
}


def _uses() -> set[tuple[str, str]]:
    found: set[tuple[str, str]] = set()
    for path in sorted(SRC.rglob("*.py")):
        rel = path.relative_to(SRC).as_posix()
        if rel == "nadare/core/folderproof.py":
            continue

        def walk(node: ast.AST, where: str, rel: str = rel) -> None:
            for child in ast.iter_child_nodes(node):
                inner = where
                if isinstance(child, (ast.FunctionDef, ast.AsyncFunctionDef)):
                    inner = child.name if where == "<module>" else f"{where}.{child.name}"
                if isinstance(child, ast.Attribute) and child.attr == "st_ino":
                    found.add((rel, inner))
                if isinstance(child, ast.Call):
                    func = child.func
                    name = func.attr if isinstance(func, ast.Attribute) else getattr(func, "id", "")
                    if name in HELPERS or (
                        name == "identity"
                        and isinstance(func, ast.Attribute)
                        and getattr(func.value, "id", "") == "folderproof"
                    ):
                        found.add((rel, inner))
                walk(child, inner)

        walk(ast.parse(path.read_text(encoding="utf-8")), "<module>")
    return found


def test_every_inode_identity_use_is_listed_with_a_reason():
    unlisted = sorted(_uses() - ALLOWED.keys())
    assert not unlisted, (
        "Device+inode is no proof after a restart (ext4 reuses inode numbers). Use "
        "nadare.core.folderproof for anything kept across a restart, or list these in "
        f"ALLOWED with the reason they are safe: {unlisted}"
    )


def test_no_listed_use_is_stale():
    assert not sorted(ALLOWED.keys() - _uses())


def test_the_scan_sees_a_new_use(tmp_path, monkeypatch):
    pkg = tmp_path / "nadare" / "core"
    pkg.mkdir(parents=True)
    (pkg / "x.py").write_text(
        "import os\n"
        "def keep(p):\n    return os.lstat(p).st_ino\n"
        "def other(p):\n    return relocate._identity(p)\n"
    )
    monkeypatch.setitem(globals(), "SRC", tmp_path)
    assert _uses() == {("nadare/core/x.py", "keep"), ("nadare/core/x.py", "other")}
