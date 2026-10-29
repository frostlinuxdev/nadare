# SPDX-License-Identifier: GPL-3.0-or-later
# Copyright (C) 2026 Nadare contributors
"""The "Add game" wizard: pick a backend, install new or import an existing one.

The dialog only reads the folders the user points it at; it never creates,
moves or deletes anything inside them. Saving a profile writes the config, and
the caller decides what to do next (open the game page, offer a verify run).
"""

from __future__ import annotations

import os
from dataclasses import dataclass, replace
from pathlib import Path
from typing import Any

from PyQt6.QtCore import Qt, pyqtSignal
from PyQt6.QtWidgets import (
    QButtonGroup,
    QDialog,
    QFileDialog,
    QFormLayout,
    QFrame,
    QHBoxLayout,
    QLabel,
    QLineEdit,
    QListWidget,
    QListWidgetItem,
    QPushButton,
    QRadioButton,
    QScrollArea,
    QSizePolicy,
    QVBoxLayout,
    QWidget,
)

from ..core import backends, paths
from ..core.backends import BackendError
from ..core.config import MAX_PROFILE_NAME, Config, valid_profile_name
from ..core.directkinds import (
    CATALOG_KINDS,
    DIRECT_WIZARD,
    HOYO_KINDS,
    LAUNCHER_WIZARD,
    TIER_HELP,
    hoyo_visible,
    tier_of,
)
from ..core.shortcuts import command_hint
from .theme import apply_theme


@dataclass(frozen=True)
class GameType:
    """One selectable backend in the wizard."""

    title: str
    kind: str
    group: str = "Games"
    install_subdir: str = ""
    #: Folder (under the prefix) that holds ``install_subdir``.
    install_root: str = "drive_c/YostarGames"
    exes: tuple[str, ...] = ()
    #: Short tag shown after the title in the list (e.g. "Experimental").
    badge: str = ""
    #: Tooltip explaining the badge.
    badge_help: str = ""


#: The wizard group for profiles that need the user's own game files.
ADVANCED_GROUP = "Advanced (bring your own game files)"

