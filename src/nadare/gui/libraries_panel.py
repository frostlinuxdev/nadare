# SPDX-License-Identifier: GPL-3.0-or-later
"""The Libraries card: the folders Nadare installs games into, the way Steam lists them.

Add and Remove act at once (like Steam's storage manager), not on the Settings Save button.
Removing a library only forgets it; its games and files stay. Adding a library that already
holds Nadare games, or pressing Find games, offers to bring those games back.
"""

from __future__ import annotations

import copy
import logging
from collections.abc import Callable
from dataclasses import dataclass
from pathlib import Path

from PyQt6.QtCore import QThreadPool, pyqtSignal
from PyQt6.QtWidgets import (
    QCheckBox,
    QDialog,
    QDialogButtonBox,
    QFileDialog,
    QHBoxLayout,
    QLabel,
    QPushButton,
    QSizePolicy,
    QVBoxLayout,
    QWidget,
)

from ..core import backends, diskspace, libraries
from ..core.config import Config
from .add_game import game_specs
from .workers import CallTask

log = logging.getLogger(__name__)


@dataclass
class LibraryRow:
    path: Path
    is_default: bool
    label: QLabel
    remove_button: QPushButton | None


def _free_text(path: Path, *, must_exist: bool = True) -> str:
    if must_exist and not path.is_dir():
        # free_bytes() climbs to the nearest existing folder: an unplugged drive would show
        # the free space of the disk it is mounted on. The default library is made on demand.
        return "folder not found (drive unplugged?)"
    try:
        return f"{diskspace.human_size(diskspace.free_bytes(path))} free"
    except (OSError, backends.BackendError):
        return "free space unknown"


@dataclass
class _Search:
    found: list[libraries.Candidate]
    guessed: list[libraries.Candidate]
    notes: list[str]


def _look_for_games(config: Config, roots: list[Path] | None) -> _Search:
    """Scan ``roots`` (all libraries by default). Disk work only: safe off the GUI thread."""
    found: list[libraries.Candidate] = []
    notes: list[str] = []
    every = roots or libraries.library_roots(config)
    for root in every:
        more, why = libraries.scan(config, root)
        found += more
        notes += why
    guessed: list[libraries.Candidate] = []
    specs = game_specs()
    for root in every:
        for guess in libraries.find_unmarked(config, root, specs):
            if not any(c.kind == guess.kind for c in found + guessed):
                guessed.append(guess)
    return _Search(found, guessed, notes)


