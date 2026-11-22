# SPDX-License-Identifier: GPL-3.0-or-later
# Copyright (C) 2026 Nadare contributors
"""The Disk space card in Settings: category totals with folded entry details.

Measurements and clearing run in the background. Every deletion uses storagecard's
existing checks; recovered settings never start a measurement.
"""

from __future__ import annotations

import copy
import logging
import threading
from collections.abc import Sequence
from datetime import datetime
from pathlib import Path

from PyQt6.QtCore import QObject, QUrl, pyqtSignal
from PyQt6.QtGui import QDesktopServices
from PyQt6.QtWidgets import (
    QCheckBox,
    QDialog,
    QHBoxLayout,
    QLabel,
    QMessageBox,
    QPushButton,
    QVBoxLayout,
    QWidget,
)

from ..core import diskspace, libraries, profilefolders, runners, steamsetup, storagecard, umu
from ..core.config import Config
from . import i18n
from .library import count_text

log = logging.getLogger(__name__)

#: Shown when the settings file was recovered: its list of games cannot be trusted.
RECOVERED_TEXT = "Not checked: Nadare is using a recovered settings file."

SHADER_NOTE = (
    "Safe to delete. Games rebuild them, and may stutter for a while after."
)

FOLDERS_NOTE = (
    "These folders in Nadare's data belonged to games that are no longer in Nadare. They may "
    "hold saves, settings or a Wine prefix with game files. Open one to look first. Only the "
    "folders you tick are deleted, and this cannot be undone."
)


class _Signals(QObject):
    # Sizes are bytes: a Qt int is 32-bit, so they travel as objects.
    measured = pyqtSignal(
        int, object, object, object
    )  # generation, [(path, size)], [Folder], Report
    runtimes = pyqtSignal(int, object)  # generation, [(path, size)]
    deleted = pyqtSignal(object, object, object)  # deleted paths, freed bytes, kept paths
    cleared = pyqtSignal(object)  # storagecard.ClearResult


def _size_text(size: int | None) -> str:
    return diskspace.human_size(size) if size is not None else "size unavailable"


def _snapshot(config: Config) -> dict:
    with config.locked():
        return copy.deepcopy(dict(config.profiles))


def _row(label: QLabel, button: QPushButton) -> QWidget:
    row = QWidget()
    layout = QHBoxLayout(row)
    layout.setContentsMargins(0, 0, 0, 0)
    layout.addWidget(label, 1)
    layout.addWidget(button)
    return row


class ClearRow(QWidget):
    """One entry with its size and Clear button."""

    def __init__(self, item: object, text: str, *, parent: QWidget | None = None) -> None:
        super().__init__(parent)
        self.item = item
        self.label = QLabel(text)
        self.label.setWordWrap(True)
        layout = QHBoxLayout(self)
        layout.setContentsMargins(0, 0, 0, 0)
        layout.setSpacing(8)
        layout.addWidget(self.label, 1)
        self.button = QPushButton(i18n.tr("Clear…"))
        self.button.setObjectName("GhostButton")
        layout.addWidget(self.button)


class CategoryRow(ClearRow):
    """A single summary, with the individual entries folded underneath."""

    def __init__(self, name: str, parent: QWidget) -> None:
        super().__init__(None, i18n.tr(name), parent=parent)  # i18n: dynamic
        self.name = name
        self.entries: list = []
        self.rows: list[ClearRow] = []
        self.total = 0
        self.toggle = QPushButton(i18n.tr("▸ Details"))
        self.toggle.setFlat(True)
        self.toggle.setStyleSheet(
            "QPushButton { background: transparent; border: none; padding: 4px; }"
        )
        self.toggle.setCheckable(True)
        self.layout().insertWidget(1, self.toggle)
        self.details = QWidget(parent)
        box = QVBoxLayout(self.details)
        box.setContentsMargins(16, 0, 0, 0)
        box.setSpacing(6)
        self.details.hide()
        self.toggle.toggled.connect(self._toggle)
        self.reset(i18n.tr("Not checked yet"))

    def _toggle(self, opened: bool) -> None:
        self.details.setVisible(opened)
        self.toggle.setText(i18n.tr("▾ Details") if opened else i18n.tr("▸ Details"))

    def reset(self, text: str) -> None:
        self.toggle.setChecked(False)
        self.details.hide()
        self.toggle.setEnabled(False)
        self.button.setEnabled(False)
        self.entries = []
        self.rows = []
        self.total = 0
        self.label.setText(f"{i18n.tr(self.name)} · {text}")  # i18n: dynamic
        DiskSpacePanel._clear_rows(self.details.layout())


