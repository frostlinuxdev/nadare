# SPDX-License-Identifier: GPL-3.0-or-later
# Copyright (C) 2026 Nadare contributors
"""The "Add game" wizard: pick a backend, install new or import an existing one.

The dialog only reads the folders the user points it at; it never creates,
moves or deletes anything inside them. Saving a profile writes the config, and
the caller decides what to do next (open the game page, offer a verify run).
"""

from __future__ import annotations

from dataclasses import dataclass
from pathlib import Path
from typing import Any

from PyQt6.QtCore import Qt, pyqtSignal
from PyQt6.QtWidgets import (
    QButtonGroup,
    QDialog,
    QFileDialog,
    QFormLayout,
    QHBoxLayout,
    QLabel,
    QLineEdit,
    QListWidget,
    QListWidgetItem,
    QPushButton,
    QRadioButton,
    QVBoxLayout,
    QWidget,
)

from ..core import backends, paths
from ..core.backends import BackendError
from ..core.config import MAX_PROFILE_NAME, Config, valid_profile_name
from .theme import apply_theme


@dataclass(frozen=True)
class GameType:
    """One selectable backend in the wizard."""

    title: str
    kind: str
    group: str = "Games"
    install_subdir: str = ""
    exes: tuple[str, ...] = ()
    #: Short tag shown after the title in the list (e.g. "Experimental").
    badge: str = ""
    #: Tooltip explaining the badge.
    badge_help: str = ""


CATALOG: tuple[GameType, ...] = (
    GameType("Chaos Zero Nightmare", "stove-czn"),
    GameType(
        "Fate/Grand Order",
        "fgo-lepton",
        badge="Experimental",
        badge_help=(
            "Runs in an Android container (Lepton). Needs podman and a kernel with "
            "binder support; run `nadare doctor --fgo` to check your host."
        ),
    ),
    GameType(
        "Arknights",
        "arknights-yostar",
        install_subdir="Arknights_EN",
        exes=(
            "Arknights.exe",
            "Arknights_EN_Gamelauncher.exe",
            "game launcher.exe",
            "Arknights_EN.exe",
        ),
    ),
    GameType(
        "Stella Sora",
        "stellasora-yostar",
        install_subdir="StellaSora_EN",
        exes=("StellaSora.exe",),
    ),
    GameType("Wine / Proton game", "wine", group="Other"),
    GameType("Steam game", "steam", group="Other"),
    GameType("Script", "script", group="Other"),
    GameType("Waydroid app", "waydroid", group="Other"),
)

#: Kinds that run through a Wine/Proton prefix (so the wizard offers a prefix).
PREFIX_KINDS = frozenset({"wine", "script", "arknights-yostar", "stellasora-yostar", "stove-czn"})

#: Prefix kinds whose whole install lives inside the Wine prefix (no separate
#: game folder), so a picked "install folder" is really the prefix.
PREFIX_ONLY_KINDS = frozenset({"stove-czn"})


def available_catalog() -> tuple[GameType, ...]:
    """The catalog minus built-in kinds this build could not load (e.g. no FGO)."""
    missing = set(backends.unavailable())
    return tuple(g for g in CATALOG if g.kind not in missing)


#: Characters kept by :func:`_slugify` (ASCII letters/digits only, so a
#: generated profile name always satisfies :func:`valid_profile_name`).
_SLUG_CHARS = "0123456789abcdefghijklmnopqrstuvwxyz"


def _slugify(text: str) -> str:
    keep = [c.lower() if c.lower() in _SLUG_CHARS else "-" for c in text.strip()]
    slug = "".join(keep).strip("-")
    while "--" in slug:
        slug = slug.replace("--", "-")
    return slug[:MAX_PROFILE_NAME].strip("-")


