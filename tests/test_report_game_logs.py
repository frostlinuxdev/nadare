# SPDX-License-Identifier: GPL-3.0-or-later
"""The support report attaches the newest run of each operation from the game's log folder."""

from __future__ import annotations

import os
from pathlib import Path

from nadare.core import runlog
from nadare.core.report import build_report


def _run(folder: Path, operation: str, stamp: str, text: str, mtime: float) -> Path:
    folder.mkdir(parents=True, exist_ok=True)
    path = folder / f"{operation}-{stamp}.log"
    path.write_text(text, encoding="utf-8")
    os.utime(path, (mtime, mtime))
    return path


def _report(tmp_path: Path, profile: str = "Blue Archive") -> str:
    config = {"active_profile": profile, "profiles": {profile: {"backend": "direct-bd2"}}}
    return build_report(
        config,
        profile_name=profile,
        logs_path=tmp_path / "logs",
        profiles_path=tmp_path / "profiles",
        username="someone",
        home=tmp_path / "home",
        hostname="somehost",
        run_command=lambda *a, **k: "unavailable",
        proc_root=tmp_path / "proc",
        doctor_text="doctor ok",
    )


def test_the_newest_run_of_each_operation_is_attached(tmp_path):
    logs = tmp_path / "logs"
    logs.mkdir()
    folder = logs / runlog.game_id("Blue Archive")
    _run(folder, "install", "20261105-100000", "OLD INSTALL RUN", 100)
    _run(folder, "install", "20261105-110000", "NEW INSTALL RUN", 200)
    _run(folder, "launch", "20261105-120000", "THE LAUNCH RUN", 300)
    report = _report(tmp_path)
    assert "NEW INSTALL RUN" in report and "THE LAUNCH RUN" in report
    assert "OLD INSTALL RUN" not in report
    assert f"### {folder.name}/install-20261105-110000.log" in report


def test_another_games_runs_are_not_attached(tmp_path):
    logs = tmp_path / "logs"
    logs.mkdir()
    _run(logs / runlog.game_id("Blue Archive"), "launch", "20261105-120000", "MINE", 300)
    _run(logs / "genshin-impact", "launch", "20261105-120000", "SOMEONE ELSES", 300)
    report = _report(tmp_path)
    assert "MINE" in report and "SOMEONE ELSES" not in report


def test_the_rolling_background_file_is_not_attached(tmp_path):
    logs = tmp_path / "logs"
    folder = logs / runlog.game_id("Blue Archive")
    folder.mkdir(parents=True)
    (folder / "background.log").write_text("NEWS POLL NOISE", encoding="utf-8")
    assert "NEWS POLL NOISE" not in _report(tmp_path)


def test_other_games_folders_are_summarised_not_dumped(tmp_path):
    logs = tmp_path / "logs"
    logs.mkdir()
    _run(logs / "genshin-impact", "launch", "20261105-120000", "x" * 50, 300)
    _run(logs / "genshin-impact", "install", "20261105-120000", "y" * 50, 300)
    report = _report(tmp_path)
    # Counted, not named (maintainer, 2026-11-11: which games someone plays is theirs).
    assert "genshin" not in report
    assert "Logs for 1 other game (not included)." in report


def test_a_missing_game_folder_is_not_an_error(tmp_path):
    (tmp_path / "logs").mkdir()
    assert "## Logs" in _report(tmp_path)
