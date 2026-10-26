# SPDX-License-Identifier: GPL-3.0-or-later
# Copyright (C) 2026 Nadare contributors
"""The "new version available" check.

Compares the running Nadare version against the project's "latest release"
endpoint and reports a newer release when there is one. All network access is
funnelled through :func:`nadare.core.download.fetch_json` and can be replaced
by injecting ``fetch``, so the whole check is testable without a network.
"""

from __future__ import annotations

import inspect
import json
import logging
import os
import re
import time
import urllib.request
from collections.abc import Callable
from dataclasses import dataclass
from typing import Any
from urllib.parse import urljoin, urlparse

from . import download, hosting, paths, sandbox

logger = logging.getLogger(__name__)

_CORE_RE = re.compile(r"^\d+(\.\d+)*$")
_DIGITS_RE = re.compile(r"(\d+)$")
#: PEP 440 pre-release suffix glued to the core, e.g. ``0.1.0b1`` / ``2.0rc2``.
_PEP440_RE = re.compile(r"^(\d+(?:\.\d+)*)(a|b|rc)(\d*)$")
_PEP440_LABEL = {"a": "alpha", "b": "beta", "rc": "rc"}

#: Components kept when padding a version for comparison; enough for any real
#: release while still distinguishing ``1.2.3`` from ``1.2.3.4``.
_VERSION_WIDTH = 8

#: Release-page hosts a known API host may legitimately link to.
_RELEASE_SITE_ALIASES = {"api.github.com": "github.com"}

#: How long a good answer is reused, and how long to leave the network alone after a failure
#: (GitHub's anonymous API allows 60 requests an hour per IP, which a shared IP or VPN exhausts).
CACHE_NAME = "update-check.json"
FRESH_SECONDS = 6 * 3600
BACKOFF_SECONDS = 3600
_PAYLOAD_KEYS = ("tag_name", "html_url", "name", "prerelease", "draft")
_GITHUB_LATEST_API = re.compile(
    r"https://api\.github\.com/repos/([A-Za-z0-9._-]+)/([A-Za-z0-9._-]+)/releases/latest"
)

#: Position of the "plain release" flag in the three-slot tail of a
#: :func:`version_key` tuple: ``1`` for a final release, ``0`` for a pre-release.
_PLAIN_TAIL_INDEX = -3

__all__ = [
    "UpdateInfo",
    "check_latest",
    "is_prerelease",
    "managed_updates",
    "newer",
    "version_key",
]


@dataclass(frozen=True)
class UpdateInfo:
    """A release newer than the running version."""

    version: str
    url: str
    name: str


def _strip_prefix(tag: str) -> str:
    text = tag.strip()
    if text[:1] in ("v", "V"):
        text = text[1:]
    return text


def version_key(tag: Any) -> tuple[int, ...] | None:
    """Turn a release tag into a comparable tuple, or ``None`` if unparseable.

    ``"0.1"`` and ``"0.1.0"`` agree; a suffix sorts *below* the same plain
    version (``1.0.0-beta.2`` < ``1.0.0``), labels rank ``alpha`` < ``beta`` <
    ``rc`` (unknown labels between ``beta`` and ``rc``) and the trailing number
    breaks ties (``beta.2`` < ``beta.10``, a bare ``rc`` counts as ``0``).
    """
    if not isinstance(tag, str):
        return None
    text = _strip_prefix(tag)
    if not text:
        return None
    core, sep, pre = text.partition("-")
    if not sep:
        # Accept PEP 440 spellings such as ``0.1.0b1`` / ``2.0rc2`` as well as
        # the dash form (``0.1.0-beta.1``).
        pep = _PEP440_RE.match(core)
        if pep:
            core, sep, pre = pep.group(1), "-", pep.group(2) + pep.group(3)
    if not _CORE_RE.match(core):
        return None
    nums = [int(part) for part in core.split(".")]
    nums = (nums + [0] * _VERSION_WIDTH)[:_VERSION_WIDTH]
    if not sep:
        return (*nums, 1, 0, 0)
    # Normalise a single-letter marker so ``b1`` ranks like ``beta.1``.
    marker = re.fullmatch(r"(a|b|rc)(\d*)", pre.lower())
    if marker:
        pre = _PEP440_LABEL[marker.group(1)] + marker.group(2)
    label = pre.lower()
    rank = (
        0
        if label.startswith("alpha")
        else 1
        if label.startswith("beta")
        else 3
        if label.startswith("rc")
        else 2
    )
    match = _DIGITS_RE.search(pre)
    return (*nums, 0, rank, int(match.group(1)) if match else 0)


