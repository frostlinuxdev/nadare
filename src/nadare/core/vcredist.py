# SPDX-License-Identifier: GPL-3.0-or-later
# Copyright (C) 2026 Nadare contributors
"""Backend-agnostic Microsoft Visual C++ runtime install helper.

Wine's builtin ``vcruntime140``/``msvcp140``/``concrt140`` stand-ins cause
multi-second main-thread freezes in some games; every Proton/GE/UMU runner and
plain Wine ship only those builtin placeholders, so launchers must install the
real 2015-2022 x64 redistributable into the prefix.

Everything here is shared logic only; a backend supplies the way to run an
installer inside its prefix (``run_in_prefix``) plus its profile/emitter/cancel
handles. ``ensure`` is deliberately best-effort: any failure becomes a single
``status`` event (the game may stutter) and only a user cancellation propagates.
"""

from __future__ import annotations

import hashlib
import json
import os
import re
import urllib.parse
import urllib.request
from collections.abc import Callable
from pathlib import Path
from typing import Any

from . import download as _net
from . import hosting
from .backends import BackendError
from .events import Cancelled, Event

#: Microsoft's "Visual C++ 2015-2022 x64 Redistributable". ``aka.ms`` is a
#: mutable redirector, so the download is accepted only when every hop stays on
#: a Microsoft host and the bytes match a pinned SHA-256.
VCREDIST_URL = "https://aka.ms/vs/17/release/vc_redist.x64.exe"
#: Pinned 2026-10-13 (build 14.44.35211.0). Microsoft re-issues this file, so the
#: pin goes stale; override with NADARE_VCREDIST_SHA256 or the profile's
#: ``vcredist_sha256`` until it is bumped. Accepting the published index hashes
#: (below) keeps installs working across re-issues without a Nadare release.
VCREDIST_SHA256: str | None = "cc0ff0eb1dc3f5188ae6300faef32bf5beeba4bdd6e8e445a9184072096b713b"
#: Hashes published in the project's index are accepted (cap, like WebView2).
VCREDIST_INDEX_MAX_HASHES = 8
#: Redirect targets of the aka.ms link that are allowed to serve the installer.
VCREDIST_HOST_SUFFIXES: tuple[str, ...] = (".microsoft.com", ".aka.ms")

#: A pinned installer hash must be exactly 64 lowercase hex digits.
_SHA256_RE = re.compile(r"^[0-9a-f]{64}$")

#: Exit codes that mean "installed" (1638 = already installed, 3010 = reboot
#: pending), exactly as the CZN backend always accepted them.
_ACCEPTED_EXIT_CODES = frozenset({0, 1638, 3010})

#: The installer's on-disk name inside a backend's cache directory.
_INSTALLER_NAME = "vc_redist.x64.exe"


def _microsoft_host(host: str) -> bool:
    """``aka.ms`` itself or a label-bounded subdomain of an allowed suffix."""
    return host == "aka.ms" or host.endswith(VCREDIST_HOST_SUFFIXES)


def _setting(profile: dict[str, Any], key: str) -> Any:
    """Read a profile key from the top level or its ``extra`` mapping."""
    value = profile.get(key)
    if value not in (None, ""):
        return value
    extra = profile.get("extra")
    if isinstance(extra, dict):
        return extra.get(key)
    return None


def installed(prefix: Path) -> bool:
    """Whether the real (not Wine-builtin) VC++ runtime is in ``prefix``.

    ``vcruntime140.dll`` and ``msvcp140.dll`` count only when both exist and
    neither is one of Wine's builtin placeholders, whose first bytes carry the
    ``Wine builtin DLL`` marker.
    """
    root = prefix / "drive_c/windows/system32"
    for name in ("vcruntime140.dll", "msvcp140.dll"):
        try:
            with open(root / name, "rb") as f:
                head = f.read(4096)
        except OSError:
            return False
        if b"Wine builtin DLL" in head:
            return False
    return True


def resolve_pin(
    profile: dict[str, Any],
    constant: str | None = VCREDIST_SHA256,
    *,
    environ: dict[str, str] | None = None,
) -> str:
    """Resolve the expected installer SHA-256 from a trusted source.

    Precedence: the ``NADARE_VCREDIST_SHA256`` environment variable, then the
    profile's ``vcredist_sha256`` setting (top level or ``extra``), then the
    caller's constant. An empty result means "not pinned".
    """
    env = os.environ if environ is None else environ
    for value in (
        env.get("NADARE_VCREDIST_SHA256"),
        _setting(profile, "vcredist_sha256"),
        constant,
    ):
        if value:
            text = str(value).strip().lower()
            if text:
                return text
    return ""


def overridden(
    profile: dict[str, Any], *, environ: dict[str, str] | None = None
) -> bool:
    """True when the user pinned a hash explicitly (env or profile)."""
    env = os.environ if environ is None else environ
    for value in (env.get("NADARE_VCREDIST_SHA256"), _setting(profile, "vcredist_sha256")):
        if value and str(value).strip():
            return True
    return False


