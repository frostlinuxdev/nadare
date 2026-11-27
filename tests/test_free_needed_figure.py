# SPDX-License-Identifier: GPL-3.0-or-later
"""One free-space figure: what the check needs is what every keep-free text says (user 11-24)."""

from __future__ import annotations

import pytest

from nadare.core import diskspace
from nadare.core.backends import BackendError


def test_free_needed_is_the_need_plus_the_margin():
    assert diskspace.free_needed(100 << 30) == int((100 << 30) * (1 + diskspace.DEFAULT_MARGIN))
    assert diskspace.free_needed(100, margin=0) == 100
    assert diskspace.free_needed(100, margin=-1) == 100
    assert diskspace.free_needed(0) == 0


def test_the_check_names_free_needed(monkeypatch, tmp_path):
    monkeypatch.setattr(diskspace, "free_bytes", lambda path: 1)
    with pytest.raises(BackendError) as err:
        diskspace.check_space(tmp_path, 80 << 30)
    assert f"need {diskspace.human_size(diskspace.free_needed(80 << 30))}," in err.value.message


@pytest.mark.parametrize(
    ("module", "cls", "peak"),
    [
        ("nadare.backends.direct_bpsr", "BpsrBackend", "PEAK_BYTES"),
        ("nadare.backends.direct_games", "DnaBackend", "DNA_PEAK_BYTES"),
    ],
)
def test_page_notices_say_the_figure_the_check_needs(module, cls, peak):
    import importlib

    mod = importlib.import_module(module)
    backend = getattr(mod, cls)
    text = backend.page_notice[0]
    figure = diskspace.human_size(diskspace.free_needed(getattr(mod, peak)))
    assert f"keep about {figure} free" in text
