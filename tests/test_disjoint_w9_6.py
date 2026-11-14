# SPDX-License-Identifier: GPL-3.0-or-later
"""W9-6: Verify must not say all checks passed when status says the runner is gone."""

from __future__ import annotations

from _backend_helpers import make_context, write_exe

from nadare.backends.direct import BrownDust2Backend


def test_verify_checks_the_explicit_runner(tmp_path, monkeypatch):
    install = tmp_path / "BD2"
    write_exe(install / "BrownDust II.exe", "MZ")
    missing_runner = tmp_path / "runners" / "GE-Proton11-7"
    backend = BrownDust2Backend(
        make_context(
            tmp_path,
            "direct-bd2",
            {
                "prefix": str(tmp_path / "pfx"),
                "install_dir": str(install),
                "runner": str(missing_runner),
            },
        )
    )
    (backend.prefix() / "drive_c").mkdir(parents=True)
    backend._write_version_file("1.0", "bd2")
    monkeypatch.setattr(backend, "_missing_entries", lambda: [])

    status = backend.status()
    result = backend.verify()
    print("status:", status.state, repr(status.message))
    print("verify:", result["verified"], repr(result["message"]))
    assert status.state == "error", "fixture: status should report the missing runner"
    assert result["verified"] is False, (
        "Verify says all checks passed while status says the runner is missing"
    )
    assert "runner" in result["message"].lower(), result["message"]
