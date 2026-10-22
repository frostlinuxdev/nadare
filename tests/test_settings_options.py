# SPDX-License-Identifier: GPL-3.0-or-later
"""Settings page: the generated Game options card."""

from __future__ import annotations

import os

os.environ.setdefault("QT_QPA_PLATFORM", "offscreen")

import pytest

try:  # pragma: no cover - only when PyQt6 is missing
    from PyQt6.QtWidgets import QApplication
except Exception:  # noqa: BLE001
    pytest.skip("PyQt6 is not importable in this interpreter", allow_module_level=True)

from nadare.core.backends import get, kinds
from nadare.core.config import Config
from nadare.gui.settings import SettingsPage, _env_to_text, _text_to_env


@pytest.fixture(scope="module")
def qapp():
    app = QApplication.instance()
    if app is None:
        app = QApplication([])
    yield app


def _config(tmp_path, kind: str, name: str = "sample", **profile):
    config = Config.load(tmp_path / "config.json")
    config.add_profile(name, backend=kind, **profile)
    return config


def test_folders_do_not_exist_warning_is_non_blocking(tmp_path, qapp):
    config = _config(tmp_path, "wine")
    page = SettingsPage()
    try:
        page.load(config, "sample")
        page.option_widget("prefix").setText(str(tmp_path / "not-there"))
        page.save()
        assert config.profile("sample")["prefix"] == str(tmp_path / "not-there")
        assert "prefix" in page.options_note.text().lower()
    finally:
        page.deleteLater()
        qapp.processEvents()


def test_every_backend_renders_all_options(tmp_path, qapp):
    page = SettingsPage()
    try:
        for kind in kinds():
            config = _config(tmp_path / f"{kind}.json", kind)
            page.load(config, "sample")
            for option in get(kind).options:
                assert page.option_widget(option.key) is not None, f"{kind}:{option.key}"
    finally:
        page.deleteLater()
        qapp.processEvents()


def test_edit_and_save_writes_top_level_key(tmp_path, qapp):
    config = _config(tmp_path, "wine")
    page = SettingsPage()
    try:
        page.load(config, "sample")
        page.option_widget("exe_name").setText("bin/game.exe")
        page.save()
        assert config.profile("sample")["exe_name"] == "bin/game.exe"
    finally:
        page.deleteLater()
        qapp.processEvents()


def test_clearing_a_field_removes_the_key_from_top_level_and_extra(tmp_path, qapp):
    config = _config(tmp_path, "wine", exe_name="x.exe")
    config.profile("sample")["extra"] = {"exe_name": "old.exe", "process_match": "keep"}
    page = SettingsPage()
    try:
        page.load(config, "sample")
        assert page.option_widget("exe_name").text() == "x.exe"
        page.option_widget("exe_name").setText("")
        page.save()
        profile = config.profile("sample")
        assert "exe_name" not in profile
        assert "exe_name" not in profile["extra"]
        assert profile["extra"]["process_match"] == "keep"
    finally:
        page.deleteLater()
        qapp.processEvents()


def test_reads_values_from_extra(tmp_path, qapp):
    config = _config(tmp_path, "wine")
    config.profile("sample")["extra"] = {"exe_name": "from-extra.exe"}
    page = SettingsPage()
    try:
        page.load(config, "sample")
        assert page.option_widget("exe_name").text() == "from-extra.exe"
    finally:
        page.deleteLater()
        qapp.processEvents()


def test_advanced_toggle_hides_and_shows_rows(tmp_path, qapp):
    config = _config(tmp_path, "wine")
    page = SettingsPage()
    try:
        page.load(config, "sample")
        advanced = page.option_widget("wine_binary")
        assert advanced is not None
        assert not advanced.isVisibleTo(page), "advanced options start hidden"
        page.show_advanced.setChecked(True)
        assert advanced.isVisibleTo(page)
        page.show_advanced.setChecked(False)
        assert not advanced.isVisibleTo(page)
        # A basic option is never hidden by the toggle.
        assert page.option_widget("exe_name").isVisibleTo(page)
    finally:
        page.deleteLater()
        qapp.processEvents()


