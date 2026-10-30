# SPDX-License-Identifier: GPL-3.0-or-later
"""NIKKE installs without a window: the miniloader's own downloader runs with its silent switches.

Found by reading the miniloader (an NSIS wrapper around a downloader that parses /Silent, /Dst=
and /StartAfterInstall=) and confirmed on a real run: with ``/StartAfterInstall=false`` it
downloads and unpacks the launcher, does not start it, and exits 0. Any other value starts it.
"""

from __future__ import annotations

import subprocess
from pathlib import Path

import pytest
from _backend_helpers import make_context, write_exe

from nadare.backends import arknights as ak
from nadare.backends import launcher
from nadare.backends import winegame as wg
from nadare.backends.launcher import NikkeBackend
from nadare.backends.winegame import WineGameBackend
from nadare.core import sevenzip
from nadare.core.backends import BackendError
from nadare.core.events import Cancelled

INNER = "nikkeminiloader.exe"


def _backend(tmp_path: Path, **profile) -> NikkeBackend:
    values = {"prefix": str(tmp_path / "prefix"), "wine_binary": "/usr/bin/wine", **profile}
    return NikkeBackend(make_context(tmp_path, "launcher-nikke", values))


def _install_launcher(prefix: Path) -> None:
    write_exe(prefix / "drive_c" / "NIKKE" / "Launcher" / "nikke_launcher.exe", "MZ")


@pytest.fixture
def rig(tmp_path, monkeypatch):
    """Everything below the install flow is faked; ``calls`` records each program run."""
    installer = write_exe(tmp_path / "nikke-wrapper.exe", "MZ")
    calls: list[list[str]] = []
    state = {"silent_installs": True, "extract_ok": True, "silent_code": 0}

    def fake_stream(argv, env, on_line=None, cancel=None):
        calls.append(list(argv))
        prefix = tmp_path / "prefix"
        ran_inner = any(part.endswith(INNER.lower()) or part.endswith(INNER) for part in argv)
        if ran_inner and state["silent_installs"]:
            _install_launcher(prefix)
        if not ran_inner:  # the wrapper window: a person clicked through it
            _install_launcher(prefix)
            return 0
        return state["silent_code"]

    def fake_extract(sevenz, archive, outdir, run=None):
        if not state["extract_ok"]:
            raise launcher.SilentInstallUnavailable("cannot extract")
        outdir.mkdir(parents=True, exist_ok=True)
        (outdir / INNER).write_text("MZ")
        (outdir / "res.zip").write_text("zip")

    monkeypatch.setattr(ak, "stream", fake_stream)

    monkeypatch.setattr(wg, "stream", fake_stream)
    monkeypatch.setattr(WineGameBackend, "_ensure_prefix", lambda self: None)
    monkeypatch.setattr(WineGameBackend, "_run_vcredist", lambda self, created=False: None)
    monkeypatch.setattr(WineGameBackend, "_download_installer", lambda self, url: installer)
    monkeypatch.setattr(launcher, "_extract_nsis", fake_extract)
    monkeypatch.setattr(sevenzip, "find", lambda **kw: "/usr/bin/7z")
    monkeypatch.delenv("NADARE_NIKKE_SILENT", raising=False)
    return calls, state, installer


def test_install_runs_the_downloader_silently_and_never_the_installer_window(tmp_path, rig):
    calls, _state, installer = rig
    backend = _backend(tmp_path)
    backend.install()
    assert len(calls) == 1
    argv = calls[0]
    assert argv[-3:] == ["/Silent", "/Dst=C:\\NIKKE", "/StartAfterInstall=false"]
    assert argv[-4].endswith(INNER)
    assert argv[-5].endswith(launcher.QUIET_NAME)
    assert str(installer) not in argv
    assert backend.find_launcher() is not None


def test_the_downloader_is_unpacked_next_to_where_the_official_installer_puts_it(tmp_path, rig):
    calls, *_ = rig
    backend = _backend(tmp_path)
    backend.install()
    helper = Path(calls[0][-5])
    assert "AppData/Local/nikkeminiloader" in str(helper).replace("\\", "/")
    assert str(helper).startswith(str(tmp_path / "prefix" / "drive_c"))
    # the loader itself is named by a Windows path, because the helper starts it inside Wine
    assert calls[0][-4].startswith("C:\\") and "nikkeminiloader" in calls[0][-4]


