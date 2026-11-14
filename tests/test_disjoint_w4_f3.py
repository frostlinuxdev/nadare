# SPDX-License-Identifier: GPL-3.0-or-later
"""W4-F3: ``nadare repair`` must forget a remembered Visual C++ failure, as the GUI does."""

from __future__ import annotations

from nadare import app
from nadare.core import vcredist
from nadare.core.config import Config


def test_cli_repair_forgets_the_visual_cpp_failure(tmp_path, monkeypatch):
    config_path = tmp_path / "config.json"
    config = Config.load(config_path)
    config.add_profile("game", backend="wine", title="Game")
    config.save()
    prefix = tmp_path / "pfx"
    (prefix / "drive_c").mkdir(parents=True)
    vcredist._mark_failed(prefix, kind="failed")
    assert (prefix / vcredist.FAILED_MARKER).exists()

    calls: list[str] = []

    class Backend:
        kind = "wine"

        def prefix(self):
            return prefix

        def requires_notice_ack(self):
            return False

        def repair(self):
            calls.append("repair")

    monkeypatch.setattr(app, "create_backend", lambda *a, **k: Backend())
    code = app.run_cli(["-c", str(config_path), "repair", "-p", "game"])

    assert code == 0, code
    assert calls == ["repair"], calls
    assert not (prefix / vcredist.FAILED_MARKER).exists(), (
        "the CLI repair left the remembered runtime failure in place"
    )
