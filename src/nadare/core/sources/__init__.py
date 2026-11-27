# SPDX-License-Identifier: GPL-3.0-or-later
# Copyright (C) 2026 Nadare contributors
"""Per-game direct-download sources.

Each module here exposes ``resolve(fetch=None) -> directdl.Plan``: it asks the
publisher's public, unauthenticated API what the latest build is and returns a
:class:`~nadare.core.directdl.Plan`. ``fetch(url, headers=None) -> bytes`` is
injectable so tests run against recorded responses; the default is
:func:`default_fetch`.
"""

from __future__ import annotations

import contextvars
import errno
import http.client
import logging
import math
import random
import socket
import urllib.error
import urllib.parse
import zlib
from collections.abc import Callable, Iterator, Mapping
from contextlib import contextmanager
from typing import Any

from .. import directdl, download
from ..events import Cancel

log = logging.getLogger(__name__)

Fetch = Callable[[str, Mapping[str, str] | None], bytes]

#: A publisher API document is small JSON; a gzip bomb must not exhaust memory.
MAX_UNZIP_BYTES = 64 << 20

#: Backoff before the second and third attempt (#1816: about 2 s, then 8 s).
_FETCH_BACKOFF = (2.0, 8.0)
#: Extra delay drawn from ``[0, JITTER] * delay``, so retries do not line up.
_FETCH_JITTER = 0.2
#: Longest ``Retry-After`` a small index fetch waits; a slow server must not stall a queue.
MAX_FETCH_RETRY_AFTER = 30.0

#: Read errors a retry can fix: a bare type is all some of them carry.
_TRANSIENT_ERRORS = (
    TimeoutError,
    ConnectionRefusedError,
    ConnectionResetError,
    ConnectionAbortedError,
)
#: ``OSError`` errnos a retry can fix (the refused/reset/aborted types are above).
_TRANSIENT_ERRNOS = frozenset(
    {
        errno.ECONNREFUSED,
        errno.ECONNRESET,
        errno.ECONNABORTED,
        errno.ETIMEDOUT,
        errno.ENETUNREACH,
        errno.EHOSTUNREACH,
    }
)
#: ``OSError`` errnos guaranteed to happen before any request byte is sent.
_PRE_SEND_ERRNOS = frozenset(
    {
        errno.ECONNREFUSED,
        errno.ENETUNREACH,
        errno.EHOSTUNREACH,
    }
)
#: A temporary resolver failure is worth a retry; NXDOMAIN/name-not-found is not.
_TEMPORARY_DNS_FAILURE = getattr(socket, "EAI_AGAIN", None)

#: The token a backend operation set with :func:`fetch_cancel`, per thread/task.
_fetch_cancel: contextvars.ContextVar[Cancel | None] = contextvars.ContextVar(
    "nadare_source_fetch_cancel", default=None
)


@contextmanager
def fetch_cancel(cancel: Cancel | None) -> Iterator[None]:
    """Make ``cancel`` the token these fetches obey inside the block (#1816).

    ``DirectBackend.plan()``/``predownload()`` wrap their source calls in this, so Stop
    interrupts a retry wait even though a source's ``resolve()`` only takes a ``Fetch``.
    """
    token = _fetch_cancel.set(cancel)
    try:
        yield
    finally:
        _fetch_cancel.reset(token)


def _cause(exc: download.DownloadError) -> BaseException | None:
    """The network error behind a ``DownloadError``: ``http_open`` wraps it in ``URLError``."""
    cause = exc.__cause__
    if isinstance(cause, urllib.error.URLError) and cause.reason is not None:
        return cause.reason
    return cause


def _transient_error(exc: download.DownloadError) -> bool:
    """Whether an index fetch may succeed when asked again (#1816).

    HTTP status is read from the exception type. A connection-level failure reaches us as
    the ``__cause__`` ``http_open`` set; only timeouts, refused/reset/aborted connections,
    a temporary DNS failure and an unreachable network are retried. A size-cap or
    redirect-policy refusal is raised directly, so it has no cause and is never retried.
    """
    status = getattr(exc, "status", None)
    if status is not None:
        return download.transient_status(int(status))
    cause = _cause(exc)
    if isinstance(cause, _TRANSIENT_ERRORS):
        return True
    if isinstance(cause, socket.gaierror):
        return _TEMPORARY_DNS_FAILURE is not None and cause.errno == _TEMPORARY_DNS_FAILURE
    if isinstance(cause, http.client.IncompleteRead):
        return True  # the body was cut short mid-read
    if isinstance(cause, OSError):
        return cause.errno in _TRANSIENT_ERRNOS
    return False