def test_env_is_edited_as_key_value_lines(tmp_path, qapp):
    assert _text_to_env("DXVK_HUD=fps\n\n# comment\nFOO=bar") == {
        "DXVK_HUD": "fps",
        "FOO": "bar",
    }
    config = _config(tmp_path, "wine")
    page = SettingsPage()
    try:
        page.load(config, "sample")
        editor = page.option_widget("extra_env")
        editor.setPlainText("DXVK_HUD=fps\nWINE_CPUCOUNT=8")
        page.save()
        assert config.profile("sample")["extra_env"] == {
            "DXVK_HUD": "fps",
            "WINE_CPUCOUNT": "8",
        }
        page.load(config, "sample")
        editor = page.option_widget("extra_env")
        assert "DXVK_HUD=fps" in editor.toPlainText()
        # Clearing the box removes the whole mapping.
        editor.setPlainText("")
        page.save()
        assert "extra_env" not in config.profile("sample")
    finally:
        page.deleteLater()
        qapp.processEvents()


def test_int_options_use_a_bounded_spin_box(tmp_path, qapp):
    config = _config(tmp_path, "arknights-yostar")
    page = SettingsPage()
    try:
        page.load(config, "sample")
        spin = page.option_widget("download_workers")
        assert (spin.minimum(), spin.maximum()) == (1, 16)
        spin.setValue(3)
        page.save()
        assert config.profile("sample")["download_workers"] == 3
    finally:
        page.deleteLater()
        qapp.processEvents()


def test_choice_option_renders_a_combo(tmp_path, qapp):
    config = _config(tmp_path, "wine")
    page = SettingsPage()
    try:
        page.load(config, "sample")
        combo = page.option_widget("renderer")
        assert combo.findData("auto") >= 0
        target = next(i for i in range(combo.count()) if combo.itemData(i) != "auto")
        combo.setCurrentIndex(target)
        page.save()
        assert config.profile("sample")["renderer"] == combo.itemData(target)
    finally:
        page.deleteLater()
        qapp.processEvents()


def test_env_to_text_is_sorted_and_stable() -> None:
    assert _env_to_text({"B": "2", "A": "1"}) == "A=1\nB=2"
    assert _env_to_text(None) == ""


def test_loading_another_backend_rebuilds_the_option_editors(tmp_path, qapp):
    page = SettingsPage()
    try:
        page.load(_config(tmp_path / "a.json", "wine"), "sample")
        assert page.option_widget("launch_args") is not None
        page.load(_config(tmp_path / "b.json", "steam"), "sample")
        assert page.option_widget("launch_args") is None
    finally:
        page.deleteLater()
        qapp.processEvents()


def test_switching_backends_does_not_leak_option_editors(tmp_path, qapp):
    """The old options form is a nested layout, which ``takeAt().widget()``
    returns None for, so its editors used to survive every rebuild and render
    over the current backend's (#47)."""
    from PyQt6.QtCore import QEvent
    from PyQt6.QtWidgets import QLineEdit

    page = SettingsPage()
    try:
        page.show()
        kind_cycle = ["wine", "arknights-yostar", "stove-czn", "wine", "arknights-yostar"]
        counts = []
        for i, kind in enumerate(kind_cycle):
            config = _config(tmp_path / f"{i}.json", kind)
            page.load(config, "sample")
            qapp.processEvents()
            # deleteLater() is only honoured for DeferredDelete events
            qapp.sendPostedEvents(None, QEvent.Type.DeferredDelete.value)
            counts.append(len(page._options_host.findChildren(QLineEdit)))
        # the same backend yields the same number of editors, rebuilt or not
        assert counts[0] == counts[3] and counts[1] == counts[4], counts
        # every editor still in the host belongs to the current form
        current = {id(w) for w in page._option_widgets.values()}
        strays = [
            w
            for w in page._options_host.findChildren(QLineEdit)
            if id(w) not in current and w.isVisibleTo(page)
        ]
        assert strays == [], f"{len(strays)} stale editor(s) still visible"
    finally:
        page.deleteLater()
        qapp.processEvents()


