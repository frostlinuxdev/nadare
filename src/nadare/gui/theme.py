# SPDX-License-Identifier: GPL-3.0-or-later
# Copyright (C) 2026 Nadare contributors
"""The Nadare look: a dark, key-art friendly launcher theme.

Everything here is presentation only. No backend logic, no copyrighted assets —
placeholders are generated procedurally so a profile without art still looks
intentional.

The palette is a near-black canvas, soft
charcoal panels, hairline borders and a single blue accent. Widgets opt into the
stylesheet with ``setObjectName``.
"""

from __future__ import annotations

import hashlib
from collections.abc import Iterable
from pathlib import Path

from PyQt6.QtCore import QRectF, Qt
from PyQt6.QtGui import (
    QBrush,
    QColor,
    QFont,
    QIcon,
    QLinearGradient,
    QPainter,
    QPainterPath,
    QPixmap,
    QRadialGradient,
)

# ---- palette -----------------------------------------------------------------
BG = "#07090d"
PANEL = "#0c0f15"
PANEL_2 = "#10141c"
PANEL_3 = "#151a24"
ELEV = "#1a2030"
LINE = "#1c2433"
LINE_SOFT = "#141a25"

TEXT = "#e6eaf2"
TEXT_SOFT = "#c3cbd9"
MUTED = "#8b93a6"
DIM = "#69738c"

#: Cool-tinted neutrals. Every grey in the stylesheet is one of these so the
#: whole surface keeps the same faint blue cast instead of drifting warm.
BORDER = "#2a3446"
BORDER_HI = "#3a4761"
BUTTON_TEXT = "#cfd6e2"
DISABLED_TEXT = "#4d566b"
DISABLED_BG = "#0e121a"
FIELD_BG = "#0e1219"
CONSOLE_BG = "#080b11"
SCROLL = "#232c3d"
SCROLL_HI = "#323d52"

ACCENT = "#3d8bfd"
ACCENT_HI = "#6ea8ff"
ACCENT_DEEP = "#2a6bd4"
ACCENT_INK = "#08131f"

OK = ACCENT
WARN = "#f5c86a"
BAD = "#ff8a8a"
INFO = "#8ab4ff"

CARD_RADIUS = 14

#: Object-name based stylesheet. Widgets opt in with ``setObjectName``.
#: Tick drawn inside a checked box (dark ink on the accent). Without the file the
#: box still shows as accent-filled, just without the mark.
_CHECK_ICON = Path(__file__).resolve().parent.parent / "assets" / "icons" / "check.svg"
_CHECK_IMAGE = f"image: url({_CHECK_ICON.as_posix()});" if _CHECK_ICON.is_file() else ""