def _pre_send_error(exc: download.DownloadError) -> bool:
    """Whether a failure happened before request bytes were sent (connect/DNS/TLS setup).

    Used to guard non-idempotent POST retries: retrying after the request may have reached
    the server risks repeating a side effect (#1853).
    """
    if not _transient_error(exc):
        return False
    if getattr(exc, "status", None) is not None:
        return False
    before_send = getattr(exc, "before_send", None)
    if before_send is True:
        return True
    if before_send is False:
        return False
    cause = _cause(exc)
    if isinstance(cause, ConnectionRefusedError):
        return True
    if isinstance(cause, socket.gaierror):
        return _TEMPORARY_DNS_FAILURE is not None and cause.errno == _TEMPORARY_DNS_FAILURE
    if isinstance(cause, OSError):
        return cause.errno in _PRE_SEND_ERRNOS
    return False


def _fetch_reason(exc: download.DownloadError) -> str:
    """A short reason for the retry log line, never the URL (it may carry a key)."""
    status = getattr(exc, "status", None)
    if status is not None:
        return f"HTTP {status}"
    cause = _cause(exc)
    if isinstance(cause, TimeoutError):
        return "timeout"
    if isinstance(cause, (ConnectionResetError, ConnectionAbortedError, ConnectionRefusedError)):
        return "connection error"
    if isinstance(cause, http.client.IncompleteRead):
        return "connection error"
    if isinstance(cause, OSError):
        return "network error"
    return type(cause).__name__ if cause is not None else type(exc).__name__


def _fetch_host(url: str) -> str:
    """The host for a log line: never the URL, whose query string may carry a key."""
    try:
        return urllib.parse.urlsplit(url).hostname or "<invalid url>"
    except ValueError:  # malformed IPv6 brackets and the like
        return "<invalid url>"


def _fetch_delay(exc: download.DownloadError, attempt: int) -> float:
    """Wait before the next attempt: the server's ``Retry-After``, else the schedule."""
    retry_after = getattr(exc, "retry_after", None)
    if retry_after is not None:
        try:
            seconds = float(retry_after)
        except (TypeError, ValueError):
            seconds = math.nan
        # A broken header (NaN/inf/negative) means the normal backoff, not no wait at all.
        if math.isfinite(seconds) and seconds >= 0:
            return min(seconds, MAX_FETCH_RETRY_AFTER)
    base = _FETCH_BACKOFF[min(attempt - 1, len(_FETCH_BACKOFF) - 1)]
    return base * (1.0 + random.random() * _FETCH_JITTER)


def _active_cancel(cancel: Cancel | None) -> Cancel | None:
    """The caller's token, else the one the running backend operation set."""
    return cancel if cancel is not None else _fetch_cancel.get()


def _failure_text(exc: download.DownloadError, url: str) -> str:
    """The failure text without the query string or fragment: they may carry a key."""
    # Every URL in the text: a redirect target or an inner error can name another one.
    text = str(exc).replace(url, download.public_url(url))
    return download.public_text(text)


def _http_request(
    url: str,
    headers: Mapping[str, str] | None,
    max_bytes: int | None,
    redirect_allowed: Callable[[str], bool] | None,
    cancel: Cancel | None,
    body: bytes | None,
) -> bytes:
    kwargs: dict[str, Any] = {"headers": headers}
    if cancel is not None:
        kwargs["cancel"] = cancel
    if max_bytes is not None:
        kwargs["max_bytes"] = max_bytes
    if redirect_allowed is not None:
        kwargs["redirect_allowed"] = redirect_allowed
    if body is None:
        return download.http_get(url, **kwargs)
    return download.http_post(url, body, **kwargs)


