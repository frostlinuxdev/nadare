# SPDX-License-Identifier: GPL-3.0-or-later
# Copyright (C) 2026 Nadare contributors
"""Sandbox: let a game open a link in the user's browser, and nothing else.

A game that says "Sign in with Google" asks Wine to open a web link. Wine hands it to the Steam
Runtime's ``steam-runtime-urlopen``, which first writes ``steam://openurl_external/<link>`` to the
Steam client's pipe (``~/.steam/steam.pipe``) and only then tries the desktop portal. Sandbox
hides the home folder and the session bus, so neither exists; this module is the stand-in for the
Steam client's end of that pipe. It reads what the game writes, keeps only plain web links, and
opens them with the system's ``xdg-open``. No D-Bus, no extra program to install.

The game is untrusted: it can write anything to the pipe, so only ``http(s)://host/...`` is ever
opened (never ``file:``, ``steam://run`` or the like), a link is at most a few KB, and the number
and rate of links is capped.
"""

from __future__ import annotations

import ipaddress
import logging
import os
import re
import select
import shutil
import subprocess
import tempfile
import threading
import time
from collections.abc import Callable
from pathlib import Path
from urllib.parse import urlsplit

from . import process

log = logging.getLogger(__name__)

#: Where the Steam Runtime looks for the Steam client's pipe, relative to the home folder.
PIPE_RELATIVE = Path(".steam") / "steam.pipe"

MAX_URL = 2048
MAX_LINE = 4096
_PREFIXES = ("steam://openurl_external/", "steam://openurl/")
#: The helper writes ``'<steam>' 'steam://openurl_external/<link>'``; take the quoted link.
_QUOTED = re.compile(r"'(steam://[^']*)'")
# Only the characters a well-formed URL is made of: no quotes, spaces, control or non-ASCII
# characters, no backslash (browsers read ``\\`` as ``/``, other parsers do not).
_URL_CHARS = re.compile(r"[A-Za-z0-9\-._~:/?#\[\]@!$&()*+,;=%]+")
_LABEL = r"[A-Za-z0-9](?:[A-Za-z0-9-]{0,61}[A-Za-z0-9])?"
_HOST = re.compile(rf"{_LABEL}(?:\.{_LABEL})*")
_OCTET = r"(?:25[0-5]|2[0-4][0-9]|1[0-9]{2}|[1-9]?[0-9])"
_IPV4 = re.compile(rf"{_OCTET}(?:\.{_OCTET}){{3}}")
#: A last label like this makes browsers read the host as a number (``0x7f.1`` is 127.0.0.1).
_NUMERIC_TAIL = re.compile(r"[0-9]+|0[xX][0-9a-fA-F]*")


def _good_host(url: str) -> bool:
    """Whether the link has one plain host that every URL parser agrees on."""
    try:
        parts = urlsplit(url)
        port = parts.port
    except ValueError:
        return False
    if "@" in parts.netloc or (port is not None and port < 1):
        return False  # no user name or password, a real port
    host = parts.hostname or ""
    if parts.netloc.startswith("["):
        try:
            return "%" not in host and isinstance(ipaddress.ip_address(host), ipaddress.IPv6Address)
        except ValueError:
            return False
    if not host or len(host) > 253 or not _HOST.fullmatch(host):
        return False
    if _NUMERIC_TAIL.fullmatch(host.rsplit(".", 1)[-1]):
        return bool(_IPV4.fullmatch(host))
    return True


def parse_request(line: str) -> str | None:
    """The web link in one line from the game, or ``None`` when it is anything else."""
    if not line or len(line) > MAX_LINE:
        return None
    quoted = _QUOTED.search(line)
    candidate = quoted.group(1) if quoted else line.strip()
    for prefix in _PREFIXES:
        if candidate.startswith(prefix):
            url = candidate[len(prefix) :]
            break
    else:
        return None
    if len(url) > MAX_URL or not _URL_CHARS.fullmatch(url):
        return None
    if not url.startswith(("http://", "https://")) or not _good_host(url):
        return None
    return url