_CATALOG: tuple[GameType, ...] = (
    GameType("Chaos Zero Nightmare", "stove-czn"),
    GameType(
        "Fate/Grand Order",
        "fgo-lepton",
        badge="Experimental",
        # The command half of this hint is filled in by _catalog() at display time.
        badge_help=(
            "Runs in an Android container (Lepton). Needs podman and a kernel with binder support."
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
    *(
        GameType(
            title,
            kind,
            install_subdir=subdir,
            install_root=root,
            exes=(exe,),
            badge=tier_of(kind),
            badge_help=(
                "Installs straight from the publisher's servers, without their launcher. "
                + TIER_HELP[tier_of(kind)]
            ),
        )
        for kind, title, root, subdir, exe in DIRECT_WIZARD
    ),
    *(
        GameType(
            title,
            kind,
            install_subdir=subdir,
            install_root=root,
            exes=(exe,),
            badge=tier_of(kind),
            badge_help=(
                "Runs the publisher's own launcher under Wine/Proton. "
                + TIER_HELP[tier_of(kind)]
                + " Anti-cheat or launcher updates can break it."
            ),
        )
        for kind, title, root, subdir, exe in LAUNCHER_WIZARD
    ),
    GameType(
        "Wine / Proton game",
        "wine",
        group=ADVANCED_GROUP,
        badge="bring your own files",
        badge_help="Use a Wine or Proton prefix and game files you already have.",
    ),
    GameType(
        "Steam game",
        "steam",
        group=ADVANCED_GROUP,
        badge="bring your own files",
        badge_help="Use a game already installed in your Steam library.",
    ),
    GameType(
        "Script",
        "script",
        group=ADVANCED_GROUP,
        badge="bring your own files",
        badge_help="Run a command or script you already have.",
    ),
)


def _catalog() -> tuple[GameType, ...]:
    """The catalog with hints that name the command this build actually has."""
    fgo_help = (
        "Runs in an Android container (Lepton). Needs podman and a kernel with "
        f"binder support; run `{command_hint('doctor --fgo')}` to check your host."
    )
    return tuple(
        replace(game, badge_help=fgo_help) if game.kind == "fgo-lepton" else game
        for game in _CATALOG
    )


def __getattr__(name: str) -> Any:
    if name == "CATALOG":
        return _catalog()
    raise AttributeError(f"module {__name__!r} has no attribute {name!r}")


#: Kinds that run through a Wine/Proton prefix (so the wizard offers a prefix).
PREFIX_KINDS = frozenset(
    {"wine", "script", "arknights-yostar", "stellasora-yostar", "stove-czn", *CATALOG_KINDS}
)

#: Prefix kinds whose whole install lives inside the Wine prefix (no separate
#: game folder), so a picked "install folder" is really the prefix.
PREFIX_ONLY_KINDS = frozenset({"stove-czn"})


def available_catalog() -> tuple[GameType, ...]:
    """The catalog minus kinds this build could not load (e.g. no FGO) and the HoYo games."""
    missing = set(backends.unavailable())
    if not hoyo_visible():
        missing |= HOYO_KINDS
    return tuple(g for g in _catalog() if g.kind not in missing)


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


def _folder_name(title: str) -> str:
    """``title`` as a folder name: path and shell-hostile characters become spaces."""
    cleaned = "".join(" " if c in '/\\:*?"<>|' else c for c in title)
    return " ".join(cleaned.split()) or "game"


def _new_or_empty(path: Path) -> bool:
    """True when ``path`` does not exist yet or is an empty folder."""
    try:
        return not path.exists() or (path.is_dir() and not any(path.iterdir()))
    except OSError:
        return False


def _absolute(path: str | Path) -> str:
    """``path`` with ``~`` expanded and made absolute, keeping its spelling (no symlink loss).

    A path with a ``..`` component is resolved physically, the way the kernel reads it;
    collapsing it lexically could land in a different folder when a symlink is involved.
    """
    text = os.path.expanduser(str(path))
    if not os.path.isabs(text):
        text = os.path.join(os.getcwd(), text)
    if ".." in Path(text).parts:
        return os.path.realpath(text)
    return os.path.normpath(text)


def _cannot_create(path: str | Path) -> bool:
    """True when ``path`` exists but cannot be written, or cannot be created at all.

    A folder that does not exist yet is allowed when its nearest existing ancestor is a
    writable folder (the install creates the chain later).
    """
    probe = Path(path).expanduser()
    try:
        while not probe.exists():
            parent = probe.parent
            if parent == probe:
                return True
            probe = parent
        if not probe.is_dir():
            return True
        return not os.access(probe, os.W_OK | os.X_OK)
    except OSError:
        return True


def _holds_game(game: GameType, folder: Path) -> bool:
    """True when ``folder`` already has one of ``game``'s executables directly in it.

    A folder the user may not look into (EACCES, a stale mount) holds no game we can use.
    """
    try:
        return any((folder / exe).exists() for exe in game.exes)
    except OSError:
        return False


def _inside(path: Path, root: Path) -> bool:
    """True when ``path`` is ``root`` or lies below it (no symlink resolution needed here)."""
    return path == root or root in path.parents


def _claimed(profiles: dict[str, Any], folder: Path) -> bool:
    """True when a profile already records ``folder`` or something inside it."""
    text = str(folder)
    for profile in profiles.values():
        if not isinstance(profile, dict):
            continue
        recorded = [
            *(profile.get("owned_dirs") or ()),
            profile.get("install_dir"),
            profile.get("prefix"),
        ]
        for value in recorded:
            if isinstance(value, str) and (value == text or value.startswith(text + "/")):
                return True
    return False


def _free_folder(root: Path, name: str, profiles: dict[str, Any]) -> Path:
    """``root/name``, or ``root/name 2``, ``name 3``... the first neither on disk nor claimed.

    A game folder is only created when its install starts, so two profiles added before
    that must be told apart by what the config records, not by the disk alone.
    """
    candidate = root / name
    index = 2
    while candidate.exists() or _claimed(profiles, candidate):
        candidate = root / f"{name} {index}"
        index += 1
    return candidate


def _drive_c_prefix(folder: Path) -> Path | None:
    """The Wine prefix that contains ``folder``, if it lies under ``drive_c``.

    Users often pick the game's own folder (``.../prefix/drive_c/...``) instead
    of the prefix itself. Walking up to the directory that holds ``drive_c``
    recovers the prefix rather than silently dropping it.
    """
    for candidate in (folder, *folder.parents):
        if (candidate / "drive_c").is_dir():
            return candidate
    return None


def detect_import(kind: str, folder: str | Path | None, prefix: str | Path | None) -> dict:
    """Best-effort, read-only detection of an existing install.

    Returns ``{"install_dir", "prefix", "notes"}``. Nothing is ever written:
    callers decide which hint to adopt.
    """
    info: dict[str, Any] = {"install_dir": "", "prefix": "", "notes": []}
    folder_path = Path(folder).expanduser() if folder else None
    prefix_path = Path(prefix).expanduser() if prefix else None
    game = next((g for g in _catalog() if g.kind == kind), None)
    adopted = (
        _drive_c_prefix(folder_path)
        if folder_path and folder_path.is_dir() and not (folder_path / "drive_c").is_dir()
        else None
    )
    if adopted is not None:
        info["prefix"] = str(adopted)
        info["notes"].append(f"Found the Wine prefix at {adopted}.")

    if game and game.install_subdir:
        subdir = Path(game.install_root) / game.install_subdir
        # The user may have picked the prefix itself (which holds drive_c/...).
        layout_root = adopted if adopted is not None else folder_path
        if layout_root and (layout_root / "drive_c").is_dir():
            candidate = layout_root / subdir
            if candidate.is_dir():
                info["prefix"] = str(layout_root)
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
        if folder_path and folder_path.is_dir():
            if kind in PREFIX_ONLY_KINDS:
                # The backend reads only ``prefix()``; only a real prefix (one
                # holding drive_c, or a folder inside it) is worth importing.
                found = _drive_c_prefix(folder_path)
                if found is not None:
                    info["prefix"] = str(found)
                    if not any("prefix" in note.lower() for note in info["notes"]):
                        info["notes"].append(f"Found the Wine prefix at {found}.")
            else:
                info["install_dir"] = str(folder_path)
                info["notes"].append("Folder found; Nadare will use it as-is.")
                if adopted is not None:
                    info["notes"].append("Nadare will use the Wine prefix that contains it.")
        if prefix_path and prefix_path.is_dir():
            # The game folder inside the prefix is an easy thing to pick: use the
            # prefix that contains it.
            found = _drive_c_prefix(prefix_path)
            if kind not in PREFIX_ONLY_KINDS or found is not None:
                info["prefix"] = str(found or prefix_path)
                info["notes"].append("Prefix folder found.")
    return info


class _ElidedLabel(QLabel):
    """A one-line label that shortens the middle of a long path instead of widening the dialog."""

    def __init__(self, parent: QWidget | None = None) -> None:
        super().__init__(parent)
        self._full = ""
        self.setSizePolicy(QSizePolicy.Policy.Ignored, QSizePolicy.Policy.Preferred)

    def full_text(self) -> str:
        return self._full

    def set_full_text(self, text: str) -> None:
        self._full = text
        self.setToolTip(text)
        self._elide()

    def resizeEvent(self, event) -> None:  # noqa: N802 - Qt naming
        super().resizeEvent(event)
        self._elide()

    def _elide(self) -> None:
        shown = self.fontMetrics().elidedText(
            self._full, Qt.TextElideMode.ElideMiddle, max(self.width(), 60)
        )
        if shown != self.text():
            self.setText(shown)


class AddGameDialog(QDialog):
    """QDialog that creates one profile for a chosen backend."""

    created = pyqtSignal(str)  # profile name

    def __init__(
        self,
        config: Config,
        parent: QWidget | None = None,
        data_root: Path | None = None,
        *,
        kind: str | None = None,
        advanced: bool = False,
    ) -> None:
        super().__init__(parent)
        self.setObjectName("Root")
        apply_theme(self)
        self.config = config
        self.created_profile = ""
        self.created_by_import = False
        self._data_root = data_root
        self._name_touched = False
        self._folder_touched = False
        self._prefix_touched = False

        self.setWindowTitle("Add game")
        self.setMinimumWidth(560)

        # The form scrolls on a small screen; the Cancel / Add game buttons stay put.
        outer = QVBoxLayout(self)
        outer.setContentsMargins(0, 0, 0, 14)
        outer.setSpacing(8)
        scroll = QScrollArea()
        scroll.setWidgetResizable(True)
        scroll.setFrameShape(QFrame.Shape.NoFrame)
        body = QWidget()
        scroll.setWidget(body)
        outer.addWidget(scroll, 1)
        root = QVBoxLayout(body)
        root.setContentsMargins(26, 22, 26, 8)
        root.setSpacing(14)

        heading = QLabel("Add a game")
        heading.setObjectName("Title")
        root.addWidget(heading)
        subtitle = QLabel(
            "Choose your game. Nadare can install the games above; the options under "
            "Advanced need files you already have."
        )
        subtitle.setObjectName("Subtitle")
        subtitle.setWordWrap(True)
        root.addWidget(subtitle)

        self.game_list = QListWidget()
        self.game_list.setObjectName("GameList")
        self.game_list.setMaximumHeight(210)
        self._first_row = -1
        valid_kind = kind if any(game.kind == kind for game in available_catalog()) else None
        self._populate_games(advanced_only=advanced)
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

        self.size_hint = QLabel("")
        self.size_hint.setObjectName("Muted")
        self.size_hint.setWordWrap(True)
        root.addWidget(self.size_hint)

        form = QFormLayout()
        form.setHorizontalSpacing(18)
        form.setVerticalSpacing(10)
        form.setFieldGrowthPolicy(QFormLayout.FieldGrowthPolicy.AllNonFixedFieldsGrow)

        self.folder_label = QLabel("Game folder")
        self.folder = QLineEdit()
        self.folder.setPlaceholderText("folder the game will live in")
        self.folder.textEdited.connect(lambda _t: setattr(self, "_folder_touched", True))
        self.folder.textChanged.connect(self._folder_changed)
        self.folder.editingFinished.connect(self.detect)
        folder_field = QWidget()
        folder_layout = QVBoxLayout(folder_field)
        folder_layout.setContentsMargins(0, 0, 0, 0)
        folder_layout.setSpacing(4)
        folder_layout.addWidget(self._with_browse(self.folder, self._browse_folder))
        self.install_summary = _ElidedLabel()
        self.install_summary.setObjectName("Muted")
        self.install_summary.setAccessibleName("Install location")
        folder_layout.addWidget(self.install_summary)
        self.prefix_summary = _ElidedLabel()
        self.prefix_summary.setObjectName("Muted")
        self.prefix_summary.setAccessibleName("Wine prefix location")
        folder_layout.addWidget(self.prefix_summary)
        self.prefix_toggle = QPushButton("Choose the Wine prefix separately…")
        self.prefix_toggle.setObjectName("Link")
        self.prefix_toggle.setAutoDefault(False)
        self.prefix_toggle.clicked.connect(self._toggle_prefix)
        folder_layout.addWidget(self.prefix_toggle, alignment=Qt.AlignmentFlag.AlignLeft)
        form.addRow(self.folder_label, folder_field)
        self.folder_free = QLabel("")
        self.folder_free.setObjectName("Muted")
        form.addRow("", self.folder_free)

        self.prefix_row = QWidget()
        prefix_layout = QHBoxLayout(self.prefix_row)
        prefix_layout.setContentsMargins(0, 0, 0, 0)
        prefix_layout.setSpacing(8)
        self.prefix = QLineEdit()
        self.prefix.setPlaceholderText("Created automatically")
        self.prefix.textEdited.connect(lambda _t: setattr(self, "_prefix_touched", True))
        self.prefix.textChanged.connect(self._update_prefix_summary)
        self.prefix.editingFinished.connect(self.detect)
        prefix_layout.addWidget(self.prefix, 1)
        prefix_browse = QPushButton("Browse")
        prefix_browse.setObjectName("GhostButton")
        prefix_browse.setAutoDefault(False)
        prefix_browse.clicked.connect(self._browse_prefix)
        prefix_layout.addWidget(prefix_browse)
        form.addRow("Wine prefix", self.prefix_row)
        self.prefix_free = QLabel("")
        self.prefix_free.setObjectName("Muted")
        form.addRow("", self.prefix_free)
        # A prefix-only kind has no install-folder row to hang the usual summary on, and its
        # picked folder is only a library: this line says where the prefix will really go.
        self.prefix_only_summary = _ElidedLabel()
        self.prefix_only_summary.setObjectName("Muted")
        self.prefix_only_summary.setAccessibleName("Wine prefix location")
        form.addRow("", self.prefix_only_summary)

        self._location_open = False
        self._prefix_open = False
        self.location_toggle = QPushButton("Choose where to install…")
        self.location_toggle.setObjectName("GhostButton")
        self.location_toggle.setAutoDefault(False)
        self.location_toggle.clicked.connect(self._toggle_location)
        self._form = form
        form.addRow("", self.location_toggle)

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
        cancel.setAutoDefault(False)
        cancel.clicked.connect(self.reject)
        create = QPushButton("Add game")
        create.setObjectName("Primary")
        create.setMinimumHeight(40)
        create.setDefault(True)  # Enter in a field adds the game, not "Browse"
        create.clicked.connect(self.create_profile)
        self.create_button = create
        footer.addWidget(cancel)
        footer.addWidget(create)
        footer.setContentsMargins(26, 0, 26, 0)
        outer.addLayout(footer)

        self._select_row(self._first_row)
        self._on_game_changed(self._first_row)
        if advanced:
            self.game_list.setAccessibleName("Advanced game type")
        elif valid_kind:
            self.set_backend(valid_kind)
            # The store already chose the game; an unknown kind keeps the full list.
            self.game_list.hide()
        self._apply_location_visibility()
        self._fit_to_screen(body)

    def _fit_to_screen(self, body: QWidget) -> None:
        """Open tall enough to show the whole form, but never taller than the screen."""
        want = body.sizeHint().height() + 80  # footer buttons and margins
        screen = self.screen()
        limit = screen.availableGeometry().height() - 80 if screen is not None else want
        self.resize(max(self.minimumWidth(), self.sizeHint().width()), max(300, min(want, limit)))

    # ---- construction helpers ---------------------------------------------
    def _populate_games(self, *, advanced_only: bool = False) -> None:
        groups = (ADVANCED_GROUP,) if advanced_only else ("Games", ADVANCED_GROUP)
        for group in groups:
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
        browse.setAutoDefault(False)
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
        return next((g for g in _catalog() if g.kind == kind), None)

    def set_backend(self, kind: str) -> None:
        for row in range(self.game_list.count()):
            item = self.game_list.item(row)
            if item is not None and item.data(Qt.ItemDataRole.UserRole) == kind:
                self._select_row(row)
                return

    def set_mode(self, import_existing: bool) -> None:
        # The two radios are exclusive: unchecking one needs the other checked.
        (self.import_existing if import_existing else self.install_new).setChecked(True)

    def set_install_dir(self, path: str | Path) -> None:
        self.folder.setText(str(path))
        self._folder_touched = True
        self._folder_changed()

    def set_prefix(self, path: str | Path) -> None:
        self._prefix_touched = True
        self.prefix.setText(str(path))

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
        # Use the unique profile name, not the game title, so a second profile
        # of the same game gets its own install folder and prefix.
        return self._default_name(game.title if game else "game")

    def _root(self) -> Path:
        return self._data_root if self._data_root is not None else paths.profiles_dir()

    def _on_game_changed(self, _row: int) -> None:
        game = self._selected_game()
        if game is None:
            return
        self._prefix_open = False
        self.prefix.setPlaceholderText(
            "Folder of the existing install"
            if self.import_existing.isChecked()
            else "Created automatically"
        )
        if game.kind in PREFIX_KINDS:
            self.prefix_row.setVisible(True)
        else:
            self.prefix_row.setVisible(False)
        if hasattr(self, "_form"):
            self._apply_location_visibility()
        if not self._name_touched:
            self.name.setText(self._default_name(game.title))
        if not self._folder_touched and self.install_new.isChecked():
            self.folder.setText(str(self._root() / self._slug() / "install"))
        if not self._prefix_touched:
            # A default prefix only makes sense for a fresh install; in Import
            # mode an auto-filled, non-existent path must never be accepted.
            if game.kind in PREFIX_KINDS and self.install_new.isChecked():
                self.prefix.setText(self._matching_prefix(self.folder.text()))
            else:
                self.prefix.clear()
        self._update_size_hint()
        self.detect()

    def _toggle_location(self) -> None:
        self._location_open = not self._location_open
        self._apply_location_visibility()

    def _matching_prefix(self, folder: str | Path) -> str:
        """The Wine prefix that sits next to ``folder`` (the default one when it has no name)."""
        text = str(folder).strip()
        path = Path(text) if text else None
        if path is None or not path.name:
            return str(self._root() / self._slug() / "prefix")  # empty, "/" or "."
        return str(path.parent / ("prefix" if path.name == "install" else f"{path.name}-prefix"))

    def _folder_changed(self, _text: str = "") -> None:
        if not self._prefix_touched and self.install_new.isChecked():
            game = self._selected_game()
            if game is not None and game.kind in PREFIX_KINDS:
                self.prefix.setText(self._matching_prefix(self.folder.text()))
        self._update_prefix_summary()
        self._update_free_space()

    def _toggle_prefix(self) -> None:
        self._prefix_open = not self._prefix_open
        self._apply_location_visibility()

    def _apply_location_visibility(self) -> None:
        """A fresh install uses the default folders unless the user asks to choose.

        Folder and Wine-prefix fields are jargon for most people, and the defaults
        are right for them; an import must show them because it needs real paths.
        """
        importing = self.import_existing.isChecked()
        show = importing or self._location_open
        game = self._selected_game()
        uses_prefix = game is not None and game.kind in PREFIX_KINDS
        prefix_only = game is not None and game.kind in PREFIX_ONLY_KINDS
        form = self._form
        # A prefix-only kind has no separate game folder: the prefix *is* the
        # game location, so the install-folder row is never shown (not even for
        # an import, which asks for that location in the prefix row instead).
        form.setRowVisible(0, show and not prefix_only)  # install folder
        form.setRowVisible(1, show and not prefix_only)
        # A fresh install hides the Wine prefix row (its label, field and free-space line)
        # behind the "separately" link; an import and a prefix-only kind always show it.
        advanced = getattr(self, "_prefix_open", False)
        prefix_shown = show and uses_prefix and (prefix_only or importing or advanced)
        form.setRowVisible(2, prefix_shown)  # Wine prefix
        form.setRowVisible(3, prefix_shown)
        if hasattr(self, "prefix_toggle"):
            prefix_controls = show and uses_prefix and not prefix_only and not importing
            self.prefix_summary.setVisible(prefix_controls)
            self.prefix_only_summary.setVisible(
                show and uses_prefix and prefix_only and not importing
            )
            self.install_summary.setVisible(show and not prefix_only and not importing)
            self.prefix_toggle.setVisible(prefix_controls)
            self.prefix_toggle.setText(
                "Use the matching prefix folder"
                if self._prefix_open
                else "Choose the Wine prefix separately…"
            )
            self._update_prefix_summary()
        form.setRowVisible(self.location_toggle, not importing)
        self._apply_prefix_label(prefix_only)
        self.location_toggle.setText(
            "Use the default location" if self._location_open else "Choose where to install…"
        )

    def _apply_prefix_label(self, prefix_only: bool) -> None:
        """Call CZN's prefix what it is to users: the game folder."""
        label = self._form.labelForField(self.prefix_row)
        if label is None:
            return
        if prefix_only:
            label.setText("Game folder")
            for widget in (label, self.prefix, self.prefix_row):
                widget.setToolTip("Wine prefix")
        else:
            label.setText("Wine prefix")
            for widget in (label, self.prefix, self.prefix_row):
                widget.setToolTip("")

    def _on_mode_changed(self, _checked: bool = False) -> None:
        self._prefix_open = self.import_existing.isChecked()
        self._apply_location_visibility()
        if self.import_existing.isChecked():
            self.folder_label.setText("Existing game folder")
            self.prefix.setPlaceholderText("Folder of the existing install")
            if not self._folder_touched:
                self.folder.clear()
            # Drop an untouched default prefix so the import guard can tell a
            # real pick from the auto-filled path.
            if not self._prefix_touched:
                self.prefix.clear()
        else:
            self.folder_label.setText("Game folder")
            self.prefix.setPlaceholderText("Created automatically")
            if not self._folder_touched:
                self.folder.setText(str(self._root() / self._slug() / "install"))
            if not self._prefix_touched and self._selected_game() is not None:
                if self._selected_game().kind in PREFIX_KINDS:
                    self.prefix.setText(self._matching_prefix(self.folder.text()))
        self._update_size_hint()
        self._folder_changed()
        self.detect()

    def _update_prefix_summary(self) -> None:
        game = self._selected_game()
        fresh = game is not None and self.install_new.isChecked()
        if fresh:
            install_dir, prefix, _owned, _dirs = self._plan_paths(
                game, self.folder.text().strip(), self.prefix.text().strip(), False
            )
            if game.kind not in PREFIX_ONLY_KINDS:
                self.install_summary.set_full_text(f"Installs to: {install_dir}")
        else:
            prefix = self.prefix.text().strip()
        if fresh and game.kind in PREFIX_KINDS:
            line = f"Wine prefix: {prefix}"
            if game.kind in PREFIX_ONLY_KINDS:
                self.prefix_only_summary.set_full_text(line)
            else:
                self.prefix_summary.set_full_text(line)

    def _update_size_hint(self) -> None:
        """Explain that Install new downloads later, with the known download size."""
        game = self._selected_game()
        if game is None or not self.install_new.isChecked():
            self.size_hint.clear()
            self.size_hint.setVisible(False)
            return
        text = "Adds the game to your library. Press INSTALL on its page to download it"
        size = backends.install_size_hint(game.kind)
        if size:
            from ..core import diskspace

            text += f" (about {diskspace.human_size(size)})"
        self.size_hint.setText(text)
        self.size_hint.setVisible(True)

    def _browse_folder(self) -> None:
        start = self.folder.text().strip() or str(Path.home())
        chosen = QFileDialog.getExistingDirectory(self, "Select folder", start)
        if chosen:
            self.folder.setText(chosen)
            self._folder_touched = True
            self._folder_changed()
            self.detect()

    def _browse_prefix(self) -> None:
        start = self.prefix.text().strip() or str(Path.home())
        chosen = QFileDialog.getExistingDirectory(self, "Select Wine prefix", start)
        if chosen:
            self._prefix_touched = True
            self.prefix.setText(chosen)
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
            self.create_button.setEnabled(True)
            return {}
        info = detect_import(kind, self.folder.text().strip(), self.prefix.text().strip())
        notes = list(info.get("notes") or [])
        if info.get("install_dir"):
            notes.append(f"Game folder: {info['install_dir']}")
        if info.get("prefix"):
            notes.append(f"Prefix: {info['prefix']}")
        self.detect_label.setText("  ".join(notes) if notes else "Nothing detected yet.")
        # Never offer to import a folder where nothing was found: that profile
        # could not launch. CZN is fine because its prefix-only detection adds a note.
        found = bool(notes)
        self.create_button.setEnabled(found)
        self.create_button.setToolTip(
            "" if found else "Pick the folder where the game is already installed."
        )
        return info

    # ---- creation ----------------------------------------------------------
    def _plan_paths(
        self, game: GameType, install_dir: str, prefix_value: str, importing: bool
    ) -> tuple[str, str, list[str], list[str]]:
        """The install folder, prefix and ownership records the profile will really get.

        The picked folder is a games library (#560): the game gets
        ``<folder>/Nadare/<Game>/install`` with its prefix next to it, so nothing of
        Nadare's ever mixes with the user's own folders. A folder that already holds this
        game's exe (or, for a prefix-only kind, a Wine prefix) is kept as picked. Nadare
        may only delete folders it made, so it records which. The preview under the folder
        field uses this too, so what it shows is what ``create_profile`` stores.
        """
        kind = game.kind
        owned: list[str] = []
        owned_dirs: list[str] = []
        install_dir = _absolute(install_dir) if install_dir else ""
        prefix_value = _absolute(prefix_value) if prefix_value else ""
        if not importing and install_dir and kind not in PREFIX_ONLY_KINDS:
            chosen = Path(install_dir)
            default_place = not self._folder_touched or _inside(chosen, self._root())
            if not default_place and not _holds_game(game, chosen):
                game_dir = _free_folder(
                    chosen / "Nadare", _folder_name(game.title), self.config.profiles
                )
                owned_dirs.append(str(game_dir))
                if kind in PREFIX_KINDS:
                    chosen = game_dir / "install"
                    if not self._prefix_touched:
                        prefix_value = str(game_dir / "prefix")
                else:
                    chosen = game_dir
                install_dir = str(chosen)
            if _new_or_empty(chosen):
                owned.append("install_dir")
        if not importing and prefix_value and kind in PREFIX_ONLY_KINDS:
            chosen = Path(prefix_value)
            default_place = not self._prefix_touched or _inside(chosen, self._root())
            # A folder that already holds a prefix is the prefix, as for other kinds;
            # every other picked folder is a library and gets Nadare's own sub-tree.
            if not default_place and _drive_c_prefix(chosen) != chosen:
                game_dir = _free_folder(
                    chosen / "Nadare", _folder_name(game.title), self.config.profiles
                )
                owned_dirs.append(str(game_dir))
                prefix_value = str(game_dir / "prefix")
        if not importing and prefix_value and kind in PREFIX_KINDS:
            if _new_or_empty(Path(prefix_value)):
                owned.append("prefix")
        return install_dir, prefix_value, owned, owned_dirs

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
                "Use letters, numbers, spaces, - or _. A / or \\ is not allowed."
            )
            return None
        if name in self.config.profiles:
            self.error_label.setText("That name is already used. Choose another name.")
            return None

        folder = self.folder.text().strip()
        prefix = self.prefix.text().strip()
        importing = self.import_existing.isChecked()
        if importing and not folder and not prefix:
            self.error_label.setText("Pick the existing install folder (or its prefix).")
            return None

        info = detect_import(kind, folder, prefix) if importing else {}
        if importing:
            # Only a real finding may create a profile: a mistyped or empty
            # folder would otherwise leave the user with a game that cannot launch.
            if not info.get("notes"):
                if game.install_subdir:
                    self.error_label.setText(
                        f"No {game.title} installation was found there. Pick the folder "
                        f"that contains {game.exes[0] if game.exes else 'the game'} or its "
                        "Wine prefix."
                    )
                elif kind in PREFIX_ONLY_KINDS:
                    self.error_label.setText(
                        f"No {game.title} Wine prefix was found there. Pick the folder "
                        "that holds the existing prefix (it must contain drive_c)."
                    )
                else:
                    self.error_label.setText(
                        "That folder does not exist. Pick the folder you already use for "
                        "this game."
                    )
                return None
            install_dir = info.get("install_dir") or info.get("prefix") or ""
            prefix_value = info.get("prefix") or prefix or ""
        else:
            install_dir = folder
            prefix_value = prefix
        if kind in PREFIX_ONLY_KINDS:
            # The prefix is the whole install; never persist a duplicate path.
            install_dir = ""

        install_dir, prefix_value, owned, owned_dirs = self._plan_paths(
            game, install_dir, prefix_value, importing
        )

        if not importing and kind in PREFIX_ONLY_KINDS and prefix_value:
            chosen = Path(prefix_value)
            container = _drive_c_prefix(chosen)
            if container is not None and container != chosen:
                self.error_label.setText(
                    "That folder is inside another Wine prefix. Pick a folder outside it."
                )
                return None

        if not importing:
            targets = []
            if kind not in PREFIX_ONLY_KINDS and install_dir:
                targets.append(("game folder", install_dir))
            if prefix_value and kind in PREFIX_KINDS:
                targets.append(("Wine prefix", prefix_value))
            for label, target in targets:
                if _cannot_create(target):
                    self.error_label.setText(
                        f"That {label} is not writable: {target}. "
                        "Pick a folder you can write to."
                    )
                    return None

        values: dict[str, Any] = {"backend": kind, "title": game.title}
        if install_dir and kind not in PREFIX_ONLY_KINDS:
            values["install_dir"] = install_dir
        if prefix_value and kind in PREFIX_KINDS:
            values["prefix"] = prefix_value
        if owned:
            values["owned_trees"] = owned
        if owned_dirs:
            values["owned_dirs"] = owned_dirs
        if owned:
            # Bind each record to the folder it was made for: a path edited later is not owned.
            made = {"install_dir": install_dir, "prefix": prefix_value}
            values["owned_paths"] = {
                key: str(Path(made[key]).expanduser().resolve()) for key in owned
            }

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
        self.created_by_import = importing
        self.created.emit(name)
        self.accept()
        return name


__all__ = [
    "AddGameDialog",
    "CATALOG",  # noqa: F822 - provided lazily by __getattr__
    "GameType",
    "PREFIX_KINDS",
    "PREFIX_ONLY_KINDS",
    "available_catalog",
    "detect_import",
]
