# SPDX-License-Identifier: GPL-3.0-or-later
# Copyright (C) 2026 Nadare contributors
"""Small actions of the Settings page: open folders and links, fetch art, runners, uninstall
Nadare.

Split out of :mod:`nadare.gui.settings`; the code is unchanged.
"""

from __future__ import annotations

import copy
from pathlib import Path

from PyQt6.QtCore import QFile, QThreadPool, QUrl
from PyQt6.QtGui import QDesktopServices
from PyQt6.QtWidgets import (
    QLineEdit,
    QMessageBox,
)

from ...core import app_uninstall, community
from ...core import runners as runners_core
from ..library import count_text
from ..workers import CallTask

#: Label used for the "no runner" choice.
SYSTEM_WINE = "System Wine"


def _runner_label(path: Path) -> str:
    """Picker label for an installed runner: tested builds are marked."""
    if runners_core.is_recommended(path):
        return f"{path.name} · recommended"
    if runners_core.runner_family(path):
        return f"{path.name} · untested"
    return path.name


def discover_runners(kind: str = "", *, include_default: bool = True) -> list[tuple[str, str]]:
    """Return ``(label, value)`` pairs for the runner picker.

    Value is a filesystem path for an installed Proton build. The empty value is
    the backend's default: the recommended (tested) DW-Proton for every Proton
    backend, including the generic Wine/Proton games. A custom Wine is chosen
    with the profile's ``wine_binary`` field, not from this picker.

    ``include_default=False`` is for a profile whose ``wine_binary`` is what will
    actually run: then the pinned DW-Proton is not an option at all.
    """
    entries: list[tuple[str, str]] = []
    proton_default = kind in runners_core.PROTON_DEFAULT_BACKENDS
    if proton_default and include_default:
        tag = runners_core.pinned_tag(runners_core.DEFAULT_FAMILY) or "DW-Proton"
        entries.append((f"Default · recommended ({tag})", ""))
    seen: set[str] = set()
    for path in runners_core.all_runners():
        key = str(path)
        if key in seen:
            continue
        seen.add(key)
        entries.append((_runner_label(path), key))
    if not proton_default:
        entries.append((SYSTEM_WINE, ""))
    return entries


def _is_runner_folder(value: str) -> bool:
    """Whether ``value`` is a Proton or Wine tree Nadare can start (a custom runner)."""
    return runners_core.looks_like_runner(value)


