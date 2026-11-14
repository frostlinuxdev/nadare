# SPDX-License-Identifier: GPL-3.0-or-later
# Copyright (C) 2026 Nadare contributors
"""Loading a profile into the form and saving the form back.

Split out of :mod:`nadare.gui.settings`; the code is unchanged.
"""

from __future__ import annotations

from typing import Any

from PyQt6.QtCore import Qt
from PyQt6.QtWidgets import (
    QCheckBox,
    QComboBox,
    QLineEdit,
    QMessageBox,
    QPlainTextEdit,
    QSpinBox,
)

from ...core import artwork as artwork_core
from ...core import flatpakfs, gamefolders
from ...core import runners as runners_core
from ...core.backends import kinds, supports_remake_prefix, supports_remember_me
from ...core.config import Config
from .. import i18n
from ..library import profile_title
from .shared import (
    GAME_SUBTITLE,
    _fill_monitor_combo,
    _text_to_env,
)

APP_SUBTITLE = "Settings that belong to Nadare itself and apply to every game."


class PersistenceMixin:
    """Mixin for :class:`SettingsPage`; see the module text."""

    def _form_state(self) -> dict[str, Any]:
        """The form's current values, for comparing against the loaded ones."""
        state: dict[str, Any] = {
            "title": self.title.text(),
            "background": self.background.text(),
            "remember": self.remember.isChecked(),
            "language": self.language.currentData(),
            "check_updates": self.check_updates.isChecked(),
            "check_notices": self.check_notices.isChecked(),
            "notify_when_done": self.notify_when_done.isChecked(),
            "hide_uninstalled": self.hide_uninstalled.isChecked(),
            "default_monitor": self.default_monitor.currentData(),
        }
        for key, widget in self._option_widgets.items():
            if key == "runner":
                state[key] = self._runner_value()
            elif isinstance(widget, QPlainTextEdit):
                state[key] = widget.toPlainText()
            elif isinstance(widget, QLineEdit):
                state[key] = widget.text()
            elif isinstance(widget, QCheckBox):
                state[key] = widget.isChecked()
            elif isinstance(widget, QSpinBox):
                state[key] = widget.value()
            elif isinstance(widget, QComboBox):
                state[key] = widget.currentData()
        return state

    def _has_unsaved_changes(self) -> bool:
        return self._loaded_state is not None and self._loaded_state != self._form_state()

    # ---- load / save -------------------------------------------------------
    def load(self, config: Config, profile: str) -> None:
        """Populate the form from ``config`` for ``profile``."""
        self._config = config
        self._profile = profile
        self._set_app_only(False)
        self._pending_art = {}
        self.tabs.setCurrentIndex(0)
        data = config.profile(profile)

        self.profile_name.setText(profile)
        self.heading.setText(f"Settings — {profile_title(profile, data)}")
        self.title.setText(str(data.get("title") or ""))
        kind = str(data.get("backend") or "")
        self._backend_kind = kind
        self._runner_default_applies = not bool(
            runners_core.profile_value(data, "wine_binary")
        )
        self.install_dir.setText(str(data.get("install_dir") or ""))
        self._update_free_hints()
        runner_text = runners_core.profile_runner_value(data)
        if not self._reload_runners(runner_text):
            self.runner.setEditText(runner_text)
        self.background.setText(str(data.get("custom_background") or ""))
        self._load_options(kind, data)
        self._update_free_hints()
        self._refresh_backup_row(data)
        if self._files_form is not None:
            self._files_form.setRowVisible(
                # Only where the backend can do it: the generic Wine game uses the Proton
                # default too but has no Rebuild prefix.
                self._remake_prefix_row, supports_remake_prefix(kind)
            )
        has_wine_prefix = any(
            option.key == "prefix" and option.label == "Wine prefix"
            for option in self._backend_options(kind)
        )
        self._refresh_prefix_lock(has_wine_prefix)
        self._show_placement()

        supported = supports_remember_me(str(data.get("backend") or ""))
        self.account_card.setVisible(supported)
        self.remember.setChecked(bool(data.get("remember_me", False)))

        self._load_app_values(config)
        self.note.setText(f"Saved in {config.path}")
        self._loaded_state = self._form_state()

    def _load_app_values(self, config: Config) -> None:
        self.libraries_panel.load(config)
        """Fill the controls that belong to Nadare itself, not to one game."""
        language = str(config.data.get("language") or i18n.DEFAULT_LANGUAGE)
        lang_index = self.language.findData(language)
        self.language.setCurrentIndex(lang_index if lang_index >= 0 else 0)
        self.check_updates.setChecked(bool(config.data.get("check_updates", True)))
        self.check_notices.setChecked(bool(config.data.get("check_notices", True)))
        self.notify_when_done.setChecked(bool(config.data.get("notify_when_done", True)))
        self.hide_uninstalled.setChecked(bool(config.data.get("sidebar_hide_uninstalled", False)))
        saved_monitor = config.data.get("default_monitor")
        _fill_monitor_combo(self.default_monitor, saved_monitor)
        monitor_index = self.default_monitor.findData(str(saved_monitor or ""))
        self.default_monitor.setCurrentIndex(monitor_index if monitor_index >= 0 else 0)

    def _set_app_only(self, app_only: bool) -> None:
        self._app_only = app_only
        self.subtitle.setText(APP_SUBTITLE if app_only else GAME_SUBTITLE)
        for index in range(self.tabs.count()):
            self.tabs.setTabVisible(index, not app_only or index == self._app_tab)
        for button in (self.remove_button, self.report_button, self.logs_button):
            button.setVisible(not app_only)

    def load_app_only(self, config: Config) -> None:
        """Show only Nadare's own settings, for when no game is open."""
        self._config = config
        self._profile = ""
        self._set_app_only(True)
        self._pending_art = {}
        self.tabs.setCurrentIndex(self._app_tab)
        self.heading.setText("Nadare settings")
        self._load_app_values(config)
        self.note.setText(f"Saved in {config.path}")
        self._loaded_state = self._form_state()

    def _save_app_only(self) -> None:
        config = self._config
        keys = (
            "language",
            "check_updates",
            "check_notices",
            "notify_when_done",
            "sidebar_hide_uninstalled",
            "default_monitor",
        )
        try:
            with config.locked():
                backup = {key: config.data.get(key) for key in keys}
                config.data["language"] = self.language.currentData() or i18n.DEFAULT_LANGUAGE
                config.data["check_updates"] = bool(self.check_updates.isChecked())
                config.data["check_notices"] = bool(self.check_notices.isChecked())
                config.data["notify_when_done"] = bool(self.notify_when_done.isChecked())
                config.data["sidebar_hide_uninstalled"] = bool(self.hide_uninstalled.isChecked())
                if self.default_monitor.currentData():
                    config.data["default_monitor"] = self.default_monitor.currentData()
                else:
                    config.data.pop("default_monitor", None)
                config.save()
        except OSError as exc:
            with config.locked():
                for key, old in backup.items():
                    if old is None:
                        config.data.pop(key, None)
                    else:
                        config.data[key] = old
            QMessageBox.critical(self, "Settings", f"Could not save config: {exc}")
            return
        i18n.set_language(config.data["language"])
        self._loaded_state = self._form_state()
        self.saved.emit("")

    def _confirm_environment(self, data: dict[str, Any]) -> bool:
        """Ask before saving a changed, non-empty Environment; True means go ahead.

        A wrong variable can stop a game launching, so a change needs a deliberate
        yes. Unchanged or cleared text never asks.
        """
        editor = self._option_widgets.get("extra_env")
        if editor is None:
            return True
        new = _text_to_env(editor.toPlainText())
        old = self._option_value(data, "extra_env")
        if not new or new == (old if isinstance(old, dict) else {}):
            return True
        answer = QMessageBox.question(
            self,
            "Change Environment?",
            "These variables change how the game starts, and a wrong one can stop it "
            "from launching. Keep them?",
            QMessageBox.StandardButton.Yes | QMessageBox.StandardButton.No,
            QMessageBox.StandardButton.No,
        )
        return answer == QMessageBox.StandardButton.Yes

    def _unsafe_folder_message(self, data: dict[str, Any]) -> str:
        """Why a changed Wine prefix or game folder cannot be saved, or ``""`` (#851).

        Only a value the user just changed is judged, so a folder saved earlier does not block
        saving other settings.
        """
        from ...core import prefixguard

        for key in ("prefix", "wineprefix", "install_dir"):
            editor = self._option_widgets.get(key)
            if editor is None or not hasattr(editor, "text"):
                continue
            value = editor.text().strip()
            if not value or value == str(data.get(key) or "").strip():
                continue
            reason = prefixguard.unsafe_reason(
                value,
                profiles=self._config.profiles if self._config is not None else None,
                own=self._profile,
            )
            if reason:
                return prefixguard.explain(value, reason)
            # The Flatpak must be able to reach a game folder in every flow, not only Add Game.
            out_of_reach = flatpakfs.unreachable_reason(value)
            if out_of_reach:
                return out_of_reach
        return ""

    def save(self) -> None:
        if self._config is not None and self._app_only:
            self._save_app_only()
            return
        if self._config is None or not self._profile:
            self.cancelled.emit()
            return
        config = self._config
        try:
            data = config.profile(self._profile)
        except KeyError:
            self.cancelled.emit()
            return

        backend = self._backend_kind
        catalog = gamefolders.in_model(data)
        if backend not in kinds():
            QMessageBox.critical(
                self,
                "Settings",
                f"{backend!r} is not a backend this build knows, so this game's "
                "settings cannot be saved.",
            )
            return

        refusal = "" if catalog else self._unsafe_folder_message(data)
        if refusal:
            QMessageBox.critical(self, "Settings", refusal)
            return

        runner_text = self.runner.currentText().strip()
        index = self.runner.currentIndex()
        if index >= 0 and runner_text == self.runner.itemText(index):
            runner_value = self.runner.currentData()
        else:
            # The user typed a custom runner path/tag.
            runner_value = runner_text

        if not self._confirm_environment(data):
            return
        if self._refuse_broken_values(backend, runner_text, index):
            return

        # The mutation and the write are taken under the config lock (as the
        # worker-side ``BackendContext.set`` does); otherwise a worker save can
        # serialise this profile dict while it is being changed.
        old_quality = self._option_value(data, "wuwa_quality")
        import copy as _copy

        with config.locked():
            backup = (
                _copy.deepcopy(data),
                config.data.get("language"),
                config.data.get("check_updates"),
                config.data.get("check_notices"),
                config.data.get("notify_when_done"),
                config.data.get("sidebar_hide_uninstalled"),
                config.data.get("default_monitor"),
            )
        warnings: list[str] = []
        install_dir_option = any(
            option.key == "install_dir" for option in self._backend_options(backend)
        )
        try:
            with config.locked():
                data["title"] = self.title.text().strip()
                data["backend"] = backend
                if install_dir_option and not catalog:
                    self._store(data, "install_dir", self.install_dir.text().strip())
                self._store(data, "runner", str(runner_value or ""))
                # A picked image is copied into Nadare's own folder (#1244): the Flatpak cannot
                # read most of the home folder, and a document-portal path is fragile.
                data["custom_background"] = artwork_core.keep_custom_image(
                    self.background.text().strip(),
                    self._profile,
                    previous=str(data.get("custom_background") or ""),
                )

                for option in self._backend_options(data["backend"]):
                    if option.key in ("install_dir", "runner"):
                        continue
                    if catalog and option.key in gamefolders.FOLDER_KEYS:
                        continue  # one location, changed only by Change location / Move
                    editor = self._option_widgets.get(option.key)
                    if editor is None:
                        continue
                    existing = self._option_value(data, option.key)
                    value = self._read_editor(option, editor, warnings)
                    if self._is_default_option_value(option, value) and not (
                        existing not in (None, "", {}, [])
                        and existing != option.default
                    ):
                        # Defaults (and empty text) are not persisted; the backend
                        # already falls back to them when the key is absent.
                        self._store(data, option.key, "")
                    else:
                        self._store(data, option.key, value)
                self.options_note.setText("  ".join(warnings))

                from ...core import artwork

                if data["custom_background"]:
                    artwork.set_art_source(data, "custom_background", artwork.SOURCE_USER)
                else:
                    artwork.art_source(data).pop("custom_background", None)
                data["remember_me"] = bool(self.remember.isChecked())
                config.data["language"] = (
                    self.language.currentData() or i18n.DEFAULT_LANGUAGE
                )
                config.data["check_updates"] = bool(self.check_updates.isChecked())
                config.data["check_notices"] = bool(self.check_notices.isChecked())
                config.data["notify_when_done"] = bool(self.notify_when_done.isChecked())
                config.data["sidebar_hide_uninstalled"] = bool(self.hide_uninstalled.isChecked())
                if self.default_monitor.currentData():
                    config.data["default_monitor"] = self.default_monitor.currentData()
                else:
                    config.data.pop("default_monitor", None)
                for kind, path in self._pending_art.items():
                    artwork.record_thirdparty(data, path, kind)
                config.save()
        except OSError as exc:
            with config.locked():
                data.clear()
                data.update(backup[0])
                for key, old in zip(
                    (
                        "language",
                        "check_updates",
                        "check_notices",
                        "notify_when_done",
                        "sidebar_hide_uninstalled",
                        "default_monitor",
                    ),
                    backup[1:],
                    strict=True,
                ):
                    if old is None:
                        config.data.pop(key, None)
                    else:
                        config.data[key] = old
            QMessageBox.critical(self, "Settings", f"Could not save config: {exc}")
            return
        self._pending_art = {}
        i18n.set_language(config.data["language"])
        self.note.setText(f"Saved to {config.path}")
        from ...core.sources import wuwa

        new_quality = self._option_value(data, "wuwa_quality")
        old_tier = wuwa.quality_of(old_quality)
        new_tier = wuwa.quality_of(new_quality)
        quality_note = ""
        if old_tier != new_tier and self._tier_complete(config, self._profile, new_tier):
            # Back to a tier that is already installed (e.g. undoing #1332's stray SD): nothing
            # to download and nothing to ask (stress round 2, r2).
            old_tier = new_tier
        if old_tier != new_tier:
            quality_note = (
                f"The {new_tier.upper()} files are downloaded by Update (the game page shows it as "
                f"an update); until then the game keeps starting with the files it has. Nadare "
                f"offers to remove the {old_tier.upper()} files once that is done."
            )
        self._loaded_state = self._form_state()
        profile = self._profile
        self.saved.emit(profile)
        if old_tier != new_tier and self._ask_download_now(new_tier):
            # Now: the main window runs the normal Update (same questions as its button).
            self.download_requested.emit(profile)
            quality_note = ""
        if warnings:
            if quality_note:
                warnings.append(quality_note)
            # The page closes on ``saved``, so the inline note is never seen: say it here.
            box = QMessageBox(
                QMessageBox.Icon.Warning,
                "Settings saved",
                "Your settings were saved, but please check:\n\n- " + "\n- ".join(warnings),
                QMessageBox.StandardButton.Ok,
                self.window(),
            )
            box.setAttribute(Qt.WidgetAttribute.WA_DeleteOnClose)
            box.open()  # non-blocking
        elif quality_note:
            # The page has already closed, so the side status is where this can be read.
            self.notice.emit(self._profile, quality_note)

    def _ask_download_now(self, tier: str) -> bool:
        """Ask whether to download a newly chosen WuWa quality now; ``False`` is "Later"."""
        answer = QMessageBox.question(
            self.window(),
            "Download the new quality?",
            f"Download the {tier.upper()} files now? This can be tens of GB.\n\n"
            "Later: the game page shows it as an update, and the game keeps starting with the "
            "files it already has.",
            QMessageBox.StandardButton.Yes | QMessageBox.StandardButton.No,
            QMessageBox.StandardButton.Yes,
        )
        return answer == QMessageBox.StandardButton.Yes

    @staticmethod
    def _tier_complete(config: Any, profile: str, tier: str) -> bool:
        """Whether WuWa's ``tier`` is already fully installed for ``profile`` (False if unsure)."""
        try:
            from ...core.backends import create as create_backend

            backend = create_backend(profile, config)
            complete = getattr(backend, "_complete_tiers", None)
            return bool(callable(complete) and tier in complete())
        except Exception:  # noqa: BLE001 - when unsure, ask as before
            return False