def is_prerelease(tag: Any) -> bool:
    """True when ``tag`` parses and names a pre-release (alpha/beta/rc).

    Decided from the parsed :func:`version_key` rather than a ``"-"``
    substring, so the PEP 440 spelling ``0.1.0b1`` is recognised (NEW-1).
    """
    key = version_key(tag)
    return key is not None and key[_PLAIN_TAIL_INDEX] == 0


def newer(candidate: Any, current: Any) -> bool:
    """True when ``candidate`` parses and is a strictly newer version."""
    candidate_key = version_key(candidate)
    current_key = version_key(current)
    if candidate_key is None or current_key is None:
        return False
    return candidate_key > current_key


def _host_of(url: Any) -> str:
    if not isinstance(url, str):
        return ""
    try:
        return (urlparse(url).hostname or "").lower()
    except ValueError:
        return ""


def _release_link_allowed(html_url: Any, api_url: str) -> bool:
    """True when ``html_url`` is on the configured release host (L9).

    ``html_url`` is the GUI's "Update" button target; a compromised or spoofed
    release endpoint must not be able to surface an arbitrary phishing link.
    """
    host = _host_of(html_url)
    api_host = _host_of(api_url)
    if not host or not api_host:
        return False
    return host == api_host or host == _RELEASE_SITE_ALIASES.get(api_host)


def _same_host_redirect(original: str) -> Callable[[str], bool]:
    """Refuse a redirect that leaves the original update host (M2)."""
    host = _host_of(original)

    def allowed(target: str) -> bool:
        return bool(host) and _host_of(target) == host

    return allowed


def _default_fetch(url: str, timeout: int) -> Any:
    # ``download.fetch_json`` accepts the redirect policy; a caller-injected
    # replacement (tests, embedders) may still have the old two-keyword shape.
    try:
        params = inspect.signature(download.fetch_json).parameters
    except (TypeError, ValueError):  # pragma: no cover - exotic callable
        params = {}
    if "redirect_allowed" in params:
        return download.fetch_json(
            url, timeout=timeout, redirect_allowed=_same_host_redirect(url)
        )
    return download.fetch_json(url, timeout=timeout)


class _Redirect(Exception):
    """Carries the ``Location`` of a redirect that was deliberately not followed."""

    def __init__(self, url: str) -> None:
        super().__init__(url)
        self.url = url


class _StopAtRedirect(urllib.request.HTTPRedirectHandler):
    def redirect_request(self, req, fp, code, msg, headers, newurl):  # noqa: ANN001
        if fp is not None:
            fp.close()
        raise _Redirect(urljoin(req.full_url, newurl))


def _github_latest_redirect(page: str, timeout: int = 10) -> str | None:
    """Where GitHub's ``releases/latest`` page redirects to, without following it.

    The web page is not behind the API's rate limit.
    """
    opener = urllib.request.build_opener(_StopAtRedirect())
    try:
        with opener.open(download._request(page), timeout=timeout):  # noqa: S310
            return None
    except _Redirect as found:
        return found.url
    except Exception:  # noqa: BLE001 - this is only a fallback
        return None


def _payload_from_redirect(url: str, redirect: Callable[[str], str | None]) -> dict | None:
    """A minimal release payload from the ``releases/latest`` redirect, GitHub URLs only."""
    match = _GITHUB_LATEST_API.fullmatch(url)
    if not match:
        return None
    owner, repo = match.groups()
    location = redirect(f"https://github.com/{owner}/{repo}/releases/latest")
    if not location:
        return None
    tag_match = re.fullmatch(
        rf"https://github\.com/{re.escape(owner)}/{re.escape(repo)}/releases/tag/([A-Za-z0-9._+-]+)",
        location,
        re.IGNORECASE,
    )
    if not tag_match or version_key(tag_match.group(1)) is None:
        return None
    return {"tag_name": tag_match.group(1), "html_url": location, "name": tag_match.group(1)}


def _read_cache(url: str) -> dict[str, Any] | None:
    try:
        entry = json.loads((paths.cache_dir() / CACHE_NAME).read_text(encoding="utf-8"))
    except (OSError, ValueError):
        return None
    if not isinstance(entry, dict) or entry.get("url") != url:
        return None
    return entry


