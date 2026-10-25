# SPDX-License-Identifier: GPL-3.0-or-later
# Copyright (C) 2026 Nadare contributors
"""Direct-download source for Wuthering Waves.

The launcher token normally comes from Nadare's published token index. The
built-in value is the offline fallback; the profile's ``kuro_token`` overrides
both.
"""

from __future__ import annotations

from .. import directdl
from . import Fetch, kuro

BUILTIN_TOKEN = "obOHXFrFanqsaIEOmuKroCcbZkQRBC7c"
INDEX_URL = (
    "https://prod-alicdn-gamestarter.kurogame.com/launcher/game/G153/"
    f"50004_{BUILTIN_TOKEN}/index.json"
)
EXE = "Wuthering Waves.exe"


def resolve(fetch: Fetch | None = None, token: str | None = None) -> directdl.Plan:
    return kuro.resolve_with_tokens(
        game_tag="wuwa",
        group="G153",
        prefix="50004",
        builtin=BUILTIN_TOKEN,
        token=token,
        exe=EXE,
        fetch=fetch,
        refused_hint=(
            "Kuro may have rotated Wuthering Waves' download token. Update Nadare, or "
            "set the profile's kuro_token (see core/sources/wuwa.py for where to find it)."
        ),
    )
