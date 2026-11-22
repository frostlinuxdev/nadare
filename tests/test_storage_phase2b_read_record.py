# SPDX-License-Identifier: GPL-3.0-or-later
"""#1372 phase 2b: ``location.game_folder`` answers from the stored record when it is current.

The record (phase 2a) is rebuilt on every save from the legacy keys and keeps a copy of them
under ``from``. When ``from`` still matches, the record's folder is the answer; a stale or
missing record falls back to the legacy keys (an older Nadare edited them). The disk check that
was always made stays: a link put at ``profiles/<name>`` is never the game's folder (B1). The
golden snapshot (tests/data/storage_shapes_golden.json) must not change.
"""

from __future__ import annotations

import copy
import json
from pathlib import Path

from nadare.core import location, paths
from nadare.core.config import Config

KIND = "direct-aethergazer"


def _prefix(root: Path) -> Path:
    (root / "drive_c").mkdir(parents=True)
    (root / "system.reg").write_text("reg")
    return root


def _library_profile(home: Path) -> dict:
    _prefix(home / "prefix")
    (home / "install").mkdir()
    return {
        "backend": KIND,
        "title": "Aether Gazer",
        "install_dir": str(home / "install"),
        "prefix": str(home / "prefix"),
        "owned_trees": ["install_dir", "prefix"],
        "owned_dirs": [str(home)],
    }


def _with_record(profile: dict, name: str = "ag") -> dict:
    out = copy.deepcopy(profile)
    location.refresh_record(out, name)
    assert location.record_is_current(out)
    return out


def test_a_current_record_is_the_answer(tmp_path):
    home = tmp_path / "lib" / "Nadare" / "Aether Gazer"
    profile = _with_record(_library_profile(home))
    elsewhere = tmp_path / "lib2" / "Nadare" / "Aether Gazer"
    profile["location"]["folder"] = str(elsewhere)  # only the record says so
    assert location.game_folder(profile, "ag") == elsewhere


def test_a_current_record_with_no_folder_means_none(tmp_path):
    home = tmp_path / "lib" / "Nadare" / "Aether Gazer"
    profile = _with_record(_library_profile(home))
    profile["location"]["folder"] = None
    assert location.game_folder(profile, "ag") is None  # the safe direction: not Nadare's


def test_a_stale_record_falls_back_to_the_legacy_keys(tmp_path):
    home = tmp_path / "lib" / "Nadare" / "Aether Gazer"
    profile = _with_record(_library_profile(home))
    profile["location"]["folder"] = str(tmp_path / "lib2" / "Nadare" / "X")
    moved = tmp_path / "lib3" / "Nadare" / "Aether Gazer"
    profile["owned_dirs"] = [str(moved)]  # an older Nadare moved it; the record is stale now
    assert not location.record_is_current(profile)
    assert location.game_folder(profile, "ag") == moved


def test_a_damaged_record_folder_falls_back_to_the_legacy_keys(tmp_path):
    home = tmp_path / "lib" / "Nadare" / "Aether Gazer"
    base = _library_profile(home)
    for bad in ("", "relative/Nadare/x", 7, ["x"], str(tmp_path / "not-a-library" / "x")):
        profile = _with_record(base)
        profile["location"]["folder"] = bad
        assert location.game_folder(profile, "ag") == home, bad


def test_a_record_is_ignored_for_a_game_nadare_does_not_lay_out(tmp_path):
    home = tmp_path / "lib" / "Nadare" / "Aether Gazer"
    profile = _with_record(_library_profile(home))
    profile["backend"] = "wine"
    assert location.game_folder(profile, "ag") is None


def test_an_explicit_default_root_plans_from_the_legacy_keys(tmp_path):
    home = tmp_path / "lib" / "Nadare" / "Aether Gazer"
    profile = _with_record(_library_profile(home))
    profile["location"]["folder"] = str(tmp_path / "lib2" / "Nadare" / "X")
    assert location.game_folder(profile, "ag", default_root=tmp_path / "planned") == home


def test_a_link_put_at_the_default_folder_after_saving_never_counts(tmp_path, monkeypatch):
    """B1 stays: the record said profiles/<name>; a link put there since is not Nadare's."""
    monkeypatch.setenv("XDG_DATA_HOME", str(tmp_path / "data"))
    root = paths.profiles_dir()
    root.mkdir(parents=True, exist_ok=True)
    profile = {"backend": KIND, "title": "Aether Gazer"}  # the backend's own folders
    profile = _with_record(profile, "ag")
    assert profile["location"]["folder"] == str(root / "ag")
    assert location.game_folder(profile, "ag") == root / "ag"
    documents = tmp_path / "home" / "Documents"
    documents.mkdir(parents=True)
    (root / "ag").symlink_to(documents)
    assert location.game_folder(profile, "ag") is None


def test_a_saved_config_reads_its_records_and_keeps_every_answer(tmp_path, monkeypatch):
    """The golden snapshot, read back through the records (none stale after a save)."""
    import test_storage_shapes as shapes  # noqa: PLC0415 - the snapshot test's own helpers

    golden = json.loads(shapes.GOLDEN.read_text())
    read = []
    real = location.record_is_current

    def counting(profile):
        current = real(profile)
        read.append(current)
        return current

    monkeypatch.setattr(location, "record_is_current", counting)
    for shape in sorted(shapes.SHAPES):
        base = tmp_path / shape
        cfg = shapes._world(shape, base, monkeypatch)
        cfg.save()
        cfg = Config.load(cfg.path)
        text = shapes._Text(base)
        got = {name: shapes._answers(cfg, name, text) for name in sorted(cfg.profiles)}
        assert got == golden[shape], shape
    assert any(read), "no answer came from a stored record"
