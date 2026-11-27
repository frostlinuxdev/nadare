# SPDX-License-Identifier: GPL-3.0-or-later
"""#806: the Safety check in the game page and the System check window."""

from __future__ import annotations

import os
import tempfile
import threading
import time
from pathlib import Path

os.environ.setdefault("QT_QPA_PLATFORM", "offscreen")

import pytest

try:
    from PyQt6.QtWidgets import QApplication
except Exception:  # pragma: no cover - optional GUI dependencies
    pytest.skip("PyQt6 is not importable in this interpreter", allow_module_level=True)

from nadare.core import safetycheck
from nadare.core.config import Config
from nadare.core.safetycheck import Match, SafetyResult
from nadare.gui.game_view import GameView
from nadare.gui.main_window import MainWindow
from nadare.gui.safety_check import SafetyResultDialog
from nadare.gui.system_check import SystemCheckDialog


@pytest.fixture(scope="module")
def qapp():
    return QApplication.instance() or QApplication([])


def _result(level: str, *, dismissed: bool = False) -> SafetyResult:
    matches = ()
    if level in ("exposed", "compromised"):
        matches = (Match(id="fake-marker", count=2, level=level),)
    return SafetyResult(
        level=level,
        matches=matches,
        checked_at="2026-11-19T10:00:00+00:00",
        files_visited=12,
        dismissed=dismissed,
    )


@pytest.fixture
def view(qapp):
    page = GameView()
    page.resize(1100, 900)
    page.set_profile("pgr", {"backend": "direct-pgr", "title": "PGR"})
    yield page
    page.deleteLater()
    qapp.processEvents()


def _wait(qapp, predicate, timeout: float = 5.0) -> bool:
    deadline = time.monotonic() + timeout
    while time.monotonic() < deadline:
        qapp.processEvents()
        if predicate():
            return True
        time.sleep(0.005)
    return predicate()


def _shots() -> Path:
    default = Path(tempfile.gettempdir()) / "nadare-safety-shots"
    folder = Path(os.environ.get("NADARE_SAFETY_SHOTS", default))
    try:
        folder.mkdir(parents=True, exist_ok=True)
        probe = folder / ".write-probe"
        probe.write_text("")
        probe.unlink()
    except OSError:
        folder = Path(os.environ.get("TMPDIR", "/tmp")) / "nadare-safety-shots"
        folder.mkdir(parents=True, exist_ok=True)
    return folder


# ---- the game page -------------------------------------------------------------------------
def test_the_page_has_a_manual_safety_check_action(view):
    seen: list[str] = []
    view.safety_check_requested.connect(seen.append)
    assert view.safety.text() == "Safety check"
    view.safety.click()
    assert seen == ["pgr"]


def test_a_clean_result_says_clean_and_says_what_clean_means(view, qapp):
    view.set_safety_result(_result("clean"))
    assert view.safety_status.text().startswith("Clean")
    assert "reinstall" in view.safety_status.text().lower()
    assert view.safety_banner.isHidden()
    view.show()
    qapp.processEvents()
    view.grab().save(str(_shots() / "safety-clean.png"))


def test_an_exposed_result_says_exposed_without_a_banner(view, qapp):
    view.set_safety_result(_result("exposed"))
    assert view.safety_status.text().startswith("Exposed")
    assert view.safety_banner.isHidden()
    view.show()
    qapp.processEvents()
    view.grab().save(str(_shots() / "safety-exposed.png"))


def test_a_compromised_result_shows_the_persistent_banner(view, qapp):
    view.set_safety_result(_result("compromised"))
    assert view.safety_status.text().startswith("Compromised")
    assert not view.safety_banner.isHidden()
    view.show()
    qapp.processEvents()
    view.grab().save(str(_shots() / "safety-compromised.png"))


def test_a_dismissed_compromised_result_shows_no_banner(view):
    view.set_safety_result(_result("compromised", dismissed=True))
    assert view.safety_banner.isHidden()
    assert view.safety_status.text().startswith("Compromised")


def test_the_banner_dismiss_button_tells_the_window_and_hides(view):
    seen: list[str] = []
    view.safety_dismissed.connect(seen.append)
    view.set_safety_result(_result("compromised"))
    view.safety_dismiss.click()
    assert seen == ["pgr"]
    assert view.safety_banner.isHidden()


