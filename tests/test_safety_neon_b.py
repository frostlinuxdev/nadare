# SPDX-License-Identifier: GPL-3.0-or-later
"""Port of the adversarial "Neon B" safety-check tests (triage: REAL P1/P2 rows).

Each test pins one defect in the #806 prefix safety check: a game-controlled symlink
changing the scanned roots, an unbounded re-walk of a deep tree, a skipped default Wine
prefix, stale enumeration sizes, cancellation inside a rule, an unlocked results store, and
a safety check left running when the window closes.
"""

from __future__ import annotations

import os
import threading
import time
from pathlib import Path
from types import SimpleNamespace

import pytest

from nadare.core import paths, safetycheck
from nadare.core.config import Config
from nadare.core.safetycheck import Indicator, SafetyResult

try:
    from PyQt6.QtWidgets import QApplication
except Exception:  # pragma: no cover - optional GUI dependencies
    QApplication = None  # type: ignore[assignment]


def _parse_rule(
    *,
    indicator_id: str = "fake-indicator",
    level: str = "exposed",
    rule: dict | None = None,
) -> list[Indicator]:
    doc = {
        "version": 1,
        "serial": 1,
        "indicators": [
            {
                "id": indicator_id,
                "title": "Example indicator",
                "kinds": ["*"],
                "level": level,
                "rule": rule or {"type": "path_exists", "glob": "bad.example"},
            }
        ],
    }
    parsed, skipped = safetycheck.parse_indicators(doc)
    assert skipped == 0 and len(parsed) == 1
    return parsed


def _shots() -> Path:
    folder = Path(os.environ.get("NADARE_SAFETY_SHOTS", "/mnt/kikyou/work/b28/safety"))
    try:
        folder.mkdir(parents=True, exist_ok=True)
        probe = folder / ".write-probe"
        probe.write_text("")
        probe.unlink()
    except OSError:
        folder = Path(os.environ.get("TMPDIR", "/tmp")) / "nadare-safety-shots"
        folder.mkdir(parents=True, exist_ok=True)
    return folder


# ---- REAL P1: a game-controlled link must not pick the scanned roots ----------------------
def test_symlinked_install_dir_inside_prefix_cannot_read_outside(tmp_path):
    """A game inside Sandbox replaces its install_dir under the prefix with a link outside."""
    home = tmp_path / "home"
    prefix = home / "prefix"
    outside = tmp_path / "outside"
    (prefix / "drive_c" / "Game").mkdir(parents=True)
    outside.mkdir()
    (outside / "secret.log").write_text("marker")

    # Hostile game replaces prefix/drive_c/Game with a symlink to outside.
    (prefix / "drive_c" / "Game").rmdir()
    (prefix / "drive_c" / "Game").symlink_to(outside, target_is_directory=True)

    profile = {
        "backend": "direct-pgr",
        "prefix": str(prefix),
        "install_dir": str(prefix / "drive_c" / "Game"),
    }
    rules = _parse_rule(
        rule={"type": "file_contains", "glob": "secret.log", "text": "marker"},
    )
    result = safetycheck.check_profile("pgr", profile, rules)
    assert result.bytes_read == 0, f"read outside prefix through game-controlled symlink: {result}"
    assert result.level != safetycheck.EXPOSED, result


def test_symlinked_install_dir_to_ancestor_cannot_evict_prefix(tmp_path):
    """Pointing a nested install_dir at an ancestor used to drop the prefix from the roots."""
    ancestor = tmp_path / "userhome"
    prefix = ancestor / "prefix"
    (prefix / "drive_c" / "Game").mkdir(parents=True)
    (prefix / "bad.example").write_text("evidence")

    # Hostile game symlinks install_dir (inside prefix) to an ancestor of prefix.
    (prefix / "drive_c" / "Game").rmdir()
    (prefix / "drive_c" / "Game").symlink_to(ancestor, target_is_directory=True)

    profile = {
        "backend": "direct-pgr",
        "prefix": str(prefix),
        "install_dir": str(prefix / "drive_c" / "Game"),
    }
    rules = _parse_rule(
        level="compromised",
        rule={"type": "path_exists", "glob": "bad.example"},
    )
    result = safetycheck.check_profile("pgr", profile, rules)
    assert result.level == safetycheck.COMPROMISED, (
        f"prefix was evicted by symlinked install_dir and indicator was missed: {result}"
    )


