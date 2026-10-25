# SPDX-License-Identifier: GPL-3.0-or-later
"""Tests for the ``direct-<game>`` backends (nadare.backends.direct)."""

from __future__ import annotations

import hashlib
import io
import zipfile
from pathlib import Path

import pytest
from _backend_helpers import make_context
from _httpserver import Server

from nadare.backends import arknights as ak
from nadare.backends import direct
from nadare.core import directdl as dd
from nadare.core import directkinds as dk
from nadare.core.backends import BackendError, get, kinds


def _zip(files: dict[str, bytes]) -> bytes:
    buf = io.BytesIO()
    with zipfile.ZipFile(buf, "w", zipfile.ZIP_DEFLATED) as z:
        for name, data in files.items():
            z.writestr(name, data)
    return buf.getvalue()


@pytest.fixture
def game(tmp_path, monkeypatch):
    """A Brown Dust 2 backend whose source points at a local server (zip + loose file)."""
    archive = _zip({"BrownDust II.exe": b"MZ", "data/a.bin": b"a" * 64})
    loose = b"loose-file" * 10
    srv = Server({"/pkg.zip": archive, "/extra/l.bin": loose})
    state = {"version": "1"}

    def plan(fetch=None) -> dd.Plan:
        return dd.Plan(
            "bd2",
            state["version"],
            files=(
                dd.RemoteFile(
                    "extra/l.bin",
                    srv.url + "/extra/l.bin",
                    len(loose),
                    md5=hashlib.md5(loose).hexdigest(),
                ),
            ),
            archives=(
                dd.Archive(
                    "bd2",
                    dd.RemoteFile(
                        "pkg.zip",
                        srv.url + "/pkg.zip",
                        len(archive),
                        md5=hashlib.md5(archive).hexdigest(),
                    ),
                ),
            ),
            exe="BrownDust II.exe",
        )

    monkeypatch.setattr(direct.BrownDust2Backend, "plan_factory", staticmethod(plan))
    monkeypatch.setattr(ak.ArknightsBackend, "_ensure_default_runner", lambda self, strict: None)
    monkeypatch.setattr(ak.ArknightsBackend, "_ensure_direct_prefix", lambda self: None)
    backend = direct.BrownDust2Backend(
        make_context(tmp_path, "direct-bd2", {"prefix": str(tmp_path / "pfx")})
    )
    backend.state = state
    backend.srv = srv
    yield backend
    srv.close()


# ------------------------------------------------------------ registry / tables
def test_kind_table_matches_the_classes():
    from nadare.backends import launcher

    for table, module in ((dk._DIRECT, direct), (dk._LAUNCHER, launcher)):
        for kind, (cls_name, title, root, subdir, exe) in table.items():
            cls = getattr(module, cls_name)
            assert cls.kind == kind and cls.display_name == title and cls.direct_exe == exe
            assert cls.install_subdir == Path(root) / subdir
            assert exe in cls.launcher_names
            assert get(kind) is cls
            assert kind in kinds()
    assert dk.DIRECT_KINDS == set(dk._DIRECT) and dk.LAUNCHER_KINDS == set(dk._LAUNCHER)
    assert dk.CATALOG_KINDS == dk.DIRECT_KINDS | dk.LAUNCHER_KINDS == set(dk.CATALOG_BACKENDS)
    assert not dk.DIRECT_KINDS & dk.LAUNCHER_KINDS


def test_catalog_kinds_are_wired_into_the_ui_and_runner_defaults():
    from nadare.core import runners
    from nadare.gui import add_game, library

    assert dk.CATALOG_KINDS <= runners.PROTON_DEFAULT_BACKENDS
    assert dk.CATALOG_KINDS <= add_game.PREFIX_KINDS
    wizard = {g.kind: g for g in add_game.CATALOG}
    for rows, label in (
        (dk.DIRECT_WIZARD, dk.DIRECT_LABEL),
        (dk.LAUNCHER_WIZARD, dk.LAUNCHER_LABEL),
    ):
        for kind, title, root, subdir, exe in rows:
            game = wizard[kind]
            assert (game.title, game.install_root, game.install_subdir, game.exes) == (
                title,
                root,
                subdir,
                (exe,),
            )
            assert game.badge == dk.tier_of(kind)
            assert library._BACKEND_LABELS[kind] == label