class DiskSpacePanel(QWidget):
    """Measures and clears what Nadare keeps on disk, and opens their managers."""

    #: The Manage runners dialog: Settings opens it (it knows the form's unsaved runner).
    runners_requested = pyqtSignal()

    def __init__(self, parent: QWidget | None = None) -> None:
        super().__init__(parent)
        self._config: Config | None = None
        self._generation = 0
        self._stale = True
        self._clearing = False
        #: ``None`` until measured; then the unused runners ``[(path, size)]``.
        self.runners_found: list[tuple[Path, int]] | None = None
        #: ``None`` until measured; then the folders of removed games.
        self.folders_found: list[profilefolders.Folder] | None = None
        #: ``None`` until measured; then every category the card can clear.
        self.report: storagecard.Report | None = None
        #: The Clear buttons' rows, in the order they are shown.
        self.temp_rows: list[ClearRow] = []
        self.backup_rows: list[ClearRow] = []
        self.shader_rows: list[ClearRow] = []
        self.web_rows: list[ClearRow] = []
        #: Set once a Clear button really removed something (the tests wait on it).
        self.cleared_once = False
        self._signals = _Signals(self)
        self._signals.measured.connect(self._on_measured)
        self._signals.runtimes.connect(self._on_runtimes)
        self.runtime_folders = []
        self._signals.cleared.connect(self._on_cleared)

        layout = QVBoxLayout(self)
        layout.setContentsMargins(0, 0, 0, 0)
        layout.setSpacing(10)
        self.runners_label = QLabel("Unused runners: not checked yet")
        self.runners_label.setWordWrap(True)
        self.manage_button = QPushButton("Manage runners…")
        self.manage_button.setObjectName("GhostButton")
        self.manage_button.setToolTip(
            "See the Proton runners Nadare downloaded and delete the ones no game uses."
        )
        self.manage_button.clicked.connect(self.runners_requested)
        layout.addWidget(_row(self.runners_label, self.manage_button))
        self.runners_note = QLabel(i18n.tr(
            "No game uses them; the recommended build is not counted."
        ))
        self.runners_note.setObjectName("Muted")
        self.runners_note.setWordWrap(True)
        layout.addWidget(self.runners_note)

        self.folders_label = QLabel("Folders of removed games: not checked yet")
        self.folders_label.setWordWrap(True)
        self.review_button = QPushButton("Review…")
        self.review_button.setObjectName("GhostButton")
        self.review_button.setToolTip(
            "List the folders of games no longer in Nadare and choose which to delete."
        )
        self.review_button.setEnabled(False)
        self.review_button.clicked.connect(self._review)
        layout.addWidget(_row(self.folders_label, self.review_button))
        self.folders_note = QLabel(i18n.tr(
            "They may hold saves. Review the folders before deleting."
        ))
        self.folders_note.setObjectName("Muted")
        self.folders_note.setWordWrap(True)
        layout.addWidget(self.folders_note)

        self.runtime_label = QLabel("umu runtimes: not checked yet")
        self.runtime_label.setWordWrap(True)
        self.runtime_button = QPushButton("Review…")
        self.runtime_button.setObjectName("GhostButton")
        self.runtime_button.setEnabled(False)
        self.runtime_button.clicked.connect(self._review_runtimes)
        layout.addWidget(_row(self.runtime_label, self.runtime_button))
        self.runtime_note = QLabel(i18n.tr("Shared runtimes may be used by other apps."))
        self.runtime_note.setObjectName("Muted")
        self.runtime_note.setWordWrap(True)
        layout.addWidget(self.runtime_note)

        self.installers_label = QLabel("Downloaded installers: not checked yet")
        self.installers_label.setWordWrap(True)
        self.installers_button = QPushButton("Clear…")
        self.installers_button.setObjectName("GhostButton")
        self.installers_button.setToolTip(
            "Delete Windows installers Nadare downloaded (WebView2, Visual C++). A game "
            "downloads one again when it needs it."
        )
        self.installers_button.setEnabled(False)
        self.installers_button.clicked.connect(self._clear_installers)
        layout.addWidget(_row(self.installers_label, self.installers_button))
        self.installers_note = QLabel(i18n.tr("Nadare downloads one again when a game needs it."))
        self.installers_note.setObjectName("Muted")
        self.installers_note.setWordWrap(True)
        layout.addWidget(self.installers_note)

        self.categories = {}
        for key, name in (
            ("temp", "Game temporary files"),
            ("web", "Launcher web caches"),
            ("shader", "Shader caches"),
            ("backup", "Steam prefix backups"),
        ):
            category = CategoryRow(name, self)
            self.categories[key] = category
            layout.addWidget(category)
            category.button.clicked.connect(
                lambda _checked=False, k=key: self._clear_category(k)
            )
            if key == "shader":
                self.shader_note = QLabel(i18n.tr(SHADER_NOTE))
                self.shader_note.setObjectName("Muted")
                self.shader_note.setWordWrap(True)
                layout.addWidget(self.shader_note)
                self.shader_warning = QLabel("")
                self.shader_warning.setWordWrap(True)
                self.shader_warning.hide()
                layout.addWidget(self.shader_warning)
            layout.addWidget(category.details)
        self.temp_box = self.categories["temp"].details.layout()
        self.web_box = self.categories["web"].details.layout()
        self.shader_box = self.categories["shader"].details.layout()
        self.backup_box = self.categories["backup"].details.layout()

        self.status = QLabel("")
        self.status.setObjectName("Muted")
        self.status.setWordWrap(True)
        layout.addWidget(self.status)

    @staticmethod
    def _clear_rows(box: QVBoxLayout) -> None:
        while box.count():
            item = box.takeAt(0)
            widget = item.widget()
            if widget is not None:
                widget.deleteLater()

    def _on_runtimes(self, generation: int, folders: object) -> None:
        if generation != self._generation:
            return
        self.runtime_folders = list(folders) if isinstance(folders, list) else []
        total = sum(size for _root, size in self.runtime_folders)
        self.runtime_label.setText(f"umu runtimes · {diskspace.human_size(total)}")
        self.runtime_button.setEnabled(bool(self.runtime_folders))

    def _review_runtimes(self) -> None:
        if self._config is None or not self.runtime_folders:
            return
        dialog = RuntimeFoldersDialog(self._config, self.runtime_folders, self)
        dialog.exec()
        if dialog.deleted_any:
            self.refresh()

    # ---- measuring ----------------------------------------------------------------------------
    def load(self, config: Config) -> None:
        """Remember ``config``; the numbers are measured at the next :meth:`refresh`."""
        self._config = config
        self._stale = True

    def mark_stale(self) -> None:
        self._stale = True

    def refresh_if_stale(self) -> None:
        if self._stale:
            self.refresh()

    def refresh(self) -> None:
        """Measure again in the background (nothing with a recovered settings file)."""
        config = self._config
        if config is None:
            return
        self._stale = False
        self._generation += 1
        self.runners_found = None
        self.folders_found = None
        self.report = None
        self.temp_rows = []
        self.backup_rows = []
        self.shader_rows = []
        self.web_rows = []
        for category in self.categories.values():
            category.reset(i18n.tr("Checking…"))
        self.shader_warning.setText("")
        self.shader_warning.setVisible(False)
        self.review_button.setEnabled(False)
        self.runtime_button.setEnabled(False)
        self.runtime_folders = []
        self.installers_button.setEnabled(False)
        if config.notice:
            self.runtime_label.setText(RECOVERED_TEXT)
            self.runners_label.setText(RECOVERED_TEXT)
            self.folders_label.setText(RECOVERED_TEXT)
            self.installers_label.setText(RECOVERED_TEXT)
            for category in self.categories.values():
                category.reset(i18n.tr(RECOVERED_TEXT))
            return
        self.runtime_label.setText("umu runtimes: checking…")
        self.runners_label.setText("Unused runners: checking…")
        self.folders_label.setText("Folders of removed games: checking…")
        self.installers_label.setText("Downloaded installers: checking…")
        profiles = _snapshot(config)
        roots = libraries.library_roots(config)
        threading.Thread(
            target=self._measure,
            args=(self._generation, profiles, roots),
            name="disk-space-card",
            daemon=True,
        ).start()

    def _measure(self, generation: int, profiles: dict, roots: list[Path]) -> None:
        try:
            pairs: object = [
                (path, runners.runner_size(path)) for path in runners.unused_old_runners(profiles)
            ]
        except Exception:  # noqa: BLE001 - a Settings hint must never break Settings
            log.debug("could not measure the unused runners", exc_info=True)
            pairs = None
        try:
            folders: object = profilefolders.find(profiles, roots)
        except Exception:  # noqa: BLE001
            log.debug("could not list the folders of removed games", exc_info=True)
            folders = None
        try:
            runtime_folders = umu.runtime_storage()
        except Exception:  # noqa: BLE001 - Settings must stay usable
            runtime_folders = []
        try:
            report: object = storagecard.scan(profiles)
        except Exception:  # noqa: BLE001
            log.debug("could not measure the disk space categories", exc_info=True)
            report = None
        try:
            self._signals.runtimes.emit(generation, runtime_folders)
            self._signals.measured.emit(generation, pairs, folders, report)
        except RuntimeError:
            pass  # the page closed meanwhile

    def _on_measured(
        self, generation: int, pairs: object, folders: object, report: object
    ) -> None:
        if generation != self._generation:
            return  # a newer measurement started
        if isinstance(pairs, list):
            self.runners_found = pairs
            if pairs:
                total = sum(size for _path, size in pairs)
                self.runners_label.setText(
                    f"Unused runners · {diskspace.human_size(total)}"
                )
            else:
                self.runners_label.setText("Unused runners · 0 B")
        else:
            self.runners_found = []
            self.runners_label.setText("Unused runners: could not check (see the log).")
        if isinstance(folders, list):
            self.folders_found = folders
            if folders:
                total = sum(folder.size for folder in folders)
                self.folders_label.setText(
                    f"Folders of removed games · {diskspace.human_size(total)}"
                )
            else:
                self.folders_label.setText("Folders of removed games · 0 B")
            self.review_button.setEnabled(bool(folders))
        else:
            self.folders_found = []
            self.folders_label.setText(
                "Folders of removed games: could not check (see the log)."
            )
        self.report = report if isinstance(report, storagecard.Report) else None
        self._fill_report(self.report)

    def _fill_report(self, report: storagecard.Report | None) -> None:
        for category in self.categories.values():
            category.reset(i18n.tr("Checking…"))
        self.temp_rows = []
        self.backup_rows = []
        self.shader_rows = []
        self.web_rows = []
        self.shader_warning.setText("")
        self.shader_warning.setVisible(False)
        if report is None:
            self.installers_label.setText("Downloaded installers: could not check (see the log).")
            self.installers_button.setEnabled(False)
            for category in self.categories.values():
                category.reset(i18n.tr("Could not check (see the log)."))
            return

        installers = report.installers
        self.installers_label.setText(
            f"Downloaded installers · {diskspace.human_size(installers.size)}"
        )
        self.installers_button.setEnabled(bool(installers.files))
        for key, entries, clear in (
            ("temp", report.temps, self._clear_temp),
            ("web", report.webcaches, self._clear_webcache),
            ("shader", report.shaders, self._clear_shader),
            ("backup", report.backups, self._clear_backup),
        ):
            category = self.categories[key]
            category_name = i18n.tr(category.name)  # i18n: dynamic
            category.entries = [entry for entry in entries
                                if entry.size is not None and entry.size >= 1024 * 1024]
            category.total = self._category_total(key, category.entries)
            games = {self._game_key(key, entry) for entry in category.entries}
            games.discard(None)  # Mesa is shared, rather than a game.
            if category.entries:
                category.label.setText(
                    f"{category_name} · {count_text(len(games), 'game')} · "
                    f"{diskspace.human_size(category.total)}"
                )
            else:
                category.label.setText(f"{category_name} · {i18n.tr('Nothing to clear')}")
            category.button.setEnabled(bool(category.entries))
            category.toggle.setEnabled(bool(category.entries))
            for entry in category.entries:
                row = ClearRow(entry, self._entry_text(key, entry))
                row.button.clicked.connect(lambda _checked=False, item=entry, fn=clear: fn(item))
                row.setEnabled(not getattr(entry, "running", False))
                category.details.layout().addWidget(row)
                category.rows.append(row)
        self.temp_rows = self.categories["temp"].rows
        self.web_rows = self.categories["web"].rows
        self.shader_rows = self.categories["shader"].rows
        self.backup_rows = self.categories["backup"].rows
        if report.shader_warning:
            self.shader_warning.setText(report.shader_warning)
            self.shader_warning.show()

        if self._clearing:
            self._set_busy(True)

    @staticmethod
    def _game_key(key: str, entry: object) -> str | None:
        return entry.app_id if key == "backup" else entry.profile

    @staticmethod
    def _category_total(key: str, entries: list) -> int:
        if key != "shader":
            return sum(entry.size for entry in entries)
        sizes = {}
        for index, entry in enumerate(entries):
            if len(entry.folder_sizes) == len(entry.folders):
                for path, size in zip(entry.folders, entry.folder_sizes, strict=True):
                    sizes[path] = size or 0
            else:
                sizes[index] = entry.size
        return sum(sizes.values())

    def _backup_name(self, entry: storagecard.Backup) -> str:
        name = next((game.game for game in steamsetup.GAMES if game.app_id == entry.app_id),
                    entry.path.name)
        if self._config is not None:
            for profile in _snapshot(self._config).values():
                if str(profile.get("steam_appid", "")) == entry.app_id:
                    name = str(profile.get("title") or name)
        try:
            stamp = entry.path.stat().st_ctime
            date = datetime.fromtimestamp(stamp).strftime("%Y-%m-%d %H:%M")
            return f"{name} · {date}"
        except (OSError, ValueError, OverflowError):
            return name

    def _entry_text(self, key: str, entry: object) -> str:
        name = (self._backup_name(entry) if key == "backup" else
                entry.label if key == "shader" else entry.title)
        text = f"{name} · {_size_text(entry.size)}"
        if key == "backup" and entry.newest:
            text += i18n.tr(" (newest backup)")
        if getattr(entry, "running", False):
            text += i18n.tr(" (the game is running)")
        if getattr(entry, "warning", ""):
            text += f"\n{entry.warning}"
        return text

    def _clear_category(self, key: str) -> None:
        self._confirm_entries(key, self.categories[key].entries)

    def _confirm_entries(self, key: str, entries: list) -> None:
        if self._clearing or not entries:
            return
        names = "\n".join(self._entry_text(key, entry) for entry in entries)
        text = i18n.tr("Clear these items?") + f"\n\n{names}\n\n"
        text += i18n.tr("Total") + f": {diskspace.human_size(self._category_total(key, entries))}"
        newest = key == "backup" and any(entry.newest for entry in entries)
        include_newest = False
        if newest:
            dialog = QMessageBox(self)
            dialog.setWindowTitle(i18n.tr("Clear Steam prefix backups"))
            dialog.setStandardButtons(
                QMessageBox.StandardButton.Yes | QMessageBox.StandardButton.No
            )
            dialog.setDefaultButton(QMessageBox.StandardButton.No)
            pick = QCheckBox(i18n.tr("Delete the newest backup too"), dialog)
            dialog.setCheckBox(pick)

            def update_confirmation(include: bool = False) -> None:
                chosen = [entry for entry in entries if not entry.newest or include]
                total = self._category_total(key, chosen)
                dialog.setText(
                    i18n.tr("Clear these items?") + f"\n\n{names}\n\n"
                    + i18n.tr("Total") + f": {diskspace.human_size(total)}\n\n"
                    + i18n.tr("The newest backup of each game is kept unless you tick the box. "
                              "This cannot be undone.")
                )

            pick.toggled.connect(update_confirmation)
            update_confirmation()
            accepted = dialog.exec() == QMessageBox.StandardButton.Yes
            include_newest = pick.isChecked()
            dialog.deleteLater()
        else:
            if key == "backup":
                text += "\n\n" + i18n.tr("This cannot be undone.")
            name = i18n.tr(self.categories[key].name)  # i18n: dynamic
            accepted = self._ask(i18n.tr("Clear") + " " + name, text)
        if not accepted:
            return
        selected = [entry for entry in entries
                    if key != "backup" or not entry.newest or include_newest]
        if not selected:
            return
        clear = {"temp": storagecard.clear_temp, "web": storagecard.clear_webcache,
                 "shader": storagecard.clear_shader, "backup": storagecard.clear_backup}[key]

        def work():
            combined = storagecard.ClearResult()
            reasons = []
            for entry in selected:
                try:
                    result = clear(entry)
                except Exception as exc:  # noqa: BLE001 - continue the other entries
                    log.warning("could not clear disk space: %s", exc)
                    result = storagecard.ClearResult(reason=str(exc))
                combined.removed.extend(result.removed)
                combined.kept.extend(result.kept)
                combined.freed += result.freed
                if result.reason and result.reason not in reasons:
                    reasons.append(result.reason)
            combined.reason = "; ".join(reasons)
            return combined

        self._start_clear(work)

    # ---- clearing -----------------------------------------------------------------------------
    def _ask(self, title: str, text: str) -> bool:
        answer = QMessageBox.question(
            self,
            title,
            text,
            QMessageBox.StandardButton.Yes | QMessageBox.StandardButton.No,
            QMessageBox.StandardButton.No,  # a destructive action never defaults to Yes
        )
        return answer == QMessageBox.StandardButton.Yes

    def _set_busy(self, busy: bool) -> None:
        self.installers_button.setEnabled(
            not busy and self.report is not None and bool(self.report.installers.files)
        )
        for row in (
            *self.temp_rows,
            *self.backup_rows,
            *self.shader_rows,
            *self.web_rows,
        ):
            row.setEnabled(not busy and not getattr(row.item, "running", False))
        for category in self.categories.values():
            category.button.setEnabled(not busy and bool(category.entries))

    def _start_clear(self, work) -> None:
        if self._clearing:
            return
        self._clearing = True
        self._set_busy(True)
        threading.Thread(
            target=self._run_clear,
            args=(work,),
            name="disk-space-clear",
            daemon=True,
        ).start()

    def _run_clear(self, work) -> None:
        try:
            result: object = work()
        except Exception as exc:  # noqa: BLE001 - a Clear button must never crash Settings
            log.warning("could not clear disk space: %s", exc)
            result = storagecard.ClearResult(reason=str(exc))
        try:
            self._signals.cleared.emit(result)
        except RuntimeError:
            pass  # the page closed meanwhile

    def _on_cleared(self, result: object) -> None:
        if not isinstance(result, storagecard.ClearResult):
            return
        self._clearing = False
        if result.removed:
            self.cleared_once = True
            text = (
                f"Deleted {count_text(len(result.removed), 'item')}, "
                f"freed {diskspace.human_size(result.freed)}."
            )
        else:
            text = "Nothing was deleted."
        if result.kept:
            text += (
                f" {count_text(len(result.kept), 'item')} left alone: "
                f"{result.reason or 'see the log'}."
            )
        elif result.reason:
            text += f" {result.reason}."
        self.status.setText(text)
        self.refresh()

    def _clear_installers(self) -> None:
        report = self.report
        if report is None or not report.installers.files:
            return
        files = report.installers.files
        if not self._ask(
            "Clear downloaded installers",
            f"Delete {count_text(len(files), 'downloaded installer')} "
            f"({diskspace.human_size(report.installers.size)})? "
            "Nadare downloads one again when a game needs it.",
        ):
            return
        self._start_clear(lambda: storagecard.clear_installers(files))

    def _clear_temp(self, entry: storagecard.PrefixTemp) -> None:
        if not self._ask(
            f"Clear {entry.title} temporary files",
            f"Delete {entry.title}'s temporary files "
            f"({diskspace.human_size(entry.size)})? The game recreates them when it runs.",
        ):
            return
        self._start_clear(lambda: storagecard.clear_temp(entry))

    def _clear_backup(self, entry: storagecard.Backup) -> None:
        self._confirm_entries("backup", [entry])

    def _clear_shader(self, entry: storagecard.ShaderCache) -> None:
        if not self._ask(
            "Clear a shader cache",
            f"Clear the {entry.label} shader cache "
            f"({_size_text(entry.size)})? Games rebuild it when they run.",
        ):
            return
        self._start_clear(lambda: storagecard.clear_shader(entry))

    def _clear_webcache(self, entry: storagecard.WebCache) -> None:
        if not self._ask(
            "Clear a launcher web cache",
            f"Clear {entry.title}'s launcher web cache "
            f"({diskspace.human_size(entry.size)})? Your sign-in is kept; the launcher "
            "downloads the cache again when it needs it.",
        ):
            return
        self._start_clear(lambda: storagecard.clear_webcache(entry))

    # ---- review -------------------------------------------------------------------------------
    def _review(self) -> None:
        if self._config is None or not self.folders_found:
            return
        dialog = ProfileFoldersDialog(self._config, list(self.folders_found), self)
        dialog.exec()
        if dialog.deleted_any:
            self.refresh()


