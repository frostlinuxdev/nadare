# SPDX-License-Identifier: GPL-3.0-or-later
"""Regression tests for status truth (#290, #291), FGO disk space (#292) and
the Steam client install variable (#36)."""

from __future__ import annotations

from pathlib import Path

from _backend_helpers import make_context, write_exe

from nadare.backends import steam as steam_mod
from nadare.backends import wine as wine_mod
from nadare.backends.fgo import readiness
from nadare.backends.fgo.readiness import Probe
from nadare.backends.steam import SteamBackend
from nadare.backends.wine import WineBackend
from nadare.core.backends import BackendError


# ---------------------------------------------------------------------------
# #290: Wine status must notice a missing runtime
# ---------------------------------------------------------------------------
def test_wine_status_without_a_runtime_is_not_installed(tmp_path, monkeypatch):
    game = tmp_path / "game"
    write_exe(game / "game.exe", "MZ")
    ctx = make_context(
        tmp_path,
        "wine",
        {
            "install_dir": str(game),
            "exe_name": "game.exe",
            "prefix": str(tmp_path / "prefix"),
        },
    )

    def _no_wine() -> Path:
        raise BackendError("wine not found on PATH")

    monkeypatch.setattr(wine_mod, "first_wine", _no_wine)
    monkeypatch.setattr(wine_mod.shutil, "which", lambda name: None)

    status = WineBackend(ctx).status()
    assert status.state != "installed"
    assert "Wine is not installed" in status.message


# ---------------------------------------------------------------------------
# #291: Steam status must look at the library for the app manifest
# ---------------------------------------------------------------------------
def _steam_home(tmp_path: Path, monkeypatch) -> Path:
    home = tmp_path / "home"
    monkeypatch.setenv("HOME", str(home))
    monkeypatch.setattr(
        steam_mod.shutil,
        "which",
        lambda name: "/usr/bin/steam" if name == "steam" else None,
    )
    return home


def test_steam_status_installed_when_appmanifest_exists(tmp_path, monkeypatch):
    home = _steam_home(tmp_path, monkeypatch)
    steamapps = home / ".local" / "share" / "Steam" / "steamapps"
    steamapps.mkdir(parents=True)
    (steamapps / "appmanifest_730.acf").write_text('"AppState" { "appid" "730" }\n')

    ctx = make_context(tmp_path, "steam", {"steam_app_id": "730"})
    status = SteamBackend(ctx).status()
    assert status.state == "installed"


def test_steam_status_without_manifest_does_not_claim_installed(tmp_path, monkeypatch):
    home = _steam_home(tmp_path, monkeypatch)
    (home / ".local" / "share" / "Steam" / "steamapps").mkdir(parents=True)

    ctx = make_context(tmp_path, "steam", {"steam_app_id": "730"})
    status = SteamBackend(ctx).status()
    assert status.state == "installed"  # still launchable: Steam handles the install
    assert status.data["installed"] is False
    assert "not found in your Steam library" in status.message


def test_steam_status_searches_extra_library_folders(tmp_path, monkeypatch):
    home = _steam_home(tmp_path, monkeypatch)
    steamapps = home / ".local" / "share" / "Steam" / "steamapps"
    steamapps.mkdir(parents=True)
    extra = tmp_path / "extra-library"
    (extra / "steamapps").mkdir(parents=True)
    (extra / "steamapps" / "appmanifest_999.acf").write_text('"AppState" {}\n')
    (steamapps / "libraryfolders.vdf").write_text(
        '"libraryfolders" { "1" { "path" "' + str(extra) + '" } }\n'
    )

    ctx = make_context(tmp_path, "steam", {"steam_app_id": "999"})
    assert SteamBackend(ctx).status().state == "installed"


# ---------------------------------------------------------------------------
# #292: the disk check measures the lepton prefix and blocks below 30 GiB
# ---------------------------------------------------------------------------
def _probe_with_free(free: int) -> Probe:
    return Probe(free_bytes=lambda: free)


def test_disk_check_measures_the_lepton_prefix(tmp_path, monkeypatch):
    prefix = tmp_path / "lepton-nv"
    seen: list[Path] = []

    def _free(path: Path) -> int:
        seen.append(Path(path))
        return 42

    monkeypatch.setattr(readiness, "_lepton_prefix", lambda: prefix)
    monkeypatch.setattr(readiness.diskspace, "free_bytes", _free)

    assert Probe().free_bytes() == 42
    assert seen == [prefix]


def test_disk_check_below_30_gib_is_blocking_and_names_both_numbers():
    check = readiness._disk_check(_probe_with_free(20 * (1 << 30)))
    assert check.tier == "blocking"
    assert check.ok is False
    assert "30" in check.detail and "20.0 GiB" in check.detail


def test_disk_check_at_30_gib_passes():
    check = readiness._disk_check(_probe_with_free(30 * (1 << 30)))
    assert check.ok is True
    assert check.tier == "blocking"


# ---------------------------------------------------------------------------
# #36: only the Steam *client* may own STEAM_COMPAT_CLIENT_INSTALL_PATH
# ---------------------------------------------------------------------------
def test_wine_proton_launch_does_not_set_the_steam_client_path(tmp_path, monkeypatch):
    game = tmp_path / "game"
    write_exe(game / "game.exe", "MZ")
    proton = tmp_path / "Proton"
    proton.mkdir()
    monkeypatch.setattr(wine_mod.runners, "resolve_runner", lambda value: proton)
    monkeypatch.setattr(
        wine_mod.shutil,
        "which",
        lambda name, *a, **k: "/opt/umu-run" if name == "umu-run" else None,
    )
    ctx = make_context(
        tmp_path,
        "wine",
        {
            "install_dir": str(game),
            "exe_name": "game.exe",
            "prefix": str(tmp_path / "prefix"),
            "runner": "MyProton",
        },
    )
    _argv, env, _prefix, _target = WineBackend(ctx).build()
    assert "STEAM_COMPAT_CLIENT_INSTALL_PATH" not in env
    assert env["STEAM_COMPAT_DATA_PATH"] == str(tmp_path / "prefix")
