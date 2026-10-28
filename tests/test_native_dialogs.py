# SPDX-License-Identifier: GPL-3.0-or-later
"""Pick a Qt platform theme so file dialogs use the desktop portal (issue #652)."""

from __future__ import annotations

import pytest

from nadare.gui import nativedialogs

PORTAL = "xdgdesktopportal"


def choose(env, *, themes=(PORTAL, "gtk3"), bus=True, portal=True):
    return nativedialogs.choose_platform_theme(
        env, has_theme=lambda name: name in themes, session_bus=bus, portal_present=portal
    )


def test_unset_theme_with_a_session_bus_selects_the_portal():
    assert choose({}) == PORTAL


def test_unset_theme_without_a_session_bus_changes_nothing():
    assert choose({}, bus=False) is None


def test_unset_theme_without_the_portal_plugin_changes_nothing():
    assert choose({}, themes=("gtk3",)) is None


def test_a_theme_the_bundled_qt_can_load_is_respected():
    assert choose({"QT_QPA_PLATFORMTHEME": "gtk3"}) is None


def test_the_portal_theme_already_selected_changes_nothing():
    assert choose({"QT_QPA_PLATFORMTHEME": PORTAL}) is None


def test_kde_that_the_bundled_qt_cannot_load_falls_back_to_the_portal():
    assert choose({"QT_QPA_PLATFORMTHEME": "kde"}, themes=(PORTAL,)) == PORTAL


def test_a_colon_list_with_one_loadable_theme_is_respected():
    assert choose({"QT_QPA_PLATFORMTHEME": "kde:gtk3"}, themes=(PORTAL, "gtk3")) is None


def test_a_colon_list_with_no_loadable_theme_falls_back():
    assert choose({"QT_QPA_PLATFORMTHEME": "kde:lxqt"}, themes=(PORTAL,)) == PORTAL


def test_unloadable_theme_and_no_bus_changes_nothing():
    assert choose({"QT_QPA_PLATFORMTHEME": "kde"}, themes=(PORTAL,), bus=False) is None


@pytest.mark.parametrize("value", ["0", "false", "no", "off"])
def test_the_opt_out_switch_changes_nothing(value):
    assert choose({"NADARE_NATIVE_DIALOGS": value}) is None
    env = {"NADARE_NATIVE_DIALOGS": value, "QT_QPA_PLATFORMTHEME": "kde"}
    assert choose(env, themes=(PORTAL,)) is None


def test_opt_out_is_case_and_space_insensitive():
    assert choose({"NADARE_NATIVE_DIALOGS": " OFF "}) is None


def test_apply_sets_the_variable_only_when_a_theme_is_chosen(monkeypatch):
    monkeypatch.setattr(nativedialogs, "choose_platform_theme", lambda *a, **k: PORTAL)
    env: dict[str, str] = {}
    nativedialogs.apply(env)
    assert env == {"QT_QPA_PLATFORMTHEME": PORTAL}


def test_apply_leaves_the_environment_alone_otherwise(monkeypatch):
    monkeypatch.setattr(nativedialogs, "choose_platform_theme", lambda *a, **k: None)
    env = {"QT_QPA_PLATFORMTHEME": "kde"}
    nativedialogs.apply(env)
    assert env == {"QT_QPA_PLATFORMTHEME": "kde"}


def test_apply_never_raises(monkeypatch):
    def boom(*_a, **_k):
        raise RuntimeError("no qt")

    monkeypatch.setattr(nativedialogs, "choose_platform_theme", boom)
    env: dict[str, str] = {}
    nativedialogs.apply(env)
    assert env == {}


def test_session_bus_detection(tmp_path, monkeypatch):
    runtime = tmp_path / "run"
    runtime.mkdir()
    assert nativedialogs.session_bus_available({"XDG_RUNTIME_DIR": str(runtime)}) is False
    (runtime / "bus").touch()
    assert nativedialogs.session_bus_available({"XDG_RUNTIME_DIR": str(runtime)}) is True
    assert nativedialogs.session_bus_available({"DBUS_SESSION_BUS_ADDRESS": "unix:path=/x"}) is True
    assert nativedialogs.session_bus_available({}) is False


# ---- scrutiny fixes ---------------------------------------------------------------------


def _service(tmp_path, name="org.freedesktop.portal.Desktop.service"):
    folder = tmp_path / "share" / "dbus-1" / "services"
    folder.mkdir(parents=True)
    (folder / name).touch()
    return str(tmp_path / "share")


def test_portal_service_found_in_xdg_data_dirs(tmp_path):
    share = _service(tmp_path)
    assert nativedialogs.portal_installed({"XDG_DATA_DIRS": f"/nonexistent:{share}"}) is True


def test_portal_service_missing_everywhere(tmp_path):
    assert nativedialogs.portal_installed({"XDG_DATA_DIRS": str(tmp_path)}) is False


def test_portal_service_default_data_dirs_are_searched(monkeypatch, tmp_path):
    share = _service(tmp_path)
    monkeypatch.setattr(nativedialogs, "DEFAULT_DATA_DIRS", (share,))
    assert nativedialogs.portal_installed({}) is True


def test_inside_flatpak_the_portal_is_assumed(tmp_path):
    marker = tmp_path / ".flatpak-info"
    marker.touch()
    assert nativedialogs.portal_installed({}, flatpak_info=marker) is True


def test_no_portal_service_changes_nothing():
    assert (
        nativedialogs.choose_platform_theme(
            {}, has_theme=lambda name: True, session_bus=True, portal_present=False
        )
        is None
    )


def test_portal_service_present_still_selects_the_portal():
    assert (
        nativedialogs.choose_platform_theme(
            {}, has_theme=lambda name: True, session_bus=True, portal_present=True
        )
        == PORTAL
    )


def test_qt_plugin_path_directories_are_searched(tmp_path):
    folder = tmp_path / "plugins" / "platformthemes"
    folder.mkdir(parents=True)
    (folder / "libqkde.so").touch()
    env = {"QT_PLUGIN_PATH": f"/nonexistent:{tmp_path / 'plugins'}"}
    assert nativedialogs.qt_theme_available("kde", env=env, library_dir=None) is True
    assert nativedialogs.qt_theme_available("lxqt", env=env, library_dir=None) is False


def test_the_library_plugin_directory_is_searched_too(tmp_path):
    folder = tmp_path / "platformthemes"
    folder.mkdir()
    (folder / "libqgtk3.so").touch()
    assert nativedialogs.qt_theme_available("gtk3", env={}, library_dir=tmp_path) is True
