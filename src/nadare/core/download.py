# SPDX-License-Identifier: GPL-3.0-or-later
# Copyright (C) 2026 Nadare contributors
"""Resumable, verified HTTP downloads.

Every download lands in ``<dest>.part`` and is only renamed onto ``dest`` once it
is complete and its size/hashes check out, so an interrupted or tampered run
never leaves a half-written game file in place. Retries use exponential backoff;
a server that supports ``Range`` lets an interrupted download resume with
``Content-Range``. Only the Python standard library is required.
"""

from __future__ import annotations

import base64
import contextlib
import errno
import hashlib
import http.client
import json
import math
import os
import random
import re
import socket
import threading
import time
import urllib.error
import urllib.parse
import urllib.request
from collections.abc import Callable, Iterator, Mapping
from dataclasses import dataclass
from pathlib import Path
from typing import Any, NoReturn

from . import tls, transfer
from .diskspace import human_size
from .events import Cancel, Emitter, Event, emitter

current_transfer = transfer.current_transfer
inherit_transfer = transfer.inherit_transfer

DEFAULT_CHUNK = 1 << 20  # 1 MiB
DEFAULT_TIMEOUT = 60
DEFAULT_RETRIES = 3
DEFAULT_BACKOFF = 2.0
MAX_BACKOFF = 60.0
#: Extra delay drawn uniformly from ``[0, JITTER] * delay``, so a fleet of
#: clients that failed together does not retry in lockstep.
BACKOFF_JITTER = 0.5
#: Longest ``Retry-After`` Nadare will honour (seconds).
RETRY_AFTER_MAX = 60.0
#: 4xx statuses that mean "try again" rather than "this will never work".
_RETRYABLE_4XX = frozenset({408, 425, 429})
#: Read caps: JSON/text fetches, and downloads that declare no expected size.
#: Most of a redirect response's body Nadare will read before discarding it.
REDIRECT_DRAIN_BYTES = 64 << 10
MAX_TEXT_BYTES = 16 << 20
MAX_DOWNLOAD_BYTES = 8 << 30
USER_AGENT = "nadare-launcher/0.1 (+https://github.com/frostlinuxdev/nadare)"

#: Write errors that a retry can never fix: the disk or the destination is the
#: problem, not the network.
_FATAL_WRITE_ERRNOS = frozenset(
    {
        errno.ENOSPC,
        errno.EACCES,
        errno.EROFS,
        getattr(errno, "EDQUOT", errno.ENOSPC),
    }
)

#: Headers carrying credentials that must never follow a redirect off-origin.
CREDENTIAL_HEADERS = frozenset({"authorization", "cookie", "proxy-authorization"})


class DownloadError(RuntimeError):
    """A download failed after exhausting retries, or failed verification.

    Its text reaches the run log and the error dialog, so every URL in it is cut to
    scheme, host and path: publisher query strings, fragments and userinfo carry secrets.
    """

    before_send: bool | None = None

    def __init__(self, *args: object) -> None:
        args = tuple(public_text(a) if isinstance(a, str) else a for a in args)
        super().__init__(*args)


class HTTPStatusError(DownloadError):
    """An HTTP response outside 2xx, carrying the status and ``Retry-After``."""

    def __init__(
        self,
        message: str,
        *,
        status: int,
        retry_after: float | None = None,
        url: str | None = None,
    ) -> None:
        super().__init__(message)
        self.status = status
        valid = retry_after is not None and math.isfinite(retry_after) and retry_after >= 0
        self.retry_after = min(retry_after, RETRY_AFTER_MAX) if valid else None
        self.url = public_url(url) if url is not None else None


class ProxyNotSupported(DownloadError):
    """The environment routes a request through a proxy type the keep-alive pool cannot use."""


class IntegrityError(DownloadError):
    """A completed download did not match the expected hash/size."""


@dataclass
class DownloadResult:
    path: Path
    size: int
    sha256: str
    sha512: str
    resumed: bool = False
    md5: str = ""


# ---------------------------------------------------------------------------
# low-level HTTP
# ---------------------------------------------------------------------------
def _request(
    url: str,
    headers: Mapping[str, str] | None = None,
    data: bytes | None = None,
) -> urllib.request.Request:
    hdrs = {"User-Agent": USER_AGENT, "Accept": "*/*"}
    if headers:
        hdrs.update({str(k): str(v) for k, v in headers.items()})
    return urllib.request.Request(url, data=data, headers=hdrs)


def parse_retry_after(value: str | None) -> float | None:
    """A numeric ``Retry-After`` in seconds, capped at :data:`RETRY_AFTER_MAX`.

    A non-finite (``NaN``/``inf``) or negative value is treated as absent, so a broken
    header means the normal backoff instead of no wait at all.
    """
    if not value:
        return None
    try:
        seconds = float(value.strip())
    except ValueError:  # the HTTP-date form; fall back to normal backoff
        return None
    if not math.isfinite(seconds) or seconds < 0:
        return None
    return min(seconds, RETRY_AFTER_MAX)


def transient_status(status: int) -> bool:
    """Whether an HTTP error status may succeed when asked again (5xx, 408, 425, 429).

    One rule for every transfer, full downloads and ranged reads alike (#1496): a 403/404 is
    permanent, and asking again only wastes time and can look like abuse.
    """
    return status >= 500 or status in _RETRYABLE_4XX


def wait(seconds: float, cancel: Cancel | None) -> None:
    """Sleep ``seconds`` between retries, stopping early with ``Cancelled`` when cancelled."""
    _sleep(seconds, cancel)


def usable_retry_after(exc: BaseException) -> float | None:
    """The server's wait carried by ``exc``, capped, or None when it is absent or broken.

    The constructor of ``HTTPStatusError`` already checks it, but a wrapper or a subclass can set
    the attribute later: NaN, infinity, a negative number or text must mean the normal backoff
    (#1853), never a crash or a wait of no time at all.
    """
    raw = getattr(exc, "retry_after", None)
    if raw is None or isinstance(raw, bool):
        return None
    try:
        seconds = float(raw)  # type: ignore[arg-type]
    except (TypeError, ValueError):
        return None
    if not math.isfinite(seconds) or seconds < 0:
        return None
    return min(seconds, RETRY_AFTER_MAX)


def retry_delay(
    base: float, attempt: int = 1, *, rand: Callable[[], float] = random.random
) -> float:
    """Exponential backoff for ``attempt`` with bounded jitter, capped."""
    delay = min(base * (2 ** max(0, attempt - 1)), MAX_BACKOFF)
    return min(delay * (1.0 + BACKOFF_JITTER * rand()), MAX_BACKOFF)


class RedirectRefused(DownloadError):
    """A redirect the policy refuses (insecure, unparsable or not on the allow-list).

    Asking again gets the same redirect and the same answer, so retry loops fail at once
    instead of waiting out their backoff (2026-11-27).
    """


class _SafeRedirectHandler(urllib.request.HTTPRedirectHandler):
    """A redirect handler that never leaks credentials or downgrades to http.

    ``urllib``'s stock handler copies every non-content header — including
    ``Authorization`` and ``Cookie`` — onto the redirect target regardless of
    origin, and follows ``https``→``http``.  This subclass drops credentials
    whenever the origin changes, refuses an insecure downgrade, and consults an
    optional per-hop ``redirect_allowed`` allow-list callback.  (The
    :class:`HttpClientPool` already implements the same policy by hand.)
    """

    def __init__(self, redirect_allowed: Callable[[str], bool] | None = None) -> None:
        super().__init__()
        self._redirect_allowed = redirect_allowed

    @staticmethod
    def _refuse(fp: Any, message: str) -> NoReturn:
        """Drain and close the abandoned response, then refuse the redirect.

        ``redirect_request`` runs before ``urllib``'s ``http_error_302`` reaches
        its own ``fp.read(); fp.close()``, so raising here would leak the
        response socket (N4). ``fp`` is ``None`` in direct unit-test calls.
        """
        if fp is not None:
            with contextlib.closing(fp):
                with contextlib.suppress(Exception):
                    fp.read(REDIRECT_DRAIN_BYTES)  # bounded: the body is untrusted
        raise RedirectRefused(message)

    def redirect_request(self, req, fp, code, msg, headers, newurl):
        target = urllib.parse.urljoin(req.full_url, newurl)
        try:
            origin = _split_url(req.full_url)[:3]
            tscheme, thost, tport, _ = _split_url(target)
        except DownloadError as exc:
            self._refuse(
                fp, f"refusing redirect from {req.full_url} to {newurl!r}: {exc}"
            )
        if origin[0] == "https" and tscheme != "https":
            self._refuse(fp, f"refusing insecure redirect from {req.full_url} to {target}")
        if self._redirect_allowed is not None and not self._redirect_allowed(target):
            self._refuse(fp, f"redirect to disallowed URL: {target}")
        new = super().redirect_request(req, fp, code, msg, headers, newurl)
        if new is not None and (tscheme, thost, tport) != origin:
            for header in [h for h in new.headers if h.lower() in CREDENTIAL_HEADERS]:
                del new.headers[header]
        return new