def index_pins() -> set[str]:
    """Installer hashes published in the Nadare index (empty on any error).

    Best effort and offline-safe: a missing, slow or malformed index just means
    the built-in pin is the only accepted hash.
    """
    url = hosting.vcredist_index_url()
    if not url.startswith("https://"):
        return set()
    try:
        req = urllib.request.Request(url, headers={"User-Agent": "nadare"})
        with urllib.request.urlopen(req, timeout=10) as r:
            payload = json.loads(r.read(65536).decode("utf-8"))
        hashes = payload["sha256"]
    except Exception:
        return set()
    if not isinstance(hashes, list):
        return set()
    return {
        h.strip().lower()
        for h in hashes[:VCREDIST_INDEX_MAX_HASHES]
        if isinstance(h, str) and _SHA256_RE.fullmatch(h.strip().lower())
    }


def index_source() -> str:
    """The immutable per-build installer URL published in the index.

    Empty on any problem (https and a Microsoft host are required), so the
    ``aka.ms`` fwlink remains the fallback.
    """
    url = hosting.vcredist_index_url()
    if not url.startswith("https://"):
        return ""
    try:
        req = urllib.request.Request(url, headers={"User-Agent": "nadare"})
        with urllib.request.urlopen(req, timeout=10) as r:
            payload = json.loads(r.read(65536).decode("utf-8"))
        source = payload["url"]
    except Exception:
        return ""
    if not isinstance(source, str):
        return ""
    parts = urllib.parse.urlparse(source)
    host = (parts.hostname or "").lower()
    if parts.scheme != "https" or not _microsoft_host(host):
        return ""
    return source


def verify(
    path: Path,
    *,
    profile: dict[str, Any],
    emit: Callable[[Event], None],
    cancel: Any,
    constant: str | None = VCREDIST_SHA256,
    index_pins_fn: Callable[[], set[str]] | None = None,
    overridden_fn: Callable[[], bool] | None = None,
) -> None:
    """Refuse an installer that matches no accepted pin (deletes it)."""
    pin = resolve_pin(profile, constant)
    if not pin or not _SHA256_RE.fullmatch(pin):
        raise BackendError(
            "the configured Visual C++ SHA-256 is missing or malformed; refusing"
        )
    digest = _sha256_file(path)
    if digest == pin:
        return
    is_overridden = overridden_fn() if overridden_fn else overridden(profile)
    if not is_overridden:
        if cancel.cancelled:
            raise Cancelled()
        pins = (index_pins_fn or index_pins)()
        if cancel.cancelled:
            raise Cancelled()
        if digest in pins:
            return
    path.unlink(missing_ok=True)
    raise BackendError(f"vc_redist.x64.exe failed its SHA-256 check: {path.name}")


def download(
    url: str,
    tmp: Path,
    inst: Path,
    *,
    verify_fn: Callable[[Path], None],
    emit: Callable[[Event], None],
    cancel: Any,
) -> None:
    """Download ``url`` to ``tmp``, verify it, and move it to ``inst``.

    The request URL and the redirect target must both be https on a Microsoft
    host (the ``aka.ms`` fwlink and ``*.microsoft.com`` serve the installer).
    """
    request = urllib.parse.urlparse(url)
    request_host = (request.hostname or "").lower()
    if request.scheme != "https" or not _microsoft_host(request_host):
        raise BackendError(f"Visual C++ download rejected off Microsoft: {url}")
    def _hop_ok(target: str) -> bool:
        hop = urllib.parse.urlparse(target)
        return hop.scheme == "https" and _microsoft_host((hop.hostname or "").lower())

    try:
        response = _net.http_open(url, timeout=60, redirect_allowed=_hop_ok)
    except _net.DownloadError as exc:
        raise BackendError(f"Visual C++ download failed: {exc}") from exc
    with response as r, open(tmp, "wb") as f:
        final = urllib.parse.urlparse(r.geturl())
        host = (final.hostname or "").lower()
        if final.scheme != "https" or not _microsoft_host(host):
            raise BackendError(
                f"Visual C++ download redirected off Microsoft: {r.geturl()}"
            )
        total = int(r.headers.get("Content-Length") or 0)
        done = 0
        while chunk := r.read(1 << 20):
            if cancel.cancelled:
                raise Cancelled()
            f.write(chunk)
            done += len(chunk)
            if done > (total or _net.MAX_DOWNLOAD_BYTES):
                f.close()
                tmp.unlink(missing_ok=True)
                raise BackendError("Visual C++ download exceeded its declared size")
            if total:
                emit(Event("progress", "Visual C++ runtime", progress=done / total))
    verify_fn(tmp)
    tmp.replace(inst)