def _find_exe(folder: Path, names: tuple[str, ...], *, max_depth: int = 2) -> Path | None:
    """Find one of ``names`` under ``folder`` (bounded, read-only walk)."""
    wanted = {name.lower() for name in names}
    stack: list[tuple[Path, int]] = [(folder, 0)]
    visited = 0
    while stack and visited < 400:
        directory, depth = stack.pop(0)
        visited += 1
        try:
            entries = list(directory.iterdir())
        except OSError:
            continue
        for entry in entries:
            if entry.is_file() and entry.name.lower() in wanted:
                return entry
        if depth < max_depth:
            stack.extend((entry, depth + 1) for entry in entries if entry.is_dir())
    return None


def detect_import(kind: str, folder: str | Path | None, prefix: str | Path | None) -> dict:
    """Best-effort, read-only detection of an existing install.

    Returns ``{"install_dir", "prefix", "notes"}``. Nothing is ever written:
    callers decide which hint to adopt.
    """
    info: dict[str, Any] = {"install_dir": "", "prefix": "", "notes": []}
    folder_path = Path(folder) if folder else None
    prefix_path = Path(prefix) if prefix else None
    game = next((g for g in CATALOG if g.kind == kind), None)

    if game and game.install_subdir:
        subdir = Path("drive_c") / "YostarGames" / game.install_subdir
        # The user may have picked the prefix itself (which holds drive_c/...).
        if folder_path and (folder_path / "drive_c").is_dir():
            candidate = folder_path / subdir
            if candidate.is_dir():
                info["prefix"] = str(folder_path)
                info["install_dir"] = str(candidate)
                info["notes"].append(f"Found the {game.title} prefix layout.")
        if not info["install_dir"] and folder_path and folder_path.is_dir():
            exe = _find_exe(folder_path, game.exes)
            if exe is not None:
                info["install_dir"] = str(folder_path)
                info["notes"].append(f"Found {exe.name}.")
        if prefix_path:
            info["prefix"] = str(prefix_path)
            candidate = prefix_path / subdir
            if candidate.is_dir():
                info["notes"].append(f"Found {game.title} in the prefix.")
                if not info["install_dir"]:
                    info["install_dir"] = str(candidate)
        if not info["install_dir"] and info["prefix"]:
            # Keep the backend's own default so the profile is usable.
            info["install_dir"] = str(Path(info["prefix"]) / subdir)
    else:
        if folder_path:
            info["install_dir"] = str(folder_path)
            if kind in PREFIX_ONLY_KINDS:
                # The backend reads only ``prefix()``; a picked folder for such
                # a kind is the existing Wine prefix, not a separate game dir.
                info["prefix"] = str(folder_path)
        if prefix_path:
            info["prefix"] = str(prefix_path)
    return info