def open_in_browser(url: str) -> bool:
    """Open ``url`` with the system's ``xdg-open``, with the same clean environment as a game."""
    tool = shutil.which("xdg-open")
    if tool is None:
        return False
    subprocess.Popen(  # noqa: S603 - fixed program, the link was validated by parse_request
        [tool, url],
        env=process.child_environ(),
        stdin=subprocess.DEVNULL,
        stdout=subprocess.DEVNULL,
        stderr=subprocess.DEVNULL,
        start_new_session=True,
    )
    return True


class LinkRelay:
    """A private pipe the game writes link requests to; a thread opens the valid ones."""

    def __init__(
        self,
        directory: Path | None = None,
        *,
        opener: Callable[[str], object] = open_in_browser,
        min_gap: float = 1.0,
        max_links: int = 20,
        on_open: Callable[[str], None] | None = None,
    ) -> None:
        self._parent = directory
        self._opener = opener
        self._min_gap = min_gap
        self._max_links = max_links
        self._on_open = on_open
        self._dir: Path | None = None
        self._fd = -1
        self._thread: threading.Thread | None = None
        self._stop = threading.Event()
        self._opened = 0
        self._last_url = ""
        self._last_at = float("-inf")

    @property
    def path(self) -> Path:
        assert self._dir is not None, "start() first"
        return self._dir / "steam.pipe"

    def start(self) -> Path:
        self._dir = Path(tempfile.mkdtemp(prefix="nadare-links-", dir=self._parent))
        try:
            self._dir.chmod(0o700)
            os.mkfifo(self.path, 0o600)
            # O_RDWR keeps the pipe from ever reporting end-of-file when a writer closes.
            self._fd = os.open(self.path, os.O_RDWR | os.O_NONBLOCK)
            self._thread = threading.Thread(
                target=self._run, name="nadare-link-relay", daemon=True
            )
            self._thread.start()
        except BaseException:
            # The temp dir and FIFO are already on disk: clean them before the caller drops
            # this object, which would otherwise have no handle left to stop.
            self.stop()
            raise
        return self.path

    def stop(self) -> None:
        self._stop.set()
        if self._thread is not None:
            # ``start()`` may fail in ``Thread.start`` after the temp dir exists: joining a
            # thread that never started raises, and would hide the original error.
            if self._thread.is_alive():
                self._thread.join(timeout=2)
            self._thread = None
        if self._fd >= 0:
            os.close(self._fd)
            self._fd = -1
        if self._dir is not None:
            shutil.rmtree(self._dir, ignore_errors=True)
            self._dir = None

    # ---- reader thread ---------------------------------------------------------------
    def _run(self) -> None:
        buffer = b""
        while not self._stop.is_set():
            ready, _, _ = select.select([self._fd], [], [], 0.2)
            if not ready:
                continue
            try:
                chunk = os.read(self._fd, 4096)
            except (BlockingIOError, InterruptedError):
                continue
            except OSError:
                return
            buffer += chunk
            while b"\n" in buffer:
                raw, buffer = buffer.split(b"\n", 1)
                self._handle(raw.decode("utf-8", errors="replace"))
            if len(buffer) > MAX_LINE:
                buffer = b""  # a line that long is not a link request

    def _handle(self, line: str) -> None:
        url = parse_request(line)
        if url is None or self._opened >= self._max_links:
            return
        now = time.monotonic()
        if url == self._last_url or now - self._last_at < self._min_gap:
            return
        self._opened += 1
        self._last_url, self._last_at = url, now
        try:
            self._opener(url)
        except Exception:  # noqa: BLE001 - a missing browser must not end the relay
            log.debug("could not open a link from the game", exc_info=True)
        if self._on_open is not None:
            try:
                self._on_open(url)
            except Exception:  # noqa: BLE001
                log.debug("link callback failed", exc_info=True)
