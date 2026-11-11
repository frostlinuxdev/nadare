# SPDX-License-Identifier: GPL-3.0-or-later
"""#974 item 1: a runner chosen under a profile's ``extra`` mapping is in use.

Backends read settings from the top level first, then from ``extra`` (``profile_setting``), and
config migration leaves ``extra.runner`` in place. Runner usage read only the top level, so such a
runner was listed as unused and could be deleted, and the next launch failed.
"""

from __future__ import annotations

from pathlib import Path

import pytest

from nadare.backends.wine import profile_setting
from nadare.core import runners


@pytest.fixture
def root(tmp_path, monkeypatch):
    folder = tmp_path / "runners"
    folder.mkdir()
    monkeypatch.setattr(runners.paths, "runners_dir", lambda: folder)
    monkeypatch.setattr(runners, "steam_runners", lambda: [])
    return folder


def _make(root: Path, name: str) -> Path:
    d = root / name
    d.mkdir()
    (d / "proton").write_text("#!/bin/sh\n")
    return d


def test_a_runner_under_extra_is_in_use(root):
    ge = _make(root, "GE-Proton9-1")
    usage = runners.runner_usage({"g": {"backend": "wine", "extra": {"runner": str(ge)}}})
    assert usage[str(ge)] == ["g"]


def test_proton_runner_under_extra_counts_too(root):
    ge = _make(root, "GE-Proton9-1")
    usage = runners.runner_usage({"g": {"backend": "wine", "extra": {"proton_runner": ge.name}}})
    assert usage[str(ge)] == ["g"]


@pytest.mark.parametrize(
    "profile",
    [
        # The top level wins over extra, key by key, as profile_setting reads it.
        {"backend": "wine", "runner": "A", "extra": {"runner": "B"}},
        {"backend": "wine", "proton_runner": "A", "extra": {"runner": "B"}},
        {"backend": "wine", "extra": {"proton_runner": "A", "runner": "B"}},
        {"backend": "wine", "runner": "", "extra": {"runner": "A"}},
    ],
)
def test_usage_follows_the_same_precedence_as_the_backends(root, profile):
    a = _make(root, "A")
    b = _make(root, "B")
    backend_choice = profile_setting(profile, "proton_runner") or profile_setting(profile, "runner")
    assert backend_choice == "A"
    usage = runners.runner_usage({"g": profile})
    assert usage[str(a)] == ["g"]
    assert usage[str(b)] == []


def test_a_non_mapping_extra_is_ignored(root):
    ge = _make(root, "GE-Proton9-1")
    usage = runners.runner_usage({"g": {"backend": "wine", "extra": "junk"}})
    assert usage[str(ge)] == []
