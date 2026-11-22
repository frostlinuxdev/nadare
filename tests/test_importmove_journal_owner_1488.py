# SPDX-License-Identifier: GPL-3.0-or-later
"""One unresolved import-move journal per config, owned by its profile (#1488).

The journal path is shared by every profile of a config, and the per-profile OperationLock lets
two profiles move at once (two windows, a CLI, a failed earlier commit). A second move used to
replace the first one's journal, and finishing one move could clear the other's. Now a second
profile's move is refused before anything moves, and a move only ever clears its own journal.
"""

from __future__ import annotations

import json
import subprocess
import sys
from pathlib import Path

import pytest

from nadare.core import importmove, process, relocate
from nadare.core.config import Config

SRC = Path(importmove.__file__).resolve().parents[2]


@pytest.fixture
def setup(tmp_path):
    cfg = Config.load(tmp_path / "config.json")
    library = tmp_path / "Games"
    library.mkdir()
    for name in ("a", "b"):
        install = tmp_path / "old" / name / "install"
        install.mkdir(parents=True)
        (install / "synthetic.pak").write_bytes(name.encode())
        prefix = tmp_path / "old" / name / "prefix"
        (prefix / "drive_c").mkdir(parents=True)
        cfg.add_profile(
            name,
            backend="direct-wuwa",
            title=f"Synthetic {name}",
            install_dir=str(install),
            prefix=str(prefix),
        )
    cfg.save()
    return cfg, library, tmp_path


def _journal(move) -> dict:
    return json.loads(move.journal.read_text(encoding="utf-8"))


def test_a_second_profile_cannot_overwrite_an_uncommitted_journal(setup):
    cfg, library, tmp = setup
    first = importmove.plan(cfg, "a", library)
    second = importmove.plan(cfg, "b", library)
    with process.OperationLock("a"), process.OperationLock("b"):
        importmove.run(first)
        with pytest.raises(relocate.RelocateError, match="(?i)unfinished"):
            importmove.run(second)
    assert _journal(first)["profile"] == "a"
    # Nothing of b moved.
    assert (tmp / "old" / "b" / "install" / "synthetic.pak").read_bytes() == b"b"
    assert (tmp / "old" / "b" / "prefix" / "drive_c").is_dir()


def test_the_second_move_runs_once_the_first_is_committed(setup):
    cfg, library, _ = setup
    first = importmove.plan(cfg, "a", library)
    importmove.run(first)
    importmove.commit(cfg, "a", first)
    cfg.save()
    second = importmove.plan(cfg, "b", library)
    importmove.run(second)
    assert _journal(second)["profile"] == "b"


def test_clearing_one_move_never_removes_another_profiles_journal(setup):
    cfg, library, _ = setup
    first = importmove.plan(cfg, "a", library)
    second = importmove.plan(cfg, "b", library)
    importmove.run(first)
    importmove.clear_journal(second)  # b's rollback/commit must not touch a's record
    assert _journal(first)["profile"] == "a"
    importmove.clear_journal(first)
    assert not first.journal.exists()


def test_a_failed_second_run_does_not_clear_the_first_journal(setup, monkeypatch):
    cfg, library, _ = setup
    first = importmove.plan(cfg, "a", library)
    importmove.run(first)
    second = importmove.plan(cfg, "b", library)
    with pytest.raises(relocate.RelocateError):
        importmove.run(second)
    assert _journal(first)["profile"] == "a"


def test_the_same_profile_can_resume_its_own_journal(setup):
    """Start-up recovery and a retry of the same game still work."""
    cfg, library, _ = setup
    first = importmove.plan(cfg, "a", library)
    importmove.run(first)
    again = importmove.plan(Config.load(cfg.path), "a", library)
    importmove.run(again)
    assert _journal(again)["profile"] == "a"


def test_a_damaged_journal_is_not_overwritten(setup):
    """Unreadable recovery evidence belongs to someone: keep it and refuse."""
    cfg, library, tmp = setup
    second = importmove.plan(cfg, "b", library)
    second.journal.write_text("{damaged", encoding="utf-8")
    with pytest.raises(relocate.RelocateError):
        importmove.run(second)
    assert second.journal.read_text(encoding="utf-8") == "{damaged"
    assert (tmp / "old" / "b" / "install" / "synthetic.pak").read_bytes() == b"b"


_CHILD = r"""
import sys
from pathlib import Path
sys.path.insert(0, sys.argv[1])
from nadare.core import importmove, relocate
from nadare.core.config import Config
cfg = Config.load(Path(sys.argv[2]))
move = importmove.plan(cfg, "b", Path(sys.argv[3]))
try:
    importmove.run(move)
except relocate.RelocateError as exc:
    print("REFUSED", exc)
else:
    print("RAN")
"""


def test_another_process_is_refused_too(setup):
    cfg, library, tmp = setup
    first = importmove.plan(cfg, "a", library)
    importmove.run(first)
    out = subprocess.run(
        [sys.executable, "-c", _CHILD, str(SRC), str(cfg.path), str(library)],
        capture_output=True,
        text=True,
        timeout=60,
    )
    assert out.returncode == 0, out.stderr
    assert out.stdout.startswith("REFUSED"), out.stdout
    assert _journal(first)["profile"] == "a"
    assert (tmp / "old" / "b" / "install" / "synthetic.pak").read_bytes() == b"b"


def test_a_journal_written_between_the_check_and_the_write_is_not_replaced(setup, monkeypatch):
    """The race the atomic claim closes: another move writes its journal at the very moment
    this one writes. A check-then-replace would overwrite it; the link fails instead."""
    cfg, library, tmp = setup
    second = importmove.plan(cfg, "b", library)
    other = json.dumps({"profile": "a", "trees": []})
    real_link, real_replace = importmove.os.link, importmove.os.replace

    def other_first(real):
        def op(src, dst, *a, **k):
            if Path(dst) == second.journal and not second.journal.exists():
                second.journal.write_text(other, encoding="utf-8")  # the other process
            return real(src, dst, *a, **k)

        return op

    monkeypatch.setattr(importmove.os, "link", other_first(real_link))
    monkeypatch.setattr(importmove.os, "replace", other_first(real_replace))
    with pytest.raises(relocate.RelocateError):
        importmove.run(second)
    assert second.journal.read_text(encoding="utf-8") == other
    assert (tmp / "old" / "b" / "install" / "synthetic.pak").read_bytes() == b"b"