def test_importing_an_existing_install_finds_the_catalog_layout(tmp_path):
    from nadare.gui import add_game

    install = tmp_path / "pfx/drive_c/Games/BrownDust2"
    install.mkdir(parents=True)
    (install / "BrownDust II.exe").write_bytes(b"MZ")
    info = add_game.detect_import("direct-bd2", tmp_path / "pfx", None)
    assert info["install_dir"] == str(install) and info["prefix"] == str(tmp_path / "pfx")
    nikke = tmp_path / "p2/drive_c/NIKKE"
    nikke.mkdir(parents=True)
    assert add_game.detect_import("launcher-nikke", tmp_path / "p2", None)["install_dir"] == str(
        nikke
    )


def test_no_yostar_options_on_direct_backends():
    keys = {o.key for o in direct.DirectBackend.options}
    assert not keys & {"manifest_index", "manifest_file", "installer_url", "media_file"}
    assert {"prefix", "install_dir", "runner", "launch_args", "download_workers"} <= keys


# ------------------------------------------------------------------- lifecycle
def test_install_status_update_verify(game):
    assert game.status().state == "not_installed"
    game.install()
    root = game.install_dir()
    assert (root / "BrownDust II.exe").read_bytes() == b"MZ"
    assert (root / "data/a.bin").is_file() and (root / "extra/l.bin").is_file()
    assert not game._install_incomplete()
    st = game.status()
    assert st.state == "installed" and st.version == "1"
    assert st.data["mode"] == "direct" and st.data["can_repair"]
    assert game.verify()["verified"] is True

    n = len(game.srv.requests)
    game.update()  # nothing changed: nothing is fetched
    assert len(game.srv.requests) == n


def test_update_to_a_new_version_refetches_the_archive_only(game):
    game.install()
    n = len(game.srv.requests)
    game.state["version"] = "2"
    game.update()
    fetched = game.srv.requests[n:]
    assert fetched == ["/pkg.zip"]  # the loose file already verified
    assert game.status().version == "2"


def test_verify_and_repair_restore_a_deleted_file(game):
    game.install()
    (game.install_dir() / "extra/l.bin").unlink()
    result = game.verify()
    assert result["verified"] is False and "extra/l.bin" in result["message"]
    game.repair()
    assert (game.install_dir() / "extra/l.bin").is_file()
    assert game.verify()["verified"] is True


def test_a_lost_archive_record_is_reported_by_name(game):
    game.install()
    (game.install_dir() / dd.STATE_DIR / "bd2.done").unlink()
    result = game.verify()
    assert result["verified"] is False and "bd2" in result["message"]


def test_check_update_caches_the_published_version(game, monkeypatch):
    game.install()
    monkeypatch.setattr(ak, "UPDATE_CHECK_INTERVAL", 0)  # the install just refreshed the cache
    game.state["version"] = "9"
    game.check_update()
    st = game.status()
    assert st.version == "1" and st.data["update_available"] is True


def test_launch_without_an_install_is_a_clear_error(game):
    with pytest.raises(BackendError, match="not found"):
        game.launch()


def test_update_without_an_install_is_a_clear_error(game):
    with pytest.raises(BackendError, match="not installed"):
        game.update()


# ------------------------------------------------------------------ error paths
def test_a_source_failure_becomes_a_backend_error(tmp_path, monkeypatch):
    def boom(fetch=None):
        raise dd.DirectError("HTTP 500")

    monkeypatch.setattr(direct.BrownDust2Backend, "plan_factory", staticmethod(boom))
    backend = direct.BrownDust2Backend(make_context(tmp_path, "direct-bd2", {}))
    with pytest.raises(BackendError, match="download information"):
        backend._sync_direct()
    # an unreadable source must never leave the install marked "in progress"
    assert not backend._install_incomplete()


