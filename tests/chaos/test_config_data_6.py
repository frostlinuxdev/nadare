# SPDX-License-Identifier: GPL-3.0-or-later
"""A save must not overwrite valid JSON that has the wrong shape without keeping a copy."""

from __future__ import annotations

import json

import pytest

from nadare.core.config import Config


@pytest.mark.parametrize(
    "damaged",
    [
        {"profiles": ["not", "profiles"]},
        ["not", "an", "object"],
    ],
    ids=["profiles-not-object", "root-not-object"],
)
def test_save_keeps_a_copy_of_a_wrong_shaped_config(tmp_path, damaged):
    path = tmp_path / "config.json"
    config = Config.load(path)
    config.add_profile("local")
    config.save()

    path.write_text(json.dumps(damaged), encoding="utf-8")
    config.data["language"] = "de"
    config.save()

    copies = sorted(tmp_path.glob("config.json.corrupt-*"))
    print("damaged on disk:", damaged)
    print("kept copies:", [copy.name for copy in copies])
    assert copies, "the wrong-shaped config was overwritten with no backup"
    assert json.loads(copies[0].read_text(encoding="utf-8")) == damaged
