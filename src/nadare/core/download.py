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
import os
import threading
import time
import urllib.error
import urllib.parse
import urllib.request
from collections.abc import Callable, Mapping
from dataclasses import dataclass
from pathlib import Path
from typing import Any, NoReturn

from . import tls
from .events import Cancel, Emitter, Event, emitter

DEFAULT_CHUNK = 1 << 20  # 1 MiB
DEFAULT_TIMEOUT = 60
DEFAULT_RETRIES = 3
DEFAULT_BACKOFF = 2.0
MAX_BACKOFF = 60.0
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
    """A download failed after exhausting retries, or failed verification."""


class IntegrityError(DownloadError):
    """A completed download did not match the expected hash/size."""


@dataclass
class DownloadResult:
    path: Path
    size: int
    sha256: str
    sha512: str
    resumed: bool = False


# ---------------------------------------------------------------------------
# low-level HTTP
# ---------------------------------------------------------------------------
def _request(url: str, headers: Mapping[str, str] | None = None) -> urllib.request.Request:
    hdrs = {"User-Agent": USER_AGENT, "Accept": "*/*"}
    if headers:
        hdrs.update({str(k): str(v) for k, v in headers.items()})
    return urllib.request.Request(url, headers=hdrs)


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
        raise DownloadError(message)

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


def _build_opener(redirect_allowed: Callable[[str], bool] | None) -> Any:
    """An opener with the safe redirect guard and Nadare's pinned CA context."""
    guard = _SafeRedirectHandler(redirect_allowed)
    handler = urllib.request.HTTPSHandler(context=tls.ssl_context())
    try:
        return urllib.request.build_opener(guard, handler)
    except TypeError as exc:  # a one-handler stub in older tests
        if "argument" not in str(exc):
            raise
    return urllib.request.build_opener(guard)


def http_open(
    url: str,
    *,
    headers: Mapping[str, str] | None = None,
    timeout: int = DEFAULT_TIMEOUT,
    redirect_allowed: Callable[[str], bool] | None = None,
) -> Any:
    """Open a URL, translating HTTP/URL errors into :class:`DownloadError`.

    Redirects go through :class:`_SafeRedirectHandler`, so credentials never
    follow a cross-origin hop, ``https``→``http`` is refused, and a supplied
    ``redirect_allowed`` allow-list is enforced per hop.
    """
    opener = _build_opener(redirect_allowed)
    try:
        return opener.open(_request(url, headers), timeout=timeout)  # noqa: S310
    except DownloadError:
        raise
    except urllib.error.HTTPError as exc:
        raise DownloadError(f"HTTP {exc.code} for {url}") from exc
    except (urllib.error.URLError, OSError, ValueError) as exc:
        raise DownloadError(f"cannot reach {url}: {exc}") from exc