def test_the_start_after_install_value_is_the_one_that_means_no(tmp_path, rig):
    """Measured: only the literal value ``false`` stops the loader from starting the launcher."""
    calls, *_ = rig
    _backend(tmp_path).install()
    flags = [part for part in calls[0] if part.startswith("/StartAfterInstall=")]
    assert flags == ["/StartAfterInstall=false"]


def test_a_custom_install_folder_is_passed_as_a_windows_path(tmp_path, rig):
    calls, *_ = rig
    prefix = tmp_path / "prefix"
    _backend(tmp_path, install_dir=str(prefix / "drive_c" / "Games" / "NIKKE")).install()
    assert "/Dst=C:\\Games\\NIKKE" in calls[0]


def test_without_7zip_the_installer_window_is_used(tmp_path, rig, monkeypatch):
    calls, _state, installer = rig
    monkeypatch.setattr(sevenzip, "find", lambda **kw: None)
    _backend(tmp_path).install()
    assert len(calls) == 1 and str(installer) in calls[0]


def test_a_failed_extraction_falls_back_to_the_installer_window(tmp_path, rig):
    calls, state, installer = rig
    state["extract_ok"] = False
    _backend(tmp_path).install()
    assert len(calls) == 1 and str(installer) in calls[0]


def test_a_silent_run_that_leaves_no_launcher_falls_back_to_the_installer_window(tmp_path, rig):
    calls, state, installer = rig
    state["silent_installs"] = False
    backend = _backend(tmp_path)
    backend.install()
    assert len(calls) == 2
    assert calls[0][-4].endswith(INNER) and str(installer) in calls[1]
    assert backend.find_launcher() is not None


def test_custom_installer_arguments_keep_the_old_behaviour(tmp_path, rig):
    calls, _state, installer = rig
    _backend(tmp_path, installer_args=["/S"]).install()
    assert len(calls) == 1 and str(installer) in calls[0] and "/Silent" not in calls[0]


def test_the_opt_out_variable_keeps_the_old_behaviour(tmp_path, rig, monkeypatch):
    calls, _state, installer = rig
    monkeypatch.setenv("NADARE_NIKKE_SILENT", "0")
    _backend(tmp_path).install()
    assert len(calls) == 1 and str(installer) in calls[0]


def test_cancelling_during_the_silent_run_is_not_turned_into_a_fallback(tmp_path, rig, monkeypatch):
    calls, *_ = rig

    def cancelled(argv, env, on_line=None, cancel=None):
        calls.append(list(argv))
        raise Cancelled()

    monkeypatch.setattr(ak, "stream", cancelled)

    monkeypatch.setattr(wg, "stream", cancelled)
    with pytest.raises(Cancelled):
        _backend(tmp_path).install()
    assert len(calls) == 1


def test_when_nothing_installs_the_launcher_the_install_still_fails_plainly(
    tmp_path, rig, monkeypatch
):
    calls, _state, _installer = rig

    def nothing(argv, env, on_line=None, cancel=None):
        calls.append(list(argv))
        return 3

    monkeypatch.setattr(ak, "stream", nothing)

    monkeypatch.setattr(wg, "stream", nothing)
    with pytest.raises(BackendError):
        _backend(tmp_path).install()


# ---- the unpacking helper ---------------------------------------------------------------------
def test_extract_nsis_asks_seven_zip_for_the_nsis_format_and_stays_in_the_folder(tmp_path):
    seen = {}

    def run(argv, **kwargs):
        seen["argv"] = argv

        class Result:
            returncode = 0
            stderr = ""

        return Result()

    out = tmp_path / "out"
    launcher._extract_nsis("/usr/bin/7z", tmp_path / "a.exe", out, run)
    assert seen["argv"] == ["/usr/bin/7z", "x", "-tNsis", "-y", f"-o{out}", str(tmp_path / "a.exe")]


def test_extract_nsis_reports_a_failure(tmp_path):
    def run(argv, **kwargs):
        class Result:
            returncode = 2
            stderr = "not an archive"

        return Result()

    with pytest.raises(launcher.SilentInstallUnavailable, match="not an archive"):
        launcher._extract_nsis("/usr/bin/7z", tmp_path / "a.exe", tmp_path / "out", run)


