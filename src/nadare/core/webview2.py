# SPDX-License-Identifier: GPL-3.0-or-later
# Copyright (C) 2026 Nadare contributors
"""Backend-agnostic Microsoft WebView2 Runtime install pipeline.

The CZN backend and the GFL2 Haoplay build both need Microsoft's WebView2
Runtime in their prefix. Everything here is shared logic only; a backend
supplies the way to run the installer inside its prefix (``run_in_prefix``)
plus its profile/emitter/cancel handles. ``ensure`` refuses to download or run
an installer that cannot be verified against a trusted pin.
"""

from __future__ import annotations

import logging
import os
import shutil
import urllib.parse
import uuid
from collections.abc import Callable
from pathlib import Path
from typing import Any

from . import download as net
from . import graphics, indexfetch
from .backends import BackendError
from .events import Cancelled, Event
from .vcredist import _SHA256_RE, _setting, _sha256_file, _unlink_quietly

log = logging.getLogger(__name__)

#: Backend-side exit codes for WebView2 failures (not czn-linux exit codes).
EXIT_WEBVIEW2_UNVERIFIED = 10
EXIT_WEBVIEW2_FAILED = 11

#: Microsoft re-issues the installer with every Edge release, so a built-in
#: pin alone would break installs on older Nadare builds. Hashes published in
#: the project's index (refreshed by ``scripts/publish_index.py``) are accepted
#: as well, unless the user set an explicit override.
WEBVIEW2_INDEX_MAX_HASHES = 8
#: Redirect targets of the fwlink that are allowed to serve the installer.
WEBVIEW2_HOST_SUFFIXES: tuple[str, ...] = (".microsoft.com",)

#: Backend-side (not czn-linux) exit codes for WebView2 failures.
MESSAGES: dict[int, str] = {
    EXIT_WEBVIEW2_UNVERIFIED: "Nadare could not verify Microsoft's WebView2 installer. "
    "Try Install again; if it keeps failing, update Nadare and try again.",
    EXIT_WEBVIEW2_FAILED: "Microsoft's WebView2 installer did not pass a safety check. "
    "Try Install again; if it keeps failing, update Nadare and try again.",
}


def installed(prefix: Path) -> bool:
    """Whether the WebView2 runtime executable is already in ``prefix``."""
    root = prefix / "drive_c/Program Files (x86)/Microsoft/EdgeWebView/Application"
    return any(root.glob("*/msedgewebview2.exe"))


def resolve_pin(profile: dict[str, Any], constant: str | None) -> str:
    """Resolve the expected installer SHA-256 from a trusted source.

    Precedence: the ``NADARE_WEBVIEW2_SHA256`` environment variable, then
    the profile's ``webview2_sha256`` setting (top level or ``extra``), then
    the caller's constant. The hash is never hardcoded, so a shipped build
    cannot silently skip verification; an empty result means "not pinned".
    A whitespace-only value is treated as unset and falls through rather
    than masking a lower-precedence pin.
    """
    for value in (
        os.environ.get("NADARE_WEBVIEW2_SHA256"),
        _setting(profile, "webview2_sha256"),
        constant,
    ):
        if value:
            text = str(value).strip().lower()
            if text:
                return text
    return ""


def pin_error(pin: str) -> BackendError:
    """A fail-fast :class:`BackendError` for an absent/malformed pin."""
    if pin and not _SHA256_RE.fullmatch(pin):
        return BackendError(
            "the configured WebView2 SHA-256 is malformed (expected 64 hex "
            "digits); refusing before the download",
            EXIT_WEBVIEW2_UNVERIFIED,
        )
    return BackendError(MESSAGES[EXIT_WEBVIEW2_UNVERIFIED], EXIT_WEBVIEW2_UNVERIFIED)


def overridden(profile: dict[str, Any]) -> bool:
    """True when the user pinned a hash explicitly (env or profile)."""
    for value in (
        os.environ.get("NADARE_WEBVIEW2_SHA256"),
        _setting(profile, "webview2_sha256"),
    ):
        if value and str(value).strip():
            return True
    return False


def _index_document() -> Any:
    """The published WebView2 index JSON, or ``None`` when unavailable."""
    try:
        return indexfetch.fetch_json(
            "webview2/latest.json", validate=indexfetch.looks_like_pins_doc
        ).data
    except Exception:
        return None


def index_pins() -> set[str]:
    """Installer hashes published in the Nadare index (empty on any error).

    Best effort and offline-safe: a missing, slow or malformed index just
    means the built-in pin is the only accepted hash.
    """
    payload = _index_document()
    if not isinstance(payload, dict):
        return set()
    hashes = payload.get("sha256")
    if not isinstance(hashes, list):
        return set()
    # Validate first, cap after: junk entries at the front must not crowd out
    # the real hashes behind them.
    valid = [
        h.strip().lower()
        for h in hashes
        if isinstance(h, str) and _SHA256_RE.fullmatch(h.strip().lower())
    ]
    return set(valid[:WEBVIEW2_INDEX_MAX_HASHES])