def _make_tracked_connection(
    base: type[http.client.HTTPConnection], state: dict[str, bool]
) -> type[http.client.HTTPConnection]:
    """Connection subclass that marks ``state['sent']`` only after connect/TLS completes."""

    class _TrackedConnection(base):  # type: ignore[valid-type, misc]
        def send(self, data: Any) -> None:
            if self.sock is None:
                if self.auto_open:
                    self.connect()
                else:
                    raise http.client.NotConnected()
            state["sent"] = True
            super().send(data)

    return _TrackedConnection


class _TrackedHTTPHandler(urllib.request.HTTPHandler):
    def __init__(self, state: dict[str, bool]) -> None:
        super().__init__()
        self._state = state
        self._conn_cls = _make_tracked_connection(http.client.HTTPConnection, state)

    def http_open(self, req: urllib.request.Request) -> Any:
        self._state["tracked"] = True
        return self.do_open(self._conn_cls, req)


class _TrackedHTTPSHandler(urllib.request.HTTPSHandler):
    def __init__(self, state: dict[str, bool], *, context: Any) -> None:
        super().__init__(context=context)
        self._state = state
        self._conn_cls = _make_tracked_connection(http.client.HTTPSConnection, state)

    def https_open(self, req: urllib.request.Request) -> Any:
        self._state["tracked"] = True
        return self.do_open(
            self._conn_cls, req, context=self._context
        )


def _build_opener(redirect_allowed: Callable[[str], bool] | None) -> Any:
    """An opener with the safe redirect guard and Nadare's pinned CA context."""
    guard = _SafeRedirectHandler(redirect_allowed)
    state = {"tracked": False, "sent": False}
    https_handler = _TrackedHTTPSHandler(state, context=tls.ssl_context())
    http_handler = _TrackedHTTPHandler(state)
    try:
        opener = urllib.request.build_opener(guard, https_handler, http_handler)
    except TypeError as exc:  # a one- or two-handler stub in older tests
        if "argument" not in str(exc):
            raise
        try:
            opener = urllib.request.build_opener(guard, https_handler)
        except TypeError as inner:
            if "argument" not in str(inner):
                raise
            opener = urllib.request.build_opener(guard)
    with contextlib.suppress(AttributeError):
        opener._nadare_send_state = state
    return opener


_URL_IN_TEXT_RE = re.compile(r"[A-Za-z][A-Za-z0-9+.-]*://[^\s'\"<>]+")


def public_text(text: str) -> str:
    """``text`` with every URL in it reduced to :func:`public_url` (no query, fragment, login)."""
    return _URL_IN_TEXT_RE.sub(lambda match: public_url(match.group(0)), text)


def public_url(url: str) -> str:
    """``url`` as it may be logged or shown: scheme, host and path only.

    A publisher URL carries keys and passwords in its query string, and the final error
    text of a failed fetch ends up in the run log and the error dialog.
    """
    try:
        parts = urllib.parse.urlsplit(url)
    except ValueError:  # a malformed IPv6 bracket
        return "<invalid url>"
    host = parts.netloc.rpartition("@")[2]
    if not host:
        return parts.path or "<invalid url>"
    if not parts.scheme:
        return f"{host}{parts.path}"
    return f"{parts.scheme}://{host}{parts.path}"


def scrub_exception(exc: BaseException) -> BaseException:
    """Scrub URL secrets from ``exc`` and every chained cause/context in place (#1853)."""
    seen: set[int] = set()
    stack: list[BaseException | None] = [exc]
    while stack:
        cur = stack.pop()
        if cur is None or id(cur) in seen:
            continue
        seen.add(id(cur))
        stack.append(cur.__cause__)
        stack.append(cur.__context__)
        if isinstance(cur, urllib.error.HTTPError):
            if isinstance(getattr(cur, "url", None), str):
                cur.url = public_url(cur.url)
            if isinstance(getattr(cur, "filename", None), str):
                cur.filename = public_url(cur.filename)
            if isinstance(getattr(cur, "msg", None), str):
                cur.msg = public_text(cur.msg)
        elif isinstance(cur, urllib.error.URLError):
            if isinstance(cur.reason, BaseException):
                stack.append(cur.reason)
            elif isinstance(cur.reason, str):
                cur.reason = public_text(cur.reason)
        if isinstance(cur, OSError):
            if isinstance(cur.strerror, str):
                cur.strerror = public_text(cur.strerror)
            if isinstance(cur.filename, str):
                cur.filename = public_text(cur.filename)
            if isinstance(cur.filename2, str):
                cur.filename2 = public_text(cur.filename2)
        if cur.args:
            cur.args = tuple(public_text(a) if isinstance(a, str) else a for a in cur.args)
    return exc


def http_open(
    url: str,
    *,
    headers: Mapping[str, str] | None = None,
    timeout: int = DEFAULT_TIMEOUT,
    redirect_allowed: Callable[[str], bool] | None = None,
    data: bytes | None = None,
) -> Any:
    """Open a URL, translating HTTP/URL errors into :class:`DownloadError`.

    Without ``data`` the request is a GET; with it, a POST carrying that body.
    Redirects go through :class:`_SafeRedirectHandler`, so credentials never
    follow a cross-origin hop, ``https``→``http`` is refused, and a supplied
    ``redirect_allowed`` allow-list is enforced per hop.
    """
    opener = _build_opener(redirect_allowed)
    send_state = getattr(opener, "_nadare_send_state", None)
    try:
        response = opener.open(_request(url, headers, data), timeout=timeout)  # noqa: S310
        final_url = getattr(response, "geturl", lambda: url)()
        return transfer.Response(response, final_url)
    except DownloadError:
        raise
    except urllib.error.HTTPError as exc:
        err = HTTPStatusError(
            f"HTTP {exc.code} for {public_url(url)}",
            status=exc.code,
            retry_after=parse_retry_after(
                exc.headers.get("Retry-After") if exc.headers else None
            ),
            url=url,
        )
        err.before_send = False
        raise err from scrub_exception(exc)
    except (urllib.error.URLError, OSError, ValueError) as exc:
        err = DownloadError(f"cannot reach {public_url(url)}: {exc}")
        if isinstance(send_state, dict) and send_state.get("tracked"):
            err.before_send = not send_state["sent"]
        raise err from scrub_exception(exc)


def _pool_can_serve(url: str) -> bool:
    """False when the environment routes ``url`` through a proxy the pool cannot use.

    The pool speaks to ``http://`` proxies only. For any other proxy it would silently go
    straight to the origin, so those downloads keep the urllib path.
    """
    scheme, host = urllib.parse.urlparse(url).scheme, urllib.parse.urlparse(url).hostname or ""
    try:
        proxied = bool(urllib.request.getproxies().get(scheme)) and not (
            urllib.request.proxy_bypass(host)
        )
    except ValueError:
        proxied = False
    return not proxied or _proxy_for(scheme, host) is not None


def _open_pooled(
    pool: HttpClientPool,
    url: str,
    *,
    headers: Mapping[str, str] | None,
    timeout: int,
    redirect_allowed: Callable[[str], bool] | None,
) -> HttpResponse:
    """:func:`http_open` over a keep-alive pool: same errors, same 2xx-only rule."""
    try:
        response = pool.open(
            url,
            headers=headers,
            timeout=timeout,
            redirects=10,  # what urllib follows
            redirect_allowed=redirect_allowed,
            strict_proxy=True,
        )
    except DownloadError:
        raise
    except (http.client.HTTPException, OSError, ValueError) as exc:
        raise DownloadError(f"cannot reach {url}: {exc}") from scrub_exception(exc)
    if not 200 <= response.status < 300:
        # Read a small declared error body so the connection can serve the next file. Anything
        # else is not read (it could block until the timeout); closing then drops the connection.
        length = response.header("Content-Length")
        if length and length.isdigit() and int(length) <= REDIRECT_DRAIN_BYTES:
            with contextlib.suppress(Exception):
                response.read(int(length))
        response.close()
        raise HTTPStatusError(
            f"HTTP {response.status} for {url}",
            status=response.status,
            retry_after=parse_retry_after(response.header("Retry-After")),
            url=url,
        )
    return response


