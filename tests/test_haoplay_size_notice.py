# SPDX-License-Identifier: GPL-3.0-or-later
"""The Haoplay build's real size: measured, not the old guess of 60 GB."""

from __future__ import annotations

from _backend_helpers import make_context

from nadare.backends import direct


def _backend(tmp_path):
    return direct.Gfl2HaoplayBackend(
        make_context(tmp_path, "direct-gfl2-haoplay", {"prefix": str(tmp_path / "p")})
    )


def test_the_page_notice_gives_the_measured_size_not_the_old_guess(tmp_path):
    text = _backend(tmp_path).page_notice[0]
    assert "60 GB" not in text
    assert "about 90 GB" in text  # 87.7 GB of game files for one voice language + the prefix


def test_the_notice_says_other_voice_packs_make_it_bigger(tmp_path):
    assert "more with others" in _backend(tmp_path).page_notice[0].lower()