def test_a_failed_download_leaves_the_install_incomplete(game):
    game.srv.objects["/pkg.zip"] = b"corrupted"  # md5 no longer matches
    with pytest.raises(BackendError):
        game.install()
    assert game._install_incomplete()
    assert game.status().state == "not_installed"
    assert not (game.install_dir() / "BrownDust II.exe").exists()


# ------------------------------------------------------------ launcher games
def _launcher(tmp_path, cls_name):
    from nadare.backends import launcher

    cls = getattr(launcher, cls_name)
    return cls(make_context(tmp_path, cls.kind, {"prefix": str(tmp_path / "pfx")}))


@pytest.mark.parametrize("cls_name", ["NikkeBackend", "NteBackend"])
def test_launcher_games_trust_only_their_publisher_hosts(tmp_path, cls_name):
    b = _launcher(tmp_path, cls_name)
    assert b.direct_mode() is False
    assert b.require_installer_url() == b.default_installer_url  # the pinned default is usable
    assert not b._installer_host_trusted("https://evil.example/setup.exe")
    assert not b._installer_host_trusted("https://notperfectworld.com/setup.exe")
    assert not b._installer_host_trusted("http://" + b.trusted_hosts[0] + "/setup.exe")
    assert b._installer_host_trusted("https://cdn." + b.trusted_hosts[0] + "/setup.exe")


def test_launcher_refuses_an_untrusted_installer_url_unless_pinned(tmp_path):
    from nadare.backends import launcher

    ctx = make_context(
        tmp_path,
        "launcher-nte",
        {"prefix": str(tmp_path / "pfx"), "installer_url": "https://evil.example/x.exe"},
    )
    b = launcher.NteBackend(ctx)
    with pytest.raises(BackendError, match="installer URL"):
        b.require_installer_url()
    ctx2 = make_context(
        tmp_path,
        "launcher-nte",
        {
            "prefix": str(tmp_path / "pfx2"),
            "installer_url": "https://evil.example/x.exe",
            "installer_sha256": "a" * 64,
        },
    )
    assert launcher.NteBackend(ctx2).require_installer_url() == "https://evil.example/x.exe"


def test_launcher_status_finds_the_installed_launcher(tmp_path):
    b = _launcher(tmp_path, "NikkeBackend")
    assert b.status().state == "not_installed"
    exe = b.install_dir() / "Launcher" / "nikke_launcher.exe"
    exe.parent.mkdir(parents=True)
    exe.write_bytes(b"MZ")
    st = b.status()
    assert st.state == "installed" and st.data["mode"] == "official"
    assert st.data["exe"].endswith("nikke_launcher.exe")


def test_launcher_options_have_installer_keys_but_no_manifest_keys():
    from nadare.backends import launcher

    keys = {o.key for o in launcher.LauncherBackend.options}
    assert {"installer_url", "installer_sha256", "prefix", "runner"} <= keys
    assert not keys & {"manifest_index", "manifest_file", "media_file"}


def test_pgr_passes_its_token_override_to_the_source(tmp_path, monkeypatch):
    seen = {}

    def fake(fetch=None, token=None):
        seen["token"] = token
        return dd.Plan("pgr", "1", exe="PGR.exe")

    monkeypatch.setattr(direct.PgrBackend, "plan_factory", staticmethod(fake))
    b = direct.PgrBackend(make_context(tmp_path, "direct-pgr", {"prefix": str(tmp_path / "pfx")}))
    b.plan()
    assert seen["token"] is None
    b2 = direct.PgrBackend(
        make_context(
            tmp_path,
            "direct-pgr",
            {"prefix": str(tmp_path / "pfx2"), "kuro_token": " ABCdef12345678 "},
            "p2",
        )
    )
    b2.plan()
    assert seen["token"] == "ABCdef12345678"
    assert direct.PgrBackend.default_display == "x11"