#: How often a read waiting for body bytes looks at the cancel token (seconds).
_CANCEL_POLL = 0.05


def _response_socket(response: Any) -> Any:
    """The socket under a urllib response, when it can be found."""
    sock = getattr(getattr(getattr(response, "fp", None), "raw", None), "_sock", None)
    return sock if hasattr(sock, "shutdown") else None


@contextlib.contextmanager
def _stop_shuts_the_socket(sock: Any, cancel: Cancel | None) -> Iterator[None]:
    """Let Stop end a read that is blocked on the network.

    A blocked read cannot be interrupted from the thread that is inside it: a stalled or
    slow-dripping response holds it until the socket timeout. A tiny watcher shuts the
    socket down as soon as Stop is pressed, so the read returns at once and the caller
    turns that into ``Cancelled``.
    """
    if sock is None or cancel is None:
        yield
        return
    stop = threading.Event()

    def watch() -> None:
        while not stop.wait(_CANCEL_POLL):
            if cancel.cancelled:
                with contextlib.suppress(OSError):
                    sock.shutdown(socket.SHUT_RDWR)
                return

    watcher = threading.Thread(target=watch, name="nadare-fetch-stop", daemon=True)
    watcher.start()
    try:
        yield
    finally:
        stop.set()
        watcher.join(timeout=1.0)


def _read_body(response: Any, amount: int, cancel: Cancel | None) -> bytes:
    """Read up to ``amount`` bytes, ending at once when Stop is pressed.

    With a token the body is read in blocks that check it, and the socket watcher ends a
    block that is still waiting for bytes.
    """
    if cancel is None:
        return response.read(amount)
    blocks: list[bytes] = []
    total = 0
    with _stop_shuts_the_socket(_response_socket(response), cancel):
        while total < amount:
            cancel.raise_if_cancelled()
            try:
                block = response.read(min(65536, amount - total))
            except (OSError, ValueError, http.client.HTTPException):
                # Stop shut the socket: TLS can report that as ValueError ("Read on
                # closed or unwrapped SSL socket") as well as OSError.
                if cancel.cancelled:
                    raise _cancelled() from None
                raise
            if not block:
                break
            blocks.append(block)
            total += len(block)
    cancel.raise_if_cancelled()  # a Stop during the last block is not a short body
    return b"".join(blocks)


def _read_bounded(
    resp: Any, url: str, max_bytes: int, cancel: Cancel | None
) -> bytes:
    """Read a bounded HTTP body, turning early EOF or short Content-Length into DownloadError."""
    if cancel is not None and hasattr(resp, "cancel"):
        resp.cancel = cancel
    try:
        data = _read_body(resp, max_bytes + 1, cancel)
    except http.client.IncompleteRead as exc:
        if cancel is not None and cancel.cancelled:
            raise _cancelled() from None
        err = DownloadError(f"incomplete response for {public_url(url)}: {exc}")
        err.before_send = False
        raise err from scrub_exception(exc)
    if len(data) > max_bytes:
        raise DownloadError(
            f"response too large (over {max_bytes} bytes) for {public_url(url)}"
        )
    expected = transfer._declared_length(resp)
    if expected is not None and len(data) < expected:
        short = http.client.IncompleteRead(data, expected - len(data))
        err = DownloadError(
            f"incomplete response for {public_url(url)}: {len(data)} of {expected} bytes"
        )
        err.before_send = False
        raise err from short
    return data


def http_get(
    url: str,
    *,
    headers: Mapping[str, str] | None = None,
    timeout: int = DEFAULT_TIMEOUT,
    redirect_allowed: Callable[[str], bool] | None = None,
    max_bytes: int = MAX_TEXT_BYTES,
    cancel: Cancel | None = None,
) -> bytes:
    _check_cancel(cancel)  # an already-stopped call opens no connection
    with http_open(
        url, headers=headers, timeout=timeout, redirect_allowed=redirect_allowed
    ) as resp:
        return _read_bounded(resp, url, max_bytes, cancel)


def http_post(
    url: str,
    body: bytes,
    *,
    headers: Mapping[str, str] | None = None,
    timeout: int = DEFAULT_TIMEOUT,
    redirect_allowed: Callable[[str], bool] | None = None,
    max_bytes: int = MAX_TEXT_BYTES,
    cancel: Cancel | None = None,
) -> bytes:
    """POST ``body`` and return the reply, with the same bounds and Stop as :func:`http_get`."""
    _check_cancel(cancel)  # an already-stopped call opens no connection
    with http_open(
        url,
        headers=headers,
        timeout=timeout,
        redirect_allowed=redirect_allowed,
        data=body,
    ) as resp:
        return _read_bounded(resp, url, max_bytes, cancel)


def fetch_json(
    url: str,
    *,
    headers: Mapping[str, str] | None = None,
    timeout: int = DEFAULT_TIMEOUT,
    redirect_allowed: Callable[[str], bool] | None = None,
) -> Any:
    hdrs = {"Accept": "application/json"}
    if headers:
        hdrs.update(headers)
    return json.loads(
        http_get(
            url,
            headers=hdrs,
            timeout=timeout,
            redirect_allowed=redirect_allowed,
        ).decode("utf-8")
    )


# ---------------------------------------------------------------------------
# hashing / verification
# ---------------------------------------------------------------------------
def hash_file(
    path: str | os.PathLike[str],
    algorithms: tuple[str, ...] = ("sha256",),
    *,
    chunk: int = DEFAULT_CHUNK,
    cancel: Cancel | None = None,
) -> dict[str, str]:
    """Stream ``path`` once, returning ``{algorithm: hexdigest}``.

    With ``cancel``, a large file stops between blocks (#1469): hashing a resumed archive can
    take minutes."""
    hashers = {name: hashlib.new(name) for name in algorithms}
    with open(path, "rb") as fh:
        while True:
            _check_cancel(cancel)
            block = fh.read(chunk)
            if not block:
                break
            for hasher in hashers.values():
                hasher.update(block)
    return {name: h.hexdigest() for name, h in hashers.items()}


def verify_file(
    path: str | os.PathLike[str],
    *,
    sha256: str | None = None,
    sha512: str | None = None,
    size: int | None = None,
    cancel: Cancel | None = None,
) -> bool:
    """Compare a file against any provided size/hash. Missing file -> False."""
    p = Path(path)
    if not p.is_file():
        return False
    if size is not None and p.stat().st_size != size:
        return False
    wanted = [a for a, v in (("sha256", sha256), ("sha512", sha512)) if v]
    if not wanted:
        return True
    digests = hash_file(p, tuple(wanted), cancel=cancel)
    if sha256 and digests["sha256"].lower() != sha256.lower():
        return False
    if sha512 and digests["sha512"].lower() != sha512.lower():
        return False
    return True


# ---------------------------------------------------------------------------
# download
# ---------------------------------------------------------------------------
def _parse_content_range(value: str) -> tuple[int, int, int] | None:
    """``bytes 100-199/1000`` -> ``(100, 199, 1000)`` (total may be ``*`` -> -1)."""
    try:
        unit, _, rest = value.partition(" ")
        if unit.lower() != "bytes":
            return None
        span, _, total_s = rest.partition("/")
        start_s, _, end_s = span.partition("-")
        total = -1 if total_s.strip() == "*" else int(total_s)
        return int(start_s), int(end_s), total
    except (ValueError, AttributeError):
        return None


def _drop_symlinked_part(part: Path) -> None:
    """Remove a symlink planted at the ``.part`` path before it is opened.

    ``open(part, "wb")``/``"ab"`` follows a symlink, so a link left at the
    staged path would make the download write *through* it and clobber its
    target (the class of escape ``_refuse_symlinked_parent`` guards against for
    directories). ``unlink`` removes the link, never its target, so the
    download then proceeds into a real file at ``part``.
    """
    if part.is_symlink():
        part.unlink()