def test_a_no_list_result_leaves_the_page_clean(view, qapp):
    """Nothing was checked, so the page keeps no sentence about it (b28)."""
    view.set_safety_result(_result("no_list"))
    assert view.safety_status.isHidden()
    assert view.safety_status.text() == ""
    assert view.safety_banner.isHidden()
    view.show()
    qapp.processEvents()
    view.grab().save(str(_shots() / "safety-no-list-page.png"))


def test_a_no_list_result_replaces_an_older_finding_on_the_page(view):
    view.set_safety_result(_result("exposed"))
    view.set_safety_result(_result("no_list"))
    assert view.safety_status.isHidden()
    assert view.safety_banner.isHidden()


def test_a_cancelled_result_leaves_the_previous_line_alone(view):
    view.set_safety_result(_result("exposed"))
    before = view.safety_status.text()
    view.set_safety_result(_result("cancelled"))
    assert view.safety_status.text() == before


def test_switching_games_clears_the_previous_banner(view, qapp):
    view.set_safety_result(_result("compromised"))
    assert not view.safety_banner.isHidden()
    view.set_profile("other", {"backend": "direct-pgr", "title": "Other"})
    assert view.safety_banner.isHidden()
    assert view.safety_status.isHidden()


# ---- the System check window ---------------------------------------------------------------
def test_the_system_check_window_shows_the_stored_results(qapp, monkeypatch):
    monkeypatch.setattr(
        safetycheck, "load_all_results", lambda **kwargs: {"pgr": _result("compromised")}
    )
    dialog = SystemCheckDialog(None, {"profiles": {}})
    try:
        text = dialog.safety_label.text()
        assert "pgr" in text and "Compromised" in text
    finally:
        dialog.deleteLater()


def test_the_system_check_window_says_when_the_check_has_not_run(qapp, monkeypatch):
    monkeypatch.setattr(safetycheck, "load_all_results", lambda **kwargs: {})
    dialog = SystemCheckDialog(None, {"profiles": {}})
    try:
        assert "not run" in dialog.safety_label.text().lower()
    finally:
        dialog.deleteLater()


# ---- the window wiring ---------------------------------------------------------------------
def _config(tmp_path: Path) -> Config:
    prefix = tmp_path / "prefix"
    prefix.mkdir()
    (prefix / "bad.example").write_text("")
    config = Config.load(tmp_path / "config.json")
    config.add_profile(
        "pgr",
        backend="direct-pgr",
        title="PGR",
        prefix=str(prefix),
    )
    config.save()
    return config


def test_the_manual_action_runs_off_the_gui_thread_and_shows_the_result(
    tmp_path, qapp, monkeypatch
):
    indicator = safetycheck.parse_indicators(
        {
            "version": 1,
            "serial": 1,
            "indicators": [
                {
                    "id": "fake-marker",
                    "title": "Example marker",
                    "kinds": ["*"],
                    "level": "exposed",
                    "rule": {"type": "path_exists", "glob": "bad.example"},
                }
            ],
        }
    )[0][0]
    threads: list[threading.Thread] = []
    real_check = safetycheck.check_profile

    def checked(*args, **kwargs):
        threads.append(threading.current_thread())
        return real_check(*args, **kwargs)

    monkeypatch.setattr(safetycheck, "load_indicators", lambda **kwargs: [indicator])
    monkeypatch.setattr(safetycheck, "check_profile", checked)
    monkeypatch.setattr(safetycheck, "save_result", lambda *args, **kwargs: None)
    config = _config(tmp_path)
    window = MainWindow(config)
    try:
        window.open_game("pgr")
        window.game.safety.click()
        assert _wait(qapp, lambda: window.game.safety_status.text().startswith("Exposed"))
        assert threads and threads[0] is not threading.main_thread()
        assert not window.game.safety.isEnabled() or window.game.safety.text() == "Safety check"
    finally:
        dialog = getattr(window, "_safety_dialog", None)
        if dialog is not None:
            dialog.close()
        window.controller._workers.clear()
        window.close()
        window.deleteLater()
        qapp.processEvents()


def test_opening_a_game_shows_its_stored_result(tmp_path, qapp, monkeypatch):
    stored = _result("compromised")
    asked: list[str] = []

    def load(profile, **kwargs):
        asked.append(profile)
        return stored

    monkeypatch.setattr(safetycheck, "load_result", load)
    config = _config(tmp_path)
    window = MainWindow(config)
    try:
        window.open_game("pgr")
        assert asked and set(asked) == {"pgr"}
        assert not window.game.safety_banner.isHidden()
        assert window.game.safety_status.text().startswith("Compromised")
    finally:
        window.controller._workers.clear()
        window.close()
        window.deleteLater()
        qapp.processEvents()


