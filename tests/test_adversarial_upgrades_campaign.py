# SPDX-License-Identifier: GPL-3.0-or-later
"""Historical config code -> candidate twice -> historical code -> candidate twice.

The round trip is an exploratory compatibility test, not a new downgrade policy.
Only local git tags and small synthetic installations are used.
"""

from __future__ import annotations

import io
import json
import os
import subprocess
import sys
import tarfile
import tempfile
from pathlib import Path

import pytest

from nadare.core import libraries
from nadare.core.config import Config

TAGS = ["v0.1.0b21", "v0.1.0b22", "v0.1.0b23", "v0.1.0b24"]


@pytest.fixture(scope="module")
def historical_sources():
    repo = Path(__file__).resolve().parents[1]
    missing = [
        tag
        for tag in TAGS
        if subprocess.run(
            ["git", "-C", str(repo), "rev-parse", "--verify", "-q", f"{tag}^{{commit}}"],
            capture_output=True,
            check=False,
        ).returncode
    ]
    if missing:  # shallow CI checkouts carry no release tags
        pytest.skip(f"release tags not in this checkout: {', '.join(missing)}")
    with tempfile.TemporaryDirectory(
        prefix="nadare-upgrade-campaign-", dir=os.environ.get("NADARE_CAMPAIGN_SCRATCH")
    ) as folder:
        sources = {}
        for tag in TAGS:
            archive = subprocess.check_output(["git", "-C", str(repo), "archive", tag, "src"])
            dest = Path(folder) / tag
            dest.mkdir()
            with tarfile.open(fileobj=io.BytesIO(archive)) as tar:
                tar.extractall(dest, filter="data")
            sources[tag] = dest / "src"
        yield sources


def historical_save(source, path, profiles=None):
    script = """
import json,sys
from pathlib import Path
from nadare.core.config import Config
path = Path(sys.argv[1])
cfg = Config.load(path)
profiles = json.loads(sys.argv[2])
if profiles is not None:
    for name, values in profiles.items():
        cfg.add_profile(name, **values)
cfg.data['campaign_future_field'] = {'preserve': [1, 'unknown']}
cfg.save()
"""
    env = dict(os.environ, PYTHONPATH=str(source), QT_QPA_PLATFORM="offscreen")
    subprocess.run(
        [sys.executable, "-c", script, str(path), json.dumps(profiles)],
        env=env,
        check=True,
        timeout=15,
        capture_output=True,
        text=True,
    )


@pytest.mark.parametrize("tag", TAGS)
@pytest.mark.parametrize("layout", ["managed", "imported", "split", "same_title_two_libraries"])
def test_historical_data_survives_repeated_upgrade_and_exploratory_roundtrip(
    tmp_path, historical_sources, tag, layout
):
    lib1, lib2 = tmp_path / "disk-one", tmp_path / "disk-two"
    container = lib1 / "Nadare"
    if tag == "v0.1.0b21":
        container /= "Nadare"
    home = container / "Game"
    install, prefix = home / "install", home / "prefix"
    if layout == "imported":
        install, prefix = tmp_path / "imports" / "Game", tmp_path / "own-prefix"
    elif layout == "split":
        prefix = lib2 / "Prefixes" / "Game"
    profiles = {
        "g": {
            "backend": "direct-bd2",
            "title": "Game",
            "install_dir": str(install),
            "prefix": str(prefix),
        }
    }
    if layout == "managed":
        profiles["g"]["owned_trees"] = ["install_dir", "prefix"]
    if layout == "same_title_two_libraries":
        profiles["other"] = {
            "backend": "direct-bd2",
            "title": "Game",
            "install_dir": str(lib2 / "Nadare" / "Game" / "install"),
            "prefix": str(lib2 / "Nadare" / "Game" / "prefix"),
        }
    original = {}
    for name, profile in profiles.items():
        for key in ("install_dir", "prefix"):
            folder = Path(profile[key])
            folder.mkdir(parents=True)
            marker = folder / ("game.exe" if key == "install_dir" else "save.dat")
            marker.write_bytes(f"{name}:{key}:keep".encode())
            original[marker] = marker.read_bytes()
    config = tmp_path / "config.json"
    historical_save(historical_sources[tag], config, profiles)
    # Each phase uses fresh Config objects, not cached in-memory state.
    for phase in range(2):
        for _restart in range(2):
            cfg = Config.load(config)
            libraries.backfill(cfg)
            cfg.save()
            assert set(cfg.profiles) == set(profiles)
            assert cfg.data["campaign_future_field"] == {"preserve": [1, "unknown"]}
            for name, profile in profiles.items():
                for key in ("install_dir", "prefix"):
                    assert cfg.profiles[name][key] == profile[key]
            for marker, content in original.items():
                assert marker.read_bytes() == content
        if phase == 0:
            historical_save(historical_sources[tag], config)