def _emit_progress(emit: Emitter, stage: str, done: int, total: int) -> None:
    measurement = transfer.current_transfer()
    connections = measurement.active if measurement is not None else 0
    progress = (done / total) if total > 0 else None
    sizes = f"{human_size(done)} / {human_size(total)}" if total > 0 else human_size(done)
    emit(
        Event(
            "progress",
            f"{stage}: {sizes}",
            progress=progress,
            data={"stage": stage, "done": done, "total": total, "connections": connections},
        )
    )


class _BeforeWriteRefused(BaseException):
    """Sentinel carrying an exception raised by a ``before_write`` guard past retry loops."""

    def __init__(self, inner: BaseException) -> None:
        self.inner = inner


def _run_before_write(before_write: Callable[[Path], None] | None, target: Path) -> None:
    if before_write is not None:
        try:
            before_write(target)
        except BaseException as exc:
            raise _BeforeWriteRefused(exc) from None


def _download_once(
    url: str,
    part: Path,
    *,
    headers: Mapping[str, str] | None,
    timeout: int,
    resume: bool,
    emit: Emitter,
    cancel: Cancel | None,
    chunk: int,
    expected_size: int | None = None,
    redirect_allowed: Callable[[str], bool] | None = None,
    pool: HttpClientPool | None = None,
    digests_out: dict[str, str] | None = None,
    before_write: Callable[[Path], None] | None = None,
) -> bool:
    """One attempt. Returns True if the server resumed, False if it restarted."""
    _run_before_write(before_write, part)
    _drop_symlinked_part(part)
    offset = part.stat().st_size if (resume and part.exists()) else 0
    req_headers = dict(headers or {})
    if offset:
        req_headers.setdefault("Range", f"bytes={offset}-")

    extra: dict[str, Any] = {}
    if redirect_allowed is not None:
        extra["redirect_allowed"] = redirect_allowed
    limit = expected_size if expected_size is not None else MAX_DOWNLOAD_BYTES
    def open_url() -> Any:
        if pool is None or not _pool_can_serve(url):
            return http_open(url, headers=req_headers, timeout=timeout, **extra)
        try:
            return _open_pooled(
                pool,
                url,
                headers=req_headers,
                timeout=timeout,
                redirect_allowed=redirect_allowed,
            )
        except ProxyNotSupported:
            # A redirect reached a scheme whose proxy the pool cannot use: urllib can.
            return http_open(url, headers=req_headers, timeout=timeout, **extra)

    try:
        resp_cm = open_url()
    except DownloadError as exc:
        if not (offset and "HTTP 416" in str(exc)):
            raise
        # The part file is already as long as the file (or unusable): resuming
        # can never succeed, so drop it and fetch the file from the start.
        part.unlink(missing_ok=True)
        offset = 0
        req_headers.pop("Range", None)
        resp_cm = open_url()

    with resp_cm as resp:
        if isinstance(resp, transfer.Response):
            resp.cancel = cancel
        status = getattr(resp, "status", None) or resp.getcode()
        # Missing Content-Length means "unknown", not zero: don't reject the body.
        length = resp.headers.get("Content-Length")
        total = int(length) if length else -1
        if status == 206 and offset:
            parsed = _parse_content_range(resp.headers.get("Content-Range", ""))
            if parsed is None:
                raise DownloadError(f"malformed Content-Range for {url}")
            start, _end, total = parsed
            if start != offset:
                raise DownloadError(f"server resumed at {start}, expected {offset}")
            mode, done, resumed = "ab", offset, True
        elif status == 206:
            # We sent no Range, so a partial body is a fragment, not the file.
            raise DownloadError(f"unexpected 206 Partial Content for {url}")
        else:
            # No resume support (200): start over from zero.
            offset, mode, done, resumed = 0, "wb", 0, False
        if total > limit:
            raise IntegrityError(f"download larger than expected: {total} > {limit} bytes")

        _run_before_write(before_write, part)
        part.parent.mkdir(parents=True, exist_ok=True)
        hashers = (
            {alg: hashlib.new(alg) for alg in ("sha256", "sha512", "md5")}
            if digests_out is not None and not resumed
            else None
        )
        with open(part, mode) as fh:
            while True:
                if cancel is not None and cancel.cancelled:
                    raise _cancelled()
                try:
                    block = resp.read(chunk)
                except http.client.IncompleteRead as exc:
                    if cancel is not None and cancel.cancelled:
                        raise _cancelled() from None
                    # Keep what arrived before the drop: the retry resumes after it.
                    if exc.partial and done + len(exc.partial) <= limit:
                        fh.write(exc.partial)
                        fh.flush()
                    raise DownloadError(
                        f"incomplete download for {public_url(url)}: {exc}"
                    ) from scrub_exception(exc)
                if not block:
                    if cancel is not None and cancel.cancelled:
                        raise _cancelled()
                    break
                fh.write(block)
                if hashers is not None:
                    for hasher in hashers.values():
                        hasher.update(block)
                done += len(block)
                if done > limit:
                    raise IntegrityError(f"download exceeded {limit} bytes for {url}")
                _emit_progress(emit, "download", done, total)
            fh.flush()
            os.fsync(fh.fileno())

        if total >= 0 and done != total:
            raise DownloadError(f"incomplete download: {done} of {total} bytes")
        if hashers is not None and digests_out is not None:
            for alg, hasher in hashers.items():
                digests_out[alg] = hasher.hexdigest()
        return resumed


RANGE_THRESHOLD = 256 << 20


class _RangeUnsupported(DownloadError):
    """The origin stopped supporting ranges after the initial probe."""



def _range_paths(part: Path, workers: int) -> list[Path]:
    return [part.with_name(f"{part.name}-{i}") for i in range(workers)]


def _clear_ranges(part: Path, workers: int) -> None:
    # A resumed download may use a different worker count from the previous run.
    prefix = part.name + "-"
    for path in part.parent.iterdir():
        suffix = path.name.removeprefix(prefix).removesuffix(".done")
        if path.name.startswith(prefix) and suffix.isdecimal():
            path.unlink(missing_ok=True)
    part.with_name(part.name + ".ranges.json").unlink(missing_ok=True)


