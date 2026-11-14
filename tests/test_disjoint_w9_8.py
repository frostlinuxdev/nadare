# SPDX-License-Identifier: GPL-3.0-or-later
"""W9-8: a runner under ``extra`` is read by WineGame but not by the support report."""

from __future__ import annotations

from nadare.backends.wine import profile_setting
from nadare.core import report


def test_report_sees_the_runner_kept_under_extra(tmp_path):
    runner = tmp_path / "runners" / "GE-Proton11-7-x86_64"
    runner.mkdir(parents=True)
    (runner / "proton").write_text("#!/bin/sh\n")
    profile = {
        "backend": "direct-wuwa",
        "title": "Wuthering Waves",
        "install_dir": str(tmp_path / "game"),
        "extra": {"runner": str(runner)},
    }
    config = {"active_profile": "wuwa", "profiles": {"wuwa": profile}}

    winegame_reads = profile_setting(profile, "runner")
    text = report.build_report(
        config,
        profile_name="wuwa",
        logs_path=tmp_path / "logs",
        profiles_path=tmp_path / "profiles",
        run_command=lambda *a, **k: "",
        doctor_text="ok",
        os_release_path=tmp_path / "missing-os-release",
    )
    line = next(line for line in text.splitlines() if line.startswith("Runner:"))
    print("WineGame reads:", winegame_reads, "| support report says:", repr(line))
    assert winegame_reads == str(runner), "fixture: WineGame reads the extra runner"
    # The report is redacted (user name, home): compare the runner, not the whole path.
    assert line.endswith(f"/runners/{runner.name}"), (
        "The support report ignores extra.runner and prints 'unknown' for a runner Nadare uses"
    )



def test_a_runner_path_in_the_home_is_redacted_in_the_report(tmp_path):
    """The report is posted publicly: a runner path must not show the user's home (#1310)."""
    home = tmp_path / "home" / "alice"
    runner = home / ".local" / "share" / "nadare" / "runners" / "GE-Proton9-1"
    profile = {"backend": "direct-bd2", "title": "G", "extra": {"runner": str(runner)}}
    text = report.build_report(
        {"active_profile": "g", "profiles": {"g": profile}},
        profile_name="g",
        logs_path=tmp_path / "logs",
        profiles_path=tmp_path / "profiles",
        username="alice",
        home=home,
        run_command=lambda *a, **k: "",
        doctor_text="ok",
        os_release_path=tmp_path / "missing-os-release",
    )
    line = next(ln for ln in text.splitlines() if ln.startswith("Runner:"))
    print(line)
    assert "alice" not in line and line.endswith("GE-Proton9-1"), line
