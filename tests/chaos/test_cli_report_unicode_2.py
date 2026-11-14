# SPDX-License-Identifier: GPL-3.0-or-later
"""A non-UTF-8 log file name must not crash the support report."""

from __future__ import annotations

import os

import pytest

from nadare import app
from nadare.core import paths
from nadare.core.config import Config


def test_report_survives_a_non_utf8_log_file_name(tmp_path, capsys):
    path = tmp_path / "config.json"
    config = Config.load(path)
    config.add_profile("g", backend="wine", title="G")
    config.save()

    logs = paths.logs_dir()
    logs.mkdir(parents=True, exist_ok=True)
    # Linux file names are bytes; this one is not valid UTF-8, so pathlib gives
    # Python a surrogate-escaped string. The report used to strict-encode it.
    bad_log = logs / os.fsdecode(b"g-launch-\xff.log")
    bad_log.write_bytes(b"a line from a game log\n")

    try:
        code = app.run_cli(["-c", str(path), "report", "--stdout"])
    except Exception as exc:  # noqa: BLE001 - any exception here is the bug
        pytest.fail(
            f"report crashed on a non-UTF-8 log file name: {type(exc).__name__}: {exc}"
        )

    out = capsys.readouterr().out
    print("return code:", code)
    print("report starts:", repr(out[:60]))
    assert code == 0
    assert "# Nadare support report" in out