def _download_ranges(url, part, *, expected_size, workers, headers, timeout,
                     emit, cancel, chunk, redirect_allowed, pool, digests_out=None,
                     before_write=None):
    """Return None for a server without Range support; otherwise join verified spans."""
    from concurrent.futures import FIRST_COMPLETED, ThreadPoolExecutor
    from concurrent.futures import wait as wait_futures

    own_pool = pool is None
    clients = pool or HttpClientPool(timeout)
    stop = threading.Event()

    class RangeCancel:
        @property
        def cancelled(self):
            return stop.is_set() or (cancel is not None and cancel.cancelled)

        def raise_if_cancelled(self):
            _check_cancel(cancel)
            if stop.is_set():
                raise _cancelled()

    combined = RangeCancel()
    req_headers = {**dict(headers or {}), "Accept-Encoding": "identity"}
    paths = _range_paths(part, workers)
    metadata = part.with_name(part.name + ".ranges.json")
    identity = hashlib.sha256(
        json.dumps([url, expected_size, workers, req_headers], sort_keys=True).encode()
    ).hexdigest()
    try:
        with _open_pooled(clients, url, headers={**req_headers, "Range": "bytes=0-0"},
                          timeout=timeout, redirect_allowed=redirect_allowed) as response:
            parsed = _parse_content_range(response.headers.get("Content-Range", ""))
            # A valid 206/Content-Range is the proof that Range is honoured. Some
            # origins send it without Accept-Ranges, so that header alone decides
            # nothing; a server that ignores Range answers 200 and falls back.
            if response.status != 206 or parsed != (0, 0, expected_size):
                return None
            response.cancel = combined
            if len(response.read(2)) != 1:
                return None
        release_thread = None if own_pool else getattr(clients, "release_thread", None)
        if release_thread is not None:
            release_thread(reuse=True)
        _run_before_write(before_write, metadata)
        if metadata.is_symlink():
            metadata.unlink()
        try:
            saved_identity = metadata.read_text() if metadata.exists() else None
        except UnicodeError:
            saved_identity = None
        if saved_identity != identity:
            _clear_ranges(part, workers)
            _run_before_write(before_write, metadata)
            metadata.parent.mkdir(parents=True, exist_ok=True)
            metadata.write_text(identity)
        resumed = any(p.exists() or p.with_name(p.name + ".done").exists() for p in paths)
        progress_lock = threading.Lock()
        amounts = [0] * workers

        def fetch(index):
            ok = False
            try:
                start = expected_size * index // workers
                end = expected_size * (index + 1) // workers - 1
                path = paths[index]
                done_path = path.with_name(path.name + ".done")
                _run_before_write(before_write, path)
                _run_before_write(before_write, done_path)
                _drop_symlinked_part(path)
                _drop_symlinked_part(done_path)
                if done_path.exists() and done_path.stat().st_size == end - start + 1:
                    with progress_lock:
                        amounts[index] = end - start + 1
                    ok = True
                    return
                done_path.unlink(missing_ok=True)
                combined.raise_if_cancelled()
                offset = path.stat().st_size if path.exists() else 0
                # Only .done proves that EOF was checked. A full unfinished span
                # may have been interrupted before that check, so fetch it again.
                if offset >= end - start + 1:
                    path.unlink()
                    offset = 0
                if offset < end - start + 1:
                    span_headers = {**req_headers, "Range": f"bytes={start + offset}-{end}"}
                    with _open_pooled(
                        clients, url, headers=span_headers, timeout=timeout,
                        redirect_allowed=redirect_allowed,
                    ) as response:
                        response.cancel = combined
                        if (response.status != 206
                                or _parse_content_range(response.headers.get("Content-Range", ""))
                                != (start + offset, end, expected_size)):
                            raise _RangeUnsupported("server stopped honouring download ranges")
                        _run_before_write(before_write, path)
                        with open(path, "ab" if offset else "wb") as output:
                            done = offset
                            while True:
                                combined.raise_if_cancelled()
                                try:
                                    block = response.read(min(chunk, end - start + 2 - done))
                                except http.client.IncompleteRead as exc:
                                    combined.raise_if_cancelled()
                                    # Keep what arrived: the retry resumes after it.
                                    if exc.partial and done + len(exc.partial) <= end - start + 1:
                                        output.write(exc.partial)
                                        output.flush()
                                    raise DownloadError("incomplete range body") from exc
                                if not block:
                                    combined.raise_if_cancelled()
                                    break
                                done += len(block)
                                if done > end - start + 1:
                                    path.unlink(missing_ok=True)
                                    raise DownloadError("range body is larger than requested")
                                output.write(block)
                                with progress_lock:
                                    amounts[index] = done
                                    _emit_progress(emit, "download", sum(amounts), expected_size)
                            output.flush()
                            os.fsync(output.fileno())
                        if done != end - start + 1:
                            raise DownloadError("incomplete range body")
                _run_before_write(before_write, path)
                _run_before_write(before_write, done_path)
                os.replace(path, done_path)
                emit(Event("log", "Download part finished", data={"range_finished": index}))
                ok = True
            finally:
                if release_thread is not None:
                    release_thread(reuse=ok)

        executor = ThreadPoolExecutor(
            max_workers=workers,
            initializer=transfer.inherit_transfer,
            initargs=(transfer.current_transfer(),),
        )
        futures = []
        try:
            futures = [executor.submit(fetch, i) for i in range(workers)]
            pending = set(futures)
            poll_timeout = 0.05 if cancel is not None else None
            while pending:
                combined.raise_if_cancelled()
                done_set, pending = wait_futures(
                    pending, timeout=poll_timeout, return_when=FIRST_COMPLETED
                )
                if done_set:
                    for future in futures:
                        if future in done_set:
                            future.result()
        except BaseException:
            stop.set()
            for future in futures:
                future.cancel()
            if own_pool:
                # Close sockets before waiting on workers so a stalled read wakes at once (#1854).
                clients.close()
            raise
        finally:
            executor.shutdown(wait=True)
        _check_cancel(cancel)
        # All parts are on disk and the bar says 100%; joining and hashing the whole file
        # takes a while on a large runner, so name it instead of leaving a finished bar.
        emit(Event("step", "Checking the download…", data={"stage": "verify"}))
        _run_before_write(before_write, part)
        _drop_symlinked_part(part)
        hashers = (
            {alg: hashlib.new(alg) for alg in ("sha256", "sha512", "md5")}
            if digests_out is not None
            else None
        )
        with open(part, "wb") as output:
            for path in paths:
                with open(path.with_name(path.name + ".done"), "rb") as source:
                    while block := source.read(chunk):
                        _check_cancel(cancel)
                        output.write(block)
                        if hashers is not None:
                            for hasher in hashers.values():
                                hasher.update(block)
            output.flush()
            os.fsync(output.fileno())
        if hashers is not None and digests_out is not None:
            for alg, hasher in hashers.items():
                digests_out[alg] = hasher.hexdigest()
        return resumed
    except ProxyNotSupported:
        # A redirect may reach a proxy the pool cannot use; urllib handles it.
        return None
    except _RangeUnsupported:
        _clear_ranges(part, workers)
        return None
    finally:
        if own_pool:
            clients.close()


def download(
    url: str,
    dest: str | os.PathLike[str],
    *,
    expected_sha256: str | None = None,
    expected_sha512: str | None = None,
    expected_size: int | None = None,
    headers: Mapping[str, str] | None = None,
    retries: int = DEFAULT_RETRIES,
    backoff: float = DEFAULT_BACKOFF,
    timeout: int = DEFAULT_TIMEOUT,
    chunk: int = DEFAULT_CHUNK,
    resume: bool = True,
    emit: Emitter | None = None,
    cancel: Cancel | None = None,
    redirect_allowed: Callable[[str], bool] | None = None,
    pool: HttpClientPool | None = None,
    range_workers: int | None = None,
    before_write: Callable[[Path], None] | None = None,
) -> DownloadResult:
    """Download ``url`` to ``dest`` atomically.

    Retries transient network errors with exponential backoff. The file is
    verified against ``expected_sha256``/``expected_sha512``/``expected_size``
    before ``os.replace`` puts it in place; a mismatch raises
    :class:`IntegrityError` and leaves no partial file at ``dest``.
    """
    try:
        return _download_guarded(
            url,
            dest,
            expected_sha256=expected_sha256,
            expected_sha512=expected_sha512,
            expected_size=expected_size,
            headers=headers,
            retries=retries,
            backoff=backoff,
            timeout=timeout,
            chunk=chunk,
            resume=resume,
            emit=emit,
            cancel=cancel,
            redirect_allowed=redirect_allowed,
            pool=pool,
            range_workers=range_workers,
            before_write=before_write,
        )
    except _BeforeWriteRefused as refusal:
        raise refusal.inner from None


