# SPDX-License-Identifier: GPL-3.0-or-later
"""User-facing hints must name the command this build actually has."""

from __future__ import annotations

import pytest

from nadare.core import shortcuts
from nadare.core.backends import BackendError

APPIMAGE = "/opt/Nadare-0.1.AppImage"


@pytest.fixture
def appimage(monkeypatch):
    monkeypatch.setenv("APPIMAGE", APPIMAGE)
    monkeypatch.setattr(shortcuts.sandbox, "in_flatpak", lambda: False)
    return APPIMAGE


def test_command_hint_uses_the_appimage_path(appimage):
    from nadare.core.shortcuts import command_hint

    assert command_hint("doctor") == f"{APPIMAGE} doctor"


def test_command_hint_uses_flatpak_run(monkeypatch):
    from nadare.core.shortcuts import FLATPAK_ID, command_hint

    monkeypatch.delenv("APPIMAGE", raising=False)
    monkeypatch.setattr(shortcuts.sandbox, "in_flatpak", lambda: True)
    assert command_hint("doctor") == f"flatpak run {FLATPAK_ID} doctor"


def test_command_hint_uses_nadare_outside_a_package(monkeypatch):
    from nadare.core.shortcuts import command_hint

    monkeypatch.delenv("APPIMAGE", raising=False)
    monkeypatch.setattr(shortcuts.sandbox, "in_flatpak", lambda: False)
    assert command_hint("doctor") == "nadare doctor"


def test_remember_notice_names_the_appimage_command(appimage):
    from nadare.backends.czn.backend import REMEMBER_NOTICE

    assert f"{APPIMAGE} doctor" in REMEMBER_NOTICE
    assert "nadare doctor" not in REMEMBER_NOTICE


def test_add_game_fgo_tooltip_names_the_appimage_command(appimage):
    from nadare.gui.add_game import CATALOG

    fgo = next(g for g in CATALOG if g.kind == "fgo-lepton")
    assert f"{APPIMAGE} doctor --fgo" in fgo.badge_help
    assert "nadare doctor" not in fgo.badge_help


def test_fgo_page_notice_names_the_appimage_command(appimage):
    from nadare.backends.fgo.backend import FgoLeptonBackend

    assert f"{APPIMAGE} doctor --fgo" in FgoLeptonBackend.page_notice[0]
    assert "nadare doctor" not in FgoLeptonBackend.page_notice[0]


def test_hostlibs_hint_names_the_appimage_command(appimage):
    from nadare.core import hostlibs

    text = hostlibs.hint(["libfreetype.so.6"], os_release="ID=gentoo")
    assert f"{APPIMAGE} doctor" in text
    assert "nadare doctor" not in text


def test_endfield_report_hint_names_the_appimage_command(appimage):
    from nadare.core import endfield_shim

    text = endfield_shim._report_hint()
    assert f"{APPIMAGE} report" in text
    assert "nadare report" not in text


def test_login_required_points_at_the_remember_me_label(tmp_path):
    from _backend_helpers import make_context

    from nadare.backends.czn import backend as czn_backend

    backend = czn_backend.CznBackend(
        make_context(tmp_path, "stove-czn", {"prefix": str(tmp_path / "prefix")})
    )

    with pytest.raises(BackendError) as exc:
        backend._raise_login_required()

    assert "turn on 'Remember me on this device'" in exc.value.message
    assert "remember_me" not in exc.value.message


def test_umu_runner_error_says_how_to_fix_it(tmp_path, monkeypatch):
    from _backend_helpers import make_context

    from nadare.backends.czn import backend as czn_backend

    backend = czn_backend.CznBackend(
        make_context(tmp_path, "stove-czn", {"runner": "GE-Proton9-99"})
    )
    monkeypatch.setattr(czn_backend.umu_mod, "system", lambda *a, **k: None)
    monkeypatch.setattr(czn_backend.sandbox, "in_flatpak", lambda: False)
    monkeypatch.setattr(czn_backend.hostlibs, "missing_32bit", lambda: [])

    with pytest.raises(BackendError) as exc:
        backend._prefix_command(["game.exe"])

    text = exc.value.message
    assert "install umu-launcher" in text
    assert "Settings" in text


def test_missing_wine_error_names_the_runner_settings(tmp_path, monkeypatch):
    from _backend_helpers import make_context

    from nadare.backends.czn import backend as czn_backend

    backend = czn_backend.CznBackend(make_context(tmp_path, "stove-czn", {"runner": "wine"}))
    monkeypatch.delenv("CZN_WINE", raising=False)
    monkeypatch.setattr(czn_backend.shutil, "which", lambda *a, **k: None)

    with pytest.raises(BackendError) as exc:
        backend._prefix_command(["game.exe"])

    text = exc.value.message
    assert "no Proton runner found" in text
    assert "Settings -> Manage runners" in text
    assert "CZN_WINE" in text


def test_removal_failure_keeps_paths_and_errno_with_plain_wording(tmp_path, monkeypatch):
    from _backend_helpers import make_context

    from nadare.backends.czn import backend as czn_backend

    prefix = tmp_path / "wine"
    (prefix / "drive_c").mkdir(parents=True)
    (prefix / czn_backend.PREFIX_MARKER).write_text("nadare\n")
    backend = czn_backend.CznBackend(make_context(tmp_path, "stove-czn", {"prefix": str(prefix)}))
    monkeypatch.setattr(czn_backend.CznBackend, "_game_running", lambda self: False)

    def boom(path, *args, **kwargs):
        raise PermissionError("locked")

    monkeypatch.setattr(czn_backend.shutil, "rmtree", boom)

    with pytest.raises(BackendError) as exc:
        backend.uninstall()

    text = exc.value.message
    assert text.startswith("Could not remove these items (permissions or in use?): ")
    assert str(prefix) in text
    assert "locked" in text
