# SPDX-License-Identifier: GPL-3.0-or-later
"""Default profile names are short, written-down names, not slugs of long titles (#1134).

The profile name is the folder under ``profiles/`` (prefix, install, logs), the config key and
the ``--profile`` argument. Slugging the catalog title gave names like
``girls-frontline-2-haoplay-global-build-publisher-s-launcher`` (59 characters). Each game kind
now has a short name the players use; the display title stays the full title.
"""

from __future__ import annotations

import os

os.environ.setdefault("QT_QPA_PLATFORM", "offscreen")

import pytest

try:  # pragma: no cover - only when PyQt6 is missing
    from PyQt6.QtWidgets import QApplication
except Exception:  # noqa: BLE001
    pytest.skip("PyQt6 is not importable in this interpreter", allow_module_level=True)

from nadare.core.config import Config, valid_profile_name
from nadare.core.profilenames import SHORT_NAMES, short_name
from nadare.gui.add_game import AddGameDialog, _catalog, _slugify

GENERIC_KINDS = {"wine", "steam", "script"}


@pytest.fixture(scope="module")
def qapp():
    app = QApplication.instance() or QApplication([])
    yield app


def test_every_game_kind_has_a_short_name():
    kinds = {game.kind for game in _catalog()} - GENERIC_KINDS
    missing = sorted(kinds - set(SHORT_NAMES))
    assert not missing, f"add a short default name for {missing} in core/profilenames.py"


def test_short_names_are_short_valid_slugs_and_unique():
    for kind, name in SHORT_NAMES.items():
        assert name == _slugify(name), (kind, name)
        assert valid_profile_name(name), (kind, name)
        assert 2 <= len(name) <= 16, (kind, name)
    assert len(set(SHORT_NAMES.values())) == len(SHORT_NAMES)


@pytest.mark.parametrize(
    ("kind", "expected"),
    [
        ("direct-gfl2", "gfl2"),
        ("direct-gfl2-haoplay", "gfl2-hp"),
        ("direct-nte", "nte"),
        ("launcher-nikke", "nikke"),
        ("direct-genshin", "genshin"),
        ("direct-wuwa", "wuwa"),
    ],
)
def test_known_short_names(kind, expected):
    assert short_name(kind) == expected


def test_a_generic_kind_has_no_short_name():
    for kind in GENERIC_KINDS:
        assert short_name(kind) == ""


def _dialog(tmp_path):
    return AddGameDialog(Config.load(tmp_path / "config.json"), data_root=tmp_path / "data")


def test_the_wizard_prefills_the_short_name_and_folder(tmp_path, qapp):
    dialog = _dialog(tmp_path)
    try:
        dialog.set_backend("direct-gfl2-haoplay")
        assert dialog.name.text() == "gfl2-hp"
        assert dialog.folder.text() == str(tmp_path / "data" / "gfl2-hp" / "install")
    finally:
        dialog.deleteLater()
        qapp.processEvents()


def test_a_taken_short_name_is_numbered_not_lengthened(tmp_path, qapp):
    dialog = _dialog(tmp_path)
    try:
        dialog.config.add_profile("gfl2", backend="wine")
        dialog.set_backend("direct-gfl2")
        assert dialog.name.text() == "gfl2-2"
    finally:
        dialog.deleteLater()
        qapp.processEvents()


def test_a_generic_kind_still_uses_its_title(tmp_path, qapp):
    dialog = _dialog(tmp_path)
    try:
        dialog.set_backend("wine")
        assert dialog.name.text() == "wine-proton-game"
    finally:
        dialog.deleteLater()
        qapp.processEvents()


def test_a_user_typed_name_is_kept(tmp_path, qapp):
    dialog = _dialog(tmp_path)
    try:
        dialog.set_backend("direct-genshin")
        dialog.name.setText("my-genshin")
        dialog.name.textEdited.emit("my-genshin")
        dialog.set_backend("direct-hsr")
        assert dialog.name.text() == "my-genshin"
    finally:
        dialog.deleteLater()
        qapp.processEvents()
