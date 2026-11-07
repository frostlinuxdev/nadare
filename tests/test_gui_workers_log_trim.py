# SPDX-License-Identifier: GPL-3.0-or-later
"""Old flat logs are left alone; a new run goes to its game's folder (issue #838)."""

from __future__ import annotations

import pytest

pytest.importorskip("PyQt6.QtWidgets")

from nadare.core.config import Config
from nadare.gui import workers


def test_a_legacy_flat_log_is_not_touched_by_a_new_run(tmp_path, monkeypatch):
    logs = tmp_path / "logs"
    logs.mkdir()
    legacy = logs / "G-install.log"
    legacy.write_text("===== old run =====\n" + "x" * 2_000_000, encoding="utf-8")
    before = legacy.read_bytes()

    monkeypatch.setattr(workers.paths, "logs_dir", lambda: logs)
    worker = workers.BackendWorker(Config.load(tmp_path / "config.json"), "G", "install")
    handle = worker._open_log()
    assert handle is not None
    handle.close()
    worker._log = None

    assert legacy.read_bytes() == before
    assert len(list(logs.glob("*/install-*.log"))) == 1
