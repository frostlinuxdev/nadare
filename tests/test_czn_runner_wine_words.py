# SPDX-License-Identifier: GPL-3.0-or-later
"""No game runs on system Wine by a profile word (maintainer decision 2026-11-13, #1310 W9-1).

``runner: "wine"`` / ``"system"`` / ``"none"`` used to mean plain system Wine for CZN and
"the default runner" for every other Proton game. One meaning now: the default runner.
"""

from __future__ import annotations

from pathlib import Path

import pytest
from _czn_test_helpers import _make_backend, make_backend_context  # noqa: F401


@pytest.mark.parametrize("word", ["wine", "system", "none", "Wine"])
def test_a_wine_word_means_the_default_runner(
    make_backend_context, tmp_path, monkeypatch, word  # noqa: F811
):
    backend = _make_backend(make_backend_context, tmp_path / "prefix")
    pinned = tmp_path / "runners" / "dwproton-11.0-15"
    monkeypatch.setattr(backend, "_pinned_runner", lambda rid: Path(pinned))
    backend.ctx.profile["runner"] = word
    assert backend.resolve_runner() == (str(pinned), "proton")
