# SPDX-License-Identifier: GPL-3.0-or-later
# Copyright (C) 2026 Nadare contributors
"""The "new version available" check.

Compares the running Nadare version against the project's "latest release"
endpoint and reports a newer release when there is one. All network access is
funnelled through :func:`nadare.core.download.fetch_json` and can be replaced
by injecting ``fetch``, so the whole check is testable without a network.
"""

from __future__ import annotations

import logging
import re
from collections.abc import Callable
from dataclasses import dataclass
from typing import Any
from urllib.parse import urlparse

from . import download, hosting

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

#: Position of the "plain release" flag in the three-slot tail of a
#: :func:`version_key` tuple: ``1`` for a final release, ``0`` for a pre-release.
_PLAIN_TAIL_INDEX = -3

__all__ = ["UpdateInfo", "check_latest", "is_prerelease", "newer", "version_key"]


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


def _default_fetch(url: str, timeout: int) -> Any:
    return download.fetch_json(url, timeout=timeout)


def check_latest(
    current: str | None = None,
    *,
    url: str | None = None,
    timeout: int = 10,
    fetch: Callable[[str, int], Any] | None = None,
) -> UpdateInfo | None:
    """Return the latest release when it is newer than ``current``, else ``None``."""
    if current is None:
        import nadare

        current = nadare.__version__

    if url is None:
        url = hosting.releases_api()
    if not url or not url.startswith("https://"):
        return None

    if fetch is None:
        fetch = _default_fetch

    try:
        payload = fetch(url, timeout)
    except Exception:  # noqa: BLE001 - an update check must never break startup
        logger.debug("update check failed for %s", url, exc_info=True)
        return None

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