def _fetch(
    url: str,
    headers: Mapping[str, str] | None,
    *,
    max_bytes: int | None,
    cancel: Cancel | None,
    redirect_allowed: Callable[[str], bool] | None = None,
    body: bytes | None = None,
    idempotent: bool = False,
) -> bytes:
    cancel = _active_cancel(cancel)
    host = _fetch_host(url)
    # Read at call time, so a test or a profile change is honoured.
    attempts = max(1, int(download.DEFAULT_RETRIES))
    for attempt in range(1, attempts + 1):
        # Before the first attempt and before every retry; a zero Retry-After cannot skip it.
        download._check_cancel(cancel)
        try:
            return _http_request(url, headers, max_bytes, redirect_allowed, cancel, body)
        except download.DownloadError as exc:
            failure = exc
        except (OSError, http.client.HTTPException) as exc:
            # http_get reads the body outside http_open's try, so a timeout or reset while
            # reading it arrives raw; retry it like the connect error it is (#1816 review).
            failure = download.DownloadError(f"cannot reach {download.public_url(url)}: {exc}")
            failure.before_send = False
            failure.__cause__ = download.scrub_exception(exc)
        retryable = (
            _transient_error(failure) if (body is None or idempotent) else _pre_send_error(failure)
        )
        if not retryable or attempt >= attempts:
            # A Stop that arrived during the attempt is why it failed: report the stop.
            download._check_cancel(cancel)
            raise directdl.DirectError(_failure_text(failure, url)) from download.scrub_exception(
                failure
            )
        log.info(
            "fetch retry %d/%d for %s: %s", attempt, attempts, host, _fetch_reason(failure)
        )
        download.wait(_fetch_delay(failure, attempt), cancel)
    raise directdl.DirectError(  # the loop always returns or raises
        f"cannot reach {download.public_url(url)}"
    )


def default_fetch(
    url: str,
    headers: Mapping[str, str] | None = None,
    *,
    cancel: Cancel | None = None,
    redirect_allowed: Callable[[str], bool] | None = None,
) -> bytes:
    """Fetch ``url`` with the shared HTTP stack (size-capped, redirect-safe).

    Transient failures are retried (#1816); a permanent one fails at once.
    """
    return _fetch(
        url,
        headers,
        max_bytes=None,
        cancel=cancel,
        redirect_allowed=redirect_allowed,
    )


#: Kuro's patch lists for a game of many small files are big (PGR 4.8.0 -> 4.9.0: ~20 MB).
MAX_PATCH_LIST_BYTES = 64 << 20


def patch_list_fetch(
    url: str, headers: Mapping[str, str] | None = None, *, cancel: Cancel | None = None
) -> bytes:
    """Like :func:`default_fetch`, with room for a publisher's patch list (#1252)."""
    return _fetch(url, headers, max_bytes=MAX_PATCH_LIST_BYTES, cancel=cancel)


def post_fetch(
    url: str,
    body: bytes,
    headers: Mapping[str, str] | None = None,
    *,
    max_bytes: int = download.MAX_TEXT_BYTES,
    cancel: Cancel | None = None,
    redirect_allowed: Callable[[str], bool] | None = None,
    idempotent: bool = False,
) -> bytes:
    """POST ``body`` through the same retry/cancel/wrapping path as :func:`default_fetch`.

    By default (``idempotent=False``), a POST is retried only when the failure occurred
    before the request was sent (DNS lookup, TCP connect or TLS setup), never after the
    request may have reached the server. Pass ``idempotent=True`` only for read-only or
    otherwise idempotent POST queries (such as Reverse: 1999's update service).
    """
    return _fetch(
        url,
        headers,
        max_bytes=max_bytes,
        cancel=cancel,
        redirect_allowed=redirect_allowed,
        body=body,
        idempotent=idempotent,
    )


def ungzip(data: bytes, *, limit: int = MAX_UNZIP_BYTES) -> bytes:
    """Return ``data`` decompressed when it starts with the gzip magic.

    Every gzip member is read, the decompressed size (all members together) is capped at
    ``limit``, and a truncated or corrupt stream becomes a
    :class:`~nadare.core.directdl.DirectError`, keeping the source contract.
    """
    if data[:2] != b"\x1f\x8b":
        return data
    out = bytearray()
    rest = data
    try:
        while rest:
            decompressor = zlib.decompressobj(16 + zlib.MAX_WBITS)
            out += decompressor.decompress(rest, limit + 1 - len(out))
            if len(out) > limit:
                raise directdl.DirectError(f"gzip payload exceeds {limit} bytes")
            if not decompressor.eof:
                raise directdl.DirectError("truncated gzip payload")
            rest = decompressor.unused_data
            if not rest.strip(b"\x00"):
                break  # zero padding after the last member
            if rest[:2] != b"\x1f\x8b":
                raise directdl.DirectError("corrupt gzip payload: unexpected data after the end")
    except zlib.error as exc:
        raise directdl.DirectError(f"corrupt gzip payload: {exc}") from download.scrub_exception(
            exc
        )
    return bytes(out)
