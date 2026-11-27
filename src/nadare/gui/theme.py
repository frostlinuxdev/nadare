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
import re
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
DIM = "#7d879f"

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
#: A game that is running right now (sidebar dot and "Playing"). The palette is blue and
#: black with no green (test_theme), so "live" is the bright accent plus the word itself.
LIVE = ACCENT_HI
BAD = "#ff8a8a"
INFO = "#8ab4ff"

CARD_RADIUS = 14


def _font_scale() -> float:
    """How much larger the desktop's text setting is than a stock 10 pt desktop.

    Qt style sheets ignore the application font, so the px sizes below would
    freeze the launcher at one size. Only scale up: a smaller system font must
    not shrink text below the designed size.
    """
    from PyQt6.QtGui import QGuiApplication

    app = QGuiApplication.instance()
    if app is None:
        return 1.0
    font = app.font()
    px = font.pixelSize()
    if px <= 0:
        point = font.pointSizeF()
        if point <= 0:
            return 1.0
        px = point * 4.0 / 3.0  # 96 dpi; the point size already carries OS scaling
    # 10 pt (13.33 px) is what a default KDE/GNOME desktop reports: it keeps the designed sizes.
    return max(1.0, px / (10.0 * 4.0 / 3.0))


#: Layout scale of the library's design pixels (1.0 = the design). Not a setting: tests vary it to
#: prove the layout follows ``scaled()``; text size follows the desktop font (``_font_scale``).
UI_SCALE = 1.0


def scaled(value: float) -> int:
    """Convert design pixels to the layout scale (at least 1 for a non-zero size)."""
    return max(1, round(value * UI_SCALE)) if value else 0


_FONT_SIZE_RE = re.compile(r"font-size:\s*(\d+)px")


def stylesheet() -> str:
    """Return the launcher stylesheet, scaled to the desktop's text size."""
    scale = _font_scale()

    def scaled_font(match: re.Match[str]) -> str:
        return f"font-size: {round(int(match.group(1)) * scale)}px"

    return _FONT_SIZE_RE.sub(scaled_font, globals().get("STYLESHEET") or __getattr__("STYLESHEET"))


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


def _set_sheet(app, sheet: str) -> None:
    """Set ``sheet`` unless it is already set.

    setStyleSheet re-polishes every live widget even for identical text; on the shared test
    application that walked widgets left from earlier tests and crashed Qt in CI (#1024).
    """
    current = getattr(app, "styleSheet", None)
    try:
        if callable(current) and current() == sheet:
            return
    except RuntimeError:  # a deleted Qt object: let setStyleSheet report it as before
        pass
    app.setStyleSheet(sheet)


def apply_theme(app) -> None:
    """Apply the stylesheet to a ``QApplication`` (or any object with setStyleSheet)."""
    try:
        register_fonts()
    except Exception:  # noqa: BLE001 - a missing font must never block startup
        pass
    _set_sheet(app, stylesheet())
    try:
        from .wheelguard import install_for

        install_for(app)  # the wheel scrolls the page, not the dropdown under it
    except Exception:  # noqa: BLE001 - a missing guard must never block startup
        pass
    try:
        # The app font can change at runtime (a desktop "Large Text" toggle).
        from PyQt6.QtCore import QTimer
        from PyQt6.QtWidgets import QApplication

        if isinstance(app, QApplication) and not getattr(app, "_nadare_font_watch", False):
            app._nadare_font_watch = True
            app._nadare_font_pending = False

            def refresh() -> None:
                app._nadare_font_pending = False
                old, new = app.styleSheet(), stylesheet()
                app.setStyleSheet(new)
                # Windows themed with their own copy (the main window) would keep the old size
                # and win over the application's sheet (#1207 b246).
                for widget in app.topLevelWidgets():
                    try:
                        if old and widget.styleSheet() == old:
                            widget.setStyleSheet(new)
                    except RuntimeError:
                        pass  # a widget deleted meanwhile

            def on_font_changed(_font) -> None:
                # Setting the style sheet inside the font-change signal re-enters Qt's font
                # machinery and can crash it, so the refresh runs on the next event-loop turn,
                # once for any number of changes.
                if not app._nadare_font_pending:
                    app._nadare_font_pending = True
                    QTimer.singleShot(0, refresh)

            app.fontChanged.connect(on_font_changed)
    except Exception:  # noqa: BLE001 - cosmetic only
        pass
    try:
        from PyQt6.QtGui import QPalette
        from PyQt6.QtWidgets import QApplication

        if isinstance(app, QApplication):
            # Links in rich-text labels default to dark blue: unreadable on this theme.
            palette = app.palette()
            palette.setColor(QPalette.ColorRole.Link, QColor(ACCENT_HI))
            palette.setColor(QPalette.ColorRole.LinkVisited, QColor(ACCENT_HI))
            app.setPalette(palette)
    except Exception:  # noqa: BLE001 - cosmetic only
        pass


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