class ProfileFoldersDialog(QDialog):
    """Lists the folders of removed games; deletes only the ticked ones, after a Yes."""

    def __init__(
        self,
        config: Config,
        folders: Sequence[profilefolders.Folder],
        parent: QWidget | None = None,
    ) -> None:
        super().__init__(parent)
        self.setWindowTitle("Folders of removed games")
        self.setMinimumWidth(560)
        self._config = config
        self._folders = list(folders)
        self.deleted_any = False
        self.finished_deleting = False
        self._signals = _Signals(self)
        self._signals.deleted.connect(self._on_deleted)

        layout = QVBoxLayout(self)
        layout.setSpacing(12)
        self.note = QLabel(FOLDERS_NOTE)
        self.note.setObjectName("Subtitle")
        self.note.setWordWrap(True)
        layout.addWidget(self.note)

        self.boxes: list[QCheckBox] = []
        for folder in self._folders:
            box = QCheckBox(f"{folder.path.name}  ({diskspace.human_size(folder.size)})")
            box.setToolTip(str(folder.path))
            box.toggled.connect(self._update_delete)
            open_button = QPushButton("Open")
            open_button.setObjectName("GhostButton")
            open_button.setToolTip(f"Open {folder.path} in the file manager.")
            open_button.clicked.connect(
                lambda _checked=False, p=folder.path: QDesktopServices.openUrl(
                    QUrl.fromLocalFile(str(p))
                )
            )
            row = QHBoxLayout()
            row.addWidget(box, 1)
            row.addWidget(open_button)
            layout.addLayout(row)
            self.boxes.append(box)

        self.status = QLabel("")
        self.status.setObjectName("Muted")
        self.status.setWordWrap(True)
        layout.addWidget(self.status)

        footer = QHBoxLayout()
        self.delete_button = QPushButton("Delete ticked folders")
        self.delete_button.setObjectName("GhostButton")
        self.delete_button.setEnabled(False)
        self.delete_button.clicked.connect(self._delete_ticked)
        footer.addWidget(self.delete_button)
        footer.addStretch(1)
        close = QPushButton("Close")
        close.setObjectName("GhostButton")
        close.clicked.connect(self.accept)
        footer.addWidget(close)
        layout.addLayout(footer)

    def _ticked(self) -> list[profilefolders.Folder]:
        return [
            folder
            for folder, box in zip(self._folders, self.boxes, strict=True)
            if box.isChecked() and box.isEnabled()
        ]

    def _update_delete(self) -> None:
        self.delete_button.setEnabled(bool(self._ticked()))

    def _confirm(self, items: list[profilefolders.Folder]) -> bool:
        """The one modal before deleting; No is the default."""
        names = "\n".join(
            f"  • {folder.path.name} ({diskspace.human_size(folder.size)})" for folder in items
        )
        answer = QMessageBox.question(
            self,
            "Delete folders",
            f"Delete these folders of removed games?\n\n{names}\n\n"
            "Saves or settings inside them are lost. This cannot be undone.",
            QMessageBox.StandardButton.Yes | QMessageBox.StandardButton.No,
            QMessageBox.StandardButton.No,  # a destructive action never defaults to Yes
        )
        return answer == QMessageBox.StandardButton.Yes

    def _delete_ticked(self) -> None:
        items = self._ticked()
        if not items or not self._confirm(items):
            return
        # The games as they are NOW: one added since the list was made keeps its folder.
        profiles = _snapshot(self._config)
        roots = libraries.library_roots(self._config)
        self.delete_button.setEnabled(False)
        for box in self.boxes:
            box.setEnabled(False)
        self.status.setText("Deleting…")
        threading.Thread(
            target=self._delete,
            args=(items, profiles, roots),
            name="profile-folders-delete",
            daemon=True,
        ).start()

    def _delete(
        self, items: list[profilefolders.Folder], profiles: dict, roots: list[Path]
    ) -> None:
        deleted: list[Path] = []
        kept: list[Path] = []
        freed = 0
        for folder in items:
            if profilefolders.remove(folder.path, profiles, roots):
                deleted.append(folder.path)
                freed += folder.size
            else:
                kept.append(folder.path)
        try:
            self._signals.deleted.emit(deleted, freed, kept)
        except RuntimeError:
            pass  # the dialog closed meanwhile

    def _on_deleted(self, deleted: object, freed: int, kept: object) -> None:
        gone = {str(p) for p in deleted} if isinstance(deleted, list) else set()
        left = list(kept) if isinstance(kept, list) else []
        self.deleted_any = self.deleted_any or bool(gone)
        for folder, box in zip(self._folders, self.boxes, strict=True):
            if str(folder.path) in gone:
                box.setChecked(False)
                box.setText(f"{folder.path.name}  (deleted)")
            else:
                box.setEnabled(True)
        text = f"Deleted {count_text(len(gone), 'folder')}, freed {diskspace.human_size(freed)}."
        if left:
            text += (
                f" {count_text(len(left), 'folder')} kept: a game uses it now, or it could not "
                "be deleted (see the log)."
            )
        self.status.setText(text)
        self._update_delete()
        self.finished_deleting = True