def _write_cache(entry: dict[str, Any]) -> None:
    path = paths.cache_dir() / CACHE_NAME
    try:
        path.parent.mkdir(parents=True, exist_ok=True)
        part = path.with_name(f"{path.name}.{os.getpid()}.part")
        part.write_text(json.dumps(entry), encoding="utf-8")
        os.replace(part, path)
    except OSError:
        logger.debug("could not write the update-check cache", exc_info=True)


def _within(stamp: Any, now: float, seconds: float) -> bool:
    return isinstance(stamp, (int, float)) and 0 <= now - stamp < seconds


def _cached_payload(
    url: str,
    timeout: int,
    fetch: Callable[[str, int], Any],
    redirect: Callable[[str], str | None],
    now: float,
) -> Any:
    """The release payload, from the disk cache while fresh and during a failure backoff."""
    entry = _read_cache(url) or {}
    last_good = entry.get("payload") if isinstance(entry.get("payload"), dict) else None
    if last_good is not None and _within(entry.get("ok_at"), now, FRESH_SECONDS):
        return last_good
    if _within(entry.get("failed_at"), now, BACKOFF_SECONDS):
        return last_good
    try:
        payload = fetch(url, timeout)
    except Exception:  # noqa: BLE001 - rate limits and outages are expected here
        logger.debug("update check failed for %s", url, exc_info=True)
        payload = None
    if not isinstance(payload, dict):
        payload = _payload_from_redirect(url, redirect)
    if isinstance(payload, dict):
        _write_cache(
            {
                "url": url,
                "ok_at": now,
                "failed_at": None,
                "payload": {k: payload[k] for k in _PAYLOAD_KEYS if k in payload},
            }
        )
        return payload
    _write_cache({"url": url, "ok_at": entry.get("ok_at"), "failed_at": now, "payload": last_good})
    return last_good


def managed_updates() -> bool:
    """True when a package manager (AUR, Flatpak) installs and updates Nadare.

    Those builds must never send the user to a release page: updating means
    ``pacman -Syu`` or ``flatpak update``. The packaging recipe sets the
    environment marker; Flatpak is recognised by its sandbox as well.
    """
    return os.environ.get("NADARE_MANAGED_UPDATES") == "1" or sandbox.in_flatpak()


def check_latest(
    current: str | None = None,
    *,
    url: str | None = None,
    timeout: int = 10,
    fetch: Callable[[str, int], Any] | None = None,
    cached_fetch: Callable[[str, int], Any] | None = None,
    redirect: Callable[[str], str | None] | None = None,
    now: float | None = None,
) -> UpdateInfo | None:
    """Return the latest release when it is newer than ``current``, else ``None``.

    By default the answer is cached on disk (six hours, one-hour backoff after a failure) and,
    for a GitHub API URL, falls back to the ``releases/latest`` page redirect when the API
    refuses. An injected ``fetch`` bypasses all of that; ``cached_fetch`` is the same with the
    cache on (for tests).
    """
    if current is None:
        import nadare

        current = nadare.__version__

    if url is None:
        url = hosting.releases_api()
    if not url or not url.startswith("https://"):
        return None

    if fetch is not None:
        try:
            payload = fetch(url, timeout)
        except Exception:  # noqa: BLE001 - an update check must never break startup
            logger.debug("update check failed for %s", url, exc_info=True)
            return None
    else:
        payload = _cached_payload(
            url,
            timeout,
            cached_fetch or _default_fetch,
            redirect or _github_latest_redirect,
            time.time() if now is None else now,
        )

    if not isinstance(payload, dict):
        return None
    if payload.get("draft"):
        return None

    tag = payload.get("tag_name")
    if not isinstance(tag, str) or version_key(tag) is None:
        return None

    html_url = payload.get("html_url")
    if not isinstance(html_url, str) or not html_url.startswith("https://"):
        return None
    if not _release_link_allowed(html_url, url):
        logger.debug("release link is not on the configured host: %s", html_url)
        return None

    if payload.get("prerelease") and not is_prerelease(current):
        return None

    if not newer(tag, current):
        return None

    name = payload.get("name")
    if not isinstance(name, str) or not name:
        name = tag
    return UpdateInfo(version=_strip_prefix(tag), url=html_url, name=name)
