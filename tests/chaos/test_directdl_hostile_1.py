# SPDX-License-Identifier: GPL-3.0-or-later
"""A NUL in a publisher's file path must be a clear DirectError, not a ValueError traceback."""

from __future__ import annotations

import pytest

from nadare.core import directdl as dd


def test_a_nul_in_a_plan_file_path_is_rejected_cleanly(tmp_path):
    path = "game\x00.exe"
    print("plan path:", repr(path))
    plan = dd.Plan("T", "1", files=(dd.RemoteFile(path, "http://127.0.0.1:9/x", 1),))

    with pytest.raises(dd.DirectError, match="(?i)nul|path"):
        dd.sync(plan, tmp_path)