__all__ = ["DiskSpacePanel", "ProfileFoldersDialog", "RuntimeFoldersDialog"]


class RuntimeFoldersDialog(ProfileFoldersDialog):
    """The existing unticked review and ask-first deletion flow, for runtime folders."""

    def __init__(self, config: Config, folders: list[tuple[Path, int]], parent=None) -> None:  # noqa: ANN001
        super().__init__(config, [profilefolders.Folder(*pair) for pair in folders], parent)
        self.setWindowTitle("umu runtimes")
        # The inherited explanatory label describes removed games; replace it here.
        note = self.note
        note.setText("Nadare can download its runtime again. Shared umu runtimes may be used "
                     "by other apps. Close all games before deleting. "
                     "Only ticked folders are deleted.")
        for folder, box in zip(self._folders, self.boxes, strict=True):
            name = ("Nadare's umu runtime" if folder.path == umu.runtime_base()
                    else "Shared umu runtime (other apps may use it)")
            box.setText(f"{name} ({diskspace.human_size(folder.size)})")

    def _confirm(self, items: list[profilefolders.Folder]) -> bool:
        names = "\n".join(str(item.path) for item in items)
        return QMessageBox.question(
            self, "Delete umu runtimes",
            "Delete these runtimes? Other apps may need the shared folder. "
            "The runtime will need to be downloaded again.\n\n" + names,
            QMessageBox.StandardButton.Yes | QMessageBox.StandardButton.No,
            QMessageBox.StandardButton.No,
        ) == QMessageBox.StandardButton.Yes

    def _delete(
        self, items: list[profilefolders.Folder], profiles: dict, roots: list[Path]
    ) -> None:
        deleted, kept, freed = [], [], 0
        for folder in items:
            if umu.remove_runtime(folder.path):
                deleted.append(folder.path)
                freed += folder.size
            else:
                kept.append(folder.path)
        try:
            self._signals.deleted.emit(deleted, freed, kept)
        except RuntimeError:
            pass
