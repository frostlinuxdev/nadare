# SPDX-License-Identifier: GPL-3.0-or-later
"""Shared locale contract through core, GUI and Settings entry points."""

from __future__ import annotations

import json
from pathlib import Path

import pytest

from nadare.core import i18n
from nadare.gui import i18n as gui_i18n


@pytest.fixture(autouse=True)
def isolated_locale_state(monkeypatch):
    monkeypatch.setattr(i18n, "_current", "en")
    monkeypatch.setattr(i18n, "_cache", {})
    monkeypatch.setattr(gui_i18n, "_qt_translator", None)


def _write_locale(directory: Path, code: str, data: dict) -> None:
    directory.mkdir(parents=True, exist_ok=True)
    (directory / f"{code}.json").write_text(json.dumps(data), encoding="utf-8")


def test_translate_falls_back_to_english_then_key(tmp_path: Path, monkeypatch) -> None:
    monkeypatch.setattr(i18n, "locale_dir", lambda: tmp_path)
    _write_locale(tmp_path, "en", {"hello": "Hello"})

    assert i18n.translate("hello", "de") == "Hello"  # de missing -> English
    assert i18n.translate("absent", "de") == "absent"  # -> key itself


def test_translate_formats_kwargs(tmp_path: Path, monkeypatch) -> None:
    monkeypatch.setattr(i18n, "locale_dir", lambda: tmp_path)
    _write_locale(tmp_path, "en", {"greet": "Hi {name}"})
    assert i18n.translate("greet", "en", name="Ada") == "Hi Ada"


def test_transient_load_failure_is_not_cached(tmp_path: Path, monkeypatch) -> None:
    """L20: a missing/unreadable locale must be retried, not cached forever."""
    monkeypatch.setattr(i18n, "locale_dir", lambda: tmp_path)

    # No file yet: the key is returned and nothing is cached.
    assert i18n.translate("hello", "xx") == "hello"
    assert "xx" not in i18n._cache

    # The file becomes available; a later lookup must pick it up.
    _write_locale(tmp_path, "xx", {"hello": "hola"})
    assert i18n.translate("hello", "xx") == "hola"


@pytest.fixture
def catalogues(tmp_path, monkeypatch):
    directory = tmp_path / "locales"
    _write_locale(directory, "en", {"audit.key": "English fallback"})
    _write_locale(directory, "xx", {})
    monkeypatch.setattr(i18n, "locale_dir", lambda: directory)
    return directory


def test_core_and_gui_have_the_same_english_fallback(catalogues):
    i18n.set_locale("xx")
    gui_i18n.set_language("xx")
    assert i18n.translate("audit.key") == gui_i18n.tr("audit.key") == "English fallback"


def test_shipped_english_strings_are_unchanged():
    catalogue = json.loads((i18n.locale_dir() / "en.json").read_text(encoding="utf-8"))
    assert i18n.available_locales() == gui_i18n.available() == ["en"]
    for key, value in catalogue.items():
        expected = value["other"] if isinstance(value, dict) else value
        assert i18n.translate(key) == gui_i18n.tr(key) == expected


@pytest.fixture(params=["core", "gui"])
def entrypoint(request):
    if request.param == "core":
        return i18n.set_locale, i18n.translate
    return gui_i18n.set_language, gui_i18n.tr


@pytest.mark.parametrize("code", ["missing", "xx"])
def test_missing_locale_or_key_is_safe(catalogues, entrypoint, code):
    select, translate = entrypoint
    select(code)
    assert translate("audit.key") == "English fallback"
    assert translate("absent") == "absent"


@pytest.mark.parametrize("template", ["Hi {name}", "Hi {", "Hi {}"])
def test_formatting_mismatch_preserves_template(catalogues, entrypoint, template):
    _write_locale(catalogues, "xx", {"greet": template})
    select, translate = entrypoint
    select("xx")
    assert translate("greet", wrong="Ada") == template


def test_both_entrypoints_format_placeholders(catalogues, entrypoint):
    _write_locale(catalogues, "xx", {"greet": "Hi {name}"})
    select, translate = entrypoint
    select("xx")
    assert translate("greet", name="Ada") == "Hi Ada"


