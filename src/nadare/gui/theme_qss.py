# SPDX-License-Identifier: GPL-3.0-or-later
# Copyright (C) 2026 Nadare contributors
"""The launcher stylesheet, split out of :mod:`nadare.gui.theme` (#1601).

The colour constants and the icon paths come from ``theme``; ``theme`` imports this
module back at the bottom, after its palette is defined.
"""

from __future__ import annotations

from pathlib import Path

from .theme import (
    ACCENT,
    ACCENT_DEEP,
    ACCENT_HI,
    ACCENT_INK,
    BAD,
    BG,
    BORDER,
    BORDER_HI,
    BUTTON_TEXT,
    CARD_RADIUS,
    CONSOLE_BG,
    DIM,
    DISABLED_BG,
    DISABLED_TEXT,
    ELEV,
    FIELD_BG,
    INFO,
    LINE,
    LINE_SOFT,
    LIVE,
    MUTED,
    OK,
    PANEL,
    PANEL_2,
    PANEL_3,
    SCROLL,
    SCROLL_HI,
    TEXT,
    TEXT_SOFT,
    WARN,
)

__all__ = [
    "STYLESHEET",
    "_ARROW_ICON",
    "_ARROW_IMAGE",
    "_CHECK_ICON",
    "_CHECK_IMAGE",
    "_UP_ARROW_ICON",
    "_UP_ARROW_IMAGE",
    "_qss_url",
]


#: Object-name based stylesheet. Widgets opt in with ``setObjectName``.
#: Tick drawn inside a checked box (dark ink on the accent). Without the file the
#: box still shows as accent-filled, just without the mark.
_CHECK_ICON = Path(__file__).resolve().parent.parent / "assets" / "icons" / "check.svg"


def _qss_url(path: Path) -> str:
    """A quoted ``url(...)``: an unquoted one breaks on a path with a space (#1207 b245)."""
    text = path.as_posix().replace("\\", "\\\\").replace('"', '\\"')
    return f'url("{text}")'


_CHECK_IMAGE = f"image: {_qss_url(_CHECK_ICON)};" if _CHECK_ICON.is_file() else ""
# Qt draws the CSS zero-size border triangle as a small bar ("="), so the arrow is an icon.
# Its stroke is MUTED; keep the two in step.
_ARROW_ICON = _CHECK_ICON.with_name("chevron-down.svg")
_ARROW_IMAGE = f"image: {_qss_url(_ARROW_ICON)};" if _ARROW_ICON.is_file() else "image: none;"
_UP_ARROW_ICON = _CHECK_ICON.with_name("chevron-up.svg")
_UP_ARROW_IMAGE = (
    f"image: {_qss_url(_UP_ARROW_ICON)};" if _UP_ARROW_ICON.is_file() else "image: none;"
)


