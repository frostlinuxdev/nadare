# SPDX-License-Identifier: GPL-3.0-or-later
# Copyright (C) 2026 Nadare contributors
"""File logging with rotation and secret masking.

Everything under Nadare logs to ``core.paths.logs_dir()/nadare.log``. A filter
runs :func:`core.events.mask` over every record before it is formatted, so a
token that sneaks into a message or argument is reduced to ``first4…last4`` and
never written verbatim. Console output is opt-in (the CLI prints Events itself).
"""

from __future__ import annotations

import contextlib
import logging
import logging.handlers
import os
import threading
from pathlib import Path
from typing import Any

from . import paths
from .events import _REDACTED, is_secret_key, mask

LOGGER_NAME = "nadare"
DEFAULT_FILE = "nadare.log"
MAX_BYTES = 2 * 1024 * 1024
BACKUP_COUNT = 5

_lock = threading.Lock()
_configured = False


class _RotatingFileHandler(logging.handlers.RotatingFileHandler):
    """A rotating handler that creates its directory when first opened.

    :func:`setup` deliberately does not touch the filesystem; the directory is
    only created when the first record is emitted. With ``delay=True`` this
    means importing a module never creates ``$XDG_STATE_HOME`` — important so
    tests can redirect it after import and never write to the real state dir.
    """

    def _open(self):
        Path(self.baseFilename).parent.mkdir(parents=True, exist_ok=True)
        stream = super()._open()
        # Logs carry paths and any secret that outran the masker; keep them
        # private like ``secrets.json`` rather than relying on the process umask.
        with contextlib.suppress(OSError):
            os.chmod(self.baseFilename, 0o600)
        return stream

    def handleError(self, record: logging.LogRecord) -> None:
        # The default prints a full "--- Logging error ---" traceback for every
        # record when the state dir is read-only or full. Switch to stderr once
        # instead, and leave ``_configured`` unset so a later setup can retry.
        with contextlib.suppress(Exception):
            self.close()
        _switch_to_fallback(self, record)


def _parent_writable(path: Path) -> bool:
    """Best-effort check before handing the path to a delayed file handler."""
    parent = path.parent
    while not parent.exists() and parent != parent.parent:
        parent = parent.parent
    if path.exists() and not os.access(path, os.W_OK):
        return False
    return os.access(parent, os.W_OK)


class MaskingFilter(logging.Filter):
    """Mask secret-looking values in the message and its arguments."""

    def filter(self, record: logging.LogRecord) -> bool:
        if isinstance(record.msg, str) and not isinstance(record.args, dict):
            try:
                record.msg = mask(record.getMessage())
                record.args = ()
                return True
            except Exception:  # noqa: BLE001 - bad format: mask pieces separately
                pass
        if isinstance(record.msg, str):
            record.msg = mask(record.msg)
        if record.args:
            if isinstance(record.args, dict):
                record.args = {
                    k: (
                        _REDACTED
                        if is_secret_key(k)
                        else (mask(v) if isinstance(v, str) else v)
                    )
                    for k, v in record.args.items()
                }
            else:
                record.args = tuple(mask(a) if isinstance(a, str) else a for a in record.args)
        return True


def _fallback_handler(
    logger: logging.Logger,
    formatter: logging.Formatter | None,
    filters: list[logging.Filter],
) -> logging.Handler:
    """Return the logger's stderr fallback handler, adding it once if needed."""
    for handler in logger.handlers:
        if getattr(handler, "_nadare_fallback", False):
            return handler
        if isinstance(handler, logging.StreamHandler) and not isinstance(
            handler, logging.FileHandler
        ):
            return handler
    stream = logging.StreamHandler()
    stream.setFormatter(formatter)
    for masking in filters:
        stream.addFilter(masking)
    stream._nadare_fallback = True  # type: ignore[attr-defined]
    logger.addHandler(stream)
    return stream


def _remove_fallback(logger: logging.Logger) -> None:
    for handler in list(logger.handlers):
        if getattr(handler, "_nadare_fallback", False):
            logger.removeHandler(handler)
            handler.close()


