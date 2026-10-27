# SPDX-License-Identifier: GPL-3.0-or-later
"""Starting an operation trims old runs so its log cannot grow without bound."""

from __future__ import annotations

import pytest

pytest.importorskip("PyQt6.QtWidgets")

from nadare.core.config import Config
from nadare.gui import workers

HEADER = "===== 2026-10-26T10:00:{second:02d} install (G) | nadare test =====\n"


def test_open_log_drops_oldest_runs_before_appending(tmp_path, monkeypatch):
    logs = tmp_path / "logs"
    logs.mkdir()
    path = logs / "G-install.log"
    blocks = [
        HEADER.format(second=index) + f"old-run-{index}\n" + "x" * 300_000 + "\n"
        for index in range(5)
    ]
    path.write_text("".join(blocks), encoding="utf-8")
    assert path.stat().st_size > 1_048_576

    monkeypatch.setattr(workers.paths, "logs_dir", lambda: logs)
    worker = workers.BackendWorker(Config.load(tmp_path / "config.json"), "G", "install")
    handle = worker._open_log()
    assert handle is not None
    handle.close()
    worker._log = None

    text = path.read_text(encoding="utf-8")
    assert "old-run-0" not in text
    assert "old-run-4" in text
    assert text.count("install (G)") == 4
    assert path.stat().st_size <= 1_048_576 + len(HEADER) + 64
