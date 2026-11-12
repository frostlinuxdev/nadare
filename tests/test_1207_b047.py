# SPDX-License-Identifier: GPL-3.0-or-later
"""The page label must name the quality the WuWa backend actually uses."""

from __future__ import annotations

from nadare.core import backends


def test_the_quality_label_matches_what_the_backend_uses():
    # wuwa.quality_of() lowercases and strips, so "UHD" is the UHD tier.
    label = backends.install_choice_label("direct-wuwa", {"wuwa_quality": "UHD"})
    assert label == "Resource quality: UHD"