# ------------------------------------------------------------ Endfield launch helper
def _endfield(tmp_path):
    pfx = tmp_path / "pfx"
    ctx = make_context(tmp_path, "direct-endfield", {"prefix": str(pfx)})
    backend = direct.EndfieldBackend(ctx)
    game = pfx / "drive_c" / "Games" / "ArknightsEndfield" / "Endfield.exe"
    game.parent.mkdir(parents=True)
    game.write_bytes(b"MZ")
    return backend, pfx, game


def test_endfield_goes_through_the_launcher_helper(tmp_path, monkeypatch):
    from nadare.core import endfield_shim as es

    backend, pfx, game = _endfield(tmp_path)
    shim = pfx / "drive_c" / "Games" / ".nadare" / "endfield-shim"
    seen = {}

    def fake_ensure(prefix, *, data_dir, **kw):
        seen["prefix"], seen["data_dir"] = prefix, data_dir
        return es.ShimFiles(shim / "suspend_start.exe", shim / "Games.exe")

    monkeypatch.setattr(es, "ensure_files", fake_ensure)
    argv = backend._direct_argv(game)
    assert argv[0] == str(shim / "suspend_start.exe")  # umu-run takes the unix path of the helper
    assert argv[1:4] == [
        "--launcher",
        "C:\\Games\\.nadare\\endfield-shim\\Games.exe",
        "C:\\Games\\ArknightsEndfield\\Endfield.exe",
    ]
    assert seen["prefix"] == pfx and seen["data_dir"] == backend._data_dir()


def test_endfield_steam_build_starts_directly(tmp_path, monkeypatch):
    from nadare.core import endfield_shim as es

    backend, _pfx, game = _endfield(tmp_path)
    (game.parent / "steam_api64.dll").write_bytes(b"x")
    monkeypatch.setattr(
        es, "ensure_files", lambda *a, **k: pytest.fail("no helper for the Steam build")
    )
    assert backend._direct_argv(game) == [str(game)]


def test_endfield_helper_failure_is_a_backend_error(tmp_path, monkeypatch):
    from nadare.core import endfield_shim as es

    backend, _pfx, game = _endfield(tmp_path)

    def boom(*a, **k):
        raise es.ShimError("mingw-w64 compiler not found")

    monkeypatch.setattr(es, "ensure_files", boom)
    with pytest.raises(BackendError, match="mingw"):
        backend._direct_argv(game)


# ------------------------------------------------------------ NTE launcher
def _nte(tmp_path):
    pfx = tmp_path / "pfx"
    ctx = make_context(tmp_path, "direct-nte", {"prefix": str(pfx)})
    backend = direct.NteDirectBackend(ctx)
    game = backend.install_dir() / "Client/WindowsNoEditor/HT/Binaries/Win64/HTGame.exe"
    game.parent.mkdir(parents=True)
    game.write_bytes(b"MZ")
    return backend, game


def test_nte_starts_the_publisher_launcher(tmp_path, monkeypatch):
    from nadare.core import nte_launcher as nl

    backend, game = _nte(tmp_path)
    exe = backend.install_dir() / "NTEGlobal" / "NTEGlobalLauncher.exe"
    seen = {}

    def fake_ensure(root, *, data_dir, **kw):
        seen["root"], seen["data_dir"] = root, data_dir
        return exe

    monkeypatch.setattr(nl, "ensure_launcher", fake_ensure)
    assert backend._direct_argv(game) == [str(exe), "/launcher", "/autoplay"]
    assert seen["root"] == backend.install_dir() and seen["data_dir"] == backend._data_dir()


def test_nte_launcher_failure_is_a_backend_error(tmp_path, monkeypatch):
    from nadare.core import nte_launcher as nl

    backend, game = _nte(tmp_path)

    def boom(*a, **k):
        raise nl.NteLauncherError("7-Zip not found")

    monkeypatch.setattr(nl, "ensure_launcher", boom)
    with pytest.raises(BackendError, match="7-Zip"):
        backend._direct_argv(game)