def test_the_dismissed_banner_is_remembered_for_the_profile(tmp_path, qapp, monkeypatch):
    stored = _result("compromised")
    monkeypatch.setattr(safetycheck, "load_result", lambda *args, **kwargs: None)
    config = _config(tmp_path)
    window = MainWindow(config)
    try:
        window.open_game("pgr")
        window.game.set_safety_result(stored)
        dismissed: list[str] = []
        monkeypatch.setattr(
            safetycheck, "dismiss", lambda profile, **kwargs: dismissed.append(profile)
        )
        window.game.safety_dismiss.click()
        assert dismissed == ["pgr"]
        assert window.game.safety_banner.isHidden()
    finally:
        window.controller._workers.clear()
        window.close()
        window.deleteLater()
        qapp.processEvents()


def test_incomplete_states_are_visible_and_keep_the_warning(view, qapp):
    from nadare.gui.safety_check import SafetyResultDialog
    view.set_safety_result(_result('compromised'))
    result = SafetyResult(level='incomplete', truncated=True)
    view.set_safety_result(result)
    assert 'Incomplete' in view.safety_status.text()
    assert not view.safety_banner.isHidden()
    view.show()
    qapp.processEvents()
    view.grab().save(str(_shots() / 'safety-incomplete-warning.png'))
    safetycheck.save_result('pgr', _result('compromised'))
    safetycheck.save_result('pgr', result)
    stored = safetycheck.load_result('pgr')
    assert stored.compromise_warning
    view.set_safety_result(None)
    view.set_safety_result(stored)
    assert not view.safety_banner.isHidden()
    system = SystemCheckDialog(None, {"profiles": {}})
    try:
        assert 'Incomplete' in system.safety_label.text()
        assert 'previous compromise' in system.safety_label.text()
        system.show()
        qapp.processEvents()
        system.grab().save(str(_shots() / 'safety-incomplete-system.png'))
    finally:
        system.close()
    dialog = SafetyResultDialog(None, result, 'pgr')
    try:
        assert dialog.heading.text() == 'Incomplete'
        assert 'skipped' in dialog.summary.text().lower()
        dialog.show()
        qapp.processEvents()
        dialog.grab().save(str(_shots() / 'safety-incomplete-dialog.png'))
    finally:
        dialog.close()


# ---- the result window ---------------------------------------------------------------------
def _no_list() -> SafetyResult:
    return SafetyResult(level="no_list", checked_at="2026-11-19T10:00:00+00:00")


def test_the_no_list_window_shows_one_short_message(qapp):
    """No repeated sentence, no heading saying the same thing twice (b28)."""
    dialog = SafetyResultDialog(None, _no_list(), "Arknights")
    try:
        text = dialog.summary.text()
        assert "No indicator list yet" not in text
        assert text.count("known bad files") == 1
        assert "not a problem with the game" in text
        assert "nothing" in text.lower()
        assert len(text) < 220
        assert dialog.heading.isHidden()  # the one message stands alone
    finally:
        dialog.close()


def test_the_no_list_window_fits_its_message(qapp):
    """A small window around a small answer: no 620x350 box with empty space (b28)."""
    dialog = SafetyResultDialog(None, _no_list(), "Arknights")
    try:
        dialog.show()
        qapp.processEvents()
        assert dialog.height() <= 200
        layout = dialog.layout()
        assert abs(dialog.height() - layout.heightForWidth(dialog.width())) <= 2
        dialog.grab().save(str(_shots() / "safety-no-list-window.png"))
    finally:
        dialog.close()


def test_the_no_list_window_says_it_was_never_checked(qapp):
    dialog = SafetyResultDialog(None, _no_list(), "Arknights")
    try:
        assert dialog.when.text().startswith("Tried")
        assert "Arknights" in dialog.when.text()
    finally:
        dialog.close()