# ---- REAL P2: a deep tree must not amplify descriptor walks --------------------------------
def test_deep_directory_tree_does_not_amplify_opens_quadratically(tmp_path, monkeypatch):
    """A deep directory chain is a crafted tree: bounded work, and coverage says so."""
    prefix = tmp_path / "prefix"
    prefix.mkdir()

    depth = 250
    # Short names: depth is what matters, and some filesystems (the CI runner's tmp) refuse
    # a path far over PATH_MAX even when each step is opened relative to its parent.
    dir_name = "d" * 8
    fd = os.open(prefix, os.O_RDONLY | os.O_DIRECTORY)
    try:
        for _ in range(depth):
            os.mkdir(dir_name, dir_fd=fd)
            nxt = os.open(dir_name, os.O_RDONLY | os.O_DIRECTORY | os.O_NOFOLLOW, dir_fd=fd)
            os.close(fd)
            fd = nxt
    finally:
        os.close(fd)

    open_calls = 0
    real_open = safetycheck.os.open

    def counted_open(*args, **kwargs):
        nonlocal open_calls
        open_calls += 1
        return real_open(*args, **kwargs)

    monkeypatch.setattr(safetycheck.os, "open", counted_open)
    result = safetycheck.check_profile("pgr", {"prefix": str(prefix)}, _parse_rule())
    assert result.level == safetycheck.INCOMPLETE and result.truncated
    assert result.files_visited <= depth
    # 250 empty dirs should take O(depth) opens (or stop at a depth cap), not the old
    # D*(D+1)/2 = 31,375+ re-walks from the root.
    assert open_calls <= depth * 4, (
        f"quadratic syscall amplification: {open_calls} os.open calls for {depth} dirs"
    )


# ---- REAL P1: a profile that never wrote its prefix still has one -------------------------
@pytest.mark.parametrize("prefix_key", ["default", "wineprefix", "extra"])
def test_default_wine_prefix_is_checked_when_prefix_key_is_empty(tmp_path, prefix_key):
    """An install_dir profile with an empty prefix uses its backend's prefix, not nothing."""
    install = tmp_path / "install"
    install.mkdir()
    (install / "game.exe").write_text("ok")

    # The alias cases point somewhere else than the default: ignoring the alias must not be
    # masked by both paths being the same folder.
    default_prefix = paths.profiles_dir() / "pgr" / "prefix"
    checked_prefix = default_prefix if prefix_key == "default" else tmp_path / "alias-prefix"
    checked_prefix.mkdir(parents=True)
    (checked_prefix / "agent.dll").write_text("malicious")

    profile = {
        "backend": "direct-pgr",
        "prefix": "",
        "install_dir": str(install),
    }
    rules = _parse_rule(
        level="compromised",
        rule={"type": "path_exists", "glob": "agent.dll"},
    )
    if prefix_key == "wineprefix":
        profile["wineprefix"] = str(checked_prefix)
    elif prefix_key == "extra":
        profile["extra"] = {"wineprefix": str(checked_prefix)}
    result = safetycheck.check_profile("pgr", profile, rules)
    assert result.level == safetycheck.COMPROMISED, (
        f"default Wine prefix was silently skipped and reported {result.level!r}: {result}"
    )


# ---- REAL P2: a file that grows after enumeration must not look clean ---------------------
@pytest.mark.parametrize("initial", [b"", b"x"])
def test_file_growing_between_scan_and_read_is_not_reported_clean(
    tmp_path, monkeypatch, initial
):
    """A log (even an empty one) that gains its marker before the read must be seen."""
    prefix = tmp_path / "prefix"
    prefix.mkdir()
    target = prefix / "example.log"
    target.write_bytes(initial)

    real_entries = safetycheck._entries

    def grow_after_scan(root, cancel, limit):
        found, cut = real_entries(root, cancel, limit)
        target.write_text("malicious-marker")
        return found, cut

    monkeypatch.setattr(safetycheck, "_entries", grow_after_scan)
    rules = _parse_rule(
        rule={"type": "file_contains", "glob": "example.log", "text": "malicious-marker"},
    )
    result = safetycheck.check_profile("pgr", {"prefix": str(prefix)}, rules)
    assert result.level != safetycheck.CLEAN, (
        f"file containing marker grew after scan yet check reported CLEAN: {result}"
    )


