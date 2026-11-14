# SPDX-License-Identifier: GPL-3.0-or-later
"""An absurd version in a notice must drop that notice, not break the whole feed."""

from __future__ import annotations

from nadare.core import notices

URL = "https://idx.example/index/notices.json"


def test_a_notice_with_an_absurd_below_version_is_skipped():
    document = {
        "notices": [
            {"id": "bad", "text": "bad version", "below": "9" * 5000},
            {"id": "good", "text": "still shown"},
        ]
    }
    try:
        parsed = notices.parse(document, url=URL)
    except Exception as exc:
        parsed = f"{type(exc).__name__}: {exc}"
    print("parse result:", parsed)
    assert isinstance(parsed, list)
    assert [item.id for item in parsed] == ["good"]