def index_source() -> str:
    """The immutable per-build installer URL published in the index.

    Microsoft's fwlink always serves the newest build, so its hash can
    change under us. The index records the exact build URL the published
    hash belongs to; downloading that keeps hash and bytes consistent even
    right after a Microsoft release. Empty on any problem (https and a
    Microsoft host are required).
    """
    payload = _index_document()
    if not isinstance(payload, dict):
        return ""
    source = payload.get("url")
    if not isinstance(source, str):
        return ""
    parts = urllib.parse.urlparse(source)
    host = (parts.hostname or "").lower()
    if parts.scheme != "https" or not host.endswith(WEBVIEW2_HOST_SUFFIXES):
        return ""
    return source


def verify(
    path: Path,
    *,
    profile: dict[str, Any],
    cancel: Any,
    constant: str | None,
    index_pins_fn: Callable[[], set[str]] | None = None,
    overridden_fn: Callable[[], bool] | None = None,
) -> None:
    """Refuse a WebView2 installer that does not match the pinned SHA-256.

    Every failure is a :class:`BackendError`, so the cached-copy re-verify
    is mapped to an exit code exactly like the download path (no raw
    ``OSError`` escapes).
    """
    pin = resolve_pin(profile, constant)
    if not pin:
        log.debug("WebView2 installer has no pinned SHA-256")
        raise BackendError(MESSAGES[EXIT_WEBVIEW2_UNVERIFIED], EXIT_WEBVIEW2_UNVERIFIED)
    if not _SHA256_RE.fullmatch(pin):
        raise pin_error(pin)
    try:
        digest = _sha256_file(path)
    except OSError as e:
        raise BackendError(f"cannot read the WebView2 installer: {e}", EXIT_WEBVIEW2_FAILED) from e
    if digest != pin:
        log.debug(
            "WebView2 installer digest mismatch for %s: got %s, expected %s",
            path,
            digest,
            pin,
        )
        is_overridden = overridden_fn() if overridden_fn else overridden(profile)
        if is_overridden:
            path.unlink(missing_ok=True)
            raise BackendError(
                MESSAGES[EXIT_WEBVIEW2_FAILED],
                EXIT_WEBVIEW2_FAILED,
            )
        if cancel.cancelled:
            raise Cancelled()
        pins = (index_pins_fn or index_pins)()
        if cancel.cancelled:
            raise Cancelled()
        if digest in pins:
            return
        path.unlink(missing_ok=True)
        raise BackendError(
            MESSAGES[EXIT_WEBVIEW2_FAILED],
            EXIT_WEBVIEW2_FAILED,
        )


def download(
    url: str,
    tmp: Path,
    inst: Path,
    *,
    verify_fn: Callable[[Path], None],
    emit: Callable[[Event], None],
    cancel: Any,
) -> None:
    """Download ``url`` to ``tmp``, verify it, and move it to ``inst``."""

    def _hop_ok(target: str) -> bool:
        hop = urllib.parse.urlparse(target)
        return hop.scheme == "https" and (hop.hostname or "").lower().endswith(
            WEBVIEW2_HOST_SUFFIXES
        )

    try:
        response = net.http_open(url, timeout=60, redirect_allowed=_hop_ok)
    except net.DownloadError as e:
        raise BackendError(f"WebView2 download failed: {e}", EXIT_WEBVIEW2_FAILED) from e
    with response as r, open(tmp, "wb") as f:
        if not _hop_ok(r.geturl()):
            raise BackendError(
                f"WebView2 download redirected off Microsoft: {r.geturl()}",
                EXIT_WEBVIEW2_FAILED,
            )
        total = int(r.headers.get("Content-Length") or 0)
        done = 0
        while chunk := r.read(1 << 20):
            if cancel.cancelled:
                raise Cancelled()
            f.write(chunk)
            done += len(chunk)
            if done > (total or net.MAX_DOWNLOAD_BYTES):
                f.close()
                tmp.unlink(missing_ok=True)
                raise BackendError(
                    "WebView2 download exceeded its declared size",
                    EXIT_WEBVIEW2_FAILED,
                )
            if total:
                emit(Event("progress", "WebView2", progress=done / total))
    verify_fn(tmp)
    tmp.replace(inst)