def test_nte_defaults_to_xwayland_but_the_profile_can_override(tmp_path):
    def display(profile):
        ctx = make_context(tmp_path, "direct-nte", {"prefix": str(tmp_path / "pfx"), **profile})
        return direct.NteDirectBackend(ctx)._wayland_default()

    assert display({}) == "0"
    assert display({"display": "wayland"}) == "1"
    assert display({"display": "x11"}) == "0"


def test_nikke_runs_under_a_non_steam_game_id(tmp_path, monkeypatch):
    # With the default GAMEID=0 the Proton fixes set SteamAppId=0 and the game believes it
    # runs on Steam, so its login never starts. A named non-Steam id avoids that.
    b = _launcher(tmp_path, "NikkeBackend")
    monkeypatch.setattr(b, "resolve_runner", lambda: ("/opt/proton", "proton"))
    monkeypatch.setattr("nadare.core.umu.system", lambda configured="umu-run": "/usr/bin/umu-run")
    _argv, env = b._prefix_command(["x.exe"])
    assert env["GAMEID"] == "umu-nikke" and env["UMU_ID"] == "umu-nikke"


def test_nikke_game_id_can_be_overridden_by_the_profile(tmp_path, monkeypatch):
    from nadare.backends import launcher

    ctx = make_context(
        tmp_path,
        "launcher-nikke",
        {
            "prefix": str(tmp_path / "pfx"),
            "extra_env": {"GAMEID": "umu-custom", "UMU_ID": "umu-custom"},
        },
    )
    b = launcher.NikkeBackend(ctx)
    monkeypatch.setattr(b, "resolve_runner", lambda: ("/opt/proton", "proton"))
    monkeypatch.setattr("nadare.core.umu.system", lambda configured="umu-run": "/usr/bin/umu-run")
    _argv, env = b._prefix_command(["x.exe"])
    assert env["GAMEID"] == "umu-custom" and env["UMU_ID"] == "umu-custom"


def test_other_launcher_games_keep_the_default_game_id(tmp_path, monkeypatch):
    b = _launcher(tmp_path, "NteBackend")
    monkeypatch.setattr(b, "resolve_runner", lambda: ("/opt/proton", "proton"))
    monkeypatch.setattr("nadare.core.umu.system", lambda configured="umu-run": "/usr/bin/umu-run")
    _argv, env = b._prefix_command(["x.exe"])
    assert env.get("GAMEID") != "umu-nikke"


def test_tiers_mark_only_the_launched_games_as_verified():
    from nadare.core import directkinds as dk

    assert dk.VERIFIED_KINDS <= dk.CATALOG_KINDS
    assert {dk.tier_of(k) for k in dk.CATALOG_KINDS} == {dk.TIER_VERIFIED, dk.TIER_EXPERIMENTAL}
    assert dk.tier_of("direct-bd2") == "Verified"
    assert dk.tier_of("direct-endfield") == "Verified"
    assert dk.tier_of("direct-nte") == "Verified"
    assert dk.tier_of("launcher-nte") == "Experimental"
    assert dk.tier_of("direct-genshin") == "Experimental"
    assert dk.tier_of("direct-wuwa") == "Experimental"
    assert dk.tier_of("not-a-kind") == "Experimental"


# ------------------------------------------------------------ Wuthering Waves launch
def _wuwa(tmp_path, profile=None):
    pfx = tmp_path / "pfx"
    ctx = make_context(tmp_path, "direct-wuwa", {"prefix": str(pfx), **(profile or {})})
    backend = direct.WutheringWavesBackend(ctx)
    root = pfx / "drive_c" / "Games" / "WutheringWaves"
    game = root / "Client" / "Binaries" / "Win64" / "Client-Win64-Shipping.exe"
    game.parent.mkdir(parents=True)
    game.write_bytes(b"MZ")
    (root / "Wuthering Waves.exe").write_bytes(b"MZ")
    return backend, root, game


