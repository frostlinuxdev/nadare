# SPDX-License-Identifier: GPL-3.0-or-later
"""A hand-edited backend value must fail as a BackendError, not a TypeError."""

from __future__ import annotations

import pytest

from nadare.core import backends
from nadare.core.config import Config


def test_create_rejects_a_non_string_backend_value(tmp_path):
    config = Config.load(tmp_path / "config.json")
    config.add_profile("p", backend=["wine"])
    with pytest.raises(backends.BackendError):
        backends.create("p", config)
