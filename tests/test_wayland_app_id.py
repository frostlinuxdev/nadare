# SPDX-License-Identifier: GPL-3.0-or-later
"""Nadare's windows carry the app id of the menu entry that started them (maintainer, 2026-11-11).

KDE Plasma Wayland, Flatpak: a bouncing Nadare icon stayed on the cursor after clicks. Qt uses
``QGuiApplication.desktopFileName()`` as the Wayland app id; Nadare never set it, so every window
said ``nadare`` while the Flatpak's menu entry is ``io.github.frostlinuxdev.Nadare.desktop``. KWin
could not tie the windows (and their activation requests) to that entry, so its launch feedback ran
until the timeout. The AppImage/desktop entry is ``nadare.desktop``.
"""

from __future__ import annotations

import os

os.environ.setdefault("QT_QPA_PLATFORM", "offscreen")

import pytest

pytest.importorskip("PyQt6.QtWidgets")

from PyQt6.QtGui import QGuiApplication  # noqa: E402

from nadare import gui  # noqa: E402
from nadare.core import sandbox  # noqa: E402


def test_the_flatpak_uses_its_app_id(monkeypatch):
    monkeypatch.setattr(sandbox, "in_flatpak", lambda: True)
    monkeypatch.setenv("FLATPAK_ID", "io.github.frostlinuxdev.Nadare")
    assert gui.desktop_file_name() == "io.github.frostlinuxdev.Nadare"


def test_the_flatpak_without_flatpak_id_still_uses_the_known_id(monkeypatch):
    monkeypatch.setattr(sandbox, "in_flatpak", lambda: True)
    monkeypatch.delenv("FLATPAK_ID", raising=False)
    assert gui.desktop_file_name() == "io.github.frostlinuxdev.Nadare"


def test_everywhere_else_it_is_the_nadare_entry(monkeypatch):
    monkeypatch.setattr(sandbox, "in_flatpak", lambda: False)
    assert gui.desktop_file_name() == "nadare"


def test_the_application_carries_it(monkeypatch):
    monkeypatch.setattr(sandbox, "in_flatpak", lambda: True)
    monkeypatch.setenv("FLATPAK_ID", "io.github.frostlinuxdev.Nadare")
    QGuiApplication.setDesktopFileName("")
    gui._application()
    assert QGuiApplication.desktopFileName() == "io.github.frostlinuxdev.Nadare"
    QGuiApplication.setDesktopFileName("nadare")
