# SPDX-License-Identifier: GPL-3.0-or-later
# Copyright (C) 2026 Nadare contributors
"""Show a game's terms of service and agree to them only when the user clicks Agree (#795).

Both network steps run on the thread pool (:class:`CallTask`): ``load()`` fetches what is still to
agree to, ``accept()`` sends the agreement. Nothing is sent until the Agree button is pressed.
"""

from __future__ import annotations

import html
from collections.abc import Callable
from typing import Any

from PyQt6.QtCore import QThreadPool, QUrl, pyqtSignal
from PyQt6.QtGui import QDesktopServices
from PyQt6.QtWidgets import (
    QDialog,
    QHBoxLayout,
    QLabel,
    QPushButton,
    QTextBrowser,
    QVBoxLayout,
    QWidget,
)

from .workers import CallTask


class _TextBrowser(QTextBrowser):
    """Shows STOVE's HTML but loads nothing it points at (no images, no style sheets, no files)."""

    def loadResource(self, _kind: int, _url: QUrl) -> None:  # noqa: N802 - Qt override
        return None


class TermsDialog(QDialog):
    """Read the terms, then press **Agree and continue** (or Cancel)."""

    #: Emitted once STOVE confirmed the agreement.
    agreed = pyqtSignal()

    def __init__(
        self,
        game_title: str,
        load: Callable[[], dict[str, Any]],
        accept: Callable[[], None],
        url: str,
        parent: QWidget | None = None,
        *,
        open_url: Callable[[str], object] | None = None,
    ) -> None:
        super().__init__(parent)
        self._load = load
        self._accept = accept
        self._url = url
        self._open_url = open_url or (lambda target: QDesktopServices.openUrl(QUrl(target)))
        self._alive = True
        self._sending = False
        self.setWindowTitle(f"{game_title}: terms of service")
        self.resize(720, 560)

        self.status = QLabel("")
        self.status.setWordWrap(True)
        self.text = _TextBrowser()
        self.text.setOpenLinks(False)
        self.text.setOpenExternalLinks(False)
        self.agree_button = QPushButton("Agree and continue")
        self.agree_button.setEnabled(False)
        # Enter must never press it: the agreement is sent only by an explicit click.
        self.agree_button.setAutoDefault(False)
        self.agree_button.setDefault(False)
        self.cancel_button = QPushButton("Cancel")
        self.browser_button = QPushButton("Open the terms in my browser")

        buttons = QHBoxLayout()
        buttons.addWidget(self.browser_button)
        buttons.addStretch(1)
        buttons.addWidget(self.cancel_button)
        buttons.addWidget(self.agree_button)
        layout = QVBoxLayout(self)
        intro = QLabel(
            f"{html.escape(game_title)} needs this STOVE account to agree to its terms of "
            "service before the game can start. Read them below. Nadare only tells STOVE you "
            "agree when you press <b>Agree and continue</b>."
        )
        intro.setWordWrap(True)
        layout.addWidget(intro)
        layout.addWidget(self.text, 1)
        layout.addWidget(self.status)
        layout.addLayout(buttons)

        self.agree_button.clicked.connect(self._on_agree)
        self.cancel_button.clicked.connect(self.reject)
        self.browser_button.clicked.connect(lambda: self._open_url(self._url))
        self.finished.connect(self._on_finished)

    def _on_finished(self, _result: int) -> None:
        self._alive = False

    def reject(self) -> None:  # noqa: D102 - Escape / the window's close button
        if self._sending:
            return  # the agreement is already on its way: wait for STOVE's answer
        super().reject()

    def _run(self, work: Callable[[], Any], done: Callable[[Any], None]) -> None:
        task = CallTask(work)
        task.signals.done.connect(done)
        task.signals.failed.connect(self._on_failed)
        QThreadPool.globalInstance().start(task)

    # ---- load ---------------------------------------------------------------
    def start(self) -> None:
        """Fetch the terms (call once, after the dialog is shown)."""
        self.status.setText("Loading the terms from STOVE…")
        self._run(self._load, self._on_loaded)

    def _on_loaded(self, result: object) -> None:
        if not self._alive:
            return
        data = result if isinstance(result, dict) else {}
        documents = data.get("documents") if isinstance(data.get("documents"), list) else []
        if not data.get("pending") or not documents:
            self.status.setText(
                "This account has already agreed to the terms. Close this window and try again."
            )
            return
        missing = data.get("missing")
        if isinstance(missing, list) and missing:
            self.status.setText(
                "STOVE also wants you to agree to terms Nadare cannot show. Read and accept "
                "them in your browser instead (the button below)."
            )
            return
        parts = []
        for doc in documents:
            title = html.escape(str(doc.get("title") or "Terms of service"))
            parts.append(f"<h3>{title}</h3>{doc.get('html') or ''}<hr>")
        self.text.setHtml("".join(parts))
        self.status.setText("")
        self.agree_button.setEnabled(True)

    def _on_failed(self, message: str) -> None:
        agreeing = self._sending
        self._sending = False
        if not self._alive:
            return
        self.status.setText(
            f"Could not send your agreement: {message}"
            if agreeing
            else f"Could not load the terms: {message}. You can read and accept them in your "
            "browser instead."
        )
        self.cancel_button.setEnabled(True)
        # Sending can be tried again; a failed load has nothing to agree to yet.
        self.agree_button.setEnabled(agreeing)

    # ---- agree --------------------------------------------------------------
    def _on_agree(self) -> None:
        self.agree_button.setEnabled(False)
        self.cancel_button.setEnabled(False)
        self._sending = True
        self.status.setText("Telling STOVE you agree…")
        self._run(self._accept, self._on_agreed)

    def _on_agreed(self, _result: object) -> None:
        self._sending = False
        if not self._alive:
            return
        self.agreed.emit()
        self.accept()