def _download_guarded(
    url: str,
    dest: str | os.PathLike[str],
    *,
    expected_sha256: str | None = None,
    expected_sha512: str | None = None,
    expected_size: int | None = None,
    headers: Mapping[str, str] | None = None,
    retries: int = DEFAULT_RETRIES,
    backoff: float = DEFAULT_BACKOFF,
    timeout: int = DEFAULT_TIMEOUT,
    chunk: int = DEFAULT_CHUNK,
    resume: bool = True,
    emit: Emitter | None = None,
    cancel: Cancel | None = None,
    redirect_allowed: Callable[[str], bool] | None = None,
    pool: HttpClientPool | None = None,
    range_workers: int | None = None,
    before_write: Callable[[Path], None] | None = None,
) -> DownloadResult:
    say = emitter(emit)
    destination = Path(dest).expanduser()
    part = destination.with_name(destination.name + ".part")
    _run_before_write(before_write, destination)
    _run_before_write(before_write, part)
    destination.parent.mkdir(parents=True, exist_ok=True)
    _drop_symlinked_part(part)

    if (
        resume
        and (expected_sha256 or expected_sha512 or expected_size is not None)
        and verify_file(
            part, sha256=expected_sha256, sha512=expected_sha512, size=expected_size,
            cancel=cancel,
        )
    ):
        _check_cancel(cancel)  # a cancelled call never promotes a cached part (#1469)
        _run_before_write(before_write, part)
        _run_before_write(before_write, destination)
        os.replace(part, destination)
        digests = hash_file(destination, ("sha256", "sha512", "md5"), cancel=cancel)
        say(Event("log", f"reused verified download: {destination.name}"))
        return DownloadResult(
            destination,
            destination.stat().st_size,
            digests["sha256"],
            digests["sha512"],
            resumed=True,
            md5=digests.get("md5", ""),
        )

    measurement = transfer.current_transfer()
    count = range_workers if range_workers is not None else getattr(measurement, "range_workers", 4)
    count = max(1, min(32, count))
    use_ranges = (expected_size is not None and expected_size > RANGE_THRESHOLD
                  and count > 1 and _pool_can_serve(url))
    if not resume:
        _clear_ranges(part, count)

    last_error: Exception | None = None
    resumed = False
    attempts = max(1, retries)
    attempt = 0
    refetch_clean = False
    while attempt < attempts:
        completed = False
        try:
            inline_digests: dict[str, str] = {}
            ranged = None
            write_extra: dict[str, Any] = (
                {"before_write": before_write} if before_write is not None else {}
            )
            if use_ranges:
                ranged = _download_ranges(
                    url, part, expected_size=expected_size, workers=count,
                    headers=headers, timeout=timeout, emit=say, cancel=cancel,
                    chunk=chunk, redirect_allowed=redirect_allowed, pool=pool,
                    digests_out=inline_digests, **write_extra,
                )
                if ranged is None:
                    use_ranges = False
            resumed = ranged if ranged is not None else _download_once(
                url,
                part,
                headers=headers,
                timeout=timeout,
                resume=resume and not refetch_clean,
                emit=say,
                cancel=cancel,
                chunk=chunk,
                expected_size=expected_size,
                redirect_allowed=redirect_allowed,
                pool=pool,
                digests_out=inline_digests,
                **write_extra,
            )
            completed = True
            size = part.stat().st_size
            if "sha256" in inline_digests and "sha512" in inline_digests:
                digests = inline_digests
            else:
                digests = hash_file(part, ("sha256", "sha512", "md5"), cancel=cancel)
            if expected_size is not None and size != expected_size:
                raise IntegrityError(
                    f"size mismatch for {destination.name}: {size} != {expected_size}"
                )
            if expected_sha256 and digests["sha256"].lower() != expected_sha256.lower():
                raise IntegrityError(f"sha256 mismatch for {destination.name}")
            if expected_sha512 and digests["sha512"].lower() != expected_sha512.lower():
                raise IntegrityError(f"sha512 mismatch for {destination.name}")
            # The commit point: once renamed, the download is done, Cancel or not (#1469).
            _check_cancel(cancel)
            _run_before_write(before_write, part)
            _run_before_write(before_write, destination)
            os.replace(part, destination)
            _fsync_dir(destination.parent)
            _clear_ranges(part, count)
            say(
                Event(
                    "log",
                    f"verified {destination.name} ({size} bytes)",
                    data={"sha256": digests["sha256"], "sha512": digests["sha512"]},
                )
            )
            return DownloadResult(
                destination,
                size,
                digests["sha256"],
                digests["sha512"],
                resumed=resumed,
                md5=digests.get("md5", ""),
            )
        except IntegrityError:
            # A corrupt transfer (bad hash/size after the bytes arrived) is not
            # worth retrying byte for byte: drop the part and refetch once from
            # zero, which recovers from a stale/wrong-prefix ``.part`` instead
            # of failing the call. Protocol-level failures raised *inside* the
            # transfer (e.g. an oversized body) are not retried: the server
            # would just repeat them.
            _remove(part)
            _clear_ranges(part, count)
            if completed and not refetch_clean:
                refetch_clean = True
                say(
                    Event(
                        "log",
                        f"integrity check failed for {destination.name}; retrying from scratch",
                    )
                )
                continue
            raise
        except Exception as exc:  # noqa: BLE001 - retry everything transient
            if _is_cancelled(exc):
                # Keep the partial bytes: the next call resumes from them and
                # the final hash check still guards the finished file.
                _remove_if_empty(part)
                raise
            if isinstance(exc, RedirectRefused):
                _remove_if_empty(part)  # the same redirect would be refused again
                raise
            if isinstance(exc, OSError) and exc.errno in _FATAL_WRITE_ERRNOS:
                # A full/read-only/unwritable destination will fail the same way
                # on every retry; tell the user what to fix instead.
                _remove_if_empty(part)
                if exc.errno in (errno.ENOSPC, getattr(errno, "EDQUOT", -1)):
                    raise DownloadError(
                        f"not enough disk space to download {destination.name}"
                    ) from scrub_exception(exc)
                raise DownloadError(f"cannot write to {destination}") from scrub_exception(exc)
            status = getattr(exc, "status", None)
            if status is not None and 400 <= status < 500 and not transient_status(status):
                # A 403/404 (etc.) is permanent: re-requesting it only wastes
                # time and can look like abuse. 408, 425 and 429 mean "try again
                # later" (429 may carry Retry-After), so those keep retrying.
                _remove_if_empty(part)
                raise
            last_error = exc
            attempt += 1
            if attempt >= attempts:
                break
            delay = usable_retry_after(exc)
            if delay is None:
                delay = retry_delay(backoff, attempt)
            say(
                Event(
                    "log",
                    f"download error ({exc}); retrying in {delay:.0f}s",
                    data={"attempt": attempt, "retries": attempts},
                )
            )
            _sleep(delay, cancel)

    _remove_if_empty(part)  # a network failure keeps what arrived, so a retry resumes
    cause = scrub_exception(last_error) if last_error is not None else None
    raise DownloadError(f"download failed for {url}: {last_error}") from cause


def remote_size(
    url: str,
    *,
    headers: Mapping[str, str] | None = None,
    timeout: int = DEFAULT_TIMEOUT,
    redirect_allowed: Callable[[str], bool] | None = None,
) -> int | None:
    """Best-effort size of ``url`` via HEAD (or a one-byte ranged GET); ``None`` if unknown.

    Used to run a disk-space check before a large download. Servers that
    reject HEAD, hide the length, or are unreachable simply yield ``None`` and
    the caller skips the check. Redirects go through :class:`_SafeRedirectHandler`
    (like :func:`http_open`), so a supplied ``redirect_allowed`` allow-list is
    enforced per hop and credentials never follow a cross-origin/downgrade hop
    (N1).
    """
    opener = _build_opener(redirect_allowed)
    size = _size_from(opener, _request(url, headers), "HEAD", timeout)
    if size is not None:
        return size
    # Signing CDNs often answer HEAD with 403/405 while GET works: ask for one byte and
    # read the total from Content-Range instead.
    ranged = _request(url, {**(headers or {}), "Range": "bytes=0-0"})
    return _size_from(opener, ranged, "GET", timeout)


def _size_from(opener, request, method: str, timeout: int) -> int | None:
    request.method = method
    try:
        with opener.open(request, timeout=timeout) as response:
            ranged = response.headers.get("Content-Range", "")
            total = ranged.rpartition("/")[2]
            if ranged and not total.isdigit():
                # "bytes 0-0/*": the length of the slice is not the size of the object (#930).
                return None
            length = total if total.isdigit() else response.headers.get("Content-Length")
    except Exception:  # noqa: BLE001 - an unknown size is not an error
        return None
    try:
        size = int(length)
    except (TypeError, ValueError):
        return None
    return size if size >= 0 else None


def download_bytes(
    url: str,
    *,
    headers: Mapping[str, str] | None = None,
    timeout: int = DEFAULT_TIMEOUT,
) -> bytes:
    return http_get(url, headers=headers, timeout=timeout)


def _remove(path: Path) -> None:
    try:
        path.unlink(missing_ok=True)
    except OSError:
        pass


def _remove_if_empty(path: Path) -> None:
    try:
        if path.stat().st_size == 0:
            path.unlink(missing_ok=True)
    except OSError:
        pass


def _fsync_dir(directory: Path) -> None:
    """Best-effort fsync of ``directory`` so the rename is durable."""
    try:
        fd = os.open(directory, os.O_RDONLY)
    except OSError:
        return
    try:
        os.fsync(fd)
    except OSError:
        pass
    finally:
        os.close(fd)


def _sleep(seconds: float, cancel: Cancel | None) -> None:
    deadline = time.monotonic() + seconds
    while time.monotonic() < deadline:
        if cancel is not None and cancel.cancelled:
            raise _cancelled()
        time.sleep(max(0.0, min(0.2, deadline - time.monotonic())))


def _check_cancel(cancel: Cancel | None) -> None:
    if cancel is not None and cancel.cancelled:
        raise _cancelled()


def _cancelled() -> BaseException:
    from .events import Cancelled

    return Cancelled("download cancelled")


def _is_cancelled(exc: BaseException) -> bool:
    from .events import Cancelled

    return isinstance(exc, Cancelled)