def test_file_growing_during_the_read_is_incomplete(tmp_path, monkeypatch):
    """A log that keeps growing while it is read leaves uncertain coverage, not a clean bill."""
    prefix = tmp_path / "prefix"
    prefix.mkdir()
    target = prefix / "example.log"
    target.write_bytes(b"x" * (1 << 20))

    real_read = safetycheck.os.read
    appended = False

    def append_after_read(fd, count):
        nonlocal appended
        chunk = real_read(fd, count)
        if not appended:
            appended = True
            with target.open("ab") as handle:
                handle.write(b"late-marker")
        return chunk

    monkeypatch.setattr(safetycheck.os, "read", append_after_read)
    rules = _parse_rule(
        rule={"type": "file_contains", "glob": "example.log", "text": "late-marker"},
    )
    result = safetycheck.check_profile("pgr", {"prefix": str(prefix)}, rules)
    assert result.level == safetycheck.INCOMPLETE and result.truncated, (
        f"file grew during the read and the check still claimed {result.level!r}: {result}"
    )


# ---- REAL P2: cancellation must reach inside a rule --------------------------------------
@pytest.mark.parametrize("file_count, file_size", [(20, 1024), (1, 3 * 1024 * 1024)])
def test_cancel_during_rule_reading_stops_reading_and_reports_cancelled(
    tmp_path, monkeypatch, file_count, file_size
):
    """Setting cancel during a content read must stop at once and return CANCELLED."""
    prefix = tmp_path / "prefix"
    prefix.mkdir()
    for i in range(file_count):
        (prefix / f"f{i:02d}.log").write_bytes(b"x" * file_size)

    cancel = threading.Event()
    reads = 0
    opened = 0
    real_read = safetycheck.os.read
    real_open = safetycheck.os.open

    def cancel_on_first_read(fd, count):
        nonlocal reads
        reads += 1
        cancel.set()
        return real_read(fd, count)

    def count_log_open(target, *args, **kwargs):
        nonlocal opened
        if str(target).endswith(".log"):
            opened += 1
        return real_open(target, *args, **kwargs)

    monkeypatch.setattr(safetycheck.os, "read", cancel_on_first_read)
    monkeypatch.setattr(safetycheck.os, "open", count_log_open)
    rules = _parse_rule(
        rule={"type": "file_contains", "glob": "*.log", "text": "never-present"},
    )
    result = safetycheck.check_profile("pgr", {"prefix": str(prefix)}, rules, cancel=cancel)
    assert result.level == safetycheck.CANCELLED and result.cancelled is True, (
        f"cancelled check during rule scan returned {result.level!r} after {reads} reads"
    )
    assert reads == 1, f"check kept reading {reads} files after cancel was set"
    assert opened == 1, f"check opened {opened} files after cancel was set"


# ---- REAL P2: the results store needs a transaction, not just an atomic write -------------
def test_concurrent_save_result_does_not_lose_compromised_verdict(tmp_path, monkeypatch):
    """An unlocked read-modify-write in save_result loses a concurrent profile's verdict."""
    store = tmp_path / "results.json"
    first_entered = threading.Event()
    second_entered = threading.Event()
    release_first = threading.Event()
    real_read_store = safetycheck._read_store
    lock = threading.Lock()
    calls = 0

    def staggered_read(path):
        nonlocal calls
        data = real_read_store(path)
        with lock:
            calls += 1
            call_num = calls
        if call_num == 1:
            first_entered.set()
            # Wait briefly for a second unlocked reader (if locked, times out cleanly and proceeds).
            release_first.wait(0.3)
        elif call_num == 2:
            second_entered.set()
        return data

    monkeypatch.setattr(safetycheck, "_read_store", staggered_read)

    errors: list[BaseException] = []

    def writer(profile: str, level: str) -> None:
        try:
            safetycheck.save_result(profile, SafetyResult(level=level), path=store)
        except BaseException as exc:  # noqa: BLE001
            errors.append(exc)

    t1 = threading.Thread(target=writer, args=("beta", safetycheck.CLEAN))
    t2 = threading.Thread(target=writer, args=("alpha", safetycheck.COMPROMISED))
    t1.start()
    try:
        assert first_entered.wait(5)
        t2.start()
        if second_entered.wait(0.25):
            # Both read the same stale snapshot; t2 saves COMPROMISED first, then t1
            # overwrites it with its older view.
            t2.join(5)
            release_first.set()
    finally:
        release_first.set()
        t1.join(5)
        t2.join(5)

    monkeypatch.setattr(safetycheck, "_read_store", real_read_store)
    assert not errors
    stored = safetycheck.load_all_results(path=store)
    assert "alpha" in stored and stored["alpha"].level == safetycheck.COMPROMISED, (
        f"concurrent save_result wiped COMPROMISED verdict for alpha: {stored}"
    )
    assert "beta" in stored