@pytest.mark.parametrize("code", ["../x", "en/../../etc", "/absolute", "en\0"])
def test_unsafe_locale_never_reads_outside_catalogues(catalogues, entrypoint, monkeypatch, code):
    # Readable traversal targets ensure rejection cannot pass by accident.
    for target in ("x", "etc", "absolute"):
        _write_locale(catalogues.parent, target, {"audit.key": "leaked"})
    if code == "/absolute":
        code = str(catalogues.parent / "absolute")
    reads = []
    original = Path.read_text

    def read(path, *args, **kwargs):
        reads.append(path)
        return original(path, *args, **kwargs)

    monkeypatch.setattr(Path, "read_text", read)
    select, translate = entrypoint
    select(code)
    assert translate("audit.key") == "English fallback"
    assert i18n.translate("audit.key", code) == "English fallback"
    assert i18n.Translator(code)("audit.key") == "English fallback"
    assert all(path.parent == catalogues and path.name == "en.json" for path in reads)


@pytest.mark.parametrize("failure", ["unavailable", "malformed", "not-object"])
def test_both_entrypoints_retry_failed_catalogues(catalogues, entrypoint, monkeypatch, failure):
    target = catalogues / "xx.json"
    if failure == "unavailable":
        original = Path.read_text
        failed = False

        def read(path, *args, **kwargs):
            nonlocal failed
            if path == target and not failed:
                failed = True
                raise OSError("temporarily unavailable")
            return original(path, *args, **kwargs)

        monkeypatch.setattr(Path, "read_text", read)
    else:
        target.write_text("{" if failure == "malformed" else "[]", encoding="utf-8")
    select, translate = entrypoint
    select("xx")
    assert translate("audit.key") == "English fallback"
    _write_locale(catalogues, "xx", {"audit.key": "Restored"})
    assert translate("audit.key") == "Restored"


def test_both_entrypoints_retry_a_missing_catalogue(catalogues, entrypoint):
    select, translate = entrypoint
    select("xx")
    (catalogues / "xx.json").unlink()
    assert translate("audit.key") == "English fallback"
    _write_locale(catalogues, "xx", {"audit.key": "Restored"})
    assert translate("audit.key") == "Restored"


def test_selection_is_shared_in_both_directions(catalogues, entrypoint):
    _write_locale(catalogues, "xx", {"audit.key": "Selected"})
    select, _translate = entrypoint
    select("xx")
    assert i18n.get_locale() == gui_i18n.language() == "xx"
    assert i18n.translate("audit.key") == gui_i18n.tr("audit.key") == "Selected"
    select("missing")
    assert i18n.get_locale() == gui_i18n.language() == "en"


@pytest.mark.parametrize("app_only", [True, False])
def test_settings_save_changes_future_translations_immediately(catalogues, qtbot, app_only):
    from PyQt6.QtWidgets import QLabel

    from nadare.core.config import Config
    from nadare.gui.settings import SettingsPage

    _write_locale(catalogues, "xx", {"audit.key": "Selected"})
    config = Config.load(catalogues.parent / "config.json")
    page = SettingsPage()
    qtbot.addWidget(page)
    if app_only:
        page.load_app_only(config)
    else:
        config.add_profile("sample", backend="wine")
        page.load(config, "sample")
    existing_label = QLabel(gui_i18n.tr("audit.key"))
    qtbot.addWidget(existing_label)
    page.language.setCurrentIndex(page.language.findData("xx"))
    assert i18n.get_locale() == gui_i18n.language() == "en"
    page.save()
    assert Config.load(config.path).data["language"] == "xx"
    assert i18n.get_locale() == gui_i18n.language() == "xx"
    assert i18n.translate("audit.key") == gui_i18n.tr("audit.key") == "Selected"
    assert existing_label.text() == "English fallback"


# ---------------------------------------------------------------------------
# Locale resolver (#1602 item 5)
# ---------------------------------------------------------------------------


