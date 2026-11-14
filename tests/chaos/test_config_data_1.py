# SPDX-License-Identifier: GPL-3.0-or-later
"""A corrupt ``dismissed_notices`` value must not break opening the window."""

from __future__ import annotations

import json

from _gui_smoke_helpers import qapp  # noqa: F401,F811 - pytest fixture

from nadare.core.config import Config
from nadare.gui.main_window import MainWindow


def test_a_non_list_dismissed_notices_does_not_crash_notice_handling(
    tmp_path, qapp, monkeypatch  # noqa: F811 - the imported fixture
):
    path = tmp_path / "config.json"
    path.write_text(
        json.dumps({"profiles": {}, "dismissed_notices": 5}), encoding="utf-8"
    )
    config = Config.load(path)
    # The background feed is irrelevant here; call the handler directly instead.
    monkeypatch.setattr(MainWindow, "_start_notices_check", lambda self: None)
    print("dismissed_notices as loaded:", repr(config.data["dismissed_notices"]))

    window = MainWindow(config)
    try:
        qapp.processEvents()
        # The notices worker calls this with the feed's items.  An empty feed still
        # has to cope with the damaged setting: it must not raise in the event loop.
        window._show_notice([])
        qapp.processEvents()
        assert window.notice_banner.isHidden()
    finally:
        window.close()
        window.deleteLater()
        qapp.processEvents()