def test_wuwa_starts_the_game_binary_not_the_launcher_stub(tmp_path):
    backend, root, game = _wuwa(tmp_path)
    assert backend.direct_target() == game
    assert backend.direct_exe == "Wuthering Waves.exe"  # still what marks an install
    assert backend._direct_argv(game) == [str(game), "-dx12", "-krqlv=hd"]


def test_wuwa_profile_overrides_win(tmp_path):
    backend, root, game = _wuwa(tmp_path, {"launch_exe": "Wuthering Waves.exe"})
    assert backend.direct_target() == root / "Wuthering Waves.exe"
    backend, root, game = _wuwa(tmp_path / "b", {"launch_args": "-dx11"})
    assert backend._direct_argv(game) == [str(game), "-dx11"]


def test_hoyo_games_are_offered_unless_hidden(monkeypatch):
    from nadare.core import directkinds as dk
    from nadare.gui import add_game

    monkeypatch.delenv("NADARE_HIDE_HOYO", raising=False)
    shown = {g.kind for g in add_game.available_catalog()}
    assert dk.HOYO_KINDS <= shown
    assert {"direct-bd2", "direct-wuwa", "direct-pgr"} <= shown
    monkeypatch.setenv("NADARE_HIDE_HOYO", "1")
    hidden = {g.kind for g in add_game.available_catalog()}
    assert not (dk.HOYO_KINDS & hidden)
    assert {"direct-bd2", "direct-wuwa", "direct-pgr"} <= hidden


def test_hoyo_kinds_still_load_for_existing_profiles():
    from nadare.core import directkinds as dk

    assert dk.HOYO_KINDS <= dk.DIRECT_KINDS
    assert dk.HOYO_KINDS <= set(dk.CATALOG_BACKENDS)
    assert dk.tier_of("direct-hi3") == "Verified"
    assert dk.tier_of("direct-zzz") == "Experimental"


def test_gfl2_haoplay_is_a_launcher_kind_with_a_trusted_installer():
    from nadare.backends import launcher
    from nadare.core import directkinds as dk

    assert "launcher-gfl2-haoplay" in dk.CATALOG_KINDS
    assert dk.tier_of("launcher-gfl2-haoplay") == "Experimental"
    cls = launcher.Gfl2HaoplayBackend
    assert cls.kind == "launcher-gfl2-haoplay" and cls.launcher_names == ("PCLauncher.exe",)
    assert cls.default_installer_url.startswith("https://gf2-dl.haoplay.com/")
    assert any(cls.default_installer_url.split("/")[2].endswith(h) for h in cls.trusted_hosts)


def test_gfl2_haoplay_direct_kind_is_registered_and_experimental():
    from nadare.backends import direct
    from nadare.core import directkinds as dk

    assert "direct-gfl2-haoplay" in dk.CATALOG_KINDS
    assert dk.tier_of("direct-gfl2-haoplay") == "Experimental"
    cls = direct.Gfl2HaoplayBackend
    assert cls.kind == "direct-gfl2-haoplay" and cls.direct_exe == "GF2_Exilium.exe"
    assert cls.plan_factory is not direct.Gfl2Backend.plan_factory


def test_gfl2_haoplay_pins_umu_id_zero_unless_the_profile_overrides_it(tmp_path, monkeypatch):
    from nadare.backends import direct

    def env_for(profile):
        ctx = make_context(
            tmp_path, "direct-gfl2-haoplay", {"prefix": str(tmp_path / "pfx"), **profile}
        )
        b = direct.Gfl2HaoplayBackend(ctx)
        monkeypatch.setattr(b, "resolve_runner", lambda: ("/opt/proton", "proton"))
        monkeypatch.setattr(
            "nadare.core.umu.system", lambda configured="umu-run": "/usr/bin/umu-run"
        )
        return b._prefix_command(["x.exe"])[1]

    env = env_for({})
    assert env["UMU_ID"] == "0" and env["GAMEID"] == "0"
    custom = env_for({"extra_env": {"UMU_ID": "umu-7", "GAMEID": "umu-7"}})
    assert custom["UMU_ID"] == "umu-7" and custom["GAMEID"] == "umu-7"