def test_the_inner_downloader_is_found_whatever_its_capitalisation(tmp_path):
    folder = tmp_path / "loader"
    folder.mkdir()
    (folder / "res.zip").write_text("zip")
    (folder / "nikke_Miniloader.exe").write_text("MZ")
    assert launcher._find_inner_loader(folder).name == "nikke_Miniloader.exe"
    with pytest.raises(launcher.SilentInstallUnavailable):
        launcher._find_inner_loader(tmp_path / "nothing-here")


# ---- review fixes -------------------------------------------------------------------------------
def test_a_failed_silent_run_is_not_a_success_just_because_an_old_launcher_exists(tmp_path, rig):
    calls, state, installer = rig
    _install_launcher(tmp_path / "prefix")  # left by an earlier install
    state["silent_installs"] = False
    state["silent_code"] = 1
    _backend(tmp_path).install()
    assert len(calls) == 2 and str(installer) in calls[1], "the installer window runs instead"


def test_a_silent_run_that_exits_zero_is_accepted_over_an_existing_install(tmp_path, rig):
    calls, state, _installer = rig
    _install_launcher(tmp_path / "prefix")
    state["silent_installs"] = False  # the downloader had nothing to do and said so
    _backend(tmp_path).install()
    assert len(calls) == 1


def test_a_filesystem_error_while_unpacking_still_falls_back(tmp_path, rig, monkeypatch):
    calls, _state, installer = rig

    def broken(sevenz, archive, outdir, run=None):
        raise PermissionError("read-only prefix")

    monkeypatch.setattr(launcher, "_extract_nsis", broken)
    _backend(tmp_path).install()
    assert len(calls) == 1 and str(installer) in calls[0]


def test_failing_to_start_the_downloader_falls_back(tmp_path, rig, monkeypatch):
    calls, _state, installer = rig
    real = ak.stream

    def flaky(argv, env, on_line=None, cancel=None):
        if str(installer) not in argv:
            calls.append(list(argv))
            raise BackendError("could not start umu-run")
        return real(argv, env, on_line, cancel)

    monkeypatch.setattr(ak, "stream", flaky)

    monkeypatch.setattr(wg, "stream", flaky)
    _backend(tmp_path).install()
    assert len(calls) == 2 and str(installer) in calls[1]


def test_the_unpack_folder_starts_clean_so_a_stale_downloader_is_never_run(
    tmp_path, rig, monkeypatch
):
    calls, *_ = rig
    folder = (
        tmp_path
        / "prefix"
        / "drive_c"
        / "users"
        / "steamuser"
        / "AppData"
        / "Local"
        / "nikkeminiloader"
    )
    folder.mkdir(parents=True)
    (folder / "nikkeminiloader.exe").write_text("OLD")
    (folder / "junk.txt").write_text("x")

    def renamed(sevenz, archive, outdir, run=None):
        outdir.mkdir(parents=True, exist_ok=True)
        (outdir / "nikke_Miniloader.exe").write_text("MZ")

    monkeypatch.setattr(launcher, "_extract_nsis", renamed)
    _backend(tmp_path).install()
    assert calls[0][-4].endswith("nikke_Miniloader.exe")
    assert not (folder / "nikkeminiloader.exe").exists() and not (folder / "junk.txt").exists()


def test_a_directory_named_like_the_downloader_is_not_taken_for_it(tmp_path):
    folder = tmp_path / "loader"
    (folder / "x-miniloader.exe").mkdir(parents=True)
    with pytest.raises(launcher.SilentInstallUnavailable):
        launcher._find_inner_loader(folder)


def test_an_install_folder_with_spaces_is_one_argument(tmp_path, rig):
    calls, *_ = rig
    prefix = tmp_path / "prefix"
    _backend(tmp_path, install_dir=str(prefix / "drive_c" / "Games" / "My NIKKE")).install()
    assert "/Dst=C:\\Games\\My NIKKE" in calls[0]


def test_the_normal_store_profile_still_installs_silently(tmp_path, rig):
    """Add Game gives every profile an install_dir OUTSIDE its prefix; the launcher installers
    have always ignored it and used C:\\NIKKE, which find_launcher() finds."""
    calls, _state, installer = rig
    backend = _backend(tmp_path, install_dir=str(tmp_path / "profiles" / "nikke" / "install"))
    backend.install()
    assert len(calls) == 1 and str(installer) not in calls[0]
    assert "/Dst=C:\\NIKKE" in calls[0]
    assert backend.find_launcher() is not None


