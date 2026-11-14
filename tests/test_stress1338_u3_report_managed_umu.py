# SPDX-License-Identifier: GPL-3.0-or-later
"""The support report must name the umu a launch really uses (#1338).

Every desktop launch now falls back to Nadare's managed umu, but `core/report.py` runs only
`umu-run --version`. On a host with no system umu (the exact #1338 setup) the report says
"not found", so a support reader cannot see which umu ran the game.
"""

from __future__ import annotations

from nadare.core import report, umu


def test_the_report_names_the_managed_umu_when_there_is_no_system_one(tmp_path, monkeypatch):
    managed = tmp_path / "data" / "nadare" / "umu" / "1.4.4" / "umu-run"
    monkeypatch.setattr(umu, "managed", lambda: managed)

    def run_command(args, **kwargs):
        if list(args)[:2] == ["umu-run", "--version"]:
            return "not found"
        return "unavailable"

    text = report.build_report(
        {"profiles": {}, "active_profile": ""},
        logs_path=tmp_path / "logs",
        profiles_path=tmp_path / "profiles",
        username="tester",
        home=tmp_path,
        hostname="host",
        run_command=run_command,
        proc_root=tmp_path / "proc",
    )
    # The report hides the home folder (privacy): the managed path shows as ~/...
    assert "umu-run (Nadare's own): ~/data/nadare/umu/1.4.4/umu-run" in text