def ensure(
    prefix: Path,
    *,
    profile: dict[str, Any],
    cache_dir_fn: Callable[[], Path],
    run_in_prefix: Callable[[list[str]], int],
    emit: Callable[[Event], None],
    cancel: Any,
    constant: str | None,
    url: str,
    installed_fn: Callable[[], bool] | None = None,
    pin_fn: Callable[[], str],
    index_pins_fn: Callable[[], set[str]],
    index_source_fn: Callable[[], str],
    overridden_fn: Callable[[], bool],
    verify_fn: Callable[[Path], None],
    download_fn: Callable[[str, Path, Path], None],
    installer_environment: Callable[[], Any],
    activity_label: Callable[[str], Any],
    explain: Callable[[str], str],
) -> None:
    """Download, verify and install the WebView2 Runtime into ``prefix``.

    ``installed_fn`` defaults to looking in ``prefix``; a backend passes its own so the
    check before and after the installer goes through the same seam.
    """
    is_installed = installed_fn if installed_fn is not None else (lambda: installed(prefix))
    if is_installed():
        return
    # Fail fast before spending the ~215 MB download: without a valid pin
    # there is nothing to verify against, so refuse up front.
    pin = pin_fn()
    if not pin or not _SHA256_RE.fullmatch(pin):
        raise pin_error(pin)
    # The installer needs a display and hangs without one (#437), so refuse
    # up front too instead of downloading ~215 MB first.
    if not graphics.has_display():
        raise BackendError(
            "No display found (DISPLAY and WAYLAND_DISPLAY are unset), and the "
            "WebView2 installer needs one. Run the install from a desktop session, "
            "or over ssh with DISPLAY set (for example ssh -X, or DISPLAY=:0 on "
            "the machine's own screen).",
            EXIT_WEBVIEW2_FAILED,
        )

    # FRESH-2: an unwritable/full cache dir must map to a BackendError like
    # every other WebView2 failure, not escape as a raw OSError.
    try:
        cache_dir = cache_dir_fn()
        cache_dir.mkdir(parents=True, exist_ok=True)
        inst = cache_dir / "MicrosoftEdgeWebView2RuntimeInstallerX64.exe"
        cached = inst.is_file() and bool(inst.stat().st_size)
    except OSError as e:
        raise BackendError(f"cannot access the WebView2 cache: {e}", EXIT_WEBVIEW2_FAILED) from e
    work_id = f"{os.getpid()}-{uuid.uuid4().hex}"
    if not cached:
        emit(Event("step", "Downloading the Microsoft WebView2 Runtime (~215 MB)…"))
        tmp = inst.with_name(f"{inst.name}.{work_id}.part")
        urls = [url]
        if not overridden_fn():
            if cancel.cancelled:
                raise Cancelled()
            indexed = index_source_fn()
            if cancel.cancelled:
                raise Cancelled()
            if indexed and indexed != url:
                urls.insert(0, indexed)
        failure: BackendError | None = None
        for source in urls:
            try:
                download_fn(source, tmp, inst)
                failure = None
                break
            except Cancelled:
                tmp.unlink(missing_ok=True)
                raise
            except BackendError as e:
                tmp.unlink(missing_ok=True)
                failure = e
            except Exception as e:
                tmp.unlink(missing_ok=True)
                failure = BackendError(f"WebView2 download failed: {e}", EXIT_WEBVIEW2_FAILED)
        if failure is not None:
            raise failure
    else:
        # N3: a cached installer that no longer matches the pinned hash must
        # be evicted here, or it would be re-verified as a throwaway copy on
        # every attempt and wedge install() forever. Removing it makes the
        # next attempt re-download a fresh copy.
        try:
            verify_fn(inst)
        except BackendError as exc:
            _unlink_quietly(inst)
            raise BackendError(
                f"{exc.message} (the cached copy was removed; retry the install "
                "to download a fresh one)",
                EXIT_WEBVIEW2_FAILED,
            ) from None
    # FRESH-6: verify a private, read-only copy and run *that*, so the
    # verified bytes cannot be swapped between the hash check and the exec.
    run_path = cache_dir / f".webview2-run-{work_id}.exe"
    try:
        try:
            shutil.copyfile(inst, run_path)
            os.chmod(run_path, 0o400)
        except OSError as e:
            raise BackendError(
                f"cannot stage the WebView2 installer: {e}", EXIT_WEBVIEW2_FAILED
            ) from e
        verify_fn(run_path)
        emit(Event("step", "Installing the WebView2 Runtime into the prefix…"))
        with installer_environment(), activity_label("Installing the WebView2 Runtime"):
            installer_code = run_in_prefix([str(run_path), "/silent", "/install"])
    finally:
        _unlink_quietly(run_path)
    if not is_installed():
        raise BackendError(
            explain(
                f"WebView2 Runtime install failed (installer exit code {installer_code}). "
                "Check the install log; a missing GPU/Vulkan driver (for example a VM), "
                "a full disk or a blocked download are the usual causes"
            ),
            EXIT_WEBVIEW2_FAILED,
        )


__all__ = [
    "EXIT_WEBVIEW2_FAILED",
    "EXIT_WEBVIEW2_UNVERIFIED",
    "MESSAGES",
    "WEBVIEW2_HOST_SUFFIXES",
    "WEBVIEW2_INDEX_MAX_HASHES",
    "download",
    "ensure",
    "index_pins",
    "index_source",
    "installed",
    "overridden",
    "pin_error",
    "resolve_pin",
    "verify",
]
