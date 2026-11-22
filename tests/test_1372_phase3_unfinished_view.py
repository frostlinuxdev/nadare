# SPDX-License-Identifier: GPL-3.0-or-later
"""#1372 phase 3: one read-only view of every unfinished storage operation.

Four records can say "a storage operation did not finish": ``removal_pending`` (Uninstall),
``move_originals_pending`` (a Move still has an original to delete), ``move_in_progress`` (a
Move a crash interrupted) and the import-move journal next to the config. Each is settled by
its own code at startup or on retry; a support report has to show all of them in one place,
without changing anything (stored keys stay as b24 reads them: DECISIONS 2026-11-15).
"""

from __future__ import annotations

import copy
import json
import os
from pathlib import Path

from nadare.core import pending
from nadare.core.report import build_report


def _profiles(tmp_path: Path) -> dict:
    old = tmp_path / "disk1" / "Game"
    old.mkdir(parents=True)
    st = os.lstat(old)
    return {
        "g": {
            "backend": "direct-wuwa",
            "removal_pending": {"install_dir": str(tmp_path / "gone-install")},
            "move_originals_pending": [
                [str(old), str(tmp_path / "disk2" / "Game"), st.st_dev, st.st_ino]
            ],
            "move_in_progress": {"from": str(old), "to": str(tmp_path / "disk2" / "Game")},
        },
        "other": {
            "backend": "direct-hsr",
            "move_in_progress": {"from": "/x/a", "to": "/y/a"},
        },
        "clean": {"backend": "direct-zzz"},
    }


def _journal(tmp_path: Path, profile: str = "g") -> Path:
    path = tmp_path / ".config.json.importmove.json"
    path.write_text(json.dumps({
        "profile": profile, "library": str(tmp_path / "Games"),
        "home": str(tmp_path / "Games" / "Nadare" / "Game"), "trees": [],
    }), encoding="utf-8")
    return path


def test_lists_every_kind_of_unfinished_operation(tmp_path: Path) -> None:
    profiles = _profiles(tmp_path)
    journal = _journal(tmp_path)
    found = pending.unfinished(profiles, journal)
    kinds = sorted((u.profile, u.kind) for u in found)
    assert kinds == [
        ("g", "import-move"), ("g", "move"), ("g", "move-original"), ("g", "removal"),
        ("other", "move"),
    ]
    by_kind = {u.kind: u for u in found if u.profile == "g"}
    assert str(tmp_path / "gone-install") in by_kind["removal"].detail
    assert "gone" in by_kind["removal"].detail  # says whether the folder is still there
    assert str(tmp_path / "disk1" / "Game") in by_kind["move-original"].detail
    assert "still there" in by_kind["move-original"].detail
    assert str(tmp_path / "Games") in by_kind["import-move"].detail


def test_is_read_only(tmp_path: Path) -> None:
    profiles = _profiles(tmp_path)
    journal = _journal(tmp_path)
    before = copy.deepcopy(profiles)
    text = journal.read_bytes()
    listing = sorted(p.relative_to(tmp_path) for p in tmp_path.rglob("*"))
    pending.unfinished(profiles, journal)
    assert profiles == before
    assert journal.read_bytes() == text
    assert sorted(p.relative_to(tmp_path) for p in tmp_path.rglob("*")) == listing


def test_nothing_unfinished_is_an_empty_list(tmp_path: Path) -> None:
    assert pending.unfinished({"clean": {"backend": "x"}}, tmp_path / "missing.json") == []


def test_damaged_records_are_shown_not_raised(tmp_path: Path) -> None:
    profiles = {
        "a": {"removal_pending": "garbage", "move_originals_pending": [["only-one"]],
              "move_in_progress": 7},
        "b": "not a profile",
    }
    journal = tmp_path / "j.json"
    journal.write_text("{not json", encoding="utf-8")
    found = pending.unfinished(profiles, journal)
    kinds = sorted((u.profile, u.kind) for u in found)
    assert kinds == [
        ("", "import-move"), ("a", "move"), ("a", "move-original"), ("a", "removal"),
    ]
    assert all("damaged" in u.detail for u in found)