class AddGameDialog(QDialog):
    """QDialog that creates one profile for a chosen backend."""

    created = pyqtSignal(str)  # profile name

    def __init__(
        self, config: Config, parent: QWidget | None = None, data_root: Path | None = None
    ) -> None:
        super().__init__(parent)
        self.setObjectName("Root")
        apply_theme(self)
        self.config = config
        self.created_profile = ""
        self._data_root = data_root
        self._name_touched = False
        self._folder_touched = False
        self._prefix_touched = False

        self.setWindowTitle("Add game")
        self.setMinimumWidth(560)

        root = QVBoxLayout(self)
        root.setContentsMargins(26, 22, 26, 22)
        root.setSpacing(14)

        heading = QLabel("Add a game")
        heading.setObjectName("Title")
        root.addWidget(heading)
        subtitle = QLabel(
            "Pick a supported game, then install a fresh copy or import one you already have."
        )
        subtitle.setObjectName("Subtitle")
        subtitle.setWordWrap(True)
        root.addWidget(subtitle)

        self.game_list = QListWidget()
        self.game_list.setObjectName("GameList")
        self.game_list.setMaximumHeight(210)
        self._first_row = -1
        self._populate_games()
        self.game_list.currentRowChanged.connect(self._on_game_changed)
        root.addWidget(self.game_list)

        mode_row = QHBoxLayout()
        self.install_new = QRadioButton("Install new")
        self.import_existing = QRadioButton("Import existing")
        self.install_new.setChecked(True)
        mode_group = QButtonGroup(self)
        mode_group.addButton(self.install_new)
        mode_group.addButton(self.import_existing)
        self.install_new.toggled.connect(self._on_mode_changed)
        mode_row.addWidget(self.install_new)
        mode_row.addWidget(self.import_existing)
        mode_row.addStretch(1)
        root.addLayout(mode_row)

        form = QFormLayout()
        form.setHorizontalSpacing(18)
        form.setVerticalSpacing(10)
        form.setFieldGrowthPolicy(QFormLayout.FieldGrowthPolicy.AllNonFixedFieldsGrow)

        self.folder_label = QLabel("Install folder")
        self.folder = QLineEdit()
        self.folder.setPlaceholderText("folder the game will live in")
        self.folder.textEdited.connect(lambda _t: setattr(self, "_folder_touched", True))
        self.folder.editingFinished.connect(self.detect)
        form.addRow(self.folder_label, self._with_browse(self.folder, self._browse_folder))
        self.folder_free = QLabel("")
        self.folder_free.setObjectName("Muted")
        form.addRow("", self.folder_free)

        self.prefix_row = QWidget()
        prefix_layout = QHBoxLayout(self.prefix_row)
        prefix_layout.setContentsMargins(0, 0, 0, 0)
        prefix_layout.setSpacing(8)
        self.prefix = QLineEdit()
        self.prefix.setPlaceholderText("existing Wine prefix")
        self.prefix.textEdited.connect(lambda _t: setattr(self, "_prefix_touched", True))
        self.prefix.editingFinished.connect(self.detect)
        prefix_layout.addWidget(self.prefix, 1)
        prefix_browse = QPushButton("Browse")
        prefix_browse.setObjectName("GhostButton")
        prefix_browse.clicked.connect(self._browse_prefix)
        prefix_layout.addWidget(prefix_browse)
        form.addRow("Wine prefix", self.prefix_row)
        self.prefix_free = QLabel("")
        self.prefix_free.setObjectName("Muted")
        form.addRow("", self.prefix_free)

        self.name = QLineEdit()
        self.name.setPlaceholderText("profile name")
        self.name.textEdited.connect(lambda _t: setattr(self, "_name_touched", True))
        form.addRow("Profile name", self.name)
        root.addLayout(form)

        self.detect_label = QLabel("")
        self.detect_label.setObjectName("Muted")
        self.detect_label.setWordWrap(True)
        root.addWidget(self.detect_label)

        self.error_label = QLabel("")
        self.error_label.setObjectName("Error")
        self.error_label.setWordWrap(True)
        root.addWidget(self.error_label)

        footer = QHBoxLayout()
        footer.addStretch(1)
        cancel = QPushButton("Cancel")
        cancel.setObjectName("GhostButton")
        cancel.clicked.connect(self.reject)
        create = QPushButton("Add game")
        create.setObjectName("Primary")
        create.setMinimumHeight(40)
        create.clicked.connect(self.create_profile)
        footer.addWidget(cancel)
        footer.addWidget(create)
        root.addLayout(footer)

        self._select_row(self._first_row)
        self._on_game_changed(self._first_row)

    # ---- construction helpers ---------------------------------------------
    def _populate_games(self) -> None:
        for group in ("Games", "Other"):
            header = QListWidgetItem(group.upper())
            header.setFlags(Qt.ItemFlag.NoItemFlags)
            self.game_list.addItem(header)
            for game in available_catalog():
                if game.group != group:
                    continue
                label = f"{game.title}  ·  {game.badge}" if game.badge else game.title
                item = QListWidgetItem(label)
                if game.badge_help:
                    item.setToolTip(game.badge_help)
                item.setData(Qt.ItemDataRole.UserRole, game.kind)
                self.game_list.addItem(item)
                if self._first_row < 0:
                    self._first_row = self.game_list.row(item)

    def _with_browse(self, line_edit: QLineEdit, slot) -> QWidget:
        row = QWidget()
        layout = QHBoxLayout(row)
        layout.setContentsMargins(0, 0, 0, 0)
        layout.setSpacing(8)
        layout.addWidget(line_edit, 1)
        browse = QPushButton("Browse")
        browse.setObjectName("GhostButton")
        browse.clicked.connect(slot)
        layout.addWidget(browse)
        return row

    # ---- selection ---------------------------------------------------------
    def _select_row(self, row: int) -> None:
        if row >= 0:
            self.game_list.setCurrentRow(row)

    def selected_kind(self) -> str:
        item = self.game_list.currentItem()
        if item is None:
            return ""
        return str(item.data(Qt.ItemDataRole.UserRole) or "")

    def _selected_game(self) -> GameType | None:
        kind = self.selected_kind()
        return next((g for g in CATALOG if g.kind == kind), None)

    def set_backend(self, kind: str) -> None:
        for row in range(self.game_list.count()):
            item = self.game_list.item(row)
            if item is not None and item.data(Qt.ItemDataRole.UserRole) == kind:
                self._select_row(row)
                return

    def set_mode(self, import_existing: bool) -> None:
        self.import_existing.setChecked(bool(import_existing))

    def set_install_dir(self, path: str | Path) -> None:
        self.folder.setText(str(path))
        self._folder_touched = True

    def set_prefix(self, path: str | Path) -> None:
        self.prefix.setText(str(path))
        self._prefix_touched = True

    def set_profile_name(self, name: str) -> None:
        self.name.setText(name)
        self._name_touched = True

    # ---- defaults / detection ---------------------------------------------
    def _default_name(self, title: str) -> str:
        base = _slugify(title) or "game"
        if base not in self.config.profiles:
            return base
        # Keep the disambiguating suffix within the name length cap so the
        # generated default always passes ``valid_profile_name``.
        index = 2
        while True:
            suffix = f"-{index}"
            candidate = f"{base[: MAX_PROFILE_NAME - len(suffix)]}{suffix}"
            if candidate not in self.config.profiles:
                return candidate
            index += 1

    def _slug(self) -> str:
        game = self._selected_game()
        return _slugify(game.title if game else "game") or "game"

    def _root(self) -> Path:
        return self._data_root if self._data_root is not None else paths.profiles_dir()

    def _on_game_changed(self, _row: int) -> None:
        game = self._selected_game()
        if game is None:
            return
        if game.kind in PREFIX_KINDS:
            self.prefix_row.setVisible(True)
        else:
            self.prefix_row.setVisible(False)
        if not self._name_touched:
            self.name.setText(self._default_name(game.title))
        if not self._folder_touched and self.install_new.isChecked():
            self.folder.setText(str(self._root() / self._slug() / "install"))
        if not self._prefix_touched:
            # A default prefix only makes sense for a fresh install; in Import
            # mode an auto-filled, non-existent path must never be accepted.
            if game.kind in PREFIX_KINDS and self.install_new.isChecked():
                self.prefix.setText(str(self._root() / self._slug() / "prefix"))
            else:
                self.prefix.clear()
        self.detect()

    def _on_mode_changed(self, _checked: bool = False) -> None:
        if self.import_existing.isChecked():
            self.folder_label.setText("Existing install folder")
            if not self._folder_touched:
                self.folder.clear()
            # Drop an untouched default prefix so the import guard can tell a
            # real pick from the auto-filled path.
            if not self._prefix_touched:
                self.prefix.clear()
        else:
            self.folder_label.setText("Install folder")
            if not self._folder_touched:
                self.folder.setText(str(self._root() / self._slug() / "install"))
            if not self._prefix_touched and self._selected_game() is not None:
                if self._selected_game().kind in PREFIX_KINDS:
                    self.prefix.setText(str(self._root() / self._slug() / "prefix"))
        self.detect()

    def _browse_folder(self) -> None:
        start = self.folder.text().strip() or str(Path.home())
        chosen = QFileDialog.getExistingDirectory(self, "Select folder", start)
        if chosen:
            self.folder.setText(chosen)
            self._folder_touched = True
            self.detect()

    def _browse_prefix(self) -> None:
        start = self.prefix.text().strip() or str(Path.home())
        chosen = QFileDialog.getExistingDirectory(self, "Select Wine prefix", start)
        if chosen:
            self.prefix.setText(chosen)
            self._prefix_touched = True
            self.detect()

    def _free_hint(self, path: str) -> str:
        text = path.strip()
        if not text:
            return ""
        try:
            from nadare.core import diskspace

            free = diskspace.human_size(diskspace.free_bytes(text))
            return f"{free} free on {diskspace.mount_point(text)}"
        except (OSError, BackendError):
            return ""

    def _update_free_space(self) -> None:
        self.folder_free.setText(self._free_hint(self.folder.text()))
        self.prefix_free.setText(self._free_hint(self.prefix.text()))

    def detect(self) -> dict:
        """Run read-only detection and update the hints; returns the result."""
        self._update_free_space()
        kind = self.selected_kind()
        if not kind or not self.import_existing.isChecked():
            self.detect_label.setText("")
            return {}
        info = detect_import(kind, self.folder.text().strip(), self.prefix.text().strip())
        notes = list(info.get("notes") or [])
        if info.get("install_dir"):
            notes.append(f"Install folder: {info['install_dir']}")
        if info.get("prefix"):
            notes.append(f"Prefix: {info['prefix']}")
        self.detect_label.setText("  ".join(notes) if notes else "Nothing detected yet.")
        return info

    # ---- creation ----------------------------------------------------------
    def create_profile(self) -> str | None:
        """Validate, write the profile to the config, and accept on success."""
        kind = self.selected_kind()
        game = self._selected_game()
        if not kind or game is None:
            self.error_label.setText("Pick a game first.")
            return None
        name = self.name.text().strip() or self._default_name(game.title)
        if not valid_profile_name(name):
            self.error_label.setText(
                "Profile names cannot contain path separators (/ or \\) or "
                "control characters, must not start with a drive letter, and "
                f"must be at most {MAX_PROFILE_NAME} characters."
            )
            return None
        if name in self.config.profiles:
            self.error_label.setText(f"A profile named {name!r} already exists.")
            return None

        folder = self.folder.text().strip()
        prefix = self.prefix.text().strip()
        importing = self.import_existing.isChecked()
        if importing and not folder and not prefix:
            self.error_label.setText("Pick the existing install folder (or its prefix).")
            return None

        info = detect_import(kind, folder, prefix) if importing else {}
        if importing:
            install_dir = info.get("install_dir") or folder or ""
            prefix_value = prefix or info.get("prefix") or ""
            if not install_dir:
                install_dir = prefix_value  # accept the picked folder as-is
        else:
            install_dir = folder
            prefix_value = prefix

        values: dict[str, Any] = {"backend": kind, "title": game.title}
        if install_dir:
            values["install_dir"] = install_dir
        if prefix_value and kind in PREFIX_KINDS:
            values["prefix"] = prefix_value

        with self.config.locked():
            self.config.add_profile(name, **values)
            try:
                self.config.save()
            except OSError as exc:
                self.config.remove_profile(name)
                self.error_label.setText(f"Could not save config: {exc}")
                return None
        # Detection only ever produced install_dir/prefix on existing dirs; the
        # prefix may be created later by install, but never here.
        self.created_profile = name
        self.created.emit(name)
        self.accept()
        return name


__all__ = [
    "AddGameDialog",
    "CATALOG",
    "GameType",
    "PREFIX_KINDS",
    "PREFIX_ONLY_KINDS",
    "available_catalog",
    "detect_import",
]