# ---- REAL P2: closing the window must stop the check and suppress its dialog --------------
@pytest.mark.skipif(QApplication is None, reason="PyQt6 is not importable")
@pytest.mark.parametrize("state", ["closing", "hidden"])
def test_a_late_safety_result_after_close_does_not_open_a_dialog(tmp_path, monkeypatch, state):
    """A completion already queued when the window goes away must not resurrect the UI."""
    from nadare.gui.main_window import MainWindow

    qapp = QApplication.instance() or QApplication([])
    prefix = tmp_path / "prefix"
    prefix.mkdir()
    config = Config.load(tmp_path / "config.json")
    config.add_profile("pgr", backend="direct-pgr", title="PGR", prefix=str(prefix))
    config.save()
    monkeypatch.setattr(safetycheck, "save_result", lambda *args, **kwargs: None)
    monkeypatch.setattr(safetycheck, "load_result", lambda *args, **kwargs: None)

    window = MainWindow(config)
    try:
        task = SimpleNamespace(profile="pgr", generation=object())
        window._safety_task = task
        window._current_profile = "pgr"
        window._closing = state == "closing"
        window._safety_hidden = state == "hidden"
        window._on_safety_done("pgr", SafetyResult(level=safetycheck.CLEAN), task.generation)
        qapp.processEvents()
        assert window._safety_task is None
        assert window._safety_dialog is None
    finally:
        window._closing = False
        window._safety_hidden = False
        dialog = getattr(window, "_safety_dialog", None)
        if dialog is not None:
            dialog.close()
        window.controller._workers.clear()
        window.close()
        window.deleteLater()
        qapp.processEvents()


@pytest.mark.skipif(QApplication is None, reason="PyQt6 is not importable")
@pytest.mark.parametrize("keep_game", [False, True])
def test_closing_window_cancels_safety_task_and_does_not_show_dialog_after_close(
    tmp_path, monkeypatch, keep_game
):
    """closeEvent must cancel _safety_task; a late result must not open a dialog."""
    from nadare.gui.main_window import MainWindow

    qapp = QApplication.instance() or QApplication([])
    prefix = tmp_path / "prefix"
    prefix.mkdir()
    config = Config.load(tmp_path / "config.json")
    config.add_profile("pgr", backend="direct-pgr", title="PGR", prefix=str(prefix))
    config.save()

    started = threading.Event()
    release = threading.Event()

    def slow_load(**_kwargs):
        started.set()
        release.wait(5)
        return _parse_rule()

    monkeypatch.setattr(safetycheck, "load_indicators", slow_load)
    monkeypatch.setattr(safetycheck, "save_result", lambda *args, **kwargs: None)

    window = MainWindow(config)
    try:
        window.open_game("pgr")
        window.game.safety.click()
        assert started.wait(5), "safety check task did not start"
        task = window._safety_task
        assert task is not None

        window.grab().save(str(_shots() / f"codexfix-before-close-{keep_game}.png"))

        # User closes Nadare while the safety check is still running.
        window.controller._workers.clear()
        if keep_game:
            monkeypatch.setattr(window.controller, "launching_profiles", lambda: ["pgr"])
            monkeypatch.setattr(window, "_notify_still_running", lambda: None)
        window.close()
        qapp.processEvents()
        cancelled_on_close = task._cancel.is_set()

        # Let the background task finish and drain Qt queued signals.
        release.set()
        deadline = time.monotonic() + 5.0
        while time.monotonic() < deadline and window._safety_task is not None:
            qapp.processEvents()
            time.sleep(0.005)
        qapp.processEvents()

        window.grab().save(str(_shots() / f"codexfix-after-close-{keep_game}.png"))
        dialog = window._safety_dialog
        dialog_shown = dialog is not None and not dialog.isHidden()
        assert cancelled_on_close and not dialog_shown, (
            f"closeEvent did not stop safety check: cancelled_on_close={cancelled_on_close}, "
            f"dialog_shown_after_close={dialog_shown}"
        )
    finally:
        release.set()
        dialog = getattr(window, "_safety_dialog", None)
        if dialog is not None:
            dialog.close()
        window.controller._workers.clear()
        window.close()
        window.deleteLater()
        qapp.processEvents()