def test_an_install_folder_elsewhere_on_the_disk_also_uses_the_default_folder(tmp_path, rig):
    calls, *_ = rig
    _backend(tmp_path, install_dir="/mnt/games/NIKKE").install()
    assert "/Dst=C:\\NIKKE" in calls[0]
    assert not any(part.startswith("/Dst=Z:") for part in calls[0])


def test_unpacking_has_a_time_limit_and_a_stuck_one_falls_back(tmp_path):
    seen = {}

    def run(argv, **kwargs):
        seen.update(kwargs)
        raise subprocess.TimeoutExpired(argv, kwargs["timeout"])

    with pytest.raises(launcher.SilentInstallUnavailable, match="too long"):
        launcher._extract_nsis("/usr/bin/7z", tmp_path / "a.exe", tmp_path / "out", run)
    assert 0 < seen["timeout"] <= 300
    assert seen.get("errors") == "replace", (
        "odd bytes in 7-Zip's message must not crash the install"
    )


def test_the_helper_that_hides_the_downloader_window_is_staged_next_to_the_loader(tmp_path, rig):
    calls, *_ = rig
    _backend(tmp_path).install()
    helper = Path(calls[0][-5])
    assert helper.is_file() and helper.read_bytes()[:2] == b"MZ"
    assert helper.parent.name == "nikkeminiloader"


def test_without_the_helper_the_loader_runs_directly(tmp_path, rig, monkeypatch):
    calls, *_ = rig
    monkeypatch.setattr(launcher, "QUIET_PREBUILT", tmp_path / "missing.exe")
    backend = _backend(tmp_path)
    backend.install()
    assert calls[0][-4].endswith(INNER) and not calls[0][-5].endswith(launcher.QUIET_NAME)
    assert backend.find_launcher() is not None


def test_a_helper_that_does_not_match_its_pin_is_not_used(tmp_path, rig, monkeypatch):
    calls, *_ = rig
    fake = tmp_path / "fake.exe"
    fake.write_bytes(b"MZ-not-ours")
    monkeypatch.setattr(launcher, "QUIET_PREBUILT", fake)
    _backend(tmp_path).install()
    assert not calls[0][-5].endswith(launcher.QUIET_NAME)


def test_the_shipped_quiet_helper_matches_its_pin_and_the_current_source():
    import hashlib

    data = launcher.QUIET_PREBUILT.read_bytes()
    assert data[:2] == b"MZ"
    assert hashlib.sha256(data).hexdigest() == launcher.QUIET_PREBUILT_SHA256
    source = Path(launcher.__file__).parents[1] / "core" / "shim_src" / "quiet_start.c"
    assert hashlib.sha256(source.read_bytes()).hexdigest() == launcher.QUIET_SOURCE_SHA256


def test_the_default_installer_is_the_publishers_current_download_not_a_pinned_old_one():
    url = NikkeBackend.default_installer_url
    assert url == "https://nikke-en.com/nikkeminiloader.exe"
    assert "0.0.6" not in url


def test_the_inner_loader_of_the_current_installer_is_found(tmp_path):
    """The current installer unpacks ``nikke_Miniloader.exe``, not ``nikkeminiloader.exe``."""
    (tmp_path / "res.zip").write_text("zip")
    (tmp_path / "nikke_Miniloader.exe").write_text("MZ")
    assert launcher._find_inner_loader(tmp_path).name == "nikke_Miniloader.exe"


@pytest.mark.parametrize("name", ["SteamGameId", "SteamAppId", "UMU_USE_STEAM"])
def test_the_launcher_never_sees_steam_variables_so_its_update_and_repair_stay_enabled(
    tmp_path, monkeypatch, name
):
    """NIKKE's launcher locks repair, download and update when it thinks Steam started it."""
    monkeypatch.setenv(name, "123")
    _argv, env = _backend(tmp_path)._prefix_command(["x.exe"])
    assert name not in env


def test_the_nikke_umu_game_id_is_still_set(tmp_path):
    _argv, env = _backend(tmp_path)._prefix_command(["x.exe"])
    assert env["GAMEID"] == "umu-nikke" and env["UMU_ID"] == "umu-nikke"