class ExtrasMixin:
    """Mixin for :class:`SettingsPage`; see the module text."""

    def _uninstall_nadare(self) -> None:
        plan = app_uninstall.preview()
        if plan is None:
            QMessageBox.information(
                self, "Uninstall Nadare",
                "Remove Nadare using the tool that installed it: your software centre for "
                "Flatpak or packages, or Gear Lever / AppImageLauncher for a managed AppImage. "
                "For a source install, remove its checkout and virtual environment. "
                "A portable AppImage can be moved to Trash in your file manager. "
                "Keep application data to preserve games and settings.",
            )
            return
        answer = QMessageBox.question(
            self, "Uninstall Nadare",
            "For a portable AppImage, move this file to Trash?\n\n"
            f"{plan.path}\n\n"
            "Your games, saves, accounts, runners and settings will be kept. "
            "Other AppImage versions and shortcuts will be kept too.\n\n"
            "If you installed Nadare with Gear Lever or AppImageLauncher, cancel and "
            "remove it there instead, so its menu entry is also removed.",
            QMessageBox.StandardButton.Yes | QMessageBox.StandardButton.No,
            QMessageBox.StandardButton.No,
        )
        if answer != QMessageBox.StandardButton.Yes:
            return
        try:
            app_uninstall.trash(plan, QFile.moveToTrash)
        except (OSError, ValueError) as exc:
            QMessageBox.warning(self, "Could not uninstall Nadare", str(exc))
            return
        QMessageBox.information(
            self, "AppImage moved to Trash",
            "The AppImage is in Trash. Close Nadare when you have finished any running "
            "games or downloads. You can restore the AppImage from Trash.",
        )

    def _open_path(self, path: str, what: str) -> None:
        """Open ``path`` in the desktop's file manager/editor, or say why not."""
        text = path.strip()
        if not text:
            self.note.setText(f"No {what} is set yet.")
            return
        target = Path(text).expanduser()
        if not target.exists():
            self.note.setText(f"The {what} does not exist yet: {target}")
            return
        QDesktopServices.openUrl(QUrl.fromLocalFile(str(target)))

    def _open_game_folder(self) -> None:
        self._open_path(self.install_dir.text(), "game folder")

    def _open_prefix_folder(self) -> None:
        editor = self._option_widgets.get("prefix")
        text = editor.text() if isinstance(editor, QLineEdit) else ""
        self._open_path(text or self._probed_prefix, "Wine prefix")

    def _open_discord(self) -> None:
        """Look up the invite off the GUI thread (it may ask the index), then open it."""
        self.discord_button.setEnabled(False)
        task = CallTask(community.discord_url)
        task.signals.done.connect(self._discord_ready)
        task.signals.failed.connect(lambda _message: self._discord_ready(None))
        QThreadPool.globalInstance().start(task)

    def _discord_ready(self, url: object) -> None:
        self.discord_button.setEnabled(True)
        if not community.is_discord_invite(url):
            url = community.DISCORD_FALLBACK  # never open anything that is not an invite
        QDesktopServices.openUrl(QUrl(str(url)))

    def _open_config_file(self) -> None:
        if self._config is None:
            return
        self._open_path(str(self._config.path), "config file")

    def _fetch_art(self) -> None:
        """Download cover art for the current profile off the GUI thread.

        ``artwork.fetch_all`` performs several blocking HTTPS queries; running it
        here would freeze the window for tens of seconds. The live profile is
        never handed to the worker (it records provenance, which would race a
        concurrent ``Config.save()``): it gets a deep copy and the result is
        applied on the GUI thread.
        """
        if self._config is None or not self._profile:
            return
        from ...core import artwork

        try:
            profile = self._config.profile(self._profile)
        except KeyError:
            return
        title = self.title.text().strip() or self._profile
        with self._config.locked():
            snapshot = copy.deepcopy(profile)
        profile_name = self._profile
        if self._fetch_button is not None:
            self._fetch_button.setEnabled(False)
        self.note.setText("Searching for artwork…")

        task = CallTask(lambda: artwork.fetch_all(snapshot, title=title))
        task.signals.done.connect(
            lambda found: self._on_art_fetched(profile_name, found)
        )
        task.signals.failed.connect(self._on_art_failed)
        QThreadPool.globalInstance().start(task)

    def _on_art_fetched(self, profile_name: str, found: object) -> None:
        """Apply a finished art fetch on the GUI thread."""
        if self._fetch_button is not None:
            self._fetch_button.setEnabled(True)
        if self._config is None or profile_name != self._profile:
            return  # the user moved on while the fetch was in flight
        paths = found if isinstance(found, dict) else {}
        if not paths:
            self.note.setText("No artwork found")
            return

        self._pending_art = dict(paths)
        cover = paths.get("cover") or next(iter(paths.values()))
        self.background.setText(str(cover))
        self.note.setText(f"Fetched {count_text(len(paths), 'image')} to {Path(cover).parent}")

    def _on_art_failed(self, message: str) -> None:
        if self._fetch_button is not None:
            self._fetch_button.setEnabled(True)
        self.note.setText(f"Artwork fetch failed: {message}")

    def _runner_value(self) -> str:
        """The runner the form currently selects (a listed entry, or typed text)."""
        text = self.runner.currentText().strip()
        index = self.runner.currentIndex()
        if index >= 0 and text == self.runner.itemText(index):
            return str(self.runner.currentData() or "")
        return text

    def _open_runners_dialog(self) -> None:
        """Show the runner manager, then refresh the picker (a runner may be gone)."""
        from ..runners_dialog import ManageRunnersDialog

        # Judge "in use" by what the form shows now, not only what is saved: an
        # unsaved selection must not make the runner look unused.
        # Under the config lock: a worker saving meanwhile must not resize the dict mid-copy.
        profiles: dict = {}
        if self._config is not None:
            with self._config.locked():
                profiles = copy.deepcopy(self._config.profiles)
        if self._profile in profiles:
            profiles[self._profile]["runner"] = self._runner_value()
        dialog = ManageRunnersDialog(profiles, self)
        dialog.exec()
        # The default entry's data is ""; falling back to its label would persist the label.
        self._reload_runners(self._runner_value())

    def _reload_runners(self, selected: str) -> bool:
        """Refill the picker and select ``selected``; False when it is not a listed runner."""
        self.runner.clear()
        entries = discover_runners(self._backend_kind)
        if not getattr(self, "_runner_default_applies", True):
            # An explicit wine_binary runs instead of the default pinned runner.
            entries = [
                (label, value)
                for label, value in entries
                if not (value == "" and label.startswith("Default · recommended"))
            ]
        for label, value in entries:
            self.runner.addItem(label, value)
        index = self.runner.findData(selected) if selected else -1
        matched = index >= 0
        if index < 0 and selected in runners_core.RUNNERS:
            # A family id (``"dwproton"``) means that family's pinned, tested build.
            tag = runners_core.pinned_tag(selected) or selected
            self.runner.insertItem(0, f"{tag} · recommended (pinned)", selected)
            index = 0
            matched = True
        elif index < 0 and selected in ("latest", "auto"):
            # resolve_runner() understands these aliases; they are not broken.
            self.runner.insertItem(0, f"{selected.title()} installed runner", selected)
            index = 0
            matched = True
        elif index < 0 and selected and _is_runner_folder(selected):
            # A Proton tree of the user's own (not in Nadare's list) works: say so (#331 F-10).
            self.runner.insertItem(0, f"Custom · {Path(selected).expanduser().name}", selected)
            index = 0
            matched = True
        elif index < 0 and selected:
            self.runner.insertItem(0, f"Missing · {selected}", selected)
            index = 0
        if index < 0:
            index = self.runner.findData("")
            matched = True
        self.runner.setCurrentIndex(max(0, index))
        return matched