# ---------------------------------------------------------------------------
# keep-alive streaming (used by the parallel Yostar sync)
# ---------------------------------------------------------------------------
class HttpResponse:
    """Thin wrapper over ``http.client.HTTPResponse`` for streaming reads."""

    __slots__ = ("status", "headers", "_response", "_conn")

    def __init__(
        self,
        response: http.client.HTTPResponse,
        conn: http.client.HTTPConnection | None = None,
    ) -> None:
        self._response = response
        #: The pooled connection this response came from; closed with a half-read body.
        self._conn = conn
        self.status = response.status
        self.headers = response.headers

    def read(self, amount: int = -1) -> bytes:
        return self._response.read(amount)

    def header(self, name: str, default: str | None = None) -> str | None:
        return self._response.getheader(name, default)

    def close(self) -> None:
        # http.client marks a response closed once its body is read to the end. Anything
        # else leaves body bytes on the socket that the next request on this keep-alive
        # connection would read as its own response (#767), so drop the connection;
        # http.client reconnects on the next request.
        unread = not getattr(self._response, "isclosed", lambda: True)()
        # A body that ended early (``length`` still owed) leaves a dead socket: drop it too.
        unread = unread or bool(getattr(self._response, "length", None))
        try:
            self._response.close()
        except OSError:
            pass
        if unread and self._conn is not None:
            try:
                self._conn.close()
            except OSError:
                pass

    def __enter__(self) -> HttpResponse:
        return self

    def __exit__(self, *exc: object) -> bool:
        self.close()
        return False


class HttpClientPool:
    """Per-thread keep-alive HTTP(S) connections.

    ``urllib`` opens a fresh socket (and pays a TLS handshake) for every
    request, which dominates a sync made of thousands of small files.
    ``http.client`` transparently reuses a connection once its response body
    has been fully consumed, so each worker thread keeps its own connection
    keyed by origin.  ``HTTPConnection`` is not thread-safe, hence the
    thread-local store and the global registry used to close every socket.
    """

    def __init__(self, timeout: int = DEFAULT_TIMEOUT) -> None:
        self.timeout = timeout
        self._local = threading.local()
        self._lock = threading.Lock()
        self._all: list[http.client.HTTPConnection] = []
        self._idle: dict[tuple[str, str, int], list[http.client.HTTPConnection]] = {}
        self._closed = False

    def __enter__(self):
        return self

    def __exit__(self, *exc):
        self.close()
        return False

    def _store(self) -> dict[tuple[str, str, int], http.client.HTTPConnection]:
        store = getattr(self._local, "connections", None)
        if store is None:
            store = {}
            self._local.connections = store
        return store

    def _connection(
        self,
        scheme: str,
        host: str,
        port: int,
        timeout: int,
        reuse_idle: bool = True,
    ) -> http.client.HTTPConnection:
        if self._closed:
            # Stop closed the pool: a worker its socket shutdown woke must not dial again
            # and wait out a fresh timeout (Neon r1, packsec).
            raise OSError("download pool is closed")
        key = (scheme, host, port)
        store = self._store()
        conn = store.get(key)
        if conn is not None:
            return conn
        if reuse_idle:
            while True:
                with self._lock:
                    bucket = self._idle.get(key)
                    recycled = bucket.pop() if bucket else None
                if recycled is None:
                    break
                sock = getattr(recycled, "sock", None)
                if sock is not None:
                    try:
                        recycled.timeout = timeout
                        sock.settimeout(timeout)
                        store[key] = recycled
                        return recycled
                    except OSError:
                        pass
                try:
                    recycled.close()
                except OSError:
                    pass
                with self._lock:
                    if recycled in self._all:
                        self._all.remove(recycled)
        proxy = _proxy_for(scheme, host)
        if scheme == "https":
            if proxy is None:
                conn = http.client.HTTPSConnection(
                    host, port, timeout=timeout, context=tls.ssl_context()
                )
            else:
                conn = http.client.HTTPSConnection(
                    proxy[0], proxy[1], timeout=timeout, context=tls.ssl_context()
                )
                conn.set_tunnel(host, port, headers=proxy[2])
        elif proxy is None:
            conn = http.client.HTTPConnection(host, port, timeout=timeout)
        else:
            conn = http.client.HTTPConnection(proxy[0], proxy[1], timeout=timeout)
        with self._lock:
            if self._closed:
                raise OSError("download pool is closed")
            self._all.append(conn)
        store[key] = conn
        return conn

    def _forget(self, scheme: str, host: str, port: int) -> None:
        conn = self._store().pop((scheme, host, port), None)
        if conn is None:
            return
        try:
            conn.close()
        except OSError:
            pass
        with self._lock:
            if conn in self._all:
                self._all.remove(conn)

    def release_thread(self, *, reuse: bool = True) -> None:
        """Hand this thread's connections back to the shared idle pool, or close them."""
        store = self._store()
        if not store:
            return
        items = list(store.items())
        store.clear()
        to_close: list[http.client.HTTPConnection] = []
        with self._lock:
            for key, conn in items:
                if reuse and not self._closed and getattr(conn, "sock", None) is not None:
                    self._idle.setdefault(key, []).append(conn)
                else:
                    if conn in self._all:
                        self._all.remove(conn)
                    to_close.append(conn)
        for conn in to_close:
            try:
                conn.close()
            except OSError:
                pass

    def open(
        self,
        url: str,
        *,
        headers: Mapping[str, str] | None = None,
        timeout: int | None = None,
        redirects: int = 5,
        redirect_allowed: Callable[[str], bool] | None = None,
        strict_proxy: bool = True,
    ) -> HttpResponse:
        """Issue a GET, following redirects, and return a streaming response.

        Credentials are dropped when a redirect leaves the original origin, and
        an ``https``→``http`` downgrade is refused. ``redirect_allowed``, when
        supplied, is consulted with the absolute target of each hop and a
        ``False`` return aborts the request (used by callers with a host
        allow-list).
        """
        request_headers = {"User-Agent": USER_AGENT, "Accept": "*/*"}
        if headers:
            request_headers.update({str(k): str(v) for k, v in headers.items()})
        current = url
        origin = _split_url(current)[:3]
        for _ in range(redirects + 1):
            scheme, host, port, path = _split_url(current)
            deadline = timeout or self.timeout
            proxy = _proxy_for(scheme, host)
            if strict_proxy and proxy is None and _proxy_configured(scheme, host):
                # Every hop is checked: a redirect can move to a scheme the proxy covers.
                raise ProxyNotSupported(f"the proxy configured for {scheme} is not supported here")
            conn = self._connection(scheme, host, port, deadline)
            send_headers = request_headers
            if proxy is not None and scheme == "http":
                # A plain-HTTP proxy wants the absolute URL and its own credentials.
                path = current
                send_headers = {**request_headers, **proxy[2]}
            opened = getattr(conn, "sock", None) is None
            try:
                conn.request("GET", path, headers=send_headers)
                response = conn.getresponse()
            except socket.gaierror:
                # A failed lookup is not a stale connection: retrying at once only repeats
                # the lookup, which a rate-limiting resolver counts against us (#767).
                self._forget(scheme, host, port)
                raise
            except (http.client.HTTPException, OSError):
                # A pooled keep-alive connection may have been closed by the
                # peer; drop only this failed socket (not healthy idle siblings)
                # and retry once on a fresh connection.
                self._forget(scheme, host, port)
                conn = self._connection(scheme, host, port, deadline, False)
                opened = getattr(conn, "sock", None) is None
                conn.request("GET", path, headers=send_headers)
                response = conn.getresponse()
            if response.status in (301, 302, 303, 307, 308):
                location = response.getheader("Location")
                # Drain a bounded amount only; a hostile 3xx can advertise an
                # arbitrarily large body. If it was not fully read the
                # connection cannot be reused, so drop it.
                response.read(REDIRECT_DRAIN_BYTES)
                finished = getattr(response, "isclosed", lambda: True)()
                if not finished:
                    with contextlib.suppress(Exception):
                        response.close()
                    self._forget(scheme, host, port)
                if not location:
                    raise DownloadError(f"redirect without Location for {current}")
                target = urllib.parse.urljoin(current, location)
                tscheme, thost, tport, _ = _split_url(target)
                if origin[0] == "https" and tscheme != "https":
                    raise RedirectRefused(
                        f"refusing insecure redirect from {current} to {target}"
                    )
                if (tscheme, thost, tport) != origin:
                    # Never forward credentials to a different origin.
                    request_headers = {
                        k: v
                        for k, v in request_headers.items()
                        if k.lower() not in CREDENTIAL_HEADERS
                    }
                if redirect_allowed is not None and not redirect_allowed(target):
                    raise RedirectRefused(f"redirect to disallowed URL: {target}")
                current = target
                continue
            return transfer.Response(HttpResponse(response, conn), current, opened)
        raise DownloadError(f"too many redirects for {url}")

    def close(self) -> None:
        with self._lock:
            self._closed = True
            self._idle.clear()
            connections, self._all = self._all, []
        for conn in connections:
            sock = getattr(conn, "sock", None)
            if sock is not None:
                # Wake a worker blocked in a stalled response read: close() alone leaves it
                # waiting for the socket timeout (Neon 2, cancel while a pack stalls).
                try:
                    sock.shutdown(socket.SHUT_RDWR)
                except OSError:
                    pass
            try:
                conn.close()
            except OSError:
                pass


