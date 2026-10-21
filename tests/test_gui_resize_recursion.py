# SPDX-License-Identifier: GPL-3.0-or-later
"""Opening a game right after a resize must not recurse in the scroll area (#196).

The page height depends on its width (16:9 news thumbnails, wrapped titles), so
an as-needed vertical scroll bar could flip on and off inside the resize it
causes and overflow the stack. It is a native crash, so the sequence runs in a
child interpreter and the test asserts on its exit status.
"""

from __future__ import annotations

import os
import subprocess
import sys
import textwrap
from pathlib import Path

import pytest

pytest.importorskip("PyQt6.QtWidgets")

_SCRIPT = textwrap.dedent(
    """
    import os, sys, time
    os.environ["XDG_CONFIG_HOME"] = sys.argv[1] + "/config"
    os.environ["XDG_DATA_HOME"] = sys.argv[1] + "/data"
    os.environ["XDG_STATE_HOME"] = sys.argv[1] + "/state"
    os.environ["XDG_CACHE_HOME"] = sys.argv[1] + "/cache"
    from nadare.core.config import Config
    from nadare.core.news import NewsResult
    from nadare.gui import _application, open_window

    app = _application()
    config = Config.load()
    config.add_profile("czn", backend="stove-czn", title="Chaos Zero Nightmare")
    config.add_profile("fgo", backend="fgo-lepton", title="Fate/Grand Order")
    window = open_window(config, profile="czn", app=app)
    end = time.time() + 1
    while time.time() < end:
        app.processEvents()
    titles = [
        "Game Update Completion Notice (10-18 21:00 PDT)",
        "[Updated - New Info] [Event] Revival: Showa Kishin Project: Chaldea Boys Collection",
        "[Ongoing] [Summon] Revival: GUDAGUDA Ryouma's Narrow Escape! Pick-Up Summon",
        "Fate/Grand Order Hints and Strategies (10-16 02:00 PDT)",
        "Regarding 10-16 Game Update",
        "[Ongoing] [Summon] Revival: GUDAGUDA Ryouma's Narrow Escape!",
        "[Ended] [Campaign] 3400 Days Anniversary Celebration Campaign",
        "[Updated - New Info] [Campaign] Lostbelt No.5 Clear Support Campaign",
    ]
    items = [
        {"id": "n%d" % i, "title": t, "category": "News" if i % 2 else "Updates",
         "published_at": "1792281600000", "url": "https://x/%d" % i, "image_path": None}
        for i, t in enumerate(titles)
    ]
    for height in range(1050, 1075):
        window.open_game("czn")
        window.resize(1748, 809)
        window.open_game("fgo")
        window.game.set_news(NewsResult(items=items, available=True))
        window.resize(1402, height)  # no event processing in between
        app.processEvents()
    print("survived", flush=True)
    os._exit(0)
    """
)


def test_open_then_resize_does_not_overflow_the_stack(tmp_path):
    src = str(Path(__file__).resolve().parents[1] / "src")
    env = {**os.environ, "QT_QPA_PLATFORM": "offscreen", "PYTHONPATH": src}
    result = subprocess.run(
        [sys.executable, "-c", _SCRIPT, str(tmp_path)],
        capture_output=True,
        text=True,
        timeout=120,
        env=env,
    )
    assert result.returncode == 0, result.stderr[-400:]
    assert "survived" in result.stdout
