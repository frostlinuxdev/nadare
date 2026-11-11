# SPDX-License-Identifier: GPL-3.0-or-later
"""#998: a ``shutil.which`` stub replaces it for the whole process, so it must take its arguments.

``monkeypatch.setattr(mod.shutil, "which", ...)`` patches the real ``shutil`` module. A stub
that takes only ``name`` broke ``graphics._vulkaninfo_path`` (which passes ``path=``) whenever
that ran inside the test, so the result depended on test order.
"""

from __future__ import annotations

import re
from pathlib import Path

NARROW = re.compile(r'shutil, "which", lambda \w+\s*:')


def test_every_shutil_which_stub_accepts_extra_arguments():
    here = Path(__file__).parent
    narrow = [
        f"{path.name}:{number}"
        for path in sorted(here.glob("test_*.py"))
        for number, line in enumerate(path.read_text(encoding="utf-8").splitlines(), 1)
        if NARROW.search(line)
    ]
    assert narrow == [], "stub shutil.which as `lambda name, *_a, **_k: ...`: " + ", ".join(narrow)
