# SPDX-License-Identifier: GPL-3.0-or-later
# Copyright (C) 2026 Nadare contributors
"""Per-user single-instance guard for the GUI.

The first GUI process listens on a Unix-domain socket named after the user id;
each later process connects, sends ``activate`` and exits so the running window
is raised instead of a second one opening. A later start with ``--profile`` then
sends ``show-profile:<name>`` on a second connection, so the running window shows
that game (#624); an older Nadare refuses that line and still activates. The CLI
never touches this: only ``nadare gui`` / the menu entry acquire the lock.
"""

from __future__ import annotations

import logging
import os
import stat
from pathlib import Path

from PyQt6.QtCore import QObject, pyqtSignal
from PyQt6.QtNetwork import QLocalServer, QLocalSocket

log = logging.getLogger(__name__)

#: How long to wait for a socket connect / write before treating it as stale.
_CONNECT_TIMEOUT_MS = 300

#: The payload a later instance sends to the primary to ask it to activate.
_ACTIVATE_MESSAGE = b"activate"

#: ``show-profile:<name>\n`` asks the primary to show a profile; the newline ends the name.
_PROFILE_PREFIX = b"show-profile:"
_PROFILE_LIMIT = len(_PROFILE_PREFIX) + 256


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
    private = _private_socket(uid)
    return private if private is not None else f"nadare-{uid}"


#: A Unix socket path must fit ``sun_path`` (108 bytes on Linux, with its terminating NUL).
_SOCKET_PATH_MAX = 100


def _private_socket(uid: int) -> str | None:
    """A socket in Nadare's own state folder (mode 0700), or ``None`` when that cannot be had.

    Without a runtime dir a bare name lands in the shared temp folder, where another local user
    could create it first and answer our activation (#811).
    """
    from ..core import paths

    folder = paths.state_dir() / "run"
    path = str(folder / f"nadare-{uid}.sock")
    if len(os.fsencode(path)) > _SOCKET_PATH_MAX:
        return None
    try:
        folder.mkdir(parents=True, exist_ok=True, mode=0o700)
        info = folder.lstat()
        if not stat.S_ISDIR(info.st_mode) or info.st_uid != os.getuid():
            return None
        if stat.S_IMODE(info.st_mode) != 0o700:
            folder.chmod(0o700)
    except (OSError, AttributeError):
        return None
    return path


class SingleInstance(QObject):
    """Own the GUI lock and emit :attr:`activation_requested` for later starts.

    Call :meth:`start`: ``True`` means this process is the primary and must keep
    the object alive for the window's lifetime; ``False`` means another instance
    is running and has been asked to activate itself.
    """

    activation_requested = pyqtSignal()
    #: A later start asked for this profile (already checked to be a valid profile name).
    profile_requested = pyqtSignal(str)

    def __init__(
        self, name: str | None = None, parent: QObject | None = None, profile: str | None = None
    ) -> None:
        super().__init__(parent)
        self._name = name or server_name()
        #: Sent to a running primary after activating it, when this start has ``--profile``.
        self._profile = profile or ""
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
            self._send_profile()
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
                self._send_profile()
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
        return self._send(_ACTIVATE_MESSAGE)

    def _send_profile(self) -> None:
        """Ask the running instance to show ``--profile``'s game (a separate connection)."""
        if self._profile and _profile_name(self._profile.encode("utf-8")) is not None:
            self._send(_PROFILE_PREFIX + self._profile.encode("utf-8") + b"\n")

    def _send(self, payload: bytes) -> bool:
        socket = QLocalSocket(self)
        socket.connectToServer(self._name)
        if not socket.waitForConnected(_CONNECT_TIMEOUT_MS):
            return False
        socket.write(payload)
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
        raw = bytes(buffer)
        if raw.startswith(_PROFILE_PREFIX) and raw.endswith(b"\n"):
            self._buffers.pop(connection, None)
            connection.disconnectFromServer()
            name = _profile_name(raw[len(_PROFILE_PREFIX) : -1])
            if name is not None:
                self.activation_requested.emit()
                self.profile_requested.emit(name)
            return
        if (
            _ACTIVATE_MESSAGE.startswith(data)
            or _PROFILE_PREFIX.startswith(raw)
            or (raw.startswith(_PROFILE_PREFIX) and b"\n" not in raw and len(raw) <= _PROFILE_LIMIT)
        ):
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


def _profile_name(raw: bytes) -> str | None:
    """The profile name in a ``show-profile`` request, or ``None`` when it is not a valid one."""
    from ..core.config import valid_profile_name

    try:
        name = raw.decode("utf-8")
    except UnicodeDecodeError:
        return None
    return name if name and valid_profile_name(name) else None


__all__ = ["SingleInstance", "server_name"]
