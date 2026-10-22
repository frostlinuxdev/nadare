# SPDX-License-Identifier: GPL-3.0-or-later
"""The recommended (tested, pinned) runner: lookup, reuse and never re-downloading."""

from __future__ import annotations

from pathlib import Path

import pytest

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


@pytest.mark.parametrize(
    ("name", "expected"),
    [
        ("dwproton-11.0-15-x86_64", True),
        ("dwproton-11.0-13-x86_64", False),
        ("dwproton-11.0-16-x86_64", False),
        ("dwproton-11.0-150-x86_64", False),
        ("dwproton-11.0-15.1-x86_64", False),
        ("proton-cachyos-11.0-20260703-slr-x86_64", True),
        ("GE-Proton11-7-x86_64", True),
        ("GE-Proton11-70-x86_64", False),
        ("UMU-Proton-10.0-4", False),
    ],
)
def test_only_the_exact_pinned_release_is_recommended(name, expected):
    assert runners.is_recommended(Path("/anywhere") / name) is expected


def test_pinned_tag_comes_from_the_tested_table():
    assert runners.pinned_tag("dwproton") == "dwproton-11.0-15"
    assert runners.pinned_tag("nope") is None


def test_installed_pinned_ignores_newer_and_older_builds(root):
    _make(root, "dwproton-11.0-13-x86_64")
    _make(root, "dwproton-11.0-16-x86_64")
    assert runners.installed_pinned("dwproton") is None
    pinned = _make(root, "dwproton-11.0-15-x86_64")
    assert runners.installed_pinned("dwproton") == pinned


def test_ensure_pinned_reuses_an_installed_copy_without_downloading(root, monkeypatch):
    pinned = _make(root, "dwproton-11.0-15-x86_64")
    monkeypatch.setattr(runners, "install", lambda *a, **k: pytest.fail("downloaded"))
    assert runners.ensure_pinned("dwproton") == pinned
    assert runners.ensure_pinned("dwproton") == pinned


def test_ensure_pinned_downloads_the_pin_once_when_only_other_builds_exist(root, monkeypatch):
    _make(root, "dwproton-11.0-16-x86_64")
    calls: list[tuple] = []

    def fake_install(family, *, latest=False, emit=None, cancel=None):
        calls.append((family, latest))
        return _make(root, "dwproton-11.0-15-x86_64")

    monkeypatch.setattr(runners, "install", fake_install)
    first = runners.ensure_pinned("dwproton")
    second = runners.ensure_pinned("dwproton")
    assert first == second
    assert calls == [("dwproton", False)], "the pin is downloaded once and never as --latest"


def test_a_pin_already_in_steams_tool_dir_is_reused(root, tmp_path, monkeypatch):
    steam = tmp_path / "steam" / "GE-Proton11-7-x86_64"
    steam.mkdir(parents=True)
    (steam / "proton").write_text("#!/bin/sh\n")
    monkeypatch.setattr(runners, "steam_runners", lambda: [steam])
    monkeypatch.setattr(runners, "install", lambda *a, **k: pytest.fail("downloaded"))
    assert runners.ensure_pinned("ge") == steam


def test_the_pin_is_not_called_outdated_because_a_newer_build_exists(root, monkeypatch):
    pinned = _make(root, "dwproton-11.0-15-x86_64")
    newer = {"tag": "dwproton-11.0-16", "published_at": "2026-10-18T00:00:00Z"}
    monkeypatch.setattr(runners, "_latest_release", lambda spec, now=None: newer)
    info = runners.runner_info(pinned)
    assert info["recommended"] is True
    assert info["outdated"] is False
    assert info["latest_tag"] == "dwproton-11.0-16"
    other = _make(root, "dwproton-11.0-13-x86_64")
    info = runners.runner_info(other)
    assert info["recommended"] is False and info["outdated"] is True


# ---- audit fixes ---------------------------------------------------------------------
def test_a_family_id_resolves_to_the_pinned_build_everywhere(root):
    pinned = _make(root, "dwproton-11.0-15-x86_64")
    _make(root, "dwproton-11.0-16-x86_64")
    assert runners.resolve_runner("dwproton") == pinned


def test_a_family_id_that_is_not_installed_is_a_clear_error(root):
    _make(root, "dwproton-11.0-16-x86_64")
    with pytest.raises(runners.RunnerError, match="not installed yet"):
        runners.resolve_runner("dwproton")


def test_runner_family_judges_the_folder_name_not_its_parents():
    assert runners.runner_family("/home/ge-proton-tests/CustomWine") is None
    assert runners.runner_family("/home/x/GE-Proton11-7-x86_64") == "ge"
    assert runners.runner_family("dwproton-11.0-15-x86_64") == "dwproton"


def test_usage_by_a_tag_that_a_backend_matches_by_substring(root):
    ge = _make(root, "GE-Proton11-7-x86_64")
    other = _make(root, "dwproton-11.0-13-x86_64")
    usage = runners.runner_usage({"czn": {"backend": "stove-czn", "runner": "GE-Proton11-7"}})
    assert usage[str(ge)] == ["czn"]
    assert usage[str(other)] == []
