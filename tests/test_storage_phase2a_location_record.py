# SPDX-License-Identifier: GPL-3.0-or-later
"""#1372 phase 2a: every save stores one ``location`` record, mirrored from the legacy keys.

In 2a the legacy keys stay the truth; ``location`` is rebuilt from them on every save, and it
records the legacy values it came from (``from``) so a later reader can tell a record an
older Nadare left stale (it edits only legacy keys) from a current one.
"""

from __future__ import annotations

import io
import json
import os
import subprocess
import sys
import tarfile
from pathlib import Path

import pytest

from nadare.core import location, paths
from nadare.core.config import Config

KIND = "direct-aethergazer"


def _prefix(root: Path) -> Path:
    (root / "drive_c").mkdir(parents=True)
    (root / "system.reg").write_text("reg")
    return root


def _library_game(tmp_path: Path) -> tuple[Path, dict]:
    home = tmp_path / "lib" / "Nadare" / "Aether Gazer"
    _prefix(home / "prefix")
    (home / "install").mkdir()
    profile = {
        "backend": KIND,
        "title": "Aether Gazer",
        "install_dir": str(home / "install"),
        "prefix": str(home / "prefix"),
        "owned_trees": ["install_dir", "prefix"],
        "owned_dirs": [str(home)],
    }
    return home, profile


def _saved(tmp_path: Path, profiles: dict) -> dict:
    cfg = Config.load(tmp_path / "config.json")
    for name, profile in profiles.items():
        cfg.profiles[name] = profile
    cfg.save()
    return json.loads((tmp_path / "config.json").read_text())["profiles"]


def test_a_library_game_gets_one_record_like_its_manifest(tmp_path):
    home, profile = _library_game(tmp_path)
    record = _saved(tmp_path, {"aether": profile})["aether"]["location"]
    assert record["folder"] == str(home)
    assert record["install"] == "install"
    assert record["prefix"] == "prefix"
    assert record["made_by_nadare"] == ["install", "prefix"]
    assert record["from"]["prefix"] == str(home / "prefix")


def test_a_player_folder_outside_is_stored_absolute_and_not_nadares(tmp_path):
    install = tmp_path / "Games" / "WuWa"
    install.mkdir(parents=True)
    prefix = _prefix(tmp_path / "Games" / "wuwa-prefix")
    profile = {"backend": "direct-wuwa", "install_dir": str(install), "prefix": str(prefix)}
    record = _saved(tmp_path, {"wuwa": profile})["wuwa"]["location"]
    assert record["folder"] is None
    assert record["install"] == str(install) and record["prefix"] == str(prefix)
    assert record["made_by_nadare"] == []


def test_a_custom_wine_game_gets_no_record(tmp_path):
    """location models catalog games only; custom games keep the legacy keys alone."""
    profile = {"backend": "wine", "prefix": str(_prefix(tmp_path / "pfx"))}
    assert "location" not in _saved(tmp_path, {"mine": profile})["mine"]


def test_a_legacy_edit_rebuilds_the_record_on_the_next_save(tmp_path):
    home, profile = _library_game(tmp_path)
    _saved(tmp_path, {"aether": profile})
    cfg = Config.load(tmp_path / "config.json")
    moved = _prefix(tmp_path / "elsewhere" / "pfx")
    cfg.profiles["aether"]["prefix"] = str(moved)
    cfg.save()
    record = json.loads((tmp_path / "config.json").read_text())["profiles"]["aether"]["location"]
    assert record["prefix"] == str(moved)
    assert record["from"]["prefix"] == str(moved)


def test_a_stale_record_is_detected_from_its_source(tmp_path):
    """What 2b relies on: a record whose ``from`` no longer matches the legacy keys is stale."""
    home, profile = _library_game(tmp_path)
    stored = _saved(tmp_path, {"aether": profile})["aether"]
    assert location.record_is_current(stored)
    stored["prefix"] = str(tmp_path / "an older Nadare moved it")
    assert not location.record_is_current(stored)
    stored.pop("location")
    assert not location.record_is_current(stored)


def test_the_record_does_not_change_any_storage_answer(tmp_path, monkeypatch):
    """Readers are unchanged in 2a: saving (and so storing location) keeps every answer."""
    import test_storage_shapes as shapes  # noqa: PLC0415 - the snapshot test's own helpers

    golden = json.loads(shapes.GOLDEN.read_text())
    for shape in sorted(shapes.SHAPES):
        base = tmp_path / shape
        cfg = shapes._world(shape, base, monkeypatch)
        cfg.save()
        cfg = Config.load(cfg.path)
        text = shapes._Text(base)
        got = {name: shapes._answers(cfg, name, text) for name in sorted(cfg.profiles)}
        assert got == golden[shape], shape


def test_b24_opens_and_saves_a_config_with_records(tmp_path):
    """Downgrade window: the released b24 loads a config with ``location`` and keeps working."""
    repo = Path(__file__).resolve().parents[1]
    tag = "v0.1.0b24"
    if subprocess.run(
        ["git", "-C", str(repo), "rev-parse", "--verify", "-q", f"{tag}^{{commit}}"],
        capture_output=True,
        check=False,
    ).returncode:
        pytest.skip("release tags not in this checkout")
    home, profile = _library_game(tmp_path)
    _saved(tmp_path, {"aether": profile})
    old = tmp_path / "b24"
    old.mkdir()
    archive = subprocess.check_output(["git", "-C", str(repo), "archive", tag, "src"])
    with tarfile.open(fileobj=io.BytesIO(archive)) as tar:
        tar.extractall(old, filter="data")
    script = (
        "import json, sys\n"
        "from pathlib import Path\n"
        "from nadare.core.config import Config\n"
        "cfg = Config.load(Path(sys.argv[1]))\n"
        "p = cfg.profiles['aether']\n"
        "p['title'] = 'Edited by b24'\n"
        "cfg.save()\n"
        "print(json.dumps({'prefix': p['prefix'], 'install': p['install_dir']}))\n"
    )
    env = {**os.environ, "PYTHONPATH": str(old / "src")}
    out = subprocess.run(
        [sys.executable, "-c", script, str(tmp_path / "config.json")],
        env=env,
        capture_output=True,
        text=True,
        check=True,
    )
    seen = json.loads(out.stdout.strip().splitlines()[-1])
    assert seen == {"prefix": str(home / "prefix"), "install": str(home / "install")}
    after = json.loads((tmp_path / "config.json").read_text())["profiles"]["aether"]
    assert after["title"] == "Edited by b24"
    # b24 may keep or drop the record; either way the next save here rebuilds a current one.
    cfg = Config.load(tmp_path / "config.json")
    cfg.save()
    assert location.record_is_current(
        json.loads((tmp_path / "config.json").read_text())["profiles"]["aether"]
    )
    assert paths.profiles_dir()  # the isolated data dir was used throughout
