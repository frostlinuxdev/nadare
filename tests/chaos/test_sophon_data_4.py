# SPDX-License-Identifier: GPL-3.0-or-later
"""A NUL path in a Sophon ledger is unsafe data; sync must reject it, not raise ValueError."""

from __future__ import annotations

import sys
from pathlib import Path

import pytest

sys.path.insert(0, str(Path(__file__).resolve().parents[1]))

from test_sophon import Build, plan_for  # noqa: E402

from nadare.core import directdl as dd


def test_sync_with_nul_in_a_ledger_reports_a_direct_error_and_writes_nothing(tmp_path):
    root = tmp_path / "install"
    (root / ".nadare").mkdir(parents=True)
    ledger = root / ".nadare" / "sophon-game.files"
    ledger.write_text("evil\x00name\n", encoding="utf-8")

    build = Build({"game.bin": b"game data"})
    server = build.serve()
    try:
        with pytest.raises(dd.DirectError):
            dd.sync(plan_for(build), root)
    finally:
        server.close()

    print("ledger content:", repr(ledger.read_text(encoding="utf-8")))
    print("game file written:", (root / "game.bin").exists())
    assert not (root / "game.bin").exists()