STYLESHEET = f"""
* {{
    color: {TEXT};
    font-size: 13px;
    font-family: "Inter", "Cantarell", "Noto Sans", "Segoe UI", sans-serif;
}}
QMainWindow, QWidget#Root, QWidget#Page {{ background: {BG}; }}
QStackedWidget {{ background: {BG}; }}
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
    background: {PANEL};
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
QLabel#GameRowTitle {{ color: {MUTED}; font-size: 12px; font-weight: 600; }}
QLabel#GameRowTitle[active="true"] {{ color: {ACCENT}; }}
QFrame#GameRow:hover QLabel#GameRowTitle {{ color: {TEXT}; }}
QLabel#GameRowIcon {{ background: {PANEL_3}; border-radius: 8px; }}
QLabel#GameRowDot {{ background: {ACCENT}; border-radius: 4px; }}
QLabel#GameRowDot[kind="update"] {{ background: {WARN}; }}

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
QFrame#GameCard {{
    background: {PANEL_2};
    border: 1px solid {LINE};
    border-radius: {CARD_RADIUS}px;
}}
QFrame#GameCard:hover {{
    background: {PANEL_3};
    border: 1px solid #27446b;
}}
QFrame#GameCard[running="true"] {{ border: 1px solid rgba(61, 139, 253, 0.45); }}
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
QPushButton#Danger:hover {{ background: rgba(255, 138, 138, 0.22); }}
QPushButton#Danger:pressed {{ background: rgba(255, 138, 138, 0.30); }}

/* ---- inputs ----------------------------------------------------------- */
QLineEdit, QComboBox, QSpinBox {{
    background: {FIELD_BG};
    border: 1px solid {LINE};
    border-radius: 8px;
    padding: 8px 11px;
    color: {TEXT};
    min-height: 18px;
    selection-background-color: {ACCENT};
    selection-color: {ACCENT_INK};
}}
QLineEdit:hover, QComboBox:hover, QSpinBox:hover {{ border-color: {BORDER_HI}; }}
QLineEdit:focus, QComboBox:focus, QSpinBox:focus {{ border: 1px solid {ACCENT}; }}
QLineEdit:read-only, QLineEdit:disabled {{ color: {MUTED}; background: {PANEL}; }}
QComboBox::drop-down {{ border: none; width: 26px; }}
QComboBox::down-arrow {{
    image: none;
    width: 0; height: 0;
    border-left: 4px solid transparent;
    border-right: 4px solid transparent;
    border-top: 5px solid {MUTED};
    margin-right: 9px;
}}
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
QCheckBox::indicator:checked {{ background: {ACCENT}; border-color: {ACCENT}; {_CHECK_IMAGE} }}
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


def stylesheet() -> str:
    """Return the launcher stylesheet."""
    return STYLESHEET


FONTS_DIR = Path(__file__).resolve().parent.parent / "assets" / "fonts"


def register_fonts() -> None:
    """Load the bundled Inter font so metrics are identical on every distro.

    The theme asks for Inter first; without it a wider fallback face made
    dialogs clip on distros that do not ship Inter. Needs a Qt application.
    Idempotent per application (a new QApplication starts with an empty
    font database, so this checks the database, not a module flag).
    """
    from PyQt6.QtGui import QFontDatabase, QGuiApplication

    if QGuiApplication.instance() is None or "Inter" in QFontDatabase.families():
        return
    for path in sorted(FONTS_DIR.glob("*.ttf")):
        QFontDatabase.addApplicationFont(str(path))


def apply_theme(app) -> None:
    """Apply the stylesheet to a ``QApplication`` (or any object with setStyleSheet)."""
    try:
        register_fonts()
    except Exception:  # noqa: BLE001 - a missing font must never block startup
        pass
    app.setStyleSheet(STYLESHEET)


def repolish(widget) -> None:
    """Re-evaluate the stylesheet for ``widget`` after a property change."""
    style = widget.style()
    style.unpolish(widget)
    style.polish(widget)
    widget.update()


def set_state_property(widget, value: str) -> None:
    """Set the dynamic ``state`` property used by pill selectors and repolish."""
    widget.setProperty("state", value)
    repolish(widget)


# ---- artwork helpers ---------------------------------------------------------
def _seed_colors(seed: str) -> tuple[QColor, QColor, QColor]:
    digest = hashlib.sha1(str(seed).encode("utf-8")).digest()

    # Placeholder art is blue by contract, so constrain every band to ~200-236°
    # and vary hue/saturation/value between seeds to keep cards distinguishable.
    def hue(byte: int) -> float:
        return 0.555 + 0.100 * (byte / 255.0)

    top = QColor.fromHsvF(
        hue(digest[0]),
        0.42 + 0.18 * (digest[3] / 255.0),
        0.58 + 0.12 * (digest[4] / 255.0),
    )
    mid = QColor.fromHsvF(
        hue(digest[1]),
        0.55 + 0.20 * (digest[5] / 255.0),
        0.28 + 0.10 * (digest[6] / 255.0),
    )
    bottom = QColor.fromHsvF(
        hue(digest[2]),
        0.62 + 0.20 * (digest[7] / 255.0),
        0.10 + 0.08 * (digest[8] / 255.0),
    )
    return top, mid, bottom


def _initial(seed: str) -> str:
    for char in str(seed).strip():
        if char.isalnum():
            return char.upper()
    return "N"


def placeholder_art(width: int, height: int, seed: str) -> QPixmap:
    """A deterministic gradient placeholder for a profile without artwork.

    The gradient is derived from the seed (usually the profile name or title),
    then a soft vignette and a large translucent monogram keep it from reading
    as a flat swatch.
    """
    width = max(1, int(width))
    height = max(1, int(height))
    pixmap = QPixmap(width, height)
    top, mid, bottom = _seed_colors(seed)
    painter = QPainter(pixmap)
    painter.setRenderHint(QPainter.RenderHint.Antialiasing, True)

    gradient = QLinearGradient(0, 0, width, height)
    gradient.setColorAt(0.0, top)
    gradient.setColorAt(0.55, mid)
    gradient.setColorAt(1.0, bottom)
    painter.fillRect(0, 0, width, height, QBrush(gradient))

    # Diagonal sheen so the surface catches the eye.
    sheen = QLinearGradient(0, 0, width, height)
    sheen.setColorAt(0.0, QColor(255, 255, 255, 30))
    sheen.setColorAt(0.45, QColor(255, 255, 255, 0))
    painter.fillRect(0, 0, width, height, QBrush(sheen))

    # Oversized monogram, clipped to the lower-right.
    font = QFont("Inter")
    font.setPixelSize(max(24, int(height * 0.52)))
    font.setWeight(QFont.Weight.Black)
    painter.setFont(font)
    painter.setPen(QColor(255, 255, 255, 26))
    painter.drawText(
        QRectF(-width * 0.08, height * 0.30, width * 1.15, height * 0.9),
        Qt.AlignmentFlag.AlignCenter,
        _initial(seed),
    )

    # Vignette to anchor the corners.
    vignette = QRadialGradient(width * 0.5, height * 0.42, max(width, height) * 0.85)
    vignette.setColorAt(0.55, QColor(0, 0, 0, 0))
    vignette.setColorAt(1.0, QColor(0, 0, 0, 120))
    painter.fillRect(0, 0, width, height, QBrush(vignette))

    painter.end()
    return pixmap


def rounded_pixmap(source: QPixmap, width: int, height: int, radius: int = 10) -> QPixmap:
    """Scale ``source`` to cover ``width``x``height`` and round its corners."""
    width = max(1, int(width))
    height = max(1, int(height))
    if source is None or source.isNull():
        scaled = placeholder_art(width, height, "nadare")
    else:
        scaled = source.scaled(
            width,
            height,
            Qt.AspectRatioMode.KeepAspectRatioByExpanding,
            Qt.TransformationMode.SmoothTransformation,
        )
    # Center-crop to the exact target size.
    x = max(0, (scaled.width() - width) // 2)
    y = max(0, (scaled.height() - height) // 2)
    scaled = scaled.copy(x, y, width, height)

    out = QPixmap(width, height)
    out.fill(Qt.GlobalColor.transparent)
    painter = QPainter(out)
    painter.setRenderHint(QPainter.RenderHint.Antialiasing, True)
    path = QPainterPath()
    path.addRoundedRect(QRectF(0, 0, width, height), radius, radius)
    painter.setClipPath(path)
    painter.drawPixmap(0, 0, scaled)
    painter.end()
    return out


def cover_pixmap(
    source: QPixmap, width: int, height: int, fallback_seed: str, radius: int = 10
) -> QPixmap:
    """Like :func:`rounded_pixmap` but generates a placeholder when source is empty."""
    if source is None or source.isNull():
        source = placeholder_art(width, height, fallback_seed)
    return rounded_pixmap(source, width, height, radius)


def load_card_pixmap(paths: Iterable[str | None]) -> QPixmap | None:
    """Like :func:`load_pixmap`, for a cover/card: drops a baked-in news banner."""
    from .images import banner_trim_for

    for candidate in paths:
        if not candidate:
            continue
        pixmap = QPixmap(str(candidate))
        if pixmap.isNull():
            continue
        trim = banner_trim_for(candidate)
        if trim:
            pixmap = pixmap.copy(0, 0, pixmap.width(), max(1, int(pixmap.height() * (1 - trim))))
        return pixmap
    return None


def load_pixmap(paths: Iterable[str | None]) -> QPixmap | None:
    """Return the first readable pixmap among ``paths``; ``None`` if none load."""
    for candidate in paths:
        if not candidate:
            continue
        pixmap = QPixmap(str(candidate))
        if not pixmap.isNull():
            return pixmap
    return None


#: The shared snowflake mark: the app icon and the sidebar glyph render from it.
BRAND_SVG = Path(__file__).resolve().parent.parent / "assets" / "nadare.svg"


def brand_pixmap(size: int) -> QPixmap:
    """Render the Nadare snowflake mark from the SVG at ``size`` pixels."""
    from PyQt6.QtSvg import QSvgRenderer

    pixmap = QPixmap(size, size)
    pixmap.fill(Qt.GlobalColor.transparent)
    renderer = QSvgRenderer(str(BRAND_SVG))
    if not renderer.isValid():
        return pixmap
    painter = QPainter(pixmap)
    painter.setRenderHint(QPainter.RenderHint.Antialiasing, True)
    renderer.render(painter)
    painter.end()
    return pixmap


def app_icon() -> QIcon:
    """The window/application icon, rendered from the SVG at the usual sizes."""
    icon = QIcon()
    for size in (16, 24, 32, 48, 64, 128, 256):
        icon.addPixmap(brand_pixmap(size))
    return icon


#: ``state`` variants understood by the ``QLabel#Pill`` selectors.
BADGE_STATES = {
    "running": ("good", "RUNNING"),
    "installed": ("good", "READY"),
    "installing": ("info", "INSTALLING"),
    "updating": ("info", "UPDATING"),
    "launching": ("info", "LAUNCHING"),
    "login": ("info", "LOGIN"),
    "verifying": ("info", "VERIFY"),
    "not_installed": ("muted", "NOT INSTALLED"),
    "error": ("bad", "ERROR"),
}


def badge_style(state: str) -> tuple[str, str]:
    """Map a backend state to ``(pill_state, label)`` for a badge label."""
    state = (state or "").strip().lower()
    if state in BADGE_STATES:
        return BADGE_STATES[state]
    return "warn", state.upper() or "UNKNOWN"