STYLESHEET = f"""
* {{
    color: {TEXT};
    font-size: 13px;
    font-family: "Inter", "Cantarell", "Noto Sans", "Segoe UI", sans-serif;
}}
QMainWindow, QWidget#Root, QWidget#Page, QStackedWidget {{
    background: qradialgradient(cx:0.16, cy:-0.06, radius:1.15, fx:0.12, fy:-0.10,
        stop:0 #27406b, stop:0.38 #131d31, stop:0.75 #0a0e16, stop:1 #06080d);
}}
QLabel {{ background: transparent; }}
QMessageBox, QDialog {{ background: {PANEL}; }}
QMessageBox QLabel {{
    color: {TEXT};
    selection-background-color: {ACCENT_DEEP};
    selection-color: {TEXT};
}}

/* ---- typography ------------------------------------------------------- */
QLabel#Title {{ font-size: 21px; font-weight: 800; letter-spacing: 1.5px; }}
QLabel#Brand {{ font-size: 22px; font-weight: 800; letter-spacing: 3px; }}
QLabel#BrandCJK {{ color: {MUTED}; font-size: 12px; font-weight: 600; }}
QLabel#BrandDot {{ color: {ACCENT}; font-size: 22px; font-weight: 800; }}
QLabel#Subtitle {{ color: {MUTED}; font-size: 12px; }}
QLabel#HeroTitle {{ font-size: 31px; font-weight: 800; letter-spacing: -0.5px; }}
QLabel#HeroKicker {{ color: {MUTED}; font-size: 10px; font-weight: 700;
                     letter-spacing: 2px; }}
QLabel#Section {{ color: {MUTED}; font-size: 10px; font-weight: 700;
                  letter-spacing: 2px; }}
QLabel#Hint {{ color: {TEXT_SOFT}; }}
QLabel#Muted {{ color: {MUTED}; }}
QLabel#Dim {{ color: {DIM}; font-size: 11px; }}
QLabel#Accent {{ color: {ACCENT}; font-weight: 700; }}
QLabel#StateGood, QLabel#StateWarn, QLabel#StateBad {{ font-weight: 700; }}
QLabel#StateGood {{ color: {OK}; }}
QLabel#StateWarn {{ color: {WARN}; }}
QLabel#StateBad {{ color: {BAD}; }}

/* ---- status pills ----------------------------------------------------- */
QLabel#Pill {{
    background: {PANEL_3};
    border: 1px solid {LINE};
    border-radius: 7px;
    padding: 3px 9px;
    font-size: 9px;
    font-weight: 750;
    letter-spacing: 1px;
    color: {MUTED};
}}
QLabel#Pill[state="good"] {{
    background: rgba(61, 139, 253, 0.14);
    border-color: rgba(61, 139, 253, 0.38);
    color: {ACCENT};
}}
QLabel#Pill[state="warn"] {{
    background: rgba(245, 200, 106, 0.14);
    border-color: rgba(245, 200, 106, 0.38);
    color: {WARN};
}}
QLabel#Pill[state="bad"] {{
    background: rgba(255, 138, 138, 0.14);
    border-color: rgba(255, 138, 138, 0.40);
    color: {BAD};
}}
QLabel#Pill[state="info"] {{
    background: rgba(138, 180, 255, 0.14);
    border-color: rgba(138, 180, 255, 0.38);
    color: {INFO};
}}

/* ---- sidebar ---------------------------------------------------------- */
QWidget#Sidebar {{
    background: rgba(5, 8, 13, 0.72);
    border-right: 1px solid {LINE_SOFT};
}}
QFrame#BrandMark {{
    background: {ACCENT};
    border-radius: 10px;
}}
QLabel#BrandGlyph {{ color: {ACCENT_INK}; font-size: 17px; font-weight: 900; }}
QPushButton#NavItem {{
    background: transparent;
    border: 1px solid transparent;
    border-radius: 8px;
    text-align: left;
    padding: 10px 13px;
    color: {MUTED};
    font-weight: 650;
}}
QPushButton#NavItem:hover {{ background: {PANEL_3}; color: {TEXT}; }}
QPushButton#NavItem[active="true"] {{
    background: rgba(61, 139, 253, 0.10);
    color: {ACCENT};
    border: 1px solid rgba(61, 139, 253, 0.18);
}}
QFrame#SideDivider {{ background: {LINE}; max-height: 1px; border: none; }}

/* ---- sidebar installed games ----------------------------------------- */
QScrollArea#SidebarScroll {{ background: transparent; border: none; }}
QWidget#SidebarGamesContainer {{ background: transparent; }}
QFrame#GameRow {{
    background: transparent;
    border: 1px solid transparent;
    border-radius: 8px;
}}
QFrame#GameRow:hover {{ background: {PANEL_3}; }}
QFrame#GameRow[active="true"] {{
    background: rgba(61, 139, 253, 0.10);
    border: 1px solid rgba(61, 139, 253, 0.18);
}}
QLabel#GameRowTitle {{ color: {TEXT_SOFT}; font-size: 12px; font-weight: 600; }}
QLabel#GameRowTitle[dim="true"] {{ color: #566073; }}
QLabel#GameRowTitle[active="true"] {{ color: {ACCENT}; }}
QFrame#GameRow:hover QLabel#GameRowTitle {{ color: {TEXT}; }}
QLabel#GameRowIcon {{ background: {PANEL_3}; border-radius: 8px; }}
QLabel#GameRowDot {{ background: {ACCENT}; border-radius: 4px; }}
QLabel#GameRowDot[kind="update"] {{ background: {WARN}; }}
QLabel#GameRowDot[kind="running"] {{ background: {LIVE}; }}
QLabel#GameRowPlaying {{ color: {LIVE}; font-size: 10px; font-weight: 600; }}

/* ---- top bar / breadcrumbs ------------------------------------------- */
QWidget#TopBar {{
    background: rgba(17, 18, 20, 0.92);
    border-bottom: 1px solid {LINE_SOFT};
}}
QLabel#Breadcrumb {{ color: {DIM}; font-size: 11px; }}
QLabel#BreadcrumbStrong {{ color: {TEXT_SOFT}; font-size: 11px; font-weight: 600; }}

/* ---- cards ------------------------------------------------------------ */
QFrame#Card {{
    background: {PANEL_2};
    border: 1px solid {LINE};
    border-radius: {CARD_RADIUS}px;
}}
QFrame#Hero {{
    background: {PANEL_2};
    border: 1px solid {BORDER};
    border-radius: 16px;
}}
QFrame#StatsBar {{
    background: {PANEL};
    border: 1px solid {LINE_SOFT};
    border-radius: 10px;
}}
QFrame#GameCard, QFrame#GameCard:hover {{ background: transparent; border: none; }}
QLabel#TileTitle {{ color: #eef3fb; font-size: 11px; font-weight: 600; }}
QLabel#LibraryTitle {{ color: #dbe4f2; font-size: 10px; }}
QLabel#LibraryMeta {{ color: #7d8aa0; font-size: 10px; }}
/* A state on a library tile: a solid pill so it reads on any art. */
QLabel#TileBadge {{
    background: rgba(10, 14, 22, 0.92);
    border: 1px solid rgba(255, 255, 255, 0.18);
    border-radius: 9px;
    padding: 0px 8px;
    font-size: 9px;
    font-weight: 700;
    letter-spacing: 1px;
    color: #c3cbd9;
}}
QLabel#TileBadge[state="good"] {{ color: {ACCENT}; border-color: rgba(61, 139, 253, 0.6); }}
QLabel#TileBadge[state="warn"] {{ color: {WARN}; border-color: rgba(245, 200, 106, 0.6); }}
QLabel#TileBadge[state="bad"] {{ color: {BAD}; border-color: rgba(255, 138, 138, 0.6); }}
QLabel#TileBadge[state="info"] {{ color: {INFO}; border-color: rgba(138, 180, 255, 0.6); }}
QLabel#LibrarySection {{ color: #8194b0; font-size: 10px; font-weight: 700;
                          letter-spacing: 2px; }}
QLabel#NewPill {{
    background: qlineargradient(x1:0, y1:0, x2:1, y2:1, stop:0 #5b8cff, stop:1 #8a5cff);
    color: white; border-radius: 7px; font-size: 9px; font-weight: 700;
}}
QLineEdit#LibrarySearch {{ background: rgba(255,255,255,0.05);
    border: 1px solid rgba(255,255,255,0.08); border-radius: 14px; padding: 0px 16px; }}
QPushButton#LibraryAdd {{ background: rgba(255,255,255,0.06);
    border: 1px solid rgba(255,255,255,0.12); border-radius: 14px; padding: 0px 14px;
    font-size: 11px; }}
QPushButton#QuickPlay {{ background: #5b8cff; color: white; border-radius: 16px;
    border: 1px solid rgba(255,255,255,0.4); padding: 0px; }}
QPushButton#QuickPlay:hover {{ background: #8a5cff; }}
QFrame#AddGameCard {{
    background: transparent;
    border: 1px dashed {BORDER};
    border-radius: {CARD_RADIUS}px;
}}
QFrame#AddGameCard:hover {{
    border: 1px dashed {ACCENT};
    background: rgba(61, 139, 253, 0.05);
}}
QLabel#CardTitle {{ font-weight: 700; font-size: 14px; }}
QLabel#CardMeta {{ color: {MUTED}; font-size: 11px; }}
QPushButton#CardPlay {{
    background: {ACCENT};
    color: #ffffff;
    border: 1px solid {ACCENT};
    border-radius: 7px;
    padding: 3px 10px;
    font-size: 10px;
    font-weight: 700;
}}
QPushButton#CardPlay:hover {{ background: {ACCENT_HI}; border-color: {ACCENT_HI}; }}
QPushButton#CardPlay:pressed {{ background: {ACCENT_DEEP}; border-color: {ACCENT_DEEP}; }}
QPushButton#CardPlay:focus {{ border: 1px solid {TEXT}; }}
QFrame#AddPlus {{
    background: rgba(61, 139, 253, 0.10);
    border: 1px solid rgba(61, 139, 253, 0.30);
    border-radius: 26px;
}}
QLabel#AddPlusGlyph {{ color: {ACCENT}; font-size: 30px; font-weight: 300; }}
QFrame#ArtFrame {{
    background: {PANEL};
    border: 1px solid {LINE};
    border-radius: 12px;
}}
QLabel#LogHeader {{ color: {MUTED}; font-size: 10px; font-weight: 700;
                    letter-spacing: 2px; }}

/* ---- news ------------------------------------------------------------- */
QScrollArea#GameScroll {{ background: {BG}; border: none; }}
QFrame#NewsSection {{
    background: {PANEL};
    border: 1px solid {LINE_SOFT};
    border-radius: 14px;
}}
QFrame#NewsCard {{
    background: {PANEL_2};
    border: 1px solid {LINE};
    border-radius: {CARD_RADIUS}px;
}}
QFrame#NewsCard:hover {{
    background: {PANEL_3};
    border: 1px solid {BORDER_HI};
}}
QLabel#NewsThumb {{ background: {CONSOLE_BG}; border-radius: 10px; }}
QLabel#Banner {{
    background: {PANEL_2};
    border: 1px solid {LINE};
    border-radius: 10px;
    padding: 0;
}}
QLabel#Banner:hover {{ border: 1px solid {BORDER_HI}; }}
QLabel#NewsTitle {{ font-weight: 600; font-size: 13px; }}
QLabel#NewsCategory {{
    background: rgba(61, 139, 253, 0.14);
    border: 1px solid rgba(61, 139, 253, 0.38);
    border-radius: 6px;
    color: {ACCENT};
    font-size: 9px;
    font-weight: 750;
    letter-spacing: 1px;
    padding: 2px 7px;
}}

/* ---- buttons ---------------------------------------------------------- */
QPushButton {{
    background: {PANEL_3};
    border: 1px solid {BORDER};
    border-radius: 8px;
    padding: 9px 14px;
    font-weight: 600;
    color: {BUTTON_TEXT};
}}
QPushButton:hover {{ background: {ELEV}; border-color: {BORDER_HI}; }}
QPushButton:pressed {{ background: {PANEL_2}; }}
QPushButton:focus {{ border: 1px solid {ACCENT}; }}
QPushButton:disabled {{
    color: {DISABLED_TEXT};
    background: {DISABLED_BG};
    border-color: {LINE};
}}
QFrame#Banner {{
    background: {PANEL_2};
    border-bottom: 1px solid {LINE};
}}
QTabWidget::pane {{ border: none; border-top: 1px solid {LINE}; top: -1px; }}
QTabBar {{ background: transparent; }}
QTabBar::tab {{
    background: transparent;
    color: {MUTED};
    padding: 9px 18px;
    margin-right: 4px;
    border: none;
    border-bottom: 2px solid transparent;
    font-weight: 600;
}}
QTabBar::tab:hover {{ color: {TEXT}; }}
QTabBar::tab:selected {{ color: {TEXT}; border-bottom: 2px solid {ACCENT}; }}
QPushButton#GhostButton {{
    background: transparent;
    border: 1px solid {LINE};
    border-radius: 7px;
    padding: 7px 12px;
    color: {TEXT_SOFT};
    font-weight: 600;
}}
QPushButton#GhostButton:hover {{
    background: {PANEL_3};
    color: {TEXT};
    border-color: {BORDER_HI};
}}
QPushButton#GhostButton:disabled {{ color: {DISABLED_TEXT}; border-color: {LINE_SOFT}; }}
QPushButton#Primary {{
    background: {ACCENT};
    color: {ACCENT_INK};
    border: 1px solid {ACCENT};
    border-radius: 10px;
    padding: 15px;
    font-size: 15px;
    font-weight: 800;
    letter-spacing: 1px;
}}
QPushButton#Primary:hover {{ background: {ACCENT_HI}; border-color: {ACCENT_HI}; }}
QPushButton#Primary:pressed {{ background: {ACCENT_DEEP}; border-color: {ACCENT_DEEP}; }}
QPushButton#Primary:disabled {{ background: #16283c; color: #6d8db8; border-color: #16283c; }}
/* Dialog buttons sit in a fixed footer row: the page-sized padding clipped
   the label's descenders ("Add game" lost the tail of the g). */
QDialog QPushButton#Primary, QDialog QPushButton#Danger {{
    padding: 9px 20px;
    font-size: 14px;
}}
QPushButton#Danger {{
    background: rgba(255, 138, 138, 0.12);
    border: 1px solid rgba(255, 138, 138, 0.45);
    color: #ffb3b3;
    border-radius: 10px;
    padding: 15px;
    font-size: 15px;
    font-weight: 800;
    letter-spacing: 1px;
}}
/* Keyboard focus. "QPushButton:focus" above loses to these #id rules, so without the lines
   below Tab moved through Launch, the sidebar and every ghost button with nothing to see.
   Only the border colour changes (same width), so nothing moves. */
QPushButton#GhostButton:focus, QPushButton#NavItem:focus {{ border: 1px solid {ACCENT}; }}
QPushButton#Primary:focus {{ border: 1px solid {TEXT}; }}
QPushButton#Danger:focus {{ border: 1px solid #ffb3b3; }}
/* Over the hero art a see-through button reads as part of the picture (user sim r2v 015):
   give the hero's buttons a dark backing, disabled ones too. */
QFrame#Hero QPushButton#GhostButton {{
    background: rgba(10, 14, 22, 0.82);
    border-color: rgba(255, 255, 255, 0.16);
}}
QFrame#Hero QPushButton#GhostButton:hover {{
    background: {PANEL_3};
    color: {TEXT};
    border-color: {BORDER_HI};
}}
QFrame#Hero QPushButton#GhostButton:focus {{ border: 1px solid {ACCENT}; }}
QFrame#Hero QPushButton#GhostButton:disabled {{
    background: rgba(10, 14, 22, 0.72);
    color: #8a94a8;
    border-color: rgba(255, 255, 255, 0.08);
}}
/* Stop next to PLAYING: solid, so it is found on any art. */
QPushButton#StopButton {{
    background: rgba(10, 14, 22, 0.92);
    border: 1px solid rgba(255, 138, 138, 0.55);
    border-radius: 10px;
    color: {BAD};
    font-size: 18px;
    font-weight: 700;
}}
QPushButton#StopButton:hover {{
    background: rgba(255, 138, 138, 0.18);
    color: #ffffff;
    border-color: {BAD};
}}
QPushButton#StopButton:focus {{ border: 1px solid {TEXT}; }}
QPushButton#StopButton:disabled {{ color: {DISABLED_TEXT}; border-color: {LINE_SOFT}; }}
/* A running game: a state, not an action. Not greyed out when disabled. */
QPushButton#Playing, QPushButton#Playing:disabled {{
    background: rgba(61, 139, 253, 0.14);
    border: 1px solid rgba(61, 139, 253, 0.38);
    color: {ACCENT};
    border-radius: 10px;
    padding: 15px;
    font-size: 15px;
    font-weight: 800;
    letter-spacing: 1px;
}}
QPushButton::menu-indicator {{ image: none; width: 0; }}
QMenu {{
    background: {PANEL_2};
    border: 1px solid {LINE};
    border-radius: 8px;
    padding: 4px;
    color: {TEXT};
}}
QMenu::item {{ padding: 7px 18px; border-radius: 6px; }}
QMenu::item:selected {{ background: rgba(61, 139, 253, 0.14); color: {ACCENT}; }}
QMenu::item:disabled {{ color: {DISABLED_TEXT}; }}
QMenu::separator {{ height: 1px; background: {LINE}; margin: 4px 8px; }}
QPushButton#Danger:hover {{ background: rgba(255, 138, 138, 0.22); }}
QPushButton#Danger:pressed {{ background: rgba(255, 138, 138, 0.30); }}

/* ---- inputs ----------------------------------------------------------- */
QLineEdit, QComboBox, QAbstractSpinBox {{
    background: {FIELD_BG};
    border: 1px solid {LINE};
    border-radius: 8px;
    padding: 8px 11px;
    color: {TEXT};
    min-height: 18px;
    selection-background-color: {ACCENT};
    selection-color: {ACCENT_INK};
}}
QPlainTextEdit {{
    background: {FIELD_BG};
    border: 1px solid {LINE};
    border-radius: 8px;
    padding: 6px 9px;
    color: {TEXT};
    selection-background-color: {ACCENT};
    selection-color: {ACCENT_INK};
}}
QPlainTextEdit:focus {{ border: 1px solid {ACCENT}; }}
QLineEdit:hover, QComboBox:hover, QAbstractSpinBox:hover {{ border-color: {BORDER_HI}; }}
QLineEdit:focus, QComboBox:focus, QAbstractSpinBox:focus {{ border: 1px solid {ACCENT}; }}
QLineEdit:read-only, QLineEdit:disabled {{ color: {MUTED}; background: {PANEL}; }}
QComboBox::drop-down {{ border: none; width: 26px; }}
QComboBox::down-arrow {{
    {_ARROW_IMAGE}
    width: 12px; height: 12px;
    margin-right: 8px;
}}
/* Styled spin boxes lose the native arrows (two dots on KDE): draw the chevrons. */
QAbstractSpinBox {{ padding-right: 24px; }}
QAbstractSpinBox::up-button, QAbstractSpinBox::down-button {{
    subcontrol-origin: border;
    width: 22px;
    border: none;
    background: transparent;
}}
QAbstractSpinBox::up-button {{ subcontrol-position: top right; margin: 3px 3px 0 0; }}
QAbstractSpinBox::down-button {{ subcontrol-position: bottom right; margin: 0 3px 3px 0; }}
QAbstractSpinBox::up-button:hover, QAbstractSpinBox::down-button:hover {{
    background: rgba(255, 255, 255, 0.06);
    border-radius: 4px;
}}
QAbstractSpinBox::up-arrow {{ {_UP_ARROW_IMAGE} width: 10px; height: 10px; }}
QAbstractSpinBox::down-arrow {{ {_ARROW_IMAGE} width: 10px; height: 10px; }}
QAbstractSpinBox::up-arrow:disabled, QAbstractSpinBox::down-arrow:disabled {{ image: none; }}
QComboBox QAbstractItemView {{
    background: {PANEL_2};
    border: 1px solid {LINE};
    border-radius: 8px;
    padding: 4px;
    outline: none;
    selection-background-color: rgba(61, 139, 253, 0.14);
    selection-color: {ACCENT};
}}
QListWidget {{
    background: {PANEL_2};
    /* Alternating rows: without this Qt takes the platform palette, which in the Flatpak (no
       KDE colour scheme) is its light default: every other row near-white (2026-11-11). */
    alternate-background-color: {PANEL};
    border: 1px solid {LINE};
    border-radius: 10px;
    padding: 6px;
    outline: none;
    color: {TEXT};
}}
QListWidget::item {{ padding: 7px 10px; border-radius: 7px; }}
QListWidget::item:hover {{ background: {PANEL_3}; }}
QListWidget::item:selected {{
    background: rgba(61, 139, 253, 0.16);
    color: {ACCENT_HI};
}}
QListWidget::item:disabled {{
    color: {DIM};
    font-size: 11px;
    font-weight: 700;
    letter-spacing: 1px;
    padding-top: 10px;
}}
QCheckBox {{ spacing: 9px; color: {TEXT_SOFT}; }}
QCheckBox::indicator {{
    width: 18px; height: 18px;
    border-radius: 5px;
    border: 1px solid {BORDER_HI};
    background: {FIELD_BG};
}}
QCheckBox::indicator:hover {{ border-color: {BORDER_HI}; }}
/* Focus is the light text colour, so accent keeps meaning "ticked". */
QCheckBox::indicator:focus, QCheckBox::indicator:checked:focus {{ border-color: {TEXT}; }}
QCheckBox::indicator:checked {{ background: {ACCENT}; border-color: {ACCENT}; {_CHECK_IMAGE} }}
QTableWidget {{
    background: {FIELD_BG}; color: {TEXT}; gridline-color: {BORDER};
    border: 1px solid {BORDER}; border-radius: 10px;
}}
QTableWidget::item {{ padding: 6px 8px; }}
QHeaderView::section {{
    background: {FIELD_BG}; color: {TEXT_SOFT}; border: none;
    border-bottom: 1px solid {BORDER}; padding: 6px 8px;
}}
QTableCornerButton::section {{ background: {FIELD_BG}; border: none; }}
QRadioButton {{ spacing: 9px; color: {TEXT_SOFT}; }}
QRadioButton:checked {{ color: {TEXT}; }}
QRadioButton:disabled {{ color: {TEXT_SOFT}; }}
QRadioButton::indicator {{
    width: 14px; height: 14px;
    border-radius: 9px;
    border: 2px solid {BORDER_HI};
    background: {FIELD_BG};
}}
QRadioButton::indicator:hover {{ border-color: {ACCENT_HI}; }}
QRadioButton::indicator:checked {{
    border: 2px solid {ACCENT};
    background: qradialgradient(cx:0.5, cy:0.5, radius:0.5, fx:0.5, fy:0.5,
        stop:0 {ACCENT}, stop:0.55 {ACCENT}, stop:0.6 {FIELD_BG}, stop:1 {FIELD_BG});
}}

/* ---- console / progress ---------------------------------------------- */
QPlainTextEdit#Console {{
    background: {CONSOLE_BG};
    border: 1px solid {LINE_SOFT};
    border-radius: 12px;
    font-family: "JetBrains Mono", "Fira Code", "DejaVu Sans Mono", monospace;
    font-size: 12px;
    color: {TEXT_SOFT};
    padding: 10px 12px;
    selection-background-color: #16283c;
    selection-color: {ACCENT};
}}
QTextBrowser#Report {{
    background: {CONSOLE_BG};
    border: 1px solid {LINE_SOFT};
    border-radius: 12px;
    font-size: 13px;
    color: {TEXT_SOFT};
    padding: 10px 12px;
    selection-background-color: #16283c;
    selection-color: {ACCENT};
}}
QProgressBar {{
    background: {PANEL_3};
    border: none;
    border-radius: 6px;
    height: 12px;
    text-align: center;
    color: {TEXT_SOFT};
    font-size: 10px;
    font-weight: 700;
}}
QProgressBar::chunk {{ background: {ACCENT}; border-radius: 6px; }}

/* ---- scrollbars ------------------------------------------------------- */
QScrollArea {{ border: none; background: transparent; }}
QScrollArea > QWidget > QWidget {{ background: transparent; }}
QScrollArea > QWidget {{ background: transparent; }}
QScrollArea > QWidget#LibraryContainer {{ background: transparent; }}
QScrollBar:vertical {{ background: transparent; width: 12px; margin: 2px; }}
QScrollBar::handle:vertical {{ background: {SCROLL}; border-radius: 5px; min-height: 36px; }}
QScrollBar::handle:vertical:hover {{ background: {SCROLL_HI}; }}
QScrollBar::add-line:vertical, QScrollBar::sub-line:vertical {{ height: 0; }}
QScrollBar:horizontal {{ background: transparent; height: 12px; margin: 2px; }}
QScrollBar::handle:horizontal {{ background: {SCROLL}; border-radius: 5px; min-width: 36px; }}
QScrollBar::handle:horizontal:hover {{ background: {SCROLL_HI}; }}
QScrollBar::add-line:horizontal, QScrollBar::sub-line:horizontal {{ width: 0; }}
QToolTip {{
    background: {PANEL_2};
    color: {TEXT};
    border: 1px solid {LINE};
    padding: 5px 7px;
    border-radius: 6px;
}}
"""
