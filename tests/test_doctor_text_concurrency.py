# SPDX-License-Identifier: GPL-3.0-or-later
"""Review wave 3: two overlapping doctor runs must not mix their output."""

from __future__ import annotations

import sys
import threading
import time

from nadare import app as cli_app


def test_overlapping_doctor_runs_keep_their_own_output_and_restore_stdout(monkeypatch):
    original = sys.stdout
    started = threading.Barrier(2)

    def fake_doctor(config, args=None):
        label = config.data["label"]
        print(f"{label} line 1")
        time.sleep(0.05)
        print(f"{label} line 2")
        return 0

    monkeypatch.setattr(cli_app, "_run_doctor", fake_doctor)
    results: dict[str, str] = {}

    def run(label: str) -> None:
        started.wait()
        config = type("C", (), {"data": {"label": label}})()
        results[label] = cli_app._doctor_text(config)

    threads = [threading.Thread(target=run, args=(name,)) for name in ("A", "B")]
    for thread in threads:
        thread.start()
    for thread in threads:
        thread.join()
    assert results["A"] == "A line 1\nA line 2"
    assert results["B"] == "B line 1\nB line 2"
    assert sys.stdout is original