def test_journal_for_a_game_that_is_gone_is_still_shown(tmp_path: Path) -> None:
    journal = _journal(tmp_path, profile="deleted-game")
    found = pending.unfinished({}, journal)
    assert [(u.profile, u.kind) for u in found] == [("deleted-game", "import-move")]


def _report(tmp_path: Path, profiles: dict, profile: str, journal: Path) -> str:
    return build_report(
        {"active_profile": profile, "profiles": profiles},
        logs_path=tmp_path / "logs",
        profiles_path=tmp_path / "profiles",
        run_command=lambda *a, **k: "unavailable",
        proc_root=tmp_path / "proc",
        config_path=journal.parent / "config.json",
    )


def test_report_details_this_game_and_only_counts_others(tmp_path: Path) -> None:
    profiles = _profiles(tmp_path)
    journal = _journal(tmp_path)
    report = _report(tmp_path, profiles, "g", journal)
    section = report.split("## Unfinished operations", 1)[1].split("\n## ", 1)[0]
    assert "Uninstall" in section and "Move" in section and "Move into a library" in section
    # Other games are counted, never named (the report's privacy rule).
    assert "1 unfinished operation for another game" in section
    assert "/x/a" not in section and "other" not in section.replace("another", "")


def test_report_says_none_when_all_settled(tmp_path: Path) -> None:
    report = _report(tmp_path, {"clean": {"backend": "x"}}, "clean", tmp_path / "none.json")
    section = report.split("## Unfinished operations", 1)[1].split("\n## ", 1)[0]
    assert "None" in section


def test_report_survives_a_failing_view(tmp_path: Path, monkeypatch) -> None:
    def boom(*_a, **_k):
        raise RuntimeError("bad")
    monkeypatch.setattr(pending, "unfinished", boom)
    report = _report(tmp_path, {"clean": {}}, "clean", tmp_path / "none.json")
    assert "## Unfinished operations" in report and "unavailable" in report
    assert "## Logs" in report


def test_journal_path_matches_importmove(tmp_path: Path) -> None:
    from nadare.core import importmove
    from nadare.core.config import Config

    cfg = Config.load(tmp_path / "config.json")
    assert pending.import_journal_path(cfg.path) == importmove._journal_path(cfg)  # noqa: SLF001


def test_cli_report_reads_the_journal_beside_its_own_config(tmp_path, monkeypatch, capsys):
    """``nadare -c other.json report`` looks next to other.json, not the default config."""
    from nadare import app

    for key in ("CONFIG", "DATA", "STATE", "CACHE"):
        monkeypatch.setenv(f"XDG_{key}_HOME", str(tmp_path / key.lower()))
    folder = tmp_path / "elsewhere"
    folder.mkdir()
    config = folder / "other.json"
    config.write_text(json.dumps({"profiles": {"g": {"backend": "direct-wuwa"}},
                                  "active_profile": "g"}), encoding="utf-8")
    (folder / ".other.json.importmove.json").write_text(json.dumps({
        "profile": "g", "library": str(tmp_path / "Lib"), "home": "x", "trees": [{}],
    }), encoding="utf-8")
    assert app.run_cli(["--config", str(config), "report", "--profile", "g", "--stdout"]) == 0
    out = capsys.readouterr().out
    section = out.split("## Unfinished operations", 1)[1].split("\n## ", 1)[0]
    assert "Move into a library: into" in section and "1 folder;" in section


def test_gui_task_passes_its_config_path(tmp_path: Path, monkeypatch) -> None:
    import pytest

    pytest.importorskip("PyQt6")
    from nadare.gui import support_report

    seen = {}

    def fake(config, **kwargs):
        seen.update(kwargs)
        return "ok"

    monkeypatch.setattr("nadare.core.report.build_report", fake)
    monkeypatch.setattr("nadare.app._doctor_text", lambda _c: "")
    task = support_report.ReportTask({}, "g", tmp_path / "c.json")
    task.run()
    assert seen["config_path"] == tmp_path / "c.json"
