# SPDX-License-Identifier: GPL-3.0-or-later
# Copyright (C) 2026 Nadare contributors
"""The shared safe opener for external web links."""
from PyQt6.QtCore import QUrl
from PyQt6.QtGui import QDesktopServices


def open_external(url: str) -> None:
    if not url:
        return
    qurl = QUrl(url)
    if qurl.scheme().lower() not in ("http", "https") or not qurl.host():
        return
    QDesktopServices.openUrl(qurl)
