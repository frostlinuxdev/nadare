# SPDX-License-Identifier: GPL-3.0-or-later
# Copyright (C) 2026 Nadare contributors
"""The "Add game" wizard: pick a backend and create a new profile.

The dialog only reads the folders the user points it at; it never creates,
moves or deletes anything inside them. Saving a profile writes the config, and
the caller decides what to do next (open the game page or start the install).
"""

from __future__ import annotations

import copy
import os
from collections.abc import Mapping
from dataclasses import dataclass, replace
from pathlib import Path
from typing import Any

from PyQt6.QtCore import Qt, QThreadPool, pyqtSignal
from PyQt6.QtWidgets import (
    QDialog,
    QFileDialog,
    QFormLayout,
    QHBoxLayout,
    QLabel,
    QLineEdit,
    QListWidget,
    QListWidgetItem,
    QPushButton,
    QSizePolicy,
    QVBoxLayout,
    QWidget,
)

from ..core import backends, flatpakfs, libraries, paths, prefixguard, relink
from ..core.backends import BackendError
from ..core.config import MAX_PROFILE_NAME, Config, valid_profile_name
from ..core.directkinds import (
    CATALOG_KINDS,
    DIRECT_WIZARD,
    HELD_BACK_KINDS,
    HOYO_KINDS,
    LAUNCHER_KINDS,
    LAUNCHER_WIZARD,
    TIER_HELP,
    hoyo_visible,
    tier_of,
)
from ..core.paths import inside as _inside
from ..core.profilenames import short_name
from ..core.shortcuts import command_hint
from .dialog_layout import fit_dialog, make_scroll_body
from .theme import apply_theme
from .workers import CallTask


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
    #: Longer name for the wizard list, when the profile title should stay short.
    display: str = ""