class LibrariesPanel(QWidget):
    """The list of libraries with Add library, Remove and Find games."""

    #: Names of the games brought back from a library (their config keys).
    games_added = pyqtSignal(list)

    def __init__(
        self,
        parent: QWidget | None = None,
        *,
        choose: Callable[[list[tuple[str, bool]]], list[bool] | None] | None = None,
    ) -> None:
        super().__init__(parent)
        self._config: Config | None = None
        self._choose = choose or self._choose_box
        self._rows: list[LibraryRow] = []
        self._searching = False
        self._queued: list[Path] | None = None  # libraries added while a scan ran

        layout = QVBoxLayout(self)
        layout.setContentsMargins(0, 0, 0, 0)
        layout.setSpacing(8)
        self._list = QVBoxLayout()
        self._list.setSpacing(6)
        layout.addLayout(self._list)

        buttons = QHBoxLayout()
        self.add_button = QPushButton("Add library…")
        self.add_button.setObjectName("GhostButton")
        self.add_button.setToolTip(
            "Pick a folder for games, for example on another drive. It may hold other "
            "files: Nadare puts games in a Nadare folder inside it."
        )
        self.add_button.clicked.connect(self._pick_folder)
        self.find_button = QPushButton("Find games")
        self.find_button.setObjectName("GhostButton")
        self.find_button.setToolTip(
            "Look in every library for games Nadare installed before that are not in your "
            "Library, for example after removing a game but keeping its files."
        )
        self.find_button.clicked.connect(lambda: self.find_games_later())
        for button in (self.add_button, self.find_button):
            button.setSizePolicy(QSizePolicy.Policy.Maximum, QSizePolicy.Policy.Fixed)
            buttons.addWidget(button)
        buttons.addStretch(1)
        layout.addLayout(buttons)

        self.message = QLabel("")
        self.message.setObjectName("Muted")
        self.message.setWordWrap(True)
        self.message.setVisible(False)
        layout.addWidget(self.message)

    # ---- state ----------------------------------------------------------------------------
    def load(self, config: Config) -> None:
        self._config = config
        self._rebuild()

    def rows(self) -> list[LibraryRow]:
        return list(self._rows)

    def _rebuild(self) -> None:
        while self._list.count():
            item = self._list.takeAt(0)
            widget = item.widget()
            if widget is not None:
                widget.deleteLater()
        self._rows = []
        if self._config is None:
            return
        default = libraries.default_root()
        for root in libraries.library_roots(self._config):
            is_default = root == default
            line = QWidget()
            row = QHBoxLayout(line)
            row.setContentsMargins(0, 0, 0, 0)
            name = "Default library" if is_default else libraries.display_path(root)
            label = QLabel(f"{name}  ·  {_free_text(root, must_exist=not is_default)}")
            label.setToolTip(str(root))
            label.setWordWrap(True)
            row.addWidget(label, 1)
            remove = None
            if not is_default:
                remove = QPushButton("Remove")
                remove.setObjectName("GhostButton")
                remove.setToolTip("Forget this library. Its games and files stay where they are.")
                remove.clicked.connect(lambda _c=False, r=root: self.remove(r))
                row.addWidget(remove)
            self._list.addWidget(line)
            self._rows.append(LibraryRow(root, is_default, label, remove))

    def _say(self, text: str) -> None:
        self.message.setText(text)
        self.message.setVisible(bool(text))

    def _save(self, change: Callable[[Config], object]) -> str:
        """Apply ``change`` and save; on a failed save undo it. Returns an error or ``""``."""
        config = self._config
        if config is None:
            return "No settings are loaded."
        with config.locked():
            before = list(config.data.get(libraries.CONFIG_KEY) or [])
            try:
                change(config)
            except libraries.LibraryError as exc:
                return str(exc)
            try:
                config.save()
            except OSError as exc:
                config.data[libraries.CONFIG_KEY] = before
                return f"Could not save the settings: {exc}"
        return ""

    # ---- actions --------------------------------------------------------------------------
    def _pick_folder(self) -> None:
        chosen = QFileDialog.getExistingDirectory(self, "Add library", str(Path.home()))
        if chosen:
            self.add_folder(chosen)

    def add_folder(self, path: str) -> str:
        """Add ``path`` as a library; returns the reason it was refused, or ``""``."""
        added: list[Path] = []
        error = self._save(lambda c: added.append(libraries.add_library(c, path)))
        if error:
            self._say(error)
            return error
        self._say("")
        self._rebuild()
        self.find_games_later(added, quiet=True)
        return ""

    def remove(self, root: Path) -> None:
        error = self._save(lambda c: libraries.remove_library(c, root))
        if error:
            self._say(error)
            return
        self._say(f"Removed {root} from the libraries. Its games and files were not touched.")
        self._rebuild()

    @property
    def searching(self) -> bool:
        """True while Find games is looking through the libraries on a worker thread."""
        return self._searching

    def _snapshot(self) -> Config:
        """A private copy of the settings for a scan on another thread."""
        assert self._config is not None
        with self._config.locked():
            return Config(path=self._config.path, data=copy.deepcopy(self._config.data))

    def find_games_later(self, roots: list[Path] | None = None, *, quiet: bool = False) -> None:
        """Look through the libraries on a worker thread, then offer what was found.

        A library on a slow, remote or hung drive must not freeze the window: the scan reads
        every game folder. The question and the profile changes stay on the GUI thread.
        """
        if self._config is None:
            return
        if self._searching:
            # Look again when this scan ends: a library added meanwhile was not in its list
            # (round 5). ``[]`` means every library.
            if roots and self._queued != []:
                self._queued = [*(self._queued or []), *roots]
            elif not roots:
                self._queued = []
            return
        self._searching = True
        self.find_button.setEnabled(False)
        self._say("Looking for games in your libraries…")
        snapshot = self._snapshot()
        wanted = list(roots) if roots else None
        task = CallTask(lambda: _look_for_games(snapshot, wanted))
        task.signals.done.connect(lambda result: self._search_done(result, quiet))
        task.signals.failed.connect(self._search_failed)
        QThreadPool.globalInstance().start(task)

    def _search_finished(self) -> bool:
        """Re-enable Find games; False when the panel is already gone."""
        self._searching = False
        try:
            self.find_button.setEnabled(True)
        except RuntimeError:
            return False  # the Settings page was closed while the scan ran
        self._say("")
        return True

    def _search_done(self, search: _Search, quiet: bool) -> None:
        if self._search_finished():
            self._offer(search, quiet=quiet)
            self._run_queued()

    def _run_queued(self) -> None:
        queued, self._queued = self._queued, None
        if queued is not None:
            self.find_games_later(queued or None, quiet=bool(queued))

    def _search_failed(self, error: str) -> None:
        if self._search_finished():
            log.warning("library scan failed: %s", error)
            self._say(f"Could not look through the libraries: {error}")
            self._run_queued()

    def find_games(self, roots: list[Path] | None = None, *, quiet: bool = False) -> list[str]:
        """Offer the games found in ``roots`` (all libraries by default); return added names.

        Runs the scan here, on the caller's thread. The buttons use :meth:`find_games_later`.
        """
        if self._config is None:
            return []
        return self._offer(_look_for_games(self._snapshot(), roots), quiet=quiet)

    def _offer(self, search: _Search, *, quiet: bool) -> list[str]:
        config = self._config
        if config is None:
            return []
        for note in search.notes:
            log.info("library scan: %s", note)
        # Profiles may have changed while a worker scanned a copy: offer only games that
        # still have no profile.
        def new(c: libraries.Candidate) -> bool:
            return backends.existing_profile(c.kind, config.profiles) is None

        found = [c for c in search.found if new(c)]
        guessed = [c for c in search.guessed if new(c)]
        if not found and not guessed:
            if not quiet:
                self._say("No new games were found in your libraries.")
            return []
        # A game with its manifest starts ticked; a guess (manifest gone, recognised by its
        # program) starts unticked and is never added unless the user ticks it.
        offered = found + guessed
        items = [(f"{c.title}  ({libraries.display_path(c.game_dir)})", True) for c in found] + [
            (
                f"{c.title}  ({libraries.display_path(c.game_dir)})  · guessed, no Nadare file",
                False,
            )
            for c in guessed
        ]
        picked = self._choose(items)
        if not picked:
            return []
        chosen = [c for c, keep in zip(offered, picked, strict=False) if keep]
        if not chosen:
            return []
        names: list[str] = []
        with config.locked():
            for candidate in chosen:
                # One profile per game: two libraries may hold the same game, or one may have
                # been added while the question was open. The first one wins.
                if backends.existing_profile(candidate.kind, config.profiles) is None:
                    names.append(libraries.adopt(config, candidate))
            try:
                config.save()
            except OSError as exc:
                for name in names:
                    config.remove_profile(name)
                self._say(f"Could not save the settings: {exc}")
                return []
        self._say(f"Added {len(names)} {'game' if len(names) == 1 else 'games'} to your Library.")
        self.games_added.emit(names)
        return names

    def _choose_box(self, items: list[tuple[str, bool]]) -> list[bool] | None:
        """A list of found games with a checkbox each; ``None`` when cancelled."""
        dialog = QDialog(self)
        dialog.setWindowTitle("Games found")
        layout = QVBoxLayout(dialog)
        intro = QLabel("Nadare found these games in your libraries. Add the ticked ones?")
        intro.setWordWrap(True)
        layout.addWidget(intro)
        boxes = []
        for label, checked in items:
            box = QCheckBox(label)
            box.setChecked(checked)
            layout.addWidget(box)
            boxes.append(box)
        buttons = QDialogButtonBox()
        buttons.addButton("Add", QDialogButtonBox.ButtonRole.AcceptRole)
        buttons.addButton("Cancel", QDialogButtonBox.ButtonRole.RejectRole)
        buttons.accepted.connect(dialog.accept)
        buttons.rejected.connect(dialog.reject)
        layout.addWidget(buttons)
        if dialog.exec() != QDialog.DialogCode.Accepted:
            return None
        return [box.isChecked() for box in boxes]


__all__ = ["LibrariesPanel", "LibraryRow"]