def http_get(
    url: str,
    *,
    headers: Mapping[str, str] | None = None,
    timeout: int = DEFAULT_TIMEOUT,
    redirect_allowed: Callable[[str], bool] | None = None,
) -> bytes:
    with http_open(
        url, headers=headers, timeout=timeout, redirect_allowed=redirect_allowed
    ) as resp:
        data = resp.read(MAX_TEXT_BYTES + 1)
    if len(data) > MAX_TEXT_BYTES:
        raise DownloadError(f"response too large (over {MAX_TEXT_BYTES} bytes) for {url}")
    return data


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
) -> dict[str, str]:
    """Stream ``path`` once, returning ``{algorithm: hexdigest}``."""
    hashers = {name: hashlib.new(name) for name in algorithms}
    with open(path, "rb") as fh:
        while True:
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
    digests = hash_file(p, tuple(wanted))
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
    progress = (done / total) if total > 0 else None
    emit(
        Event(
            "progress",
            f"{stage}: {done}/{total or '?'} bytes",
            progress=progress,
            data={"stage": stage, "done": done, "total": total},
        )
    )


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
) -> bool:
    """One attempt. Returns True if the server resumed, False if it restarted."""
    _drop_symlinked_part(part)
    offset = part.stat().st_size if (resume and part.exists()) else 0
    req_headers = dict(headers or {})
    if offset:
        req_headers.setdefault("Range", f"bytes={offset}-")

    extra: dict[str, Any] = {}
    if redirect_allowed is not None:
        extra["redirect_allowed"] = redirect_allowed
    limit = expected_size if expected_size is not None else MAX_DOWNLOAD_BYTES
    try:
        resp_cm = http_open(url, headers=req_headers, timeout=timeout, **extra)
    except DownloadError as exc:
        if not (offset and "HTTP 416" in str(exc)):
            raise
        # The part file is already as long as the file (or unusable): resuming
        # can never succeed, so drop it and fetch the file from the start.
        part.unlink(missing_ok=True)
        offset = 0
        req_headers.pop("Range", None)
        resp_cm = http_open(url, headers=req_headers, timeout=timeout, **extra)

    with resp_cm as resp:
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

        part.parent.mkdir(parents=True, exist_ok=True)
        with open(part, mode) as fh:
            while True:
                if cancel is not None and cancel.cancelled:
                    raise _cancelled()
                block = resp.read(chunk)
                if not block:
                    break
                fh.write(block)
                done += len(block)
                if done > limit:
                    raise IntegrityError(f"download exceeded {limit} bytes for {url}")
                _emit_progress(emit, "download", done, total)
            fh.flush()
            os.fsync(fh.fileno())

        if total >= 0 and done != total:
            raise DownloadError(f"incomplete download: {done} of {total} bytes")
        return resumed


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
) -> DownloadResult:
    """Download ``url`` to ``dest`` atomically.

    Retries transient network errors with exponential backoff. The file is
    verified against ``expected_sha256``/``expected_sha512``/``expected_size``
    before ``os.replace`` puts it in place; a mismatch raises
    :class:`IntegrityError` and leaves no partial file at ``dest``.
    """
    say = emitter(emit)
    destination = Path(dest).expanduser()
    destination.parent.mkdir(parents=True, exist_ok=True)
    part = destination.with_name(destination.name + ".part")
    _drop_symlinked_part(part)

    if (
        resume
        and (expected_sha256 or expected_sha512 or expected_size is not None)
        and verify_file(
            part, sha256=expected_sha256, sha512=expected_sha512, size=expected_size
        )
    ):
        os.replace(part, destination)
        digests = hash_file(destination, ("sha256", "sha512"))
        say(Event("log", f"reused verified download: {destination.name}"))
        return DownloadResult(
            destination,
            destination.stat().st_size,
            digests["sha256"],
            digests["sha512"],
            resumed=True,
        )

    last_error: Exception | None = None
    resumed = False
    attempts = max(1, retries)
    attempt = 0
    refetch_clean = False
    while attempt < attempts:
        completed = False
        try:
            resumed = _download_once(
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
            )
            completed = True
            size = part.stat().st_size
            digests = hash_file(part, ("sha256", "sha512"))
            if expected_size is not None and size != expected_size:
                raise IntegrityError(
                    f"size mismatch for {destination.name}: {size} != {expected_size}"
                )
            if expected_sha256 and digests["sha256"].lower() != expected_sha256.lower():
                raise IntegrityError(f"sha256 mismatch for {destination.name}")
            if expected_sha512 and digests["sha512"].lower() != expected_sha512.lower():
                raise IntegrityError(f"sha512 mismatch for {destination.name}")
            os.replace(part, destination)
            _fsync_dir(destination.parent)
            say(
                Event(
                    "log",
                    f"verified {destination.name} ({size} bytes)",
                    data={"sha256": digests["sha256"], "sha512": digests["sha512"]},
                )
            )
            return DownloadResult(
                destination, size, digests["sha256"], digests["sha512"], resumed=resumed
            )
        except IntegrityError:
            # A corrupt transfer (bad hash/size after the bytes arrived) is not
            # worth retrying byte for byte: drop the part and refetch once from
            # zero, which recovers from a stale/wrong-prefix ``.part`` instead
            # of failing the call. Protocol-level failures raised *inside* the
            # transfer (e.g. an oversized body) are not retried: the server
            # would just repeat them.
            _remove(part)
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
            if isinstance(exc, OSError) and exc.errno in _FATAL_WRITE_ERRNOS:
                # A full/read-only/unwritable destination will fail the same way
                # on every retry; tell the user what to fix instead.
                _remove_if_empty(part)
                if exc.errno in (errno.ENOSPC, getattr(errno, "EDQUOT", -1)):
                    raise DownloadError(
                        f"not enough disk space to download {destination.name}"
                    ) from exc
                raise DownloadError(f"cannot write to {destination}") from exc
            last_error = exc
            attempt += 1
            if attempt >= attempts:
                break
            delay = min(backoff * (2 ** (attempt - 1)), MAX_BACKOFF)
            say(
                Event(
                    "log",
                    f"download error ({exc}); retrying in {delay:.0f}s",
                    data={"attempt": attempt, "retries": attempts},
                )
            )
            _sleep(delay, cancel)

    _remove_if_empty(part)  # a network failure keeps what arrived, so a retry resumes
    raise DownloadError(f"download failed for {url}: {last_error}") from last_error


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
            total = response.headers.get("Content-Range", "").rpartition("/")[2]
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

    __slots__ = ("status", "headers", "_response")

    def __init__(self, response: http.client.HTTPResponse) -> None:
        self._response = response
        self.status = response.status
        self.headers = response.headers

    def read(self, amount: int = -1) -> bytes:
        return self._response.read(amount)

    def header(self, name: str, default: str | None = None) -> str | None:
        return self._response.getheader(name, default)

    def close(self) -> None:
        try:
            self._response.close()
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

    def _store(self) -> dict[tuple[str, str, int], http.client.HTTPConnection]:
        store = getattr(self._local, "connections", None)
        if store is None:
            store = {}
            self._local.connections = store
        return store

    def _connection(
        self, scheme: str, host: str, port: int, timeout: int
    ) -> http.client.HTTPConnection:
        key = (scheme, host, port)
        store = self._store()
        conn = store.get(key)
        if conn is not None:
            return conn
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
        store[key] = conn
        with self._lock:
            self._all.append(conn)
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

    def open(
        self,
        url: str,
        *,
        headers: Mapping[str, str] | None = None,
        timeout: int | None = None,
        redirects: int = 5,
        redirect_allowed: Callable[[str], bool] | None = None,
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
            conn = self._connection(scheme, host, port, deadline)
            proxy = _proxy_for(scheme, host)
            send_headers = request_headers
            if proxy is not None and scheme == "http":
                # A plain-HTTP proxy wants the absolute URL and its own credentials.
                path = current
                send_headers = {**request_headers, **proxy[2]}
            try:
                conn.request("GET", path, headers=send_headers)
                response = conn.getresponse()
            except (http.client.HTTPException, OSError):
                # A pooled keep-alive connection may have been closed by the
                # peer; drop it and retry once on a fresh socket.
                self._forget(scheme, host, port)
                conn = self._connection(scheme, host, port, deadline)
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
                    raise DownloadError(
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
                    raise DownloadError(f"redirect to disallowed URL: {target}")
                current = target
                continue
            return HttpResponse(response)
        raise DownloadError(f"too many redirects for {url}")

    def close(self) -> None:
        with self._lock:
            connections, self._all = self._all, []
        for conn in connections:
            try:
                conn.close()
            except OSError:
                pass


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
        raise DownloadError(f"invalid URL host: {url!r}") from exc
    if not host:
        raise DownloadError(f"URL has no host: {url!r}")
    try:
        port = parsed.port or (443 if scheme == "https" else 80)
    except ValueError as exc:
        # A malformed port (e.g. ``https://host:abc/``) must not escape as a
        # raw ``ValueError`` from the redirect path (N4).
        raise DownloadError(f"invalid port in URL: {url!r}") from exc
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
                block = response.read(chunk)
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