_CATALOG: tuple[GameType, ...] = (
    GameType("Chaos Zero Nightmare", "stove-czn"),
    GameType(
        "Fate/Grand Order",
        "fgo-lepton",
        display="Fate/Grand Order (NA)",
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
    GameType(
        "Heaven Burns Red",
        "hbr-yostar",
        install_subdir="HeavenBurnsRed_EN",
        exes=("HeavenBurnsRed.exe",),
        badge="Experimental",
        badge_help=TIER_HELP["Experimental"],
    ),
    GameType(
        "Azur Promilia (JP)",
        "azurpromilia-jp-yostar",
        install_subdir="AZUPRO_JP",
        exes=("AzurPromiliaJP.exe",),
        badge="Experimental",
        badge_help=TIER_HELP["Experimental"],
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
    {
        "arknights-yostar",
        "stellasora-yostar",
        "hbr-yostar",
        "azurpromilia-jp-yostar",
        "stove-czn",
        *CATALOG_KINDS,
    }
)

#: Prefix kinds whose whole install lives inside the Wine prefix (no separate
#: game folder), so a picked "install folder" is really the prefix.
PREFIX_ONLY_KINDS = frozenset({"stove-czn", "fgo-lepton"})

#: Kinds whose "prefix" is a host lepton install (``lepton-x86_64/lepton-fgo``),
#: not a Wine prefix.
LEPTON_KINDS = frozenset({"fgo-lepton"})


def available_catalog() -> tuple[GameType, ...]:
    """The catalog minus kinds this build could not load (e.g. no FGO), held-back and HoYo games."""
    missing = set(backends.unavailable()) | HELD_BACK_KINDS
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


def game_spec(game: GameType) -> libraries.GameSpec:
    """How ``game`` looks on disk, for finding it in a library."""
    return libraries.GameSpec(
        kind=game.kind,
        title=game.title,
        exes=game.exes,
        install_root=game.install_root,
        install_subdir=game.install_subdir,
    )


def game_specs() -> list[libraries.GameSpec]:
    """Every catalog game, for Find games."""
    return [game_spec(game) for game in _catalog()]


def _folder_name(title: str) -> str:
    """``title`` as a folder name: the one rule Install and Move share (#1229)."""
    return libraries.game_folder_name(title)


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


def unique_context(profiles: Mapping[str, Mapping[str, Any]]) -> str | None:
    """A lepton context no other FGO profile uses, or ``None`` when the default is free.

    Profiles with no ``context`` all mean the one called ``fgo``, so a second copy would
    control the same Android context as the first (#921).
    """
    used = {
        str(p.get("context") or "fgo")
        for p in profiles.values()
        if str(p.get("backend") or "") in LEPTON_KINDS
    }
    if "fgo" not in used:
        return None
    number = 2
    while f"fgo-{number}" in used:
        number += 1
    return f"fgo-{number}"


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


class _ErrorLabel(QLabel):
    """Shown only while it has something to say (it sits above the buttons, outside the form)."""

    def __init__(self, text: str = "") -> None:
        super().__init__(text)
        self.setVisible(bool(text))

    def setText(self, text: str) -> None:  # noqa: N802 - Qt naming
        super().setText(text)
        self.setVisible(bool(text))


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
    ) -> None:
        super().__init__(parent)
        self.setObjectName("Root")
        apply_theme(self)
        self.config = config
        self.created_profile = ""
        self._data_root = data_root
        self._name_touched = False
        self._folder_touched = False
        self.reused: libraries.Candidate | None = None  # a library folder _plan_paths reuses
        self._prefix_touched = False
        #: Bumped per background detection / free-space read so a stale result cannot
        #: overwrite the hints. Separate counters: typing must not cancel a reuse check.
        self._detect_generation = 0
        #: Reusable copies the background detection found, by folder (#1207 b150).
        self._reuse_cache: dict[str, Any] = {}
        self._free_generation = 0

        self.setWindowTitle("Add game")
        self.setMinimumWidth(560)
        self._shown_once = False

        # The form scrolls on a small screen; the heading and buttons stay put.
        outer = QVBoxLayout(self)
        outer.setContentsMargins(22, 20, 22, 20)
        outer.setSpacing(14)

        heading = QLabel("Add game")
        heading.setObjectName("Title")
        heading.setWordWrap(True)
        outer.addWidget(heading)

        self._scroll, _body, root = make_scroll_body(spacing=14)
        outer.addWidget(self._scroll, 1)

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
        self._populate_games()
        self.game_list.currentRowChanged.connect(self._on_game_changed)
        root.addWidget(self.game_list)

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
        self.folder.editingFinished.connect(self._detect_async)
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
        self.folder_free.setWordWrap(True)
        form.addRow("", self.folder_free)

        self.prefix_row = QWidget()
        prefix_layout = QHBoxLayout(self.prefix_row)
        prefix_layout.setContentsMargins(0, 0, 0, 0)
        prefix_layout.setSpacing(8)
        self.prefix = QLineEdit()
        self.prefix.setPlaceholderText("Created automatically")
        self.prefix.textEdited.connect(lambda _t: setattr(self, "_prefix_touched", True))
        self.prefix.textChanged.connect(self._update_prefix_summary)
        self.prefix.editingFinished.connect(self._detect_async)
        prefix_layout.addWidget(self.prefix, 1)
        prefix_browse = QPushButton("Browse")
        prefix_browse.setObjectName("GhostButton")
        prefix_browse.setAutoDefault(False)
        prefix_browse.clicked.connect(self._browse_prefix)
        prefix_layout.addWidget(prefix_browse)
        form.addRow("Wine prefix", self.prefix_row)
        self.prefix_free = QLabel("")
        self.prefix_free.setObjectName("Muted")
        self.prefix_free.setWordWrap(True)
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
        self.name.setPlaceholderText("name for this game")
        self.name.textEdited.connect(lambda _t: setattr(self, "_name_touched", True))
        form.addRow("Game name", self.name)
        root.addLayout(form)

        self.error_label = _ErrorLabel("")
        self.error_label.setObjectName("Error")
        self.error_label.setWordWrap(True)
        # Above the buttons, outside the scrolling form: on a short screen the form's end is
        # out of view, and a refused Add game looked like it did nothing.
        outer.addWidget(self.error_label)

        footer = QHBoxLayout()
        footer.addStretch(1)
        cancel = QPushButton("Cancel")
        cancel.setObjectName("GhostButton")
        cancel.setAutoDefault(False)
        cancel.clicked.connect(self.reject)
        create = QPushButton("Add game")
        create.setObjectName("Primary")
        create.setMinimumHeight(40)
        # Enter in a text field only finishes typing: it used to add the (preselected) game
        # before the user had looked at the rest. No button here is a default, so Enter
        # cannot press "Browse" either; the buttons work with a click, or Space/Enter on focus.
        create.setAutoDefault(False)
        create.clicked.connect(self.create_profile)
        self.create_button = create
        footer.addWidget(cancel)
        footer.addWidget(create)
        outer.addLayout(footer)
        for button in self.findChildren(QPushButton):
            button.setAutoDefault(False)

        self._select_row(self._first_row)
        self._on_game_changed(self._first_row)
        if valid_kind:
            self.set_backend(valid_kind)
            # The store already chose the game; an unknown kind keeps the full list.
            self.game_list.hide()
        self._apply_location_visibility()
        self._fit_height()

    def showEvent(self, event) -> None:  # noqa: N802 - Qt naming
        super().showEvent(event)
        self._fit_height(keep_width=self._shown_once)
        self._shown_once = True

    def _fit_height(self, *, keep_width: bool = False) -> None:
        """Open tall enough to show the whole form, but never taller than the screen."""
        fit_dialog(self, min_width=560, scroll=self._scroll, keep_width=keep_width)

    # ---- construction helpers ---------------------------------------------
    def _populate_games(self) -> None:
        header = QListWidgetItem("GAMES")
        header.setFlags(Qt.ItemFlag.NoItemFlags)
        self.game_list.addItem(header)
        for game in available_catalog():
            shown = game.display or game.title
            label = f"{shown}  ·  {game.badge}" if game.badge else shown
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

    def set_install_dir(self, path: str | Path) -> None:
        self.folder.setText(str(path))
        self._folder_touched = True
        self._folder_changed()
        self._update_free_space()
        self._detect_async()  # the reuse hint is looked up off the GUI thread (#1207 b150)

    def use_default_location(self) -> None:
        """Back to the default library: the folder and prefix Nadare picks itself."""
        self._folder_touched = False
        self._prefix_touched = False
        self._on_game_changed(self.game_list.currentRow())

    def set_prefix(self, path: str | Path) -> None:
        self._prefix_touched = True
        self.prefix.setText(str(path))
        self._update_free_space()

    def set_profile_name(self, name: str) -> None:
        self.name.setText(name)
        self._name_touched = True
        game = self._selected_game()
        # A chosen profile key names the default-library game folder (one rule with
        # libraries._game_folder): <root>/<key>/install and <root>/<key>/prefix.
        if not self._folder_touched:
            self.folder.setText(str(self._root() / self._slug() / "install"))
        if not self._prefix_touched and game is not None and game.kind in PREFIX_KINDS:
            self.prefix.setText(str(self._root() / self._slug() / "prefix"))

    # ---- defaults / detection ---------------------------------------------
    def _default_name(self, title: str, kind: str = "") -> str:
        # A game kind's written-down short name (#1134), else the slug of its title.
        base = _slugify(short_name(kind) or title) or "game"
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
        current = self.name.text().strip()
        if current:
            # Use the profile key, not the game title, so a second profile of
            # the same game gets its own install folder and prefix.
            return current
        game = self._selected_game()
        return self._default_name(game.title if game else "game", game.kind if game else "")

    def _root(self) -> Path:
        return self._data_root if self._data_root is not None else paths.profiles_dir()

    def _on_game_changed(self, _row: int) -> None:
        game = self._selected_game()
        if game is None:
            return
        self._prefix_open = False
        self.prefix.setPlaceholderText("Created automatically")
        if game.kind in PREFIX_KINDS:
            self.prefix_row.setVisible(True)
        else:
            self.prefix_row.setVisible(False)
        if hasattr(self, "_form"):
            self._apply_location_visibility()
        if not self._name_touched:
            self.name.setText(self._default_name(game.title, game.kind))
        if not self._folder_touched:
            self.folder.setText(str(self._root() / self._slug() / "install"))
        if not self._prefix_touched:
            if game.kind in PREFIX_KINDS:
                self.prefix.setText(self._matching_prefix(self.folder.text()))
            else:
                self.prefix.clear()
        self._update_size_hint()
        self._detect_async()

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
        if not self._prefix_touched:
            game = self._selected_game()
            if game is not None and game.kind in PREFIX_KINDS:
                self.prefix.setText(self._matching_prefix(self.folder.text()))
        self._update_prefix_summary()
        self._update_free_space_async()

    def _toggle_prefix(self) -> None:
        self._prefix_open = not self._prefix_open
        self._apply_location_visibility()

    def _apply_location_visibility(self) -> None:
        """A fresh install uses the default folders unless the user asks to choose.

        Folder and Wine-prefix fields stay behind the location controls.
        """
        show = self._location_open
        game = self._selected_game()
        uses_prefix = game is not None and (
            game.kind in PREFIX_KINDS or game.kind in LEPTON_KINDS
        )
        prefix_only = game is not None and game.kind in PREFIX_ONLY_KINDS
        form = self._form
        # A prefix-only kind has no separate game folder: the prefix *is* the
        # game location, so the install-folder row is never shown.
        form.setRowVisible(0, show and not prefix_only)  # install folder
        form.setRowVisible(1, show and not prefix_only)
        # A fresh install hides the Wine prefix row (its label, field and free-space line)
        # behind the "separately" link; a prefix-only kind always shows it.
        advanced = getattr(self, "_prefix_open", False)
        editable_prefix = game is not None and not game.kind.startswith("direct-")
        prefix_shown = show and uses_prefix and editable_prefix and (prefix_only or advanced)
        form.setRowVisible(2, prefix_shown)  # Wine prefix
        form.setRowVisible(3, prefix_shown)
        if hasattr(self, "prefix_toggle"):
            prefix_controls = show and uses_prefix and editable_prefix and not prefix_only
            self.prefix_summary.setVisible(prefix_controls)
            self.prefix_only_summary.setVisible(
                show and uses_prefix and prefix_only and game.kind not in LEPTON_KINDS
            )
            self.install_summary.setVisible(show and not prefix_only)
            self.prefix_toggle.setVisible(prefix_controls)
            self.prefix_toggle.setText(
                "Use the matching prefix folder"
                if self._prefix_open
                else "Choose the Wine prefix separately…"
            )
            self._update_prefix_summary()
        form.setRowVisible(self.location_toggle, True)
        self._apply_prefix_label(prefix_only)
        self.location_toggle.setText(
            "Use the default location" if self._location_open else "Choose where to install…"
        )

    def _apply_prefix_label(self, prefix_only: bool) -> None:
        """Call CZN's prefix what it is to users: the game folder."""
        game = self._selected_game()
        label = self._form.labelForField(self.prefix_row)
        if label is None:
            return
        if game is not None and game.kind in LEPTON_KINDS:
            label.setText("lepton-fgo install folder")
            for widget in (label, self.prefix, self.prefix_row):
                widget.setToolTip(
                    "The folder that holds lepton-x86_64/lepton-fgo "
                    "(usually ~/.local/share/lepton-nv)."
                )
        elif prefix_only:
            label.setText("Game folder")
            for widget in (label, self.prefix, self.prefix_row):
                widget.setToolTip("Wine prefix")
        else:
            label.setText("Wine prefix")
            for widget in (label, self.prefix, self.prefix_row):
                widget.setToolTip("")

    def _update_prefix_summary(self) -> None:
        game = self._selected_game()
        fresh = game is not None
        if fresh:
            install_dir, prefix, _owned, _dirs = self._plan_paths(
                game,
                self.folder.text().strip(),
                self.prefix.text().strip(),
                cached_reuse_only=True,
            )
            if game.kind not in PREFIX_ONLY_KINDS:
                text = f"Installs to: {install_dir}"
                if self.reused is not None:
                    text += f" (uses the {game.title} files already there)"
                self.install_summary.set_full_text(text)
        else:
            prefix = self.prefix.text().strip()
        if fresh and game.kind in PREFIX_KINDS:
            line = f"Wine prefix: {prefix}"
            if game.kind in PREFIX_ONLY_KINDS:
                if self.reused is not None:
                    line += f" (uses the {game.title} files already there)"
                self.prefix_only_summary.set_full_text(line)
            else:
                self.prefix_summary.set_full_text(line)

    def _update_size_hint(self) -> None:
        """Explain that Install new downloads later, with the known download size."""
        game = self._selected_game()
        if game is None:
            self.size_hint.clear()
            self.size_hint.setVisible(False)
            return
        text = "Adds the game to your library. Press INSTALL on its page to download it"
        size = backends.download_size_hint(game.kind)
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
            self._detect_async()

    def _browse_prefix(self) -> None:
        start = self.prefix.text().strip() or str(Path.home())
        chosen = QFileDialog.getExistingDirectory(self, "Select Wine prefix", start)
        if chosen:
            self._prefix_touched = True
            self.prefix.setText(chosen)
            self._detect_async()

    def _free_hint(self, path: str) -> str:
        text = path.strip()
        if not text:
            return ""
        try:
            from nadare.core import diskspace, execmount

            free = diskspace.human_size(diskspace.free_bytes(text))
            mount = execmount.noexec_mount(text)
            if mount is not None:  # #1861: games stop at once with "exit code 5" there
                return f"{free} free on {mount}, but it is mounted noexec: games can't run there"
            return f"{free} free on {diskspace.mount_point(text)}"
        except (OSError, BackendError):
            return ""

    def _update_free_space(self) -> None:
        self.folder_free.setText(self._free_hint(self.folder.text()))
        self.prefix_free.setText(self._free_hint(self.prefix.text()))

    def _update_free_space_async(self) -> None:
        """Read ``statvfs`` off the GUI thread; a hung mount must not freeze the dialog."""
        self._free_generation += 1
        generation = self._free_generation
        folder = self.folder.text().strip()
        prefix = self.prefix.text().strip()

        def run() -> tuple:
            return folder, prefix, self._free_hint(folder), self._free_hint(prefix)

        task = CallTask(run)
        task.signals.done.connect(
            lambda result: self._apply_free_space(generation, result)
        )
        QThreadPool.globalInstance().start(task)

    def _apply_free_space(self, generation: int, result: tuple) -> None:
        folder, prefix, folder_hint, prefix_hint = result
        if generation != self._free_generation:
            return
        try:
            current = (self.folder.text().strip(), self.prefix.text().strip())
        except RuntimeError:
            return  # the dialog was closed before the worker answered
        if current != (folder, prefix):
            return
        self.folder_free.setText(folder_hint)
        self.prefix_free.setText(prefix_hint)

    def _detect_async(self) -> None:
        """Check reusable library folders and free space on a worker thread (#102)."""
        self._detect_generation += 1
        generation = self._detect_generation
        folder = self.folder.text().strip()
        prefix = self.prefix.text().strip()
        game = self._selected_game()
        candidates = (
            self._reuse_candidates(game, folder, prefix)
            if game is not None
            else []
        )
        config, spec = self.config, game_spec(game) if game is not None else None

        def run() -> tuple:
            hints = (self._free_hint(folder), self._free_hint(prefix))
            reuse = {}
            for candidate in candidates:
                try:
                    reuse[str(candidate)] = libraries.reusable_folder(config, candidate, spec)
                except Exception:  # noqa: BLE001 - a hint only; Add looks again itself
                    reuse[str(candidate)] = None
            return folder, prefix, hints, reuse

        task = CallTask(run)
        task.signals.done.connect(
            lambda result: self._apply_detection(generation, result)
        )
        QThreadPool.globalInstance().start(task)

    def _apply_detection(self, generation: int, result: tuple) -> None:
        folder, prefix, hints, reuse = result
        if generation != self._detect_generation:
            return
        try:
            current = (self.folder.text().strip(), self.prefix.text().strip())
        except RuntimeError:
            return  # the dialog was closed before the worker answered
        if current != (folder, prefix):
            return
        self.folder_free.setText(hints[0])
        self.prefix_free.setText(hints[1])
        if reuse:
            self._reuse_cache.update(reuse)
            self._update_prefix_summary()

    # ---- creation ----------------------------------------------------------
    def _reusable(self, game: GameType, folder: Path, *, cached_only: bool):
        """A copy of ``game`` already in ``folder`` (Steam-style reuse), or ``None``.

        The live preview passes ``cached_only``: looking into a library can be slow (a network
        drive), so it only reads what the background detection found (#1207 b150).
        """
        if cached_only:
            return self._reuse_cache.get(str(folder))
        return libraries.reusable_folder(self.config, folder, game_spec(game))

    def _reuse_candidates(self, game: GameType, folder: str, prefix: str) -> list[Path]:
        """The folders _plan_paths would look into for a reusable copy (cheap, no disk reads)."""
        picked = prefix if game.kind in PREFIX_ONLY_KINDS else folder
        if not picked:
            return []
        library = libraries.library_of(Path(_absolute(picked)))
        return [library / "Nadare" / _folder_name(game.title)]

    def _plan_paths(
        self,
        game: GameType,
        install_dir: str,
        prefix_value: str,
        *,
        cached_reuse_only: bool = False,
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
        self.reused = None
        install_dir = _absolute(install_dir) if install_dir else ""
        prefix_value = _absolute(prefix_value) if prefix_value else ""
        if install_dir and kind not in PREFIX_ONLY_KINDS:
            chosen = Path(install_dir)
            library = libraries.library_of(chosen)
            default_place = not self._folder_touched or _inside(chosen, self._root())
            if not default_place and not _holds_game(game, chosen):
                # This game's folder may already be in the library (removed from Nadare with
                # its files kept, or a lost manifest): use it, as Steam does, instead of
                # downloading everything again into "<Game> 2" (#libraries step 4).
                reuse = self._reusable(
                    game,
                    library / "Nadare" / _folder_name(game.title),
                    cached_only=cached_reuse_only,
                )
                if reuse is not None:
                    game_dir = reuse.game_dir
                else:
                    game_dir = relink.free_folder(
                        library / "Nadare", _folder_name(game.title), self.config.profiles
                    )
                self.reused = reuse
                owned_dirs.append(str(game_dir))
                if kind in PREFIX_KINDS:
                    chosen = (reuse.install_dir if reuse else None) or game_dir / "install"
                    if not self._prefix_touched:
                        prefix_value = str((reuse.prefix if reuse else None) or game_dir / "prefix")
                else:
                    chosen = (reuse.install_dir if reuse else None) or game_dir
                install_dir = str(chosen)
            if _new_or_empty(chosen):
                owned.append("install_dir")
        # Lepton uses its own existing host installation, not a Wine library layout.
        if prefix_value and kind in PREFIX_ONLY_KINDS and kind not in LEPTON_KINDS:
            chosen = Path(prefix_value)
            default_place = not self._prefix_touched or _inside(chosen, self._root())
            # A folder that already holds a prefix is the prefix, as for other kinds;
            # every other picked folder is a library and gets Nadare's own sub-tree.
            if not default_place and _drive_c_prefix(chosen) != chosen:
                library = libraries.library_of(chosen)
                # Reuse this game's folder when it is already in the library, as for the other
                # kinds, instead of "<Game> 2" and a whole new download (round 4).
                reuse = self._reusable(
                    game,
                    library / "Nadare" / _folder_name(game.title),
                    cached_only=cached_reuse_only,
                )
                if reuse is not None and reuse.prefix is not None:
                    game_dir = reuse.game_dir
                    prefix_value = str(reuse.prefix)
                    self.reused = reuse
                else:
                    game_dir = relink.free_folder(
                        library / "Nadare", _folder_name(game.title), self.config.profiles
                    )
                    prefix_value = str(game_dir / "prefix")
                owned_dirs.append(str(game_dir))
        if (
            prefix_value
            and kind in PREFIX_KINDS
            and kind not in PREFIX_ONLY_KINDS
            and self._prefix_touched
        ):
            # A Wine prefix picked on its own is not a library, but a drive root, the home
            # folder or a personal folder picked as one would get drive_c and friends dropped in
            # it: give such a place Nadare's own sub-tree, as a picked game folder gets (#851).
            reason = prefixguard.unsafe_reason(prefix_value, as_prefix=True)
            if reason:
                game_dir = relink.free_folder(
                    Path(prefix_value) / "Nadare", _folder_name(game.title), self.config.profiles
                )
                owned_dirs.append(str(game_dir))
                prefix_value = str(game_dir / "prefix")
        if prefix_value and kind in PREFIX_KINDS:
            if _new_or_empty(Path(prefix_value)):
                owned.append("prefix")
        if self.reused is not None:
            # A reused folder is Nadare's only where its manifest says Nadare made it.
            # Only for the folder the profile really uses: a prefix the user picked instead of
            # the manifest's stays theirs (Remove must never offer to delete it as Nadare's).
            made = {"install": "install_dir", "prefix": "prefix"}
            used = {"install_dir": install_dir, "prefix": prefix_value}
            for field in self.reused.made_by_nadare:
                key = made[field]
                folder = self.reused.install_dir if field == "install" else self.reused.prefix
                value = used.get(key)
                if folder is None or not value or Path(value).expanduser().resolve() != folder:
                    continue
                if key not in owned:
                    owned.append(key)
        return install_dir, prefix_value, owned, owned_dirs

    def create_profile(self) -> str | None:
        """Validate, write the profile to the config, and accept on success."""
        kind = self.selected_kind()
        game = self._selected_game()
        if not kind or game is None:
            self.error_label.setText("Pick a game first.")
            return None
        existing = backends.existing_profile(kind, self.config.profiles)
        if existing:
            data = self.config.profiles.get(existing) or {}
            existing_title = data.get("title") or existing
            self.error_label.setText(
                f'{game.title} is already in your library ("{existing_title}"). '
                "Open it from the Library; one copy per game keeps its sign-in, saves and "
                "files apart."
            )
            return None
        name = self.name.text().strip() or self._default_name(game.title, game.kind)
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
        install_dir = folder
        prefix_value = prefix
        if kind in PREFIX_ONLY_KINDS:
            # The prefix is the whole install; never persist a duplicate path.
            install_dir = ""
        if kind in LAUNCHER_KINDS and not self._folder_touched:
            # Launchers install inside the prefix; a default folder would be ignored.
            install_dir = ""

        install_dir, prefix_value, owned, owned_dirs = self._plan_paths(
            game, install_dir, prefix_value
        )
        for label, value in (("game folder", install_dir), ("Wine prefix", prefix_value)):
            reason = prefixguard.unsafe_reason(value, profiles=self.config.profiles)
            if reason:
                self.error_label.setText(f"That {label}: {prefixguard.explain(value, reason)}")
                return None
            out_of_reach = flatpakfs.unreachable_reason(value) if value else None
            if out_of_reach:
                self.error_label.setText(out_of_reach)
                return None

        if kind in PREFIX_ONLY_KINDS and prefix_value:
            chosen = Path(prefix_value)
            container = _drive_c_prefix(chosen)
            if container is not None and container != chosen:
                self.error_label.setText(
                    "That folder is inside another Wine prefix. Pick a folder outside it."
                )
                return None

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
        if prefix_value and (kind in PREFIX_KINDS or kind in LEPTON_KINDS):
            values["prefix"] = prefix_value
        if kind in LEPTON_KINDS and (context := unique_context(self.config.profiles)):
            values["context"] = context
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
            libraries_before = copy.deepcopy(self.config.data.get(libraries.CONFIG_KEY))
            for game_dir in owned_dirs:
                # A picked folder is a library (<folder>/Nadare/<Game>): remember it, as Steam
                # does when a library is made in its install dialog. An unsafe or known one is
                # simply not added.
                library = libraries.library_root_for(Path(game_dir))
                if library is not None:
                    try:
                        libraries.add_library(self.config, library)
                    except libraries.LibraryError:
                        pass
            try:
                self.config.save()
            except OSError as exc:
                if libraries_before is None:
                    self.config.data.pop(libraries.CONFIG_KEY, None)
                else:
                    self.config.data[libraries.CONFIG_KEY] = libraries_before
                self.config.remove_profile(name)
                self.error_label.setText(f"Could not save config: {exc}")
                return None
        # A small manifest lets a library added back find this game again (Steam-like).
        libraries.record_new_game(self.config.profiles[name], name, default_root=self._root())
        # Install creates the prefix later, never here.
        self.created_profile = name
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
]
