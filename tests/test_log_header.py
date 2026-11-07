# SPDX-License-Identifier: GPL-3.0-or-later
"""Every per-game log says which Nadare build wrote it (a tester's log once could not tell us)."""

from __future__ import annotations

import os
import re

os.environ.setdefault("QT_QPA_PLATFORM", "offscreen")

import nadare
from nadare.core.config import Config
from nadare.gui import workers


def _header(tmp_path, monkeypatch, operation: str) -> str:
    monkeypatch.setattr(workers.paths, "logs_dir", lambda: tmp_path / "logs")
    worker = workers.BackendWorker(Config.load(tmp_path / "config.json"), "czn", operation)
    handle = worker._open_log()
    assert handle is not None
    handle.close()
    worker._log = None
    (written,) = (tmp_path / "logs" / "czn").glob(f"{operation}-*.log")
    text = written.read_text(encoding="utf-8")
    return text.strip().splitlines()[0]


def test_log_header_names_the_nadare_build(tmp_path, monkeypatch):
    header = _header(tmp_path, monkeypatch, "launch")
    assert f"nadare {nadare.__version__}" in header


def test_log_header_keeps_its_existing_shape(tmp_path, monkeypatch):
    header = _header(tmp_path, monkeypatch, "install")
    assert re.fullmatch(
        r"===== \d{4}-\d\d-\d\dT\d\d:\d\d:\d\d install \(czn\) \| nadare \S+ =====", header
    ), header


def test_each_new_run_gets_its_own_file_and_header(tmp_path, monkeypatch):
    monkeypatch.setattr(workers.paths, "logs_dir", lambda: tmp_path / "logs")
    for _ in range(2):
        worker = workers.BackendWorker(Config.load(tmp_path / "config.json"), "czn", "launch")
        worker._open_log().close()
    files = sorted((tmp_path / "logs" / "czn").glob("launch-*.log"))
    assert len(files) == 2
    for path in files:
        text = path.read_text(encoding="utf-8")
        assert text.count("===== ") == 1
        assert text.count(f"nadare {nadare.__version__}") == 1
