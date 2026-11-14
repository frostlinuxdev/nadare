# SPDX-License-Identifier: GPL-3.0-or-later
"""W8-7: the support report must read the installed version the UI/CLI show."""

from __future__ import annotations

import json

from nadare.core import report


def test_support_report_reads_the_install_version_file(tmp_path):
    install = tmp_path / "install"
    install.mkdir()
    (install / ".nadare-version.json").write_text(json.dumps({"version": "3.8.0"}))
    profile = {
        "backend": "direct-wuwa",
        "title": "Wuthering Waves",
        "install_dir": str(install),
        "prefix": str(tmp_path / "prefix"),
    }
    config = {"active_profile": "wuwa", "profiles": {"wuwa": profile}}
    text = report.build_report(
        config,
        profile_name="wuwa",
        logs_path=tmp_path / "logs",
        profiles_path=tmp_path / "profiles",
        run_command=lambda *a, **k: "",
        doctor_text="ok",
        os_release_path=tmp_path / "missing-os-release",
    )
    line = next(line for line in text.splitlines() if line.startswith("Installed version:"))
    print("report line:", line, "| UI/CLI version: 3.8.0")
    assert line == "Installed version: 3.8.0", (
        "Report reads profile['version'] (never written) instead of the install record"
    )
