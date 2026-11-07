# SPDX-License-Identifier: GPL-3.0-or-later
# Copyright (C) 2026 Nadare contributors
"""Direct-download source for Wuthering Waves.

Kuro's launcher 3.0 reads a bundle index (``resourcePacks`` and ``bundles``): a resource
quality (SD, HD or UHD) is the shared ``common`` pack plus one tier pack. Nadare asks that index
first, with the profile's ``wuwa_quality`` (HD by default). HD alone can fall back to the older
single-list index, whose launcher token normally comes from Nadare's published token index; the
built-in value is the offline fallback and the profile's ``kuro_token`` overrides both.
"""

from __future__ import annotations

from .. import directdl
from . import Fetch, kuro

BUILTIN_TOKEN = "obOHXFrFanqsaIEOmuKroCcbZkQRBC7c"
INDEX_URL = (
    "https://prod-alicdn-gamestarter.kurogame.com/launcher/game/G153/"
    f"50004_{BUILTIN_TOKEN}/index.json"
)
#: The key the launcher's own ``KRApp.conf`` carries for the bundle index (a shared launcher
#: value, like the token above, not an account secret).
LAUNCHER_APP_KEY = "P7xcUZnEr1AXIGON25E6KjpOgTlVrg6e"
BUNDLE_INDEX_URL = (
    "https://prod-alicdn-gamestarter.kurogame.com/launcher/game/"
    f"50004_{LAUNCHER_APP_KEY}/G153/official/index.json"
)
EXE = "Wuthering Waves.exe"
QUALITIES = ("sd", "hd", "uhd")
DEFAULT_QUALITY = "hd"


def quality_of(value: object) -> str:
    """The resource quality a profile asks for; anything unknown means the default (HD)."""
    text = str(value or "").strip().lower()
    return text if text in QUALITIES else DEFAULT_QUALITY


_HINT = (
    "Kuro may have changed Wuthering Waves' download index. Update Nadare, or "
    "set the profile's kuro_token (see core/sources/wuwa.py for where to find it)."
)


def resolve_legacy(fetch: Fetch | None = None, token: str | None = None) -> directdl.Plan:
    """The older single-list index (HD only), with the profile, published or built-in token."""
    return kuro.resolve_with_tokens(
        game_tag="wuwa",
        group="G153",
        prefix="50004",
        builtin=BUILTIN_TOKEN,
        token=token,
        exe=EXE,
        fetch=fetch,
        refused_hint=_HINT,
    )


def resolve(
    fetch: Fetch | None = None, token: str | None = None, quality: str = DEFAULT_QUALITY
) -> directdl.Plan:
    tier = quality_of(quality)
    if token and tier == DEFAULT_QUALITY:
        return resolve_legacy(fetch, token)  # the user pinned a token: use exactly that index
    try:
        return kuro.resolve_bundle(
            BUNDLE_INDEX_URL,
            game_tag="wuwa",
            exe=EXE,
            tier=tier,
            fetch=fetch,
            refused_hint=_HINT,
        )
    except directdl.DirectError:
        if tier != DEFAULT_QUALITY:
            raise  # the older index has HD only: never hand out a different quality silently
    return resolve_legacy(fetch, token)