def test_the_time_line_is_local_and_needs_no_iso_reading(qapp):
    old = os.environ.get("TZ")
    os.environ["TZ"] = "Asia/Tokyo"
    time.tzset()
    try:
        dialog = SafetyResultDialog(None, _result("exposed"), "Arknights")
        try:
            when = dialog.when.text()
        finally:
            dialog.close()
    finally:
        if old is None:
            os.environ.pop("TZ", None)
        else:
            os.environ["TZ"] = old
        time.tzset()
    assert "2026-11-19T10:00:00" not in when
    assert "+00:00" not in when
    assert "19:00" in when  # 10:00 UTC read in the reader's own time zone


def test_a_found_indicator_still_lists_ids_and_counts(qapp):
    dialog = SafetyResultDialog(None, _result("compromised"), "Arknights")
    try:
        assert dialog.heading.text() == "Compromised"
        assert "fake-marker" in dialog.summary.text()
        assert dialog.details.text() == "fake-marker: 2 files"
        assert not dialog.details.isHidden()
        assert dialog.when.text().startswith("Checked")
    finally:
        dialog.close()


def test_the_result_window_names_the_game_by_its_title(tmp_path, qapp, monkeypatch):
    from types import SimpleNamespace

    monkeypatch.setattr(safetycheck, "save_result", lambda *args, **kwargs: None)
    monkeypatch.setattr(safetycheck, "load_result", lambda *args, **kwargs: None)
    window = MainWindow(_config(tmp_path))
    try:
        window.open_game("pgr")
        generation = object()
        window._safety_task = SimpleNamespace(profile="pgr", generation=generation)
        MainWindow._on_safety_done(window, "pgr", _result("clean"), generation)
        dialog = window._safety_dialog
        assert dialog is not None
        assert "PGR" in dialog.when.text()
        assert "pgr" not in dialog.when.text()  # the id never reaches the window
    finally:
        if window._safety_dialog is not None:
            window._safety_dialog.close()
            window._safety_dialog = None
        window._safety_task = None
        window.controller._workers.clear()
        window.close()
        window.deleteLater()
        qapp.processEvents()


def test_a_no_list_window_leaves_the_page_clean_after_it_closes(tmp_path, qapp, monkeypatch):
    """The sentence must not stay under the buttons, also not while playing (b28)."""
    from types import SimpleNamespace

    monkeypatch.setattr(safetycheck, "save_result", lambda *args, **kwargs: None)
    stored = _result("no_list")
    monkeypatch.setattr(safetycheck, "load_result", lambda *args, **kwargs: stored)
    window = MainWindow(_config(tmp_path))
    try:
        window.open_game("pgr")  # a no-list result stored by an older run
        assert window.game.safety_status.isHidden()
        generation = object()
        window._safety_task = SimpleNamespace(profile="pgr", generation=generation)
        MainWindow._on_safety_done(window, "pgr", _result("no_list"), generation)
        assert window._safety_dialog is not None
        window._safety_dialog.close()
        qapp.processEvents()
        assert window.game.safety_status.isHidden()
        assert window.game.safety_banner.isHidden()
    finally:
        dialog = window._safety_dialog
        window._safety_dialog = None
        if dialog is not None:
            try:
                dialog.close()
            except RuntimeError:  # WA_DeleteOnClose already removed it
                pass
        window._safety_task = None
        window.controller._workers.clear()
        window.close()
        window.deleteLater()
        qapp.processEvents()


def test_reopening_running_profile_keeps_button_disabled(tmp_path, qapp):
    from types import SimpleNamespace
    window = MainWindow(_config(tmp_path))
    try:
        window._safety_task = SimpleNamespace(profile='pgr')
        window.open_game('pgr')
        assert not window.game.safety.isEnabled()
    finally:
        window._safety_task = None
        window.controller._workers.clear()
        window.close()
        window.deleteLater()
        qapp.processEvents()


def test_obsolete_same_profile_completion_cannot_save_or_update(tmp_path, qapp, monkeypatch):
    from types import SimpleNamespace
    window = MainWindow(_config(tmp_path))
    saved = []
    monkeypatch.setattr(safetycheck, 'save_result', lambda *args: saved.append(args))
    try:
        window.open_game('pgr')
        current = SimpleNamespace(profile='pgr', generation=object())
        window._safety_task = current
        window.game.set_safety_running(True)
        MainWindow._on_safety_done(window, 'pgr', _result('clean'), object())
        assert window._safety_task is current
        assert not saved
        assert not window.game.safety.isEnabled()
    finally:
        window._safety_task = None
        window.controller._workers.clear()
        window.close()
        window.deleteLater()
        qapp.processEvents()
