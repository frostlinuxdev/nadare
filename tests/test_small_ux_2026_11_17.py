# SPDX-License-Identifier: GPL-3.0-or-later
# Copyright (C) 2026 Nadare contributors
"""Two small things the maintainer saw on 2026-11-17.

1. An older DW-Proton build was labelled "untested"; it was tested (it was the pin), it is just
   older. Only a build newer than the pin is untested.
2. "Folders of removed games" listed empty folders as "may hold saves".
"""

from __future__ import annotations

from pathlib import Path

import pytest

from nadare.core import paths, profilefolders, runners


# ---- 1. runner standing ------------------------------------------------------
def _pinned_numbers() -> str:
    tag = runners.pinned_tag("dwproton")
    assert tag and tag.startswith("dwproton-")
    return tag.removeprefix("dwproton-")


def _bump(version: str, delta: int) -> str:
    head, _, last = version.rpartition("-")
    return f"{head}-{int(last) + delta}"


def test_the_pin_is_recommended_an_older_build_is_older_a_newer_one_untested():
    pinned = _pinned_numbers()
    assert runners.standing(Path(f"dwproton-{pinned}-x86_64")) == "recommended"
    assert runners.standing(Path(f"dwproton-{_bump(pinned, -1)}-x86_64")) == "older"
    assert runners.standing(Path(f"dwproton-{_bump(pinned, -4)}-x86_64")) == "older"
    assert runners.standing(Path(f"dwproton-{_bump(pinned, 1)}-x86_64")) == "untested"
    assert runners.standing(Path("MyCustomProton")) == "custom"


def test_the_arch_suffix_does_not_count_as_a_version():
    # "x86_64" carries digits; a newer build must not look older because of them.
    pinned = _pinned_numbers()
    newer = f"dwproton-{_bump(pinned, 1)}-x86_64"
    assert runners.standing(Path(newer)) == "untested"
    assert runners.standing(Path(f"dwproton-{_bump(pinned, -1)}")) == "older"


def test_the_runner_labels_say_older_not_untested():
    from nadare.gui.runners_dialog import status_text
    from nadare.gui.settings_parts.extras import _runner_label

    old = Path(f"dwproton-{_bump(_pinned_numbers(), -2)}-x86_64")
    assert status_text(old) == "older build"
    assert _runner_label(old).endswith("· older")
    new = Path(f"dwproton-{_bump(_pinned_numbers(), 1)}-x86_64")
    assert status_text(new) == "newer (untested)"
    assert _runner_label(new).endswith("· untested")


# ---- 2. empty folders of removed games --------------------------------------
@pytest.fixture
def root() -> Path:
    folder = paths.profiles_dir()
    folder.mkdir(parents=True, exist_ok=True)
    return folder


def test_a_folder_that_holds_nothing_is_not_offered(root):
    (root / "empty").mkdir()
    (root / "only-folders" / "prefix" / "drive_c").mkdir(parents=True)
    kept = root / "kept"
    (kept / "prefix").mkdir(parents=True)
    (kept / "prefix" / "save.dat").write_bytes(b"")  # an empty file still counts
    linked = root / "linked"
    linked.mkdir()
    (linked / "link").symlink_to(root / "empty")  # a link is something to look at
    found = [item.path.name for item in profilefolders.find({}, [])]
    assert found == ["kept", "linked"]
    # Nothing is deleted by looking.
    assert (root / "empty").is_dir() and (root / "only-folders" / "prefix").is_dir()


# ---- 3. every catalog game has seed art -------------------------------------
def test_every_catalog_game_has_art_in_the_seed():
    """DNA and Snowbreak shipped with no index art (blank card): the seed never got them."""
    import json

    from nadare.core import artindex
    from nadare.core import backends as core_backends

    seed = json.loads(
        (Path(__file__).resolve().parents[1] / "docs" / "art-index.seed.json").read_text("utf-8")
    )["games"]
    missing = sorted(set(core_backends.CATALOG_KINDS) - set(seed))
    assert not missing, f"no art in docs/art-index.seed.json for {missing}"
    for kind in core_backends.CATALOG_KINDS:
        assert artindex.parse({"schema": 1, "games": {kind: seed[kind]}}).get(kind), kind
