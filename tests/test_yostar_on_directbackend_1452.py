# SPDX-License-Identifier: GPL-3.0-or-later
"""#1452 step 3d: Arknights and Stella Sora are DirectBackend games.

Their launcher route is retired (3c), so they are direct-download games like the others: the
same base class, the game's own executable for process detection (never a Yostar launcher
left in the folder), no predownload and no Kuro tidy. What stays Yostar's: the folder
``YostarGames/<Game>_EN``, the CI-signed manifest as the plan (no archive pins: the manifest
already carries every file's hash), the display/DXVK defaults, news and media.
"""

from __future__ import annotations

import json
from pathlib import Path

import pytest
from _backend_helpers import make_context

from nadare.backends import arknights as ak
from nadare.backends import direct
from nadare.backends import stellasora as ss
from nadare.backends.arknights import ArknightsBackend
from nadare.backends.stellasora import StellaSoraBackend
from nadare.core import archivepins, yostar
from nadare.core.backends import BackendError

BASE = "https://cdn.yo-star.com/assets"


def _manifest(tmp_path: Path, tag: str, files: dict[str, bytes]) -> Path:
    payload = {
        "game_tag": tag,
        "version": "2.0.0",
        "base_url": BASE,
        "generated_at": "2026-01-01T00:00:00Z",
        "files": [
            {"path": path, "size": len(data), "crc64": yostar.crc64(data)}
            for path, data in files.items()
        ],
    }
    path = tmp_path / f"{tag}.json"
    path.write_text(json.dumps(payload), encoding="utf-8")
    return path


def _backend(cls, tmp_path: Path, kind: str, profile: dict | None = None):
    return cls(make_context(tmp_path, kind, dict(profile or {}), "game"))


@pytest.mark.parametrize("cls", [ArknightsBackend, StellaSoraBackend])
def test_both_yostar_games_are_direct_games(cls):
    assert issubclass(cls, direct.DirectBackend)
    assert cls.predownload_factory is None
    assert cls.tidies_retired is False
    # The launcher route's attributes are gone from the Yostar classes.
    for name in ("default_installer_url", "installer_api"):
        assert name not in vars(ArknightsBackend), name
    assert cls.launcher_names == (cls.direct_exe,)


def test_the_folders_and_executables_stay_where_players_have_them():
    assert ArknightsBackend.install_subdir == Path("drive_c/YostarGames/Arknights_EN")
    assert StellaSoraBackend.install_subdir == Path("drive_c/YostarGames/StellaSora_EN")
    assert ArknightsBackend.direct_exe == "Arknights.exe"
    assert StellaSoraBackend.direct_exe == "StellaSora.exe"
    assert ArknightsBackend.default_display == "wayland"
    assert StellaSoraBackend.default_display == "x11"
    assert "enablePresentTiming = False" in ArknightsBackend.dxvk_config
    assert StellaSoraBackend.dxvk_config == ""
    assert ArknightsBackend.official_media is True
    assert ArknightsBackend.supports_secure_mode is True


def test_stella_keeps_its_own_display_help():
    """DirectBackend renames an X11 game's display help; Stella's names the swipe reason."""
    option = next(o for o in StellaSoraBackend.options if o.key == "display")
    assert "swipe" in option.help
    assert option.choices[0] == ("Auto (X11)", "auto")
    # A direct game that still says "Auto (Wayland)" for an X11 default is corrected as before.
    wuwa = next(o for o in direct.WutheringWavesBackend.options if o.key == "display")
    assert wuwa.choices[0] == ("Auto (X11)", "auto")


def test_the_manifest_options_stay():
    keys = {o.key for o in ArknightsBackend.options}
    assert {"manifest_index", "manifest_file", "media_file", "download_workers"} <= keys
    keys = {o.key for o in StellaSoraBackend.options}
    assert {"manifest_index", "manifest_file", "download_workers"} <= keys


def test_process_detection_names_the_game_not_a_leftover_launcher(tmp_path):
    """An old launcher install can leave Yostar's launcher next to the game; the game runs."""
    backend = _backend(ArknightsBackend, tmp_path, ak.KIND)
    folder = backend.install_dir()
    folder.mkdir(parents=True)
    (folder / "game launcher.exe").write_bytes(b"MZ-launcher")
    (folder / "Arknights.exe").write_bytes(b"MZ-game")
    assert backend._patterns().names == ("Arknights.exe",)


def test_the_plan_is_the_manifest_without_archive_pins(tmp_path, monkeypatch):
    def no_pins(*_a, **_k):
        raise AssertionError("archive pins are not read for a Yostar manifest")

    monkeypatch.setattr(archivepins, "load_state", no_pins)
    files = {"StellaSora.exe": b"MZ-stella", "data/a.bin": b"A" * 64}
    manifest = _manifest(tmp_path, "StellaSora_EN", files)
    backend = _backend(StellaSoraBackend, tmp_path, ss.KIND, {"manifest_file": str(manifest)})
    plan = backend.plan()
    assert plan.version == "2.0.0"
    assert plan.exe == "StellaSora.exe"
    assert sorted(rf.path for rf in plan.files) == sorted(files)
    assert all(rf.crc64 for rf in plan.files)
    # Direct machinery that reads "the manifest" gets the same plan.
    assert backend._load_manifest().version == "2.0.0"


def test_no_manifest_refuses_with_the_yostar_message(tmp_path, monkeypatch):
    backend = _backend(ArknightsBackend, tmp_path, ak.KIND)
    monkeypatch.setattr(yostar, "configured_manifest", lambda profile: "")
    with pytest.raises(BackendError, match="manifest index"):
        backend.plan()


def test_a_bad_manifest_is_a_backend_error(tmp_path):
    bad = tmp_path / "bad.json"
    bad.write_text("{}", encoding="utf-8")
    backend = _backend(ArknightsBackend, tmp_path, ak.KIND, {"manifest_file": str(bad)})
    with pytest.raises(BackendError, match="download information is invalid"):
        backend.plan()


def test_no_predownload(tmp_path):
    backend = _backend(ArknightsBackend, tmp_path, ak.KIND)
    with pytest.raises(BackendError, match="has no predownload"):
        backend.predownload()