def test_an_unset_bool_option_takes_the_options_default(qapp):
    """An unset key means "use the default", not "off" (#214)."""
    from PyQt6.QtWidgets import QCheckBox

    from nadare.core.backends import Option

    page = SettingsPage()
    try:
        on_by_default = Option("x", "X", "bool", "help", default=True)
        off_by_default = Option("y", "Y", "bool", "help", default=False)
        box = QCheckBox()
        page._set_editor(on_by_default, box, None)
        assert box.isChecked()
        page._set_editor(on_by_default, box, False)
        assert not box.isChecked(), "an explicit false stays off"
        page._set_editor(off_by_default, box, None)
        assert not box.isChecked()
        page._set_editor(off_by_default, box, True)
        assert box.isChecked()
    finally:
        page.deleteLater()
        qapp.processEvents()


def test_czn_has_no_visual_cpp_runtime_toggle(tmp_path, qapp):
    """The VC++ runtime is always installed for CZN, so Settings must not offer to skip it."""
    config = _config(tmp_path, "stove-czn")
    page = SettingsPage()
    try:
        page.load(config, "sample")
        assert page.option_widget("vcredist") is None
        page.save()
        assert "vcredist" not in config.profile("sample")
    finally:
        page.deleteLater()
        qapp.processEvents()


def test_settings_diet_options_are_gone_from_the_shipped_backends(qapp):
    """Review 2026-10-21: no VC++ box for Arknights/Stella Sora, no MangoHud box for CZN."""
    from nadare.core import backends as core_backends

    def keys(kind):
        return {option.key for option in core_backends.get(kind).options}

    assert "vcredist" not in keys("arknights-yostar")
    assert "vcredist" not in keys("stellasora-yostar")
    assert "vcredist" not in keys("stove-czn")
    assert "mangohud" not in keys("stove-czn")
    assert "vcredist" in keys("wine"), "the generic wine backend keeps its opt-in"


def test_czn_display_help_does_not_recommend_x11(qapp):
    from nadare.core import backends as core_backends

    display = next(o for o in core_backends.get("stove-czn").options if o.key == "display")
    assert "tested" in display.help.lower() and "experimental" in display.help.lower()
    assert "try it if" not in display.help


def test_unmatched_quote_in_launch_args_is_warned_about(tmp_path, qapp, monkeypatch):
    shown = []
    monkeypatch.setattr(
        "nadare.gui.settings.QMessageBox.open", lambda self, *a: shown.append(self.text())
    )
    config = _config(tmp_path, "wine")
    page = SettingsPage()
    try:
        page.load(config, "sample")
        page.option_widget("launch_args").setText('--flag "oops')
        page.save()
        assert shown and "no closing quote" in shown[0]
        assert config.profile("sample")["launch_args"] == '--flag "oops'
    finally:
        page.deleteLater()
        qapp.processEvents()


def test_legacy_runner_and_prefix_show_and_save_under_canonical_keys(tmp_path, qapp):
    """Old beta aliases must not leave Runner/Wine prefix blank or shadow a save."""
    config = _config(tmp_path, "wine")
    profile = config.profile("sample")
    profile.pop("runner", None)
    profile["proton_runner"] = "/old/runner"
    profile["wineprefix"] = "/old/prefix"
    page = SettingsPage()
    try:
        page.load(config, "sample")
        assert page.runner.currentData() == "/old/runner"
        assert page.option_widget("prefix").text() == "/old/prefix"

        page.save()

        profile = config.profile("sample")
        assert profile["runner"] == "/old/runner"
        assert profile["prefix"] == "/old/prefix"
        assert "proton_runner" not in profile
        assert "wineprefix" not in profile
    finally:
        page.deleteLater()
        qapp.processEvents()