@pytest.mark.parametrize(
    ("setting", "environ", "available", "expected"),
    [
        # The Nadare setting wins over every environment variable.
        ("de", {"LC_ALL": "fr_FR.UTF-8", "LANG": "fr_FR.UTF-8"}, ["en", "de", "fr"], "de"),
        # A full code is kept when its catalogue exists, then its language.
        ("de_DE", {}, ["en", "de_DE", "de"], "de_DE"),
        ("de_DE", {}, ["en", "de"], "de"),
        # Encoding and modifier are stripped before the catalogue is looked up.
        ("de_DE.UTF-8@euro", {}, ["en", "de_DE", "de"], "de_DE"),
        ("de_DE.UTF-8@euro", {}, ["en", "de"], "de"),
        ("de-DE", {}, ["en", "de"], "de"),
        # C and POSIX mean English, from the setting or from the environment.
        ("C", {"LANG": "de_DE.UTF-8"}, ["en", "de"], "en"),
        ("POSIX", {"LANG": "de_DE.UTF-8"}, ["en", "de"], "en"),
        (None, {"LC_ALL": "C", "LANG": "de_DE.UTF-8"}, ["en", "de"], "en"),
        (None, {"LC_ALL": "POSIX", "LANG": "de_DE.UTF-8"}, ["en", "de"], "en"),
        # Environment order: LC_ALL, LC_MESSAGES, LANG, LANGUAGE's first entry.
        (
            None,
            {"LC_ALL": "de_DE.UTF-8", "LC_MESSAGES": "fr_FR.UTF-8", "LANG": "it_IT.UTF-8"},
            ["en", "de", "fr", "it"],
            "de",
        ),
        (
            None,
            {"LC_ALL": "", "LC_MESSAGES": "fr_FR.UTF-8", "LANG": "de_DE.UTF-8"},
            ["en", "de", "fr"],
            "fr",
        ),
        (None, {"LC_ALL": "", "LC_MESSAGES": "", "LANG": "de_DE.UTF-8"}, ["en", "de"], "de"),
        (None, {"LANGUAGE": "fr_FR:de_DE"}, ["en", "de", "fr"], "fr"),
        (None, {"LANGUAGE": "fr:de"}, ["en", "de", "fr"], "fr"),
        # A setting with no catalogue falls through to the environment.
        ("xx", {"LANG": "de_DE.UTF-8"}, ["en", "de"], "de"),
        # Unsafe or absent values never select a locale outside the catalogues.
        ("../../../etc/passwd", {"LANG": "de_DE.UTF-8"}, ["en", "de"], "de"),
        ("", {"LANG": "de_DE.UTF-8"}, ["en", "de"], "de"),
        ("", {}, ["en", "de"], "en"),
        (None, {}, ["en", "de"], "en"),
    ],
)
def test_resolve_locale_table(tmp_path: Path, monkeypatch, setting, environ, available, expected):
    directory = tmp_path / "locales"
    monkeypatch.setattr(i18n, "locale_dir", lambda: directory)
    for code in available:
        _write_locale(directory, code, {})
    assert i18n.resolve_locale(setting, environ) == expected


def test_resolve_locale_reads_the_process_environment_by_default(tmp_path, monkeypatch):
    monkeypatch.setattr(i18n, "locale_dir", lambda: tmp_path)
    _write_locale(tmp_path, "en", {})
    _write_locale(tmp_path, "de", {})
    for name in ("LC_ALL", "LC_MESSAGES", "LANGUAGE"):
        monkeypatch.delenv(name, raising=False)
    monkeypatch.setenv("LANG", "de_DE.UTF-8@euro")
    assert i18n.resolve_locale() == "de"


# ---------------------------------------------------------------------------
# Qt's own button texts (#1602 item 4)
# ---------------------------------------------------------------------------


class _FakeApplication:
    def __init__(self) -> None:
        self.installed: list[object] = []

    def installTranslator(self, translator: object) -> None:  # noqa: N802 - Qt naming
        self.installed.append(translator)


def _recording_loader():
    calls: list[tuple[str, Path]] = []
    translator = object()

    def load(code: str, path: Path):
        calls.append((code, path))
        return translator

    return load, calls, translator


def test_qt_translator_is_loaded_for_german(tmp_path: Path) -> None:
    (tmp_path / "qtbase_de.qm").write_bytes(b"")
    application = _FakeApplication()
    load, calls, translator = _recording_loader()

    loaded = gui_i18n.install_qt_translations(
        application, "de", loader=load, translations_dir=tmp_path
    )

    assert loaded is True
    assert calls == [("de", tmp_path / "qtbase_de.qm")]
    assert application.installed == [translator]
    assert gui_i18n._qt_translator is translator  # kept alive for Qt


def test_qt_translator_falls_back_to_the_language_code(tmp_path: Path) -> None:
    (tmp_path / "qtbase_de.qm").write_bytes(b"")
    application = _FakeApplication()
    load, calls, _translator = _recording_loader()

    assert gui_i18n.install_qt_translations(
        application, "de_DE.UTF-8", loader=load, translations_dir=tmp_path
    )
    assert [code for code, _path in calls] == ["de"]


def test_qt_translator_is_not_loaded_for_english(tmp_path: Path) -> None:
    (tmp_path / "qtbase_en.qm").write_bytes(b"")
    application = _FakeApplication()

    def load(code, path):  # pragma: no cover - the guard must keep it unused
        raise AssertionError(f"loaded {code} from {path}")

    assert not gui_i18n.install_qt_translations(
        application, "en", loader=load, translations_dir=tmp_path
    )
    assert not gui_i18n.install_qt_translations(
        application, "en_GB", loader=load, translations_dir=tmp_path
    )
    assert application.installed == []