def _proxy_configured(scheme: str, host: str) -> bool:
    """Whether the environment names any proxy (of any type) for ``scheme``/``host``."""
    try:
        return bool(urllib.request.getproxies().get(scheme)) and not urllib.request.proxy_bypass(
            host
        )
    except ValueError:
        return False


def _proxy_for(scheme: str, host: str) -> tuple[str, int, dict[str, str]] | None:
    """The ``http://`` proxy the environment names for ``scheme``/``host``, if any.

    Honours ``http_proxy``/``https_proxy``/``no_proxy`` like the urllib paths do.
    Returns ``(proxy_host, proxy_port, proxy_headers)``.
    """
    try:
        proxies = urllib.request.getproxies()
        value = proxies.get(scheme)
        if not value or urllib.request.proxy_bypass(host):
            return None
        parsed = urllib.parse.urlparse(value if "//" in value else f"http://{value}")
        if parsed.scheme != "http" or not parsed.hostname:
            return None
        port = parsed.port or 80
    except ValueError:
        return None
    headers: dict[str, str] = {}
    if parsed.username is not None:
        token = base64.b64encode(
            f"{urllib.parse.unquote(parsed.username)}:"
            f"{urllib.parse.unquote(parsed.password or '')}".encode()
        ).decode("ascii")
        headers["Proxy-Authorization"] = f"Basic {token}"
    return parsed.hostname, port, headers


def _split_url(url: str) -> tuple[str, str, int, str]:
    parsed = urllib.parse.urlparse(url)
    scheme = parsed.scheme.lower()
    if scheme not in ("http", "https"):
        raise DownloadError(f"unsupported URL scheme: {url!r}")
    try:
        host = parsed.hostname
    except ValueError as exc:
        raise DownloadError(f"invalid URL host: {url!r}") from scrub_exception(exc)
    if not host:
        raise DownloadError(f"URL has no host: {url!r}")
    try:
        port = parsed.port or (443 if scheme == "https" else 80)
    except ValueError as exc:
        # A malformed port (e.g. ``https://host:abc/``) must not escape as a
        # raw ``ValueError`` from the redirect path (N4).
        raise DownloadError(f"invalid port in URL: {url!r}") from scrub_exception(exc)
    path = parsed.path or "/"
    if parsed.query:
        path = f"{path}?{parsed.query}"
    return scheme, host, port, path


@dataclass
class StreamResult:
    path: Path
    size: int
    resumed: bool = False


def stream_download(
    url: str,
    dest: str | os.PathLike[str],
    *,
    pool: HttpClientPool,
    hasher: Any | None = None,
    expected_size: int | None = None,
    headers: Mapping[str, str] | None = None,
    timeout: int = DEFAULT_TIMEOUT,
    chunk: int = DEFAULT_CHUNK,
    resume: bool = True,
    redirect_allowed: Callable[[str], bool] | None = None,
    cancel: Cancel | None = None,
    on_progress: Callable[[int, int, int], None] | None = None,
) -> StreamResult:
    """Stream ``url`` into ``dest`` + ``.part``, hashing bytes as they arrive.

    When the part file already exists the transfer resumes with a ``Range``
    request and ``hasher`` is seeded once from the existing bytes, so the CRC
    is computed without a second full read after the download.  The hasher must
    start empty; if the server refuses the range (a ``200`` restart) it is used
    as-is rather than reset, so it must not carry stale state.  The caller owns
    verification and the final ``os.replace``; on cancellation the part file is
    deliberately kept so a later run can resume it.
    """
    destination = Path(dest).expanduser()
    part = destination.with_name(destination.name + ".part")
    _drop_symlinked_part(part)
    offset = part.stat().st_size if (resume and part.exists()) else 0
    if offset and expected_size is not None and offset > expected_size:
        # A part file larger than the file can never be resumed.
        part.unlink(missing_ok=True)
        offset = 0

    request_headers = dict(headers or {})
    if offset:
        request_headers["Range"] = f"bytes={offset}-"
    response = pool.open(
        url, headers=request_headers, timeout=timeout, redirect_allowed=redirect_allowed
    )
    if response.status == 416 and offset:
        # "Range not satisfiable": the part file is already as long as the
        # file (cancelled right after the last byte) or otherwise unusable.
        # Resuming can never succeed, so start this file over.
        response.close()
        part.unlink(missing_ok=True)
        return stream_download(
            url,
            dest,
            pool=pool,
            hasher=hasher,
            expected_size=expected_size,
            headers=headers,
            timeout=timeout,
            chunk=chunk,
            resume=False,
            redirect_allowed=redirect_allowed,
            cancel=cancel,
            on_progress=on_progress,
        )
    if isinstance(response, transfer.Response):
        response.cancel = cancel
    try:
        length = response.header("Content-Length")
        known_total = int(length) if length else -1
        if response.status == 206 and offset:
            parsed = _parse_content_range(response.header("Content-Range", "") or "")
            if parsed is None:
                raise DownloadError(f"malformed Content-Range for {url}")
            start, _end, total = parsed
            if start != offset:
                raise DownloadError(f"server resumed at {start}, expected {offset}")
            mode, done, resumed = "ab", offset, True
            # Seed the hasher only here, from the bytes already on disk; the
            # 200 branch below therefore never needs a (possibly absent)
            # ``hasher.reset()`` to discard stale input.
            if hasher is not None:
                with open(part, "rb") as fh:
                    while True:
                        block = fh.read(chunk)
                        if not block:
                            break
                        hasher.update(block)
        elif response.status == 206:
            # We sent no Range, so a partial body is a fragment, not the file.
            raise DownloadError(f"unexpected 206 Partial Content for {url}")
        elif 200 <= response.status < 300:
            offset, mode, done, resumed = 0, "wb", 0, False
            total = known_total
        else:
            raise DownloadError(f"HTTP {response.status} for {url}")

        limit = expected_size if expected_size is not None else MAX_DOWNLOAD_BYTES
        if total > limit:
            raise DownloadError(f"download larger than expected: {total} > {limit} bytes")
        part.parent.mkdir(parents=True, exist_ok=True)
        with open(part, mode) as fh:
            while True:
                if cancel is not None and cancel.cancelled:
                    raise _cancelled()
                try:
                    block = response.read(chunk)
                except http.client.IncompleteRead as exc:
                    if cancel is not None and cancel.cancelled:
                        raise _cancelled() from None
                    # Keep what arrived before the drop: a resumed run continues after it.
                    if exc.partial and done + len(exc.partial) <= limit:
                        fh.write(exc.partial)
                        fh.flush()
                        if hasher is not None:
                            hasher.update(exc.partial)
                    raise DownloadError(
                        f"incomplete download for {public_url(url)}: {exc}"
                    ) from scrub_exception(exc)
                if not block:
                    break
                fh.write(block)
                if hasher is not None:
                    hasher.update(block)
                done += len(block)
                if done > limit:
                    fh.close()
                    part.unlink(missing_ok=True)
                    raise DownloadError(f"download exceeded {limit} bytes for {url}")
                if on_progress is not None:
                    on_progress(len(block), done, total)
            fh.flush()
            os.fsync(fh.fileno())

        if total >= 0 and done != total:
            raise DownloadError(f"incomplete download: {done} of {total} bytes")
        if expected_size is not None and done != expected_size:
            raise DownloadError(f"size mismatch: {done} != {expected_size}")
        return StreamResult(destination, done, resumed)
    finally:
        response.close()


#: Convenience alias used by runner/backends that want a callable.
DownloadFn = Callable[..., DownloadResult]
