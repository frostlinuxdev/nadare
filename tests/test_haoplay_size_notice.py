# SPDX-License-Identifier: GPL-3.0-or-later
"""The Haoplay build's size: the full data set, not the small client only."""

from __future__ import annotations

from _backend_helpers import make_context

from nadare.backends import direct
from nadare.core.diskspace import human_size

GB = 10**9


def _backend(tmp_path):
    return direct.Gfl2HaoplayBackend(
        make_context(tmp_path, "direct-gfl2-haoplay", {"prefix": str(tmp_path / "p")})
    )


def test_the_size_hint_is_the_full_data_set(tmp_path):
    assert _backend(tmp_path).install_size_hint == int(94.0 * GB)


def test_the_page_notice_gives_the_full_size_not_the_old_estimate(tmp_path):
    backend = _backend(tmp_path)
    text = backend.page_notice[0]
    assert "60 GB" not in text
    assert f"about {human_size(backend.install_size_hint)}" in text
    assert not any(token.endswith("GB") for token in text.split())


def test_the_notice_says_other_voice_packs_make_it_bigger(tmp_path):
    assert "more with others" in _backend(tmp_path).page_notice[0].lower()
