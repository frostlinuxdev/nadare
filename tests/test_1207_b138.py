# SPDX-License-Identifier: GPL-3.0-or-later
# repo/tests/test_core_yostar_media_url.py
from __future__ import annotations

from nadare.core import artwork, yostar


def test_a_malformed_media_port_is_skipped_not_raised(monkeypatch):
    calls = []

    def fake_download(*args, **kwargs):
        calls.append(args)
        return None

    monkeypatch.setattr(artwork, "download", fake_download)
    media = yostar.Media(
        game_tag="Arknights_EN",
        generated_at="t",
        background="https://cdn.yo-star.com:bad/bg.jpg",
    )

    assert yostar.cache_media(media, "arknights") == {"hero": None, "banners": []}
    assert calls == []