#: How far down a wide cover's spare height the banner window starts: key art keeps its logo and
#: faces in the upper part, so a centre crop would cut them off.
_BANNER_TOP_BIAS = 0.15
#: A cover at least this much wider than tall is cropped for the banner; anything squarer or
#: taller is shown whole over a dim, blurred copy of itself.
_BANNER_WIDE_RATIO = 1.2


def banner_from_cover(source: QPixmap, width: int, height: int, radius: int = 10) -> QPixmap:
    """A ``width``x``height`` hero banner made from a cover, for games with no hero art."""
    width = max(1, int(width))
    height = max(1, int(height))
    if source.width() >= source.height() * _BANNER_WIDE_RATIO:
        scaled = source.scaled(
            width,
            height,
            Qt.AspectRatioMode.KeepAspectRatioByExpanding,
            Qt.TransformationMode.SmoothTransformation,
        )
        x = (scaled.width() - width) // 2
        y = int((scaled.height() - height) * _BANNER_TOP_BIAS)
        return rounded_pixmap(scaled.copy(x, y, width, height), width, height, radius)
    backdrop = source.scaled(
        width,
        height,
        Qt.AspectRatioMode.KeepAspectRatioByExpanding,
        Qt.TransformationMode.SmoothTransformation,
    )
    backdrop = backdrop.copy(
        (backdrop.width() - width) // 2, (backdrop.height() - height) // 2, width, height
    )
    # Shrinking and re-growing is a cheap blur.
    small = backdrop.scaled(
        max(1, width // 24),
        max(1, height // 24),
        Qt.AspectRatioMode.IgnoreAspectRatio,
        Qt.TransformationMode.SmoothTransformation,
    )
    canvas = small.scaled(
        width,
        height,
        Qt.AspectRatioMode.IgnoreAspectRatio,
        Qt.TransformationMode.SmoothTransformation,
    )
    painter = QPainter(canvas)
    painter.fillRect(canvas.rect(), QColor(8, 10, 18, 150))
    whole = source.scaledToHeight(height, Qt.TransformationMode.SmoothTransformation)
    painter.drawPixmap((width - whole.width()) // 2, 0, whole)
    painter.end()
    return rounded_pixmap(canvas, width, height, radius)


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
    "running": ("good", "PLAYING"),
    "installed": ("good", "READY"),
    "installing": ("info", "INSTALLING"),
    "waiting": ("muted", "WAITING"),
    "paused": ("muted", "PAUSED"),
    "finished": ("good", "FINISHED"),
    "elsewhere": ("muted", "BUSY ELSEWHERE"),
    "runner": ("info", "PROTON RUNNER"),
    "updating": ("info", "UPDATING"),
    "repairing": ("info", "REPAIRING"),
    "launching": ("info", "LAUNCHING"),
    "login": ("info", "LOGIN"),
    "verifying": ("info", "CHECKING"),
    "cleaning": ("info", "CLEANING UP"),
    "not_installed": ("muted", "NOT INSTALLED"),
    "checking": ("muted", "CHECKING…"),
    "error": ("bad", "ERROR"),
}


def badge_style(state: str) -> tuple[str, str]:
    """Map a backend state to ``(pill_state, label)`` for a badge label."""
    state = (state or "").strip().lower()
    if state in BADGE_STATES:
        return BADGE_STATES[state]
    return "warn", state.upper() or "UNKNOWN"


# The stylesheet and its icon urls live in theme_qss (#1601), which imports the palette above.
# Loaded on first use (PEP 562) so either module can be imported first; ``theme.STYLESHEET``
# and friends are the same objects as in theme_qss.
_QSS_NAMES = frozenset(
    {
        "_ARROW_ICON",
        "_ARROW_IMAGE",
        "_CHECK_ICON",
        "_CHECK_IMAGE",
        "_UP_ARROW_ICON",
        "_UP_ARROW_IMAGE",
        "STYLESHEET",
        "_qss_url",
    }
)


def __getattr__(name: str):
    if name in _QSS_NAMES:
        from . import theme_qss

        value = getattr(theme_qss, name)
        globals()[name] = value  # later reads are plain module globals
        return value
    raise AttributeError(f"module {__name__!r} has no attribute {name!r}")