def test_qt_translator_missing_file_means_english(tmp_path: Path) -> None:
    application = _FakeApplication()
    load, calls, _translator = _recording_loader()

    assert not gui_i18n.install_qt_translations(
        application, "de", loader=load, translations_dir=tmp_path
    )
    assert calls == []
    assert application.installed == []
    assert gui_i18n._qt_translator is None


def test_main_window_loads_qt_translations_at_start(tmp_path: Path, monkeypatch, qapp) -> None:
    """The window asks for the resolved language's Qt texts before it builds widgets."""
    from nadare.core.config import Config
    from nadare.gui.main_window import MainWindow

    directory = tmp_path / "locales"
    monkeypatch.setattr(i18n, "locale_dir", lambda: directory)
    _write_locale(directory, "en", {})
    _write_locale(directory, "de", {})
    calls: list[str | None] = []

    def install(application, language=None):
        calls.append(language)
        return True

    monkeypatch.setattr(gui_i18n, "install_qt_translations", install)
    config = Config.load(tmp_path / "config.json")
    config.data["language"] = "de_DE.UTF-8"  # the resolver strips this to "de"
    window = MainWindow(config)
    try:
        assert calls == ["de"]
        assert gui_i18n.language() == "de"
    finally:
        window.close()
        window.deleteLater()
        qapp.processEvents()
        gui_i18n.set_language("en")


def test_real_qt_translator_file_loads() -> None:
    translations = gui_i18n._qt_translations_dir()
    if not (translations / "qtbase_de.qm").is_file():
        pytest.skip("this PyQt6 build ships no German Qt translations")
    application = _FakeApplication()

    assert gui_i18n.install_qt_translations(application, "de", translations_dir=translations)
    assert len(application.installed) == 1


def test_resolver_uses_documented_environment_precedence(tmp_path, monkeypatch):
    monkeypatch.setattr(i18n, "locale_dir", lambda: tmp_path)
    for code in ("en", "de", "fr"):
        _write_locale(tmp_path, code, {})
    assert i18n.resolve_locale(None, {"NADARE_LANG": "fr", "LC_ALL": "de"}) == "de"


@pytest.mark.parametrize("saved", [{}, {"language": None}, {"language": "en"}])
def test_unset_config_language_follows_desktop(tmp_path, monkeypatch, saved, qtbot):
    from nadare import app
    from nadare.core.config import Config
    from nadare.gui.settings import SettingsPage

    locales = tmp_path / "locales"
    monkeypatch.setattr(i18n, "locale_dir", lambda: locales)
    for code in ("en", "de"):
        _write_locale(locales, code, {})
    monkeypatch.setenv("LC_ALL", "de_DE.UTF-8")
    path = tmp_path / "config.json"
    path.write_text(json.dumps(saved), encoding="utf-8")
    config = Config.load(path)
    expected = "en" if saved.get("language") == "en" else "de"
    assert gui_i18n.resolve_locale(config.data.get("language")) == expected
    page = SettingsPage()
    qtbot.addWidget(page)
    page.load_app_only(config)
    assert page.language.currentData() == expected
    config.save()
    assert gui_i18n.resolve_locale(Config.load(path).data.get("language")) == expected
    assert app.run_cli(["-c", str(path), "list"]) == 0
    assert i18n.get_locale() == expected


def test_saving_other_settings_keeps_an_unset_language_unset(tmp_path, monkeypatch, qtbot):
    """Saving Settings without touching Language must not pin today's desktop language."""
    from nadare.core.config import Config
    from nadare.gui.settings import SettingsPage

    locales = tmp_path / "locales"
    monkeypatch.setattr(i18n, "locale_dir", lambda: locales)
    for code in ("en", "de"):
        _write_locale(locales, code, {})
    monkeypatch.setenv("LC_ALL", "de_DE.UTF-8")
    path = tmp_path / "config.json"
    path.write_text("{}", encoding="utf-8")
    config = Config.load(path)
    page = SettingsPage()
    qtbot.addWidget(page)
    page.load_app_only(config)
    page.check_updates.setChecked(not page.check_updates.isChecked())
    page._save_app_only()
    assert Config.load(path).data.get("language") is None
    assert i18n.get_locale() == "de"

    page.language.setCurrentIndex(page.language.findData("en"))
    page._save_app_only()
    assert Config.load(path).data.get("language") == "en"