def install(
    prefix: Path,
    *,
    profile: dict[str, Any],
    cache_dir: Path,
    run_in_prefix: Callable[[list[str]], int],
    emit: Callable[[Event], None],
    cancel: Any,
    constant: str | None = VCREDIST_SHA256,
    index_pins_fn: Callable[[], set[str]] | None = None,
    index_source_fn: Callable[[], str] | None = None,
    overridden_fn: Callable[[], bool] | None = None,
    verify_fn: Callable[[Path], None] | None = None,
    download_fn: Callable[[str, Path, Path], None] | None = None,
) -> None:
    """Download, verify and run the VC++ redistributable (may raise)."""
    emit(
        Event("step", "Installing the Microsoft Visual C++ runtime (one time, ~25 MB)…")
    )
    cache_dir.mkdir(parents=True, exist_ok=True)
    inst = cache_dir / _INSTALLER_NAME
    pins_fn = index_pins_fn or index_pins
    source_fn = index_source_fn or index_source
    over_fn = overridden_fn or (lambda: overridden(profile))

    def _verify(path: Path) -> None:
        if verify_fn is not None:
            verify_fn(path)
            return
        verify(
            path,
            profile=profile,
            emit=emit,
            cancel=cancel,
            constant=constant,
            index_pins_fn=pins_fn,
            overridden_fn=over_fn,
        )

    def _download(url: str, tmp: Path, dest: Path) -> None:
        if download_fn is not None:
            download_fn(url, tmp, dest)
            return
        download(url, tmp, dest, verify_fn=_verify, emit=emit, cancel=cancel)

    if inst.is_file() and inst.stat().st_size:
        try:
            _verify(inst)
        except BackendError:
            _unlink_quietly(inst)
    if not (inst.is_file() and inst.stat().st_size):
        tmp = inst.with_suffix(".part")
        urls = [VCREDIST_URL]
        if not over_fn():
            if cancel.cancelled:
                raise Cancelled()
            indexed = source_fn()
            if cancel.cancelled:
                raise Cancelled()
            if indexed and indexed != VCREDIST_URL:
                urls.insert(0, indexed)
        failure: BaseException | None = None
        for url in urls:
            try:
                _download(url, tmp, inst)
                failure = None
                break
            except Cancelled:
                tmp.unlink(missing_ok=True)
                raise
            except Exception as e:  # noqa: BLE001 - reported, not fatal
                tmp.unlink(missing_ok=True)
                failure = e
        if failure is not None:
            raise failure
    emit(Event("step", "Installing the Visual C++ runtime into the prefix…"))
    code = run_in_prefix([str(inst), "/install", "/quiet", "/norestart"])
    if code not in _ACCEPTED_EXIT_CODES:
        raise BackendError(f"the Visual C++ installer exited {code}")


def ensure(
    prefix: Path,
    *,
    profile: dict[str, Any],
    cache_dir: Path,
    run_in_prefix: Callable[[list[str]], int],
    emit: Callable[[Event], None],
    cancel: Any,
    constant: str | None = VCREDIST_SHA256,
    index_pins_fn: Callable[[], set[str]] | None = None,
    index_source_fn: Callable[[], str] | None = None,
    overridden_fn: Callable[[], bool] | None = None,
    verify_fn: Callable[[Path], None] | None = None,
    download_fn: Callable[[str, Path, Path], None] | None = None,
    install_fn: Callable[[], None] | None = None,
) -> None:
    """Best-effort VC++ runtime install; never aborts the caller.

    This is an optimisation, so every failure becomes a single ``status`` event:
    the game still launches, it may just stutter. Only a user cancellation
    propagates. File contents are never logged.
    """
    if installed(prefix):
        return
    if install_fn is None:

        def install_fn() -> None:
            install(
                prefix,
                profile=profile,
                cache_dir=cache_dir,
                run_in_prefix=run_in_prefix,
                emit=emit,
                cancel=cancel,
                constant=constant,
                index_pins_fn=index_pins_fn,
                index_source_fn=index_source_fn,
                overridden_fn=overridden_fn,
                verify_fn=verify_fn,
                download_fn=download_fn,
            )

    try:
        install_fn()
    except Cancelled:
        raise
    except BackendError as e:
        reason = e.message
    except OSError:
        reason = "download failed"
    except Exception:  # noqa: BLE001 - an optimisation must never abort
        reason = "install failed"
    else:
        return
    emit(
        Event(
            "status",
            f"Visual C++ runtime not installed ({reason}): the game may stutter",
        )
    )


def _sha256_file(path: Path) -> str:
    """Streamed SHA-256 of a file, so large installers are not read at once."""
    h = hashlib.sha256()
    with open(path, "rb") as f:
        for chunk in iter(lambda: f.read(1 << 20), b""):
            h.update(chunk)
    return h.hexdigest()


def _unlink_quietly(path: Path) -> None:
    try:
        Path(path).unlink()
    except OSError:
        pass


__all__ = [
    "VCREDIST_HOST_SUFFIXES",
    "VCREDIST_INDEX_MAX_HASHES",
    "VCREDIST_SHA256",
    "VCREDIST_URL",
    "download",
    "ensure",
    "index_pins",
    "index_source",
    "install",
    "installed",
    "overridden",
    "resolve_pin",
    "verify",
]
