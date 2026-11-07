# SPDX-License-Identifier: GPL-3.0-or-later
"""``runners.remove`` deletes one folder directly inside the runners folder, nothing else (#860)."""

from __future__ import annotations

import pytest

from nadare.core import runners


@pytest.fixture
def tree(tmp_path, monkeypatch):
    """A Nadare data folder with a runners folder and precious sibling data (#860)."""
    data = tmp_path / "data" / "nadare"
    root = data / "runners"
    (root / "GE-Proton10-1").mkdir(parents=True)
    (root / "GE-Proton10-1" / "proton").write_text("x")
    (data / "profiles" / "game").mkdir(parents=True)
    (data / "profiles" / "game" / "save.dat").write_text("precious")
    monkeypatch.setattr(runners.paths, "runners_dir", lambda: root)
    return data, root


def _intact(data, root):
    return (data / "profiles" / "game" / "save.dat").read_text() == "precious" and root.is_dir()


@pytest.mark.parametrize(
    "name", ["..", ".", "", "../profiles", "GE-Proton10-1/..", "./..", "a/../.."]
)
def test_a_relative_name_with_dots_is_refused(tree, name):
    data, root = tree
    assert runners.remove(name) is False
    assert _intact(data, root)
    assert (root / "GE-Proton10-1").is_dir()


def test_an_absolute_path_through_dotdot_is_refused(tree):
    data, root = tree
    assert runners.remove(str(root / "..")) is False
    assert runners.remove(str(root / "GE-Proton10-1" / ".." / "..")) is False
    assert runners.remove(str(root / ".." / "profiles")) is False
    assert _intact(data, root)


def test_the_runners_folder_itself_is_refused(tree):
    data, root = tree
    assert runners.remove(str(root)) is False
    assert _intact(data, root)


def test_a_runner_is_still_removed_by_name_and_by_path(tree):
    data, root = tree
    (root / "second").mkdir()
    assert runners.remove("GE-Proton10-1") is True
    assert runners.remove(str(root / "second")) is True
    assert _intact(data, root)
    assert not (root / "GE-Proton10-1").exists() and not (root / "second").exists()


def test_a_symlink_to_a_runner_is_unlinked_not_followed(tree, tmp_path):
    data, root = tree
    outside = tmp_path / "elsewhere"
    outside.mkdir()
    (outside / "keep.txt").write_text("keep")
    (root / "link").symlink_to(outside)
    assert runners.remove("link") is True
    assert (outside / "keep.txt").read_text() == "keep"
    assert not (root / "link").exists() and not (root / "link").is_symlink()