def _switch_to_fallback(broken: _RotatingFileHandler, record: logging.LogRecord) -> None:
    """Drop a failed file handler and emit this record to stderr instead."""
    global _configured
    logger = logging.getLogger(LOGGER_NAME)
    with _lock:
        for handler in list(logger.handlers):
            if handler is broken or isinstance(handler, logging.FileHandler):
                logger.removeHandler(handler)
        stream = _fallback_handler(logger, broken.formatter, list(broken.filters))
        _configured = False
    stream.handle(record)


def setup(
    level: int | str | None = None,
    *,
    filename: str | Path | None = None,
    console: bool = False,
    max_bytes: int = MAX_BYTES,
    backup_count: int = BACKUP_COUNT,
) -> logging.Logger:
    """Configure the ``nadare`` root logger once; safe to call repeatedly.

    A later call that names a ``level`` still applies it (``-v`` runs after the
    import-time setup), but never reattaches handlers.
    """
    global _configured
    logger = logging.getLogger(LOGGER_NAME)
    with _lock:
        if _configured:
            if level is not None:
                logger.setLevel(level if isinstance(level, int) else logging.getLevelName(level))
            return logger
        if level is None:
            level = logging.INFO
        logger.setLevel(level if isinstance(level, int) else logging.getLevelName(level))
        logger.propagate = False

        path = Path(filename) if filename else (paths.logs_dir() / DEFAULT_FILE)

        formatter = logging.Formatter(
            "%(asctime)s %(levelname)-7s %(name)s: %(message)s",
            datefmt="%Y-%m-%d %H:%M:%S",
        )
        masking = MaskingFilter()

        attached = False
        file_ok = False
        try:
            if not _parent_writable(path):
                raise OSError(f"log folder is not writable: {path.parent}")
            file_handler = _RotatingFileHandler(
                path,
                maxBytes=max_bytes,
                backupCount=backup_count,
                encoding="utf-8",
                delay=True,
            )
            file_handler.setFormatter(formatter)
            file_handler.addFilter(masking)
            logger.addHandler(file_handler)
            file_ok = True
        except OSError:
            # A read-only state dir must not take the launcher down; a stderr
            # fallback below keeps the record visible without a traceback.
            pass

        if file_ok:
            _remove_fallback(logger)
            attached = True
        elif not console:
            _fallback_handler(logger, formatter, [masking])

        if console:
            _remove_fallback(logger)
            stream_handler = logging.StreamHandler()
            stream_handler.setFormatter(formatter)
            stream_handler.addFilter(masking)
            logger.addHandler(stream_handler)
            attached = True

        _configured = attached
    return logger


def reset() -> None:
    """Remove and close the ``nadare`` handlers, allowing a fresh :func:`setup`.

    Used by the test suite to re-point the file handler at a per-test tmp dir;
    production code never needs it.
    """
    global _configured
    logger = logging.getLogger(LOGGER_NAME)
    with _lock:
        for handler in list(logger.handlers):
            logger.removeHandler(handler)
            try:
                handler.close()
            except Exception:  # noqa: BLE001 - closing must never raise
                pass
        _configured = False


def get_logger(name: str | None = None) -> logging.Logger:
    """Return a child logger, configuring the root on first use."""
    setup()
    if not name or name == LOGGER_NAME:
        return logging.getLogger(LOGGER_NAME)
    return logging.getLogger(
        name if name.startswith(LOGGER_NAME + ".") else f"{LOGGER_NAME}.{name}"
    )


def log_path() -> Path:
    """The configured log file, or the default location when not yet set up."""
    logger = logging.getLogger(LOGGER_NAME)
    for handler in logger.handlers:
        if isinstance(handler, logging.FileHandler):
            return Path(handler.baseFilename)
    return paths.logs_dir() / DEFAULT_FILE


def log_event(logger: logging.Logger, level: int, message: str, **data: Any) -> None:
    """Log a message plus optional structured data, both masked by the filter."""
    suffix = (" " + " ".join(f"{k}={v}" for k, v in data.items())) if data else ""
    logger.log(level, "%s%s", message, suffix)
