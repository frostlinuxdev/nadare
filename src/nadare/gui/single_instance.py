# SPDX-License-Identifier: GPL-3.0-or-later
# Copyright (C) 2026 Nadare contributors
"""Per-user single-instance guard for the GUI.

The first GUI process listens on a Unix-domain socket named after the user id;
each later process connects, sends ``activate`` and exits so the running window
is raised instead of a second one opening. The CLI never touches this: only
``nadare gui`` / the menu entry acquire the lock.
"""

from __future__ import annotations

import logging
import os
from pathlib import Path

from PyQt6.QtCore import QObject, pyqtSignal
from PyQt6.QtNetwork import QLocalServer, QLocalSocket

log = logging.getLogger(__name__)

#: How long to wait for a socket connect / write before treating it as stale.
_CONNECT_TIMEOUT_MS = 300

#: The payload a later instance sends to the primary to ask it to activate.
_ACTIVATE_MESSAGE = b"activate"


def server_name() -> str:
    """The per-user local-server name.

    An absolute socket under ``$XDG_RUNTIME_DIR`` (per-user, mode 0700) is
    preferred so the lock is not world-writable; a bare ``nadare-<uid>`` name
    (which Qt resolves under the shared temp dir) is only used as a fallback
    when no runtime dir is available.
    """
    try:
        uid = os.getuid()
    except AttributeError:  # pragma: no cover - non-POSIX
        uid = 0
    runtime = os.environ.get("XDG_RUNTIME_DIR")
    if runtime:
        base = Path(runtime)
        # A stale/misconfigured runtime dir must not stop the GUI opening: only
        # use the absolute socket when the directory is actually usable.
        try:
            usable = base.is_dir() and os.access(base, os.W_OK | os.X_OK)
        except OSError:
            usable = False
        if usable:
            return str(base / f"nadare-{uid}.sock")
    return f"nadare-{uid}"


class SingleInstance(QObject):
    """Own the GUI lock and emit :attr:`activation_requested` for later starts.

    Call :meth:`start`: ``True`` means this process is the primary and must keep
    the object alive for the window's lifetime; ``False`` means another instance
    is running and has been asked to activate itself.
    """

    activation_requested = pyqtSignal()

    def __init__(self, name: str | None = None, parent: QObject | None = None) -> None:
        super().__init__(parent)
        self._name = name or server_name()
        self._server: QLocalServer | None = None
        self._connections: list[QLocalSocket] = []
        #: Bytes read per connection so a split ``activate`` payload reassembles.
        self._buffers: dict[QLocalSocket, bytearray] = {}
        #: Why the last :meth:`start` returned ``False`` (``""`` on success).
        self.status = ""

    @property
    def name(self) -> str:
        return self._name

    def start(self) -> bool:
        """Try to become the primary instance. Returns ``False`` if another runs."""
        self.status = ""
        if self._ping():
            self.status = "another instance is already running"
            return False
        # Do not unlink the socket up front: another process may have bound it
        # since the ping above. Try to bind; only a provably stale socket (no
        # live server answers a second ping) may be reclaimed.
        server = QLocalServer(self)
        server.newConnection.connect(self._on_new_connection)
        if not server.listen(self._name):
            if self._ping():
                server.close()
                self.status = "another instance is already running"
                return False
            QLocalServer.removeServer(self._name)
            if not server.listen(self._name):
                # Never fail silently: a missing window with no message is
                # indistinguishable from a launcher that does nothing.
                self.status = f"could not create the single-instance lock {self._name!r}"
                log.warning(
                    "single-instance lock unavailable at %r: %s",
                    self._name,
                    server.errorString(),
                )
                return False
        self._server = server
        return True

    def close(self) -> None:
        """Stop listening and remove the socket so a fresh start is possible."""
        for connection in self._connections:
            connection.abort()
        self._connections.clear()
        if self._server is not None:
            self._server.close()
            QLocalServer.removeServer(self._name)
            self._server = None

    # ---- internals ---------------------------------------------------------
    def _ping(self) -> bool:
        """Send ``activate`` to a running instance. ``True`` if one answered."""
        socket = QLocalSocket(self)
        socket.connectToServer(self._name)
        if not socket.waitForConnected(_CONNECT_TIMEOUT_MS):
            return False
        socket.write(_ACTIVATE_MESSAGE)
        socket.flush()
        socket.waitForBytesWritten(_CONNECT_TIMEOUT_MS)
        socket.disconnectFromServer()
        socket.deleteLater()
        return True

    def _on_new_connection(self) -> None:
        if self._server is None:
            return
        while self._server.hasPendingConnections():
            connection = self._server.nextPendingConnection()
            if connection is None:
                continue
            self._connections.append(connection)
            connection.readyRead.connect(lambda c=connection: self._consume(c))
            connection.disconnected.connect(lambda c=connection: self._drop(c))
            if connection.bytesAvailable():
                # The activation bytes can arrive before readyRead is connected.
                self._consume(connection)

    def _consume(self, connection: QLocalSocket) -> None:
        """Read a connection and activate only for a valid ``activate`` payload.

        Any local process can connect to the socket, so a bare connection (or
        arbitrary bytes) must never raise the window. Bytes are accumulated per
        connection so a payload split across reads (``act`` then ``ivate``) is
        reassembled instead of being dropped.
        """
        buffer = self._buffers.setdefault(connection, bytearray())
        buffer.extend(bytes(connection.readAll()))
        data = bytes(buffer).strip()
        if data == _ACTIVATE_MESSAGE:
            self._buffers.pop(connection, None)
            connection.disconnectFromServer()
            self.activation_requested.emit()
            return
        if _ACTIVATE_MESSAGE.startswith(data):
            # A (still empty or partial) prefix: wait for the rest.
            return
        # Anything that can never grow into the message is refused.
        self._buffers.pop(connection, None)
        connection.disconnectFromServer()

    def _drop(self, connection: QLocalSocket) -> None:
        self._buffers.pop(connection, None)
        if connection in self._connections:
            self._connections.remove(connection)
        connection.deleteLater()


__all__ = ["SingleInstance", "server_name"]
