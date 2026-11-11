# SPDX-License-Identifier: GPL-3.0-or-later
"""#842: the AppImage build drops what Nadare never loads and the libraries' debug info."""

from __future__ import annotations

from pathlib import Path

SCRIPT = Path("scripts/build_appimage.sh").read_text(encoding="utf-8")


def test_debug_info_is_stripped_but_dynamic_symbols_stay():
    assert "strip --strip-debug" in SCRIPT
    # --strip-all only on libpython, never on every library (it would remove nothing the
    # loader needs, but a blanket strip-all is the risky kind of change this avoids).
    assert SCRIPT.count("strip --strip-all") == 1 and 'libpython3*' in SCRIPT


def test_qt_pdf_and_the_zstd_cffi_backend_are_dropped():
    assert "libQt6Pdf.so.6" in SCRIPT and "libqpdf.so" in SCRIPT
    assert "zstandard/_cffi" in SCRIPT


def test_nothing_in_nadare_uses_qt_pdf():
    hits = [
        p for p in Path("src/nadare").rglob("*.py")
        if "QtPdf" in p.read_text(encoding="utf-8") or "QPdf" in p.read_text(encoding="utf-8")
    ]
    assert hits == []


def test_the_spec_asks_for_zstandards_c_backend():
    spec = Path("packaging/nadare.spec").read_text(encoding="utf-8")
    assert '"zstandard.backend_c"' in spec
